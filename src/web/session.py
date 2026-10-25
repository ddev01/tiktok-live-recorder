"""Reuse a TikTok session that was created by logging in manually in a browser.

Export the browser cookies as a Netscape ``cookies.txt`` file, then load them
into a Playwright context. No password login or challenge bypass is involved.
"""

from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from utils.utils import cookie_file, read_cookies

# TikTok's messages page redirects to /login without a valid session, unlike
# the homepage which renders for logged-out visitors too.
AUTH_CHECK_URL = "https://www.tiktok.com/messages"
SESSION_COOKIE_NAMES = ("sessionid", "sessionid_ss", "session_ss")
COOKIE_HELP = (
    "TikTok session is missing or expired. Log in at https://www.tiktok.com, "
    "export cookies.txt with the 'Get cookies.txt' browser extension, and point "
    "TIKTOK_SESSION_SS_FILE at that file."
)


class SessionExpired(RuntimeError):
    """The cookie file has no usable TikTok session."""


def parse_netscape_cookies(text):
    """Parse Netscape cookies into Playwright's ``add_cookies`` dictionaries."""
    cookies = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        http_only = line.startswith("#HttpOnly_")
        if line.startswith("#") and not http_only:
            continue
        if http_only:
            line = line[len("#HttpOnly_") :]

        fields = line.split("\t")
        if len(fields) != 7:
            continue
        domain, _, path, secure, expires, name, value = fields
        bare_domain = domain.lstrip(".")
        if not (bare_domain == "tiktok.com" or bare_domain.endswith(".tiktok.com")):
            continue
        if not name or not value:
            continue

        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": secure.upper() == "TRUE",
            "httpOnly": http_only,
        }
        try:
            expiry = int(float(expires))
        except ValueError:
            expiry = 0
        if expiry > 0:
            cookie["expires"] = expiry
        cookies.append(cookie)
    return cookies


def load_cookies(path=None):
    """Read and validate a Netscape cookie file, raising ``SessionExpired``."""
    path = path or cookie_file()
    if not path or not Path(path).is_file():
        raise SessionExpired(COOKIE_HELP)

    cookies = parse_netscape_cookies(Path(path).read_text(encoding="utf-8"))
    if not cookies:
        # Some exports are a raw "Cookie:" header; Playwright only needs
        # name/value pairs scoped to tiktok.com for those.
        cookies = [
            {"name": name, "value": value, "domain": ".tiktok.com", "path": "/"}
            for name, value in read_cookies(path).items()
            if isinstance(value, str) and value
        ]
    if not any(c["name"] in SESSION_COOKIE_NAMES for c in cookies):
        raise SessionExpired(COOKIE_HELP)
    return cookies


def check_session(page, timeout_ms=30000):
    """Visit an auth-only TikTok page and confirm the session is still valid."""
    if not any(
        c.get("name") in SESSION_COOKIE_NAMES and c.get("value")
        for c in page.context.cookies()
    ):
        raise SessionExpired(COOKIE_HELP)

    page.goto(AUTH_CHECK_URL, wait_until="domcontentloaded", timeout=timeout_ms)
    # shortcut: this relies on TikTok redirecting expired sessions to /login;
    # revisit if TikTok changes that route or its redirect behaviour.
    if urlparse(page.url).path.startswith("/login"):
        raise SessionExpired(COOKIE_HELP)


@contextmanager
def open_session(cookie_path=None, headless=True):
    """Yield a page whose context already holds the exported cookies."""
    from playwright.sync_api import sync_playwright

    cookies = load_cookies(cookie_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        try:
            context = browser.new_context()
            context.add_cookies(cookies)
            yield context.new_page()
        finally:
            browser.close()


def assert_live_room_loaded(page, url, timeout_ms=30000):
    """Open a live room and wait until its video element is visible."""
    host = urlparse(url).hostname or ""
    if not (host == "tiktok.com" or host.endswith(".tiktok.com")):
        raise ValueError("Live room URL must be on tiktok.com")

    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if urlparse(page.url).path.startswith("/login"):
        raise SessionExpired(COOKIE_HELP)
    page.locator("video").first.wait_for(state="visible", timeout=timeout_ms)


if __name__ == "__main__":
    try:
        with open_session(headless=False) as session_page:
            check_session(session_page)
    except SessionExpired as error:
        raise SystemExit(str(error)) from error
    print("TikTok session is valid.")
