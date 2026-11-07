import hashlib
import hmac
import math
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, abort, jsonify, render_template, request, send_file

from web.capture import HANDLE_RE as TIKTOK_HANDLE_RE
from web.capture import TWITCH_HANDLE_RE, is_live
from web.search import avatar_file, cache_profile_avatar, search_profiles
from web.store import Store
from web.worker import LOW_DISK_BYTES, disk_status


DEFAULT_DURATION = 1320
ALLOWED_DURATIONS = {30, 900, 1320, 1800, 3600, 14400, 28800}
GAME_WEBHOOK_MAX_SKEW = 300
GAME_EVENT_MAX = 50
GAME_EVENT_MAX_AGE = 86400
GAME_EVENT_MAX_FUTURE = 60
EVENT_LEAD_SECONDS = 10
EVENT_ROLES = {"self", "teammate", "streamer", "streamer_teammate"}
EVENT_OUR_ROLES = {"self", "teammate"}
EVENT_STREAMER_ROLES = {"streamer", "streamer_teammate"}
EVENT_KINDS = {"knock", "elimination"}
HANDLE_RE = TIKTOK_HANDLE_RE
TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}


def _env_secret(env_name, file_env_name):
    value = os.environ.get(env_name, "").strip()
    if value:
        return value
    path = os.environ.get(file_env_name, "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def _game_signature(secret, timestamp, body):
    message = str(timestamp).encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _duration(value, default=None):
    if value is None:
        return default
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value not in ALLOWED_DURATIONS
    ):
        raise ValueError(
            "duration_seconds must be 30, 900, 1320, 1800, 3600, 14400, 28800, or null"
        )
    return value


def _platform(value):
    if value in (None, ""):
        return "tiktok"
    if value not in ("tiktok", "twitch"):
        raise ValueError("platform must be tiktok or twitch")
    return value


def _handle_re(platform):
    return TWITCH_HANDLE_RE if platform == "twitch" else HANDLE_RE


def _game_auth_error(app):
    secret = app.config.get("GAME_WEBHOOK_SECRET")
    token = app.config.get("GAME_WEBHOOK_TOKEN")
    if not secret or not token:
        return jsonify(error="Game webhook is not configured"), 503
    request_token = request.headers.get("X-TT-Token", "").strip()
    if not request_token or not hmac.compare_digest(
        request_token.encode("utf-8"), token.encode("utf-8")
    ):
        return jsonify(error="Invalid token or signature"), 401
    body_bytes = request.get_data(cache=True)
    timestamp = request.headers.get("X-TT-Timestamp", "").strip()
    signature = request.headers.get("X-TT-Signature", "").strip().lower()
    try:
        sent_at = int(timestamp)
    except ValueError:
        sent_at = 0
    expected = _game_signature(secret, timestamp, body_bytes)
    if (
        not timestamp
        or len(signature) != 64
        or any(char not in "0123456789abcdef" for char in signature)
        or abs(time.time() - sent_at) > GAME_WEBHOOK_MAX_SKEW
        or not hmac.compare_digest(expected, signature)
    ):
        return jsonify(error="Invalid token or signature"), 401
    return None


def _game_event(item, now):
    if not isinstance(item, dict):
        raise ValueError("Each event must be an object")
    try:
        platform = _platform(item.get("platform"))
    except ValueError as error:
        raise ValueError(str(error)) from error
    username = item.get("username")
    if not isinstance(username, str):
        raise ValueError("Each event needs a username")
    username = username.strip().lstrip("@").lower()
    if not _handle_re(platform).fullmatch(username):
        raise ValueError(f"Invalid {platform} username: {username or '(empty)'}")
    kind = item.get("kind")
    if kind not in EVENT_KINDS:
        raise ValueError("kind must be knock or elimination")
    event_at = item.get("event_at")
    if (
        isinstance(event_at, bool)
        or not isinstance(event_at, (int, float))
        or not math.isfinite(event_at)
    ):
        raise ValueError("event_at must be Unix seconds")
    event_at = float(event_at)
    if event_at > now + GAME_EVENT_MAX_FUTURE or event_at < now - GAME_EVENT_MAX_AGE:
        raise ValueError("event_at is outside the accepted time window")
    actor = item.get("actor")
    victim = item.get("victim")
    for name in (actor, victim):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
            raise ValueError("actor and victim must be non-empty names")
    actor_role = item.get("actor_role")
    victim_role = item.get("victim_role")
    if actor_role not in EVENT_ROLES or victim_role not in EVENT_ROLES:
        raise ValueError(
            "actor_role and victim_role must be self, teammate, streamer, "
            "or streamer_teammate"
        )
    if not (
        (actor_role in EVENT_OUR_ROLES and victim_role in EVENT_STREAMER_ROLES)
        or (actor_role in EVENT_STREAMER_ROLES and victim_role in EVENT_OUR_ROLES)
    ):
        raise ValueError(
            "Each event needs one side on your team and one side on the streamer's team"
        )
    return {
        "platform": platform,
        "username": username,
        "kind": kind,
        "event_at": event_at,
        "actor": actor.strip(),
        "actor_role": actor_role,
        "victim": victim.strip(),
        "victim_role": victim_role,
    }


