import React, { useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import type { UserManager } from 'oidc-client-ts';
import { api, desktop, identity, initTransport, setBearer } from './api';
import type { IdentityConfig, Operation, Project, Quota, Resource, Tenant } from './api';
import './style.css';
import AISettings from './AISettings';

const labels: Record<string, string> = {workspace: '工作空间', agent: '编程助手', database: '数据库', bucket: '对象存储', deployment: '应用部署', requested: '已提交', planned: '方案已生成', cancelled: '已取消'};

function App() {
  const [manager, setManager] = useState<UserManager>();
  const [ready, setReady] = useState(false);
  const [signedIn, setSignedIn] = useState(false);
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [tenantId, setTenantId] = useState('');
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState('');
  const [resources, setResources] = useState<Resource[]>([]);
  const [quota, setQuota] = useState<Quota>();
  const [operations, setOperations] = useState<Operation[]>([]);
  const [name, setName] = useState('');
  const [kind, setKind] = useState('workspace');
  const [resourceName, setResourceName] = useState('');
  const [image, setImage] = useState('');
  const [workspace, setWorkspace] = useState('');
  const tenantPath = `/v1/tenants/${tenantId}`;

  async function perform(action: () => Promise<unknown>) {
    setError(''); setBusy(true);
    try {await action();} catch (e) {setError(e instanceof Error ? e.message : '操作未完成。');} finally {setBusy(false);}
  }
  async function loadTenants() {const [values,me] = await Promise.all([api<Tenant[]>('/v1/tenants'),api<{platform_admin:boolean}>('/v1/me')]); setIsAdmin(me.platform_admin); setTenants(values); setTenantId(values[0]?.id || '');}
  async function refreshResources() {
    const [r, q, o] = await Promise.all([api<Resource[]>(`${tenantPath}/projects/${projectId}/resources`), api<Quota>(tenantPath + '/quota'), api<Operation[]>(tenantPath + '/operations')]);
    setResources(r); setQuota(q); setOperations(o);
  }
  useEffect(() => {void perform(async () => {
    await initTransport();
    const config = await api<IdentityConfig>('/v1/config');
    if (desktop) {setReady(true); return;}
    if (!config.issuer || !config.client_id) throw new Error('请先由管理员完成统一登录配置。');
    const auth = identity(config); setManager(auth);
    const query = new URLSearchParams(location.search);
    if (query.has('code') && query.has('state')) {
      const user = await auth.signinRedirectCallback(location.href);
      history.replaceState({}, '', '/'); setBearer(user.access_token); setSignedIn(true); await loadTenants();
    }
    setReady(true);
  });}, []);
  useEffect(() => {
    setProjectId(''); setProjects([]); setResources([]); setQuota(undefined); setOperations([]);
    if (tenantId) void perform(async () => {const p = await api<Project[]>(tenantPath + '/projects'); setProjects(p); setProjectId(p[0]?.id || '');});
  }, [tenantId]);
  useEffect(() => {setResources([]); if (projectId) void perform(refreshResources);}, [projectId]);

  return <div className="shell">
    <aside><div className="brand">◈ <strong>CodeChat 管理平台</strong></div><div className="aside-caption">模型、套餐与平台资源</div><div className="nav active">平台管理</div><div className="aside-footer">内部预览</div></aside>
    <main><header><div><div className="eyebrow">管理员控制台</div><h1>管理模型与服务权益</h1><p>配置普通聊天与编程模型、充值折算规则及套餐。用户请从 LibreChat 使用个人空间。</p></div>{signedIn && <button className="subtle" onClick={() => void manager?.signoutRedirect()}>退出登录</button>}</header>
      {error && <div className="error" role="alert">{error}</div>}
      {!signedIn ? <section className="panel welcome"><span className="symbol">↗</span><h2>从统一登录开始</h2><p>{desktop ? '桌面进程连接已就绪。桌面登录回调尚未接入，请通过浏览器使用。' : '使用组织账号，进入你的项目空间。'}</p><button disabled={!ready || !manager || busy} onClick={() => void perform(async () => manager?.signinRedirect())}>使用 Authentik 登录</button></section> : <>
        {isAdmin && <AISettings/>}
        <div className="notice">此处保留资源申请与规划记录。普通用户的聊天、编程、文件和应用管理请使用 LibreChat。</div>
        <section className="toolbar"><label>当前空间<select value={tenantId} onChange={e => setTenantId(e.target.value)}>{tenants.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}</select></label><label>当前项目<select value={projectId} onChange={e => setProjectId(e.target.value)}><option value="">选择项目</option>{projects.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label></section>
        {!tenantId ? <section className="panel"><h2>创建你的个人空间</h2><p>项目、开发和部署资源都记入这个空间的额度。</p><form onSubmit={e => {e.preventDefault(); void perform(async () => {await api('/v1/tenants', 'POST', {name}); await loadTenants(); setName('');});}}><input aria-label="空间名称" required value={name} onChange={e => setName(e.target.value)} placeholder="空间名称" maxLength={120}/><button disabled={busy}>创建空间</button></form></section> : <>
          <section className="stats">{[['cpu_millicores', 'CPU 预留', 'm'], ['memory_mib', '内存预留', ' MiB'], ['storage_gib', '存储预留', ' GiB']].map(([key, title, unit]) => <article key={key}><span>{title}</span><strong>{quota?.reserved[key] ?? '—'}<small> / {quota?.limits[key] ?? '—'}{unit}</small></strong></article>)}</section>
          <section className="panel"><h2>新建项目</h2><form onSubmit={e => {e.preventDefault(); void perform(async () => {const p = await api<Project>(tenantPath + '/projects', 'POST', {name}); setProjects([...projects, p]); setProjectId(p.id); setName('');});}}><input aria-label="项目名称" required pattern="[a-z0-9]+(?:-[a-z0-9]+)*" value={name} onChange={e => setName(e.target.value)} placeholder="项目名称，例如 my-project" maxLength={63}/><button disabled={busy}>创建项目</button></form></section>
          {projectId && <><section className="panel"><div className="panel-heading"><h2>项目资源</h2><button className="subtle" disabled={busy} onClick={() => void perform(refreshResources)}>刷新</button></div>{resources.length ? <div className="resource-list">{resources.map(r => <div className="resource" key={r.id}><div><strong>{r.name}</strong><span>{labels[r.kind]}</span></div><span className={`badge ${r.status}`}>{labels[r.status] || r.status}</span>{r.status !== 'cancelled' && <button className="subtle" disabled={busy} onClick={() => void perform(async () => {await api(tenantPath + '/resources/' + r.id, 'DELETE'); await refreshResources();})}>取消申请</button>}</div>)}</div> : <p className="empty">项目已经准备好，可以添加第一个工作空间。</p>}</section>
          <section className="panel"><h2>申请新资源</h2><form className="resource-form" onSubmit={e => {e.preventDefault(); void perform(async () => {const body = {kind, name: resourceName, ...(kind === 'deployment' ? {image} : {}), ...(kind === 'agent' ? {workspace_id: workspace} : {})}; await api(`${tenantPath}/projects/${projectId}/resources`, 'POST', body); setResourceName(''); await refreshResources();});}}><select aria-label="资源类型" value={kind} onChange={e => setKind(e.target.value)}>{Object.entries(labels).filter(([key]) => ['workspace', 'agent', 'database', 'bucket', 'deployment'].includes(key)).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select><input required aria-label="资源名称" pattern="[a-z0-9]+(?:-[a-z0-9]+)*" maxLength={63} value={resourceName} onChange={e => setResourceName(e.target.value)} placeholder="资源名称"/>{kind === 'deployment' && <input required aria-label="已审核镜像" value={image} onChange={e => setImage(e.target.value)} placeholder="已审核的镜像地址（包含 sha256）"/>}{kind === 'agent' && <select required aria-label="关联工作空间" value={workspace} onChange={e => setWorkspace(e.target.value)}><option value="">选择工作空间</option>{resources.filter(r => r.kind === 'workspace' && r.status !== 'cancelled').map(r => <option key={r.id} value={r.id}>{r.name}</option>)}</select>}<button disabled={busy}>提交申请</button></form></section>
          {operations.length > 0 && <section className="panel"><h2>最近的申请</h2>{operations.slice(0, 5).map(o => <div className="operation" key={o.id}><span className="dot"/><span>{o.status === 'planned' ? '方案已生成，等待开通集成。' : o.status === 'cancelled' ? '申请已取消。' : '申请已接收，等待生成方案。'}</span></div>)}</section>}</>}
        </>}
      </>}
    </main>
  </div>;
}

createRoot(document.getElementById('root')!).render(<App/>);
