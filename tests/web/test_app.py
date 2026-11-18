import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest
import streamlink

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import web.app as app_module  # noqa: E402
from web.app import create_app  # noqa: E402


def make_app(tmp_path, **config):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "state.sqlite3"),
            "RECORDINGS_DIR": str(tmp_path / "recordings"),
            **config,
        }
    )


def test_recording_validates_query_duration_and_url(tmp_path):
    client = make_app(tmp_path).test_client()
    assert (
        client.post("/api/recordings", json={"query": "not a user!"}).status_code == 400
    )
    assert (
        client.post(
            "/api/recordings", json={"query": "creator", "duration_seconds": 1200}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/recordings", json={"query": "https://example.com/@creator/live"}
        ).status_code
        == 400
    )


def test_recording_offline_and_live_submission(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    client = app.test_client()
    monkeypatch.setattr(app_module, "_resolve_live", lambda username, platform="tiktok": False)
    response = client.post("/api/recordings", json={"query": "@creator"})
    assert response.status_code == 200
    assert response.json == {
        "status": "offline",
        "username": "creator",
        "platform": "tiktok",
    }
    assert app.extensions["tt_clipper_store"].recent_searches() == [
        {"username": "creator", "nickname": None, "platform": "tiktok"}
    ]

    monkeypatch.setattr(app_module, "_resolve_live", lambda username, platform="tiktok": True)
    monkeypatch.setattr(
        "web.worker.StreamCapture",
        lambda *args, **kwargs: pytest.fail(
            "HTTP app must not start recorder children"
        ),
    )
    response = client.post(
        "/api/recordings",
        json={"query": "https://www.tiktok.com/@creator/live", "duration_seconds": 900},
    )
    assert response.status_code == 201
    assert response.json["username"] == "creator"
    assert response.json["status"] == "starting"
    assert response.json["duration_seconds"] == 900
    assert "pid" not in response.json


def test_recording_live_check_uses_streamlink_without_network(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    calls = []
    state = {"streams": {}}
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setenv("TIKTOK_SESSION_SS_FILE", str(cookie_path))

    class FakeStreamlink:
        def __init__(self):
            self.http = type(
                "FakeHTTP",
                (),
                {"set_cookies_from_file": calls.append, "close": lambda self: None},
            )()

        def streams(self, url):
            calls.append(url)
            return state["streams"]

    monkeypatch.setattr(streamlink, "Streamlink", FakeStreamlink)
    client = app.test_client()
    response = client.post("/api/recordings", json={"query": "creator"})
    assert response.json == {
        "status": "offline",
        "username": "creator",
        "platform": "tiktok",
    }

    state["streams"] = {"best": object()}
    response = client.post("/api/recordings", json={"query": "creator"})
    assert response.status_code == 201
    assert response.json["status"] == "starting"
    assert calls == [
        str(cookie_path),
        "https://www.tiktok.com/@creator/live",
        str(cookie_path),
        "https://www.tiktok.com/@creator/live",
    ]


def test_duplicate_returns_existing_active_recording(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO recordings(username, status, created_at, started_at, duration_seconds) "
            "VALUES ('creator', 'recording', 1, 1, 1800)"
        )
    monkeypatch.setattr(
        app_module, "_resolve_live", lambda username, platform="tiktok": pytest.fail("must not check live")
    )
    response = app.test_client().post("/api/recordings", json={"query": "creator"})
    assert response.status_code == 200
    assert response.json["username"] == "creator"
    assert response.json["status"] == "recording"


def test_duration_patch_and_watchlist_validation(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    with app.extensions["tt_clipper_store"].connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, started_at, duration_seconds) "
            "VALUES ('creator', 'recording', 1, 1, 1800)"
        )
        recording_id = cursor.lastrowid
    response = client.patch(
        f"/api/recordings/{recording_id}", json={"duration_seconds": None}
    )
    assert response.status_code == 200
    assert response.json["duration_seconds"] is None
    response = client.patch(
        f"/api/recordings/{recording_id}", json={"duration_seconds": 30}
    )
    assert response.status_code == 200
    assert response.json["duration_seconds"] == 30
    assert response.json["keep_forever"] is False
    assert (
        client.patch(
            f"/api/recordings/{recording_id}", json={"keep_forever": 1}
        ).status_code
        == 400
    )
    assert (
        client.patch(
            f"/api/recordings/{recording_id}", json={"keep_forever": "true"}
        ).status_code
        == 400
    )
    response = client.patch(
        f"/api/recordings/{recording_id}", json={"keep_forever": True}
    )
    assert response.status_code == 200
    assert response.json["keep_forever"] is True
    assert "pinned" not in response.json
    assert client.get(f"/api/recordings/{recording_id}").json["keep_forever"] is True
    assert client.get("/api/recordings").json["recordings"][0]["keep_forever"] is True
    response = client.patch(
        f"/api/recordings/{recording_id}", json={"keep_forever": False}
    )
    assert response.status_code == 200
    assert response.json["keep_forever"] is False
    assert (
        client.post(
            "/api/watchlist", json={"username": "creator", "mode": "sometimes"}
        ).status_code
        == 400
    )
    response = client.post(
        "/api/watchlist", json={"username": "onceuser", "mode": "once"}
    )
    assert response.status_code == 201
    assert response.json["duration_seconds"] == 1320
    response = client.post(
        "/api/watchlist", json={"username": "creator", "mode": "always"}
    )
    assert response.status_code == 201
    assert response.json["max_duration_seconds"] == 14400
    response = client.post(
        "/api/watchlist",
        json={"username": "creator", "mode": "always", "max_duration_seconds": 28800},
    )
    assert response.status_code == 200
    assert response.json["max_duration_seconds"] == 28800


def test_video_endpoint_supports_byte_ranges_and_delete_is_db_scoped(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    video = Path(app.config["RECORDINGS_DIR"]) / "clip.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"0123456789")
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, finished_at, file_path) "
            "VALUES ('creator', 'completed', 1, 1, ?)",
            (str(video),),
        )
        recording_id = cursor.lastrowid
    client = app.test_client()
    response = client.get(
        f"/recordings/{recording_id}/video", headers={"Range": "bytes=2-5"}
    )
    assert response.status_code == 206
    assert response.data == b"2345"
    assert response.headers["Content-Range"] == "bytes 2-5/10"
    assert client.delete(f"/api/recordings/{recording_id}").status_code == 204
    assert not video.exists()

    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"private")
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, finished_at, file_path) "
            "VALUES ('other', 'completed', 1, 1, ?)",
            (str(outside),),
        )
        outside_id = cursor.lastrowid
    assert client.get(f"/recordings/{outside_id}/video").status_code == 404
    assert outside.exists()