def _username_from_query(query, platform="tiktok"):
    query = query.strip()
    platform = _platform(platform)
    if query.startswith("@"):
        query = query[1:]
    if query.startswith("http://") or query.startswith("https://"):
        parsed = urlparse(query)
        host = parsed.hostname or ""
        if parsed.scheme != "https":
            raise ValueError("Enter a live URL or exact username")
        if host in TWITCH_HOSTS:
            platform = "twitch"
            match = re.fullmatch(r"/([A-Za-z0-9_]{3,25})/?", parsed.path)
            if not match:
                raise ValueError("Twitch URL must be a channel URL")
            query = match.group(1)
        elif host in TIKTOK_HOSTS:
            platform = "tiktok"
            match = re.fullmatch(r"/@([A-Za-z0-9._]{1,24})/live/?", parsed.path)
            if not match:
                raise ValueError("TikTok URL must be a live URL for one account")
            query = match.group(1)
        else:
            raise ValueError("Enter a TikTok or Twitch URL, or a username")
    if not _handle_re(platform).fullmatch(query):
        raise ValueError("Enter a valid username or live URL")
    return query.lower(), platform


def _recording_json(recording, recordings_dir, avatar_url=None):
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
    result["avatar_url"] = avatar_url
    return result


def _resolve_live(username, platform="tiktok"):
    return is_live(username, platform)


