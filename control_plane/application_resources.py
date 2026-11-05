"""Runtime limits derived only from activated plan snapshots, never client claims."""
from .application_observation import quantity
from .models import CapacityAccount


CPU = 'application_cpu_millicores'
MEMORY = 'application_memory_mib'


def plan_resources(plan):
    # Compatibility for snapshots issued before compute rights were explicit.
    # Unknown/never-purchased plans get trial rights, not rights inferred from price.
    cpu, memory = {'basic': (500, 512), 'pro': (1000, 1024)}.get(plan.get('id'), (250, 256))
    cpu = cpu if plan.get(CPU) is None else plan[CPU]
    memory = memory if plan.get(MEMORY) is None else plan[MEMORY]
    if type(cpu) is not int or type(memory) is not int or not 50 <= cpu <= 4000 or not 128 <= memory <= 8192:
        raise ValueError('Invalid application resource policy')
    return {CPU: cpu, MEMORY: memory}


def enriched_plan(plan):
    from .code_execution import code_rights
    from .plan_quotas import quota_rights
    return {**plan, **plan_resources(plan), **code_rights(plan), **quota_rights(plan)}


def active_resources(db, environment_id):
    account = db.get(CapacityAccount, environment_id)
    return plan_resources(account.plan if account else {})


def container_resources(rights):
    return {'requests': {'cpu': f'{min(100, rights[CPU])}m', 'memory': '128Mi'},
            'limits': {'cpu': f'{rights[CPU]}m', 'memory': f'{rights[MEMORY]}Mi'}}


def resources_match(actual, desired):
    try:
        return all(quantity(str(actual[kind][resource]), cpu=resource == 'cpu') ==
                   quantity(str(desired[kind][resource]), cpu=resource == 'cpu')
                   for kind in ('requests', 'limits') for resource in ('cpu', 'memory'))
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return False


def template_resources_match(actual, desired):
    containers = actual.get('spec', {}).get('containers', [])
    current = next((value for value in containers if value.get('name') == 'app'), {})
    expected = next(value for value in desired['spec']['containers'] if value['name'] == 'app')
    return resources_match(current.get('resources', {}), expected['resources'])
