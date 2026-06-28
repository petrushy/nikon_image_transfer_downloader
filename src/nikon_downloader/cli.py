"""Command-line interface for the Nikon Imaging Cloud downloader.

Entry points (both resolve to this module):
  nikon-downloader   — canonical name
  nikon-downloader — legacy alias

Commands
--------
  auth login/logout/status   manage authentication
  list                       list images in Nikon Imaging Cloud
  download [ID …]            download images to local disk
  sync                       continuous poll loop (daemon mode)
  config show/set/get        manage persistent configuration
  ui                         launch the NiceGUI control panel
"""

from __future__ import annotations

import asyncio
import csv
import json as json_mod
import logging
import signal
import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .auth import AuthError as EngineAuthError, TokenStore
from .config import (
    VALID_CONFIG_KEYS,
    config_file_path,
    load_config_file,
    load_settings,
    save_config_file,
)
from .downloader import Manifest, build_target_path
from .models import ImageItem
from .sync import SyncEngine, SyncFilter, SyncStats


# ---------------------------------------------------------------------------
# Shared context object
# ---------------------------------------------------------------------------


class _Ctx:
    """Carries global flags across the command group."""

    def __init__(
        self,
        config_path: str | None,
        verbose: int,
        json_mode: bool,
        no_color: bool,
    ) -> None:
        self.config_path: Path | None = (
            Path(config_path) if config_path else None
        )
        self.verbose = verbose
        self.json_mode = json_mode
        self.no_color = no_color

    def console(self) -> Console:
        plain = self.no_color or not sys.stdout.isatty()
        return Console(no_color=plain, highlight=False)

    def settings(self, **overrides):  # type: ignore[no-untyped-def]
        return load_settings(self.config_path, **overrides)

    def engine(self, **overrides):  # type: ignore[no-untyped-def]
        return SyncEngine(self.settings(**overrides))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _configure_logging(verbose: int) -> None:
    level = (
        logging.DEBUG
        if verbose >= 2
        else logging.INFO
        if verbose >= 1
        else logging.WARNING
    )
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _run_async(coro):  # type: ignore[no-untyped-def]
    """Run a coroutine, mapping known exceptions to clean CLI exits."""
    try:
        return asyncio.run(coro)
    except EngineAuthError as exc:
        raise click.ClickException(
            f"Authentication error: {exc}  (run `auth login` first)"
        ) from exc
    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)
        raise SystemExit(130) from None


def _die(code: int, msg: str) -> None:
    click.echo(f"Error: {msg}", err=True)
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

_TYPE_CHOICE = click.Choice(
    ["ALL", "RAW", "JPEG"], case_sensitive=False
)
_FMT_CHOICE = click.Choice(
    ["table", "json", "csv"], case_sensitive=False
)
_SORT_CHOICE = click.Choice(["date", "name"], case_sensitive=False)
_DATE_TYPE = click.DateTime(formats=["%Y-%m-%d"])


@click.group()
@click.option(
    "--config",
    "config_path",
    default=None,
    metavar="PATH",
    help="Config file (default: ~/.nikon_transfer/config.json)",
)
@click.option(
    "-v", "--verbose",
    count=True,
    help="Increase log verbosity (-v info, -vv debug).",
)
@click.option(
    "--json",
    "json_mode",
    is_flag=True,
    help="Emit all output as JSON.",
)
@click.option(
    "--no-color",
    "no_color",
    is_flag=True,
    help="Disable ANSI colour output.",
)
@click.pass_context
def cli(
    ctx: click.Context,
    config_path: str | None,
    verbose: int,
    json_mode: bool,
    no_color: bool,
) -> None:
    """Nikon Imaging Cloud — image downloader and sync tool."""
    ctx.ensure_object(dict)
    ctx.obj = _Ctx(config_path, verbose, json_mode, no_color)
    _configure_logging(verbose)


# ---------------------------------------------------------------------------
# auth group
# ---------------------------------------------------------------------------


@cli.group()
def auth() -> None:
    """Manage Nikon Imaging Cloud authentication."""


@auth.command("login")
@click.pass_obj
def auth_login(ctx: _Ctx) -> None:
    """Open a browser to authenticate and store the session."""
    engine = ctx.engine()
    _run_async(engine.login())
    click.echo("Authenticated. Session stored.")


@auth.command("logout")
@click.pass_obj
def auth_logout(ctx: _Ctx) -> None:
    """Delete stored tokens and end the session."""
    settings = ctx.settings()
    TokenStore(settings.token_file).clear()
    click.echo("Session cleared.")


