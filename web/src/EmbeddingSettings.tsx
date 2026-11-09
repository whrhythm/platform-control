import { useEffect, useState } from 'react';
import { api } from './api';

type Profile = { id: string; model: string; url: string | null };
type Configuration = { profiles: Profile[]; selected: string; version: number };
const path = '/v1/admin/knowledge-embedding';

export default function EmbeddingSettings() {
  const [configuration, setConfiguration] = useState<Configuration>();
  const [selected, setSelected] = useState('local');
  const [url, setUrl] = useState('');
  const [model, setModel] = useState('');
  const [secret, setSecret] = useState('');
  const [requestDimensions, setRequestDimensions] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  function receive(value: Configuration) {
    setConfiguration(value);
    setSelected(value.selected);
  }

  async function perform(action: () => Promise<Configuration>, failure: string, success = '') {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      receive(await action());
      setNotice(success);
    } catch {
      setError(failure);
    } finally {
      setBusy(false);
      setSecret('');
    }
  }

  useEffect(() => {
    void perform(() => api<Configuration>(path), '无法读取模型配置，请刷新重试。');
  }, []);

  return (
    <section className="panel" aria-label="知识库 Embedding 配置">
      <div className="panel-heading">
        <div>
          <h2>推荐 Embedding 模型</h2>
          <p>库所有者可以在知识库设置中采用推荐模型并重建索引。已发布的智能体继续使用发布时的知识修订。</p>
        </div>
        <button className="subtle" disabled={busy} onClick={() =>
          void perform(() => api<Configuration>(path), '无法读取模型配置，请刷新重试。')
        }>刷新</button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {configuration && <>
        <form onSubmit={event => {
          event.preventDefault();
          void perform(() => api<Configuration>(path, 'PUT', {
            profile: selected === 'local' ? null : selected,
            version: configuration.version,
          }), '无法保存推荐模型，请刷新配置后重试。', '推荐模型已保存。');
        }}>
          <label>知识库推荐模型
            <select value={selected} disabled={busy} onChange={event => setSelected(event.target.value)}>
              {configuration.profiles.map(profile => <option key={profile.id} value={profile.id}>
                {profile.url ? `外部 · ${profile.model} · ${new URL(profile.url).hostname} · ${profile.id.slice(0, 8)}`
                  : `本地 · ${profile.model}`}
              </option>)}
            </select>
          </label>
          <button disabled={busy || selected === configuration.selected}>保存推荐模型</button>
        </form>
        <form onSubmit={event => {
          event.preventDefault();
          void perform(() => api<Configuration>(path + '/profiles', 'POST', {
            url, model, secret: secret || null, request_dimensions: requestDimensions,
          }), '连接检查失败，请确认公网 HTTPS 地址、密钥和 512 维输出。',
          '连接检查通过。可以在上方选择该模型。');
        }}>
          <h3>添加外部 Embedding 模型</h3>
          <label>完整 Embeddings 地址
            <input required type="url" maxLength={500} placeholder="https://example.com/v1/embeddings"
              value={url} onChange={event => setUrl(event.target.value)} />
          </label>
          <label>模型标识
            <input required maxLength={200} value={model} onChange={event => setModel(event.target.value)} />
          </label>
          <label>API 密钥（服务需要时填写）
            <input type="password" autoComplete="new-password" maxLength={4096}
              value={secret} onChange={event => setSecret(event.target.value)} />
          </label>
          <label className="mcp-ack">
            <input type="checkbox" checked={requestDimensions}
              onChange={event => setRequestDimensions(event.target.checked)} />
            请求中指定 512 维（服务支持 dimensions 参数时勾选）
          </label>
          <p>连接检查只发送合成文本。请使用固定模型版本；添加新配置会保留旧索引依赖的模型及凭据。</p>
          <button disabled={busy}>检查连接并保存</button>
        </form>
      </>}
    </section>
  );
}
