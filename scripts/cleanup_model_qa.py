"""Remove only the two identities created by enrollment_acceptance.cjs."""
import json
from pathlib import Path
import re
from psycopg import sql
from cluster_transport import remote

root = Path(__file__).resolve().parents[1]
credentials = json.loads((root / 'release/live-qa-credentials.json').read_text())
results = json.loads((root / 'release/enrollment-results.json').read_text())
run = credentials['run']
if not re.fullmatch(r'[0-9a-f]{12}', run) or results['run'] != run or len(results['identities']) != 2:
    raise RuntimeError('QA run identity mismatch; refusing cleanup')
if [u['username'] for u in credentials['users']] != [f'platform-qa-{run}-a', f'platform-qa-{run}-b']:
    raise RuntimeError('Unexpected QA usernames; refusing cleanup')
identities = [{'issuer': item['issuer'].rstrip('/') + '/', 'subject': item['subject']} for item in results['identities']]
if len({(i['issuer'], i['subject']) for i in identities}) != 2:
    raise RuntimeError('Expected distinct identities')

# Preflight before revoking anything: newer control-plane versions have an
# environment record, and cleanup must never implicitly delete a real sandbox.
preflight = []
for identity in identities:
    issuer = sql.Literal(identity['issuer']).as_string()
    subject = sql.Literal(identity['subject']).as_string()
    preflight.append(f"DO $qa$ BEGIN IF EXISTS (SELECT 1 FROM user_environments WHERE issuer={issuer} AND subject={subject}) OR EXISTS (SELECT 1 FROM projects p JOIN tenants t ON p.tenant_id=t.id WHERE t.owner_issuer={issuer} AND t.owner_subject={subject}) OR EXISTS (SELECT 1 FROM resources r JOIN tenants t ON r.tenant_id=t.id WHERE t.owner_issuer={issuer} AND t.owner_subject={subject}) OR EXISTS (SELECT 1 FROM test_orders o JOIN tenants t ON o.tenant_id=t.id WHERE t.owner_issuer={issuer} AND t.owner_subject={subject}) THEN RAISE EXCEPTION 'QA identity owns resources; refusing automatic cleanup'; END IF; END $qa$;")
remote(['kubectl', '-n', 'postgres', 'exec', '-i', 'postgres-1', '-c', 'postgres', '--', 'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'platform_control'], '\n'.join(preflight))

program = '''
import hashlib,json,os,urllib.parse,urllib.request,urllib.error
identities=json.loads(PAYLOAD)
def call(path, body=None):
    headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY']}
    data=None if body is None else json.dumps(body).encode()
    if data: headers['Content-Type']='application/json'
    try:
        with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:4000'+path, data=data, headers=headers), timeout=30) as r:
            return r.status,json.load(r)
    except urllib.error.HTTPError as e:
        if e.code==404: return 404,{}
        raise
for identity in identities:
    digest=hashlib.sha256(json.dumps([identity['issuer'],identity['subject']],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    user='codechat-'+digest
    status,data=call('/user/info?'+urllib.parse.urlencode({'user_id':user}))
    if status==404: continue
    info=data['user_info']
    assert info['user_id']==user and info['metadata']['managed_by']=='codechat' and info['metadata']['identity_hash']==digest
    keys=data.get('keys',[])
    assert all(k['user_id']==user for k in keys)
    if keys:
        status,_=call('/key/delete',{'keys':[k['token'] for k in keys]})
        assert status==200
    status,_=call('/user/delete',{'user_ids':[user]})
    assert status==200
    status,_=call('/user/info?'+urllib.parse.urlencode({'user_id':user}))
    assert status==404
print('RESULT:Temporary LiteLLM users and keys removed')
'''.replace('PAYLOAD', repr(json.dumps(identities)))
out = remote(['kubectl', '-n', 'litellm', 'exec', '-i', 'deployment/litellm', '--', 'python', '-'], program)
if 'RESULT:Temporary LiteLLM users and keys removed' not in out:
    raise RuntimeError('Provider cleanup not confirmed')

statements = ['BEGIN;']
for identity in identities:
    issuer = sql.Literal(identity['issuer']).as_string()
    subject = sql.Literal(identity['subject']).as_string()
    where = f'owner_issuer={issuer} AND owner_subject={subject}'
    statements.append(f"DO $qa$ BEGIN IF EXISTS (SELECT 1 FROM projects p JOIN tenants t ON p.tenant_id=t.id WHERE t.{where}) OR EXISTS (SELECT 1 FROM resources r JOIN tenants t ON r.tenant_id=t.id WHERE t.{where}) OR EXISTS (SELECT 1 FROM test_orders o JOIN tenants t ON o.tenant_id=t.id WHERE t.{where}) THEN RAISE EXCEPTION 'QA tenant contains resources; refusing deletion'; END IF; END $qa$;")
    statements.append(f'DELETE FROM model_credentials WHERE issuer={issuer} AND subject={subject};')
    statements.append(f'DELETE FROM memberships WHERE tenant_id IN (SELECT id FROM tenants WHERE {where});')
    statements.append(f'DELETE FROM tenants WHERE {where};')
statements.append('COMMIT;')
remote(['kubectl', '-n', 'postgres', 'exec', '-i', 'postgres-1', '-c', 'postgres', '--', 'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'platform_control'], '\n'.join(statements))
print('Temporary LiteLLM users, keys and owned platform metadata removed; audit retained')
