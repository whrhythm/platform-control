"""Owner-scoped, bounded encrypted source versions. Existing files are never replaced."""
import base64
import difflib
import hashlib
import json
import zlib
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from .database import get_db
from .models import AgentTask, AuditEvent, CapacityAccount, ProjectRestore, ProjectVersion
from .settings import get_settings
from .workspace_gateway import own_environment, call_files
from .workspace_checkpoints import MAX_BYTES, EXCLUDED, encode, validate
from .workspace_files import WorkspaceFiles


class CreateVersion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    path: str = Field(min_length=1,max_length=255)
    name: str = Field(min_length=1,max_length=80)


class RestoreVersion(BaseModel):
    model_config = ConfigDict(extra='forbid')
    target: str = Field(min_length=1,max_length=255)


def version_usage(db, environment_id):
    return db.scalar(select(func.coalesce(func.sum(ProjectVersion.stored_bytes),0)).where(
        ProjectVersion.environment_id==environment_id, ProjectVersion.deleted.is_(False)))


def public(row):
    return {'id':row.id, 'path':row.project_path, 'name':row.name, 'digest':row.digest,
        'stored_bytes':row.stored_bytes,'source_bytes':row.source_bytes,'created_at':row.created_at,
        'files':len(row.manifest.get('entries',{})), 'excluded':row.manifest.get('excluded',[])}


def unpack(row,settings):
    try:
        compressed=Fernet(settings.model_credential_encryption_key.encode()).decrypt(row.encrypted_content)
    except (InvalidToken, ValueError):
        raise HTTPException(409,'versions_integrity_error') from None
    decoder=zlib.decompressobj()
    content=decoder.decompress(compressed,8*1024*1024+1)
    if len(content)>8*1024*1024 or not decoder.eof or decoder.unused_data:
        raise HTTPException(409,'versions_integrity_error')
    if hashlib.sha256(content).hexdigest()!=row.digest:
        raise HTTPException(409,'versions_integrity_error')
    value=json.loads(content);validate(value)
    return value


