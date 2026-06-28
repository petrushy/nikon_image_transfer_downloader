"""Application configuration.

Settings are resolved from four sources (highest priority first):

  1. Explicit CLI overrides passed to :func:`load_settings`
  2. Environment variables (``NIC_*``)
  3. Persistent JSON config file
     (default: ``~/.nikon_transfer/config.json``)
  4. Credentials YAML file
     (default: ``.env/login.txt``, for username/password)
  5. Built-in defaults

See ``.env.example`` for the full list of environment variable keys.
The ``nikon-transfer config set/get/show`` commands manage the JSON
config file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

RAW_EXTENSIONS: frozenset[str] = frozenset({"nef", "nrw", "raw"})
JPEG_EXTENSIONS: frozenset[str] = frozenset({"jpg", "jpeg"})

VALID_CONFIG_KEYS: frozenset[str] = frozenset(
    {"country", "dest_dir", "poll_interval", "file_filter"}
)

DEFAULT_CONFIG_FILE = Path.home() / ".nikon_transfer" / "config.json"


# ---------------------------------------------------------------------------
# Credentials file (username / password — never stored in the JSON config)
# ---------------------------------------------------------------------------


def _load_credentials_file(
    path: Path,
) -> tuple[str | None, str | None]:
    """Read ``login.username`` / ``login.password`` from a YAML file.

    Returns ``(None, None)`` if the file is missing or unparseable so
    env-only configurations keep working.
    """
    if not path.is_file():
        return None, None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None, None
    login = data.get("login", {}) if isinstance(data, dict) else {}
    if not isinstance(login, dict):
        return None, None
    username = login.get("username")
    password = login.get("password")
    return (
        str(username) if username is not None else None,
        str(password) if password is not None else None,
    )


# ---------------------------------------------------------------------------
# Persistent JSON config file (non-secret settings)
# ---------------------------------------------------------------------------


def load_config_file(path: Path | None = None) -> dict[str, str]:
    """Load the persistent config file; return ``{}`` if missing/corrupt."""
    p = path or DEFAULT_CONFIG_FILE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_config_file(
    data: dict[str, str], path: Path | None = None
) -> None:
    """Persist the config dict to the JSON file, creating parent dirs."""
    p = path or DEFAULT_CONFIG_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def config_file_path(override: Path | None = None) -> Path:
    return override or DEFAULT_CONFIG_FILE


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Resolved runtime configuration."""

    username: str | None
    password: str | None
    country: str
    dest_dir: Path
    file_format: str    # "all" | "raw" | "jpeg"
    poll_interval: int  # seconds between polls in service/sync mode
    headless: bool
    state_dir: Path     # where the captured token + manifest live

    @property
    def token_file(self) -> Path:
        return self.state_dir / "session.json"

    @property
    def manifest_file(self) -> Path:
        return self.state_dir / "manifest.json"

    def wants_extension(self, extension: str) -> bool:
        """Whether an item with this ``file_extension`` should be kept."""
        ext = extension.lower().lstrip(".")
        if self.file_format == "raw":
            return ext in RAW_EXTENSIONS
        if self.file_format == "jpeg":
            return ext in JPEG_EXTENSIONS
        return True  # "all"


def load_settings(
    config_path: Path | None = None,
    *,
    dest_dir: str | None = None,
    file_format: str | None = None,
    poll_interval: int | None = None,
    country: str | None = None,
) -> Settings:
    """Build :class:`Settings` from all sources with optional CLI overrides.

    Priority (highest wins): CLI arg > env var > config file > default.
    """
    cfg = load_config_file(config_path)

    credentials_file = Path(
        os.environ.get("NIC_CREDENTIALS_FILE", ".env/login.txt")
    )
    file_user, file_pass = _load_credentials_file(credentials_file)

    raw_fmt = (
        file_format
        or os.environ.get("NIC_FILE_FORMAT")
        or cfg.get("file_filter")
        or "all"
    )
    fmt = raw_fmt.strip().lower()
    if fmt not in {"all", "raw", "jpeg"}:
        fmt = "all"

    env_interval = os.environ.get("NIC_POLL_INTERVAL")
    cfg_interval = cfg.get("poll_interval")
    resolved_interval: int = (
        poll_interval
        or (int(env_interval) if env_interval else None)
        or (int(cfg_interval) if cfg_interval else None)
        or 300
    )

    return Settings(
        username=os.environ.get("NIC_USERNAME") or file_user,
        password=os.environ.get("NIC_PASSWORD") or file_pass,
        country=(
            country
            or os.environ.get("NIC_COUNTRY")
            or cfg.get("country")
            or "SE"
        ),
        dest_dir=Path(
            dest_dir
            or os.environ.get("NIC_DEST_DIR")
            or cfg.get("dest_dir")
            or "downloads"
        ),
        file_format=fmt,
        poll_interval=resolved_interval,
        headless=_env_bool("NIC_HEADLESS", False),
        state_dir=Path(os.environ.get("NIC_STATE_DIR", ".state")),
    )