def create_app(test_config=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        DATABASE=os.environ.get("TT_CLIPPER_DATABASE", "state/tt-clipper.sqlite3"),
        RECORDINGS_DIR=os.environ.get(
            "RECORDINGS_DIR", os.environ.get("OUTPUT_DIR", "output")
        ),
        GAME_WEBHOOK_SECRET=_env_secret(
            "TT_GAME_WEBHOOK_SECRET", "TT_GAME_WEBHOOK_SECRET_FILE"
        ),
        GAME_WEBHOOK_TOKEN=_env_secret(
            "TT_GAME_WEBHOOK_TOKEN", "TT_GAME_WEBHOOK_TOKEN_FILE"
        ),
    )
    if test_config:
        app.config.update(test_config)

    recordings_dir = Path(app.config["RECORDINGS_DIR"]).resolve()
    avatars_dir = Path(
        app.config.get("AVATARS_DIR")
        or os.environ.get(
            "TT_CLIPPER_AVATARS",
            str(Path(app.config["DATABASE"]).resolve().parent / "avatars"),
        )
    )
    store = Store(app.config["DATABASE"])
    app.extensions["tt_clipper_store"] = store

    def _avatar_url(username, platform="tiktok"):
        if avatar_file(avatars_dir, username, platform):
            return f"/api/avatars/{username}?platform={platform}"
        return None

    def _recording_payload(recording):
        platform = recording.get("platform") or "tiktok"
        return _recording_json(
            recording,
            recordings_dir,
            _avatar_url(recording["username"], platform),
        )

    def _remember(username, platform="tiktok"):
        store.remember_search(username, time.time(), platform)
        if not app.config.get("TESTING") and not avatar_file(
            avatars_dir, username, platform
        ):
            def _cache():
                try:
                    cache_profile_avatar(
                        username, avatars_dir, platform=platform
                    )
                except Exception:
                    pass

            threading.Thread(target=_cache, daemon=True).start()

    def _queue_recording(username, platform, duration):
        _remember(username, platform)
        active = store.active_recording(username, platform)
        if active:
            return _recording_payload(active), 200
        if disk_status(recordings_dir)["low_disk"]:
            return {
                "error": "Recording blocked: less than 2 GB of free disk space"
            }, 507
        try:
            live = _resolve_live(username, platform)
        except Exception:
            return {"error": "Could not check whether this account is live"}, 502
        if not live:
            return {"status": "offline", "username": username, "platform": platform}, 200
        try:
            recording, created = store.enqueue_recording(
                username, duration, platform=platform
            )
        except Exception:
            return {"error": "Could not queue this recording"}, 500
        return _recording_payload(recording), 201 if created else 200

    @app.context_processor
    def nav_page():
        page_by_endpoint = {
            "home": "home",
            "dashboard": "dashboard",
            "recording_page": "recording",
        }
        page = page_by_endpoint.get(request.endpoint or "", "")
        nav_back_href = {
            "dashboard": "/",
            "recording": "/dashboard",
        }.get(page)
        dev_reload = bool(app.debug)
        asset_v = ""
        if dev_reload:
            css_path = Path(app.static_folder or "") / "app.css"
            if css_path.is_file():
                asset_v = str(int(css_path.stat().st_mtime_ns))
        return {
            "page": page,
            "nav_back_href": nav_back_href,
            "dev_reload": dev_reload,
            "asset_v": asset_v,
        }

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

    def _known_result(username, platform):
        return {
            "username": username,
            "display_name": username,
            "platform": platform,
            "avatar_url": _avatar_url(username, platform),
            "is_live": None,
        }

    @app.get("/api/search")
    def search():
        query = request.args.get("q", "").strip()
        try:
            platform = _platform(request.args.get("platform"))
        except ValueError as error:
            return jsonify(error=str(error)), 400
        if len(query) > 100:
            return jsonify(error="q must contain at most 100 characters"), 400
        recent = [
            {
                "username": item["username"],
                "platform": item["platform"],
                "avatar_url": _avatar_url(item["username"], item["platform"]),
            }
            for item in store.recent_searches(platform)
        ]
        if not query:
            return jsonify(recent=recent, results=[])
        needle = query.lstrip("@").lower()
        seen = set()
        results = []
        for username in store.search_known(needle, platform):
            seen.add(username)
            results.append(_known_result(username, platform))
        if not results:
            try:
                if platform == "twitch":
                    if TWITCH_HANDLE_RE.fullmatch(needle):
                        results.append(_known_result(needle, platform))
                else:
                    for item in search_profiles(needle):
                        username = (item.get("username") or "").lower()
                        if not username or username in seen:
                            continue
                        seen.add(username)
                        results.append(
                            {
                                **item,
                                "username": username,
                                "platform": platform,
                                "avatar_url": _avatar_url(username, platform)
                                or item.get("avatar_url"),
                            }
                        )
            except Exception:
                pass
        return jsonify(recent=recent, results=results)

    @app.get("/api/avatars/<username>")
    def avatar(username):
        try:
            platform = _platform(request.args.get("platform"))
        except ValueError:
            abort(404)
        if not _handle_re(platform).fullmatch(username):
            abort(404)
        path = avatar_file(avatars_dir, username.lower(), platform)
        if not path:
            abort(404)
        return send_file(path, max_age=86400)

    @app.post("/api/recordings")
    def create_recording():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("query"), str):
            return jsonify(error="query is required"), 400
        try:
            username, platform = _username_from_query(
                body["query"], body.get("platform")
            )
            duration = _duration(body.get("duration_seconds"), DEFAULT_DURATION)
        except ValueError as error:
            return jsonify(error=str(error)), 400

        payload, status = _queue_recording(username, platform, duration)
        return jsonify(payload), status

    @app.post("/api/game/players")
    def game_players():
        auth_error = _game_auth_error(app)
        if auth_error:
            return auth_error

        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("players"), list):
            return jsonify(error="players is required"), 400
        if not 1 <= len(body["players"]) <= 20:
            return jsonify(error="players must contain 1 to 20 entries"), 400
        try:
            duration = _duration(body.get("duration_seconds"), DEFAULT_DURATION)
        except ValueError as error:
            return jsonify(error=str(error)), 400

        players = []
        seen = set()
        for item in body["players"]:
            if not isinstance(item, dict) or not isinstance(
                item.get("username"), str
            ):
                return jsonify(error="Each player needs a username"), 400
            try:
                platform = item.get("platform")
                if isinstance(platform, str):
                    platform = platform.lower()
                platform = _platform(platform)
            except ValueError as error:
                return jsonify(error=str(error)), 400
            username = item["username"].strip().lstrip("@").lower()
            if not _handle_re(platform).fullmatch(username):
                return jsonify(
                    error=f"Invalid {platform} username: {username or '(empty)'}"
                ), 400
            if (username, platform) not in seen:
                seen.add((username, platform))
                players.append((username, platform))

        results = []
        for username, platform in players:
            payload, status = _queue_recording(username, platform, duration)
            result = {
                "username": username,
                "platform": platform,
                "status": payload.get("status")
                or ("error" if "error" in payload else None),
                "duration_seconds": payload.get("duration_seconds", duration),
            }
            if "id" in payload:
                result["recording_id"] = payload["id"]
            if "error" in payload:
                result["error"] = payload["error"]
            results.append(result)
        return jsonify(results=results), 200

    @app.post("/api/game/events")
    def game_events():
        auth_error = _game_auth_error(app)
        if auth_error:
            return auth_error

        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("events"), list):
            return jsonify(error="events is required"), 400
        if not 1 <= len(body["events"]) <= GAME_EVENT_MAX:
            return jsonify(
                error=f"events must contain 1 to {GAME_EVENT_MAX} entries"
            ), 400
        now = time.time()
        try:
            items = [_game_event(item, now) for item in body["events"]]
        except ValueError as error:
            return jsonify(error=str(error)), 400

        results = [None] * len(items)
        grouped = {}
        for index, item in enumerate(items):
            recording = store.recording_for_event(
                item["username"], item["platform"], item["event_at"]
            )
            if not recording:
                results[index] = {
                    "platform": item["platform"],
                    "username": item["username"],
                    "status": "ignored",
                    "reason": "no_recording",
                }
                continue
            grouped.setdefault(recording["id"], []).append((index, item, recording))
            results[index] = {
                "platform": item["platform"],
                "username": item["username"],
                "status": "ignored",
                "reason": "not_stored",
            }

        for recording_id, entries in grouped.items():
            stored = store.add_recording_events(
                recording_id,
                [{**item, "created_at": now} for _, item, _ in entries],
            )
            for (index, item, recording), was_stored in zip(entries, stored):
                started_at = recording["started_at"]
                results[index] = {
                    "platform": item["platform"],
                    "username": item["username"],
                    "status": "stored" if was_stored else "duplicate",
                    "recording_id": recording_id,
                    "offset_seconds": (
                        round(item["event_at"] - started_at, 3)
                        if started_at is not None
                        else None
                    ),
                }
        return jsonify(results=results), 200

    @app.get("/api/recordings")
    def recordings():
        return jsonify(
            recordings=[_recording_payload(item) for item in store.recordings()]
        )

    @app.get("/api/recordings/<int:recording_id>")
    def get_recording(recording_id):
        recording = store.recording(recording_id)
        if not recording:
            abort(404)
        return jsonify(_recording_payload(recording))

    @app.get("/api/recordings/<int:recording_id>/events")
    def get_recording_events(recording_id):
        recording = store.recording(recording_id)
        if not recording:
            abort(404)
        started_at = recording["started_at"]
        events = []
        for event in store.recording_events(recording_id):
            offset = None
            if started_at is not None:
                offset = round(event["event_at"] - started_at, 3)
                if offset < 0:
                    continue
            events.append(
                {
                    **event,
                    "offset_seconds": offset,
                    "seek_seconds": (
                        max(0.0, round(offset - EVENT_LEAD_SECONDS, 3))
                        if offset is not None
                        else None
                    ),
                }
            )
        return jsonify(events=events)

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
        return jsonify(_recording_payload(store.recording(recording_id)))

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
        return jsonify(_recording_payload(recording))

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
            return jsonify(
                watchlist=[
                    {
                        **item,
                        "avatar_url": _avatar_url(
                            item["username"], item.get("platform") or "tiktok"
                        ),
                    }
                    for item in store.watchlist()
                ]
            )
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return jsonify(error="JSON body must be an object"), 400
        if request.method == "DELETE":
            username = request.args.get("username") or body.get("username")
            if not isinstance(username, str) or not username:
                return jsonify(error="username is required"), 400
            try:
                platform = _platform(
                    request.args.get("platform") or body.get("platform")
                )
            except ValueError as error:
                return jsonify(error=str(error)), 400
            with store.connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM watchlist WHERE username = ? AND platform = ?",
                    (username, platform),
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
            username, platform = _username_from_query(username, body.get("platform"))
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
        _remember(username, platform)
        with store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM watchlist WHERE username = ? AND platform = ?",
                (username, platform),
            ).fetchone()
            connection.execute(
                "INSERT INTO watchlist(username, platform, mode, duration_seconds, "
                "max_duration_seconds, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(username, platform) DO UPDATE SET "
                "mode = excluded.mode, duration_seconds = excluded.duration_seconds, "
                "max_duration_seconds = excluded.max_duration_seconds, last_error = NULL",
                (username, platform, mode, duration, maximum, time.time()),
            )
            item = connection.execute(
                "SELECT * FROM watchlist WHERE username = ? AND platform = ?",
                (username, platform),
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
