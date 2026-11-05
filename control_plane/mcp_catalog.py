"""Versioned MCP review and owner-bound credentials. No remote calls in serializers."""
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import select

from .models import MCPServer, MCPCredential


def endpoint(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment
            or '%' in parsed.netloc or not re.fullmatch(r'[a-zA-Z0-9.-]+', parsed.hostname)
            or '.' not in parsed.hostname or parsed.hostname.endswith('.')):
        raise ValueError('mcp_https_public_endpoint_required')
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError('mcp_https_public_endpoint_required')
    return 'https://' + parsed.hostname.lower() + (parsed.path or '/')


class ServerInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(max_length=500)
    url: str = Field(max_length=500)
    auth: str = Field(pattern=r'^(none|platform_bearer|user_bearer)$')
    data_notice: str = Field(min_length=1, max_length=500)
    secret: SecretStr | None = None
    _url = field_validator('url')(endpoint)


def credential_value(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise HTTPException(422, 'mcp_credential_invalid')
    return value


def encrypt(value, settings, owner, server, revision):
    if not settings.mcp_credential_encryption_key:
        raise HTTPException(503, 'mcp_credentials_unavailable')
    payload = {'owner': owner, 'server': server, 'revision': revision, 'value': credential_value(value)}
    return Fernet(settings.mcp_credential_encryption_key.encode()).encrypt(json.dumps(payload).encode()).decode()


def decrypt(value, settings, owner, server, revision):
    data = json.loads(Fernet(settings.mcp_credential_encryption_key.encode()).decrypt(value.encode()))
    if any(data.get(key) != expected for key, expected in [('owner', owner), ('server', server), ('revision', revision)]):
        raise ValueError('mcp_credential_binding_mismatch')
    return data['value']


def safe_schema(schema):
    """Bound schemas before they reach validators; no references or regex execution."""
    if not isinstance(schema, dict) or schema.get('type') != 'object' or len(json.dumps(schema)) > 16384:
        return False
    def walk(value, depth=0):
        if depth > 12:
            return False
        if isinstance(value, dict):
            if {'$ref', '$dynamicRef', 'pattern', 'patternProperties', 'format'} & value.keys():
                return False
            return len(value) <= 80 and all(walk(v, depth + 1) for v in value.values())
        if isinstance(value, list):
            return len(value) <= 80 and all(walk(v, depth + 1) for v in value)
        return isinstance(value, (str, int, float, bool, type(None)))
    return walk(schema)


def fingerprint(tool):
    return hashlib.sha256(json.dumps({k: tool.get(k) for k in ('name', 'description', 'inputSchema', 'annotations')}, sort_keys=True).encode()).hexdigest()


def legacy():
    from .agent_mcp import CATALOG
    raw = CATALOG['deepwiki']
    tools = []
    for name in raw['tools']:
        properties = {'repoName': {'type': 'string', 'maxLength': 201}}
        if name == 'ask_question':
            properties['question'] = {'type': 'string', 'maxLength': 2000}
        tools.append({'name': name, 'description': 'Query public GitHub repository documentation',
                      'inputSchema': {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False},
                      'permission': 'read'})
    return MCPServer(id='deepwiki', revision=1, state='published',
                     config={**raw, 'auth': 'none'}, approved=tools, discovered=[],
                     addresses=['32.186.91.142', '32.191.178.174', '35.80.103.206'])


def servers(db):
    rows = list(db.scalars(select(MCPServer)))
    if not any(row.id == 'deepwiki' for row in rows):
        rows.insert(0, legacy())
    return rows


def server(db, identifier):
    row = db.get(MCPServer, identifier)
    if row is None and identifier == 'deepwiki':
        row = legacy()
    return row


def published(db, settings, row):
    if not row or row.state != 'published':
        return False
    if row.id == 'deepwiki':
        from .ai_policy import effective_policy
        return row.id in effective_policy(db, settings).get('mcp_servers', [])
    return True


def secret_for(db, settings, row, environment_id):
    if row.config['auth'] == 'none':
        return '', 0
    if row.config['auth'] == 'platform_bearer':
        return decrypt(row.encrypted_credential, settings, 'platform', row.id, 0), 0
    credential = db.get(MCPCredential, (environment_id, row.id))
    if not credential or not credential.encrypted_value or credential.server_revision != row.revision:
        raise HTTPException(409, 'mcp_personal_credential_required')
    return decrypt(credential.encrypted_value, settings, environment_id, row.id, credential.revision), credential.revision


def snapshot(db, settings, selected, environment_id):
    if len(selected) != len(set(selected)):
        raise HTTPException(409, 'agent_mcp_selection_unavailable')
    versions, credentials = {}, {}
    for identifier in selected:
        row = server(db, identifier)
        if not published(db, settings, row):
            raise HTTPException(409, 'agent_mcp_selection_unavailable')
        _, credentials[identifier] = secret_for(db, settings, row, environment_id)
        versions[identifier] = row.revision
    return versions, credentials


def catalog(db, settings, environment_id):
    result = []
    for row in servers(db):
        if row.state not in ('published', 'disabled'):
            continue
        configured = row.config['auth'] != 'user_bearer'
        credential_revision = 0
        credential_stored = False
        if not configured:
            value = db.get(MCPCredential, (environment_id, row.id))
            configured = bool(value and value.encrypted_value and value.server_revision == row.revision)
            credential_revision = value.revision if value else 0
            credential_stored = bool(value and value.encrypted_value)
        result.append({'id': row.id, **{k: row.config[k] for k in ('name', 'description', 'url', 'data_notice', 'auth')},
                       'version': row.revision, 'enabled': published(db, settings, row), 'credential_configured': configured,
                       'credential_revision': credential_revision,
                       'credential_stored': credential_stored,
                       'tools': [tool['name'] for tool in row.approved],
                       'permission': 'write' if any(t['permission'] == 'write' for t in row.approved) else 'read'})
    return {'servers': result}
