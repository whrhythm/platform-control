"""Restricted create-only Kubernetes API adapter; not wired to any worker.

Only exact server-generated workspace manifests are accepted. This client does
not provide delete, patch, apply, arbitrary paths, or end-user authorization.
"""
from copy import deepcopy
import ipaddress
import json
import re
import ssl
from urllib.parse import quote, urlsplit

import httpx

from .user_environments import plan_user_environment


class KubernetesProviderError(RuntimeError):
    """Safe error: deliberately excludes credentials and response bodies."""


class KubernetesWorkspaceProvider:
    def __init__(self, *, api_url: str, token: str, issuer: str, subject: str,
                 storage_gib: int, ca_file: str | None = None, dry_run: bool = False,
                 allow_test_loopback_http: bool = False, timeout_seconds: float = 10):
        url = urlsplit(api_url)
        if url.username or url.password or url.query or url.fragment or not url.hostname:
            raise ValueError('Invalid Kubernetes API URL')
        if url.scheme != 'https':
            try:
                local = ipaddress.ip_address(url.hostname).is_loopback
            except ValueError:
                local = url.hostname == 'localhost'
            if url.scheme != 'http' or not allow_test_loopback_http or not local:
                raise ValueError('Kubernetes API must use HTTPS')
        if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
            raise ValueError('A valid provider token is required')
        if not 0 < timeout_seconds <= 60:
            raise ValueError('Timeout must be between 0 and 60 seconds')
        context = ssl.create_default_context(cafile=ca_file)
        self.plan = plan_user_environment(issuer, subject, storage_gib=storage_gib)
        self.namespace = self.plan['namespace']
        self.objects = {item['kind']: item for item in self.plan['objects']}
        self._manifest_seals = {kind: json.dumps(item, sort_keys=True, separators=(',', ':')) for kind, item in self.objects.items()}
        self.dry_run = dry_run
        self.client = httpx.Client(base_url=api_url.rstrip('/') + '/', verify=context,
                                   headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'},
                                   timeout=timeout_seconds, trust_env=False, follow_redirects=False)

    def close(self):
        self.client.close()

    def _request(self, method: str, path: str, *, manifest: dict | None = None) -> dict:
        try:
            result = self.client.request(method, path, json=manifest,
                                         params={'dryRun': 'All', 'fieldValidation': 'Strict'} if self.dry_run and method == 'POST' else None)
        except httpx.HTTPError:
            raise KubernetesProviderError('Kubernetes API request failed; outcome may be unknown') from None
        if result.status_code != (201 if method == 'POST' else 200):
            raise KubernetesProviderError(f'Kubernetes API returned HTTP {result.status_code}; outcome requires review')
        try:
            payload = result.json()
        except ValueError:
            raise KubernetesProviderError('Kubernetes API returned invalid JSON; outcome may be unknown') from None
        if not isinstance(payload, dict):
            raise KubernetesProviderError('Kubernetes API returned invalid resource; outcome may be unknown')
        return payload

    def _create(self, kind: str, manifest: dict, path: str) -> dict:
        if json.dumps(manifest, sort_keys=True, separators=(',', ':')) != self._manifest_seals[kind]:
            raise ValueError('Only the exact generated workspace manifest is allowed')
        return self._request('POST', path, manifest=deepcopy(manifest))

    def create_namespace(self, manifest: dict) -> dict:
        return self._create('Namespace', manifest, 'api/v1/namespaces')

    def read_namespace(self, name: str) -> dict:
        if name != self.namespace:
            raise ValueError('Namespace is outside this workspace')
        return self._request('GET', 'api/v1/namespaces/' + quote(name, safe=''))

    def create_pvc(self, manifest: dict) -> dict:
        return self._create('PersistentVolumeClaim', manifest, f'api/v1/namespaces/{self.namespace}/persistentvolumeclaims')

    def create_resource_quota(self, manifest: dict) -> dict:
        return self._create('ResourceQuota', manifest, f'api/v1/namespaces/{self.namespace}/resourcequotas')

    def create_network_policy(self, manifest: dict) -> dict:
        return self._create('NetworkPolicy', manifest, f'apis/networking.k8s.io/v1/namespaces/{self.namespace}/networkpolicies')

    def read_resource_quota(self) -> dict:
        return self._request('GET', f'api/v1/namespaces/{self.namespace}/resourcequotas/user-capacity')

    def read_network_policy(self) -> dict:
        return self._request('GET', f'apis/networking.k8s.io/v1/namespaces/{self.namespace}/networkpolicies/default-deny')

    def inspect_claim_volume(self, expected_pvc_uid: str) -> tuple[dict, dict | None]:
        """Read only the PV named by this workspace's previously created PVC."""
        if not isinstance(expected_pvc_uid, str) or not expected_pvc_uid.strip():
            raise ValueError('A recorded PVC UID is required')
        pvc = self._request('GET', f'api/v1/namespaces/{self.namespace}/persistentvolumeclaims/workspace-data')
        meta, spec = pvc.get('metadata', {}), pvc.get('spec', {})
        labels = self.objects['PersistentVolumeClaim']['metadata']['labels']
        if (meta.get('uid') != expected_pvc_uid or meta.get('name') != 'workspace-data' or
                meta.get('namespace') != self.namespace or meta.get('deletionTimestamp') or
                any(meta.get('labels', {}).get(key) != value for key, value in labels.items()) or
                spec.get('storageClassName') != 'codechat-workspace-retain'):
            raise KubernetesProviderError('Observed PVC does not match the recorded workspace')
        if pvc.get('status', {}).get('phase') != 'Bound':
            return pvc, None
        volume_name = spec.get('volumeName')
        if (not isinstance(volume_name, str) or len(volume_name) > 253 or
                not re.fullmatch(r'[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?', volume_name)):
            raise KubernetesProviderError('Observed PVC has invalid PV name')
        pv = self._request('GET', 'api/v1/persistentvolumes/' + quote(volume_name, safe=''))
        return pvc, pv
