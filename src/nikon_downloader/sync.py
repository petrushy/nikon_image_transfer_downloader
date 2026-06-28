"""Sync orchestration: session handling, filtering, one-shot sync, service.

This module is the headless "engine". It has no UI dependency, so it can
run as a service/daemon or be driven by the NiceGUI front-end or the CLI.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

import httpx

from .api import NikonCloudClient, Unauthorized
from .auth import (
    AuthError,
    BrowserAuthenticator,
    CapturedSession,
    RefreshUnavailable,
    TokenStore,
)
from .config import JPEG_EXTENSIONS, RAW_EXTENSIONS, Settings
from .downloader import Downloader, DownloadResult, Manifest
from .models import ImageItem

log = logging.getLogger(__name__)

LogCallback = Callable[[str], None]
ProgressCallback = Callable[["SyncStats"], None]


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


@dataclass
class SyncFilter:
    """Criteria that decide which cloud images to process.

    All fields are optional; an unset field matches everything.
    """

    file_type: str = "all"       # "all" | "raw" | "jpeg"
    camera: str | None = None    # substring match against device_name
    date_from: date | None = None
    date_to: date | None = None

    def accepts(self, item: ImageItem) -> bool:
        ext = item.file_extension.lower()
        if self.file_type == "raw" and ext not in RAW_EXTENSIONS:
            return False
        if self.file_type == "jpeg" and ext not in JPEG_EXTENSIONS:
            return False
        if self.camera and self.camera.lower() not in (
            item.device_name.lower()
        ):
            return False
        d = item.effective_date
        if d:
            item_date = d.date()
            if self.date_from and item_date < self.date_from:
                return False
            if self.date_to and item_date > self.date_to:
                return False
        return True


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@dataclass
class SyncStats:
    total: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def record(self, result: DownloadResult) -> None:
        if result.status == "downloaded":
            self.downloaded += 1
        elif result.status == "skipped":
            self.skipped += 1
        else:
            self.failed += 1
            if result.error:
                self.errors.append(f"{result.item.name}: {result.error}")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

# Default concurrent file downloads (can be overridden per-call).
_DEFAULT_CONCURRENCY = 3


class SyncEngine:
    """Coordinates authentication, listing, filtering and downloading."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = TokenStore(settings.token_file)
        self.authenticator = BrowserAuthenticator(
            headless=settings.headless
        )
        self._session: CapturedSession | None = None

    # -- session management -------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        return (self._session or self.store.load()) is not None

    async def login(self) -> CapturedSession:
        """Force a fresh interactive browser login and persist it."""
        session = await self.authenticator.authenticate(
            self.settings.username, self.settings.password
        )
        self.store.save(session)
        self._session = session
        return session

    async def ensure_session(
        self, interactive: bool = True
    ) -> CapturedSession:
        """Return a usable session, refreshing or logging in as needed."""
        session = self._session or self.store.load()

        if session and not session.looks_expired():
            self._session = session
            return session

        if session and session.looks_expired():
            try:
                session = await self.authenticator.refresh(session)
                self.store.save(session)
                self._session = session
                log.info("Refreshed access token")
                return session
            except RefreshUnavailable as exc:
                log.info(
                    "Cannot refresh (%s); need a browser login", exc
                )

        if not interactive:
            raise AuthError(
                "No valid session and interactive login is disabled."
            )
        return await self.login()

    async def _reauthenticate(self) -> CapturedSession:
        """Recover from a rejected token: refresh if possible, else login."""
        session = self._session
        if session:
            try:
                session = await self.authenticator.refresh(session)
                self.store.save(session)
                self._session = session
                return session
            except RefreshUnavailable:
                pass
        return await self.login()

    # -- listing ------------------------------------------------------------

    async def list_preview(self, limit: int = 24) -> list[ImageItem]:
        """Fetch a single page of images (for a UI preview grid)."""
        session = await self.ensure_session()
        async with httpx.AsyncClient(timeout=60) as http:
            client = NikonCloudClient(session, http)
            try:
                return await client.list_images(offset=1, limit=limit)
            except Unauthorized:
                session = await self._reauthenticate()
                client.update_session(session)
                return await client.list_images(offset=1, limit=limit)

    async def list_all(
        self, filt: SyncFilter | None = None
    ) -> list[ImageItem]:
        """Return all cloud images, optionally filtered."""
        session = await self.ensure_session()
        async with httpx.AsyncClient(timeout=60) as http:
            client = NikonCloudClient(session, http)
            try:
                items = [
                    item async for item in client.iter_all_images()
                ]
            except Unauthorized:
                session = await self._reauthenticate()
                client.update_session(session)
                items = [
                    item async for item in client.iter_all_images()
                ]
        if filt is not None:
            items = [i for i in items if filt.accepts(i)]
        return items

    # -- syncing ------------------------------------------------------------

    async def sync_once(
        self,
        on_log: LogCallback | None = None,
        on_progress: ProgressCallback | None = None,
        max_items: int | None = None,
        filt: SyncFilter | None = None,
        items_override: list[ImageItem] | None = None,
        concurrency: int = _DEFAULT_CONCURRENCY,
        retries: int = 3,
    ) -> SyncStats:
        """List, filter, and download anything new. Returns stats.

        ``items_override`` skips listing entirely and downloads exactly
        those items (caller is responsible for pre-filtering).
        ``max_items`` caps downloads when no override is given.
        """

        def emit(msg: str) -> None:
            log.info(msg)
            if on_log:
                on_log(msg)

        session = await self.ensure_session()
        manifest = Manifest(self.settings.manifest_file)
        stats = SyncStats()

        timeout = httpx.Timeout(60.0, read=300.0)
        async with httpx.AsyncClient(timeout=timeout) as http:
            client = NikonCloudClient(session, http)

            if items_override is not None:
                wanted = items_override
            else:
                emit("Listing images from Nikon Imaging Cloud...")
                items = await self._collect_items(client, emit)

                if filt is not None:
                    wanted = [i for i in items if filt.accepts(i)]
                else:
                    wanted = [
                        i
                        for i in items
                        if self.settings.wants_extension(i.file_extension)
                    ]
                if max_items is not None:
                    wanted = wanted[:max_items]

                filter_desc = (
                    filt.file_type if filt else self.settings.file_format
                )
                emit(
                    f"Found {len(items)} images, {len(wanted)} match "
                    f"filter '{filter_desc}'."
                )

            stats.total = len(wanted)
            if on_progress:
                on_progress(stats)

            downloader = Downloader(
                self.settings.dest_dir, manifest, http
            )
            await self._download_all(
                downloader, wanted, stats, emit, on_progress, concurrency,
                retries,
            )
            manifest.save()

        skip_note = " (already on disk)" if stats.skipped > 0 else ""
        emit(
            f"Done: {stats.downloaded} downloaded, "
            f"{stats.skipped} skipped{skip_note}, {stats.failed} failed."
        )
        return stats

    async def _collect_items(
        self, client: NikonCloudClient, emit: LogCallback
    ) -> list[ImageItem]:
        """Enumerate all items, re-authenticating once on a 401/403."""
        try:
            return [item async for item in client.iter_all_images()]
        except Unauthorized:
            emit("Access token rejected; re-authenticating...")
            session = await self._reauthenticate()
            client.update_session(session)
            return [item async for item in client.iter_all_images()]

    async def _download_all(
        self,
        downloader: Downloader,
        items: list[ImageItem],
        stats: SyncStats,
        emit: LogCallback,
        on_progress: ProgressCallback | None,
        concurrency: int,
        retries: int,
    ) -> None:
        semaphore = asyncio.Semaphore(concurrency)

        async def worker(item: ImageItem) -> None:
            async with semaphore:
                result = await downloader.download(item, retries=retries)
            stats.record(result)
            if result.status == "downloaded":
                emit(f"  ↓ {result.path}")
            elif result.status == "failed":
                emit(f"  ✗ {item.name}: {result.error}")
            if on_progress:
                on_progress(stats)

        await asyncio.gather(*(worker(item) for item in items))

    # -- service mode -------------------------------------------------------

    async def run_service(
        self,
        on_log: LogCallback | None = None,
        on_progress: ProgressCallback | None = None,
        stop_event: asyncio.Event | None = None,
        filt: SyncFilter | None = None,
    ) -> None:
        """Poll the cloud until ``stop_event``, syncing each cycle."""

        def emit(msg: str) -> None:
            log.info(msg)
            if on_log:
                on_log(msg)

        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            try:
                await self.sync_once(
                    on_log=on_log, on_progress=on_progress, filt=filt
                )
            except Exception as exc:  # noqa: BLE001
                emit(f"Sync cycle failed: {exc}")
                log.exception("Sync cycle failed")

            emit(
                f"Sleeping {self.settings.poll_interval}s until next poll."
            )
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.settings.poll_interval
                )
            except asyncio.TimeoutError:
                pass  # interval elapsed → next cycle
