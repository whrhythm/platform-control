// Temporary marked QA identity; credentials are never printed.
const { chromium } = require('../release/browser-qa/node_modules/playwright');
const fs = require('node:fs');
const path = require('node:path');
const identity = JSON.parse(fs.readFileSync(path.join(__dirname, '../release/live-qa-credentials.json'))).users[0];

(async () => {
  const browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--no-proxy-server', '--host-resolver-rules=MAP chat.k3s.local 192.168.10.30,MAP authentik.k3s.local 192.168.10.30']});
  try {
    const page = await browser.newPage({ignoreHTTPSErrors: true, locale: 'en-US'});
    await page.goto('https://chat.k3s.local:30443/login', {waitUntil: 'domcontentloaded', timeout: 60000});
    console.log('Login page loaded');
    await page.getByRole('link', {name: 'Authentik'}).click();
    const username = page.getByRole('textbox', {name: /Email or Username|邮箱或用户名/});
    try { await username.waitFor({timeout: 30000}); } catch (error) {
      console.log('Identity page:', new URL(page.url()).pathname, (await page.locator('body').innerText()).slice(0, 500));
      throw error;
    }
    console.log('Authentik username form ready');
    await username.fill(identity.username);
    await page.getByRole('button', {name: 'Log in', exact: true}).click();
    const password = page.getByPlaceholder('Please enter your password');
    await password.waitFor({timeout: 45000});
    console.log('Authentik password form ready');
    const userResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/user' && response.status() === 200, {timeout: 60000});
    await password.fill(identity.password);
    await password.press('Enter');
    await page.waitForURL(url => url.hostname === 'chat.k3s.local' || url.pathname.includes('default-provider-authorization'), {timeout: 60000});
    if (new URL(page.url()).hostname !== 'chat.k3s.local') {
      const consent = page.getByRole('button', {name: /Continue|Authorize|Allow/i});
      await Promise.race([
        page.waitForURL(url => url.hostname === 'chat.k3s.local', {timeout: 60000}),
        consent.waitFor({timeout: 60000}).then(() => consent.click()),
      ]);
    }
    await page.waitForURL(url => url.hostname === 'chat.k3s.local' && !url.pathname.startsWith('/oauth/'), {timeout: 60000});
    console.log('Returned to LibreChat');
    const user = await (await userResponse).json();
    if (user.email !== `${identity.username}@example.test`) throw new Error('Returned user does not match the test identity');
    console.log('LibreChat Authentik login: authenticated user endpoint 200');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.message.replaceAll(identity.password, '[redacted]')); process.exitCode = 1; });
