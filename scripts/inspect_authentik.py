# Execute inside authentik's `ak shell`; prints no credentials.
import inspect
import json
from authentik.core.models import Application, User, Group
from authentik.crypto.models import CertificateKeyPair
from authentik.flows.models import Flow
from authentik.providers.oauth2.models import AccessToken, OAuth2Provider, ScopeMapping
from authentik.providers.oauth2.id_token import IDToken

result = {
    "applications": list(Application.objects.values_list("slug", flat=True)),
    "flows": list(Flow.objects.values_list("slug", "designation")),
    "certificates": list(CertificateKeyPair.objects.values_list("name", flat=True)),
    "scope_mappings": list(ScopeMapping.objects.values("name", "scope_name", "managed")),
    "administrators": [user.username for user in User.objects.filter(is_active=True) if getattr(user, "is_superuser", False)],
    "provider_fields": [field.name for field in OAuth2Provider._meta.fields],
    "profile_expression": ScopeMapping.objects.get(scope_name="profile").expression,
    "group_fields": [field.name for field in Group._meta.get_fields()],
    "group_methods": [name for name in dir(User) if "group" in name],
}
print("RESULT:" + json.dumps(result, default=str))
