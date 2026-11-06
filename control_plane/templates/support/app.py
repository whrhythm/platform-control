"""Small invitation-only support desk. DATABASE_URL is supplied by the platform.

Startup only creates missing tables. Rebuilding or restarting never seeds tickets,
resets passwords, deletes records, or replaces an existing database.
"""
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (Column, Integer, String, MetaData, Table, create_engine,
                        select, insert, update, delete)
from sqlalchemy.exc import IntegrityError

metadata = MetaData()
members = Table('support_members', metadata,
    Column('id', String(40), primary_key=True), Column('name', String(80), unique=True, nullable=False),
    Column('role', String(20), nullable=False), Column('password', String(200), nullable=False))
sessions = Table('support_sessions', metadata, Column('id', String(64), primary_key=True),
    Column('member_id', String(40), nullable=False), Column('expires', Integer, nullable=False))
invites = Table('support_invites', metadata, Column('id', String(64), primary_key=True),
    Column('role', String(20), nullable=False), Column('expires', Integer, nullable=False),
    Column('used_by', String(40)))
tickets = Table('support_tickets', metadata,
    Column('id', String(40), primary_key=True), Column('title', String(160), nullable=False),
    Column('customer', String(80), nullable=False), Column('description', String(4000), nullable=False),
    Column('assignee', String(40)), Column('status', String(20), nullable=False),
    Column('created_at', Integer, nullable=False), Column('updated_at', Integer, nullable=False),
    Column('revision', Integer, nullable=False))


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + digest


def password_matches(password, value):
    try: return hmac.compare_digest(password_hash(password, value.split(':')[0]), value)
    except (ValueError, TypeError): return False


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(Input):
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=10, max_length=128)

    @field_validator('name')
    @classmethod
    def nonempty_name(cls, value):
        if not value.strip(): raise ValueError('请输入用户名')
        return value.strip()


class Join(Login):
    invitation: str = Field(min_length=20, max_length=100)


class Invitation(Input):
    role: str = Field(pattern='^(agent|viewer)$')


class Ticket(Input):
    title: str = Field(min_length=1, max_length=160)
    customer: str = Field(min_length=1, max_length=80)
    description: str = Field(default='', max_length=4000)
    assignee: str | None = Field(default=None, max_length=40)
    status: str = Field(default='open', pattern='^(open|working|resolved)$')


class Edit(Ticket):
    revision: int = Field(ge=1)


