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
