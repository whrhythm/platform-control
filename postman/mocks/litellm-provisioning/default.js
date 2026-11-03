// Based on the deployed LiteLLM v1.109.0-dev.2 probe. Test data only.
const http = require('http');
const crypto = require('crypto');
const send = (res, code, body) => { res.writeHead(code, {'Content-Type': 'application/json'}); res.end(JSON.stringify(body)); };
http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const users = (await pm.state.get('litellm_users')) || {};
  const keys = (await pm.state.get('litellm_keys')) || {};
  if(req.method==='POST'&&url.pathname==='/__test__/reset'){
    await pm.state.set('litellm_users',{});await pm.state.set('litellm_keys',{});await pm.state.set('litellm_writes',[]);await pm.state.set('litellm_delay',0);return send(res,200,{});
  }
  if(req.method==='POST'&&url.pathname==='/__test__/fault'){
    await pm.state.set('litellm_delay',req.body.after_user_update_ms||0);return send(res,200,{});
  }
  if(req.method==='GET'&&url.pathname==='/__test__/snapshot')return send(res,200,{users,keys,writes:(await pm.state.get('litellm_writes'))||[]});
  if(req.method==='GET'&&url.pathname==='/model/info')return send(res,200,{data:[{model_name:'sim-model',model_info:{input_cost_per_token:0.000001,output_cost_per_token:0.000002}}]});
  if(req.method==='GET'&&url.pathname==='/user/daily/activity/aggregated'){
    if(!users[url.searchParams.get('user_id')])return send(res,404,{});
    return send(res,200,{metadata:{total_tokens:300,total_prompt_tokens:200,total_completion_tokens:100,has_more:false}});
  }
  if(req.method==='POST'&&['/user/update','/key/update'].includes(url.pathname)){
    const body=req.body||{};if(Object.keys(body).some(key=>!['user_id','key','max_budget','models'].includes(key)))return send(res,400,{});
    const hash=typeof body.key==='string'?(body.key.startsWith('sk-')?crypto.createHash('sha256').update(body.key).digest('hex'):body.key):null;
    const target=url.pathname==='/user/update'?users[body.user_id]:keys[hash];if(!target)return send(res,404,{});
    if(body.max_budget!==undefined)target.max_budget=body.max_budget;if(body.models!==undefined)target.models=body.models;
    await pm.state.set('litellm_users',users);await pm.state.set('litellm_keys',keys);
    const writes=(await pm.state.get('litellm_writes'))||[];writes.push({path:url.pathname,max_budget:body.max_budget});await pm.state.set('litellm_writes',writes);
    const delay=url.pathname==='/user/update'?(await pm.state.get('litellm_delay'))||0:0;
    if(delay){await pm.state.set('litellm_delay',0);setTimeout(()=>{if(!res.destroyed)send(res,200,{})},delay);return;}
    return send(res,200,{});
  }
  if (req.method === 'GET' && url.pathname === '/user/info') {
    const user = users[url.searchParams.get('user_id')];
    return send(res, user ? 200 : 404, user ? {user_info: user} : {});
  }
  if (req.method === 'POST' && url.pathname === '/user/new') {
    const body = req.body || {};
    if (!body.user_id || body.auto_create_key !== false) return send(res, 400, {});
    if (users[body.user_id]) return send(res, 409, {});
    users[body.user_id] = {...body,spend:0.4};
    await pm.state.set('litellm_users', users);
    return send(res, 200, {user_id: body.user_id});
  }
  if (req.method === 'GET' && url.pathname === '/key/info') {
    const info = keys[url.searchParams.get('key')];
    return send(res, info ? 200 : 404, info ? {info} : {});
  }
  if (req.method === 'POST' && url.pathname === '/key/generate') {
    const body = req.body || {};
    if (!body.key || !users[body.user_id]) return send(res, 400, {});
    const hash = crypto.createHash('sha256').update(body.key).digest('hex');
    keys[hash] = {user_id: body.user_id, models: body.models,max_budget:body.max_budget,spend:0.4};
    await pm.state.set('litellm_keys', keys);
    return send(res, 200, {key: body.key});
  }
  return send(res, 501, {error: 'Not modelled'});
}).listen(process.env.PORT || 4500);