def create_app(database_url=None, config=None):
    root = Path(__file__).parent
    config = config or json.loads((root / 'settings.json').read_text())
    database_url = database_url or os.environ['DATABASE_URL']
    if database_url.startswith('postgresql://'):
        database_url = database_url.replace('postgresql://', 'postgresql+psycopg://', 1)
    engine = create_engine(database_url, pool_pre_ping=True)

    @asynccontextmanager
    async def lifespan(_app):
        metadata.create_all(engine)
        try:
            with engine.begin() as db:
                if not db.execute(select(members.c.id).where(members.c.name == 'owner')).first():
                    db.execute(insert(members).values(id='owner', name='owner', role='owner', password=config['owner_password_hash']))
        except IntegrityError:
            # Two replicas may start together; only the unique owner insertion wins.
            pass
        yield
        engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def boundaries(request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and urlsplit(origin).netloc != request.headers.get('host'):
                return Response(status_code=403)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    def identity(request, db, roles=None):
        token = request.cookies.get('support_session', '')
        person = db.execute(select(members.c.id, members.c.name, members.c.role)
            .join(sessions, sessions.c.member_id == members.c.id).where(
                sessions.c.id == fingerprint(token), sessions.c.expires > int(time.time()))).mappings().first()
        if not person: raise HTTPException(401, '请先登录')
        if roles and person['role'] not in roles: raise HTTPException(403, '当前角色没有此权限')
        return dict(person)

    def begin_session(db, person, response, request):
        token = secrets.token_urlsafe(32)
        db.execute(delete(sessions).where(sessions.c.expires < int(time.time())))
        db.execute(insert(sessions).values(id=fingerprint(token), member_id=person, expires=int(time.time()) + 86400))
        secure = request.url.scheme == 'https' or request.headers.get('x-forwarded-proto') == 'https'
        response.set_cookie('support_session', token, httponly=True, samesite='strict', secure=secure, max_age=86400)

    @app.get('/', response_class=HTMLResponse)
    def home(): return (root / 'index.html').read_text()

    @app.get('/api/info')
    def info(): return {'title': config['title'], 'version': config.get('version', '1')}

    @app.post('/api/login')
    def login(body: Login, response: Response, request: Request):
        with engine.begin() as db:
            person = db.execute(select(members).where(members.c.name == body.name)).mappings().first()
            if not person or not password_matches(body.password, person['password']):
                raise HTTPException(401, '用户名或口令不正确')
            begin_session(db, person['id'], response, request)
        return {'ok': True}

    @app.post('/api/logout')
    def logout(request: Request, response: Response):
        with engine.begin() as db:
            db.execute(delete(sessions).where(sessions.c.id == fingerprint(request.cookies.get('support_session', ''))))
        response.delete_cookie('support_session')
        return {'ok': True}

    @app.get('/api/me')
    def me(request: Request):
        with engine.connect() as db: return identity(request, db)

    @app.get('/api/members')
    def people(request: Request):
        with engine.connect() as db:
            identity(request, db)
            return [dict(row) for row in db.execute(select(members.c.id, members.c.name, members.c.role)).mappings()]

    @app.post('/api/invitations')
    def invite(body: Invitation, request: Request):
        token = secrets.token_urlsafe(32)
        with engine.begin() as db:
            identity(request, db, ('owner',))
            db.execute(insert(invites).values(id=fingerprint(token), role=body.role, expires=int(time.time()) + 7*86400))
        return {'invitation': token, 'expires_in_days': 7}

    @app.post('/api/join')
    def join(body: Join, request: Request, response: Response):
        try:
            with engine.begin() as db:
                row = db.execute(select(invites).where(invites.c.id == fingerprint(body.invitation)).with_for_update()).mappings().first()
                if not row or row['used_by'] or row['expires'] <= int(time.time()):
                    raise HTTPException(409, '邀请已使用或过期')
                identifier = secrets.token_hex(16)
                db.execute(insert(members).values(id=identifier, name=body.name.strip(), role=row['role'], password=password_hash(body.password)))
                changed = db.execute(update(invites).where(invites.c.id == row['id'], invites.c.used_by.is_(None)).values(used_by=identifier))
                if changed.rowcount != 1: raise HTTPException(409, '邀请已使用')
                begin_session(db, identifier, response, request)
        except IntegrityError: raise HTTPException(409, '用户名已存在') from None
        return {'ok': True}

    def filtered(status, assignee):
        query = select(tickets).order_by(tickets.c.created_at.desc(), tickets.c.id)
        if status: query = query.where(tickets.c.status == status)
        if assignee: query = query.where(tickets.c.assignee == assignee)
        return query

    @app.get('/api/tickets')
    def listing(request: Request, status: str = '', assignee: str = ''):
        with engine.connect() as db:
            identity(request, db)
            rows = list(db.execute(filtered(status, assignee).limit(501)).mappings())
            return {'tickets': [dict(row) for row in rows[:500]], 'truncated': len(rows) > 500}

    def validate_ticket(db, body):
        if not body.title.strip() or not body.customer.strip(): raise HTTPException(422, '标题和客户不能为空')
        if body.assignee and not db.execute(select(members.c.id).where(
            members.c.id == body.assignee, members.c.role.in_(('owner', 'agent')))).first():
            raise HTTPException(422, '请选择有效处理人')

    @app.post('/api/tickets', status_code=201)
    def create(body: Ticket, request: Request):
        with engine.begin() as db:
            identity(request, db, ('owner', 'agent')); validate_ticket(db, body)
            identifier = secrets.token_hex(16)
            db.execute(insert(tickets).values(**body.model_dump(), id=identifier,
                created_at=int(time.time()), updated_at=int(time.time()), revision=1))
        return {'id': identifier}

    @app.put('/api/tickets/{identifier}')
    def edit(identifier: str, body: Edit, request: Request):
        with engine.begin() as db:
            identity(request, db, ('owner', 'agent')); validate_ticket(db, body)
            changed = db.execute(update(tickets).where(tickets.c.id == identifier, tickets.c.revision == body.revision)
                .values(**body.model_dump(exclude={'revision'}), revision=body.revision+1, updated_at=int(time.time())))
            if changed.rowcount != 1: raise HTTPException(409, '工单已变化，请刷新后再修改')
        return {'ok': True}

    @app.get('/api/export')
    def export(request: Request, status: str = '', assignee: str = ''):
        with engine.connect() as db:
            identity(request, db, ('owner',))
            rows = list(db.execute(filtered(status, assignee).limit(5001)).mappings())
        if len(rows) > 5000: raise HTTPException(413, '请缩小筛选范围后导出')
        output = io.StringIO(); writer = csv.writer(output)
        writer.writerow(['工单编号', '标题', '客户', '描述', '处理人编号', '状态'])
        for row in rows:
            values = [str(row[key] or '') for key in ('id', 'title', 'customer', 'description', 'assignee', 'status')]
            writer.writerow(["'"+v if v.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in values])
        return Response('\ufeff'+output.getvalue(), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="support-tickets.csv"'})

    return app


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(create_app(), host='0.0.0.0', port=8080, access_log=False)
