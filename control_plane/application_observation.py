"""Bounded app-owner observations from named Kubernetes pods, never shared logs."""
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote

import httpx


def quantity(value, *, cpu=False):
    suffixes = {'n': Decimal('0.000000001'), 'u': Decimal('0.000001'), 'm': Decimal('0.001')} if cpu else {
        'Ki': 1024, 'Mi': 1024 ** 2, 'Gi': 1024 ** 3, 'K': 1000, 'M': 1000 ** 2, 'G': 1000 ** 3}
    for suffix, multiplier in suffixes.items():
        if value.endswith(suffix):
            return Decimal(value[:-len(suffix)]) * multiplier
    return Decimal(value)


def observe(cluster, namespace, application_id):
    selector = 'codechat.io/app-id=' + application_id
    result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'metrics': None, 'logs': ''}
    pods = cluster.client.get(f'api/v1/namespaces/{namespace}/pods', params={'labelSelector': selector})
    if pods.status_code != 200:
        return result
    items = [pod for pod in pods.json().get('items', []) if pod.get('metadata', {}).get('labels', {}).get('codechat.io/app-id') == application_id
             and not pod.get('metadata', {}).get('deletionTimestamp')]
    if not items:
        return result
    # Prefer the newest non-terminating pod during a rolling release.
    pod = sorted(items, key=lambda item: item['metadata'].get('creationTimestamp', ''), reverse=True)[0]
    name = quote(pod['metadata']['name'], safe='')
    response = cluster.client.get(f'api/v1/namespaces/{namespace}/pods/{name}/log',
        params={'container': 'app', 'tailLines': 50, 'limitBytes': 16384, 'timestamps': 'true'})
    if response.status_code == 200:
        result['logs'] = response.text[-16384:]
    response = cluster.client.get(f'apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods/{name}')
    if response.status_code == 200:
        try:
            containers = [item for item in response.json()['containers'] if item['name'] == 'app']
            limits = next(item for item in pod['spec']['containers'] if item['name'] == 'app')['resources']['limits']
            cpu_limit, memory_limit = quantity(str(limits['cpu']), cpu=True), quantity(str(limits['memory']))
            if not containers or cpu_limit <= 0 or memory_limit <= 0:
                return result
            cpu = sum(quantity(item['usage']['cpu'], cpu=True) for item in containers)
            memory = sum(quantity(item['usage']['memory']) for item in containers)
            result['metrics'] = {'cpu_millicores': round(float(cpu * 1000), 2), 'cpu_limit_millicores': float(cpu_limit * 1000),
                'cpu_percent': round(float(cpu / cpu_limit * 100), 2), 'memory_bytes': int(memory),
                'memory_limit_bytes': int(memory_limit), 'memory_percent': round(float(memory / memory_limit * 100), 2)}
        except (KeyError, TypeError, ArithmeticError, ValueError, StopIteration):
            pass
    return result
