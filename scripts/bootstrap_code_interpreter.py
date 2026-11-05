"""Provision CodeAPI credentials and a bounded, dedicated shared-MinIO bucket."""
import base64
import json
import shlex
import secrets
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from cluster_transport import remote

def secret(namespace, name):
    result = remote(['kubectl','-n',namespace,'get','secret',name,'--ignore-not-found','-o','json'])
    if not result.strip(): return None
    return {k:base64.b64decode(v).decode() for k,v in json.loads(result)['data'].items()}

def install(namespace, name, data):
    remote(['kubectl','apply','-f','-'],json.dumps({'apiVersion':'v1','kind':'Secret','type':'Opaque',
        'metadata':{'name':name,'namespace':namespace},'stringData':data}),redact=tuple(data.values()))

def private_key():
    return Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()

remote(['kubectl','apply','-f','-'],json.dumps({'apiVersion':'v1','kind':'Namespace','metadata':{'name':'codeapi'}}))
data=secret('codeapi','codeapi-secrets')
if data is None:
    data={'codeapi-internal-service-token':secrets.token_urlsafe(48),
        'codeapi-egress-grant-secret':secrets.token_urlsafe(48),
        'codeapi-execution-manifest-private-key':private_key(),
        'redis-password':secrets.token_urlsafe(32),
        'minio-access-key':'starbridge-codeapi','minio-secret-key':secrets.token_urlsafe(32)}
    install('codeapi','codeapi-secrets',data)
lc=secret('librechat','librechat-codeapi')
if lc is None:
    lc={'CODEAPI_JWT_PRIVATE_KEY':private_key()}
    install('librechat','librechat-codeapi',lc)
manifest_key=serialization.load_pem_private_key(data['codeapi-execution-manifest-private-key'].encode(),password=None).public_key()
jwt_key=serialization.load_pem_private_key(lc['CODEAPI_JWT_PRIVATE_KEY'].encode(),password=None).public_key()
public={'manifest':manifest_key.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
        'jwt':jwt_key.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode()}
(Path(__file__).resolve().parents[1]/'release/codeapi-public.json').write_text(json.dumps(public))

storage_code='''
import json,sys,urllib3
from minio import Minio,MinioAdmin
from minio.credentials import StaticProvider
from minio.lifecycleconfig import LifecycleConfig,Rule,Expiration
from minio.commonconfig import ENABLED,Filter
from control_plane.object_storage_provider import missing_admin_resource,canonical_policy
from minio.error import MinioAdminException
p=json.load(sys.stdin)
pool=urllib3.PoolManager(timeout=urllib3.Timeout(connect=3,read=10),retries=False)
a=MinioAdmin(endpoint='127.0.0.1:19001',credentials=StaticProvider(p['root-user'],p['root-password']),secure=False,http_client=pool)
c=Minio('127.0.0.1:19001',access_key=p['root-user'],secret_key=p['root-password'],secure=False,http_client=pool)
bucket='starbridge-codeapi-files'
if not c.bucket_exists(bucket): c.make_bucket(bucket)
policy={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['s3:GetBucketLocation','s3:ListBucket','s3:ListBucketMultipartUploads'],'Resource':['arn:aws:s3:::'+bucket]},{'Effect':'Allow','Action':['s3:GetObject','s3:PutObject','s3:DeleteObject','s3:AbortMultipartUpload','s3:ListMultipartUploadParts'],'Resource':['arn:aws:s3:::'+bucket+'/*']}]}
name='starbridge-codeapi-files-v1'
try:
 existing=json.loads(a.policy_info(name)); existing=existing.get('policy',existing)
 if isinstance(existing,str):existing=json.loads(existing)
 assert canonical_policy(existing)==canonical_policy(policy)
except MinioAdminException as e:
 if not missing_admin_resource(e,('XMinioAdminNoSuchPolicy','NoSuchPolicy')):raise
 a.policy_add(name,policy=policy)
try:info=json.loads(a.user_info(p['minio-access-key']))
except MinioAdminException as e:
 if not missing_admin_resource(e,('XMinioAdminNoSuchUser','NoSuchUser')):raise
 a.user_add(p['minio-access-key'],p['minio-secret-key']);info={}
assert info.get('policyName') in (None,'',name)
a.policy_set(name,user=p['minio-access-key'])
a.bucket_quota_set(bucket,2*1024**3)
c.set_bucket_lifecycle(bucket,LifecycleConfig([Rule(ENABLED,rule_filter=Filter(prefix=''),rule_id='temporary-code-results',expiration=Expiration(days=1))]))
client=Minio('127.0.0.1:19001',access_key=p['minio-access-key'],secret_key=p['minio-secret-key'],secure=False,http_client=pool)
assert client.bucket_exists(bucket)
print('RESULT:CodeAPI storage verified; dedicated 2 GiB bucket, one-day expiration, no root credentials in runtime')
'''
root=secret('minio','minio-root')
payload={**root,**{k:data[k] for k in ('minio-access-key','minio-secret-key')}}
image='harbor.k3s.local/internal/platform-control@sha256:864419863258efe2aea5fa96989575e09da8107b5a5ab38343742bb91fcd459c'
command="kubectl -n minio port-forward --address=127.0.0.1 svc/minio 19001:9000 >/tmp/codeapi-storage-forward.log 2>&1 & forward_pid=$!; trap 'kill $forward_pid 2>/dev/null || true' EXIT; sleep 2; kill -0 $forward_pid || exit 1; docker run --rm -i --network=host "+shlex.quote(image)+' python -c '+shlex.quote(storage_code)
result=remote(['/bin/sh','-c',command],json.dumps(payload),redact=tuple(payload.values()))
print('\n'.join(line for line in result.splitlines() if line.startswith('RESULT:')))
