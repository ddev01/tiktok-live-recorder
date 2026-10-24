import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from web.store import Store  # noqa: E402
from web import worker as worker_module  # noqa: E402
from web.worker import LOW_DISK_BYTES, RETENTION_SECONDS, RecordingWorker  # noqa: E402


def make_worker(tmp_path, is_live=lambda username: False):
    store = Store(tmp_path / "state.sqlite3")
    recordings_dir = tmp_path / "recordings"
    worker = RecordingWorker(store, recordings_dir, is_live)
    return worker, store, recordings_dir


def test_low_disk_blocks_recording_before_starting_a_child(tmp_path, monkeypatch):
    worker, _, _ = make_worker(tmp_path)
    monkeypatch.setattr(
        worker,
        "disk_status",
        lambda: {"free_bytes": LOW_DISK_BYTES - 1, "low_disk": True},
    )
    monkeypatch.setattr(
        "web.worker.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("must not start with low disk"),
    )
    recording = worker.enqueue("creator")
    assert worker.launch_queued(recording["id"]) is False
    assert worker.store.recording(recording["id"])["status"] == "failed"


def test_http_queue_is_started_only_by_worker_service(tmp_path, monkeypatch):
    worker, store, _ = make_worker(tmp_path)
    recording = worker.enqueue("creator", 1800)
    assert recording["status"] == "starting"
    assert recording["pid"] is None

    class Child:
        pid = 456

        def poll(self):
            return None

    monkeypatch.setattr(worker, "disk_status", lambda: {"low_disk": False})
    monkeypatch.setattr("web.worker.subprocess.Popen", lambda *args, **kwargs: Child())
    worker.launch_pending()
    started = store.recording(recording["id"])
    assert started["status"] == "recording"
    assert started["pid"] == 456


def test_store_deduplicates_jobs_with_partial_active_index(tmp_path):
    _, store, _ = make_worker(tmp_path)
    first, created = store.enqueue_recording("creator", 1800)
    duplicate, duplicate_created = store.enqueue_recording("creator", 900)
    assert created is True
    assert duplicate_created is False
    assert duplicate["id"] == first["id"]
    with store.connect() as connection:
        index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'one_active_recording_per_user'"
        ).fetchone()[0]
    assert "WHERE status IN" in index


def test_low_disk_gracefully_stops_an_active_child(tmp_path):
    worker, store, _ = make_worker(tmp_path)
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, started_at, pid) "
            "VALUES ('creator', 'recording', 1, 1, 456)"
        )
        recording_id = cursor.lastrowid

    class Child:
        def __init__(self):
            self.signals = []

        def send_signal(self, sig):
            self.signals.append(sig)

        def poll(self):
            return None

    child = Child()
    worker.processes[recording_id] = child
    worker.disk_status = lambda: {"low_disk": True}
    worker._monitor()
    assert child.signals == [worker_module.signal.SIGINT]
    assert store.recording(recording_id)["status"] == "stopping"


def test_worker_discovers_growing_flv_file_for_recording_detail(tmp_path):
    worker, store, recordings_dir = make_worker(tmp_path)
    output = recordings_dir / "TK_creator_2026.01.01_00-00-00_flv.mp4"
    output.write_bytes(b"growing video")
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, started_at, "
            "duration_seconds, pid) VALUES ('creator', 'recording', ?, ?, 1800, 456)",
            (time.time() - 10, time.time() - 10),
        )
        recording_id = cursor.lastrowid

    class Child:
        def poll(self):
            return None

    worker.processes[recording_id] = Child()
    worker.disk_status = lambda: {"low_disk": False}
    worker._monitor()
    assert store.recording(recording_id)["file_path"] == str(output)


def test_worker_reads_persisted_duration_changes_each_poll(tmp_path, monkeypatch):
    worker, store, _ = make_worker(tmp_path)
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, started_at, "
            "duration_seconds, pid) VALUES ('creator', 'recording', 100, 100, 1800, 123)"
        )
        recording_id = cursor.lastrowid
    worker.processes[recording_id] = SimpleNamespace(poll=lambda: None)
    worker.disk_status = lambda: {"low_disk": False}
    monkeypatch.setattr("web.worker.time", SimpleNamespace(time=lambda: 1000))
    stopped = []
    worker.stop_recording = stopped.append

    worker._monitor()
    assert not stopped
    with store.connect() as connection:
        connection.execute(
            "UPDATE recordings SET duration_seconds = 900 WHERE id = ?", (recording_id,)
        )
    worker._monitor()
    assert stopped == [recording_id]


