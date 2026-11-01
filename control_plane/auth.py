from dataclasses import dataclass
from functools import lru_cache

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

from .settings import Settings, get_settings


@dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    groups: tuple[str, ...] = ()

    @property
    def is_platform_admin(self) -> bool:
        return get_settings().admin_group in self.groups


bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=4)
def jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, timeout=5)


def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if not settings.oidc_issuer or not settings.oidc_audience or not settings.oidc_jwks_url:
        raise HTTPException(status_code=503, detail="OIDC is not configured")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Bearer token required", headers={"WWW-Authenticate": "Bearer"})
    try:
        key = jwks_client(settings.oidc_jwks_url).get_signing_key_from_jwt(credentials.credentials)
        claims = jwt.decode(
            credentials.credentials,
            key.key,
            algorithms=["RS256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat", "iss", "sub", "aud"]},
        )
        if not isinstance(claims["sub"], str) or not claims["sub"] or len(claims["sub"]) > 255:
            raise jwt.InvalidTokenError("Invalid subject")
        scopes = claims.get("scope", "")
        if not isinstance(scopes, str) or settings.oidc_required_scope not in scopes.split():
            raise jwt.InvalidTokenError("API scope required")
        groups = claims.get("groups", [])
        if not isinstance(groups, list) or not all(isinstance(group, str) for group in groups):
            raise jwt.InvalidTokenError("Invalid groups")
        return Principal(issuer=claims["iss"], subject=claims["sub"], groups=tuple(groups))
    except (jwt.PyJWTError, ValueError, OSError):
        raise HTTPException(status_code=401, detail="Invalid or unverifiable access token", headers={"WWW-Authenticate": "Bearer"})

