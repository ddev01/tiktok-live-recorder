"""Replace the local Following list from newline-separated usernames on stdin.

    docker exec -i <web-container> /app/.venv/bin/python -m web.following_import \
        < following.txt
"""

import os
import sys

from web.store import Store

if __name__ == "__main__":
    names = [line.strip() for line in sys.stdin if line.strip()]
    if not names:
        raise SystemExit("No usernames on stdin; nothing changed")
    store = Store(os.environ.get("TT_CLIPPER_DATABASE", "state/tt-clipper.sqlite3"))
    print(f"following: {store.replace_following(names)}")
