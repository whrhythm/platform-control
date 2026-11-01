// Exercise real enrollment with a uniquely named disposable account.
const { chromium } = require('../release/browser-qa/node_modules/playwright');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const credentialPath = path.join(__dirname, '../release/live-qa-credentials.json');
if (fs.existsSync(credentialPath)) throw new Error('Existing QA credentials; refusing to overwrite');
const run = crypto.randomBytes(6).toString('hex');
const users = ['a', 'b'].map(suffix => ({username: `platform-qa-${run}-${suffix}`, password: crypto.randomBytes(24).toString('base64url'), admin: false}));
fs.writeFileSync(credentialPath, JSON.stringify({run, users}), {mode: 0o600, flag: 'wx'});
const resultsPath = path.join(__dirname, '../release/enrollment-results.json');
const result = {run, identities: []};
fs.writeFileSync(resultsPath, JSON.stringify(result));
let user = users[0];

(async () => {
  const browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--no-proxy-server', '--host-resolver-rules=MAP chat.k3s.local 192.168.10.30,MAP authentik.k3s.local 192.168.10.30,MAP platform.k3s.local 192.168.10.30']});
  try {
    for (user of users) {
    const context = await browser.newContext({ignoreHTTPSErrors: true, locale: 'en-US'});
    if (process.env.CODECHAT_PREVIEW_HEADER) await context.setExtraHTTPHeaders({'X-CodeChat-Preview': process.env.CODECHAT_PREVIEW_HEADER});
    const page = await context.newPage();
    let bearer = '';
    page.on('request', request => { if (request.url().includes('/api/user') && request.headers().authorization) bearer = request.headers().authorization; });
    await page.goto('https://chat.k3s.local:30443/login', {waitUntil: 'domcontentloaded', timeout: 60000});
    await page.getByRole('link', {name: 'Authentik'}).click();
    await page.getByRole('link', {name: /Sign up|注册/}).click();
    await page.getByRole('textbox', {name: '用户名'}).fill(user.username);
    await page.getByRole('textbox', {name: '密码', exact: true}).fill(user.password);
    await page.getByRole('textbox', {name: '再次输入密码'}).fill(user.password);
    await page.getByRole('button', {name: /Continue|继续/}).click();
    await page.getByRole('textbox', {name: '显示名称'}).fill('CodeChat QA');
    await page.getByRole('textbox', {name: '邮箱（未验证）'}).fill(`${user.username}@example.test`);
    const authenticated = page.waitForResponse(response => new URL(response.url()).pathname === '/api/user' && response.status() === 200, {timeout: 60000});
    await page.getByRole('button', {name: /Continue|继续/}).click();
    const consent = page.getByRole('button', {name: /Continue|Authorize|Allow/i});
    await Promise.race([
      page.waitForURL(url => url.hostname === 'chat.k3s.local' && !url.pathname.startsWith('/oauth/'), {timeout: 60000}),
      consent.waitFor({timeout: 60000}).then(() => consent.click()),
    ]);
    const me = await (await authenticated).json();
    if (me.email !== `${user.username}@example.test` || me.role !== 'USER') throw new Error('Incorrect user identity or role');
    const identity = await page.evaluate(async bearer => {
      const response = await fetch('/api/user/codechat-identity', {headers: {Authorization: bearer}});
      if (!response.ok) throw new Error('identity lookup failed');
      return response.json();
    }, bearer);
    result.identities.push(identity);
    fs.writeFileSync(resultsPath, JSON.stringify(result));
    const checks = await page.evaluate(async bearer => {
      const headers = {Authorization: bearer, 'Content-Type': 'application/json'};
      const status = await fetch('/api/keys?name=LiteLLM', {headers}).then(r => r.json());
      const manual = await fetch('/api/keys', {method: 'PUT', headers, body: JSON.stringify({name: 'LiteLLM', value: 'not-a-key'})});
      return {expiry: status.expiresAt, manualStatus: manual.status};
    }, bearer);
    if (checks.expiry !== 'never' || checks.manualStatus !== 403) throw new Error('Managed credential is missing or manual replacement is allowed');
    await context.close();
    }
    console.log('Two registrations, OIDC login, managed credentials, and manual key rejection verified');
  } finally { await browser.close(); }
})().catch(error => {console.error(error.message.replaceAll(user.password, '[redacted]')); process.exitCode = 1;});
