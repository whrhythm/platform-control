// Temporary account only; no credentials or response bodies are printed.
const {chromium} = require('../release/browser-qa/node_modules/playwright');
const fs = require('node:fs');
const user = JSON.parse(fs.readFileSync('release/live-qa-credentials.json')).users[0];
(async () => {
  const browser = await chromium.launch({executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true, args: ['--no-proxy-server', '--host-resolver-rules=MAP chat.k3s.local 192.168.10.30,MAP authentik.k3s.local 192.168.10.30,MAP platform.k3s.local 192.168.10.30']});
  const context = await browser.newContext({ignoreHTTPSErrors: true, locale: 'en-US'});
  if (process.env.CODECHAT_PREVIEW_HEADER) await context.setExtraHTTPHeaders({'X-CodeChat-Preview': process.env.CODECHAT_PREVIEW_HEADER});
  const page = await context.newPage();
  let auth = '';
  page.on('request', request => { if (request.url().includes('/api/user') && request.headers().authorization) auth = request.headers().authorization; });
  page.on('response', response => { if (response.status() >= 400) console.log('HTTP error:', response.status(), new URL(response.url()).pathname); });
  try {
    await page.goto('https://chat.k3s.local:30443/login', {waitUntil: 'domcontentloaded'});
    await page.getByRole('link', {name: 'Authentik'}).click();
    await page.waitForLoadState('domcontentloaded');
    await page.getByRole('textbox', {name: /Email or Username|邮箱或用户名/}).fill(user.username, {timeout: 90000});
    await page.getByRole('button', {name: 'Log in', exact: true}).click();
    const password = page.getByPlaceholder('Please enter your password');
    await password.fill(user.password);
    await password.press('Enter');
    await page.waitForURL(url => url.hostname === 'chat.k3s.local' && !url.pathname.startsWith('/oauth/'), {timeout: 60000});
    await page.waitForTimeout(3000);
    const catalog = await page.evaluate(async auth => {
      const response = await fetch('/api/models', {headers: {Authorization: auth}});
      const data = await response.json();
      return {status: response.status, models: data.LiteLLM};
    }, auth);
    if (catalog.status !== 200 || !catalog.models.includes('deepseek-v4-flash-v2')) throw new Error('LiteLLM model missing');
    await page.getByRole('button', {name: 'Select a model'}).click();
    if (await page.getByRole('button', {name: 'Set API Key LiteLLM'}).count() !== 0) throw new Error('Managed endpoint still shows manual key setup');
    await page.getByText('LiteLLM', {exact:true}).first().click({timeout: 15000});
    await page.getByText('deepseek-v4-flash-v2', {exact:true}).first().click();
    const input = page.getByRole('textbox', {name: 'Message input'});
    await input.fill('Reply with exactly OK.');
    await input.press('Enter');
    await page.getByText(/^OK[.!]?$/i).first().waitFor({timeout: 120000});
    console.log('Real LibreChat model request received a response using the managed user credential');
  } finally { await browser.close(); }
})().catch(error => { console.error('Model chat acceptance failed:', error.message.replaceAll(user.password, '[redacted]')); process.exitCode = 1; });
