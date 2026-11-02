"""Verify Retain with a uniquely owned disposable PVC, then remove only its PV.

This never touches a user workspace. If ownership cannot be proved, cleanup
stops and reports the test resources for an operator to inspect.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import time

binary = os.environ.get('KUBECTL_BIN') or shutil.which('kubectl')
if not binary:
    raise RuntimeError('kubectl not found')
K = [binary, '--kubeconfig', os.environ['KUBECONFIG'], '--server', os.environ['PLATFORM_KUBE_SERVER']]
run = secrets.token_hex(6)
namespace = 'codechat-retain-qa-' + run
labels = {'codechat.io/qa-run': run, 'codechat.io/purpose': 'workspace-retention-test'}
pv_name = pv_uid = claim_uid = volume = None
namespace_created = False


def call(*args, data=None):
    result = subprocess.run(K + list(args), input=json.dumps(data) if data is not None else None,
                            text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError('kubectl failed: ' + result.stderr[-1000:])
    return result.stdout


def get(*args):
    return json.loads(call('get', *args, '-o', 'json'))


def wait_for(fn, timeout=180):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(3)
    raise RuntimeError('Timed out waiting for test resource')


def owned_pv():
    pv = get('pv', pv_name)
    ref = pv['spec'].get('claimRef', {})
    if (pv['metadata']['uid'] != pv_uid or ref.get('uid') != claim_uid
            or ref.get('namespace') != namespace or ref.get('name') != 'data'
            or pv['spec'].get('storageClassName') != 'codechat-workspace-retain'):
        raise RuntimeError('Test PV ownership changed; refusing cleanup')
    return pv


try:
    ns = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': namespace, 'labels': labels}}
    call('create', '-f', '-', data=ns)
    namespace_created = True
    pvc = {'apiVersion': 'v1', 'kind': 'PersistentVolumeClaim', 'metadata': {'name': 'data', 'namespace': namespace, 'labels': labels},
           'spec': {'storageClassName': 'codechat-workspace-retain', 'accessModes': ['ReadWriteOnce'], 'resources': {'requests': {'storage': '1Gi'}}}}
    call('create', '-f', '-', data=pvc)
    claim = wait_for(lambda: (p if (p := get('pvc', 'data', '-n', namespace)).get('status', {}).get('phase') == 'Bound' else None))
    claim_uid = claim['metadata']['uid']
    pv_name = claim['spec']['volumeName']
    pv = get('pv', pv_name)
    pv_uid = pv['metadata']['uid']
    pv = owned_pv()
    assert pv['spec']['persistentVolumeReclaimPolicy'] == 'Retain'
    assert pv['spec']['csi']['driver'] == 'driver.longhorn.io'
    volume = pv['spec']['csi']['volumeHandle']
    call('-n', namespace, 'delete', 'pvc', 'data', '--wait=true', '--timeout=120s')
    released = wait_for(lambda: (p if (p := owned_pv()).get('status', {}).get('phase') == 'Released' else None))
    assert released['spec']['persistentVolumeReclaimPolicy'] == 'Retain'
    get('volumes.longhorn.io', volume, '-n', 'longhorn-system')
    print('PASS: PVC deleted; PV Released and Longhorn volume retained')
finally:
    if pv_name and pv_uid and claim_uid:
        # Atomic tests prevent a changed/reassigned PV from being modified.
        pv = owned_pv()
        patch = [{'op': 'test', 'path': '/metadata/uid', 'value': pv_uid},
                 {'op': 'test', 'path': '/spec/claimRef/uid', 'value': claim_uid},
                 {'op': 'replace', 'path': '/spec/persistentVolumeReclaimPolicy', 'value': 'Delete'}]
        call('patch', 'pv', pv_name, '--type=json', '-p', json.dumps(patch))
        # The PV controller can delete the Released PV immediately after the patch.
        call('delete', 'pv', pv_name, '--wait=true', '--timeout=120s', '--ignore-not-found=true')
        if volume:
            wait_for(lambda: not call('-n', 'longhorn-system', 'get', 'volumes.longhorn.io', volume, '--ignore-not-found=true', '-o', 'name').strip())
    if namespace_created:
        ns = get('namespace', namespace)
        if any(ns['metadata'].get('labels', {}).get(k) != v for k, v in labels.items()):
            raise RuntimeError('Test namespace ownership changed; refusing cleanup')
        call('delete', 'namespace', namespace, '--wait=true', '--timeout=180s')
    print('Cleanup requested for owned test resources:', namespace, pv_name)
