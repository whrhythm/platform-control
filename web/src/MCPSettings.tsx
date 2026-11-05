import React, { useEffect, useState } from 'react';
import { api } from './api';
import './MCPSettings.css';

type Tool = {name:string;description?:string;inputSchema:Record<string,unknown>;supported?:boolean;permission?:string};
type Server = {id:string;revision:number;state:string;name:string;description:string;url:string;auth:string;data_notice:string;has_credential:boolean;discovered:Tool[];approved:Tool[];addresses:string[]};
const empty = {name:'',description:'',url:'',auth:'none',data_notice:'工具参数会发送给该服务，请勿提交与本次任务无关的私有数据。'};
const labels:Record<string,string>={draft:'草稿',verified:'待审核',published:'已发布',disabled:'已停用',archived:'已归档'};

export default function MCPSettings(){
  const [list,setList]=useState<Server[]>([]);
  const [current,setCurrent]=useState<Server>();
  const [form,setForm]=useState(empty);
  const [secret,setSecret]=useState('');
  const [probeSecret,setProbeSecret]=useState('');
  const [permissions,setPermissions]=useState<Record<string,string>>({});
  const [ack,setAck]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [dirty,setDirty]=useState(false);
  async function load(){const result=await api<{servers:Server[]}>('/v1/admin/mcp');setList(result.servers);return result.servers;}
  function choose(server?:Server){setCurrent(server);setForm(server?{name:server.name,description:server.description,url:server.url,auth:server.auth,data_notice:server.data_notice}:empty);setSecret('');setProbeSecret('');setAck(false);setDirty(false);setPermissions(Object.fromEntries((server?.approved??[]).map(t=>[t.name,t.permission??'disabled'])));}
  useEffect(()=>{void load().catch(()=>setError('MCP 目录无法加载，请刷新重试。'));},[]);
  function edit(patch:Partial<typeof empty>){setForm({...form,...patch});setDirty(true);setNotice('');}
  async function perform(action:()=>Promise<Server>,message:string){setBusy(true);setError('');setNotice('');try{const value=await action();await load();choose(value);setNotice(message);}catch{setError('操作未完成。请检查地址、凭据和工具权限；连接检查失败或配置冲突后，请刷新目录核对当前状态。');}finally{setBusy(false);setSecret('');setProbeSecret('');}}
  async function refresh(){setBusy(true);setError('');try{const values=await load();if(current)choose(values.find(s=>s.id===current.id));}catch{setError('暂时无法读取配置。');}finally{setBusy(false);}}
  const path=current?'/v1/admin/mcp/'+encodeURIComponent(current.id):'/v1/admin/mcp';
  return <section className="panel mcp-admin" aria-label="编程 MCP 连接管理">
    <div className="panel-heading"><div><h2>MCP 服务目录</h2><p>先保存连接、检查工具，再逐项审核发布。修改已发布连接会立即撤销旧版本权限。</p></div><button className="subtle" disabled={busy} onClick={()=>void refresh()}>刷新 MCP 目录</button></div>
    {error&&<p className="error" role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}
    <div className="mcp-columns"><nav aria-label="MCP 服务列表"><button disabled={busy} onClick={()=>choose()}>新增 MCP 服务</button>{list.map(s=><button className={current?.id===s.id?'mcp-selected':'subtle'} key={s.id} disabled={busy} onClick={()=>choose(s)}><strong>{s.name}</strong><small>{labels[s.state]??s.state}</small></button>)}</nav>
      <div className="mcp-editor"><form onSubmit={e=>{e.preventDefault();void perform(()=>api<Server>(path,current?'PUT':'POST',{...form,revision:current?.revision??0,...(secret?{secret}:{})}),'草稿已保存。请检查连接并重新审核工具。');}}>
        <h3>{current?current.name:'新增 MCP 服务'}</h3>
        <div className="mcp-fields"><label>服务名称<input required maxLength={80} value={form.name} onChange={e=>edit({name:e.target.value})}/></label>
        <label>HTTPS MCP 地址<input required type="url" placeholder="https://example.com/mcp" value={form.url} onChange={e=>edit({url:e.target.value})}/></label>
        <label>凭据归属<select value={form.auth} onChange={e=>edit({auth:e.target.value})}><option value="none">公共服务，无需凭据</option><option value="platform_bearer">平台专用只读凭据</option><option value="user_bearer">每位用户配置自己的凭据</option></select></label>
        {form.auth==='platform_bearer'&&<label>{current?.has_credential?'替换平台凭据（留空保留，变更地址须重新填写）':'平台服务专用 Bearer Token'}<input type="password" autoComplete="new-password" maxLength={4096} value={secret} onChange={e=>{setSecret(e.target.value);setDirty(true);}}/></label>}
        </div><label>用途说明<textarea maxLength={500} value={form.description} onChange={e=>edit({description:e.target.value})}/></label>
        <label>向用户说明数据去向<textarea required maxLength={500} value={form.data_notice} onChange={e=>edit({data_notice:e.target.value})}/></label>
        <p className="mcp-hint">支持公网 HTTPS / Streamable HTTP，端口 443。密钥加密保存且不回显。平台凭据只用于共享只读服务，不能使用集群、模型平台或个人管理员密钥。OAuth、内网地址和本地命令暂不支持。</p>
        <button disabled={busy||!!current&&!dirty}>{busy?'处理中…':'保存草稿'}</button>
      </form>
      {current&&<div className="mcp-review"><h3>连接检查与工具审核</h3><p>当前状态：{labels[current.state]} · 版本 {current.revision}。检查会使旧版本下线，凭据仅发给上方固定地址。</p>
        {current.auth==='user_bearer'&&<label>仅本次连接检查使用的测试 Token（不保存）<input type="password" autoComplete="new-password" value={probeSecret} onChange={e=>setProbeSecret(e.target.value)}/></label>}
        <button disabled={busy||dirty||(current.auth==='user_bearer'&&!probeSecret)} onClick={()=>void perform(()=>api<Server>(path+'/probe','POST',{revision:current.revision,...(probeSecret?{secret:probeSecret}:{})}),'连接检查通过。请选择允许的工具并审核发布。')}>检查连接与发现工具</button>
        {current.addresses.length>0&&<p className="mcp-hint">已核验公网地址：{current.addresses.join('、')}</p>}
        {current.discovered.map(tool=><article key={tool.name} className="mcp-tool"><div><strong>{tool.name}</strong><select aria-label={'工具权限 '+tool.name} disabled={busy||current.state!=='verified'||!tool.supported} value={permissions[tool.name]??'disabled'} onChange={e=>setPermissions({...permissions,[tool.name]:e.target.value})}><option value="disabled">不开放</option><option value="read">只读，每次确认</option>{current.auth!=='platform_bearer'&&<option value="write">外部写入，每次确认</option>}</select></div><p>{tool.description}</p>{!tool.supported&&<p>此工具参数结构暂不支持，不能发布。</p>}<details><summary>查看参数结构</summary><pre>{JSON.stringify(tool.inputSchema,null,2)}</pre></details></article>)}
        {current.state==='verified'&&<><label className="mcp-ack"><input type="checkbox" checked={ack} onChange={e=>setAck(e.target.checked)}/>我已审核服务用途、数据范围与各工具副作用，确认所选权限；不只依赖服务自报的“只读”标记。</label><button disabled={busy||dirty||!ack||!Object.values(permissions).some(p=>p==='read'||p==='write')} onClick={()=>void perform(()=>api<Server>(path+'/publish','POST',{revision:current.revision,acknowledged:ack,tools:Object.fromEntries(Object.entries(permissions).filter(([,p])=>p!=='disabled'))}),'已审核发布，用户现在可在编程设置中选择。')}>审核并发布</button></>}
        <div className="mcp-actions"><button className="subtle" disabled={busy||current.state==='disabled'||current.state==='archived'} onClick={()=>void perform(()=>api<Server>(path+'/disable','POST',{revision:current.revision}),'服务已停用，旧任务不能继续调用。')}>立即停用</button><button className="subtle" disabled={busy||current.state==='archived'} onClick={()=>{if(window.confirm('归档将停用服务并清除关联凭据，保留审计。是否继续？'))void perform(()=>api<Server>(path+'/archive','POST',{revision:current.revision}),'服务已归档，关联凭据已清除。');}}>归档并清除凭据</button></div>
      </div>}</div>
    </div>
  </section>;
}
