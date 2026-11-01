"""Pure, deterministic deployment plans. No network access or secret material."""
from .models import Resource, Tenant
from .settings import Settings


def namespace(tenant_id: str) -> str:
    return "tn-" + tenant_id.replace("-", "")


def plan(resource: Resource, tenant: Tenant, settings: Settings) -> dict:
    ns = namespace(tenant.id)
    name = resource.kind + "-" + resource.id.replace("-", "")
    labels = {"platform.example/tenant-id": tenant.id, "platform.example/project-id": resource.project_id, "platform.example/resource-id": resource.id, "app.kubernetes.io/managed-by": "platform-control"}
    result = {"mode": "plan", "executable": False, "namespace": ns, "resource_id": resource.id, "kubernetes": [], "blockers": ["Apply adapter and runtime readiness checks are not enabled in this release."]}
    objects = result["kubernetes"]
    objects.append({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {"platform.example/tenant-id": tenant.id, "pod-security.kubernetes.io/enforce": "restricted", "pod-security.kubernetes.io/enforce-version": "v1.36"}}})
    objects.append({"apiVersion": "v1", "kind": "ResourceQuota", "metadata": {"name": "tenant-budget", "namespace": ns}, "spec": {"hard": {"requests.cpu": f"{tenant.cpu_millicores}m", "limits.cpu": f"{tenant.cpu_millicores}m", "requests.memory": f"{tenant.memory_mib}Mi", "limits.memory": f"{tenant.memory_mib}Mi", "requests.storage": f"{tenant.storage_gib}Gi", "pods": str(tenant.resource_limit), "services.nodeports": "0", "services.loadbalancers": "0"}}})
    objects.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "default-deny", "namespace": ns}, "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": []}})
    objects.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": {"name": "allow-dns", "namespace": ns}, "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": [{"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}], "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]}]}})
    spec = resource.spec
    security = {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}}
    container_security = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}
    if resource.kind in {"workspace", "deployment"}:
        resources = {key: {"cpu": f"{resource.cpu_millicores}m", "memory": f"{resource.memory_mib}Mi"} for key in ("requests", "limits")}
        pod = {"metadata": {"labels": labels}, "spec": {"runtimeClassName": settings.runtime_class, "automountServiceAccountToken": False, "securityContext": security, "containers": [{"name": "app", "image": settings.workspace_image if resource.kind == "workspace" else spec["image"], "securityContext": container_security, "resources": resources}]}}
        container = pod["spec"]["containers"][0]
        if resource.kind == "workspace":
            objects.append({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": name, "namespace": ns, "labels": labels}, "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": settings.storage_class, "resources": {"requests": {"storage": f"{resource.storage_gib}Gi"}}}})
            # Workspace base image is the same tool environment as OpenHands;
            # do not start its shared UI or claim an Agent is running yet.
            container["command"] = ["/bin/sh", "-c", "exec sleep infinity"]
            container["volumeMounts"] = [{"name": "workspace", "mountPath": "/home/openhands"}]
            pod["spec"]["volumes"] = [{"name": "workspace", "persistentVolumeClaim": {"claimName": name}}]
        else:
            container["ports"] = [{"name": "http", "containerPort": spec["port"]}]
            container["readinessProbe"] = {"tcpSocket": {"port": "http"}, "periodSeconds": 5}
            objects.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns, "labels": labels}, "spec": {"type": "ClusterIP", "selector": {"platform.example/resource-id": resource.id}, "ports": [{"port": spec["port"], "targetPort": "http"}]}})
            result["proposed_hostname"] = f"app-{resource.id.replace('-', '')}.{settings.deployment_domain}" if settings.deployment_domain else None
            result["blockers"].append("APISIX route, TLS, DNS and deployment-specific access policy require integration; no public route is emitted.")
        objects.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns, "labels": labels}, "spec": {"replicas": 1, "strategy": {"type": "Recreate"}, "selector": {"matchLabels": {"platform.example/resource-id": resource.id}}, "template": pod}})
    elif resource.kind == "database":
        identifier = "p_" + resource.project_id.replace("-", "") + "_" + spec["environment"] + "_" + resource.id.replace("-", "")[:8]
        result["database"] = {"database": identifier, "role": identifier, "required_controls": ["Generate password out of band; never log or return it in this plan", "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION", "REVOKE ALL ON DATABASE from PUBLIC; grant CONNECT only to this project role", "Revoke PUBLIC CREATE on public schema and restrict shared-cluster pg_hba", "Separate development and production credentials", "Cap connections and statement duration; monitor pg_database_size (not a hard per-database disk quota)"]}
        result["blockers"].append("PostgreSQL provisioner, credential delivery and backup acceptance are pending.")
    elif resource.kind == "bucket":
        bucket = "p-" + resource.id.replace("-", "")
        result["bucket"] = {"name": bucket, "quota_gib": resource.storage_gib, "policy": {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": ["s3:ListBucket", "s3:GetBucketLocation"], "Resource": [f"arn:aws:s3:::{bucket}"]}, {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"], "Resource": [f"arn:aws:s3:::{bucket}/*"]}]}}
        result["blockers"].append("MinIO policy, credentials, bucket quota and cross-bucket negative tests are pending.")
    elif resource.kind == "agent":
        result["agent"] = {"provider": "openhands", "workspace_id": spec["workspace_id"], "exclusive_workspace": True, "shared_integration_instance": False}
        result["blockers"].append("Project-scoped OpenHands adapter, access proxy and metered LLM gateway are pending.")
    return result
