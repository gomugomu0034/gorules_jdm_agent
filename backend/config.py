"""Application configuration.

Values come from the environment (or ``backend/.env``); every setting has a
sane default so the app boots with no configuration at all.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repository root, resolved from this file rather than the process cwd.
# The agent used to build paths like "backend/jdm_graphs" relative to cwd, which
# silently broke depending on where uvicorn was started from.
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = Path(__file__).resolve().parent


def repo_path(*parts: str) -> Path:
    """Absolute path inside the repository, independent of the current cwd."""
    return REPO_ROOT.joinpath(*parts)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "GoRules JDM Studio"
    version: str = "1.0.0"

    # Storage
    db_path: str = str(REPO_ROOT / "data" / "studio.db")
    legacy_graphs_dir: str = str(BACKEND_ROOT / "jdm_graphs")
    legacy_tests_dir: str = str(BACKEND_ROOT / "jdm_tests")
    debug_dir: str = ""  # empty -> tempfile.gettempdir()

    # HTTP
    cors_origins: str = "http://localhost:3000,http://127.0.0.1:3000"
    request_timeout: int = 30

    # Agent
    agent_run_timeout: int = 900  # hard wall-clock budget for one agent run
    llm_timeout: int = 120

    # Rate limits (slowapi syntax)
    rate_limit_default: str = "120/minute"
    rate_limit_llm: str = "10/minute"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def db_file(self) -> Path:
        return Path(self.db_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
