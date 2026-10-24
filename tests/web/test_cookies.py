import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from utils import utils  # noqa: E402


def test_cookie_loader_prefers_plain_docker_secret_without_printing_value(
    tmp_path, monkeypatch
):
    secret = tmp_path / "session"
    secret.write_text("test-only-cookie", encoding="utf-8")
    monkeypatch.setenv("TIKTOK_SESSION_SS_FILE", str(secret))
    cookies = utils.read_cookies()
    assert list(cookies) == ["session_ss"]
    assert isinstance(cookies["session_ss"], str)


def test_cookie_loader_parses_cookie_header_secret(tmp_path, monkeypatch):
    secret = tmp_path / "session"
    secret.write_text(
        "session_ss=test-only-cookie; msToken=test-only-token", encoding="utf-8"
    )
    monkeypatch.setenv("TIKTOK_SESSION_SS_FILE", str(secret))
    cookies = utils.read_cookies()
    assert set(cookies) == {"session_ss", "msToken"}
    assert all(isinstance(value, str) for value in cookies.values())


def test_cookie_loader_keeps_cli_config_fallback_when_secret_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TIKTOK_SESSION_SS_FILE", str(tmp_path / "missing-secret"))
    original_open = open
    opened = []

    def fake_open(path, *args, **kwargs):
        opened.append(str(path))
        if str(path).endswith("cookies.json"):
            from io import StringIO

            return StringIO('{"cli": true}')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    assert utils.read_cookies() == {"cli": True}
    assert opened and opened[-1].endswith("cookies.json")
