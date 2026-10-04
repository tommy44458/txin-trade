"""Private loopback API, packaged web UI and workers managed by the desktop app."""

import argparse
import importlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .platform_process import NO_WINDOW, contain_descendants
from .product_version import product_version

# Exit code for a database upgraded by a newer txinTrade (sysexits EX_DATAERR).
DATABASE_FROM_NEWER_VERSION_EXIT = 65
SERVICES = (
    "worker", "shadow_v4", "event_sync", "macro_actual_sync", "news_sync",
    "sec_news_sync", "news_classification_worker", "outcome_worker",
)


def service_command(service: str) -> list[str]:
    if service not in SERVICES:
        raise ValueError("Unknown desktop service")
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [
        sys.executable, "-m", "trade_helper.desktop_runtime",
    ]
    return [*prefix, "--service", service]


def mount_web(app, web_dir: Path) -> None:
    web_dir = web_dir.resolve()
    if not (web_dir / "index.html").is_file():
        raise RuntimeError("Desktop web build is missing")
    app.mount("/assets", StaticFiles(directory=web_dir / "assets"), name="desktop-assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/") or path == "api":
            raise HTTPException(404, "Not found")
        target = (web_dir / path).resolve()
        if not target.is_relative_to(web_dir):
            raise HTTPException(404, "Not found")
        return FileResponse(target if target.is_file() else web_dir / "index.html")


def stop_services(children: list[subprocess.Popen]) -> None:
    for child in children:
        if child.poll() is None:
            child.terminate()
    deadline = time.monotonic() + 2.5
    for child in children:
        try:
            child.wait(timeout=max(0.05, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def monitor_services(children, server, stopped: threading.Event, failed: threading.Event,
                     parent: int | None = None) -> None:
    # Never leave the UI accepting jobs when its worker has died. The desktop
    # shell owns the restart action and will terminate this private process group.
    while not stopped.wait(0.5):
        # The app was force-quit (for example Ctrl+C in a terminal) without stopping
        # this group: exit too, so no orphan keeps the data locks or the cloud link.
        if parent is not None and os.getppid() != parent:
            stopped.set()
            server.should_exit = True
            return
        if any(child.poll() is not None for child in children):
            failed.set()
            server.should_exit = True
            return


def watch_lifeline(stream, stop) -> None:
    """Stop when the desktop app closes our stdin, including when it crashes.

    Windows has no SIGTERM to ask for a clean exit and does not reparent an
    orphan, so the app keeps a pipe open instead: end-of-file means stop.
    """
    try:
        while stream.read(4096):
            pass
    except (OSError, ValueError):
        pass
    stop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", action="version", version=f"txinTrade {product_version()}")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--web-dir", type=Path)
    parser.add_argument("--service", choices=SERVICES)
    parser.add_argument("--no-workers", action="store_true",
                        help="Run only the private API and web UI for offline package verification")
    args = parser.parse_args(argv)
    if os.environ.get("APP_DESKTOP") != "1" or not os.environ.get("APP_DESKTOP_TOKEN"):
        raise RuntimeError("Desktop runtime requires a private session token")
    if args.service:
        if args.no_workers:
            parser.error("--service cannot be combined with --no-workers")
        importlib.import_module(f"trade_helper.{args.service}").main()
        return
    if not args.web_dir or not 1 <= args.port <= 65535:
        parser.error("--web-dir and a valid --port are required")
    # Workers and model CLIs end with this process on Windows, however it ends.
    contain_descendants()
    from .api import app
    from .db import DatabaseFromNewerVersion, init_db
    from .desktop_updates import reset_desktop_update_gate

    # Apply embedded-database migrations before workers begin sharing the WAL file.
    try:
        init_db()
    except DatabaseFromNewerVersion:
        # The desktop app reads this exit code and offers the update instead of a generic failure.
        print("txinTrade: the local database is from a newer version; update the app.", file=sys.stderr)
        raise SystemExit(DATABASE_FROM_NEWER_VERSION_EXIT) from None
    reset_desktop_update_gate()
    mount_web(app, args.web_dir)
    children: list[subprocess.Popen] = []
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning",
    ))
    stopped, failed = threading.Event(), threading.Event()

    def shutdown(_signal, _frame):
        stopped.set()
        server.should_exit = True
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if os.environ.get("APP_DESKTOP_LIFELINE") == "stdin":
        threading.Thread(target=watch_lifeline, args=(sys.stdin.buffer, lambda: shutdown(None, None)),
                         daemon=True).start()
    try:
        if not args.no_workers:
            children.extend(subprocess.Popen(service_command(service), stdin=subprocess.DEVNULL,
                                             creationflags=NO_WINDOW) for service in SERVICES)
            threading.Thread(target=monitor_services, args=(children, server, stopped, failed, os.getppid()),
                             daemon=True).start()
        server.run()
    finally:
        stopped.set()
        stop_services(children)
    if failed.is_set():
        raise SystemExit("A desktop background service stopped; restart the local application")


if __name__ == "__main__":
    main()
