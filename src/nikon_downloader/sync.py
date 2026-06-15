"""Sync orchestration: session handling, one-shot sync, and the poll service.

This module is the headless "engine". It has no UI dependency, so it can run as
a service/daemon or be driven by the NiceGUI front-end.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from .api import NikonCloudClient, Unauthorized
from .auth import (
    AuthError,
    BrowserAuthenticator,
    CapturedSession,
    RefreshUnavailable,
    TokenStore,
)
from .config import Settings
from .downloader import Downloader, DownloadResult, Manifest
from .models import ImageItem

log = logging.getLogger(__name__)

# Max concurrent file downloads.
DOWNLOAD_CONCURRENCY = 4

LogCallback = Callable[[str], None]
ProgressCallback = Callable[["SyncStats"], None]


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


class SyncEngine:
    """Coordinates authentication, listing, filtering and downloading."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = TokenStore(settings.token_file)
        self.authenticator = BrowserAuthenticator(headless=settings.headless)
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
                log.info("Cannot refresh (%s); need a browser login", exc)

        if not interactive:
            raise AuthError(
                "No valid session and interactive login is disabled."
            )
        return await self.login()

    async def _reauthenticate(self) -> CapturedSession:
        """Recover from a rejected token: refresh if possible, else log in."""
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

    # -- listing / preview --------------------------------------------------

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

    # -- syncing ------------------------------------------------------------

    async def sync_once(
        self,
        on_log: LogCallback | None = None,
        on_progress: ProgressCallback | None = None,
        max_items: int | None = None,
    ) -> SyncStats:
        """List the cloud, filter, and download anything new. Returns stats.

        ``max_items`` caps how many matching images are downloaded (useful for
        a quick test); ``None`` means all of them.
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

            emit("Listing images from Nikon Imaging Cloud...")
            items = await self._collect_items(client, emit)

            wanted = [
                i
                for i in items
                if self.settings.wants_extension(i.file_extension)
            ]
            if max_items is not None:
                wanted = wanted[:max_items]
            stats.total = len(wanted)
            emit(
                f"Found {len(items)} images, {len(wanted)} match filter "
                f"'{self.settings.file_format}'."
            )
            if on_progress:
                on_progress(stats)

            downloader = Downloader(self.settings.dest_dir, manifest, http)
            await self._download_all(
                downloader, wanted, stats, emit, on_progress
            )
            manifest.save()

        emit(
            f"Done: {stats.downloaded} downloaded, {stats.skipped} skipped, "
            f"{stats.failed} failed."
        )
        return stats

    async def _collect_items(
        self, client: NikonCloudClient, emit: LogCallback
    ) -> list[ImageItem]:
        """Enumerate all items, re-authenticating once on a rejected token."""
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
    ) -> None:
        semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

        async def worker(item: ImageItem) -> None:
            async with semaphore:
                result = await downloader.download(item)
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
    ) -> None:
        """Poll the cloud until ``stop_event``, syncing each cycle."""

        def emit(msg: str) -> None:
            log.info(msg)
            if on_log:
                on_log(msg)

        stop = stop_event or asyncio.Event()
        while not stop.is_set():
            try:
                await self.sync_once(on_log=on_log, on_progress=on_progress)
            except Exception as exc:  # noqa: BLE001 - keep the service alive
                emit(f"Sync cycle failed: {exc}")
                log.exception("Sync cycle failed")

            emit(f"Sleeping {self.settings.poll_interval}s until next poll.")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=self.settings.poll_interval
                )
            except asyncio.TimeoutError:
                pass  # interval elapsed -> next cycle
