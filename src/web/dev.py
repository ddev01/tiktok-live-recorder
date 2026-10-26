"""Local dev server: Flask reload on template/static changes."""

from __future__ import annotations

import os
from pathlib import Path

from web.app import create_app

WEB_DIR = Path(__file__).resolve().parent
REPO_ROOT = WEB_DIR.parent.parent


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
