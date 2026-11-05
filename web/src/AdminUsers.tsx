import { useEffect, useState } from 'react';
import { api } from './api';
import './admin-users.css';
import Modal from './Modal';
import Accounts from './Accounts';

type Sample = { component: string; used_bytes: number | null; budget_bytes: number | null;
  status: string; observed_at: string | null };
type Wallet = { status: string; observed_at: string | null; allocated_usd_micros: number | null;
  spent_usd_micros: number | null; remaining_usd_micros: number | null; total_tokens: number | null };
type User = {
  environment_id: string; issuer: string; subject: string; workspace_name: string; workspace_status: string;
  file_service_status: string | null; namespace: string | null; created_at: string; plan_id: string | null;
  plan_name: string | null; application_limit: number; application_count: number; files: Sample; model_usage: Wallet;
};
type Detail = {
  user: User; storage: Sample[];
  applications: { id: string; project: string; status: string; desired_state: string; created_at: string }[];
  next_application_offset: number | null;
};
type Page = { items: User[]; next_offset: number | null };

const endpoint = '/v1/admin/users';
const pageSize = 25;
const labels: Record<string, string> = {
  requested: '已申请', provisioning: '准备中', preparing: '准备中', ready: '已就绪', suspended: '已暂停',
  deleted: '已删除', requires_review: '待核验', running: '运行中', stopped: '已停止',
  failed: '失败', changing: '变更中', queued: '排队中', building: '构建中', deploying: '部署中',
  ok: '已采集', stale: '采样已过期', unknown: '暂无数据', not_metered: '未计量',
  files: '个人文件与缓存', database: '数据库', objects: '对象存储', images: '镜像', app_volumes: '应用卷',
};
const label = (value: string | null) => value ? labels[value] ?? value : '暂无记录';
const date = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN') : '—';
const count = (value: number | null) => value === null ? '—' : value.toLocaleString('zh-CN');
const usd = (value: number | null) => value === null ? '—' : new Intl.NumberFormat('zh-CN',
  { style: 'currency', currency: 'USD', maximumFractionDigits: 6 }).format(value / 1_000_000);
const plan = (user: User) => user.plan_id === 'existing' ? '默认权益' : user.plan_name ?? '默认权益';
const workspace = (user: User) => label(['suspended', 'deleted'].includes(user.workspace_status)
  ? user.workspace_status : user.file_service_status ?? user.workspace_status);

function bytes(value: number | null) {
  if (value === null) return '—';
  for (const [unit, size] of [['GiB', 2 ** 30], ['MiB', 2 ** 20], ['KiB', 2 ** 10]] as const) {
    if (value >= size) return `${(value / size).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} ${unit}`;
  }
  return `${value} B`;
}

