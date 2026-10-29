"""Local dev server: Flask reload on template/static changes."""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
from pathlib import Path

from web.app import create_app

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent.parent

_worker_proc: subprocess.Popen[bytes] | None = None


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(REPO_ROOT / "src")
    prefix = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src if not prefix else f"{src}{os.pathsep}{prefix}"
    return env


def start_worker() -> None:
    """Spawn the recording queue worker (dev only)."""
    global _worker_proc
    if _worker_proc is not None and _worker_proc.poll() is None:
        return
    _worker_proc = subprocess.Popen(
        [sys.executable, "-m", "web.worker"],
        cwd=str(REPO_ROOT),
        env=_worker_env(),
    )


def stop_worker() -> None:
    global _worker_proc
    if _worker_proc is None or _worker_proc.poll() is not None:
        _worker_proc = None
        return
    _worker_proc.send_signal(signal.SIGINT)
    try:
        _worker_proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _worker_proc.kill()
        _worker_proc.wait()
    _worker_proc = None


def _extra_files() -> list[str]:
    paths: list[Path] = []
    templates = WEB_DIR / "templates"
    if templates.is_dir():
        paths.extend(p for p in templates.rglob("*") if p.is_file())
    for name in ("app.js", "app.css", "favicon.svg"):
        path = WEB_DIR / "static" / name
        if path.is_file():
            paths.append(path)
    ui_main = REPO_ROOT / "ui" / "main.css"
    if ui_main.is_file():
        paths.append(ui_main)
    return [str(p) for p in paths]


def main() -> None:
    os.environ.setdefault("FLASK_DEBUG", "1")
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_worker()
        atexit.register(stop_worker)

    app = create_app({"DEBUG": True})
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "8000")),
        debug=True,
        use_reloader=True,
        extra_files=_extra_files(),
    )


if __name__ == "__main__":
    main()