@auth.command("status")
@click.pass_obj
def auth_status(ctx: _Ctx) -> None:
    """Show current authentication state and token expiry."""
    import time

    settings = ctx.settings()
    session = TokenStore(settings.token_file).load()

    if ctx.json_mode:
        if session is None:
            click.echo(
                json_mod.dumps({"status": "not_authenticated"})
            )
        else:
            exp = session.expires_at
            click.echo(
                json_mod.dumps(
                    {
                        "status": "authenticated",
                        "expires_at": (
                            datetime.fromtimestamp(exp).isoformat()
                            if exp
                            else None
                        ),
                        "expired": session.looks_expired(),
                    }
                )
            )
        return

    if session is None:
        click.echo("Status:  not authenticated")
        click.echo("Run `nikon-downloader auth login` to log in.")
        return

    console = ctx.console()
    console.print("[bold]Status:[/bold]  Connected")
    exp = session.expires_at
    if exp:
        remaining = exp - time.time()
        if remaining > 0:
            mins, secs = divmod(int(remaining), 60)
            console.print(
                f"[bold]Token:[/bold]   expires in "
                f"{mins} min {secs} s  (auto-refreshed)"
            )
        else:
            console.print(
                "[bold]Token:[/bold]   [red]expired[/red]"
                "  (run `auth login`)"
            )
    else:
        console.print("[bold]Token:[/bold]   expiry unknown")


# ---------------------------------------------------------------------------
# list command
# ---------------------------------------------------------------------------


@cli.command("list")
@click.option(
    "--format", "fmt",
    default="table",
    type=_FMT_CHOICE,
    show_default=True,
    help="Output format.",
)
@click.option(
    "--filter-type",
    default="ALL",
    type=_TYPE_CHOICE,
    show_default=True,
    help="Restrict to file category.",
)
@click.option(
    "--filter-camera",
    default=None,
    metavar="DEVICE",
    help="Restrict to camera name (substring match).",
)
@click.option(
    "--date-from",
    default=None,
    type=_DATE_TYPE,
    metavar="YYYY-MM-DD",
    help="Include only images shot on or after this date.",
)
@click.option(
    "--date-to",
    default=None,
    type=_DATE_TYPE,
    metavar="YYYY-MM-DD",
    help="Include only images shot on or before this date.",
)
@click.option(
    "--sort",
    "sort_by",
    default="date",
    type=_SORT_CHOICE,
    show_default=True,
)
@click.option(
    "--asc",
    is_flag=True,
    help="Ascending sort (default: descending).",
)
@click.option(
    "--limit",
    default=None,
    type=int,
    metavar="N",
    help="Stop after N images.",
)
@click.pass_obj
def cmd_list(
    ctx: _Ctx,
    fmt: str,
    filter_type: str,
    filter_camera: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    sort_by: str,
    asc: bool,
    limit: int | None,
) -> None:
    """List images currently in Nikon Imaging Cloud."""
    filt = SyncFilter(
        file_type=filter_type.lower(),
        camera=filter_camera,
        date_from=date_from.date() if date_from else None,
        date_to=date_to.date() if date_to else None,
    )
    items = _run_async(ctx.engine().list_all(filt=filt))

    reverse = not asc
    if sort_by == "date":
        items.sort(
            key=lambda i: i.effective_date or datetime.min,
            reverse=reverse,
        )
    else:
        items.sort(key=lambda i: i.name.lower(), reverse=reverse)

    if limit is not None:
        items = items[:limit]

    _render_list(items, fmt, ctx)


def _render_list(items: list[ImageItem], fmt: str, ctx: _Ctx) -> None:
    fmt = fmt.lower()

    if fmt == "json":
        out = []
        for item in items:
            d = item.effective_date
            out.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "camera": item.device_name,
                    "shot_date": d.isoformat() if d else None,
                    "type": item.file_extension,
                    "lifetime_days": item.lifetime,
                }
            )
        click.echo(json_mod.dumps(out, indent=2))
        return

    if fmt == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(
            ["id", "name", "camera", "shot_date", "type",
             "lifetime_days"]
        )
        for item in items:
            d = item.effective_date
            writer.writerow(
                [
                    item.id,
                    item.name,
                    item.device_name,
                    d.strftime("%Y-%m-%d") if d else "",
                    item.file_extension,
                    item.lifetime if item.lifetime is not None else "",
                ]
            )
        return

    # Rich table
    console = ctx.console()
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Name", no_wrap=True)
    table.add_column("Camera")
    table.add_column("Shot Date")
    table.add_column("Type")
    table.add_column("Lifetime", justify="right")

    for item in items:
        d = item.effective_date
        date_str = d.strftime("%Y-%m-%d") if d else "—"
        lt = item.lifetime
        if lt is not None:
            if lt <= 3:
                lt_str = f"[red]{lt} d[/red]"
            elif lt <= 7:
                lt_str = f"[yellow]{lt} d[/yellow]"
            else:
                lt_str = f"{lt} d"
        else:
            lt_str = "—"
        id_short = item.id[:12] + "…" if len(item.id) > 12 else item.id
        table.add_row(
            id_short,
            item.name,
            item.device_name,
            date_str,
            item.file_extension,
            lt_str,
        )

    console.print(table)
    n = len(items)
    console.print(
        f"\n{n} image{'s' if n != 1 else ''}",
        style="dim",
    )


