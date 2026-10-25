import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from web import session  # noqa: E402

NETSCAPE = """# Netscape HTTP Cookie File
#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\ttest-only-session
.tiktok.com\tTRUE\t/\tTRUE\t1893456000\ttt-target-idc\ttest-only-idc
.example.com\tTRUE\t/\tFALSE\t1893456000\tunrelated\ttest-only-other
malformed line
"""


def fake_page(url, cookies=(("sessionid", "test-only-session"),)):
    return SimpleNamespace(
        url=url,
        context=SimpleNamespace(
            cookies=lambda: [{"name": n, "value": v} for n, v in cookies]
        ),
        goto=lambda target, **_: None,
    )


def test_parse_netscape_keeps_tiktok_fields_and_http_only():
    cookies = session.parse_netscape_cookies(NETSCAPE)

    assert [c["name"] for c in cookies] == ["sessionid", "tt-target-idc"]
    assert cookies[0]["httpOnly"] is True
    assert cookies[0]["domain"] == ".tiktok.com"
    assert cookies[0]["secure"] is True
    assert "expires" not in cookies[0]
    assert cookies[1]["expires"] == 1893456000


def test_parse_netscape_ignores_empty_values():
    text = ".tiktok.com\tTRUE\t/\tFALSE\t0\tsessionid\t\n"
    assert session.parse_netscape_cookies(text) == []


def test_load_cookies_asks_for_reexport_when_file_is_missing(tmp_path):
    with pytest.raises(session.SessionExpired, match="re-export|export cookies.txt"):
        session.load_cookies(tmp_path / "missing.txt")


def test_load_cookies_asks_for_reexport_without_session_cookie(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n.tiktok.com\tTRUE\t/\tFALSE\t0\tmsToken\tx\n",
        encoding="utf-8",
    )
    with pytest.raises(session.SessionExpired, match="export cookies.txt"):
        session.load_cookies(path)


def test_load_cookies_accepts_raw_cookie_header(tmp_path):
    path = tmp_path / "cookie.txt"
    path.write_text(
        "tt_csrf_token=x; sessionid=test-only-session; msToken=y", encoding="utf-8"
    )
    cookies = session.load_cookies(path)
    assert {c["name"] for c in cookies} == {"tt_csrf_token", "sessionid", "msToken"}
    assert all(c["domain"] == ".tiktok.com" for c in cookies)


def test_check_session_accepts_authenticated_messages_page(monkeypatch):
    page = fake_page("https://www.tiktok.com/messages")
    monkeypatch.setattr(page, "goto", lambda target, **_: setattr(page, "url", target))
    session.check_session(page)
    assert page.url == session.AUTH_CHECK_URL


def test_check_session_rejects_login_redirect(monkeypatch):
    page = fake_page("https://www.tiktok.com/")
    monkeypatch.setattr(
        page,
        "goto",
        lambda target, **_: setattr(
            page, "url", "https://www.tiktok.com/login?redirect_url=%2Fmessages"
        ),
    )
    with pytest.raises(session.SessionExpired, match="export cookies.txt"):
        session.check_session(page)


def test_check_session_rejects_context_without_session_cookie():
    page = fake_page("https://www.tiktok.com/", cookies=())
    with pytest.raises(session.SessionExpired, match="export cookies.txt"):
        session.check_session(page)


def test_live_room_url_must_be_tiktok():
    with pytest.raises(ValueError, match="tiktok.com"):
        session.assert_live_room_loaded(
            fake_page("https://example.com"), "https://example.com/live"
        )


@pytest.mark.skipif(
    not (
        os.environ.get("TIKTOK_LIVE_URL") and os.environ.get("TIKTOK_SESSION_SS_FILE")
    ),
    reason="set TIKTOK_LIVE_URL and TIKTOK_SESSION_SS_FILE to run the live room test",
)
def test_exported_cookies_open_a_live_room():
    with session.open_session(headless=os.environ.get("TIKTOK_HEADED") != "1") as page:
        session.check_session(page)
        session.assert_live_room_loaded(page, os.environ["TIKTOK_LIVE_URL"])
