import os
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from control_plane.templates.support.app import create_app, password_hash, metadata

PASSWORD = 'synthetic-owner-password-1'


def config(title='工单验收'):
    return {'title': title, 'owner_password_hash': password_hash(PASSWORD), 'version': '1'}


def exercise(url):
    application = create_app(url, config())
    with TestClient(application) as owner:
        assert owner.get('/api/tickets').status_code == 401
        assert owner.post('/api/login', json={'name':'owner','password':PASSWORD}, headers={'Origin':'https://foreign.test'}).status_code == 403
        assert owner.post('/api/login', json={'name':'owner','password':PASSWORD}).status_code == 200
        invited = owner.post('/api/invitations', json={'role':'agent'}).json()['invitation']
        with TestClient(application) as colleague:
            body = {'name':'同事','password':'synthetic-colleague-password','invitation':invited}
            assert colleague.post('/api/join', json=body).status_code == 200
            colleague_id = colleague.get('/api/me').json()['id']
            assert colleague.post('/api/join', json={**body,'name':'第二人'}).status_code == 409
            assert colleague.get('/api/export').status_code == 403
            assert colleague.post('/api/invitations', json={'role':'viewer'}).status_code == 403
            values = {'title':'=安全导出验收','customer':'示例客户','description':'需安排回访','assignee':colleague_id,'status':'open'}
            identifier = owner.post('/api/tickets', json=values).json()['id']
            edit = {**values, 'status':'working','revision':1}
            assert colleague.put('/api/tickets/'+identifier, json=edit).status_code == 200
            assert owner.put('/api/tickets/'+identifier, json=edit).status_code == 409
            assert colleague.get('/api/tickets?status=open').json()['tickets'] == []
            assert colleague.get('/api/tickets?status=working&assignee='+colleague_id).json()['tickets'][0]['id'] == identifier
            csv = owner.get('/api/export?status=working').text
            assert "'=安全导出验收" in csv and '需安排回访' in csv
        invitation = owner.post('/api/invitations', json={'role':'viewer'}).json()['invitation']
        with TestClient(application) as viewer:
            assert viewer.post('/api/join', json={'name':'只读同事','password':'synthetic-viewer-password','invitation':invitation}).status_code == 200
            assert viewer.get('/api/tickets').json()['tickets'][0]['id'] == identifier
            assert viewer.post('/api/tickets', json=values).status_code == 403
            assert viewer.get('/api/export').status_code == 403
            assert viewer.post('/api/logout').status_code == 200
            assert viewer.get('/api/tickets').status_code == 401
    # New application instance / changed code config, same real DB, no seeding/reset.
    with TestClient(create_app(url, config('新版售后工单'))) as next_release:
        assert next_release.get('/api/info').json()['title'] == '新版售后工单'
        assert next_release.post('/api/login', json={'name':'同事','password':'synthetic-colleague-password'}).status_code == 200
        data = next_release.get('/api/tickets').json()['tickets']
        assert len(data) == 1 and data[0]['id'] == identifier and data[0]['revision'] == 2
        assert data[0]['status'] == 'working'


def test_support_workflow_and_redeploy_preserves_records(tmp_path):
    exercise('sqlite:///'+str(tmp_path/'support.db'))


def test_support_workflow_postgres():
    url = os.environ.get('PLATFORM_TEST_DATABASE_URL')
    if not url: pytest.skip('Disposable local PostgreSQL only')
    parsed = make_url(url)
    assert parsed.host in ('127.0.0.1','localhost') and parsed.database.startswith('platform_test_')
    engine = create_engine(url)
    try: exercise(url)
    finally:
        metadata.drop_all(engine)
        engine.dispose()
