const fs = require('node:fs');
const https = require('node:https');
const path = require('node:path');
const ca = fs.readFileSync(path.join(__dirname, '../release/internal-ca.crt'));
async function check(host, target) {
  return new Promise((resolve, reject) => {
    const request = https.get({hostname: '127.0.0.1', port: 30443, servername: host, path: target, headers: {Host: host + ':30443'}, ca, rejectUnauthorized: true, timeout: 20000}, response => {
      const certificateVerified = response.socket.authorized;
      let data = ''; response.on('data', chunk => data += chunk);
      response.on('end', () => {
        let document = {}; try {document = JSON.parse(data);} catch {}
        resolve({host, status: response.statusCode, certificateVerified, issuer: document.issuer, serviceStatus: document.status});
      });
    });
    request.on('error', reject); request.on('timeout', () => request.destroy(new Error('TLS check timeout')));
  });
}
(async () => {
  for (const [host, endpoint] of [['authentik.k3s.local', '/application/o/platform/.well-known/openid-configuration'], ['platform.k3s.local', '/healthz']]) console.log(JSON.stringify(await check(host, endpoint)));
})().catch(error => {console.error(error.message); process.exitCode = 1;});
