import { useEffect, useState } from 'react';
import { api } from './api';
import Modal from './Modal';
type Account={pk:number;username:string;name:string;email:string;is_active:boolean;protected:boolean;platform_blocked:boolean;revision:string};
type Page={items:Account[];next_page:number|null};
type Draft={pk?:number;username:string;name:string;email:string;is_active:boolean;revision:string;password:string};
const blank:Draft={username:'',name:'',email:'',is_active:true,revision:'',password:''};
export default function Accounts(){
 const [data,setData]=useState<Page>();const [search,setSearch]=useState('');const [page,setPage]=useState(1);const [version,setVersion]=useState(0);
 const [draft,setDraft]=useState<Draft>();const [busy,setBusy]=useState(false);const [error,setError]=useState('');const [notice,setNotice]=useState('');
 useEffect(()=>{let active=true;setData(undefined);void api<Page>('/v1/admin/accounts?'+new URLSearchParams({q:search,page:String(page)})).then(v=>{if(active)setData(v);}).catch(()=>{if(active)setError('暂时无法读取 Authentik 账号，请稍后刷新。');});return()=>{active=false;};},[search,page,version]);
 function close(){if(!busy)setDraft(undefined);}
 async function save(){if(!draft)return;setBusy(true);setError('');try{
  const {pk,password,...body}=draft;
  await api('/v1/admin/accounts'+(pk?'/'+pk:''),pk?'PUT':'POST',{...body,...(password?{password}:{})});
  setDraft(undefined);setNotice('账号设置已保存。禁用会阻止新请求，已有文件和应用保留。');setVersion(v=>v+1);
 }catch{setError('操作未确认，请先刷新核对账号状态，不要重复新增。若账号已经创建但尚未完成初始化，可编辑账号设置初始密码并启用。');setDraft(v=>v?{...v,password:''}:v);}finally{setBusy(false);}}
 return <section className="panel" aria-label="登录账号管理">
  <div className="account-actions"><h2>登录账号管理</h2><div><button className="subtle" onClick={()=>{setError('');setVersion(v=>v+1);}}>刷新账号</button><button onClick={()=>{setError('');setDraft({...blank});}}>添加用户</button></div></div>
  <p>账号统一保存在 Authentik。用户首次登录后自动准备个人空间。管理员与服务账号受保护；禁用不会删除文件、数据库或已部署应用。</p>
  <label>搜索账号<input value={search} onChange={e=>{setSearch(e.target.value);setPage(1);}} placeholder="用户名、姓名或邮箱"/></label>
  {error&&!draft&&<p role="alert" className="error">{error}</p>}{notice&&<p role="status">{notice}</p>}
  {!data?<p role="status">正在读取账号…</p>:<><div className="inventory-scroll"><table className="inventory-table"><thead><tr><th>用户名</th><th>姓名与邮箱</th><th>登录状态</th><th>管理</th></tr></thead><tbody>{data.items.map(row=><tr key={row.pk}><td>{row.username}</td><td>{row.name}<span className="inventory-meta">{row.email}</span></td><td>{row.platform_blocked?'平台已限制访问':row.is_active?'已启用':'已禁用'}</td><td>{row.protected?<span>受保护账号</span>:<button className="subtle" onClick={()=>{setError('');setDraft({pk:row.pk,username:row.username,name:row.name,email:row.email,is_active:row.is_active&&!row.platform_blocked,revision:row.revision,password:''});}}>编辑用户</button>}</td></tr>)}</tbody></table></div>
  <div className="inventory-pagination"><button disabled={page===1} onClick={()=>setPage(p=>p-1)}>上一页账号</button><span>第 {page} 页</span><button disabled={!data.next_page} onClick={()=>setPage(p=>p+1)}>下一页账号</button></div></>}
  {draft&&<Modal title={draft.pk?'编辑用户':'添加用户'} onClose={close}><form className="account-form" onSubmit={e=>{e.preventDefault();void save();}}>
   <label>用户名<input required maxLength={150} value={draft.username} onChange={e=>setDraft({...draft,username:e.target.value})}/></label>
   <label>姓名<input required maxLength={150} value={draft.name} onChange={e=>setDraft({...draft,name:e.target.value})}/></label>
   <label>邮箱<input type="email" maxLength={254} value={draft.email} onChange={e=>setDraft({...draft,email:e.target.value})}/></label>
   <label>{draft.pk?'重设密码（留空保留）':'初始密码'}<input type="password" autoComplete="new-password" required={!draft.pk} minLength={12} maxLength={256} value={draft.password} onChange={e=>setDraft({...draft,password:e.target.value})}/></label>
   <label className="account-active"><input type="checkbox" checked={draft.is_active} onChange={e=>setDraft({...draft,is_active:e.target.checked})}/>允许登录与使用平台</label>
   <p className="account-help">取消勾选即可禁用。平台新请求最迟在 15 秒缓存到期后被阻止，运行中的编程任务会停止后续操作；已发布业务继续运行。密码至少 12 个字符，不回显、不写入平台数据库。</p>
   {error&&<p role="alert" className="error account-help">{error}</p>}<footer><button type="submit" disabled={busy}>{busy?'正在保存…':'保存用户'}</button><button type="button" className="subtle" disabled={busy} onClick={close}>取消</button></footer>
  </form></Modal>}
 </section>;
}
