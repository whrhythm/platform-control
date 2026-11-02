// Narrow, synthetic create-only model. Not a Kubernetes implementation.
const http = require('http');
const send = (res, code, body) => { res.writeHead(code, {'Content-Type': 'application/json'}); res.end(JSON.stringify(body)); };
http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const objects = (await pm.state.get('workspace_objects')) || {};
  const match = url.pathname.match(/^\/api\/v1\/namespaces\/([^/]+)$/);
  if (req.method === 'GET' && match) {
    const item = objects['Namespace/' + match[1]];
    return send(res, item ? 200 : 404, item || {});
  }
  const quotaRead = url.pathname.match(/^\/api\/v1\/namespaces\/([^/]+)\/resourcequotas\/([^/]+)$/);
  const policyRead = url.pathname.match(/^\/apis\/networking\.k8s\.io\/v1\/namespaces\/([^/]+)\/networkpolicies\/([^/]+)$/);
  const claimRead = url.pathname.match(/^\/api\/v1\/namespaces\/([^/]+)\/persistentvolumeclaims\/([^/]+)$/);
  const volumeRead = url.pathname.match(/^\/api\/v1\/persistentvolumes\/([^/]+)$/);
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
      const item = {...body, metadata: {...body.metadata, uid: 'test-uid-' + kind}};
      if (kind === 'Namespace') item.status = {phase: 'Active'};
      if (url.searchParams.get('dryRun') !== 'All') {
        if (kind === 'PersistentVolumeClaim') {
          const volumeName = 'pvc-' + found[1];
          item.spec = {...item.spec, volumeName};
          item.status = {phase: 'Bound'};
          objects['PersistentVolume/' + volumeName] = {
            metadata: {name: volumeName, uid: 'test-uid-pv'}, status: {phase: 'Bound'},
            spec: {storageClassName: 'codechat-workspace-retain', persistentVolumeReclaimPolicy: 'Retain',
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