def create_router(principal_dependency):
    router=APIRouter(prefix='/internal/v1/workspace/versions',tags=['project-versions'])
    def own(db,principal, *, idle=False):
        settings=get_settings()
        if not settings.project_versions_enabled:
            raise HTTPException(503,'versions_disabled')
        env=own_environment(db,principal);db.refresh(env,with_for_update=True)
        if env.status=='suspended':raise HTTPException(403,'versions_account_suspended')
        if idle and db.scalar(select(AgentTask.id).where(AgentTask.environment_id==env.id,
                AgentTask.status.in_(('queued','running','awaiting_approval','cancelling'))).limit(1)):
            raise HTTPException(409,'versions_agent_active')
        return env,settings
    def find(db,env,identifier):
        row=db.get(ProjectVersion,str(identifier))
        if row is None or row.environment_id!=env.id or row.deleted:
            raise HTTPException(404,'versions_not_found')
        return row
    def audit(db,env,principal,action,identifier):
        db.add(AuditEvent(tenant_id=env.first_tenant_id,issuer=principal.issuer,subject=principal.subject,
            action='versions.'+action,target_id=identifier))
    def quota(db,env,principal,settings,additional):
        observed=call_files(db,principal,settings,'GET','/v1/usage','').json()
        from .capacity import sample_storage
        samples=sample_storage(db,env,observed)
        if samples['files'].status!='ok':raise HTTPException(503,'versions_usage_unavailable')
        used=sum(max(0,item.used_bytes or 0) for item in samples.values())+version_usage(db,env.id)
        account=db.get(CapacityAccount,env.id)
        budget=(account.plan['storage_gib'] if account else env.storage_gib)*1024**3
        if used+additional>budget:raise HTTPException(507,'versions_storage_full')
    def source(db,env,principal,settings,path):
        try:WorkspaceFiles._parts(path)
        except ValueError:raise HTTPException(400,'versions_invalid_path') from None
        try:
            value=call_files(db,principal,settings,'GET','/v1/checkpoint',path).json()
        except HTTPException as error:
            code={409:'versions_source_changed',413:'versions_source_too_large',404:'versions_source_missing'}.get(error.status_code,'versions_unavailable')
            raise HTTPException(error.status_code,code) from None
        try:validate(value)
        except (ValueError,TypeError,KeyError):raise HTTPException(503,'versions_invalid_snapshot') from None
        return value

    @router.get('')
    def listing(path: str=Query(min_length=1,max_length=255),principal=Depends(principal_dependency),db=Depends(get_db)):
        env,settings=own(db,principal)
        rows=db.scalars(select(ProjectVersion).where(ProjectVersion.environment_id==env.id,
            ProjectVersion.project_path==path,ProjectVersion.deleted.is_(False)).order_by(ProjectVersion.created_at.desc()))
        restores=db.execute(select(ProjectRestore.id,ProjectRestore.version_id,ProjectRestore.target,ProjectRestore.status,ProjectRestore.request_key)
            .join(ProjectVersion,ProjectRestore.version_id==ProjectVersion.id).where(ProjectRestore.environment_id==env.id,
                ProjectVersion.project_path==path,ProjectVersion.deleted.is_(False)).order_by(ProjectRestore.created_at.desc()).limit(20)).mappings().all()
        return {'versions':[public(row) for row in rows],'restores':[dict(row) for row in restores],
            'used_bytes':version_usage(db,env.id),'limit':settings.project_versions_limit,'max_source_bytes':MAX_BYTES,
            'excluded':sorted(EXCLUDED)}

    @router.post('',status_code=201)
    def create(body:CreateVersion,principal=Depends(principal_dependency),db=Depends(get_db),
               idempotency_key:str=Header(min_length=16,max_length=128,pattern=r'^[a-zA-Z0-9_-]+$')):
        env,settings=own(db,principal,idle=True)
        previous=db.scalar(select(ProjectVersion).where(ProjectVersion.environment_id==env.id,ProjectVersion.request_key==idempotency_key))
        if previous:
            if previous.project_path!=body.path or previous.name!=body.name or previous.deleted:
                raise HTTPException(409,'versions_request_conflict')
            return public(previous)
        count=db.scalar(select(func.count()).select_from(ProjectVersion).where(ProjectVersion.environment_id==env.id,ProjectVersion.deleted.is_(False)))
        if count>=settings.project_versions_limit:raise HTTPException(409,'versions_count_limit')
        value=source(db,env,principal,settings,body.path)
        content=encode(value)
        encrypted=Fernet(settings.model_credential_encryption_key.encode()).encrypt(zlib.compress(content))
        quota(db,env,principal,settings,len(encrypted))
        manifest={'entries':{path:{k:v for k,v in item.items() if k!='data'} for path,item in value['entries'].items()},'excluded':value['excluded']}
        row=ProjectVersion(environment_id=env.id,request_key=idempotency_key,project_path=body.path,name=body.name,
            digest=hashlib.sha256(content).hexdigest(),manifest=manifest,encrypted_content=encrypted,
            stored_bytes=len(encrypted),source_bytes=value['bytes'])
        db.add(row);db.flush();audit(db,env,principal,'created',row.id);db.commit()
        return public(row)

    @router.get('/{identifier}/diff')
    def diff(identifier:UUID,principal=Depends(principal_dependency),db=Depends(get_db)):
        env,settings=own(db,principal,idle=True);row=find(db,env,identifier)
        before=unpack(row,settings)['entries']
        try:after=source(db,env,principal,settings,row.project_path)['entries']
        except HTTPException as error:
            if error.status_code!=404:raise
            after={}
        changes=[];remaining=128*1024
        for path in sorted(set(before)|set(after)):
            old,new=before.get(path),after.get(path)
            if old==new:continue
            kind='added' if old is None else 'removed' if new is None else 'modified'
            patch=None;limited=False
            if all(item is None or item['kind']=='file' and item['size']<=64*1024 for item in (old,new)):
                try:
                    left=base64.b64decode(old['data']).decode('utf8') if old else ''
                    right=base64.b64decode(new['data']).decode('utf8') if new else ''
                    if '\x00' not in left+right:
                        text=''.join(difflib.unified_diff(left.splitlines(True),right.splitlines(True),fromfile='version/'+path,tofile='current/'+path))
                        patch=text[:min(remaining,12000)];limited=len(patch)!=len(text);remaining-=len(patch)
                except UnicodeDecodeError:pass
            changes.append({'path':path,'change':kind,'diff':patch,'truncated':limited})
        return {'version':public(row),'changes':changes,'unchanged':len(set(before)&set(after))-sum(item['change']=='modified' for item in changes)}

    @router.post('/{identifier}/restore')
    def restore(identifier:UUID,body:RestoreVersion,principal=Depends(principal_dependency),db=Depends(get_db),
                idempotency_key:str=Header(min_length=16,max_length=128,pattern=r'^[a-zA-Z0-9_-]+$')):
        env,settings=own(db,principal,idle=True);row=find(db,env,identifier)
        try:
            if len(WorkspaceFiles._parts(body.target))!=1:raise ValueError()
        except ValueError:raise HTTPException(400,'versions_invalid_target') from None
        operation=db.scalar(select(ProjectRestore).where(ProjectRestore.environment_id==env.id,ProjectRestore.request_key==idempotency_key))
        if operation and (operation.version_id!=row.id or operation.target!=body.target):
            raise HTTPException(409,'versions_request_conflict')
        if operation is None:
            operation=ProjectRestore(environment_id=env.id,version_id=row.id,request_key=idempotency_key,target=body.target)
            db.add(operation);db.flush();audit(db,env,principal,'restore_requested',operation.id);db.commit()
            env,settings=own(db,principal,idle=True)
        if operation.status=='ready':return {'id':operation.id,'status':'ready','target':operation.target}
        try:
            state=call_files(db,principal,settings,'GET','/v1/checkpoint-status',body.target,
                params={'request_id':operation.id,'digest':row.digest}).json()
            if not state.get('restored'):
                quota(db,env,principal,settings,row.source_bytes+4096)
                payload=unpack(row,settings)
                call_files(db,principal,settings,'POST','/v1/checkpoint-restore',body.target,
                    body=encode({'payload':payload,'request_id':operation.id}))
            operation.status='ready';audit(db,env,principal,'restored',operation.id);db.commit()
        except HTTPException as error:
            operation.status='unknown' if error.status_code>=500 and error.status_code!=507 else 'blocked';db.commit()
            code='versions_restore_uncertain' if operation.status=='unknown' else 'versions_storage_full' if error.status_code==507 else 'versions_restore_blocked'
            raise HTTPException(error.status_code,code) from None
        return {'id':operation.id,'status':operation.status,'target':operation.target}

    @router.delete('/{identifier}')
    def remove(identifier:UUID,principal=Depends(principal_dependency),db=Depends(get_db)):
        env,_=own(db,principal);row=find(db,env,identifier)
        if db.scalar(select(ProjectRestore.id).where(ProjectRestore.version_id==row.id,ProjectRestore.status.in_(('pending','unknown'))).limit(1)):
            raise HTTPException(409,'versions_restore_pending')
        row.deleted=True;row.encrypted_content=b'';row.stored_bytes=0
        audit(db,env,principal,'deleted',row.id);db.commit()
        return {'deleted':True}
    return router
