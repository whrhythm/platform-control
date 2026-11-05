const http = require('http');
http.createServer(async (req, res) => {
  if (req.url === '/calls') {
    res.setHeader('Content-Type','application/json');
    return res.end(JSON.stringify({calls:await pm.state.get('agent_stream_calls') || []}));
  }
  if (req.url !== '/v1/chat/completions' || req.method !== 'POST') {res.writeHead(501);return res.end();}
  const calls = await pm.state.get('agent_stream_calls') || [];
  calls.push({stream:req.body.stream,finishAvailable:req.body.tools.some(t=>t.function.name==='finish')});
  await pm.state.set('agent_stream_calls',calls);
  if (!req.body.stream) {res.writeHead(400);return res.end();}
  res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-store'});
  const emit=(delta,finish=null)=>res.write('data: '+JSON.stringify({id:'synthetic-stream',object:'chat.completion.chunk',created:1,model:'test',choices:[{index:0,delta,finish_reason:finish}]})+'\n\n');
  emit({role:'assistant',reasoning_content:'SYNTHETIC_PRIVATE_REASONING'});
  emit({tool_calls:[{index:0,id:'finish-call',type:'function',function:{name:'finish',arguments:''}}]});
  const message=JSON.stringify({message:'流式输出验证：'+ '这是逐步到达的公开回答。'.repeat(24)});
  for(let i=0;i<message.length;i+=12){
    emit({tool_calls:[{index:0,function:{arguments:message.slice(i,i+12)}}]});
    await new Promise(resolve=>setTimeout(resolve,140));
  }
  emit({},'tool_calls');res.end('data: [DONE]\n\n');
}).listen(process.env.PORT);
