import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request, send_file

from web.capture import is_live
from web.search import search_profiles
from web.store import Store
from web.worker import LOW_DISK_BYTES, disk_status


DEFAULT_DURATION = 1800
ALLOWED_DURATIONS = {30, 900, 1800, 3600, 14400, 28800}
HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,24}$")


def _duration(value, default=None):
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value not in ALLOWED_DURATIONS
    ):
        raise ValueError(
            "duration_seconds must be 30, 900, 1800, 3600, 14400, 28800, or null"
        )
    return value


def _username_from_query(query):
    query = query.strip()
    if query.startswith("@"):
        query = query[1:]
    if query.startswith("http://") or query.startswith("https://"):
        parsed = urlparse(query)
        if parsed.scheme != "https" or parsed.hostname not in (
            "tiktok.com",
            "www.tiktok.com",
            "m.tiktok.com",
        ):
            raise ValueError("Enter a TikTok live URL or exact username")
        match = re.fullmatch(r"/@([A-Za-z0-9._]{1,24})/live/?", parsed.path)
        if not match:
            raise ValueError("TikTok URL must be a live URL for one account")
        query = match.group(1)
    if not HANDLE_RE.fullmatch(query):
        raise ValueError("Enter a valid TikTok username or live URL")
    return query.lower()


def _recording_json(recording, recordings_dir):
    result = dict(recording)
    result["keep_forever"] = bool(result["keep_forever"])
    path = Path(result["file_path"]).resolve() if result.get("file_path") else None
    in_recordings_dir = bool(path and path.is_relative_to(recordings_dir))
    size_bytes = 0
    finalized_interrupted = result.get("status") != "interrupted" or bool(
        result.get("finished_at")
        and in_recordings_dir
        and path.suffix.lower() == ".mp4"
        and not path.name.endswith("_flv.mp4")
        and path.is_file()
        and path.stat().st_size > 0
    )
    if in_recordings_dir and path.is_file() and finalized_interrupted:
        result["file_name"] = path.name
        size_bytes = path.stat().st_size
    else:
        result["file_name"] = None
    result["size_bytes"] = size_bytes
    result["file_size_bytes"] = size_bytes
    now = time.time()
    started_at = result.get("started_at")
    finished_at = result.get("finished_at")
    result["elapsed_seconds"] = (
        max(0, int((finished_at or now) - started_at)) if started_at else 0
    )
    duration = result.get("duration_seconds")
    result["remaining_seconds"] = (
        max(0, duration - result["elapsed_seconds"])
        if duration is not None and not finished_at
        else (0 if finished_at else None)
    )
    bitrate = (
        size_bytes * 8 / result["elapsed_seconds"]
        if size_bytes and result["elapsed_seconds"]
        else None
    )
    result["observed_bitrate_bps"] = round(bitrate) if bitrate else None
    result["estimated_total_bytes"] = (
        round(bitrate * duration / 8)
        if bitrate and duration is not None and not finished_at
        else None
    )
    result["estimated_remaining_bytes"] = (
        round(bitrate * result["remaining_seconds"] / 8)
        if bitrate and result["remaining_seconds"] is not None and not finished_at
        else None
    )
    free_bytes = disk_status(recordings_dir)["free_bytes"]
    result["estimated_seconds_until_low_disk"] = (
        max(0, int((free_bytes - LOW_DISK_BYTES) * 8 / bitrate)) if bitrate else None
    )
    needs_warning = bool(
        bitrate
        and result["estimated_remaining_bytes"] is not None
        and result["estimated_remaining_bytes"] > max(0, free_bytes - LOW_DISK_BYTES)
    )
    result["low_space_warning"] = (
        "At the observed bitrate, this recording may stop early to preserve 2 GB of free space."
        if needs_warning
        else None
    )
    result.pop("file_path", None)
    result.pop("pid", None)
    result.pop("worker_pid", None)
    return result


def _resolve_live(username):
    return is_live(username)