# ---------------------------------------------------------------------------
# download command
# ---------------------------------------------------------------------------


@cli.command("download")
@click.argument("ids", nargs=-1, metavar="[ID...]")
@click.option(
    "--dest",
    default=None,
    metavar="PATH",
    help="Override the destination root for this run.",
)
@click.option(
    "--filter-type",
    default="ALL",
    type=_TYPE_CHOICE,
    show_default=True,
)
@click.option("--filter-camera", default=None, metavar="DEVICE")
@click.option(
    "--date-from", default=None, type=_DATE_TYPE, metavar="YYYY-MM-DD"
)
@click.option(
    "--date-to", default=None, type=_DATE_TYPE, metavar="YYYY-MM-DD"
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print what would be downloaded; write nothing.",
)
@click.option(
    "--concurrency",
    default=3,
    show_default=True,
    type=int,
    help="Parallel download workers.",
)
@click.option(
    "--retries",
    default=3,
    show_default=True,
    type=int,
    help="Per-file retry attempts on transient errors.",
)
@click.pass_obj
def cmd_download(
    ctx: _Ctx,
    ids: tuple[str, ...],
    dest: str | None,
    filter_type: str,
    filter_camera: str | None,
    date_from: datetime | None,
    date_to: datetime | None,
    dry_run: bool,
    concurrency: int,
    retries: int,
) -> None:
    """Download images from Nikon Imaging Cloud.

    Pass one or more IDs to download specific images; omit them to
    download everything matching the filter options.
    """
    filt = SyncFilter(
        file_type=filter_type.lower(),
        camera=filter_camera,
        date_from=date_from.date() if date_from else None,
        date_to=date_to.date() if date_to else None,
    )
    dest_override = {"dest_dir": dest} if dest else {}
    engine = ctx.engine(**dest_override)

    if dry_run:
        settings = ctx.settings(**dest_override)
        all_items = _run_async(engine.list_all(filt=filt))
        if ids:
            id_set = set(ids)
            all_items = [i for i in all_items if i.id in id_set]

        manifest = Manifest(settings.manifest_file)
        would_download: list[ImageItem] = []
        already_on_disk: list[ImageItem] = []
        for item in all_items:
            path = build_target_path(item, settings.dest_dir)
            if manifest.has(item) or (
                path.exists()
                and (
                    item.original_file_size is None
                    or path.stat().st_size == item.original_file_size
                )
            ):
                already_on_disk.append(item)
            else:
                would_download.append(item)

        ft = filt.file_type
        click.echo(
            f"Dry run — {len(all_items)} images match filter '{ft}':"
        )
        click.echo(f"  Already on disk:  {len(already_on_disk)}")
        click.echo(f"  Would download:   {len(would_download)}")
        if would_download:
            for item in would_download:
                d = item.effective_date
                date_str = (
                    d.strftime("%Y-%m-%d") if d else "unknown date"
                )
                click.echo(
                    f"    {item.name}  ({item.device_name})  {date_str}"
                )
            click.echo(
                "\nRun without --dry-run to proceed."
            )
        else:
            click.echo("Nothing new to download.")
        return

    def _on_log(msg: str) -> None:
        if ctx.verbose or not ctx.json_mode:
            click.echo(msg)

    if ids:
        # Download by specific IDs: list, filter, then restrict to IDs.
        all_items = _run_async(engine.list_all(filt=filt))
        id_set = set(ids)
        wanted = [i for i in all_items if i.id in id_set]
        missing = id_set - {i.id for i in wanted}
        for mid in sorted(missing):
            click.echo(f"Warning: ID not found on cloud: {mid}", err=True)
        stats: SyncStats = _run_async(
            engine.sync_once(
                on_log=_on_log,
                items_override=wanted,
                concurrency=concurrency,
                retries=retries,
            )
        )
    else:
        stats = _run_async(
            engine.sync_once(
                filt=filt,
                on_log=_on_log,
                concurrency=concurrency,
                retries=retries,
            )
        )

    if ctx.json_mode:
        click.echo(
            json_mod.dumps(
                {
                    "downloaded": stats.downloaded,
                    "skipped": stats.skipped,
                    "failed": stats.failed,
                    "errors": stats.errors,
                }
            )
        )

    if stats.failed > 0:
        raise SystemExit(4)


