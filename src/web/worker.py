import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from web.store import Store


LOW_DISK_BYTES = 2 * 1024**3
RETENTION_SECONDS = 7 * 24 * 60 * 60


def disk_status(recordings_dir):
    path = Path(recordings_dir).resolve()
    while not path.exists():
        path = path.parent
    usage = shutil.disk_usage(path)
    return {
        "free_bytes": usage.free,
        "total_bytes": usage.total,
        "low_disk": usage.free < LOW_DISK_BYTES,
    }


class RecordingWorker:
    """Single-process queue worker; only this process starts recorder children."""

    def __init__(self, store, recordings_dir, is_live):
        self.store = store
        self.recordings_dir = Path(recordings_dir).resolve()
        self.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.is_live = is_live
        self.processes = {}
        self._signalled = set()
        self._stop = threading.Event()
        self._last_watch_check = 0

    def disk_status(self):
        return disk_status(self.recordings_dir)

    def enqueue(self, username, duration_seconds=1800, keep_forever=False):
        return self.store.enqueue_recording(username, duration_seconds, keep_forever)[0]

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

        if self.disk_status()["low_disk"]:
            self._finish(recording_id, "failed", "Recording blocked below 2 GB free")
            return False

        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[1] / "main.py"),
                    "-user",
                    username,
                    "-output",
                    str(self.recordings_dir),
                    "-no-update-check",
                ],
                cwd=Path(__file__).resolve().parents[2],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except OSError:
            self._finish(recording_id, "failed", "Could not start recorder process")
            return False

        with self.store.connect() as connection:
            connection.execute(
                "UPDATE recordings SET status = CASE WHEN status = 'starting' "
                "THEN 'recording' ELSE status END, started_at = ?, pid = ? WHERE id = ?",
                (time.time(), process.pid, recording_id),
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

    def _find_output(self, username, started_at):
        candidates = sorted(
            self.recordings_dir.glob(f"TK_{username}_*.mp4"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return next(
            (path for path in candidates if path.stat().st_mtime >= (started_at or 0)),
            None,
        )

    def _finish(self, recording_id, status, error=None, output=None):
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

            output = self._find_output(recording["username"], recording["started_at"])
            if output and output != recording.get("file_path"):
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE recordings SET file_path = ? WHERE id = ?",
                        (str(output), recording["id"]),
                    )

            if self._process_exited(recording):
                output = self._find_output(
                    recording["username"], recording["started_at"]
                )
                finished_status = (
                    "stopped" if recording["status"] == "stopping" else "completed"
                )
                has_video = bool(output and output.stat().st_size)
                self._finish(
                    recording["id"],
                    finished_status if has_video else "failed",
                    None if has_video else "Recorder exited without producing a video",
                    output if has_video else None,
                )

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
            self.enqueue(watch["username"], duration)
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
                live = self.is_live(watch["username"])
            except Exception:
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE watchlist SET last_error = ? WHERE id = ?",
                        ("TikTok profile check failed", watch["id"]),
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
        with self.store.transaction() as connection:
            expired = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM recordings WHERE status IN ('completed', 'failed', 'stopped') "
                    "AND keep_forever = 0 AND finished_at < ?",
                    (cutoff,),
                )
            ]
            for row in expired:
                connection.execute("DELETE FROM recordings WHERE id = ?", (row["id"],))
        for row in expired:
            path = Path(row["file_path"]).resolve() if row.get("file_path") else None
            if path and path.is_relative_to(self.recordings_dir):
                path.unlink(missing_ok=True)

    def recover_orphans(self):
        for recording in self._active_recordings():
            if recording.get("pid") and recording["id"] not in self.processes:
                self.stop_recording(recording["id"])
            elif recording["status"] == "starting" and recording.get("worker_pid"):
                with self.store.connect() as connection:
                    connection.execute(
                        "UPDATE recordings SET worker_pid = NULL WHERE id = ?",
                        (recording["id"],),
                    )

    def run(self):
        try:
            self.recover_orphans()
            while not self._stop.is_set():
                self.launch_pending()
                self._monitor()
                self._retention()
                if time.monotonic() - self._last_watch_check >= 60:
                    self._check_watchlist()
                    self._last_watch_check = time.monotonic()
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
                output = self._find_output(
                    recording["username"], recording["started_at"]
                )
                has_video = bool(output and output.stat().st_size)
                self._finish(
                    recording_id,
                    "stopped" if has_video else "failed",
                    None if has_video else "Recorder exited without producing a video",
                    output if has_video else None,
                )
        for recording in self._active_recordings():
            if recording.get("pid") and recording["id"] not in self.processes:
                while True:
                    try:
                        os.kill(recording["pid"], 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.2)
                output = self._find_output(
                    recording["username"], recording["started_at"]
                )
                has_video = bool(output and output.stat().st_size)
                self._finish(
                    recording["id"],
                    "stopped" if has_video else "failed",
                    None if has_video else "Recorder exited without producing a video",
                    output if has_video else None,
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


def _is_live(username):
    from core.tiktok_api import TikTokAPI
    from utils.utils import read_cookies

    api = TikTokAPI(proxy=None, cookies=read_cookies())
    room_id = api.get_room_id_from_user(username)
    return bool(room_id and api.is_room_alive(room_id))


def main():
    store = Store(os.environ.get("TT_CLIPPER_DATABASE", "state/tt-clipper.sqlite3"))
    store.initialize()
    recordings_dir = os.environ.get(
        "RECORDINGS_DIR", os.environ.get("OUTPUT_DIR", "output")
    )
    worker = RecordingWorker(store, recordings_dir, _is_live)

    def request_shutdown(_signum, _frame):
        worker.request_shutdown()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    worker.run()


if __name__ == "__main__":
    main()
