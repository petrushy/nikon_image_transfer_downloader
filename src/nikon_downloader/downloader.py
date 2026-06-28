"""Downloads image files into a YYYY/MM/DD directory layout, with resume.

A small JSON manifest records which item ids have been downloaded (plus
size and path) so re-runs are idempotent and interrupted runs resume
safely.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from .auth import DEFAULT_USER_AGENT
from .models import ImageItem

log = logging.getLogger(__name__)


class Manifest:
    """Tracks downloaded items in a JSON file keyed by item id."""

    def __init__(self, path: Path):
        self.path = path
        self._entries: dict[str, dict] = {}
        if path.is_file():
            try:
                self._entries = json.loads(
                    path.read_text(encoding="utf-8")
                )
            except (json.JSONDecodeError, ValueError):
                log.warning(
                    "Ignoring unreadable manifest at %s", path
                )

    def has(self, item: ImageItem) -> bool:
        """Whether this item is already recorded and the file is on disk."""
        entry = self._entries.get(item.id)
        if not entry:
            return False
        # Always verify the file still exists — manifest entries survive
        # deletes and renames, so a missing file must be re-downloaded.
        recorded_path = entry.get("path")
        if recorded_path and not Path(recorded_path).exists():
            return False
        if item.original_file_size is None:
            # Size unavailable from the API; trust the manifest + presence
            # check above rather than downloading unconditionally.
            return True
        return entry.get("size") == item.original_file_size

    def record(self, item: ImageItem, path: Path) -> None:
        self._entries[item.id] = {
            "name": item.name,
            "size": item.original_file_size,
            "path": str(path),
            "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._entries, indent=2), encoding="utf-8"
        )


@dataclass
class DownloadResult:
    item: ImageItem
    status: str  # "downloaded" | "skipped" | "failed"
    path: Path | None = None
    error: str | None = None


def build_target_path(item: ImageItem, dest_dir: Path) -> Path:
    """Return the local path where ``item`` would be (or is) stored."""
    date = item.effective_date
    sub = date.strftime("%Y/%m/%d") if date else "unknown-date"
    name = item.name or f"{item.id}.{item.file_extension or 'bin'}"
    return dest_dir / sub / name


class Downloader:
    """Downloads items via their presigned ``original_file_url``."""

    def __init__(
        self, dest_dir: Path, manifest: Manifest, http: httpx.AsyncClient
    ):
        self.dest_dir = dest_dir
        self.manifest = manifest
        self._http = http

    def target_path(self, item: ImageItem) -> Path:
        return build_target_path(item, self.dest_dir)

    async def download(
        self, item: ImageItem, retries: int = 3
    ) -> DownloadResult:
        """Download one item, skipping if already present.

        Retries up to ``retries`` times on transient errors using
        exponential backoff (1 s, 2 s, 4 s …).
        """
        if self.manifest.has(item):
            log.debug("Skip %s (recorded in manifest)", item.name)
            return DownloadResult(item, "skipped")
        if not item.original_file_url:
            return DownloadResult(item, "failed", error="no download URL")

        path = self.target_path(item)
        # A previous run may have written the file before recording it.
        if path.exists() and (
            item.original_file_size is None
            or path.stat().st_size == item.original_file_size
        ):
            log.debug("Skip %s (already on disk at %s)", item.name, path)
            self.manifest.record(item, path)
            return DownloadResult(item, "skipped", path=path)

        last_error = "unknown error"
        for attempt in range(max(1, retries)):
            if attempt > 0:
                await asyncio.sleep(2.0 ** (attempt - 1))
            try:
                await self._stream_to_file(item.original_file_url, path)
                self.manifest.record(item, path)
                return DownloadResult(item, "downloaded", path=path)
            except (httpx.HTTPError, OSError) as exc:
                last_error = str(exc)
                log.debug(
                    "Attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    retries,
                    item.name,
                    exc,
                )

        return DownloadResult(item, "failed", error=last_error)

    async def _stream_to_file(self, url: str, path: Path) -> None:
        """Stream a URL to disk via a ``.part`` temp file, then rename."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        async with self._http.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes(chunk_size=1 << 16):
                    fh.write(chunk)
        tmp.replace(path)
