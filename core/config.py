"""Configuration loading.

Precedence (later wins): config/settings.yaml -> config/settings.local.yaml -> environment (.env).
Settings are exposed as nested attribute-access dicts: ``settings().deal.mao_pct_conservative``.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
CACHE_DIR = DATA_DIR / "cache"
DEFAULT_DB_PATH = ROOT / "wholesaling.db"
# Personal overrides and secrets. The environment variables point a scratch copy (tests, demos) elsewhere.
LOCAL_SETTINGS = Path(os.environ.get("WHOLESALING_LOCAL_SETTINGS") or CONFIG_DIR / "settings.local.yaml")
ENV_FILE = Path(os.environ.get("WHOLESALING_ENV_FILE") or ROOT / ".env")


class Section(dict):
    """dict with attribute access, recursively applied."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    @classmethod
    def wrap(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return cls({k: cls.wrap(v) for k, v in value.items()})
        if isinstance(value, list):
            return [cls.wrap(v) for v in value]
        return value


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Minimal .env loader. Variables already set in the environment win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_settings(config_dir: Path = CONFIG_DIR) -> Section:
    load_dotenv()
    data: dict = {}
    local = LOCAL_SETTINGS if config_dir == CONFIG_DIR else config_dir / "settings.local.yaml"
    for path in (config_dir / "settings.yaml", local):
        if path.exists():
            data = _deep_merge(data, yaml.safe_load(path.read_text()) or {})
    return Section.wrap(data)


@lru_cache(maxsize=1)
def settings() -> Section:
    return load_settings()


def reload_settings() -> Section:
    settings.cache_clear()
    return settings()


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def database_url() -> str:
    return env("WHOLESALING_DATABASE_URL") or f"sqlite:///{DEFAULT_DB_PATH}"


def user_agent() -> str:
    s = settings()
    contact = (s.get("scrapers", {}).get("contact_email") or s.get("operator", {}).get("email")
               or "contact-via-repo")
    return f"NJWholesaleResearch/0.1 (public-records research; {contact})"
