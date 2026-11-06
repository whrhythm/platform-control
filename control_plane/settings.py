from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLATFORM_", extra="ignore")

    database_url: str = "sqlite:///./control-plane.db"
    project_versions_enabled: bool = False
    workspace_projects_limit: int = Field(default=100, ge=1, le=500)
    business_templates_enabled: bool = False
    project_versions_limit: int = Field(default=20, ge=1, le=100)
    code_execution_enabled: bool = False
    code_execution_url: str = 'http://codeapi-api.codeapi.svc.cluster.local:3112/v1/'
    code_execution_public_key: str = ''
    code_execution_transfer_bytes: int = Field(default=16 * 1024 * 1024, ge=1024, le=32 * 1024 * 1024)
    code_execution_timeout_seconds: int = Field(default=90, ge=30, le=120)
    code_execution_lease_seconds: int = Field(default=180, ge=150, le=600)
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_pool_overflow: int = Field(default=10, ge=0, le=20)
    model_settlement_cny_per_usd: str = '7.000000'
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
    xunhupay_simulator_enabled: bool = False
    xunhupay_simulator_url: str = 'http://127.0.0.1:9066'
    xunhupay_notify_url: str = 'http://127.0.0.1:8088/payments/xunhupay/notify'
    xunhupay_appid: str = 'sim-starbridge'
    xunhupay_secret: str = ''
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
    agent_tasks_enabled: bool = False
    agent_mcp_seconds: int = Field(default=60, ge=5, le=120)
    agent_mcp_calls: int = Field(default=8, ge=1, le=20)
    mcp_gateway_url: str = ''
    identity_admin_url: str = ''
    identity_admin_token: str = ''
    mcp_gateway_token: str = ''
    mcp_credential_encryption_key: str = ''
    agent_command_image: str = ''
    agent_platform_url: str = 'https://platform.k3s.local:30443'
    agent_approval_seconds: int = 900
    agent_command_seconds: int = 180
    agent_command_cpu: str = '500m'
    agent_command_memory: str = '512Mi'
    agent_task_seconds: int = 1800
    agent_concurrency: int = 2
    workspace_python_base_image: str = ""
    workspace_pip_index_url: str = ""
    workspace_service_ca: str = "/etc/platform-ca/internal-ca.crt"
    publisher_enabled: bool = False
    buildkit_image: str = ""
    harbor_url: str = "https://harbor.k3s.local"
    harbor_username: str = "codechat-provisioner"
    harbor_password: str = ""
    application_origin: str = "http://192.168.10.30.sslip.io:30097"
    default_application_limit: int = 3
    build_job_retention_seconds: int = 300
    build_scratch_mib: int = 1024
    build_memory_mib: int = 2048
    project_databases_enabled: bool = False
    project_database_provisioner_url: str = ""
    project_database_host: str = "postgres-rw.postgres.svc.cluster.local"
    project_database_ca: str = "/etc/platform-ca/postgres-ca.crt"
    project_database_budget_mib: int = 256
    project_database_reservation_mib: int = 128
    project_database_limit: int = 2
    storage_metering_enabled: bool = False
    object_storage_enabled: bool = False
    object_storage_endpoint: str = "minio.minio.svc.cluster.local:9000"
    object_storage_secure: bool = False
    object_storage_access_key: str = ""
    object_storage_secret_key: str = ""
    object_storage_budget_mib: int = 256
    object_storage_scan_limit: int = 10000
    storage_sample_max_age_seconds: int = 180


@lru_cache
def get_settings() -> Settings:
    return Settings()

