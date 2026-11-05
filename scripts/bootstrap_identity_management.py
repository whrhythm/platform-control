"""Install a dedicated Authentik directory credential; secret values stay in captured IO."""
import json
from cluster_transport import remote

code = '''
import json
from datetime import timedelta
from django.utils.timezone import now
from django.contrib.auth.models import Permission
from authentik.core.models import User, Token, TokenIntents
from authentik.providers.oauth2.models import OAuth2Provider
from authentik.rbac.models import Role
assert OAuth2Provider.objects.get(client_id='platform-console').sub_mode == 'hashed_user_id'
user, created = User.objects.get_or_create(username='platform-account-manager', defaults={'name':'Platform account directory', 'type':'service_account', 'is_active':True, 'path':'service-accounts/platform'})
assert user.type == 'service_account' and not user.is_superuser
permissions = Permission.objects.filter(content_type__app_label='authentik_core', codename__in=['view_user','add_user','change_user','reset_user_password'])
assert permissions.count() == 4
role, _ = Role.objects.get_or_create(name='platform-account-directory')
RolePermission = Role._meta.get_field('rolemodelpermission').related_model
assert not RolePermission.objects.filter(role=role).exclude(permission__in=permissions).exists()
for p in permissions:
    RolePermission.objects.get_or_create(role=role, permission=p, defaults={'content_type':p.content_type})
user.roles.add(role)
user.set_unusable_password(); user.save()
for p in permissions: assert user.has_perm('authentik_core.'+p.codename), p.codename
token, created = Token.objects.get_or_create(identifier='platform-account-manager-api', defaults={'user':user, 'intent':TokenIntents.INTENT_API, 'expiring':True, 'expires':now()+timedelta(days=365)})
assert token.user_id == user.pk and token.intent == TokenIntents.INTENT_API and token.expires > now()
print('SECRET_RESULT:'+json.dumps({'token':token.key}))
'''

result = remote(['kubectl','-n','authentik','exec','deploy/authentik-worker','--','env','AUTHENTIK_LOG_LEVEL=error','ak','shell','-c',code])
payload = json.loads(next(line.removeprefix('SECRET_RESULT:') for line in result.splitlines() if line.startswith('SECRET_RESULT:')))
secret = {'apiVersion':'v1','kind':'Secret','type':'Opaque','metadata':{'name':'platform-identity-directory','namespace':'platform-system'},
          'stringData':{'PLATFORM_IDENTITY_ADMIN_TOKEN':payload['token'],'PLATFORM_IDENTITY_ADMIN_URL':'https://authentik.k3s.local/api/v3/'}}
remote(['kubectl','apply','-f','-'],json.dumps(secret),redact=(payload['token'],))
print('RESULT:Dedicated account-management credential installed; four directory permissions, no administrator role')
