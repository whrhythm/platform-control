import React, { useEffect, useState } from 'react';
import { api } from './api';

type Connector = {id:string;name:string;description:string;url:string;tools:string[];enabled:boolean;data_notice:string};
type State = {revision:number;servers:Connector[]};

export default function MCPSettings() {
  const [state,setState] = useState<State>();
  const [busy,setBusy] = useState(false);
  const [error,setError] = useState('');
  const [saved,setSaved] = useState(false);
  async function load(){setError('');setSaved(false);try{setState(await api<State>('/v1/admin/ai/mcp'));}catch{setError('MCP 配置暂时无法读取，请重试。');}}
  useEffect(()=>{void load();},[]);
  async function save(){if(!state)return;setBusy(true);setError('');setSaved(false);try{setState(await api<State>('/v1/admin/ai/mcp','PUT',{revision:state.revision,servers:state.servers.filter(s=>s.enabled).map(s=>s.id)}));setSaved(true);}catch{setError('未保存。配置可能已更新，请刷新后重试。');}finally{setBusy(false);}}
  return <section className="panel ai-settings" aria-label="编程 MCP 连接管理">
    <div className="panel-heading"><div><h2>编程 MCP 连接</h2><p>管理用户可以选用的外部工具。用户需要自行勾选，平台不会默认替用户开启。</p></div><button type="button" className="subtle" disabled={busy} onClick={()=>void load()}>刷新 MCP 配置</button></div>
    {error&&<p role="alert" className="error">{error}</p>}
    {!state&&!error&&<p role="status">正在读取 MCP 配置…</p>}
    {state&&<form onSubmit={event=>{event.preventDefault();void save();}}>
      {state.servers.map(server=><fieldset key={server.id}><legend>{server.name}</legend>
        <label><input type="checkbox" checked={server.enabled} disabled={busy} onChange={event=>{setSaved(false);setState({...state,servers:state.servers.map(s=>s.id===server.id?{...s,enabled:event.target.checked}:s)});}}/>允许用户选用</label>
        <p>{server.description}</p><p><code>{server.url}</code></p>
        <p>只读工具：{server.tools.join('、')}</p><p>{server.data_notice}</p>
      </fieldset>)}
      <p>每次外发查询都须在聊天中确认。关闭后，等待审批及执行中的查询会停止继续调用；已发送的数据无法撤回。配置变更写入审计。当前不接入私有账号、任意服务器地址或本地命令。</p>
      <button disabled={busy}>{busy?'正在保存…':'保存 MCP 配置'}</button>
      {saved&&<p role="status">MCP 配置已保存。</p>}
    </form>}
  </section>;
}
