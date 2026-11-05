"""Grow only a recorded retained claim; activate rights after observed growth."""
from datetime import timezone
import json
import re

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from .auth import Principal
from .capacity import GIB
from .application_resources import CPU, MEMORY, plan_resources
from .kubernetes_workspace_provider import KubernetesProviderError
from .models import (AuditEvent, CapacityAccount, CapacityOrder, UserEnvironment,
                     WorkspaceProvisioningIntent, WorkspaceVolumeBinding, now)
from .workspace_binding import BindingMismatch, VolumeBinding, verify_volume_binding
from .workspace_gateway import call_files
from .workspace_provisioning import _metadata_match, UnsafeProviderResponse


class ExpansionNeedsReview(ValueError):
    pass


def storage_bytes(value):
    match = re.fullmatch(r'(\d+)(Ki|Mi|Gi|Ti)?', str(value))
    if not match:
        raise ExpansionNeedsReview()
    return int(match[1]) * {'Ki': 1024, 'Mi': 1024 ** 2, 'Gi': GIB, 'Ti': 1024 ** 4, None: 1}[match[2]]


def patch_increase(provider, path, actual, field, current, target):
    if current > target:
        raise ExpansionNeedsReview()
    if current == target:
        return False
    meta = actual['metadata']
    patch = [{'op': 'test', 'path': '/metadata/uid', 'value': meta['uid']},
             {'op': 'test', 'path': '/metadata/resourceVersion', 'value': meta['resourceVersion']},
             {'op': 'replace', 'path': field, 'value': f'{target // GIB}Gi'}]
    result = provider.client.patch(path, content=json.dumps(patch), headers={'Content-Type': 'application/json-patch+json'})
    if result.status_code != 200:
        raise KubernetesProviderError('Capacity write must be observed before retry')
    return True


