"""
config.py
Centralized settings — all env vars flow through here.
"""

from functools import lru_cache
from typing import Literal
from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── LLM ──────────────────────────────────────────────────────────────────
    llm_provider: Literal["anthropic", "openai", "groq"] = "groq"
    llm_model: str = "llama-3.3-70b-versatile"
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    groq_api_key: str = Field(default="", alias="GROQ_API_KEY")

    # ── Splunk ────────────────────────────────────────────────────────────────
    splunk_host: str = "localhost"
    splunk_port: int = 8088
    splunk_username: str = "admin"
    splunk_password: str = ""
    splunk_scheme: Literal["https", "http"] = "https"
    splunk_mcp_url: str = "http://localhost:3000/mcp"
    splunk_index_logs: str = "main"
    splunk_index_metrics: str = "metrics"
    splunk_index_deploys: str = "deployments"
    splunk_max_results: int = 50

    # ── App ───────────────────────────────────────────────────────────────────
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_env: Literal["development", "production"] = "development"
    log_level: str = "INFO"
    agent_timeout_seconds: int = 120

    # ── Computed ──────────────────────────────────────────────────────────────
    @computed_field
    @property
    def splunk_base_url(self) -> str:
        return f"{self.splunk_scheme}://{self.splunk_host}:{self.splunk_port}"

    @computed_field
    @property
    def is_dev(self) -> bool:
        return self.app_env == "development"

    def get_llm_api_key(self) -> str:
        if self.llm_provider == "anthropic":
            return self.anthropic_api_key
        if self.llm_provider == "groq":
            return self.groq_api_key
        return self.openai_api_key


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()