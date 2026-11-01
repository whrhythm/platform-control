"""Inspect only QA identity associations and budgets; never print credentials."""
import json
from pathlib import Path
from cluster_transport import remote

results = json.loads((Path(__file__).resolve().parents[1] / 'release/enrollment-results.json').read_text())
if len(results['identities']) != 2 or results['identities'][0] == results['identities'][1]:
    raise RuntimeError('Expected two different QA identities')
program = '''
import hashlib, json, os, urllib.parse, urllib.request
items = json.loads(PAYLOAD)
ids = []
for identity in items:
    issuer = identity['issuer'].rstrip('/') + '/'  # Authentik's configured token issuer, not LibreChat's normalized issuer.
    digest = hashlib.sha256(json.dumps([issuer, identity['subject']], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    user_id = 'codechat-' + digest
    url = 'http://127.0.0.1:4000/user/info?' + urllib.parse.urlencode({'user_id': user_id})
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + os.environ['LITELLM_MASTER_KEY']})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    record = data['user_info']
    assert record['user_id'] == user_id and float(record['max_budget']) == 1.0
    assert record['metadata']['managed_by'] == 'codechat' and record['metadata']['identity_hash'] == digest
    keys = data.get('keys', [])
    assert len(keys) == 1, 'Expected exactly one key for each QA user'
    print('RESULT:Key record fields:', sorted(keys[0].keys()))
    print('RESULT:QA user spend recorded:', float(record.get('spend') or 0))
    ids.append(user_id)
assert len(set(ids)) == 2
print('RESULT:Two separate LiteLLM users each have one key and a one-dollar budget')
'''.replace('PAYLOAD', repr(json.dumps(results['identities'])))
out = remote(['kubectl', '-n', 'litellm', 'exec', '-i', 'deployment/litellm', '--', 'python', '-'], program)
if 'RESULT:' not in out:
    raise RuntimeError('LiteLLM QA inspection did not complete')
for line in out.splitlines():
    if line.startswith('RESULT:'):
        print(line)
