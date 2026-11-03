// Narrow synthetic provisioning/resize model. Filesystem growth remains a separate observation.
const http = require('http');
const send = (res, code, body) => { res.writeHead(code, {'Content-Type': 'application/json'}); res.end(JSON.stringify(body)); };
http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const objects = (await pm.state.get('workspace_objects')) || {};
  if (req.method === 'GET' && url.pathname === '/apis/storage.k8s.io/v1/storageclasses/codechat-workspace-retain')
    return send(res, 200, {allowVolumeExpansion: true, provisioner: 'driver.longhorn.io'});
  const match = url.pathname.match(/^\/api\/v1\/namespaces\/([^/]+)$/);
  if (req.method === 'GET' && match) {
    const item = objects['Namespace/' + match[1]];
    return send(res, item ? 200 : 404, item || {});
  }
  const quotaRead = url.pathname.match(/^\/api\/v1\/namespaces\/([^/]+)\/resourcequotas\/([^/]+)$/);
  const policyRead = url.pathname.match(/^\/apis\/networking\.k8s\.io\/v1\/namespaces\/([^/]+)\/networkpolicies\/([^/]+)$/);
  const claimRead = url.pathname.match(/^\/api\/v1\/namespaces\/([^/]+)\/persistentvolumeclaims\/([^/]+)$/);
  const volumeRead = url.pathname.match(/^\/api\/v1\/persistentvolumes\/([^/]+)$/);
  if (req.method === 'PATCH' && (quotaRead || claimRead)) {
    const parts = quotaRead || claimRead;
    const kind = quotaRead ? 'ResourceQuota' : 'PersistentVolumeClaim';
    const item = objects[kind + '/' + parts[1] + '/' + parts[2]];
    const patch = req.body;
    const field = quotaRead ? '/spec/hard/requests.storage' : '/spec/resources/requests/storage';
    if (!item || !Array.isArray(patch) || patch.length !== 3 ||
        patch[0].op !== 'test' || patch[0].path !== '/metadata/uid' || patch[0].value !== item.metadata.uid ||
        patch[1].op !== 'test' || patch[1].path !== '/metadata/resourceVersion' || patch[1].value !== item.metadata.resourceVersion ||
        patch[2].op !== 'replace' || patch[2].path !== field || !/^\d+Gi$/.test(patch[2].value)) return send(res, 409, {});
    const old = quotaRead ? item.spec.hard['requests.storage'] : item.spec.resources.requests.storage;
    const size = parseInt(patch[2].value, 10);
    if (size < parseInt(old, 10)) return send(res, 422, {});
    if (quotaRead) item.spec.hard['requests.storage'] = patch[2].value;
    else {
      const quota = objects['ResourceQuota/' + parts[1] + '/user-capacity'];
      if (!quota || size > parseInt(quota.spec.hard['requests.storage'], 10)) return send(res, 403, {});
      item.spec.resources.requests.storage = patch[2].value;
      item.status.capacity = {storage: patch[2].value};
      objects['PersistentVolume/' + item.spec.volumeName].spec.capacity = {storage: patch[2].value};
    }
    item.metadata.resourceVersion = String(Number(item.metadata.resourceVersion || 1) + 1);
    await pm.state.set('workspace_objects', objects);
    return send(res, 200, item);
  }
  if (req.method === 'GET' && (quotaRead || policyRead || claimRead || volumeRead)) {
    const parts = quotaRead || policyRead || claimRead || volumeRead;
    const kind = quotaRead ? 'ResourceQuota' : policyRead ? 'NetworkPolicy' : claimRead ? 'PersistentVolumeClaim' : 'PersistentVolume';
    const item = objects[kind + '/' + (volumeRead ? parts[1] : parts[1] + '/' + parts[2])];
    return send(res, item ? 200 : 404, item || {});
  }
  const paths = [
    [/^\/api\/v1\/namespaces$/, 'Namespace'],
    [/^\/api\/v1\/namespaces\/([^/]+)\/persistentvolumeclaims$/, 'PersistentVolumeClaim'],
    [/^\/api\/v1\/namespaces\/([^/]+)\/resourcequotas$/, 'ResourceQuota'],
    [/^\/apis\/networking\.k8s\.io\/v1\/namespaces\/([^/]+)\/networkpolicies$/, 'NetworkPolicy'],
  ];
  if (req.method === 'POST') {
    for (const [pattern, kind] of paths) {
      const found = url.pathname.match(pattern);
      if (!found) continue;
      const body = req.body || {};
      if (body.kind !== kind || !body.metadata || !body.metadata.name || (found[1] && body.metadata.namespace !== found[1])) return send(res, 400, {});
      const key = kind + '/' + (found[1] || '') + '/' + body.metadata.name;
      if (objects[key]) return send(res, 409, {});
      const item = {...body, metadata: {...body.metadata, uid: 'test-uid-' + kind, resourceVersion: '1'}};
      if (kind === 'Namespace') item.status = {phase: 'Active'};
      if (url.searchParams.get('dryRun') !== 'All') {
        if (kind === 'PersistentVolumeClaim') {
          const volumeName = 'pvc-' + found[1];
          item.spec = {...item.spec, volumeName};
          item.status = {phase: 'Bound', capacity: {storage: item.spec.resources.requests.storage}};
          objects['PersistentVolume/' + volumeName] = {
            metadata: {name: volumeName, uid: 'test-uid-pv'}, status: {phase: 'Bound'},
            spec: {capacity: {storage: item.spec.resources.requests.storage}, storageClassName: 'codechat-workspace-retain', persistentVolumeReclaimPolicy: 'Retain',
              claimRef: {namespace: found[1], name: body.metadata.name, uid: item.metadata.uid},
              csi: {driver: 'driver.longhorn.io', volumeHandle: 'test-volume-handle'}},
          };
        }
        objects[key] = item;
        if (kind === 'Namespace') objects['Namespace/' + body.metadata.name] = item;
        await pm.state.set('workspace_objects', objects);
      }
      return send(res, 201, item);
    }
  }
  return send(res, 501, {error: 'not modelled'});
}).listen(process.env.PORT || 4501);
