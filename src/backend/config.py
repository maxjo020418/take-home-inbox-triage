"""Backend settings from the environment (.env is loaded for local runs).

Deliberately NOT exposed here: the write token. It is read from the environment
only at the moment a human-approved action executes (see `write_token()`), so
the poller, workers and request handlers never hold it otherwise.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv(".env")
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    read_token: str
    database_url: str
    openrouter_api_key: str
    openrouter_model: str
    poll_interval_s: float
    poll_overlap_s: float
    workers: int
    max_attempts: int
    llm_timeout_s: float
    session_ttl_h: float
    bootstrap_user: str | None
    bootstrap_password: str | None


def _req(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required env var {name} (see env.example)")
    return value


def load_settings() -> Settings:
    return Settings(
        api_base_url=os.environ.get("API_BASE_URL", "http://127.0.0.1:8099"),
        read_token=_req("READ_TOKEN"),
        database_url=os.environ.get("DATABASE_URL", "postgresql://triage:triage@127.0.0.1:5432/triage"),
        openrouter_api_key=_req("OPENROUTER_API_KEY"),
        openrouter_model=_req("OPENROUTER_MODEL"),
        poll_interval_s=float(os.environ.get("POLL_INTERVAL_S", "5")),
        poll_overlap_s=float(os.environ.get("POLL_OVERLAP_S", "120")),
        workers=int(os.environ.get("WORKERS", "3")),
        max_attempts=int(os.environ.get("MAX_ATTEMPTS", "3")),
        llm_timeout_s=float(os.environ.get("LLM_TIMEOUT_S", "60")),
        session_ttl_h=float(os.environ.get("SESSION_TTL_H", "12")),
        bootstrap_user=os.environ.get("BOOTSTRAP_ADMIN_USER") or None,
        bootstrap_password=os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or None,
    )


def write_token() -> str:
    """Fetch the write-scope token. Call ONLY on the human-approved execute path."""
    return _req("WRITE_TOKEN")