def reconcile_capacity(sessions, provider, env_id, settings, *, probe=None):
    """Return True while an upgrade owns storage reconciliation, including failures."""
    with sessions.begin() as db:
        env = db.scalar(select(UserEnvironment).where(UserEnvironment.id == env_id).with_for_update())
        account = db.get(CapacityAccount, env_id)
        order = db.get(CapacityOrder, account.active_order_id) if account and account.active_order_id else None
        if not order or order.status in ('pending_payment', 'ready', 'cancelled'):
            return False
        if order.status == 'failed':
            return True
        if order.plan.get('model_credit_usd_micros', 0) and order.evidence.get('model_credit_status') != 'applied':
            order.stage = 'crediting_models'
            return True
        started = order.started_at.replace(tzinfo=timezone.utc) if order.started_at and order.started_at.tzinfo is None else order.started_at
        if (not started or not 1 <= settings.capacity_resize_timeout_seconds <= 86400 or
                not 0 <= settings.capacity_filesystem_overhead_percent <= 20):
            order.status, order.error_code = 'failed', 'capacity_configuration_requires_review'
            return True
        if (now() - started).total_seconds() >= settings.capacity_resize_timeout_seconds:
            order.status, order.stage, order.error_code = 'failed', 'failed', 'capacity_expand_timeout'
            order.completed_at = now()
            return True
        binding = db.get(WorkspaceVolumeBinding, env_id)
        intent = db.get(WorkspaceProvisioningIntent, env_id)
        if not binding or not intent or not intent.quota_uid or env.status not in ('requested', 'ready'):
            order.status, order.error_code = 'failed', 'capacity_identity_requires_review'
            return True
        recorded = VolumeBinding(**{key: getattr(binding, key) for key in VolumeBinding.__dataclass_fields__})
        issuer, subject, current_gib = env.issuer, env.subject, env.storage_gib
        quota_uid, target = intent.quota_uid, order.plan['storage_gib'] * GIB
        order_id, attempt, evidence = order.id, order.attempt, dict(order.evidence)

    def checkpoint(stage, *, error=None, baseline=None, complete=False):
        with sessions.begin() as db:
            env = db.scalar(select(UserEnvironment).where(UserEnvironment.id == env_id).with_for_update())
            account = db.get(CapacityAccount, env_id)
            order = db.get(CapacityOrder, order_id)
            if (not account or account.active_order_id != order_id or not order or
                    order.status != 'applying' or order.attempt != attempt):
                return
            order.stage = stage
            if baseline:
                order.evidence = {**order.evidence, **baseline}
            if error:
                order.status, order.error_code = 'failed', error
                order.completed_at = now()
            if complete:
                if env.storage_gib != current_gib or env.storage_gib > order.plan['storage_gib']:
                    order.status, order.error_code = 'failed', 'capacity_identity_requires_review'
                    return
                env.storage_gib = order.plan['storage_gib']
                active_plan = dict(account.plan) if order.evidence.get('keep_current_plan') else dict(order.plan)
                active_plan.update(storage_gib=order.plan['storage_gib'], application_limit=order.plan['application_limit'])
                active_plan.update({key: max(plan_resources(account.plan)[key], plan_resources(order.plan)[key]) for key in (CPU, MEMORY)})
                from .code_execution import code_rights, FIELDS
                active_plan.update({key: max(code_rights(account.plan)[key], code_rights(order.plan)[key]) for key in FIELDS})
                from .plan_quotas import quota_rights, FIELDS as QUOTA_FIELDS
                active_plan.update({key: max(quota_rights(account.plan)[key], quota_rights(order.plan)[key]) for key in QUOTA_FIELDS})
                account.plan, account.active_order_id, account.updated_at = active_plan, None, now()
                order.status, order.completed_at, order.error_code = 'ready', now(), None
                db.add(AuditEvent(tenant_id=env.first_tenant_id, issuer=issuer, subject=subject,
                    action='capacity.activated', target_id=order_id))

    def measure():
        if probe:
            return probe()
        with sessions() as db:
            return call_files(db, Principal(issuer=issuer, subject=subject), settings, 'GET', '/v1/usage', '').json()

    try:
        ns = provider.read_namespace(provider.namespace)
        pvc, pv = provider.inspect_claim_volume(recorded.pvc_uid)
        if not pv or verify_volume_binding(issuer, subject, recorded, ns, pvc, pv) != 'ready':
            checkpoint('waiting_for_storage')
            return True
        quota = provider.read_resource_quota()
        _metadata_match(quota, provider.objects['ResourceQuota'], namespace_uid=quota_uid)
        sc = provider._request('GET', 'apis/storage.k8s.io/v1/storageclasses/codechat-workspace-retain')
        if sc.get('allowVolumeExpansion') is not True or sc.get('provisioner') != 'driver.longhorn.io':
            raise ExpansionNeedsReview()
        requested = storage_bytes(pvc['spec']['resources']['requests']['storage'])
        quota_size = storage_bytes(quota['spec']['hard']['requests.storage'])
        if requested > target or quota_size > target:
            raise ExpansionNeedsReview()
        if 'before_filesystem_bytes' not in evidence:
            if requested != current_gib * GIB:
                raise ExpansionNeedsReview()
            sample = measure()
            total = sample.get('physical_total_bytes')
            if type(total) is not int or total <= 0:
                checkpoint('waiting_for_measurement')
                return True
            # Commit before the first provider mutation, so process death can resume safely.
            checkpoint('checking_storage', baseline={'before_filesystem_bytes': total,
                'before_storage_bytes': current_gib * GIB, 'pvc_uid': recorded.pvc_uid})
            return True
        root = f'api/v1/namespaces/{provider.namespace}'
        if patch_increase(provider, root + '/resourcequotas/user-capacity', quota,
                          '/spec/hard/requests.storage', quota_size, target):
            checkpoint('quota_updated')
            return True
        if patch_increase(provider, root + '/persistentvolumeclaims/workspace-data', pvc,
                          '/spec/resources/requests/storage', requested, target):
            checkpoint('waiting_for_storage')
            return True
        conditions = pvc.get('status', {}).get('conditions', [])
        if (storage_bytes(pvc.get('status', {}).get('capacity', {}).get('storage', '0')) < target or
                storage_bytes(pv.get('spec', {}).get('capacity', {}).get('storage', '0')) < target or
                any(item.get('status') == 'True' and item.get('type') in ('Resizing', 'FileSystemResizePending') for item in conditions)):
            checkpoint('waiting_for_filesystem')
            return True
        sample = measure()
        total = sample.get('physical_total_bytes')
        if (type(total) is not int or total < target * (100 - settings.capacity_filesystem_overhead_percent) // 100 or
                (target > evidence['before_storage_bytes'] and total <= evidence['before_filesystem_bytes'])):
            checkpoint('waiting_for_filesystem')
            return True
        checkpoint('active', baseline={'observed_filesystem_bytes': total, 'observed_storage_bytes': target}, complete=True)
    except (ExpansionNeedsReview, BindingMismatch, UnsafeProviderResponse):
        checkpoint('failed', error='capacity_identity_requires_review')
    except (httpx.HTTPError, KubernetesProviderError, HTTPException, KeyError, ValueError, TypeError):
        checkpoint('waiting_for_provider')
    return True
