"""Report the installed gateway's missing-user response without printing secrets."""
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

user = 'codechat-nonexistent-' + secrets.token_hex(12)
url = 'http://127.0.0.1:4000/user/info?' + urllib.parse.urlencode({'user_id': user})
request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + os.environ['LITELLM_MASTER_KEY']})
try:
    with urllib.request.urlopen(request, timeout=10) as response:
        body = json.load(response)
        print('status', response.status, 'user_info_is_null', body.get('user_info') is None)
except urllib.error.HTTPError as error:
    print('status', error.code)