def test_in_progress_flv_is_not_served_as_a_completed_mp4(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    partial = Path(app.config["RECORDINGS_DIR"]) / "TK_creator_live_flv.mp4"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial flv data")
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, file_path) "
            "VALUES ('creator', 'recording', 1, ?)",
            (str(partial),),
        )
        recording_id = cursor.lastrowid

    assert app.test_client().get(f"/recordings/{recording_id}/video").status_code == 404


def test_interrupted_partial_mp4_is_playable_only_after_worker_finishes(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    video = Path(app.config["RECORDINGS_DIR"]) / "partial.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"partial video")
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, finished_at, file_path, error) "
            "VALUES ('creator', 'interrupted', 1, 2, ?, 'stream interrupted')",
            (str(video),),
        )
        recording_id = cursor.lastrowid
    client = app.test_client()
    assert client.get(f"/recordings/{recording_id}/video").data == b"partial video"

    with store.connect() as connection:
        connection.execute(
            "UPDATE recordings SET status = 'recording', finished_at = NULL "
            "WHERE id = ?",
            (recording_id,),
        )
    assert client.get(f"/recordings/{recording_id}/video").status_code == 404


def test_interrupted_finalized_mp4_is_visible_and_playable(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    partial = Path(app.config["RECORDINGS_DIR"]) / "partial.mp4"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"finalized partial mp4")
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, finished_at, "
            "file_path, error) VALUES ('creator', 'interrupted', 1, 2, ?, ?)",
            (str(partial), "Streamlink lost the active stream"),
        )
        recording_id = cursor.lastrowid

    client = app.test_client()
    detail = client.get(f"/api/recordings/{recording_id}").json
    assert detail["status"] == "interrupted"
    assert detail["file_name"] == "partial.mp4"
    assert detail["error"] == "Streamlink lost the active stream"
    assert client.get(f"/recordings/{recording_id}/video").status_code == 200
    assert client.delete(f"/api/recordings/{recording_id}").status_code == 204


