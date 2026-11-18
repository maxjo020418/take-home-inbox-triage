"""Saved connection profile: backend URL + last username. Never the password or
session token — those live in memory only.

Location: $XDG_CONFIG_HOME/inbox-triage/config.toml (Linux/macOS, default
~/.config) or %APPDATA%\\inbox-triage\\config.toml (Windows).
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

import tomli_w

DEFAULT_URL = "http://127.0.0.1:8000"


def config_path() -> Path:
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "inbox-triage" / "config.toml"


@dataclass
class Profile:
    backend_url: str = DEFAULT_URL
    username: str = ""
    refresh_s: float = 1.5
    detail_width: int = 0  # columns; 0 = default (45% of the screen)
    log_height: int = 7  # rows

    @classmethod
    def load(cls, path: Path | None = None) -> Profile:
        path = path or config_path()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, tomllib.TOMLDecodeError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tomli_w.dumps(asdict(self)), encoding="utf-8")
        return path
