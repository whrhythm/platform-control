"""Run the real gateway against the local Postman HTTP simulation."""
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from control_plane.database import get_db, make_engine
from control_plane.main import app
from control_plane.models import Base, Tenant, UserEnvironment, CodeExecution
from control_plane.settings import get_settings
from control_plane.user_environments import identity_digest
from sqlalchemy import select

url = os.environ.get('CODEAPI_SIMULATION_URL', 'http://127.0.0.1:4520/compute/')
issuer = 'https://synthetic-identity.test/'
key = Ed25519PrivateKey.generate()
with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
    'PLATFORM_CODE_EXECUTION_ENABLED':'true', 'PLATFORM_CODE_EXECUTION_URL':url,
    'PLATFORM_OIDC_ISSUER':issuer,
    'PLATFORM_CODE_EXECUTION_PUBLIC_KEY':key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode(),
}):
    get_settings.cache_clear()
    engine = make_engine('sqlite:///'+str(Path(directory)/'test.db')); Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as db:
        tenant = Tenant(name='synthetic',owner_issuer=issuer,owner_subject='synthetic',project_limit=1); db.add(tenant); db.flush()
        db.add(UserEnvironment(issuer=issuer,subject='synthetic',identity_hash=identity_digest(issuer,'synthetic'),first_tenant_id=tenant.id,storage_gib=2))
    def database():
        with sessions() as db: yield db
    app.dependency_overrides[get_db] = database
    try:
        httpx.post(url+'__reset', trust_env=False).raise_for_status()
        token = jwt.encode(dict(iss='librechat',aud='codeapi',sub='synthetic',platform_issuer=issuer,platform_subject='synthetic',iat=int(time.time()),exp=int(time.time())+300),key,algorithm='EdDSA')
        client = TestClient(app); headers = {'Authorization':'Bearer '+token}
        assert client.post('/internal/v1/codeapi/exec',headers=headers,json={'code':'42','lang':'py','timeout':99999}).status_code == 200
        failure = client.post('/internal/v1/codeapi/exec',headers=headers,json={'code':'ambiguous','lang':'py'})
        assert failure.status_code == 503 and 'private-provider' not in failure.text
        assert client.post('/internal/v1/codeapi/exec',headers=headers,json={'code':'42','lang':'py'}).status_code == 429
        calls = httpx.get(url+'__calls', trust_env=False).json()['calls']; assert len(calls) == 2 and calls[0]['timeout'] == 10000
        with sessions() as db:
            assert sorted(row.state for row in db.scalars(select(CodeExecution))) == ['complete','unknown']
        print('PASS: forced timeout, durable accounting, uncertain outcome without retry, concurrent admission blocked')
    finally:
        app.dependency_overrides.clear(); engine.dispose(); get_settings.cache_clear()
