# Execute in Authentik's ak shell after the platform blueprint is loaded.
import json
from authentik.core.models import Application, Group, User
from authentik.providers.oauth2.models import OAuth2Provider
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

provider = OAuth2Provider.objects.get(client_id="platform-console")
key = serialization.load_pem_private_key(provider.signing_key.key_data.encode(), password=None)
assert isinstance(key, RSAPrivateKey), "Platform API requires RS256"
assert provider.client_type == "public"
assert provider.grant_types == ["authorization_code"]
admin = User.objects.get(username="akadmin")
assert admin.is_superuser and admin.is_active
group = Group.objects.get(name="platform-admins")
assert not group.is_superuser
group.users.add(admin)
print("RESULT:" + json.dumps({"application": Application.objects.get(slug="platform").slug, "public_client": True, "signing_algorithm": "RS256", "platform_admin": admin.username}))
