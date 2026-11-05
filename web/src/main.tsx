import { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { UserManager } from 'oidc-client-ts';
import { api, desktop, identity, initTransport, setBearer } from './api';
import type { IdentityConfig } from './api';
import './style.css';
import AISettings from './AISettings';
import MCPSettings from './MCPSettings';
import ReleaseReviews from './ReleaseReviews';
import AdminUsers from './AdminUsers';
const pages = {
  plans: {nav:'套餐与配额', title:'套餐与配额', description:'设置新用户的体验权益，以及付费套餐的价格、模型额度和资源上限。'},
  models: {nav:'模型与结算', title:'模型与结算', description:'管理聊天和编程模型，明确人民币模型额度到美元预算的折算规则。'},
  users: {nav:'用户与资源', title:'用户与资源', description:'查看用户的独立空间、实际用量和已生效权益。'},
  reviews: {nav:'待核验', title:'发布核验工作台', description:'对比发布意图与集群状态，核验后恢复任务并保留审计记录。'},
  mcp: {nav:'MCP 服务', title:'MCP 服务', description:'接入、审核和管理可供用户选择的编程工具服务。'},
} as const;
function App() {
  const [manager,setManager]=useState<UserManager>();
  const [ready,setReady]=useState(false);
  const [signedIn,setSignedIn]=useState(false);
  const [admin,setAdmin]=useState(false);
  const [page,setPage]=useState<keyof typeof pages>('plans');
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [dirty,setDirty]=useState(false);
  async function perform(action:()=>Promise<unknown>){setError('');setBusy(true);try{await action();}catch(e){setError(e instanceof Error?e.message:'操作未完成。');}finally{setBusy(false);}}
  useEffect(()=>{void perform(async()=>{
    await initTransport();const config=await api<IdentityConfig>('/v1/config');
    if(desktop){setReady(true);return;}
    if(!config.issuer||!config.client_id)throw new Error('请先完成统一登录配置。');
    const auth=identity(config);setManager(auth);
    const query=new URLSearchParams(location.search);
    if(query.has('code')&&query.has('state')){
      const user=await auth.signinRedirectCallback(location.href);
      history.replaceState({},'', '/');setBearer(user.access_token);
      const me=await api<{platform_admin:boolean}>('/v1/me');setAdmin(me.platform_admin);setSignedIn(true);
    }
    setReady(true);
  });},[]);
  function navigate(next:keyof typeof pages){if(next===page)return;if(dirty&&!window.confirm('有尚未保存的配置，确定放弃修改并切换页面吗？'))return;setDirty(false);setPage(next);}
  return <div className="shell"><aside><div className="brand">◈ <strong>星桥</strong></div><div className="aside-caption">管理员控制台</div>
    <nav aria-label="管理导航">{signedIn&&admin&&Object.entries(pages).map(([key,value])=><button key={key} className={`nav ${page===key?'active':''}`} aria-current={page===key?'page':undefined} onClick={()=>navigate(key as keyof typeof pages)}>{value.nav}</button>)}</nav><div className="aside-footer">统一管理 · 用户独立空间</div></aside>
    <main><header><div><div className="eyebrow">平台管理</div><h1>{pages[page].title}</h1><p>{pages[page].description}</p></div>{signedIn&&<button className="subtle" onClick={()=>{if(!dirty||window.confirm('放弃未保存的修改并退出？'))void manager?.signoutRedirect();}}>退出登录</button>}</header>
    {error&&<div className="error" role="alert">{error}</div>}
    {!signedIn?<section className="panel welcome"><h2>登录管理平台</h2><p>{desktop?'请通过浏览器完成管理员登录。':'使用管理员账号管理用户、套餐与平台服务。'}</p><button disabled={!ready||!manager||busy} onClick={()=>void perform(async()=>manager?.signinRedirect())}>使用 Authentik 登录</button></section>:
    !admin?<section className="panel"><h2>这里是管理员控制台</h2><p>请在 LibreChat 使用聊天、我的空间和应用管理。个人空间会在首次登录时自动准备，无需在此创建。</p></section>:
    page==='users'?<AdminUsers/>:page==='reviews'?<ReleaseReviews/>:page==='mcp'?<MCPSettings/>:<AISettings key={page} view={page} onDirty={setDirty}/>}
    </main></div>;
}
createRoot(document.getElementById('root')!).render(<App/>);
