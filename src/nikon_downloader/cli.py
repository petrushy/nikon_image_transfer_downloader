"""Headless command-line entry points.

    nikon-downloader login      # open a browser, log in, store the session
    nikon-downloader sync       # one-shot: download anything new, then exit
    nikon-downloader service    # poll the cloud on an interval (daemon)
    nikon-downloader ui         # launch the NiceGUI control panel
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .config import load_settings
from .sync import SyncEngine


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


async def _run(args: argparse.Namespace) -> int:
    settings = load_settings()
    engine = SyncEngine(settings)

    if args.command == "login":
        await engine.login()
        print("Login captured and stored.")
        return 0

    if args.command == "sync":
        stats = await engine.sync_once(on_log=print, max_items=args.limit)
        return 0 if stats.failed == 0 else 1

    if args.command == "service":
        print(
            f"Polling every {settings.poll_interval}s. "
            "Press Ctrl+C to stop."
        )
        await engine.run_service(on_log=print)
        return 0

    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nikon-downloader",
        description="One-way downloader for Nikon Imaging Cloud images.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="debug logging"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="open a browser, log in, store the session")
    sync_p = sub.add_parser("sync", help="download anything new, then exit")
    sync_p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only download the first N matching images (for testing)",
    )
    sub.add_parser("service", help="poll the cloud on an interval")
    sub.add_parser("ui", help="launch the NiceGUI control panel")

    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    if args.command == "ui":
        # Imported lazily so the CLI works without NiceGUI installed.
        from .ui import run_ui

        run_ui()
        return 0

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
