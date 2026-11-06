"""Xunhupay scalar signing contract; this first adapter is explicitly local-simulation only."""
import hashlib
import hmac
import re
import secrets
import time
from decimal import Decimal
from urllib.parse import urlsplit

import httpx


class PaymentError(ValueError):
    pass


def signature(values, secret):
    if not isinstance(values, dict) or any(not isinstance(key, str) or not key.isascii() for key in values):
        raise PaymentError('invalid_fields')
    included = {}
    for key, value in values.items():
        if key == 'hash' or value is None or value == '':
            continue
        if type(value) not in (str, int):
            raise PaymentError('unsupported_field_type')
        included[key] = str(value)
    canonical = '&'.join(key+'='+included[key] for key in sorted(included))
    return hashlib.md5((canonical+secret).encode('utf-8')).hexdigest()


def verify(values, secret):
    supplied = values.get('hash') if isinstance(values, dict) else None
    if not isinstance(supplied, str) or len(supplied) != 32:
        return False
    try:
        return hmac.compare_digest(signature(values, secret), supplied)
    except PaymentError:
        return False


def loopback_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1') or
            parsed.username or parsed.password or parsed.fragment):
        raise PaymentError('local_simulator_required')
    return parsed


def amount_minor(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,8}(?:\.[0-9]{1,2})?', value):
        raise PaymentError('invalid_amount')
    return int(Decimal(value)*100)


class LocalPaymentGateway:
    def __init__(self, settings):
        self.settings = settings
        loopback_url(settings.xunhupay_simulator_url)
        loopback_url(settings.xunhupay_notify_url)
        if not settings.xunhupay_appid.startswith('sim-') or len(settings.xunhupay_secret) < 32:
            raise PaymentError('local_simulator_credentials_required')

    def create(self, order):
        settings = self.settings
        if order.plan['currency'] != 'CNY' or order.plan['amount_minor'] <= 0:
            raise PaymentError('invalid_amount')
        body = {'version':'1.1','appid':settings.xunhupay_appid,'trade_order_id':order.id.replace('-',''),
            'total_fee':format(Decimal(order.plan['amount_minor'])/100,'.2f'), 'title':'Starbridge plan',
            'time':int(time.time()),'notify_url':settings.xunhupay_notify_url,'nonce_str':secrets.token_hex(16)}
        body['hash'] = signature(body, settings.xunhupay_secret)
        try:
            with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
                response = client.post(settings.xunhupay_simulator_url.rstrip('/')+'/payment/do.html', json=body)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError):
            raise PaymentError('payment_result_unknown') from None
        if not verify(data, settings.xunhupay_secret) or data.get('errcode') != 0:
            raise PaymentError('payment_response_unverified')
        url = data.get('url')
        if not isinstance(url,str) or loopback_url(url).netloc != loopback_url(settings.xunhupay_simulator_url).netloc:
            raise PaymentError('payment_response_unverified')
        return url
