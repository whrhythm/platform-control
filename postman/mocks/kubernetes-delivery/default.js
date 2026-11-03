// Narrow synthetic UID/patch contract. This does not simulate Kubernetes scheduling or garbage collection.
const http = require('http');
const send = (res, code, body) => { res.writeHead(code, {'Content-Type': 'application/json'}); res.end(JSON.stringify(body)); };
http.createServer(async (req, res) => {
  const path = new URL(req.url, 'http://localhost').pathname.replace(/^\//, '');
  if (req.method === 'POST' && path === '__test__/seed') {
    await pm.state.set('delivery_objects', req.body.objects || {});
    await pm.state.set('delivery_mutations', []);
    return send(res, 200, {});
  }
  const objects = (await pm.state.get('delivery_objects')) || {};
  const mutations = (await pm.state.get('delivery_mutations')) || [];
  if (req.method === 'GET' && path === '__test__/snapshot') return send(res, 200, {objects, mutations});
  const secretCollection = /^api\/v1\/namespaces\/codechat-user-[a-f0-9]{40}\/secrets$/.test(path);
  const databaseSecret = /^api\/v1\/namespaces\/codechat-user-[a-f0-9]{40}\/secrets\/database-[a-f0-9]{24}$/.test(path);
  if (secretCollection && req.method === 'POST') {
    const document = req.body;
    if (!/^database-[a-f0-9]{24}$/.test(document?.metadata?.name || '') || document?.metadata?.labels?.['app.kubernetes.io/managed-by'] !== 'platform-control') return send(res, 400, {});
    const key = path + '/' + document.metadata.name;
    if (objects[key]) return send(res, 409, {});
    document.metadata.uid = 'synthetic-' + document.metadata.name;
    document.metadata.resourceVersion = '1';
    objects[key] = document;
    mutations.push(['POST', key]);
    await pm.state.set('delivery_objects', objects);
    await pm.state.set('delivery_mutations', mutations);
    return send(res, 201, document);
  }
  if (databaseSecret) {
    if (req.method === 'GET') return send(res, objects[path] ? 200 : 404, objects[path] || {});
    if (req.method !== 'DELETE') return send(res, 501, {});
    if (!objects[path]) return send(res, 404, {});
    if (req.body?.preconditions?.uid !== objects[path].metadata.uid) return send(res, 409, {});
    delete objects[path];
    mutations.push(['DELETE', path]);
    await pm.state.set('delivery_objects', objects);
    await pm.state.set('delivery_mutations', mutations);
    return send(res, 202, {});
  }
  const job = /^apis\/batch\/v1\/namespaces\/codechat-builds\/jobs\/build-[a-z0-9]+$/.test(path);
  const deployment = /^apis\/apps\/v1\/namespaces\/codechat-user-[a-f0-9]{40}\/deployments\/app-[a-f0-9]+$/.test(path);
  if (!job && !deployment) return send(res, 501, {error: 'not modelled'});
  if (req.method === 'GET') return send(res, objects[path] ? 200 : 404, objects[path] || {});
  if (!objects[path]) return send(res, 404, {});
  if (job && req.method === 'DELETE') {
    if (req.body?.preconditions?.uid !== objects[path].metadata.uid) return send(res, 409, {});
    if (req.body?.propagationPolicy !== 'Foreground') return send(res, 400, {});
    delete objects[path];
  } else if (deployment && req.method === 'PATCH') {
    const patch = req.body;
    if (!Array.isArray(patch) || patch[0]?.path !== '/metadata/uid' || patch[1]?.path !== '/metadata/resourceVersion' ||
        patch[0]?.value !== objects[path].metadata.uid || patch[1]?.value !== objects[path].metadata.resourceVersion) return send(res, 409, {});
    for (const item of patch.slice(2)) {
      if (item.op !== 'replace' || !['/spec/template', '/spec/replicas'].includes(item.path)) return send(res, 501, {});
      objects[path].spec[item.path.split('/').pop()] = item.value;
    }
    objects[path].status = {observedGeneration: objects[path].metadata.generation,
      replicas: objects[path].spec.replicas, availableReplicas: objects[path].spec.replicas,
      updatedReplicas: objects[path].spec.replicas, readyReplicas: objects[path].spec.replicas};
  } else return send(res, 501, {});
  mutations.push([req.method, path]);
  await pm.state.set('delivery_objects', objects);
  await pm.state.set('delivery_mutations', mutations);
  return send(res, job ? 202 : 200, objects[path] || {});
}).listen(process.env.PORT || 4502);