def test_retention_deletes_old_unpinned_video_but_keeps_keep_forever(tmp_path):
    worker, store, recordings_dir = make_worker(tmp_path)
    old_time = time.time() - RETENTION_SECONDS - 10
    removed = recordings_dir / "old.mp4"
    kept = recordings_dir / "kept.mp4"
    removed.write_bytes(b"video")
    kept.write_bytes(b"video")
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO recordings(username, status, created_at, finished_at, file_path) "
            "VALUES ('old', 'completed', ?, ?, ?)",
            (old_time, old_time, str(removed)),
        )
        connection.execute(
            "INSERT INTO recordings(username, status, created_at, finished_at, file_path, keep_forever) "
            "VALUES ('kept', 'completed', ?, ?, ?, 1)",
            (old_time, old_time, str(kept)),
        )
    worker._retention()
    assert not removed.exists()
    assert kept.exists()
    assert [item["username"] for item in store.recordings()] == ["kept"]


def test_watchlist_once_consumes_and_always_uses_max_duration(tmp_path):
    live = {"once-user": False, "always-user": False}
    worker, store, _ = make_worker(tmp_path, is_live=lambda username: live[username])
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO watchlist(username, mode, duration_seconds, max_duration_seconds, created_at) "
            "VALUES ('once-user', 'once', 1800, 14400, 1)"
        )
        connection.execute(
            "INSERT INTO watchlist(username, mode, duration_seconds, max_duration_seconds, created_at) "
            "VALUES ('always-user', 'always', 14400, 3600, 1)"
        )
    launched = []
    worker.enqueue = lambda username, duration: (
        launched.append((username, duration)) or ({}, True)
    )
    worker._check_watchlist()
    assert launched == []
    live.update({"once-user": True, "always-user": True})
    worker._check_watchlist()
    assert launched == [("once-user", 1800), ("always-user", 3600)]
    assert [item["username"] for item in store.watchlist()] == ["always-user"]
    worker._check_watchlist()
    assert len(launched) == 2


def test_shutdown_signals_active_recorders_for_graceful_finalize(tmp_path, monkeypatch):
    worker, store, _ = make_worker(tmp_path)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO recordings(username, status, created_at, started_at, pid) "
            "VALUES ('creator', 'recording', 1, 1, 456)"
        )
    events = []

    class Child:
        def send_signal(self, sig):
            events.append(("signal", sig))

        def wait(self):
            events.append(("wait",))

        def poll(self):
            return 0

    worker.processes[1] = Child()
    worker.request_shutdown()
    worker.shutdown()
    assert events == [("signal", worker_module.signal.SIGINT), ("wait",)]
    assert store.recording(1)["status"] == "failed"


def test_recovery_never_signals_pid_from_a_previous_container(tmp_path, monkeypatch):
    worker, store, _ = make_worker(tmp_path)
    with store.connect() as connection:
        connection.execute(
            "INSERT INTO recordings(username, status, created_at, started_at, pid) "
            "VALUES ('creator', 'recording', 1, 1, 98765)"
        )
    monkeypatch.setattr(
        worker_module.os,
        "kill",
        lambda *args: pytest.fail("a persisted PID may have been reused"),
    )

    worker.recover_orphans()

    recording = store.recordings()[0]
    assert recording["status"] == "failed"
    assert recording["pid"] is None
    assert "partial output" in recording["error"]


def test_retention_keeps_metadata_when_video_cannot_be_removed(tmp_path, monkeypatch):
    worker, store, recordings_dir = make_worker(tmp_path)
    old_time = time.time() - RETENTION_SECONDS - 10
    video = recordings_dir / "locked.mp4"
    video.write_bytes(b"video")
    with store.connect() as connection:
        cursor = connection.execute(
            "INSERT INTO recordings(username, status, created_at, finished_at, file_path) "
            "VALUES ('locked', 'completed', ?, ?, ?)",
            (old_time, old_time, str(video)),
        )
        recording_id = cursor.lastrowid
    original_unlink = Path.unlink

    def fail_video_unlink(path, *args, **kwargs):
        if path == video:
            raise PermissionError
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_video_unlink)

    worker._retention()

    assert store.recording(recording_id) is not None
    assert video.exists()
