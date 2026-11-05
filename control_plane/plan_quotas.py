"""Versioned quota snapshots shared by admission, workers and both user interfaces."""
from pydantic import BaseModel, Field


class PlanQuotas(BaseModel):
    applications_cpu_millicores: int | None = Field(default=None, ge=50, le=32000)
    applications_memory_mib: int | None = Field(default=None, ge=128, le=65536)
    database_budget_mib: int | None = Field(default=None, ge=0, le=40960)
    database_per_instance_mib: int | None = Field(default=None, ge=0, le=40960)
    database_limit: int | None = Field(default=None, ge=0, le=100)
    agent_cpu_millicores: int | None = Field(default=None, ge=100, le=4000)
    agent_memory_mib: int | None = Field(default=None, ge=128, le=8192)
    agent_command_seconds: int | None = Field(default=None, ge=10, le=1800)
    agent_task_seconds: int | None = Field(default=None, ge=60, le=7200)
    agent_concurrency: int | None = Field(default=None, ge=1, le=2)
    build_cpu_millicores: int | None = Field(default=None, ge=250, le=4000)
    build_memory_mib: int | None = Field(default=None, ge=1536, le=8192)
    build_timeout_seconds: int | None = Field(default=None, ge=60, le=1800)
    build_concurrency: int | None = Field(default=None, ge=1, le=4)


FIELDS = tuple(PlanQuotas.model_fields)


def quota_rights(plan, settings=None):
    from .application_resources import CPU, MEMORY, plan_resources
    from .settings import get_settings
    settings = settings or get_settings()
    compute = plan_resources(plan)
    defaults = dict(
        applications_cpu_millicores=compute[CPU] * plan.get('application_limit', settings.default_application_limit),
        applications_memory_mib=compute[MEMORY] * plan.get('application_limit', settings.default_application_limit),
        database_budget_mib=settings.project_database_budget_mib,
        database_per_instance_mib=settings.project_database_reservation_mib,
        database_limit=settings.project_database_limit,
        agent_cpu_millicores=max(100, compute[CPU]), agent_memory_mib=compute[MEMORY],
        agent_command_seconds=settings.agent_command_seconds, agent_task_seconds=settings.agent_task_seconds,
        agent_concurrency=1, build_cpu_millicores=2000, build_memory_mib=settings.build_memory_mib,
        build_timeout_seconds=900, build_concurrency=1)
    return {key: plan[key] if plan.get(key) is not None else value for key, value in defaults.items()}


def active_quotas(db, environment_id, settings=None):
    from .models import CapacityAccount
    account = db.get(CapacityAccount, environment_id)
    return quota_rights(account.plan if account else {}, settings)


def application_allocations(db, environment_id, *, exclude=None):
    from sqlalchemy import select
    from .application_resources import CPU, MEMORY, active_resources
    from .models import UserApplication
    fallback = active_resources(db, environment_id)
    total = {CPU: 0, MEMORY: 0}
    for app in db.scalars(select(UserApplication).where(UserApplication.environment_id == environment_id)):
        if app.id == exclude or app.status in ('deleted', 'stopped'):
            continue
        if (app.status == 'failed' and app.resources.get('cleanup', {}).get('status') == 'complete'
                and app.resources.get('cleanup', {}).get('runtime') != 'previous_release'):
            continue
        current = app.resources.get('runtime_rights') or app.resources.get('last_good', {}).get('runtime_rights') or fallback
        previous = (app.resources.get('last_good', {}).get('runtime_rights') or current) if app.status != 'running' else current
        for key in total:
            total[key] += max(current[key], previous[key])
    return total


def admit_application(db, env, rights, *, exclude=None, previous=None):
    from fastapi import HTTPException
    from .application_resources import CPU, MEMORY
    quotas = active_quotas(db, env.id)
    used = application_allocations(db, env.id, exclude=exclude)
    for key, limit in ((CPU, 'applications_cpu_millicores'), (MEMORY, 'applications_memory_mib')):
        if used[key] + max(rights[key], (previous or rights)[key]) > quotas[limit]:
            raise HTTPException(409, 'application_compute_allowance_exceeded')
