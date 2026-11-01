// Real browser acceptance with disposable users. Never print access tokens or passwords.
const { chromium } = require('../release/browser-qa/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.join(__dirname, '..');
const credentials = JSON.parse(fs.readFileSync(path.join(root, 'release/live-qa-credentials.json')));
const resultsPath = path.join(root, 'release/live-qa-results.json');
const result = { run: credentials.run, tenants: [], checks: [] };
const save = () => fs.writeFileSync(resultsPath, JSON.stringify(result, null, 2));
save();
let stage = 'start';

async function login(browser, user) {
  const context = await browser.newContext({ignoreHTTPSErrors: true, locale: 'en-US', viewport: {width: 1400, height: 1000}});
  if (process.env.CODECHAT_PREVIEW_HEADER) {
    await context.setExtraHTTPHeaders({'X-CodeChat-Preview': process.env.CODECHAT_PREVIEW_HEADER});
  }
  const page = await context.newPage();
  let authorization = '';
  page.on('request', request => {
    if (request.url().startsWith('https://platform.k3s.local:30443/v1/')) {
      authorization = request.headers().authorization || authorization;
    }
  });
  stage = 'login-page';
  await page.goto('https://chat.k3s.local:30443/login', {waitUntil: 'domcontentloaded', timeout: 60000});
  await page.getByRole('link', {name: 'Authentik'}).click();
  stage = 'identity-username';
  await page.waitForTimeout(5000);
  console.log('Identity page:', new URL(page.url()).pathname, (await page.locator('body').innerText()).slice(0, 240));
  await page.getByRole('textbox', {name: /Email or Username|邮箱或用户名/}).fill(user.username, {timeout: 90000});
  await page.getByRole('button', {name: 'Log in', exact: true}).click();
  stage = 'identity-password';
  const password = page.getByPlaceholder('Please enter your password');
  await password.waitFor({timeout: 45000});
  await password.fill(user.password);
  const accepted = page.waitForResponse(response => new URL(response.url()).pathname === '/api/user' && response.status() === 200, {timeout: 60000});
  await password.press('Enter');
  stage = 'identity-redirect';
  const consent = page.getByRole('button', {name: /Continue|Authorize|Allow/i});
  await Promise.race([
    page.waitForURL(url => url.hostname === 'chat.k3s.local', {timeout: 60000}),
    consent.waitFor({timeout: 60000}).then(() => consent.click()),
  ]);
  stage = 'librechat-session';
  await accepted;
  stage = 'workspace-navigation';
  await page.getByTestId('nav-panel-codechat-workspace').first().click({timeout: 60000});
  stage = 'workspace-connect';
  await page.getByRole('heading', {name: 'Development projects'}).waitFor({timeout: 60000});
  await page.getByRole('textbox', {name: 'Project name'}).waitFor({timeout: 60000});
  assert.ok(authorization, 'No authenticated platform request');
  const me = await page.evaluate(async bearer => {
    const response = await fetch('https://platform.k3s.local:30443/v1/me', {headers: {Authorization: bearer}, credentials: 'omit'});
    if (!response.ok) return null;
    return response.json();
  }, authorization);
  assert.ok(me && me.issuer && me.subject);
  const tenants = await page.evaluate(async bearer => {
    const response = await fetch('https://platform.k3s.local:30443/v1/tenants', {headers: {Authorization: bearer}, credentials: 'omit'});
    if (!response.ok) return [];
    return response.json();
  }, authorization);
  assert.equal(tenants.length, 1);
  result.tenants.push({id: tenants[0].id, issuer: me.issuer, subject: me.subject});
  save();
  return {context, page, authorization, tenant: tenants[0].id};
}

(async () => {
  const browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--no-proxy-server', '--host-resolver-rules=MAP chat.k3s.local 192.168.10.30,MAP authentik.k3s.local 192.168.10.30,MAP platform.k3s.local 192.168.10.30']});
  try {
    stage = 'first-user-login-and-workspace';
    const a = await login(browser, credentials.users[0]);
    stage = 'project-creation';
    const name = `qa-${credentials.run}`;
    await a.page.getByRole('textbox', {name: 'Project name'}).fill(name);
    await a.page.getByRole('button', {name: 'Create project'}).click();
    await a.page.getByText(name, {exact: true}).waitFor({timeout: 30000});
    await a.page.getByText('Draft · development environment not started').waitFor();
    stage = 'second-user-login-and-isolation';
    const b = await login(browser, credentials.users[1]);
    assert.notEqual(a.tenant, b.tenant);
    const forbidden = await b.page.evaluate(async ({tenant, bearer}) => {
      const response = await fetch(`https://platform.k3s.local:30443/v1/tenants/${tenant}/projects`, {headers: {Authorization: bearer}, credentials: 'omit'});
      return response.status;
    }, {tenant: a.tenant, bearer: b.authorization});
    assert.equal(forbidden, 404);
    assert.equal(await b.page.getByText(name, {exact: true}).count(), 0);
    result.checks = ['real_sso', 'personal_tenant', 'project_create', 'cross_tenant_rejected'];
    result.success = true;
    save();
    console.log('Workspace acceptance passed:', result.checks.join(', '));
  } finally { await browser.close(); }
})().catch(() => {save(); console.error('Workspace acceptance failed at ' + stage); process.exitCode = 1;});
