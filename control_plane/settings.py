from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_", extra="ignore")

    database_url: str = "sqlite:///./control-plane.db"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    default_project_limit: int = 3
    default_cpu_millicores: int = 4000
    default_memory_mib: int = 8192
    default_storage_gib: int = 40
    default_resource_limit: int = 20
    default_environment_storage_gib: int = 2
    oidc_required_scope: str = "platform-api"
    oidc_client_id: str = ""
    admin_group: str = "platform-admins"
    public_origin: str = "http://localhost:5173"
    additional_public_origin: str = ""
    static_dir: str = ""
    deployment_domain: str = ""
    approved_registry: str = "harbor.k3s.local/internal/"
    workspace_image: str = "ghcr.nju.edu.cn/openhands/agent-canvas:1.34.0"
    runtime_class: str = "kata-qemu-runtime-rs"
    storage_class: str = "longhorn"
    test_payments_enabled: bool = False
    capacity_upgrades_enabled: bool = False
    capacity_resize_timeout_seconds: int = 900
    capacity_filesystem_overhead_percent: int = 10
    capacity_plans_json: str = '[{"id":"basic","version":"test-v1","name":"Basic","storage_gib":4,"application_limit":5,"amount_minor":1000,"currency":"CNY"},{"id":"pro","version":"test-v1","name":"Pro","storage_gib":8,"application_limit":10,"amount_minor":2000,"currency":"CNY"}]'
    require_project_entitlement: bool = False
    litellm_url: str = ""
    litellm_master_key: str = ""
    litellm_models: str = ""
    capabilities_enabled: bool = False
    capability_chat_route: str = "codechat-chat"
    capability_coding_route: str = "codechat-coding"
    capability_chat_model: str = ""
    model_credential_encryption_key: str = ""
    librechat_service_token: str = ""
    workspace_files_enabled: bool = False
    workspace_service_image: str = ""
    workspace_agent_image: str = ""
    workspace_agent_max_iterations: int = 40
    workspace_agent_max_seconds: int = 180
    workspace_python_base_image: str = ""
    workspace_service_ca: str = "/etc/platform-ca/internal-ca.crt"
    publisher_enabled: bool = False
    buildkit_image: str = ""
    harbor_url: str = "https://harbor.k3s.local"
    harbor_username: str = "codechat-provisioner"
    harbor_password: str = ""
    application_origin: str = "http://192.168.10.30.sslip.io:30097"
    default_application_limit: int = 3
    build_job_retention_seconds: int = 300
    project_databases_enabled: bool = False
    project_database_provisioner_url: str = ""
    project_database_host: str = "postgres-rw.postgres.svc.cluster.local"
    project_database_ca: str = "/etc/platform-ca/postgres-ca.crt"
    project_database_budget_mib: int = 256
    project_database_reservation_mib: int = 128
    project_database_limit: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()

