"""Optional NiceGUI control panel.

A thin front-end over :class:`~nikon_downloader.sync.SyncEngine`. It owns no
core logic — it only triggers engine actions and visualises their results, so
the engine remains fully usable headless (CLI / service).
"""

from __future__ import annotations

import asyncio
import logging

from nicegui import ui

from .config import load_settings
from .sync import SyncEngine, SyncStats

log = logging.getLogger(__name__)


class AppState:
    """Holds the engine and the live service task for the running UI."""

    def __init__(self) -> None:
        self.settings = load_settings()
        self.engine = SyncEngine(self.settings)
        self.service_task: asyncio.Task | None = None
        self.stop_event: asyncio.Event | None = None

    @property
    def service_running(self) -> bool:
        return self.service_task is not None and not self.service_task.done()


def run_ui(port: int = 8080) -> None:
    """Build and launch the control panel (blocks until the window closes)."""
    state = AppState()

    @ui.page("/")
    def main_page() -> None:
        _build_page(state)

    ui.run(
        title="Nikon Imaging Cloud Downloader",
        port=port,
        reload=False,
        show=True,
    )


def _build_page(state: AppState) -> None:
    settings = state.settings

    ui.label("Nikon Imaging Cloud — Downloader").classes("text-2xl font-bold")

    # -- Connection ---------------------------------------------------------
    with ui.card().classes("w-full"):
        ui.label("Connection").classes("text-lg font-semibold")
        status = ui.label()
        ui.label(f"Account: {settings.username or '(not set)'}")

        def refresh_status() -> None:
            ok = state.engine.is_authenticated
            status.text = (
                "Status: connected" if ok else "Status: not logged in"
            )
            status.classes(
                replace="text-positive" if ok else "text-negative"
            )

        async def do_login() -> None:
            login_btn.disable()
            ui.notify("A browser window will open — complete the login there.")
            try:
                await state.engine.login()
                ui.notify("Logged in and session stored.", type="positive")
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                ui.notify(f"Login failed: {exc}", type="negative")
            finally:
                login_btn.enable()
                refresh_status()

        with ui.row():
            login_btn = ui.button("Log in to Nikon", on_click=do_login)
        refresh_status()

    # -- Settings -----------------------------------------------------------
    with ui.card().classes("w-full"):
        ui.label("Settings").classes("text-lg font-semibold")
        dest = ui.input("Destination folder", value=str(settings.dest_dir))
        fmt = ui.select(
            ["all", "raw", "jpeg"],
            value=settings.file_format,
            label="File format",
        )
        interval = ui.number(
            "Poll interval (seconds)", value=settings.poll_interval, min=30
        )

        def apply_settings() -> None:
            from pathlib import Path

            settings.dest_dir = Path(dest.value)
            settings.file_format = fmt.value
            settings.poll_interval = int(interval.value)
            ui.notify("Settings applied.", type="positive")

        ui.button("Apply", on_click=apply_settings)

    # -- Sync ---------------------------------------------------------------
    with ui.card().classes("w-full"):
        ui.label("Sync").classes("text-lg font-semibold")
        stats_label = ui.label("Idle.")
        progress = ui.linear_progress(value=0, show_value=False)
        log_view = ui.log(max_lines=200).classes("w-full h-48")

        def on_log(msg: str) -> None:
            log_view.push(msg)

        def on_progress(stats: SyncStats) -> None:
            done = stats.downloaded + stats.skipped + stats.failed
            progress.value = (done / stats.total) if stats.total else 0
            stats_label.text = (
                f"Total {stats.total} · downloaded {stats.downloaded} · "
                f"skipped {stats.skipped} · failed {stats.failed}"
            )

        async def do_sync() -> None:
            sync_btn.disable()
            try:
                await state.engine.sync_once(
                    on_log=on_log, on_progress=on_progress
                )
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"Sync failed: {exc}", type="negative")
            finally:
                sync_btn.enable()

        def toggle_service() -> None:
            if state.service_running:
                if state.stop_event:
                    state.stop_event.set()
                service_btn.text = "Start service"
                ui.notify("Stopping service after current cycle...")
            else:
                state.stop_event = asyncio.Event()
                state.service_task = asyncio.create_task(
                    state.engine.run_service(
                        on_log=on_log,
                        on_progress=on_progress,
                        stop_event=state.stop_event,
                    )
                )
                service_btn.text = "Stop service"
                ui.notify("Service started.", type="positive")

        with ui.row():
            sync_btn = ui.button("Sync now", on_click=do_sync)
            service_btn = ui.button("Start service", on_click=toggle_service)

    # -- Preview ------------------------------------------------------------
    with ui.card().classes("w-full"):
        ui.label("Preview").classes("text-lg font-semibold")
        grid = ui.row().classes("flex-wrap gap-2")

        async def load_preview() -> None:
            grid.clear()
            try:
                items = await state.engine.list_preview(limit=24)
            except Exception as exc:  # noqa: BLE001
                ui.notify(f"Could not load preview: {exc}", type="negative")
                return
            if not items:
                with grid:
                    ui.label("No images found.")
                return
            with grid:
                for item in items:
                    with ui.card().classes("w-40"):
                        if item.thumbnail_file_url:
                            ui.image(item.thumbnail_file_url).classes("w-full")
                        ui.label(item.name).classes("text-xs truncate")
                        date = item.effective_date
                        ui.label(
                            date.strftime("%Y-%m-%d") if date else "—"
                        ).classes("text-xs text-grey")

        ui.button("Load preview", on_click=load_preview)
