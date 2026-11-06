"""Local simulator checkout -> verified callback -> existing asynchronous entitlement workflow."""
import re
import time
from datetime import timezone
from uuid import UUID
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .database import get_db
from .models import CapacityOrder, CapacityAccount, PaymentReceipt, UserEnvironment, AuditEvent, now
from .settings import get_settings
from .workspace_gateway import own_environment
from .xunhupay import LocalPaymentGateway, PaymentError, verify, amount_minor


def configured():
    settings=get_settings()
    if not settings.xunhupay_simulator_enabled or not settings.test_payments_enabled or not settings.capacity_upgrades_enabled:
        raise HTTPException(404,'payment_simulator_disabled')
    try:LocalPaymentGateway(settings)
    except PaymentError:raise HTTPException(503,'payment_configuration_invalid') from None
    return settings


def receipt_out(receipt):
    return {'state':receipt.state,'checkout_url':receipt.checkout_url,'mode':'local_simulator'}


def observe_intent(db, env, receipt):
    """A process may have died after recording intent and before saving the HTTP result."""
    created = receipt.created_at
    if created.tzinfo is None:created = created.replace(tzinfo=timezone.utc)
    if receipt.state == 'creating' and (now() - created).total_seconds() > 60:
        receipt.state = 'requires_review'
        db.add(AuditEvent(tenant_id=env.first_tenant_id,issuer='internal:payment-simulator',subject='xunhupay',
            action='payment.intent_requires_review',target_id=receipt.order_id))
        db.commit()
    return receipt_out(receipt)


def create_router(principal_dependency):
    router=APIRouter(tags=['payments'])

    @router.post('/internal/v1/workspace/capacity/orders/{order_id}/checkout')
    def checkout(order_id:UUID,principal=Depends(principal_dependency),db=Depends(get_db)):
        settings=configured();env=own_environment(db,principal);db.refresh(env,with_for_update=True)
        order=db.get(CapacityOrder,str(order_id),with_for_update=True)
        if not order or order.environment_id!=env.id:raise HTTPException(404,'payment_order_not_found')
        if env.status=='suspended':raise HTTPException(403,'payment_account_suspended')
        previous=db.get(PaymentReceipt,order.id)
        if previous:return observe_intent(db,env,previous)
        account=db.get(CapacityAccount,env.id)
        if order.status!='pending_payment' or not account or account.active_order_id!=order.id:
            raise HTTPException(409,'payment_order_closed')
        receipt=PaymentReceipt(order_id=order.id,state='creating');db.add(receipt);db.commit()
        # Commit intent before HTTP. A lost response must not blindly create another provider order.
        try:url=LocalPaymentGateway(settings).create(order)
        except PaymentError:
            db.refresh(receipt,with_for_update=True)
            if receipt.state=='creating':receipt.state='requires_review'
            db.commit();return receipt_out(receipt)
        db.refresh(receipt,with_for_update=True)
        receipt.checkout_url=url
        if receipt.state=='creating':receipt.state='awaiting_payment'
        db.commit();return receipt_out(receipt)

    @router.get('/internal/v1/workspace/capacity/orders/{order_id}/payment')
    def status(order_id:UUID,principal=Depends(principal_dependency),db=Depends(get_db)):
        configured();env=own_environment(db,principal);db.refresh(env,with_for_update=True)
        order=db.get(CapacityOrder,str(order_id))
        if not order or order.environment_id!=env.id:raise HTTPException(404,'payment_order_not_found')
        row=db.get(PaymentReceipt,order.id)
        return observe_intent(db,env,row) if row else {'state':'not_started','checkout_url':None,'mode':'local_simulator'}

    @router.post('/payments/xunhupay/notify',response_class=PlainTextResponse)
    async def notify(request:Request,db=Depends(get_db)):
        settings=configured()
        if request.headers.get('content-type','').split(';')[0]!='application/x-www-form-urlencoded':
            raise HTTPException(400,'payment_invalid_notification')
        content=await request.body()
        if len(content)>8192:raise HTTPException(413,'payment_invalid_notification')
        try:
            pairs=parse_qsl(content.decode('utf-8'),keep_blank_values=True,strict_parsing=True)
            values=dict(pairs)
            if len(values)!=len(pairs) or not verify(values,settings.xunhupay_secret) or values.get('appid')!=settings.xunhupay_appid:
                raise ValueError()
            merchant=values.get('trade_order_id','')
            if not re.fullmatch('[0-9a-f]{32}',merchant):raise ValueError()
            order_id=str(UUID(hex=merchant))
            transaction=values.get('transaction_id','')
            if not re.fullmatch('[A-Za-z0-9_-]{1,32}',transaction):raise ValueError()
            if not values.get('nonce_str') or abs(time.time()-int(values['time']))>86400:raise ValueError()
            paid=amount_minor(values['total_fee'])
        except (ValueError,KeyError,UnicodeError):raise HTTPException(400,'payment_invalid_notification') from None
        order=db.get(CapacityOrder,order_id)
        if not order:raise HTTPException(404,'payment_order_not_found')
        env=db.get(UserEnvironment,order.environment_id,with_for_update=True)
        db.refresh(order,with_for_update=True)
        receipt=db.get(PaymentReceipt,order.id,with_for_update=True)
        if not receipt or paid!=order.plan['amount_minor'] or order.plan['currency']!='CNY':
            raise HTTPException(409,'payment_order_mismatch')
        if receipt.transaction_id and receipt.transaction_id!=transaction:
            raise HTTPException(409,'payment_transaction_mismatch')
        if values.get('status')!='OD':
            # Refund statuses require a separate reviewed compensation workflow, not another grant.
            raise HTTPException(409,'payment_status_requires_review')
        if receipt.confirmed_at and receipt.state in ('confirmed','requires_review'):return 'success'
        receipt.transaction_id=transaction;receipt.confirmed_at=now()
        try:db.flush()
        except IntegrityError:
            db.rollback();raise HTTPException(409,'payment_transaction_mismatch') from None
        account=db.get(CapacityAccount,env.id)
        if order.status!='pending_payment' or not account or account.active_order_id!=order.id or env.status in ('suspended','deleted'):
            receipt.state='requires_review'
            order.payment_status='confirmed'
            order.error_code='payment_late_requires_review'
            order.evidence={**order.evidence,'payment_review_required':True}
        else:
            receipt.state='confirmed'
            order.status,order.payment_status,order.stage='applying','confirmed','checking_storage'
            order.started_at=now();order.completed_at=None;order.error_code=None;order.attempt+=1
        db.add(AuditEvent(tenant_id=env.first_tenant_id,issuer='internal:payment-simulator',subject='xunhupay',
            action='payment.'+receipt.state,target_id=order.id))
        db.commit();return 'success'

    return router
