import os
import shutil
import signal
import threading
import time
import urllib.request
from pathlib import Path

from web.capture import StreamCapture
from web.search import avatar_file, cache_profile_avatar
from web.store import Store


LOW_DISK_BYTES = 2 * 1024**3
RETENTION_SECONDS = 7 * 24 * 60 * 60
HEALTHCHECK_TIMEOUT = 5


def healthcheck_ping(path=""):
    """Best-effort dead-man's-switch ping; a configured URL never affects the worker."""
    base = os.environ.get("HEALTHCHECK_URL", "").strip()
    if not base:
        return
    try:
        response = urllib.request.urlopen(
            base.rstrip("/") + path, timeout=HEALTHCHECK_TIMEOUT
        )
        response.close()
    except Exception:
        pass


def disk_status(recordings_dir):
    path = Path(recordings_dir).resolve()
    while not path.exists():
        path = path.parent
    usage = shutil.disk_usage(path)
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "low_disk": usage.free <= LOW_DISK_BYTES,
    }


class RecordingWorker:
    """Single-process queue worker; only this process starts recorder children."""

    def __init__(self, store, recordings_dir, is_live, avatars_dir=None):
        self.store = store
        self.recordings_dir = Path(recordings_dir).resolve()
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.avatars_dir = Path(avatars_dir).resolve() if avatars_dir else None
        self.is_live = is_live
        self.processes = {}
        self._signalled = set()
        self._stop = threading.Event()
        self._last_watch_check = 0

    def disk_status(self):
        return disk_status(self.recordings_dir)

    def enqueue(
        self, username, duration_seconds=1800, keep_forever=False, platform="tiktok"
    ):
        return self.store.enqueue_recording(
            username, duration_seconds, keep_forever, platform
        )[0]

    def launch_queued(self, recording_id):
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE id = ? AND status = 'starting' "
                "AND pid IS NULL AND worker_pid IS NULL",
                (recording_id,),
            ).fetchone()
            if not row:
                return False
            connection.execute(
                "UPDATE recordings SET worker_pid = ? WHERE id = ?",
                (os.getpid(), recording_id),
            )
            username = row["username"]
            platform = row["platform"] if "platform" in row.keys() else "tiktok"

        if self.disk_status()["low_disk"]:
            self._finish(recording_id, "failed", "Recording blocked below 2 GB free")
            return False

        prefix = "TW" if platform == "twitch" else "TK"
        output = self.recordings_dir / (
            f"{prefix}_{username}_{time.strftime('%Y.%m.%d_%H-%M-%S')}_web_{recording_id}.mp4"
        )
        try:
            process = StreamCapture(username, output, platform=platform)
        except OSError:
            self._finish(recording_id, "failed", "Could not start recorder process")
            return False

        with self.store.connect() as connection:
            connection.execute(
                "UPDATE recordings SET status = CASE WHEN status = 'starting' "
                "THEN 'recording' ELSE status END, started_at = ?, pid = ?, "
                "file_path = ? WHERE id = ?",
                (time.time(), process.pid, str(output), recording_id),
            )
            status = connection.execute(
                "SELECT status FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()["status"]
        self.processes[recording_id] = process
        if status == "stopping":
            self.stop_recording(recording_id)
        return True

    def launch_pending(self):
        with self.store.connect() as connection:
            queued = [
                row[0]
                for row in connection.execute(
                    "SELECT id FROM recordings WHERE status = 'starting' "
                    "AND pid IS NULL AND worker_pid IS NULL ORDER BY created_at"
                )
            ]
        for recording_id in queued:
            self.launch_queued(recording_id)

    def stop_recording(self, recording_id):
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
            if not row or row["status"] not in ("starting", "recording", "stopping"):
                return dict(row) if row else None
            if row["status"] != "stopping":
                connection.execute(
                    "UPDATE recordings SET status = 'stopping' WHERE id = ?",
                    (recording_id,),
                )
            pid = row["pid"]

        if pid and recording_id not in self._signalled:
            try:
                process = self.processes.get(recording_id)
                if process:
                    process.send_signal(signal.SIGINT)
                else:
                    os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            self._signalled.add(recording_id)
        return self.store.recording(recording_id)

    def _active_recordings(self):
        with self.store.connect() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM recordings WHERE status IN ('starting', 'recording', 'stopping')"
                )
            ]

    def _capture_output(self, recording):
        process = self.processes.get(recording["id"])
        return process.output if process and hasattr(process, "output") else None

    @staticmethod
    def _has_finalized_video(process, output):
        if process and hasattr(process, "finalized"):
            return process.finalized
        return bool(
            output
            and output.is_file()
            and output.stat().st_size
            and not output.name.endswith("_flv.mp4")
        )

    def _finish_capture(self, recording, process):
        output = self._capture_output(recording)
        has_video = self._has_finalized_video(process, output)
        interrupted = bool(process and getattr(process, "interrupted", False))
        if recording["status"] == "stopping":
            status, error = (
                ("stopped", None)
                if has_video
                else ("failed", "Recorder stopped without finalizing an MP4")
            )
        elif interrupted and has_video:
            # shortcut: Streamlink cannot reconnect opened HTTP-FLV; upgrade only with safe continuation, never by appending into this MP4.
            status, error = (
                "interrupted",
                "Streamlink lost the active stream; a playable partial recording was saved",
            )
        elif has_video:
            status, error = "completed", None
        else:
            status, error = "failed", "Recorder exited without finalizing an MP4"
        self._finish(recording["id"], status, error, output if has_video else None)

    def _finish(self, recording_id, status, error=None, output=None):
        if status == "interrupted":
            path = Path(output).resolve() if output else None
            if (
                not path
                or not path.is_relative_to(self.recordings_dir)
                or not path.is_file()
                or path.suffix.lower() != ".mp4"
                or path.name.endswith("_flv.mp4")
                or path.stat().st_size == 0
            ):
                status = "failed"
                error = "Stream was interrupted before a playable MP4 was finalized"
                output = None
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE recordings SET status = ?, finished_at = ?, file_path = ?, "
                "error = ?, pid = NULL, worker_pid = NULL WHERE id = ?",
                (
                    status,
                    time.time(),
                    str(output) if output else None,
                    error,
                    recording_id,
                ),
            )
        self.processes.pop(recording_id, None)
        self._signalled.discard(recording_id)

    def _process_exited(self, recording):
        process = self.processes.get(recording["id"])
        if process:
            return process.poll() is not None
        if not recording.get("pid"):
            return True
        try:
            os.kill(recording["pid"], 0)
            return False
        except ProcessLookupError:
            return True

    def _monitor(self):
        low_disk = self.disk_status()["low_disk"]
        now = time.time()
        for recording in self._active_recordings():
            if recording["status"] == "starting":
                continue
            if recording["status"] == "stopping" and not recording.get("pid"):
                self._finish(
                    recording["id"], "stopped", "Stopped before recorder launch"
                )
                continue
            if low_disk or recording["status"] == "stopping":
                self.stop_recording(recording["id"])
                recording["status"] = "stopping"
            elif (
                recording["duration_seconds"] is not None
                and recording["started_at"] is not None
                and recording["started_at"] + recording["duration_seconds"] <= now
            ):
                self.stop_recording(recording["id"])
                recording["status"] = "stopping"

            if self._process_exited(recording):
                process = self.processes.get(recording["id"])
                if process:
                    process.wait()
                self._finish_capture(recording, process)

    def _queue_watch(self, watch):
        if self.disk_status()["low_disk"]:
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE watchlist SET last_error = ? WHERE id = ?",
                    ("Recording blocked below 2 GB free", watch["id"]),
                )
            return False

        duration = watch["duration_seconds"]
        if watch["mode"] == "always":
            duration = (
                min(duration, watch["max_duration_seconds"])
                if duration
                else watch["max_duration_seconds"]
            )
        try:
            self.enqueue(
                watch["username"],
                duration,
                platform=watch.get("platform") or "tiktok",
            )
        except Exception:
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE watchlist SET last_error = ? WHERE id = ?",
                    ("Could not queue recording", watch["id"]),
                )
            return False
        with self.store.connect() as connection:
            if watch["mode"] == "once":
                connection.execute("DELETE FROM watchlist WHERE id = ?", (watch["id"],))
            else:
                connection.execute(
                    "UPDATE watchlist SET is_live = 1, last_started_at = ?, last_error = NULL WHERE id = ?",
                    (time.time(), watch["id"]),
                )
        return True

    def _check_watchlist(self):
        for watch in self.store.watchlist():
            try:
                live = self.is_live(
                    watch["username"], watch.get("platform") or "tiktok"
                )
            except Exception:
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE watchlist SET last_error = ? WHERE id = ?",
                        ("Live check failed", watch["id"]),
                    )
                continue
            if not live:
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE watchlist SET is_live = 0, last_error = NULL WHERE id = ?",
                        (watch["id"],),
                    )
                continue
            if watch["is_live"]:
                continue
            self._queue_watch(watch)

    def _retention(self):
        cutoff = time.time() - RETENTION_SECONDS
        with self.store.connect() as connection:
            expired = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM recordings WHERE status IN ('completed', 'failed', 'stopped', 'interrupted') "
                    "AND keep_forever = 0 AND finished_at < ?",
                    (cutoff,),
                )
            ]
        for row in expired:
            path = Path(row["file_path"]).resolve() if row.get("file_path") else None
            if path:
                if not path.is_relative_to(self.recordings_dir):
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
            with self.store.connect() as connection:
                connection.execute("DELETE FROM recordings WHERE id = ?", (row["id"],))

    def _cache_one_avatar(self):
        if not self.avatars_dir:
            return
        for username, platform in self.store.known_usernames():
            if avatar_file(self.avatars_dir, username, platform):
                continue
            try:
                cache_profile_avatar(
                    username, self.avatars_dir, platform=platform
                )
            except Exception:
                pass
            return

    def recover_orphans(self):
        for recording in self._active_recordings():
            if recording["status"] == "starting" and recording.get("worker_pid"):
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE recordings SET worker_pid = NULL WHERE id = ?",
                        (recording["id"],),
                    )
            elif recording.get("pid") and recording["id"] not in self.processes:
                # PIDs belong to the previous container namespace; never signal a reused PID.
                output = (
                    Path(recording["file_path"]).resolve()
                    if recording.get("file_path")
                    else None
                )
                if not output or not output.is_relative_to(self.recordings_dir):
                    output = None
                self._finish(
                    recording["id"],
                    "failed",
                    "Worker restarted; partial output may need manual cleanup",
                    output,
                )

    def run(self):
        try:
            self.recover_orphans()
            while not self._stop.is_set():
                self.launch_pending()
                self._monitor()
                self._retention()
                if time.monotonic() - self._last_watch_check >= 60:
                    try:
                        self._check_watchlist()
                        self._cache_one_avatar()
                    except Exception:
                        healthcheck_ping("/fail")
                        raise
                    self._last_watch_check = time.monotonic()
                    # The watchlist poll is the periodic job; silence means watches stop being checked.
                    healthcheck_ping()
                self._stop.wait(1)
        finally:
            self.shutdown()

    def shutdown(self):
        self._stop.set()
        for recording in self._active_recordings():
            if recording["status"] in ("recording", "stopping"):
                self.stop_recording(recording["id"])
        for recording_id, process in list(self.processes.items()):
            process.wait()
            recording = self.store.recording(recording_id)
            if recording:
                self._finish_capture(recording, process)
        for recording in self._active_recordings():
            if recording.get("pid") and recording["id"] not in self.processes:
                while True:
                    try:
                        os.kill(recording["pid"], 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.2)
                self._finish(
                    recording["id"],
                    "failed",
                    "Worker restarted; partial output may need manual cleanup",
                )
            elif recording["status"] == "stopping" and not recording.get("pid"):
                self._finish(
                    recording["id"], "stopped", "Stopped before recorder launch"
                )

    def request_shutdown(self):
        self._stop.set()
        for recording_id, process in self.processes.items():
            if recording_id not in self._signalled and process.poll() is None:
                try:
                    process.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass
                self._signalled.add(recording_id)


def _is_live(username, platform="tiktok"):
    from web.capture import is_live

    return is_live(username, platform)


def main():
    database = os.environ.get("TT_CLIPPER_DATABASE", "state/tt-clipper.sqlite3")
    store = Store(database)
    store.initialize()
    recordings_dir = os.environ.get(
        "RECORDINGS_DIR", os.environ.get("OUTPUT_DIR", "output")
    )
    avatars_dir = os.environ.get(
        "TT_CLIPPER_AVATARS", str(Path(database).resolve().parent / "avatars")
    )
    worker = RecordingWorker(store, recordings_dir, _is_live, avatars_dir)

    def request_shutdown(_signum, _frame):
        worker.request_shutdown()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    worker.run()


if __name__ == "__main__":
    main()
