const http=require('http');
const send=(res,status,body)=>{res.writeHead(status,{'Content-Type':'application/json'});res.end(status===204?'':JSON.stringify(body));};
http.createServer(async(req,res)=>{
 const raw=req.url.split('?')[0], path=raw.startsWith('/core/')?raw.replace(/\/?$/,'/'):raw, body=req.body||{};
 if(path==='/__seed'){await pm.state.set('identity_rows',{});await pm.state.set('identity_fail',body.fail||false);await pm.state.set('identity_writes',0);return send(res,200,{});}
 if(path==='/__writes')return send(res,200,{count:await pm.state.get('identity_writes')});
 if(req.headers.authorization!=='Bearer synthetic-directory-token')return send(res,403,{});
 const rows=(await pm.state.get('identity_rows'))||{};
 if(path==='/core/users/' && req.method==='GET')return send(res,200,{results:Object.values(rows),pagination:{next:0}});
 if(path==='/core/users/' && req.method==='POST'){
  if(Object.values(rows).some(r=>r.username===body.username))return send(res,400,{});
  const pk=Object.keys(rows).length+1;
  const row={...body,pk,uid:'synthetic-'+pk,groups_obj:[],is_superuser:false};
  rows[pk]=row;await pm.state.set('identity_rows',rows);return send(res,201,row);
 }
 const match=path.match(/^\/core\/users\/(\d+)\/(set_password\/)?$/);
 if(!match)return send(res,501,{});
 const row=rows[match[1]];if(!row)return send(res,404,{});
 if(req.method==='GET')return send(res,200,row);
 if(match[2]&&req.method==='POST')return send(res,204,{});
 if(req.method==='PATCH'){
  rows[row.pk]={...row,...body};await pm.state.set('identity_rows',rows);
  await pm.state.set('identity_writes',((await pm.state.get('identity_writes'))||0)+1);
  if(await pm.state.get('identity_fail'))return send(res,503,{error:'private-provider-diagnostic'});
  return send(res,200,rows[row.pk]);
 }
 return send(res,501,{});
}).listen(process.env.PORT);