def create_app(test_config=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        DATABASE=os.environ.get("TT_CLIPPER_DATABASE", "state/tt-clipper.sqlite3"),
        RECORDINGS_DIR=os.environ.get(
            "RECORDINGS_DIR", os.environ.get("OUTPUT_DIR", "output")
        ),
    )
    if test_config:
        app.config.update(test_config)

    recordings_dir = Path(app.config["RECORDINGS_DIR"]).resolve()
    store = Store(app.config["DATABASE"])
    app.extensions["tt_clipper_store"] = store

    @app.get("/")
    def home():
        return render_template("home.html")

    @app.get("/dashboard")
    def dashboard():
        return render_template("dashboard.html")

    @app.get("/recordings/<int:recording_id>")
    def recording_page(recording_id):
        if not store.recording(recording_id):
            abort(404)
        return render_template("recording.html", recording_id=recording_id)

    @app.get("/api/search")
    def search():
        query = request.args.get("q", "").strip()
        if len(query) > 100:
            return jsonify(error="q must contain at most 100 characters"), 400
        if not query:
            return jsonify(recent=store.recent_searches(), results=[])
        try:
            results = search_profiles(query)
            return jsonify(recent=store.recent_searches(), results=results)
        except Exception:
            return jsonify(
                error="TikTok profile search is temporarily unavailable"
            ), 502

    @app.post("/api/recordings")
    def create_recording():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("query"), str):
            return jsonify(error="query is required"), 400
        try:
            username = _username_from_query(body["query"])
            duration = _duration(body.get("duration_seconds"), DEFAULT_DURATION)
        except ValueError as error:
            return jsonify(error=str(error)), 400

        store.remember_search(username, time.time())
        active = store.active_recording(username)
        if active:
            return jsonify(_recording_json(active, recordings_dir)), 200
        if disk_status(recordings_dir)["low_disk"]:
            return jsonify(
                error="Recording blocked: less than 2 GB of free disk space"
            ), 507

        try:
            live = _resolve_live(username)
        except Exception:
            return jsonify(
                error="Could not check whether this TikTok account is live"
            ), 502
        if not live:
            return jsonify(status="offline", username=username), 200
        try:
            recording, created = store.enqueue_recording(username, duration)
        except Exception:
            return jsonify(error="Could not queue this recording"), 500
        if not created:
            return jsonify(_recording_json(recording, recordings_dir)), 200
        return jsonify(_recording_json(recording, recordings_dir)), 201

    @app.get("/api/recordings")
    def recordings():
        return jsonify(
            recordings=[
                _recording_json(item, recordings_dir) for item in store.recordings()
            ]
        )

    @app.get("/api/recordings/<int:recording_id>")
    def get_recording(recording_id):
        recording = store.recording(recording_id)
        if not recording:
            abort(404)
        return jsonify(_recording_json(recording, recordings_dir))

    @app.patch("/api/recordings/<int:recording_id>")
    def update_recording(recording_id):
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not body:
            return jsonify(error="JSON updates are required"), 400
        if set(body) - {"duration_seconds", "keep_forever"}:
            return jsonify(
                error="Only duration_seconds and keep_forever can be changed"
            ), 400
        recording = store.recording(recording_id)
        if not recording:
            abort(404)
        try:
            duration = (
                _duration(body["duration_seconds"])
                if "duration_seconds" in body
                else None
            )
        except ValueError as error:
            return jsonify(error=str(error)), 400
        if "keep_forever" in body and not isinstance(body["keep_forever"], bool):
            return jsonify(error="keep_forever must be a boolean"), 400
        if (
            recording["status"] not in ("starting", "recording", "stopping")
            and "duration_seconds" in body
        ):
            return jsonify(
                error="Only an active recording's duration can be changed"
            ), 409
        updates, values = [], []
        if "duration_seconds" in body:
            updates.append("duration_seconds = ?")
            values.append(duration)
        if "keep_forever" in body:
            updates.append("keep_forever = ?")
            values.append(int(body["keep_forever"]))
        values.append(recording_id)
        with store.connect() as connection:
            connection.execute(
                f"UPDATE recordings SET {', '.join(updates)} WHERE id = ?", values
            )
        return jsonify(_recording_json(store.recording(recording_id), recordings_dir))

    @app.post("/api/recordings/<int:recording_id>/stop")
    def stop_recording(recording_id):
        with store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
            if not row:
                abort(404)
            if row["status"] in ("starting", "recording"):
                connection.execute(
                    "UPDATE recordings SET status = 'stopping' WHERE id = ?",
                    (recording_id,),
                )
        recording = store.recording(recording_id)
        return jsonify(_recording_json(recording, recordings_dir))

    @app.delete("/api/recordings/<int:recording_id>")
    def delete_recording(recording_id):
        recording = store.recording(recording_id)
        if not recording:
            abort(404)
        if recording["status"] in ("starting", "recording", "stopping"):
            return jsonify(error="Stop the active recording before deleting it"), 409
        path = (
            Path(recording["file_path"]).resolve()
            if recording.get("file_path")
            else None
        )
        if path and path.is_relative_to(recordings_dir):
            path.unlink(missing_ok=True)
        with store.connect() as connection:
            connection.execute("DELETE FROM recordings WHERE id = ?", (recording_id,))
        return "", 204

    @app.route("/api/watchlist", methods=["GET", "POST", "DELETE"])
    def watchlist():
        if request.method == "GET":
            return jsonify(watchlist=store.watchlist())
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="JSON body must be an object"), 400
        if request.method == "DELETE":
            username = request.args.get("username") or body.get("username")
            if not isinstance(username, str) or not username:
                return jsonify(error="username is required"), 400
            with store.connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM watchlist WHERE username = ?", (username,)
                )
            return (
                ("", 204)
                if cursor.rowcount
                else (jsonify(error="Watchlist entry not found"), 404)
            )

        username = body.get("username")
        mode = body.get("mode")
        if not isinstance(username, str):
            return jsonify(error="username is required"), 400
        try:
            username = _username_from_query(username)
            if mode not in ("once", "always"):
                raise ValueError("mode must be once or always")
            duration = (
                _duration(body["duration_seconds"])
                if "duration_seconds" in body
                else (DEFAULT_DURATION if mode == "once" else None)
            )
            maximum = _duration(body.get("max_duration_seconds"), 14400)
            if maximum is None:
                raise ValueError("max_duration_seconds cannot be null")
        except ValueError as error:
            return jsonify(error=str(error)), 400
        store.remember_search(username, time.time())
        with store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM watchlist WHERE username = ?", (username,)
            ).fetchone()
            connection.execute(
                "INSERT INTO watchlist(username, mode, duration_seconds, max_duration_seconds, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(username) DO UPDATE SET "
                "mode = excluded.mode, duration_seconds = excluded.duration_seconds, "
                "max_duration_seconds = excluded.max_duration_seconds, last_error = NULL",
                (username, mode, duration, maximum, time.time()),
            )
            item = connection.execute(
                "SELECT * FROM watchlist WHERE username = ?", (username,)
            ).fetchone()
        return jsonify(dict(item)), 200 if existing else 201

    @app.get("/api/system")
    def system_status():
        disk = disk_status(recordings_dir)
        with store.connect() as connection:
            counts = {
                row["status"]: row["count"]
                for row in connection.execute(
                    "SELECT status, COUNT(*) AS count FROM recordings GROUP BY status"
                )
            }
        return jsonify(
            disk={**disk, "warning": disk["free_bytes"] <= LOW_DISK_BYTES},
            recordings=counts,
            active_recordings=sum(
                counts.get(state, 0) for state in ("starting", "recording", "stopping")
            ),
        )

    def video_response(recording_id, attachment):
        recording = store.recording(recording_id)
        if (
            not recording
            or recording["status"] not in ("completed", "stopped", "interrupted")
            or not recording.get("file_path")
        ):
            abort(404)
        path = Path(recording["file_path"]).resolve()
        if (
            not path.is_relative_to(recordings_dir)
            or not path.is_file()
            or path.name.endswith("_flv.mp4")
            or path.suffix.lower() != ".mp4"
            or path.stat().st_size == 0
            or (
                recording["status"] == "interrupted"
                and not recording.get("finished_at")
            )
        ):
            abort(404)
        return send_file(
            path,
            mimetype="video/mp4",
            as_attachment=attachment,
            download_name=path.name,
            conditional=True,
        )

    @app.get("/recordings/<int:recording_id>/video")
    def video(recording_id):
        return video_response(recording_id, False)

    @app.get("/recordings/<int:recording_id>/download")
    def download(recording_id):
        return video_response(recording_id, True)

    return app


app = create_app()
