"""Transactional plan worker. Deliberately cannot apply infrastructure."""
import argparse
import time

from sqlalchemy import select

from .database import SessionLocal
from .models import AuditEvent, Operation, Resource, Tenant
from .planner import plan
from .settings import get_settings


def plan_one(session_factory=SessionLocal) -> bool:
    with session_factory() as db:
        candidate = db.scalar(select(Operation).where(Operation.status == "pending").order_by(Operation.created_at).limit(1))
        if candidate is None:
            return False
        # Same tenant -> resource -> operation lock order as cancellation.
        tenant = db.scalar(select(Tenant).where(Tenant.id == candidate.tenant_id).with_for_update())
        resource = db.scalar(select(Resource).where(Resource.id == candidate.resource_id).with_for_update())
        operation = db.scalar(select(Operation).where(Operation.id == candidate.id).with_for_update().execution_options(populate_existing=True))
        if operation.status != "pending":
            return True
        if resource.status == "cancelled":
            operation.status = "cancelled"
        else:
            operation.plan = plan(resource, tenant, get_settings())
            operation.status = "planned"
            resource.status = "planned"
            db.add(AuditEvent(tenant_id=tenant.id, issuer="internal:worker", subject="plan-worker", action="resource.plan", target_id=resource.id))
        db.commit()
        return True


def main():
    parser = argparse.ArgumentParser(description="Generate plans only; no Kubernetes/database/MinIO credentials required")
    parser.add_argument("--once", action="store_true", help="Process the currently pending queue and exit")
    args = parser.parse_args()
    while True:
        if not plan_one():
            if args.once:
                return
            time.sleep(2)


if __name__ == "__main__":
    main()
