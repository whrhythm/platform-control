import { useEffect, useState } from 'react';
import { api } from './api';

type State = 'pending' | 'approved' | 'rejected' | 'revoked';
type Review = {
  id: string;
  project_id: string;
  reference: string;
  title: string;
  name: string;
  description: string;
  target: string;
  parameters: string[];
  state: State;
  revision: number;
  generated?: {
    source_digest: string;
    program: { expression: unknown };
    cases: Array<{ inputs: Record<string, number>; result: number }>;
  };
};
const status: Record<State, string> = {
  pending: '待审核', approved: '已通过', rejected: '未通过', revoked: '已撤销',
};
const path = '/v1/admin/assistant-tool-reviews';

export default function AssistantToolReviews() {
  const [rows, setRows] = useState<Review[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [ack, setAck] = useState<string | null>(null);

  async function load() {
    setError('');
    setBusy(true);
    try {
      setRows((await api<{ reviews: Review[] }>(path)).reviews);
    } catch (e) {
      setError(e instanceof Error ? e.message : '审核列表无法加载。');
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => { void load(); }, []);

  async function decide(row: Review, state: Exclude<State, 'pending'>) {
    setError('');
    setBusy(true);
    try {
      const updated = await api<Review>(path + '/' + row.id, 'POST', {
        revision: row.revision, state, acknowledged: true,
      });
      setRows((current) => current.map((item) => item.id === updated.id ? updated : item));
      setAck(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : '审核未保存。');
    } finally {
      setBusy(false);
    }
  }

  return <section className="panel" aria-label="智能体工具审核">
    <div className="panel-heading">
      <h2>智能体工具审核</h2>
      <button className="subtle" disabled={busy} onClick={() => void load()}>刷新审核</button>
    </div>
    {error && <p role="alert" className="error">{error}</p>}
    {!rows.length && !busy && !error && <p>暂无审核申请。</p>}
    {rows.map((row) => <article key={row.id} aria-label={row.title}>
      <h3>{row.title} · {status[row.state]}</h3>
      <p>{row.description}</p>
      <dl>
        <dt>项目</dt><dd>{row.project_id}</dd>
        <dt>执行范围</dt><dd><code>{row.generated ? row.target : 'GET '+row.target}</code></dd>
        <dt>参数</dt><dd>{row.parameters.join('、') || '无'}</dd>
        <dt>工具版本</dt><dd><code>{row.reference}</code></dd>
      </dl>
      {row.generated && <details>
        <summary>规则与测试用例（{row.generated.cases.length} 项通过）</summary>
        <p>源码摘要 <code>{row.generated.source_digest}</code></p>
        <pre>{JSON.stringify({ expression: row.generated.program.expression, cases: row.generated.cases }, null, 2)}</pre>
      </details>}
      {row.state === 'pending' && <>
        <label><input type="checkbox" checked={ack === row.id}
          onChange={(e) => setAck(e.target.checked ? row.id : null)} /> 已核对查询用途、数据范围和副作用</label>
        <button disabled={busy || ack !== row.id} onClick={() => void decide(row, 'approved')}>通过审核</button>
        <button className="subtle" disabled={busy || ack !== row.id} onClick={() => void decide(row, 'rejected')}>拒绝</button>
      </>}
      {row.state === 'approved' && <button className="subtle" disabled={busy} onClick={() => {
        if (window.confirm('撤销后，引用这个工具版本的正式助手将停止提供服务。')) void decide(row, 'revoked');
      }}>撤销审核</button>}
    </article>)}
  </section>;
}
