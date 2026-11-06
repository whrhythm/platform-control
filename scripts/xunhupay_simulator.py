"""Local-only Xunhupay V3 stand-in, with a synthetic checkout page and signed callbacks.

Never accepts production credentials or remote callback URLs. State is intentionally ephemeral.
"""
import argparse
import html
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from control_plane.xunhupay import PaymentError, signature, verify, loopback_url, amount_minor


def create_simulator(appid, secret, origin, notify_url):
    loopback_url(origin);loopback_url(notify_url)
    if not appid.startswith('sim-') or len(secret)<32:
        raise ValueError('Only synthetic sim- credentials are accepted')
    app=FastAPI();orders={};app.state.orders=orders

    def signed(body):return {**body,'hash':signature(body,secret)}

    def checkout_page(identifier, row, message='确认后会向本地平台发送模拟付款通知。'):
        return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>本地模拟支付</title>
<style>body{margin:0;background:#f5f7fa;color:#182535;font:15px/1.6 system-ui,sans-serif}
main{max-width:480px;margin:10vh auto;padding:28px;background:white;border:1px solid #dde3eb;border-radius:16px}
h1{font-size:22px;margin:12px 0}.notice{color:#715109;background:#fff7df;border-radius:8px;padding:10px 14px}
.amount{font-size:36px;font-weight:650;margin:24px 0 12px}.id{color:#607086;font-size:12px;overflow-wrap:anywhere}
button{background:#234b69;color:white;border:0;border-radius:8px;padding:12px 18px;font:inherit;cursor:pointer}
@media(max-width:550px){main{margin:24px 12px;padding:20px}}</style><main>
<h1>虎皮椒 V3 本地模拟器</h1><p class="notice">测试环境，不会扣款。</p><p class="amount">¥'''+html.escape(row['total_fee'])+'''</p>
<p class="id">订单 '''+html.escape(identifier)+'''</p><p role="status">'''+html.escape(message)+'''</p>
<form method="post"><button name="action" value="paid">模拟支付成功 / 重发通知</button></form>
<p class="id">付款确认不等于权益开通。请返回平台“套餐与用量”查看模型额度和资源进度。</p></main></html>'''

    @app.post('/payment/do.html')
    async def create(request:Request):
        raw=await request.body()
        if len(raw)>8192:raise HTTPException(413)
        if 'application/json' in request.headers.get('content-type',''):
            body=await request.json()
        else:body=dict(parse_qsl(raw.decode(),keep_blank_values=True))
        if not verify(body,secret) or body.get('appid')!=appid:
            return signed({'errcode':500,'errmsg':'invalid sign!'})
        if body.get('version')!='1.1' or body.get('notify_url')!=notify_url:
            return signed({'errcode':500,'errmsg':'invalid parameters'})
        identifier=body.get('trade_order_id','')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',identifier):raise HTTPException(400)
        try:
            if amount_minor(str(body.get('total_fee','')))<=0:raise PaymentError('invalid_amount')
        except PaymentError:raise HTTPException(400,'invalid_amount') from None
        # Simulator-specific retry guarantee, NOT an assertion about provider idempotency.
        previous=orders.get(identifier)
        if previous and previous['total_fee']!=str(body['total_fee']):
            return signed({'errcode':500,'errmsg':'order conflict'})
        if not previous:
            orders[identifier]={**body,'total_fee':str(body['total_fee']),'status':'WP',
                'transaction_id':secrets.token_hex(16),'open_order_id':secrets.token_hex(16),'notifications':0}
        return signed({'errcode':0,'errmsg':'success!','openid':orders[identifier]['open_order_id'],
            'url':origin+'/checkout/'+identifier})

    @app.get('/checkout/{identifier}',response_class=HTMLResponse)
    async def checkout(identifier:str):
        row=orders.get(identifier)
        if not row:raise HTTPException(404)
        return checkout_page(identifier,row)

    @app.post('/checkout/{identifier}')
    async def pay(identifier:str,request:Request):
        row=orders.get(identifier)
        if not row:raise HTTPException(404)
        row['status']='OD'
        body={key:row[key] for key in ('trade_order_id','total_fee','transaction_id','open_order_id','status','appid')}
        body.update(order_title=row['title'],time=str(int(time.time())),nonce_str=secrets.token_hex(8))
        row['notifications']+=1
        try:
            async with httpx.AsyncClient(timeout=5,trust_env=False,follow_redirects=False) as client:
                response=await client.post(notify_url,data=signed(body))
            acknowledged=response.status_code==200 and response.text=='success'
        except httpx.HTTPError:acknowledged=False
        if 'text/html' in request.headers.get('accept',''):
            return HTMLResponse(checkout_page(identifier,row,
                '模拟付款已确认。请返回平台查看权益开通进度。' if acknowledged else
                '已记录模拟付款，但平台尚未确认。可以重发通知，无需重新付款。'))
        return {'simulated_paid':True,'callback_acknowledged':acknowledged,'notifications':row['notifications'],
            'message':'回到平台查看权益开通进度。' if acknowledged else '通知未确认，可重发；不会生成第二笔模拟付款。'}

    @app.api_route('/payment/{path:path}',methods=['GET','POST'])
    async def unsupported(path:str):
        raise HTTPException(501,'This simulator does not model query/refund contracts')
    return app


if __name__=='__main__':
    import os
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=9066)
    parser.add_argument('--notify-url',default='http://127.0.0.1:8088/payments/xunhupay/notify')
    args=parser.parse_args()
    secret=os.environ.get('XUNHUPAY_SIM_SECRET','')
    app=create_simulator(os.environ.get('XUNHUPAY_SIM_APPID','sim-starbridge'),secret,
        'http://127.0.0.1:'+str(args.port),args.notify_url)
    uvicorn.run(app,host='127.0.0.1',port=args.port,access_log=False)
