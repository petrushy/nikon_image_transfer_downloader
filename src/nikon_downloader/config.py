"""Application configuration.

Settings come from environment variables, with a YAML credentials file as a
fallback for the username/password (handy for local testing). Environment
variables always win over the file.

See ``.env.example`` for the full list of keys.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

# File formats we know how to recognise from an item's file extension.
RAW_EXTENSIONS = {"nef", "nrw", "raw"}
JPEG_EXTENSIONS = {"jpg", "jpeg"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_credentials_file(path: Path) -> tuple[str | None, str | None]:
    """Read ``login.username`` / ``login.password`` from a YAML file.

    Returns ``(None, None)`` if the file is missing or unparseable rather than
    raising, so env-only configurations keep working.
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


@dataclass
class Settings:
    """Resolved runtime configuration."""

    username: str | None
    password: str | None
    dest_dir: Path
    file_format: str  # "all" | "raw" | "jpeg"
    poll_interval: int  # seconds between polls in service mode
    headless: bool
    state_dir: Path  # where the captured token + manifest live

    @property
    def token_file(self) -> Path:
        return self.state_dir / "session.json"

    @property
    def manifest_file(self) -> Path:
        return self.state_dir / "manifest.json"

    def wants_extension(self, extension: str) -> bool:
        """Whether an item with this file extension should be downloaded."""
        ext = extension.lower().lstrip(".")
        if self.file_format == "raw":
            return ext in RAW_EXTENSIONS
        if self.file_format == "jpeg":
            return ext in JPEG_EXTENSIONS
        return True  # "all"


def load_settings() -> Settings:
    """Build :class:`Settings` from the environment and credentials file."""
    credentials_file = Path(
        os.environ.get("NIC_CREDENTIALS_FILE", ".env/login.txt")
    )
    file_user, file_pass = _load_credentials_file(credentials_file)

    file_format = os.environ.get("NIC_FILE_FORMAT", "all").strip().lower()
    if file_format not in {"all", "raw", "jpeg"}:
        file_format = "all"

    state_dir = Path(os.environ.get("NIC_STATE_DIR", ".state"))

    return Settings(
        username=os.environ.get("NIC_USERNAME") or file_user,
        password=os.environ.get("NIC_PASSWORD") or file_pass,
        dest_dir=Path(os.environ.get("NIC_DEST_DIR", "downloads")),
        file_format=file_format,
        poll_interval=int(os.environ.get("NIC_POLL_INTERVAL", "900")),
        headless=_env_bool("NIC_HEADLESS", False),
        state_dir=state_dir,
    )