def test_interrupted_record_without_finalized_mp4_is_not_exposed(tmp_path):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    path = Path(app.config["RECORDINGS_DIR"]) / "missing.mp4"
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, finished_at, file_path) "
            "VALUES ('creator', 'interrupted', 1, 2, ?)",
            (str(path),),
        )
        recording_id = cursor.lastrowid

    client = app.test_client()
    assert client.get(f"/api/recordings/{recording_id}").json["file_name"] is None
    assert client.get(f"/recordings/{recording_id}/video").status_code == 404


def test_system_reports_low_disk_warning(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(
        app_module,
        "disk_status",
        lambda path: {"free_bytes": 1, "total_bytes": 100, "low_disk": True},
    )
    response = app.test_client().get("/api/system")
    assert response.status_code == 200
    assert response.json["disk"]["warning"] is True


def test_recording_json_reports_growth_estimates_and_low_space_warning(
    tmp_path, monkeypatch
):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    video = Path(app.config["RECORDINGS_DIR"]) / "TK_creator_live_flv.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x" * 100_000)
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, started_at, "
            "duration_seconds, file_path) VALUES ('creator', 'recording', ?, ?, 1800, ?)",
            (time.time() - 20, time.time() - 20, str(video)),
        )
        recording_id = cursor.lastrowid
    monkeypatch.setattr(
        app_module,
        "disk_status",
        lambda path: {"free_bytes": app_module.LOW_DISK_BYTES + 1000},
    )
    recording = app.test_client().get(f"/api/recordings/{recording_id}").json
    assert recording["elapsed_seconds"] >= 19
    assert recording["file_size_bytes"] == 100_000
    assert recording["observed_bitrate_bps"] > 0
    assert recording["estimated_total_bytes"] > recording["file_size_bytes"]
    assert recording["remaining_seconds"] > 0
    assert recording["low_space_warning"]


