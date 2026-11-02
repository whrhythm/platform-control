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
    require_project_entitlement: bool = False
    litellm_url: str = ""
    litellm_master_key: str = ""
    litellm_models: str = ""
    model_credential_encryption_key: str = ""
    librechat_service_token: str = ""
    workspace_files_enabled: bool = False
    workspace_service_image: str = ""
    workspace_agent_image: str = ""
    workspace_agent_max_iterations: int = 40
    workspace_service_ca: str = "/etc/platform-ca/internal-ca.crt"


@lru_cache
def get_settings() -> Settings:
    return Settings()

