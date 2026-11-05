// MCP 2025-11-25 Streamable HTTP, synthetic public documentation only.
const http = require('http');
const send = (res, code, body) => {res.writeHead(code, {'Content-Type':'application/json'});res.end(JSON.stringify(body));};
http.createServer(async (req,res) => {
  const body = req.body || {};
  if(req.url === '/__seed' && req.method === 'POST') {
    await pm.state.set('mcp_docs_mode', body.mode);
    await pm.state.set('mcp_docs_calls', 0);
    return send(res,200,{});
  }
  if(req.url === '/__calls') return send(res,200,{calls:(await pm.state.get('mcp_docs_calls')) || 0});
  if(req.url !== '/mcp') return send(res,501,{error:'Not modelled'});
  if(req.method === 'GET' || req.method === 'DELETE') return send(res,405,{});
  const reply = result => send(res,200,{jsonrpc:'2.0',id:body.id,result});
  if(body.method === 'initialize') return reply({protocolVersion:'2025-11-25',capabilities:{tools:{}},serverInfo:{name:'synthetic-docs',version:'1'}});
  if(body.method === 'notifications/initialized') {res.writeHead(202);return res.end();}
  if(body.method === 'tools/list') return reply({tools:[{name:'read_wiki_structure',description:'Synthetic docs',inputSchema:{type:'object',properties:{repoName:{type:'string'}},required:['repoName']}}]});
  if(body.method === 'tools/call') {
    await pm.state.set('mcp_docs_calls', ((await pm.state.get('mcp_docs_calls')) || 0) + 1);
    const mode = await pm.state.get('mcp_docs_mode');
    if(mode === 'unavailable') return send(res,503,{error:'synthetic private diagnostic'});
    if(mode === 'slow') {await new Promise(resolve=>setTimeout(resolve,7000));}
    if(mode === 'tool_error') return reply({isError:true,content:[{type:'text',text:'private diagnostic'}]});
    if(mode === 'large') return reply({content:[{type:'text',text:'x'.repeat(300000)}]});
    return reply({content:[{type:'text',text:'Synthetic documentation: use a dependency declaration.'}]});
  }
  return send(res,501,{error:'Not modelled'});
}).listen(process.env.PORT || 4507);
