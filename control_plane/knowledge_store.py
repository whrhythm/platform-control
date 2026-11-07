"""Bounded private retrieval service. Immutable revision writes and deletion tombstones."""
import hmac
import math
import os
from contextlib import asynccontextmanager
from uuid import UUID

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .knowledge_format import prepare, DIMENSIONS, FORMAT


class File(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(max_length=124)
    content: str = Field(max_length=1024*1024)


class Ingest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    files: list[File] = Field(min_length=1, max_length=10)
    digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class Query(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=2000)
    k: int = Field(default=5, ge=1, le=10)


def vector(value):
    numbers = [float(v) for v in value]
    if len(numbers) != DIMENSIONS or not all(math.isfinite(n) for n in numbers):
        raise ValueError('invalid_embedding')
    return '['+','.join(str(n) for n in numbers)+']'


def create_app(connect=None, embed=None, secret=None):
    connect = connect or (lambda: psycopg.connect(os.environ['KNOWLEDGE_DATABASE_URL'],
        connect_timeout=10, options='-c statement_timeout=180000 -c lock_timeout=10000'))
    key = secret or os.environ.get('KNOWLEDGE_SERVICE_TOKEN', '')
    if len(key) < 32:
        raise RuntimeError('knowledge_service_key_required')
    encoder = embed

    @asynccontextmanager
    async def lifespan(app):
        nonlocal encoder
        if encoder is None:
            from fastembed import TextEmbedding
            model = TextEmbedding('BAAI/bge-small-zh-v1.5', threads=2,
                specific_model_path=os.environ['KNOWLEDGE_MODEL_PATH'], local_files_only=True)
            encoder = lambda texts: list(model.embed(texts, batch_size=16))
            vector(encoder(['启动自检'])[0])
        # Schema provision is an explicit deployment migration, not a side effect of requests.
        with connect() as db:
            db.execute('SELECT revision FROM knowledge_revisions LIMIT 0')
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def auth(authorization: str = Header(default='')):
        if not hmac.compare_digest(authorization, 'Bearer '+key):
            raise HTTPException(403, 'knowledge_forbidden')

    def locked(db, scope, revision):
        db.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (str(scope)+str(revision),))
        return db.execute('SELECT digest, deleted, source_bytes, index_bytes, chunk_count FROM knowledge_revisions '
                          'WHERE scope=%s AND revision=%s', (scope, revision)).fetchone()

    @app.get('/health')
    def health():
        with connect() as db:
            db.execute('SELECT 1')
        return {'status': 'ready', 'format': FORMAT}

    @app.get('/v1/{scope}/{revision}', dependencies=[Depends(auth)])
    def status(scope: UUID, revision: UUID):
        with connect() as db:
            row = locked(db, scope, revision)
        if not row or row[1]:
            raise HTTPException(404, 'knowledge_not_found')
        return dict(digest=row[0], source_bytes=row[2], index_bytes=row[3], chunks=row[4], format=FORMAT)

    @app.put('/v1/{scope}/{revision}', dependencies=[Depends(auth)])
    def ingest(scope: UUID, revision: UUID, body: Ingest):
        try:
            digest, source_bytes, index_bytes, chunks = prepare([f.model_dump() for f in body.files])
        except ValueError:
            raise HTTPException(400, 'knowledge_invalid_input') from None
        if body.digest != digest:
            raise HTTPException(409, 'knowledge_digest_mismatch')
        with connect() as db:
            row = locked(db, scope, revision)
            if row and (row[1] or row[0] != digest):
                raise HTTPException(409, 'knowledge_revision_conflict')
            if not row:
                embeddings = list(encoder([c['text'] for c in chunks]))
                if len(embeddings) != len(chunks):
                    raise HTTPException(503, 'knowledge_embedding_failed')
                db.execute('INSERT INTO knowledge_revisions VALUES (%s,%s,%s,false,%s,%s,%s)',
                           (scope, revision, digest, source_bytes, index_bytes, len(chunks)))
                with db.cursor() as cursor:
                    cursor.executemany('INSERT INTO knowledge_chunks (scope,revision,ordinal,filename,line,content,embedding) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s::vector)', [
                            (scope, revision, c['ordinal'], c['file'], c['line'], c['text'], vector(v))
                            for c, v in zip(chunks, embeddings)])
        return dict(digest=digest, source_bytes=source_bytes, index_bytes=index_bytes, chunks=len(chunks), format=FORMAT)

    @app.delete('/v1/{scope}/{revision}', dependencies=[Depends(auth)])
    def delete(scope: UUID, revision: UUID):
        with connect() as db:
            row = locked(db, scope, revision)
            if not row:
                db.execute('INSERT INTO knowledge_revisions VALUES (%s,%s,%s,true,0,0,0)', (scope, revision, ''))
            else:
                db.execute('UPDATE knowledge_revisions SET deleted=true,source_bytes=0,index_bytes=0,chunk_count=0 '
                           'WHERE scope=%s AND revision=%s', (scope, revision))
                db.execute('DELETE FROM knowledge_chunks WHERE scope=%s AND revision=%s', (scope, revision))
        return {'deleted': True}

    @app.post('/v1/{scope}/{revision}/query', dependencies=[Depends(auth)])
    def query(scope: UUID, revision: UUID, body: Query):
        embedded = vector(encoder(['为这个句子生成表示以用于检索相关文章：'+body.query])[0])
        with connect() as db:
            rows = db.execute('SELECT filename,line,content,ordinal,embedding <=> %s::vector AS distance '
                'FROM knowledge_chunks WHERE scope=%s AND revision=%s ORDER BY distance,ordinal LIMIT %s',
                (embedded, scope, revision, body.k)).fetchall()
        return {'sources': [dict(name=r[0], line=r[1], content=r[2], ordinal=r[3], distance=r[4]) for r in rows]}

    return app
