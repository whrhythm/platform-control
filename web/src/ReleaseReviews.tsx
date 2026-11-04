import { useEffect, useState } from 'react';
import { api } from './api';
import './release-reviews.css';

type Fields = Record<string, string | number | boolean | string[]>;
type Evidence = { id: string; checked_at: string; result: string; actual: Fields; differences: string[] };
type Review = {
  application_id: string; project: string; environment_id: string; desired_state: string;
  revision: string; image: string | null; error_code: string; review_id: string | null;
  created_at: string | null; prior_status: string | null; resume_pending: boolean;
  intent: { method: string; kind: string; namespace: string; name: string; expected: Fields } | null;
  evidence: Evidence | null;
};
type Audit = { id: string; target_id: string; subject: string; action: string; created_at: string };
const base = '/v1/admin/release-reviews';
const PAGE_SIZE = 50;
const label: Record<string, string> = {
  POST: '创建', PATCH: '更新', DELETE: '删除', running: '运行', stopped: '停止', deleted: '删除',
  confirmed: '已确认写入结果', unconfirmed: '证据不一致', unsupported: '需要线下核验',
  uid: '资源 UID', replicas: '副本数', revision: '版本', image: '镜像', exists: '资源存在', hosts: '域名',
  cpu_limit: '单应用 CPU 上限', memory_limit: '单应用内存上限',
  metadata_unavailable: '控制器无法读取这类资源的证据', object_missing: '未找到目标资源',
  object_still_exists: '目标资源仍存在', object_terminating: '目标资源正在删除',
  ownership_mismatch: '归属或 UID 不匹配', attempt_mismatch: '无法证明由这次写入创建',
  '/spec': '资源配置与预期不一致', '/spec/replicas': '副本数与预期不一致',
  '/spec/template': '运行模板与预期不一致',
  'application.review.requested': '提交恢复请求', 'application.review.resumed': '重新核验通过，已恢复',
  'application.review.blocked': '重新核验未通过，仍暂停',
};
const date = (value: string) => new Date(value).toLocaleString('zh-CN');

function Details({ values }: { values: Fields }) {
  return <dl className="review-fields">{Object.entries(values).map(([key, value]) =>
    <div key={key}><dt>{label[key] ?? key}</dt><dd>{typeof value === 'boolean' ? value ? '是' : '否' :
      Array.isArray(value) ? value.join('、') : String(value)}</dd></div>,
  )}</dl>;
}

export default function ReleaseReviews() {
  const [rows, setRows] = useState<Review[]>();
  const [audit, setAudit] = useState<Audit[]>([]);
  const [page, setPage] = useState(0);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [confirm, setConfirm] = useState<Review>();

  useEffect(() => {
    let active = true;
    let pending = false;
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const [items, events] = await Promise.all([
          api<Review[]>(`${base}?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`),
          api<Audit[]>(base + '/audit'),
        ]);
        if (active) { setRows(items); setAudit(events); setError(''); }
      } catch {
        if (active) setError('暂时无法读取待核验记录，请刷新或重新登录。');
      } finally { pending = false; }
    }
    void load();
    const timer = window.setInterval(() => { void load(); }, 5000);
    return () => { active = false; window.clearInterval(timer); };
  }, [page, revision]);

  async function resume(item: Review) {
    if (!item.review_id || !item.evidence) return;
    setSubmitting(true);
    setError('');
    try {
      await api(`${base}/${item.application_id}/resume`, 'POST',
        { review_id: item.review_id, evidence_id: item.evidence.id });
      setConfirm(undefined);
      setRevision(value => value + 1);
    } catch {
      setConfirm(undefined);
      setError('恢复请求未确认。请查看最新证据和审计后再操作。');
    } finally { setSubmitting(false); }
  }

  return <>
    <section className="panel" aria-label="待核验发布">
      <div className="panel-heading"><h2>待核验发布</h2><button className="subtle" onClick={() => setRevision(v => v + 1)}>刷新列表</button></div>
      <p>控制器持续读取集群证据。只有证据确认写入结果时才能提交恢复，执行前会再次核验。</p>
      {error && <p role="alert" className="error">{error}</p>}
      {!rows && !error && <p role="status">正在读取待核验记录…</p>}
      {rows?.length === 0 && <p className="empty">当前没有待核验发布。</p>}
      {rows?.map(item => <article className="review-card" key={item.application_id}>
        <div className="panel-heading"><h3>{item.project}</h3><span className="badge">
          {item.resume_pending ? '等待控制器复核' : item.evidence ? label[item.evidence.result] : '待核验'}
        </span></div>
        <p>应用 {item.application_id} · 空间 {item.environment_id}</p>
        <div className="review-evidence">
          <section><h4>原发布意图</h4>
            <Details values={{ revision: item.revision, ...(item.image ? { image: item.image } : {}) }}/>
            <p>目标状态 {label[item.desired_state] ?? item.desired_state}</p>
            {item.intent ? <><p>{label[item.intent.method]} {item.intent.kind} · {item.intent.namespace}/{item.intent.name}</p>
              <Details values={item.intent.expected}/></> : <p>这条记录缺少写入凭证，需要人工核验。</p>}
          </section>
          <section><h4>集群实际状态</h4>
            {item.evidence ? <><p>检查时间 {date(item.evidence.checked_at)}</p><Details values={item.evidence.actual}/></> :
              <p>{item.intent ? '等待控制器读取集群证据。' : '暂无可比对的证据。'}</p>}
          </section>
          <section><h4>差异</h4>
            {item.evidence ? item.evidence.differences.length > 0 ?
              <ul>{item.evidence.differences.map(d => <li key={d}>{label[d] ?? d}</li>)}</ul> :
              <p>写入结果与预期一致。</p> : <p>{item.error_code}</p>}
          </section>
        </div>
        {item.evidence?.result === 'confirmed' && !item.resume_pending &&
          <button disabled={submitting} onClick={() => setConfirm(item)}>核验并恢复</button>}
      </article>)}
      {(page > 0 || rows?.length === PAGE_SIZE) && <div className="review-pagination">
        <button className="subtle" disabled={page === 0} onClick={() => setPage(v => v - 1)}>上一页</button>
        <span>第 {page + 1} 页</span>
        <button className="subtle" disabled={rows?.length !== PAGE_SIZE} onClick={() => setPage(v => v + 1)}>下一页</button>
      </div>}
      {confirm && <section className="notice review-confirm" aria-label="确认恢复任务">
        <p>提交「{confirm.project}」的恢复请求？控制器会再次核对当前资源，复核通过后继续原任务。</p>
        <button disabled={submitting} onClick={() => void resume(confirm)}>确认提交</button>
        <button className="subtle" disabled={submitting} onClick={() => setConfirm(undefined)}>取消</button>
      </section>}
    </section>
    <section className="panel" aria-label="核验审计">
      <h2>核验审计</h2>
      {audit.length === 0 ? <p>暂无操作记录。</p> : <ul className="review-audit">{audit.map(event => <li key={event.id}>
        <strong>{label[event.action] ?? event.action}</strong>
        <span>{date(event.created_at)} · 操作者 {event.subject} · 应用 {event.target_id}</span>
      </li>)}</ul>}
    </section>
  </>;
}
