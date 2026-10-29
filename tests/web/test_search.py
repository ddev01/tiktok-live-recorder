import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from web import search  # noqa: E402


def test_profile_search_parses_tiktok_response_without_live_requests(monkeypatch):
    calls = []

    class FakeClient:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "status_code": 0,
                    "user_list": [
                        {
                            "user_info": {
                                "unique_id": "creator",
                                "nickname": "Creator",
                                "avatar_larger": {
                                    "url_list": ["https://img.test/avatar"]
                                },
                                "is_live": 1,
                            }
                        }
                    ],
                },
            )

    monkeypatch.setattr(search, "read_cookies", lambda: {})
    monkeypatch.setattr(
        search, "HttpClient", lambda **kwargs: SimpleNamespace(req=FakeClient())
    )
    assert search.search_profiles("creator") == [
        {
            "username": "creator",
            "display_name": "Creator",
            "avatar_url": "https://img.test/avatar",
            "is_live": True,
        }
    ]
    assert calls[0][1]["params"]["keyword"] == "creator"


def test_cache_profile_avatar_writes_image_bytes(tmp_path, monkeypatch):
    class FakeClient:
        def get(self, url, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                text='<meta property="og:image" content="https://p16-sign.tiktokcdn.com/pic.jpg">',
            )

    image_calls = []

    def fake_get(url, **kwargs):
        image_calls.append(url)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            headers={"Content-Type": "image/jpeg"},
            content=b"\xff\xd8\xff",
        )

    monkeypatch.setattr(search, "read_cookies", lambda: {"session_ss": "secret"})
    monkeypatch.setattr(
        search, "HttpClient", lambda **kwargs: SimpleNamespace(req=FakeClient())
    )
    monkeypatch.setattr(search.requests, "get", fake_get)
    dest = search.cache_profile_avatar("creator", tmp_path / "avatars")
    assert dest.read_bytes() == b"\xff\xd8\xff"
    assert image_calls == ["https://p16-sign.tiktokcdn.com/pic.jpg"]


def test_cache_profile_avatar_skips_offsite_image(tmp_path, monkeypatch):
    class FakeClient:
        def get(self, url, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                text='<meta property="og:image" content="https://img.test/pic.jpg">',
            )

    monkeypatch.setattr(search, "read_cookies", lambda: {"session_ss": "secret"})
    monkeypatch.setattr(
        search, "HttpClient", lambda **kwargs: SimpleNamespace(req=FakeClient())
    )
    monkeypatch.setattr(
        search.requests,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    assert search.cache_profile_avatar("creator", tmp_path / "avatars") is None
