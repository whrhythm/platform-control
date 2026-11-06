"""Real loopback HTTP between merchant and simulator; no cluster or real money."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from sqlalchemy import select, func

from control_plane.main import app
from control_plane.models import AuditEvent, CapacityOrder, PaymentReceipt
from control_plane.settings import get_settings
from control_plane.xunhupay import signature, verify, amount_minor, PaymentError, loopback_url
from scripts.xunhupay_simulator import create_simulator
from test_capacity import capacity_harness, create_order, PREFIX, ResizeHTTP, provider_for
from test_model_membership import membership
from control_plane.model_billing_controller import apply_credit
from control_plane.capacity_controller import reconcile_capacity

SECRET = 'synthetic-payment-test-secret-not-a-real-key'


@contextmanager
def listener(application):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    origin = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(application, lifespan='off', access_log=False, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(8)
        sock.close()
        assert not thread.is_alive()


@contextmanager
def local_payment(monkeypatch):
    with listener(app) as merchant:
        # The simulator origin is set after binding, before any request is made.
        from fastapi import FastAPI
        placeholder = FastAPI()
        with listener(placeholder) as simulator_origin:
            simulator = create_simulator('sim-tests', SECRET, simulator_origin,
                merchant + '/payments/xunhupay/notify')
            placeholder.router.routes = simulator.router.routes
            for key, value in {
                'PLATFORM_XUNHUPAY_SIMULATOR_ENABLED': 'true',
                'PLATFORM_XUNHUPAY_SIMULATOR_URL': simulator_origin,
                'PLATFORM_XUNHUPAY_NOTIFY_URL': merchant + '/payments/xunhupay/notify',
                'PLATFORM_XUNHUPAY_APPID': 'sim-tests', 'PLATFORM_XUNHUPAY_SECRET': SECRET,
            }.items():
                monkeypatch.setenv(key, value)
            get_settings.cache_clear()
            yield simulator, simulator_origin


def notification(order, **overrides):
    body = dict(appid='sim-tests', trade_order_id=order['id'].replace('-', ''),
        total_fee='10.00', transaction_id='synthetic-transaction', open_order_id='provider-order',
        status='OD', time=str(int(time.time())), nonce_str='synthetic-nonce')
    body.update(overrides)
    return {**body, 'hash': signature(body, SECRET)}


def test_signature_raw_values_empty_fields_and_decimal_amount():
    fields = {'z': '中文 & =', 'a': 0, 'empty': '', 'none': None, 'hash': 'ignored'}
    expected = hashlib.md5(('a=0&z=中文 & =' + SECRET).encode()).hexdigest()
    assert signature(fields, SECRET) == expected
    assert verify({**fields, 'hash': expected}, SECRET)
    assert not verify({**fields, 'z': 'tampered', 'hash': expected}, SECRET)
    assert amount_minor('10.01') == 1001
    for value in ('1e2', '-1', '0.001', 'NaN', '１', 10):
        with pytest.raises(PaymentError): amount_minor(value)
    for value in ('https://provider.test', 'http://127.0.0.1.evil.test', 'http://user@localhost'):
        with pytest.raises(PaymentError): loopback_url(value)


def test_real_http_checkout_callback_credit_and_observed_entitlements(membership, capacity_harness, monkeypatch):
    client, auth, env, _, budget, gateway = membership
    _, _, _, observed, issuer = capacity_harness
    with local_payment(monkeypatch) as (simulator, origin):
        order = create_order(client, auth).json()
        path = PREFIX + '/orders/' + order['id']
        assert client.post(path + '/confirm-test', headers=auth).status_code == 409
        checkout = client.post(path + '/checkout', headers=auth).json()
        assert checkout['state'] == 'awaiting_payment'
        assert client.post(path + '/checkout', headers=auth).json() == checkout
        assert len(simulator.state.orders) == 1
        with httpx.Client(trust_env=False) as http:
            page = http.get(checkout['checkout_url'])
            assert '不会扣款' in page.text
            # Visiting the return/checkout page never confirms a purchase.
            assert client.get(PREFIX, headers=auth).json()['active_order']['payment_status'] == 'unpaid'
            for _ in range(2):
                assert http.post(checkout['checkout_url']).json()['callback_acknowledged']
        current = client.get(PREFIX, headers=auth).json()
        assert current['active_order']['payment_status'] == 'confirmed'
        assert current['active_order']['attempt'] == 1
        assert current['plan']['storage_gib'] == 2
        for _ in range(6): apply_credit(client.session_factory, order['id'], gateway, get_settings())
        assert [p for p, _ in budget['writes']] == ['/user/update', '/key/update']
        assert budget['user']['max_budget'] == budget['info']['max_budget'] == 2.428571
        transport = ResizeHTTP(issuer)
        provider = provider_for(issuer, transport)
        try:
            for _ in range(4):
                reconcile_capacity(client.session_factory, provider, env.id, get_settings(), probe=lambda: observed)
            assert client.get(PREFIX, headers=auth).json()['plan']['storage_gib'] == 2
            transport.objects['PersistentVolumeClaim']['status']['capacity']['storage'] = '4Gi'
            transport.objects['PersistentVolume']['spec']['capacity']['storage'] = '4Gi'
            observed['physical_total_bytes'] = 4 * 1024 ** 3
            reconcile_capacity(client.session_factory, provider, env.id, get_settings(), probe=lambda: observed)
            final = client.get(PREFIX, headers=auth).json()
            assert final['plan']['storage_gib'] == 4
            assert final['orders'][0]['status'] == 'ready'
            with httpx.Client(trust_env=False) as http:
                assert http.post(checkout['checkout_url']).json()['callback_acknowledged']
            assert client.get(PREFIX, headers=auth).json()['orders'][0]['attempt'] == 1
        finally: provider.close()
        with client.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == 'payment.confirmed')) == 1


def test_signed_callback_rejects_tampering_owner_and_late_payment(capacity_harness, monkeypatch):
    client, auth, *_ = capacity_harness
    with local_payment(monkeypatch):
        order = create_order(client, auth).json()
        path = PREFIX + '/orders/' + order['id']
        assert client.post(path + '/checkout', headers={**auth, 'X-CodeChat-Subject': 'bob'}).status_code == 404
        assert client.get(path + '/payment', headers={**auth, 'X-CodeChat-Subject': 'bob'}).status_code == 404
        assert client.post(path + '/checkout', headers=auth).status_code == 200
        notify = '/payments/xunhupay/notify'
        body = notification(order); body['total_fee'] = '99.00'
        assert client.post(notify, data=body).status_code == 400
        assert client.post(notify, data=notification(order, total_fee='99.00')).status_code == 409
        assert client.post(notify, data=notification(order, status='CD')).status_code == 409
        assert client.post(notify, data=notification(order, time='0')).status_code == 400
        assert client.post(path + '/cancel', headers=auth).status_code == 200
        for _ in range(2): assert client.post(notify, data=notification(order)).text == 'success'
        assert client.get(path + '/payment', headers=auth).json()['state'] == 'requires_review'
        with client.session_factory() as db:
            row = db.get(CapacityOrder, order['id'])
            assert row.status == 'cancelled' and row.payment_status == 'confirmed'
            assert row.evidence['payment_review_required']
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == 'payment.requires_review')) == 1


def test_lost_create_response_is_not_automatically_recreated(capacity_harness, monkeypatch):
    client, auth, *_ = capacity_harness
    with local_payment(monkeypatch):
        order = create_order(client, auth).json()
        calls = []
        def ambiguous(_self, _order):
            calls.append(True)
            raise PaymentError('payment_result_unknown')
        monkeypatch.setattr('control_plane.payments.LocalPaymentGateway.create', ambiguous)
        path = PREFIX + '/orders/' + order['id']
        for _ in range(2):
            assert client.post(path + '/checkout', headers=auth).json()['state'] == 'requires_review'
        assert len(calls) == 1
        assert client.post('/payments/xunhupay/notify', data=notification(order)).text == 'success'
        assert client.get(path + '/payment', headers=auth).json()['state'] == 'confirmed'


def test_one_provider_transaction_cannot_credit_two_orders(capacity_harness, monkeypatch):
    client, auth, *_ = capacity_harness
    with local_payment(monkeypatch):
        first = create_order(client, auth).json()
        first_path = PREFIX + '/orders/' + first['id']
        client.post(first_path + '/checkout', headers=auth)
        client.post(first_path + '/cancel', headers=auth)
        assert client.post('/payments/xunhupay/notify', data=notification(first)).text == 'success'
        second = create_order(client, auth, key='second-payment-order').json()
        second_path = PREFIX + '/orders/' + second['id']
        client.post(second_path + '/checkout', headers=auth)
        assert client.post('/payments/xunhupay/notify', data=notification(second)).status_code == 409
        assert client.get(second_path + '/payment', headers=auth).json()['state'] == 'awaiting_payment'


def test_pilot_price_model_credit_and_immutable_order_snapshot(capacity_harness, monkeypatch):
    from control_plane.ai_policy import validate_trial
    client, auth, *_ = capacity_harness
    config = json.loads((Path(__file__).resolve().parents[1] / 'config/plans-local-pilot.json').read_text())
    assert validate_trial(config['trial_plan'])['application_limit'] == 1
    monkeypatch.setenv('PLATFORM_CAPACITY_PLANS_JSON', json.dumps(config['plans']))
    get_settings.cache_clear()
    with local_payment(monkeypatch):
        order = create_order(client, auth).json()
        assert order['plan']['amount_minor'] == 2900
        assert order['plan']['model_credit_cny_minor'] == 1000
        assert order['plan']['model_credit_usd_micros'] == 1428571
        config['plans'][0]['amount_minor'] = 3900
        config['plans'][0]['model_credit_cny_minor'] = 500
        monkeypatch.setenv('PLATFORM_CAPACITY_PLANS_JSON', json.dumps(config['plans']))
        get_settings.cache_clear()
        client.post(PREFIX + '/orders/' + order['id'] + '/checkout', headers=auth)
        assert client.post('/payments/xunhupay/notify', data=notification(order, total_fee='39.00')).status_code == 409
        assert client.post('/payments/xunhupay/notify', data=notification(order, total_fee='29.00')).text == 'success'
        current = client.get(PREFIX, headers=auth).json()['active_order']
        assert current['plan']['amount_minor'] == 2900
        assert current['plan']['model_credit_usd_micros'] == 1428571


def test_restarted_checkout_is_reviewed_instead_of_waiting_forever(capacity_harness, monkeypatch):
    from datetime import timedelta
    from control_plane.models import now
    client, auth, *_ = capacity_harness
    with local_payment(monkeypatch):
        order = create_order(client, auth).json()
        path = PREFIX + '/orders/' + order['id']
        with client.session_factory.begin() as db:
            db.add(PaymentReceipt(order_id=order['id'], state='creating', created_at=now()-timedelta(minutes=2)))
        for _ in range(2):
            assert client.get(path + '/payment', headers=auth).json()['state'] == 'requires_review'
        with client.session_factory() as db:
            assert db.scalar(select(func.count()).select_from(AuditEvent).where(
                AuditEvent.action == 'payment.intent_requires_review')) == 1
        assert client.post('/payments/xunhupay/notify', data=notification(order)).text == 'success'
        assert client.get(path + '/payment', headers=auth).json()['state'] == 'confirmed'
