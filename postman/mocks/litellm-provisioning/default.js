// Based on the deployed LiteLLM v1.109.0-dev.2 probe. Test data only.
const http = require('http');
const crypto = require('crypto');
const send = (res, code, body) => { res.writeHead(code, {'Content-Type': 'application/json'}); res.end(JSON.stringify(body)); };
http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const users = (await pm.state.get('litellm_users')) || {};
  const keys = (await pm.state.get('litellm_keys')) || {};
  if (req.method === 'GET' && url.pathname === '/user/info') {
    const user = users[url.searchParams.get('user_id')];
    return send(res, user ? 200 : 404, user ? {user_info: user} : {});
  }
  if (req.method === 'POST' && url.pathname === '/user/new') {
    const body = req.body || {};
    if (!body.user_id || body.auto_create_key !== false) return send(res, 400, {});
    if (users[body.user_id]) return send(res, 409, {});
    users[body.user_id] = body;
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
    keys[hash] = {user_id: body.user_id, models: body.models};
    await pm.state.set('litellm_keys', keys);
    return send(res, 200, {key: body.key});
  }
  return send(res, 501, {error: 'Not modelled'});
}).listen(process.env.PORT || 4500);
