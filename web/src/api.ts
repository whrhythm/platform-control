import { InMemoryWebStorage, UserManager, WebStorageStateStore } from 'oidc-client-ts';

export interface IdentityConfig {issuer: string; client_id: string; scope: string; provisioning_mode: string}
export interface Tenant {id: string; name: string; project_limit: number}
export interface Project {id: string; name: string}
export interface Resource {id: string; name: string; kind: string; status: string; spec: Record<string, unknown>}
export interface Quota {limits: Record<string, number>; reserved: Record<string, number>}
export interface Operation {id: string; status: string; plan: {blockers: string[]} | null}

let bearer = '';
let base = '';
let desktopToken = '';
export let desktop = false;

export async function initTransport() {
  desktop = '__TAURI_INTERNALS__' in window;
  if (desktop) {
    const { invoke } = await import('@tauri-apps/api/core');
    const { listen } = await import('@tauri-apps/api/event');
    const load = async () => {const bootstrap = await invoke<{url: string; token: string}>('sidecar_info'); base = bootstrap.url; desktopToken = bootstrap.token;};
    await listen('sidecar-stopped', () => {base = ''; desktopToken = '';});
    await listen('sidecar-ready', () => {void load();});
    for (let attempt = 0; ; attempt++) {
      try {await load(); break;} catch (error) {
        if (attempt >= 149) throw error;
        await new Promise(resolve => setTimeout(resolve, 200));
      }
    }
  }
}

export function setBearer(token: string) {bearer = token;}

export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  if (desktop && !base) throw new Error('本地连接正在恢复，请稍后重试。');
  const response = await fetch(base + path, {
    method,
    headers: {...(bearer ? {Authorization: `Bearer ${bearer}`} : {}), ...(desktopToken ? {'X-Desktop-Token': desktopToken} : {}), ...(body ? {'Content-Type': 'application/json', 'Idempotency-Key': crypto.randomUUID()} : {})},
    body: body ? JSON.stringify(body) : undefined,
    credentials: 'omit',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const friendly: Record<number, string> = {401: '登录已过期，请重新登录。', 403: '你没有执行此操作的权限。', 404: '没有找到可访问的记录。', 409: '申请与现有记录或额度冲突。', 422: '请检查填写的内容。', 503: '登录服务尚未配置完成。'};
    throw new Error(`${friendly[response.status] || '请求失败。'}${typeof error.detail === 'string' ? ` ${error.detail}` : ''}`);
  }
  return response.status === 204 ? undefined as T : response.json();
}

export function identity(config: IdentityConfig) {
  return new UserManager({
    authority: config.issuer, client_id: config.client_id,
    redirect_uri: window.location.origin + '/', post_logout_redirect_uri: window.location.origin + '/',
    response_type: 'code', scope: config.scope,
    userStore: new WebStorageStateStore({store: new InMemoryWebStorage()}),
    stateStore: new WebStorageStateStore({store: window.sessionStorage}),
    automaticSilentRenew: false, loadUserInfo: false,
  });
}
