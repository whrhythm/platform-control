"""Background object provisioning and provider metering, separate from publication."""
from pathlib import Path
import logging
import ssl
import time

import httpx

from .application_controller import Cluster
from .database import SessionLocal
from .object_storage_controller import reconcile_objects
from .object_storage_provider import MinioObjectProvider
from .settings import get_settings
from .storage_metering import reconcile_storage


def main():
    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    account = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    objects = MinioObjectProvider(settings) if settings.object_storage_access_key and settings.object_storage_secret_key else None
    while True:
        with httpx.Client(base_url='https://kubernetes.default.svc/', verify=ssl.create_default_context(cafile=str(account / 'ca.crt')),
            headers={'Authorization': 'Bearer ' + (account / 'token').read_text().strip()}, timeout=10, trust_env=False) as kube, \
             httpx.Client(base_url=settings.harbor_url, verify=ssl.create_default_context(cafile=settings.workspace_service_ca),
                auth=(settings.harbor_username, settings.harbor_password), timeout=10, trust_env=False) as harbor:
            cluster = Cluster(kube)
            if objects:
                reconcile_objects(SessionLocal, cluster, settings, objects)
            reconcile_storage(SessionLocal, cluster, harbor, settings, objects)
        time.sleep(5)


if __name__ == '__main__':
    main()
