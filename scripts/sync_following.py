#!/usr/bin/env python3
"""Print the accounts the saved TikTok session follows, one per line.

Run locally; the web image has no browser extra:
    TIKTOK_SESSION_SS_FILE=cookies.txt PYTHONPATH=src .venv/bin/python \
        scripts/sync_following.py > state/following.txt

TikTok's /api/user/list/ needs a signed X-Bogus, so a real page is used: the
Following tab makes the request itself and we read the responses.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web.session import open_session  # noqa: E402


def main():
    names = []

    def on_response(response):
        if "/api/user/list/" not in response.url:
            return
        try:
            data = response.json()
        except Exception:
            return
        for item in data.get("userList") or []:
            username = (item.get("user") or {}).get("uniqueId")
            if username:
                names.append(username.lower())

    with open_session(headless=True) as page:
        info = page.context.request.get(
            "https://www.tiktok.com/passport/web/account/info/?aid=1988"
        )
        username = ((info.json() or {}).get("data") or {}).get("username")
        if not username:
            raise SystemExit("TikTok session is not valid; see web/session.py")

        page.on("response", on_response)
        page.goto(
            f"https://www.tiktok.com/@{username}?lang=en",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        try:
            page.locator('[data-e2e="following"]').first.click(timeout=15000)
        except Exception:
            page.get_by_text("Following", exact=True).first.click(timeout=10000)

        page.wait_for_timeout(4000)
        last = -1
        for _ in range(20):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1200)
            if len(set(names)) == last:
                break
            last = len(set(names))

    seen = set()
    ordered = [name for name in names if not (name in seen or seen.add(name))]
    if not ordered:
        raise SystemExit("No accounts found; TikTok may have shown a challenge")
    print("\n".join(ordered))


if __name__ == "__main__":
    main()
