import sys
import time
from pathlib import Path

import pytest
import streamlink

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import web.app as app_module  # noqa: E402
from web.app import create_app  # noqa: E402


def make_app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "state.sqlite3"),
            "RECORDINGS_DIR": str(tmp_path / "recordings"),
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
    monkeypatch.setattr(app_module, "_resolve_live", lambda username: False)
    response = client.post("/api/recordings", json={"query": "@creator"})
    assert response.status_code == 200
    assert response.json == {"status": "offline", "username": "creator"}
    assert app.extensions["tt_clipper_store"].recent_searches() == ["creator"]

    monkeypatch.setattr(app_module, "_resolve_live", lambda username: True)
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
    assert response.json == {"status": "offline", "username": "creator"}

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
        app_module, "_resolve_live", lambda username: pytest.fail("must not check live")
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
    assert response.json["duration_seconds"] == 1800
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
    assert response.json["recent"][0] == "creator11"


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
    assert response.json == {"recent": ["recent_user"], "results": []}


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
    assert response.status_code == 502
    assert response.json == {
        "error": "TikTok profile search is temporarily unavailable"
    }
