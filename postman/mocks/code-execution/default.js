const http = require('http');
const send = (res, status, body) => { res.writeHead(status, {'Content-Type':'application/json'}); res.end(JSON.stringify(body)); };
http.createServer(async (req, res) => {
  const path = req.url.split('?')[0].replace(/\/$/, '');
  if (path === '/__reset') { await pm.state.set('code_execution_calls', []); return send(res,200,{}); }
  if (path === '/__calls') return send(res,200,{calls:await pm.state.get('code_execution_calls') || []});
  if (path !== '/exec' || req.method !== 'POST') return send(res,501,{error:'not modeled'});
  const body = req.body || {};
  const calls = await pm.state.get('code_execution_calls') || [];
  calls.push({timeout:body.timeout, code:body.code}); await pm.state.set('code_execution_calls',calls);
  if (body.code === 'ambiguous') return send(res,503,{error:'private-provider-diagnostic'});
  return send(res,200,{stdout:'42',stderr:'',session_id:'synthetic-session',files:[]});
}).listen(process.env.PORT);