# ---------------------------------------------------------------------------
# sync command
# ---------------------------------------------------------------------------


@cli.command("sync")
@click.option(
    "--interval",
    default=300,
    show_default=True,
    type=int,
    help="Poll interval in seconds.",
)
@click.option(
    "--once",
    "run_once",
    is_flag=True,
    help="Run exactly one poll cycle then exit.",
)
@click.option("--dest", default=None, metavar="PATH")
@click.option(
    "--filter-type",
    default="ALL",
    type=_TYPE_CHOICE,
    show_default=True,
)
@click.option("--filter-camera", default=None, metavar="DEVICE")
@click.pass_obj
def cmd_sync(
    ctx: _Ctx,
    interval: int,
    run_once: bool,
    dest: str | None,
    filter_type: str,
    filter_camera: str | None,
) -> None:
    """Poll Nikon Imaging Cloud and download new images.

    Runs continuously by default; use --once for a single pass.
    Responds to SIGINT/SIGTERM by completing the current download
    then exiting cleanly.
    """
    filt = SyncFilter(
        file_type=filter_type.lower(),
        camera=filter_camera,
    )
    dest_override = {"dest_dir": dest} if dest else {}
    engine = ctx.engine(poll_interval=interval, **dest_override)

    def _on_log(msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        click.echo(f"{ts}  {msg}")

    if run_once:
        stats: SyncStats = _run_async(
            engine.sync_once(filt=filt, on_log=_on_log)
        )
        if stats.failed > 0:
            raise SystemExit(4)
        return

    click.echo(
        f"Syncing every {interval}s.  Press Ctrl+C to stop.",
        err=True,
    )

    async def _service() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _request_stop() -> None:
            click.echo(
                "\nSignal received — finishing current transfer…",
                err=True,
            )
            stop_event.set()

        try:
            loop.add_signal_handler(signal.SIGINT, _request_stop)
            loop.add_signal_handler(signal.SIGTERM, _request_stop)
        except (NotImplementedError, OSError):
            pass  # Windows fallback: KeyboardInterrupt below

        try:
            await engine.run_service(
                filt=filt, on_log=_on_log, stop_event=stop_event
            )
        finally:
            try:
                loop.remove_signal_handler(signal.SIGINT)
                loop.remove_signal_handler(signal.SIGTERM)
            except (NotImplementedError, OSError):
                pass

    try:
        asyncio.run(_service())
    except KeyboardInterrupt:
        click.echo("\nInterrupted.", err=True)


# ---------------------------------------------------------------------------
# config group
# ---------------------------------------------------------------------------


@cli.group("config")
def cmd_config() -> None:
    """Manage persistent configuration (non-secret settings)."""


@cmd_config.command("show")
@click.pass_obj
def config_show(ctx: _Ctx) -> None:
    """Print all current configuration values."""
    path = config_file_path(ctx.config_path)
    cfg = load_config_file(ctx.config_path)
    if ctx.json_mode:
        click.echo(json_mod.dumps(cfg, indent=2))
        return
    console = ctx.console()
    console.print(f"[dim]Config: {path}[/dim]")
    if not cfg:
        console.print("[dim](no values set)[/dim]")
        return
    for key, value in sorted(cfg.items()):
        console.print(f"  [bold]{key}[/bold] = {value}")


@cmd_config.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_obj
def config_set(ctx: _Ctx, key: str, value: str) -> None:
    """Set a persistent configuration key."""
    if key not in VALID_CONFIG_KEYS:
        _die(
            3,
            f"Unknown key '{key}'. "
            f"Valid keys: {', '.join(sorted(VALID_CONFIG_KEYS))}",
        )
    cfg = load_config_file(ctx.config_path)
    cfg[key] = value
    save_config_file(cfg, ctx.config_path)
    click.echo(f"{key} = {value}")


@cmd_config.command("get")
@click.argument("key")
@click.pass_obj
def config_get(ctx: _Ctx, key: str) -> None:
    """Print a single configuration key."""
    cfg = load_config_file(ctx.config_path)
    if key not in cfg:
        click.echo("(not set)", err=True)
        raise SystemExit(3)
    click.echo(cfg[key])


# ---------------------------------------------------------------------------
# ui command
# ---------------------------------------------------------------------------


@cli.command("ui")
@click.option(
    "--port", default=8080, show_default=True, type=int,
    help="Port for the NiceGUI web interface."
)
def cmd_ui(port: int) -> None:
    """Launch the NiceGUI control panel."""
    from .ui import run_ui  # lazy import: NiceGUI is optional

    run_ui(port=port)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    cli(argv)


if __name__ == "__main__":
    main()
