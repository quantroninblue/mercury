"""Paths and settings shared by the relay, the CLI and the agent plugin.

Everything lives under MERCURY_HOME (default ``~/.mercury``), which is also the
engine's HERMES_HOME, so one folder holds config, memory, skills and the device
database. Settings come from the process environment first, then MERCURY_HOME/.env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def mercury_home() -> Path:
    raw = os.environ.get("MERCURY_HOME") or os.environ.get("HERMES_HOME") or "~/.mercury"
    return Path(os.path.expandvars(raw)).expanduser()


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader (comments, blank lines and optional quotes)."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


@dataclass(frozen=True)
class Settings:
    home: Path
    db_path: Path
    relay_host: str
    relay_port: int
    relay_url: str
    admin_token: str
    public_url: str
    api_url: str = ""
    api_key: str = ""

    @classmethod
    def load(cls) -> "Settings":
        home = mercury_home()
        env = {**read_env_file(home / ".env"), **os.environ}
        host = env.get("MERCURY_RELAY_HOST", "127.0.0.1")
        port = int(env.get("MERCURY_RELAY_PORT", "8765"))
        return cls(
            home=home,
            db_path=Path(env.get("MERCURY_DB", str(home / "mercury" / "hub.db"))).expanduser(),
            relay_host=host,
            relay_port=port,
            relay_url=env.get("MERCURY_RELAY_URL", f"http://127.0.0.1:{port}").rstrip("/"),
            admin_token=env.get("MERCURY_ADMIN_TOKEN", ""),
            public_url=env.get("MERCURY_PUBLIC_URL", "").rstrip("/"),
            # The agent's API server, which devices reach only through the relay's /v1/ask.
            api_url=env.get("MERCURY_AGENT_API_URL",
                            f"http://127.0.0.1:{env.get('API_SERVER_PORT', '8642')}").rstrip("/"),
            api_key=env.get("API_SERVER_KEY", ""),
        )
