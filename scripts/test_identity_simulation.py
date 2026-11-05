"""Exercise the actual account routes and HTTP client against a Postman stand-in."""
import os
import tempfile
from pathlib import Path
import httpx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from control_plane import accounts
from control_plane.auth import Principal
from control_plane.database import make_engine, get_db
from control_plane.settings import get_settings
from control_plane.main import app, platform_admin

base=os.environ['IDENTITY_SIMULATION_URL'].rstrip('/')
os.environ.update(PLATFORM_IDENTITY_ADMIN_URL=base+'/',PLATFORM_IDENTITY_ADMIN_TOKEN='synthetic-directory-token',PLATFORM_WORKSPACE_SERVICE_CA='')
get_settings.cache_clear()
from control_plane.models import Base
with tempfile.TemporaryDirectory() as directory:
 engine=make_engine('sqlite:///'+str(Path(directory)/'accounts.db'));Base.metadata.create_all(engine)
 def db():
  with Session(engine) as session:yield session
 app.dependency_overrides[get_db]=db
 app.dependency_overrides[platform_admin]=lambda:Principal(issuer=get_settings().oidc_issuer,subject='operator',groups=('platform-admins',))
 try:
  client=TestClient(app)
  for fail in (False,True):
   httpx.post(base+'/__seed',json={'fail':fail},trust_env=False).raise_for_status()
   response=client.post('/v1/admin/accounts',json={'username':'synthetic','name':'Synthetic','email':'synthetic@example.test','password':'Synthetic-Password-27'})
   assert response.status_code==(503 if fail else 200),response.text
   assert 'private-provider' not in response.text
   assert httpx.get(base+'/__writes',trust_env=False).json()['count']==1
   with Session(engine) as session:
    assert accounts.allowed(session,get_settings().oidc_issuer,'synthetic-1')== (not fail)
   print(('ambiguous applied write' if fail else 'create and activate')+': verified; one PATCH, safe response, correct local access fence')
 finally:
  app.dependency_overrides.clear();engine.dispose();get_settings.cache_clear()
