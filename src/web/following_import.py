"""Replace the local Following list from JSON lines on stdin.

Each line is {"username", "nickname", "avatar_url"}; a plain username is also
accepted. Profile pictures are downloaded from the CDN URLs that came with the
following-list response, without cookies, so no extra profile pages are opened.

    docker exec -i <web-container> /app/.venv/bin/python -m web.following_import \
        < following.jsonl
"""

import json
import os
import sys
import time
from pathlib import Path

from web.search import cache_profile_avatar
from web.store import Store


def _accounts(stream):
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            item = {"username": line}
        if isinstance(item, str):
            item = {"username": item}
        if isinstance(item, dict) and item.get("username"):
            yield item


if __name__ == "__main__":
    accounts = list(_accounts(sys.stdin))
    if not accounts:
        raise SystemExit("No accounts on stdin; nothing changed")

    database = os.environ.get("TT_CLIPPER_DATABASE", "state/tt-clipper.sqlite3")
    store = Store(database)
    kept = store.replace_following(accounts)

    avatars_dir = Path(
        os.environ.get(
            "TT_CLIPPER_AVATARS", str(Path(database).resolve().parent / "avatars")
        )
    )
    saved = failed = 0
    for account in accounts:
        url = account.get("avatar_url")
        if not url:
            continue
        try:
            if cache_profile_avatar(
                account["username"].lstrip("@").lower(),
                avatars_dir,
                image_url=url,
                platform="tiktok",
            ):
                saved += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        time.sleep(0.1)

    print(f"following: {kept}, avatars_saved: {saved}, avatars_failed: {failed}")