export default function AdminUsers() {
  const [input, setInput] = useState('');
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [reload, setReload] = useState(0);
  const [page, setPage] = useState<Page>();
  const [listError, setListError] = useState('');
  const [selected, setSelected] = useState<string>();
  const [appOffset, setAppOffset] = useState(0);
  const [detail, setDetail] = useState<Detail>();
  const [detailError, setDetailError] = useState('');

  useEffect(() => {
    let active = true;
    setPage(undefined);
    setListError('');
    const params = new URLSearchParams({ q: query, limit: String(pageSize), offset: String(offset) });
    void api<Page>(`${endpoint}?${params}`).then(value => {
      if (active) setPage(value);
    }).catch(() => { if (active) setListError('无法读取用户列表，请刷新重试。'); });
    return () => { active = false; };
  }, [query, offset, reload]);

  useEffect(() => {
    let active = true;
    setDetail(undefined);
    setDetailError('');
    if (selected) {
      void api<Detail>(`${endpoint}/${encodeURIComponent(selected)}?application_offset=${appOffset}`).then(value => {
        if (active) setDetail(value);
      }).catch(() => { if (active) setDetailError('无法读取用户资源，请刷新重试。'); });
    }
    return () => { active = false; };
  }, [selected, appOffset, reload]);

  function changePage(next: number) {
    setOffset(next);
    setSelected(undefined);
    setAppOffset(0);
  }

  return <>
    <Accounts />
    <section className="panel" aria-label="平台用户">
      <div className="panel-heading"><h2>平台用户</h2>
        <button className="subtle" onClick={() => setReload(v => v + 1)}>刷新数据</button>
      </div>
      <p>此处列出已建立平台工作空间记录的用户。使用 OIDC 用户标识区分账号。</p>
      <form className="inventory-search" onSubmit={event => {
        event.preventDefault(); setQuery(input.trim()); changePage(0); setReload(v => v + 1);
      }}>
        <input aria-label="搜索用户" placeholder="用户标识、空间名称、空间 ID 或命名空间" value={input}
          maxLength={255} onChange={event => setInput(event.target.value)}/>
        <button type="submit">搜索</button>
        {query && <button type="button" className="subtle" onClick={() => {
          setInput(''); setQuery(''); changePage(0);
        }}>清除搜索</button>}
      </form>
      {listError ? <p role="alert" className="error">{listError}</p> :
        !page ? <p role="status">正在读取用户列表…</p> : page.items.length === 0 ?
          <p className="empty">没有符合条件的平台用户。</p> : <div className="inventory-scroll">
            <table className="inventory-table">
              <thead><tr><th>用户与空间</th><th>状态</th><th>应用</th><th>个人盘用量</th><th>模型余额</th><th>套餐</th><th>详情</th></tr></thead>
              <tbody>{page.items.map(user => <tr key={user.environment_id}>
                <td><strong>{user.workspace_name}</strong><span className="inventory-meta">{user.subject}</span></td>
                <td>{workspace(user)}</td>
                <td>{count(user.application_count)} / {count(user.application_limit)}</td>
                <td>{bytes(user.files.used_bytes)} / {bytes(user.files.budget_bytes)}
                  <span className="inventory-meta">{label(user.files.status)}</span></td>
                <td>{usd(user.model_usage.remaining_usd_micros)}<span className="inventory-meta">{label(user.model_usage.status)}</span></td>
                <td>{plan(user)}</td>
                <td><button className="subtle" aria-expanded={selected === user.environment_id}
                  aria-controls="inventory-user-detail" onClick={() => {
                    setSelected(selected === user.environment_id ? undefined : user.environment_id); setAppOffset(0);
                  }}>{selected === user.environment_id ? '收起' : '查看详情'}</button></td>
              </tr>)}</tbody>
            </table>
          </div>}
      {page && (offset > 0 || page.next_offset !== null) && <div className="inventory-pagination">
        <button className="subtle" disabled={offset === 0} onClick={() => changePage(offset - pageSize)}>上一页</button>
        <span>第 {Math.floor(offset / pageSize) + 1} 页</span>
        <button className="subtle" disabled={page.next_offset === null}
          onClick={() => { if (page.next_offset !== null) changePage(page.next_offset); }}>下一页</button>
      </div>}
    </section>

    {selected && <Modal title="用户资源详情" onClose={()=>setSelected(undefined)}><section id="inventory-user-detail" aria-label="用户资源详情">
      {detailError ? <p className="error" role="alert">{detailError}</p> :
        !detail || detail.user.environment_id !== selected ? <p role="status">正在读取用户资源…</p> : <>
          <dl className="inventory-identity">
            <div><dt>OIDC 用户标识</dt><dd>{detail.user.subject}</dd></div>
            <div><dt>身份签发方</dt><dd>{detail.user.issuer}</dd></div>
            <div><dt>空间 ID</dt><dd>{detail.user.environment_id}</dd></div>
            <div><dt>已记录的命名空间</dt><dd>{detail.user.namespace ?? '暂无绑定记录'}</dd></div>
            <div><dt>空间状态</dt><dd>{workspace(detail.user)}</dd></div>
            <div><dt>套餐</dt><dd>{plan(detail.user)}</dd></div>
          </dl>
          <h3>存储明细</h3>
          <div className="inventory-scroll"><table className="inventory-table">
            <thead><tr><th>类型</th><th>最近用量</th><th>已记录额度</th><th>采样状态</th><th>最近采集</th></tr></thead>
            <tbody>{detail.storage.map(sample => <tr key={sample.component}><td>{label(sample.component)}</td>
              <td>{bytes(sample.used_bytes)}</td><td>{bytes(sample.budget_bytes)}</td>
              <td>{label(sample.status)}</td><td>{date(sample.observed_at)}</td></tr>)}</tbody>
          </table></div>
          <h3>模型用量</h3>
          <dl className="inventory-identity">
            <div><dt>已分配（美元）</dt><dd>{usd(detail.user.model_usage.allocated_usd_micros)}</dd></div>
            <div><dt>已消费（美元）</dt><dd>{usd(detail.user.model_usage.spent_usd_micros)}</dd></div>
            <div><dt>剩余（美元）</dt><dd>{usd(detail.user.model_usage.remaining_usd_micros)}</dd></div>
            <div><dt>已记录 Token</dt><dd>{count(detail.user.model_usage.total_tokens)}</dd></div>
            <div><dt>最近采集</dt><dd>{date(detail.user.model_usage.observed_at)} · {label(detail.user.model_usage.status)}</dd></div>
          </dl>
          <h3>应用（{count(detail.user.application_count)} / {count(detail.user.application_limit)}）</h3>
          {detail.applications.length ? <div className="inventory-scroll"><table className="inventory-table">
            <thead><tr><th>项目</th><th>平台记录状态</th><th>创建时间</th></tr></thead>
            <tbody>{detail.applications.map(app => <tr key={app.id}><td>{app.project}</td>
              <td>{label(app.status)}</td><td>{date(app.created_at)}</td></tr>)}</tbody>
          </table></div> : <p>当前页没有应用。</p>}
          {(appOffset > 0 || detail.next_application_offset !== null) && <div className="inventory-pagination">
            <button className="subtle" disabled={appOffset === 0} onClick={() => setAppOffset(appOffset - pageSize)}>上一页应用</button>
            <button className="subtle" disabled={detail.next_application_offset === null}
              onClick={() => { if (detail.next_application_offset !== null) setAppOffset(detail.next_application_offset); }}>下一页应用</button>
          </div>}
        </>}
    </section></Modal>}
  </>;
}
