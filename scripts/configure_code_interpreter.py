"""Generate public Helm values only; signing keys stay in Kubernetes Secrets."""
import json
from pathlib import Path
from cluster_transport import remote

root=Path(__file__).resolve().parents[1]
public=json.loads((root/'release/codeapi-public.json').read_text())
tag='codeapi-20261105.1'
def image(name):
    result=remote(['cat','/home/edge/platform-releases/'+tag+'/'+name+'.json'])
    value=json.loads(result)
    digest=value['repo_digests'][0].split('@')[1]
    return {'repository':'harbor.k3s.local/internal/platform-control','tag':tag+'-'+name+'@'+digest,'pullPolicy':'IfNotPresent'}
env={'LOCAL_MODE':'false','CODEAPI_AUTH_PROVIDER':'librechat-jwt','CODEAPI_JWT_ISSUER':'librechat',
     'CODEAPI_JWT_AUDIENCE':'codeapi','CODEAPI_JWT_ALLOWED_ALGS':'EdDSA','CODEAPI_JWT_KID':'starbridge-codeapi-20261105',
     'CODEAPI_JWT_MAX_TTL_SECONDS':'300','CODEAPI_JWT_SINGLE_TENANT_ID':'legacy','CODEAPI_JWT_PUBLIC_KEY':public['jwt']}
values={'fullnameOverride':'codeapi','hardenedSandboxMode':True,'executionProfile':'default',
    'executionManifest':{'privateKey':'managed-by-codeapi-secrets','publicKey':''.join(public['manifest'].splitlines()[1:-1])},
    'api':{'replicaCount':1,'image':image('api'),'httpJsonLimit':'4mb','extraEnv':[{'name':k,'value':v} for k,v in env.items()]},
    'workerSandbox':{'replicaCount':1,'workerImage':image('worker'),'sandboxImage':image('runner'),
        'kvmEnabled':True,'kvmGid':108,'config':{'pythonConcurrency':1,'otherConcurrency':1,'jobTimeout':25000},
        'kvmDevicePlugin':{'enabled':True,'resourceName':'starbridge.internal/kvm'},
        'sandbox':{'maxConcurrentJobs':2,'executeBodyLimit':'4mb','workspaceReaperMaxAgeSeconds':1800,'inputCacheMaxBytes':134217728}},
    'fileServer':{'image':image('files')},'toolCallServer':{'image':image('tools')},'egressGateway':{'image':image('egress')},
    'redis':{'enabled':False,'external':{'host':'codeapi-redis','port':6379}},
    'minio':{'enabled':False,'external':{'endpoint':'minio.minio.svc.cluster.local','port':9000,'bucket':'starbridge-codeapi-files','useSSL':False}},
    'networkPolicy':{'enabled':True}}
(root.parent/'ops/apps/codeapi/values.yaml').write_text(json.dumps(values,indent=2)+'\n')
print('RESULT:Public values generated with pinned image digests; no credentials included')
