// Contract: installed Harbor v2.15.4 swagger.json. Synthetic credentials only.
const http = require('http');
const crypto = require('crypto');
const send = (res, code, body) => { res.writeHead(code, {'Content-Type': 'application/json'}); res.end(JSON.stringify(body)); };
http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const projects = (await pm.state.get('harbor_delivery_projects')) || {};
  const robots = (await pm.state.get('harbor_delivery_robots')) || {};
  const body = req.body || {};
  if (req.method === 'POST' && url.pathname === '/__test__/seed') {
    await pm.state.set('harbor_delivery_projects', body.projects || {});
    await pm.state.set('harbor_delivery_quota_status', body.quota_status || 200);
    return send(res, 200, {});
  }
  const summary = url.pathname.match(/^\/api\/v2\.0\/projects\/(\d+)\/summary$/);
  if (req.method === 'GET' && summary) {
    const project = Object.values(projects).find(value => value.project_id === Number(summary[1]));
    if (!project) return send(res, 404, {});
    const status = (await pm.state.get('harbor_delivery_quota_status')) || 200;
    return send(res, status, status === 200 ? { quota: project.quota } : { error: 'synthetic quota unavailable' });
  }
  if (req.method === 'POST' && url.pathname === '/api/v2.0/projects') {
    if (!body.project_name || body.metadata?.public !== 'false') return send(res, 400, {});
    if (projects[body.project_name]) return send(res, 409, {});
    projects[body.project_name] = {project_id: Object.keys(projects).length + 1, name: body.project_name, metadata: body.metadata};
    await pm.state.set('harbor_delivery_projects', projects);
    return send(res, 201, {});
  }
  const match = url.pathname.match(/^\/api\/v2\.0\/projects\/([^/]+)$/);
  if (req.method === 'GET' && match) {
    const project = projects[decodeURIComponent(match[1])] || Object.values(projects).find(value => value.project_id === Number(match[1]));
    return send(res, project ? 200 : 404, project || {});
  }
  if (req.method === 'POST' && url.pathname === '/api/v2.0/robots') {
    if (body.level !== 'project' || body.permissions?.length !== 1) return send(res, 403, {});
    const permission = body.permissions[0];
    if (!projects[permission.namespace] || !body.name) return send(res, 400, {});
    if (permission.access.some(a => a.resource !== 'repository' || !['pull','push'].includes(a.action))) return send(res, 403, {});
    const name = 'robot$' + permission.namespace + '+' + body.name;
    if (robots[name]) return send(res, 409, {});
    robots[name] = {id: Object.keys(robots).length + 1, name, secret: crypto.randomBytes(24).toString('hex')};
    await pm.state.set('harbor_delivery_robots', robots);
    return send(res, 201, robots[name]);
  }
  return send(res, 501, {error: 'Not modelled'});
}).listen(process.env.PORT || 4501);
