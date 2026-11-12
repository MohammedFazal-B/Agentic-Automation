"""Runtime configuration. Everything overridable by environment variable."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    # "postgresql://user:pass@host:5432/itsar"  |  "sqlite:./itsar.db"
    db_url: str = field(default_factory=lambda: _env("ITSAR_DB_URL", "sqlite:./itsar.db"))
    evidence_dir: Path = field(default_factory=lambda: Path(_env("ITSAR_EVIDENCE_DIR", "./evidence")))
    out_dir: Path = field(default_factory=lambda: Path(_env("ITSAR_OUT_DIR", "./out")))

    # Browser policy (directive 3): Chrome is mandatory; Firefox is installed on the fly
    # only when a tool strictly requires it.
    chrome_path: str = field(default_factory=lambda: _env("ITSAR_CHROME", "/usr/bin/google-chrome"))
    firefox_path: str = field(default_factory=lambda: _env("ITSAR_FIREFOX", "/usr/bin/firefox"))
    allow_firefox_fallback: bool = field(
        default_factory=lambda: _env("ITSAR_ALLOW_FIREFOX", "1") == "1"
    )

    # Self-healing limits. Bounded on purpose: an unbounded heal loop is an
    # unbounded write loop against a production device.
    max_heal_attempts: int = field(default_factory=lambda: int(_env("ITSAR_MAX_HEAL", "3")))
    dry_run: bool = field(default_factory=lambda: _env("ITSAR_DRY_RUN", "0") == "1")

    # Credentials live outside the DB. The DB stores only credential_ref.
    credential_file: Path = field(
        default_factory=lambda: Path(_env("ITSAR_CREDENTIALS", "./credentials.json"))
    )

    # Optional LLM diagnosis (OpenAI/Anthropic-compatible endpoint). When unset,
    # the healer falls back to deterministic strategies -- see itsar/llm.py.
    llm_api_key: str = field(default_factory=lambda: _env("ITSAR_LLM_API_KEY", ""))
    llm_base_url: str = field(
        default_factory=lambda: _env("ITSAR_LLM_BASE_URL", "https://api.anthropic.com/v1/messages")
    )
    llm_model: str = field(default_factory=lambda: _env("ITSAR_LLM_MODEL", "claude-sonnet-4-6"))

    def ensure_dirs(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)
