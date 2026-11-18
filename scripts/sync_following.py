#!/usr/bin/env python3
"""Print the accounts the saved TikTok session follows, one JSON object per line.

Run locally; the web image has no browser extra:
    TIKTOK_SESSION_SS_FILE=cookies.txt PYTHONPATH=src .venv/bin/python \
        scripts/sync_following.py > state/following.jsonl

TikTok's /api/user/list/ needs a signed X-Bogus, so a real page is used: the
Following modal makes the request itself. Only scene 21 (the actual follows) is
kept; scene 151 is TikTok's suggested-accounts carousel.

If TikTok shows a verification challenge, the run aborts instead of retrying.
Set SYNC_HEADED=1 to open a visible browser and solve the challenge yourself.
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from web.session import open_session  # noqa: E402

FOLLOWING_SCENE = "21"
CHALLENGE_WORDS = ("drag the slider", "puzzle", "captcha", "security check")


def _account(user):
    username = user.get("uniqueId")
    if not isinstance(username, str) or not username.strip():
        return None
    username = username.strip().lower()
    nickname = user.get("nickname")
    nickname = nickname.strip() if isinstance(nickname, str) else None
    avatar_url = None
    for key in ("avatarLarger", "avatarMedium", "avatarThumb"):
        value = user.get(key)
        if isinstance(value, dict):
            urls = value.get("url_list") or []
            if urls:
                avatar_url = urls[0]
                break
        elif isinstance(value, str) and value:
            avatar_url = value
            break
    return {
        "username": username,
        "nickname": nickname or None,
        "avatar_url": avatar_url,
    }


def _challenge_visible(page):
    try:
        text = page.locator('[role="dialog"]').first.inner_text(timeout=1000)
    except Exception:
        return False
    lowered = (text or "").lower()
    return any(word in lowered for word in CHALLENGE_WORDS)


def _scroll(page):
    page.evaluate(
        """() => {
            const dialog = document.querySelector('[role="dialog"]');
            const root = dialog || document;
            for (const el of root.querySelectorAll('*')) {
                if (el.scrollHeight > el.clientHeight + 10) {
                    el.scrollTop = el.scrollHeight;
                }
            }
            window.scrollTo(0, document.body.scrollHeight);
        }"""
    )
    box = None
    try:
        box = page.locator('[role="dialog"]').first.bounding_box()
    except Exception:
        pass
    if box:
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.wheel(0, 8000)


def main():
    accounts = {}
    state = {"pages": 0, "has_more": True}

    def on_response(response):
        if "/api/user/list/" not in response.url:
            return
        query = parse_qs(urlparse(response.url).query)
        if (query.get("scene") or [""])[0] != FOLLOWING_SCENE:
            return
        try:
            data = response.json()
        except Exception:
            return
        state["pages"] += 1
        state["has_more"] = bool(data.get("hasMore"))
        for item in data.get("userList") or []:
            try:
                account = _account(item.get("user") or {})
            except Exception:
                continue
            if account:
                accounts[account["username"]] = account

    headed = bool(os.environ.get("SYNC_HEADED"))
    with open_session(headless=not headed) as page:
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
        expected = None
        try:
            count_text = page.locator(
                'strong[data-e2e="following-count"]'
            ).first.inner_text(timeout=5000)
            expected = int(count_text.replace(",", "").strip())
        except Exception:
            pass

        try:
            page.locator('[data-e2e="following"]').first.click(timeout=15000)
        except Exception:
            page.get_by_text("Following", exact=True).first.click(timeout=10000)

        if _challenge_visible(page):
            if not headed:
                raise SystemExit(
                    "TikTok showed a verification challenge. Rerun with "
                    "SYNC_HEADED=1 and solve it in the open window."
                )
            print("Solve the TikTok challenge in the open window.", file=sys.stderr)
            for _ in range(60):
                page.wait_for_timeout(2000)
                if not _challenge_visible(page):
                    break
            else:
                raise SystemExit("Verification challenge was not solved in time.")

        page.wait_for_timeout(3000)
        last = -1
        stalled = 0
        for _ in range(60):
            _scroll(page)
            page.wait_for_timeout(3000)
            if state["pages"] and not state["has_more"]:
                break
            if len(accounts) == last:
                stalled += 1
                if stalled >= 5:
                    break
            else:
                stalled = 0
                last = len(accounts)

    if not accounts:
        raise SystemExit("No followed accounts found; TikTok may have blocked the page.")

    for account in accounts.values():
        print(json.dumps(account, ensure_ascii=False))

    print(
        f"pages={state['pages']} accounts={len(accounts)} "
        f"has_more={state['has_more']} expected={expected}",
        file=sys.stderr,
    )
    if state["has_more"]:
        raise SystemExit("TikTok still reports more pages; not a complete list.")
    if expected is not None and len(accounts) < expected:
        print(
            f"warning: web list returned {len(accounts)} of {expected} follows; "
            "private or unavailable accounts are hidden from the web list. "
            "Use TikTok's Download your data export for those.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