def test_search_recent_list_returns_ten_usernames(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    for i in range(12):
        store.remember_search(f"creator{i}", i)
    monkeypatch.setattr(
        app_module,
        "search_profiles",
        lambda query: [
            {
                "username": f"creator{i}",
                "display_name": f"Creator {i}",
                "avatar_url": None,
                "is_live": False,
            }
            for i in range(12)
        ],
    )
    response = app.test_client().get("/api/search?q=creator")
    assert response.status_code == 200
    assert len(response.json["recent"]) == 10
    assert response.json["recent"][0] == {
        "username": "creator11",
        "display_name": "creator11",
        "platform": "tiktok",
        "avatar_url": None,
    }


def test_empty_search_returns_recent_without_requesting_tiktok(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    store.remember_search("recent_user", time.time())
    monkeypatch.setattr(
        app_module,
        "search_profiles",
        lambda query: pytest.fail("empty search must not call TikTok"),
    )
    response = app.test_client().get("/api/search")
    assert response.status_code == 200
    assert response.json == {
        "recent": [
            {
                "username": "recent_user",
                "display_name": "recent_user",
                "platform": "tiktok",
                "avatar_url": None,
            }
        ],
        "results": [],
    }


def test_app_creation_does_not_initialize_storage_or_start_worker(
    tmp_path, monkeypatch
):
    app = create_app(
        {
            "DATABASE": str(tmp_path / "state.sqlite3"),
            "RECORDINGS_DIR": str(tmp_path / "recordings"),
        }
    )
    monkeypatch.setattr(app_module, "render_template", lambda name, **context: name)
    assert app.test_client().get("/").data == b"home.html"
    assert not (tmp_path / "state.sqlite3").exists()
    assert not (tmp_path / "recordings").exists()


def test_search_errors_are_returned_without_leaking_details(tmp_path, monkeypatch):
    def fail(query):
        raise RuntimeError("private response details")

    monkeypatch.setattr(app_module, "search_profiles", fail)
    response = make_app(tmp_path).test_client().get("/api/search?q=creator")
    assert response.status_code == 200
    assert "private response details" not in str(response.json)
    assert response.json["results"] == []


def test_unknown_query_uses_profile_search(tmp_path, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "search_profiles",
        lambda query: [
            {
                "username": "new_creator",
                "display_name": "New",
                "avatar_url": "https://img.test/a",
                "is_live": True,
            }
        ],
    )
    response = make_app(tmp_path).test_client().get("/api/search?q=new_cr")
    assert response.status_code == 200
    assert response.json["results"] == [
        {
            "username": "new_creator",
            "display_name": "New",
            "platform": "tiktok",
            "avatar_url": "https://img.test/a",
            "is_live": True,
        }
    ]


def test_search_matches_known_usernames_without_tiktok(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    store.remember_search("n1tr01l", time.time())
    monkeypatch.setattr(
        app_module,
        "search_profiles",
        lambda query: pytest.fail("known matches must not require TikTok"),
    )
    response = app.test_client().get("/api/search?q=n1tr")
    assert response.status_code == 200
    assert [item["username"] for item in response.json["results"]] == ["n1tr01l"]


def test_search_known_ranks_prefix_above_contains(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    store.remember_search("bobuser", 1)
    store.remember_search("xbob", 2)
    store.remember_search("bobby", 3)
    monkeypatch.setattr(app_module, "search_profiles", lambda query: [])
    response = app.test_client().get("/api/search?q=bob")
    assert [item["username"] for item in response.json["results"]] == [
        "bobby",
        "bobuser",
        "xbob",
    ]


def test_following_matches_substrings_and_typos(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    assert store.replace_following(
        ["Demo_Wie_Clip", "@synthetic_stewie_account", "other_sample"]
    ) == 3
    monkeypatch.setattr(app_module, "search_profiles", lambda query: [])
    client = app.test_client()
    expected = {
        "wie": "demo_wie_clip",
        "stewei": "synthetic_stewie_account",
        "demowia": "demo_wie_clip",
        "demowue": "demo_wie_clip",
    }
    for query, wanted in expected.items():
        names = [
            item["username"]
            for item in client.get(f"/api/search?q={query}").json["results"]
        ]
        assert wanted in names, query
    assert store.search_known("wie", "twitch") == []


def test_following_searches_and_displays_nicknames(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    assert (
        store.replace_following(
            [
                {"username": "synthetic_handle_9x", "nickname": "sample_nick"},
                "plain_handle",
            ]
        )
        == 2
    )
    monkeypatch.setattr(app_module, "search_profiles", lambda query: [])
    client = app.test_client()

    by_nickname = client.get("/api/search?q=sample_nick").json["results"]
    assert by_nickname[0]["username"] == "synthetic_handle_9x"
    assert by_nickname[0]["display_name"] == "sample_nick"

    by_handle = client.get("/api/search?q=handle_9").json["results"]
    assert by_handle[0]["display_name"] == "sample_nick"

    plain = client.get("/api/search?q=plain").json["results"]
    assert plain[0]["display_name"] == "plain_handle"


def test_recent_search_shows_and_matches_nickname(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    store = app.extensions["tt_clipper_store"]
    store.remember_search("recent_handle", time.time())
    store.set_nickname("recent_handle", "tiktok", "Recent Nick")
    store.remember_search("recent_handle", time.time() + 1)
    assert store.recent_searches("tiktok")[0]["nickname"] == "Recent Nick"

    monkeypatch.setattr(app_module, "search_profiles", lambda query: [])
    client = app.test_client()
    recent = client.get("/api/search?q=&platform=tiktok").json["recent"]
    assert recent[0]["display_name"] == "Recent Nick"

    results = client.get("/api/search?q=recent_nick&platform=tiktok").json["results"]
    assert results[0]["username"] == "recent_handle"
    assert results[0]["display_name"] == "Recent Nick"


def test_avatar_endpoint_serves_cached_file_only(tmp_path):
    app = make_app(tmp_path)
    avatars = Path(app.config["DATABASE"]).resolve().parent / "avatars"
    avatars.mkdir()
    (avatars / "creator").write_bytes(b"\xff\xd8\xff")
    client = app.test_client()
    missing = client.get("/api/avatars/missing_user")
    assert missing.status_code == 404
    assert client.get("/api/avatars/..").status_code == 404
    found = client.get("/api/avatars/creator")
    assert found.status_code == 200
    assert found.data == b"\xff\xd8\xff"


def test_twitch_query_records_without_using_tiktok_search(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    client = app.test_client()
    monkeypatch.setattr(
        app_module,
        "search_profiles",
        lambda query: pytest.fail("twitch search must not call TikTok"),
    )
    monkeypatch.setattr(
        app_module, "_resolve_live", lambda username, platform="tiktok": False
    )
    search = client.get("/api/search?q=test_user_01&platform=twitch")
    assert search.status_code == 200
    assert search.json["results"] == [
        {
            "username": "test_user_01",
            "display_name": "test_user_01",
            "platform": "twitch",
            "avatar_url": None,
            "is_live": None,
        }
    ]
    response = client.post(
        "/api/recordings",
        json={"query": "https://www.twitch.tv/test_user_01", "platform": "tiktok"},
    )
    assert response.status_code == 200
    assert response.json == {
        "status": "offline",
        "username": "test_user_01",
        "platform": "twitch",
    }
    monkeypatch.setattr(
        app_module, "_resolve_live", lambda username, platform="tiktok": platform == "twitch"
    )
    started = client.post(
        "/api/recordings", json={"query": "test_user_01", "platform": "twitch"}
    )
    assert started.status_code == 201
    assert started.json["username"] == "test_user_01"
    assert started.json["platform"] == "twitch"
    assert started.json["status"] == "starting"


def test_same_handle_can_record_on_both_platforms(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    monkeypatch.setattr(
        app_module, "_resolve_live", lambda username, platform="tiktok": True
    )
    client = app.test_client()
    tiktok = client.post("/api/recordings", json={"query": "creator"})
    twitch = client.post(
        "/api/recordings", json={"query": "creator", "platform": "twitch"}
    )
    assert tiktok.status_code == 201
    assert twitch.status_code == 201
    assert tiktok.json["id"] != twitch.json["id"]
    assert tiktok.json["platform"] == "tiktok"
    assert twitch.json["platform"] == "twitch"


def signed_game_post(
    client, payload, secret, timestamp=None, token="test-token", path="/api/game/players"
):
    body = json.dumps(payload).encode()
    sent_at = str(int(time.time() if timestamp is None else timestamp))
    signature = hmac.new(
        secret.encode(), sent_at.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return client.post(
        path,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-TT-Token": token,
            "X-TT-Timestamp": sent_at,
            "X-TT-Signature": signature,
        },
    )


def test_game_webhook_requires_a_valid_token_and_signature(tmp_path):
    app = make_app(
        tmp_path, GAME_WEBHOOK_SECRET="test-secret", GAME_WEBHOOK_TOKEN="test-token"
    )
    client = app.test_client()
    payload = {"players": [{"platform": "twitch", "username": "creator"}]}
    assert client.post("/api/game/players", json=payload).status_code == 401
    assert (
        client.post(
            "/api/game/players",
            json=payload,
            headers={
                "X-TT-Token": "wrong-token",
                "X-TT-Timestamp": str(int(time.time())),
                "X-TT-Signature": "0" * 64,
            },
        ).status_code
        == 401
    )
    assert (
        signed_game_post(
            client, payload, "test-secret", token="wrong-token"
        ).status_code
        == 401
    )
    assert (
        signed_game_post(client, payload, "test-secret", time.time() - 301).status_code
        == 401
    )
    assert (
        signed_game_post(client, payload, "wrong-secret").status_code == 401
    )


def test_game_webhook_is_disabled_without_both_credentials(tmp_path):
    client = make_app(tmp_path).test_client()
    assert (
        client.post("/api/game/players", json={"players": []}).status_code == 503
    )
    client = make_app(
        tmp_path / "secret-only", GAME_WEBHOOK_SECRET="test-secret"
    ).test_client()
    assert (
        client.post("/api/game/players", json={"players": []}).status_code == 503
    )


def test_game_webhook_queues_multiple_players_with_default_duration(
    tmp_path, monkeypatch
):
    app = make_app(
        tmp_path, GAME_WEBHOOK_SECRET="test-secret", GAME_WEBHOOK_TOKEN="test-token"
    )
    monkeypatch.setattr(
        app_module, "_resolve_live", lambda username, platform="tiktok": True
    )
    payload = {
        "players": [
            {"platform": "Twitch", "username": "Test_User"},
            {"platform": "TikTok", "username": "@Creator"},
            {"platform": "twitch", "username": "test_user"},
        ]
    }
    response = signed_game_post(app.test_client(), payload, "test-secret")
    assert response.status_code == 200
    results = response.json["results"]
    assert [(item["username"], item["platform"]) for item in results] == [
        ("test_user", "twitch"),
        ("creator", "tiktok"),
    ]
    assert all(item["status"] == "starting" for item in results)
    assert all(item["duration_seconds"] == 1320 for item in results)
    recordings = app.extensions["tt_clipper_store"].recordings()
    assert {(item["username"], item["platform"]) for item in recordings} == {
        ("test_user", "twitch"),
        ("creator", "tiktok"),
    }
    assert all(item["duration_seconds"] == 1320 for item in recordings)


def test_game_webhook_rejects_an_invalid_username(tmp_path):
    app = make_app(
        tmp_path, GAME_WEBHOOK_SECRET="test-secret", GAME_WEBHOOK_TOKEN="test-token"
    )
    response = signed_game_post(
        app.test_client(),
        {"players": [{"platform": "twitch", "username": "not a user!"}]},
        "test-secret",
    )
    assert response.status_code == 400
    assert not app.extensions["tt_clipper_store"].recordings()


def _game_event(**overrides):
    event = {
        "platform": "twitch",
        "username": "bop123",
        "kind": "elimination",
        "event_at": time.time(),
        "actor": "Sophie123",
        "actor_role": "teammate",
        "victim": "TTV_bop123",
        "victim_role": "streamer",
    }
    event.update(overrides)
    return event


def _started_recording(tmp_path, monkeypatch, started_at):
    app = make_app(
        tmp_path,
        GAME_WEBHOOK_SECRET="test-secret",
        GAME_WEBHOOK_TOKEN="test-token",
    )
    monkeypatch.setattr(
        app_module, "_resolve_live", lambda username, platform="tiktok": True
    )
    client = app.test_client()
    response = client.post(
        "/api/recordings", json={"query": "bop123", "platform": "twitch"}
    )
    assert response.status_code == 201
    recording_id = response.json["id"]
    with app.extensions["tt_clipper_store"].connect() as connection:
        connection.execute(
            "UPDATE recordings SET status = 'recording', created_at = ?, "
            "started_at = ? WHERE id = ?",
            (started_at, started_at, recording_id),
        )
    return app, client, recording_id


def test_game_events_require_a_valid_token_and_signature(tmp_path):
    app = make_app(
        tmp_path, GAME_WEBHOOK_SECRET="test-secret", GAME_WEBHOOK_TOKEN="test-token"
    )
    client = app.test_client()
    payload = {"events": [_game_event()]}
    path = "/api/game/events"
    assert client.post(path, json=payload).status_code == 401
    assert (
        signed_game_post(
            client,
            payload,
            "test-secret",
            token="wrong-token",
            path=path,
        ).status_code
        == 401
    )
    assert (
        signed_game_post(
            client, payload, "wrong-secret", path=path
        ).status_code
        == 401
    )
    assert (
        signed_game_post(
            client, payload, "test-secret", time.time() - 301, path=path
        ).status_code
        == 401
    )


def test_game_events_are_disabled_without_both_credentials(tmp_path):
    client = make_app(tmp_path).test_client()
    assert (
        client.post("/api/game/events", json={"events": []}).status_code == 503
    )


def test_game_events_are_stored_with_offsets_and_seek_times(tmp_path, monkeypatch):
    now = time.time()
    app, client, recording_id = _started_recording(
        tmp_path, monkeypatch, now - 400
    )
    payload = {
        "events": [
            _game_event(event_at=now - 10, kind="knock"),
            _game_event(
                event_at=now - 5,
                actor="TTV_bop123",
                actor_role="streamer",
                victim="Sophie123",
                victim_role="teammate",
            ),
        ]
    }
    response = signed_game_post(
        client, payload, "test-secret", path="/api/game/events"
    )
    assert response.status_code == 200
    assert [item["status"] for item in response.json["results"]] == [
        "stored",
        "stored",
    ]
    assert all(
        item["recording_id"] == recording_id for item in response.json["results"]
    )

    events = client.get(f"/api/recordings/{recording_id}/events").json["events"]
    assert [event["kind"] for event in events] == ["knock", "elimination"]
    assert [event["actor_role"] for event in events] == ["teammate", "streamer"]
    assert events[0]["offset_seconds"] == 390
    assert events[0]["seek_seconds"] == 380
    assert events[1]["offset_seconds"] == 395
    assert events[1]["seek_seconds"] == 385

    duplicate = signed_game_post(
        client, payload, "test-secret", path="/api/game/events"
    )
    assert [item["status"] for item in duplicate.json["results"]] == [
        "duplicate",
        "duplicate",
    ]
    assert len(client.get(f"/api/recordings/{recording_id}/events").json["events"]) == 2


def test_game_events_ignore_accounts_without_a_recording(tmp_path):
    app = make_app(
        tmp_path, GAME_WEBHOOK_SECRET="test-secret", GAME_WEBHOOK_TOKEN="test-token"
    )
    response = signed_game_post(
        app.test_client(),
        {"events": [_game_event()]},
        "test-secret",
        path="/api/game/events",
    )
    assert response.status_code == 200
    assert response.json["results"] == [
        {
            "platform": "twitch",
            "username": "bop123",
            "status": "ignored",
            "reason": "no_recording",
        }
    ]
    assert app.extensions["tt_clipper_store"].recording_events(1) == []


def test_game_events_are_deleted_with_the_recording(tmp_path, monkeypatch):
    now = time.time()
    app, client, recording_id = _started_recording(
        tmp_path, monkeypatch, now - 100
    )
    assert (
        signed_game_post(
            client,
            {"events": [_game_event(event_at=now - 5)]},
            "test-secret",
            path="/api/game/events",
        ).status_code
        == 200
    )
    with app.extensions["tt_clipper_store"].connect() as connection:
        connection.execute(
            "UPDATE recordings SET status = 'completed', finished_at = ? WHERE id = ?",
            (now, recording_id),
        )
    assert client.delete(f"/api/recordings/{recording_id}").status_code == 204
    assert app.extensions["tt_clipper_store"].recording_events(recording_id) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"kind": "kill"},
        {"event_at": "now"},
        {"event_at": time.time() + 601},
        {"actor": ""},
        {"username": "not a user!"},
        {"actor_role": "streamer", "victim_role": "streamer_teammate"},
        {"actor_role": "self", "victim_role": "teammate"},
        {"actor_role": "unknown", "victim_role": "streamer"},
    ],
)
def test_game_events_reject_invalid_payloads(tmp_path, overrides):
    app = make_app(
        tmp_path, GAME_WEBHOOK_SECRET="test-secret", GAME_WEBHOOK_TOKEN="test-token"
    )
    response = signed_game_post(
        app.test_client(),
        {"events": [_game_event(**overrides)]},
        "test-secret",
        path="/api/game/events",
    )
    assert response.status_code == 400
