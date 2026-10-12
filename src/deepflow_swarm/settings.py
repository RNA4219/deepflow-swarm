from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = "postgresql+asyncpg://swarm:swarm@localhost:5433/swarm"

    deerflow_langgraph_url: str = "http://localhost:2026/api/langgraph"
    deerflow_api_token: SecretStr | None = None
    deerflow_planner_model: str = "planner"
    deerflow_worker_model: str = "worker"
    deerflow_reviewer_model: str = "reviewer"
    deerflow_local_private_model: str = "local_private"

    open_sandbox_domain: str = "localhost:8080"
    open_sandbox_protocol: str = "http"
    open_sandbox_api_key: SecretStr | None = None
    open_sandbox_worker_image: str = "deepflow-swarm/opencode-worker:dev"
    open_sandbox_timeout_seconds: int = 7200
    open_sandbox_egress_domains: str = ""

    litellm_base_url: str = "http://host.docker.internal:4000/v1"
    litellm_api_key: SecretStr = SecretStr("sk-swarm-local")
    litellm_worker_model: str = "worker"

    swarm_max_concurrency: int = Field(default=3, ge=1, le=100)
    swarm_max_tasks: int = Field(default=12, ge=1, le=12)
    swarm_max_replans: int = Field(default=2, ge=0, le=10)
    swarm_task_timeout_seconds: int = Field(default=3600, ge=1)

    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_base_url: str = "http://localhost:3000"

    @property
    def langfuse_enabled(self) -> bool:
        return bool(
            self.langfuse_public_key
            and self.langfuse_public_key.get_secret_value().strip()
            and self.langfuse_secret_key
            and self.langfuse_secret_key.get_secret_value().strip()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
