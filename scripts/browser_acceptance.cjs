const {chromium} = require('../release/browser-qa/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.join(__dirname, '..');
const credentials = JSON.parse(fs.readFileSync(path.join(root, 'release/live-qa-credentials.json')));
let browser;
const privateValues = credentials.users.map(user => user.password);
const redact = value => privateValues.reduce((text, secret) => text.split(secret).join('[redacted]'), String(value));
const result = {run: credentials.run, tenants: [], checks: []};

async function login(user) {
  const context = await browser.newContext({ignoreHTTPSErrors: true, locale: 'en-US', viewport: {width: 1400, height: 1000}});
  const page = await context.newPage();
  let bearer = '';
  page.on('request', request => {
    if (request.url().startsWith('https://platform.k3s.local:30443/v1/')) {
      const value = request.headers().authorization;
      if (value) {bearer = value; if (!privateValues.includes(value)) privateValues.push(value);}
    }
  });
  page.on('pageerror', error => console.log('Browser script error: ' + redact(error.message)));
  await page.goto('https://platform.k3s.local:30443/', {waitUntil: 'networkidle', timeout: 60000});
  assert.equal(await page.evaluate(async () => (await fetch('/v1/me')).status), 401);
  await page.getByRole('button', {name: '使用 Authentik 登录'}).click();
  await page.getByRole('textbox', {name: 'Email or Username'}).waitFor({timeout: 60000});
  console.log('Authentik username stage ready');
  await page.getByRole('textbox', {name: 'Email or Username'}).fill(user.username);
  await page.getByRole('button', {name: 'Log in', exact: true}).click();
  const password = page.getByPlaceholder('Please enter your password');
  await password.waitFor({timeout: 45000});
  await password.fill(user.password);
  assert.equal((await password.inputValue()).length, user.password.length);
  console.log('Authentik password stage ready');
  await password.press('Enter');
  try {
    await page.waitForURL(url => url.hostname === 'platform.k3s.local' || url.pathname.includes('default-provider-authorization'), {timeout: 45000});
  } catch (error) {
    await page.screenshot({path: path.join(root, 'release/login-diagnostic.png'), fullPage: true});
    const snapshot = (await page.locator('body').ariaSnapshot()).split('\n').filter(line => !line.includes('/url:')).join('\n');
    console.log('Login diagnostic: ' + redact(snapshot.slice(-1800)));
    throw error;
  }
  if (new URL(page.url()).hostname !== 'platform.k3s.local') {
    console.log('Authentik consent stage ready');
    await page.getByRole('button', {name: /Continue|Authorize|Allow/i}).click();
  }
  await page.waitForURL(url => url.hostname === 'platform.k3s.local', {timeout: 45000});
  await page.getByRole('heading', {name: '创建你的个人空间'}).waitFor({timeout: 45000});
  assert.ok(bearer);
  const api = (endpoint, method = 'GET', body, key) => page.evaluate(async ({endpoint, method, body, bearer, key}) => {
    const response = await fetch(endpoint, {method, headers: {Authorization: bearer, ...(body ? {'Content-Type': 'application/json', 'Idempotency-Key': key || crypto.randomUUID()} : {})}, body: body ? JSON.stringify(body) : undefined});
    const data = response.status === 204 ? null : await response.json();
    return {status: response.status, data};
  }, {endpoint, method, body, bearer, key});
  const me = await api('/v1/me');
  assert.equal(me.status, 200);
  assert.equal(me.data.platform_admin, user.admin);
  result.checks.push(user.admin ? 'real_pkce_admin_login' : 'real_pkce_member_login');
  console.log('Real PKCE callback and API access accepted');
  return {page, context, api, me: me.data};
}

(async () => {
  browser = await chromium.launch({executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true, args: ['--no-proxy-server', '--host-resolver-rules=MAP platform.k3s.local 127.0.0.1,MAP authentik.k3s.local 127.0.0.1']});
  const a = await login(credentials.users[0]);
  await a.page.getByRole('textbox', {name: '空间名称'}).fill('QA ' + credentials.run);
  await a.page.getByRole('button', {name: '创建空间', exact: true}).click();
  await a.page.getByRole('heading', {name: '新建项目', exact: true}).waitFor();
  const tenantA = await a.page.getByLabel('当前空间').inputValue();
  result.tenants.push({id: tenantA, issuer: a.me.issuer, subject: a.me.subject});
  fs.writeFileSync(path.join(root, 'release/live-qa-results.json'), JSON.stringify(result, null, 2));
  await a.page.getByRole('textbox', {name: '项目名称', exact: true}).fill('qa-' + credentials.run);
  await a.page.getByRole('button', {name: '创建项目', exact: true}).click();
  await a.page.getByRole('heading', {name: '项目资源', exact: true}).waitFor();
  const projectA = await a.page.getByLabel('当前项目').inputValue();
  const base = `/v1/tenants/${tenantA}`;
  const body = {kind: 'workspace', name: 'qa-dev'};
  const key = 'live-qa-' + credentials.run;
  const first = await a.api(`${base}/projects/${projectA}/resources`, 'POST', body, key);
  assert.equal(first.status, 201);
  const replay = await a.api(`${base}/projects/${projectA}/resources`, 'POST', body, key);
  assert.equal(replay.data.id, first.data.id);
  let operation;
  for (let attempt = 0; attempt < 20; attempt++) {
    operation = (await a.api(base + '/operations')).data.find(item => item.resource_id === first.data.id);
    if (operation.status === 'planned') break;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  assert.equal(operation.status, 'planned');
  assert.equal(operation.plan.executable, false);
  const quota = await a.api(base + '/quota');
  assert.equal(quota.data.reserved.cpu_millicores, 500);
  assert.equal((await a.api('/v1/admin/tenants')).status, 200);
  result.checks.push('ui_tenant_and_project_creation', 'idempotency', 'real_worker_plan', 'quota_reservation');
  await a.page.getByRole('button', {name: '刷新', exact: true}).click();
  await a.page.screenshot({path: path.join(root, 'release/platform-console.png'), fullPage: true});
  const b = await login(credentials.users[1]);
  assert.equal((await b.api('/v1/admin/tenants')).status, 403);
  assert.equal((await b.api(base + '/projects')).status, 404);
  assert.equal((await b.api(`${base}/projects/${projectA}/resources`, 'POST', {kind: 'workspace', name: 'forbidden'})).status, 404);
  result.checks.push('non_admin_rejected', 'cross_tenant_read_rejected', 'cross_tenant_write_rejected');
  assert.equal((await a.api(base + '/resources/' + first.data.id, 'DELETE')).status, 200);
  assert.equal((await a.api(base + '/quota')).data.reserved.cpu_millicores, 0);
  assert.ok((await a.api(base + '/audit')).data.length > 0);
  result.checks.push('quota_release', 'audit_read');
  result.project = projectA;
  result.success = true;
  fs.writeFileSync(path.join(root, 'release/live-qa-results.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify({success: true, checks: result.checks}));
})().catch(error => {
  console.error(redact(error.message));
  fs.writeFileSync(path.join(root, 'release/live-qa-results.json'), JSON.stringify(result, null, 2));
  process.exitCode = 1;
}).finally(async () => {if (browser) await browser.close();});
