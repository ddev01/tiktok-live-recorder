# Game webhook client guide (PC side)

This guide is for the AI building the Fortnite-side client that sends Twitch and TikTok usernames to the recorder.

## What the client does

On the configured key press, collect the platform and exact handle for each streamer in the current match, then send **one signed HTTP request** containing 1–20 players.

The server queues recordings for live accounts and returns one result per unique player. The client does not wait for the recordings to finish.

## Endpoint

```text
POST https://tt.peltux.xyz/api/game/players
Content-Type: application/json
```

## Credentials

Three values are configured locally:

| Setting | Purpose |
| --- | --- |
| `RECORDER_URL` | `https://tt.peltux.xyz/api/game/players` |
| `RECORDER_TOKEN` | Static token sent in `X-TT-Token`. |
| `RECORDER_HMAC_SECRET` | HMAC key used to sign the request. Never send this value. |

`RECORDER_TOKEN` and `RECORDER_HMAC_SECRET` correspond to the server's `TT_GAME_WEBHOOK_TOKEN` and `TT_GAME_WEBHOOK_SECRET`. If the client currently uses `STREAMER_API_URL` / `STREAMER_API_TOKEN`, replace that path: this endpoint accepts a batch, needs the token header, and needs the HMAC signature.

Store both in the OS credential store or a local config file with restrictive permissions. Never put them in Git, logs, screenshots, crash reports, or the game overlay.

## Request body

```json
{
  "players": [
    {"platform": "twitch", "username": "exact_twitch_handle"},
    {"platform": "tiktok", "username": "exact_tiktok_handle"}
  ],
  "duration_seconds": 1320
}
```

Rules:

- `players` is required and must contain 1–20 objects.
- `platform` is `twitch` or `tiktok`. Case does not matter.
- `username` must be the exact platform handle, not the display name.
- The server lowercases handles and removes a leading `@`.
- Valid Twitch handles: 3–25 letters, digits, or `_`.
- Valid TikTok handles: 1–24 letters, digits, `.`, or `_`.
- Duplicate `(platform, username)` pairs in one request are ignored.
- `duration_seconds` is optional. Omit it to use the server default of `1320` seconds (22 minutes). If provided, it must be one of `30`, `900`, `1320`, `1800`, `3600`, `14400`, or `28800`.
- An invalid handle rejects the whole request with `400`, so filter out obvious display names before sending. Do not silently replace a bad handle with a guess.

## Signing

Build the JSON once, as compact UTF-8 bytes:

```python
body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
```

Then:

```python
timestamp = str(int(time.time()))
signature = hmac.new(
    secret.encode("utf-8"),
    timestamp.encode("utf-8") + b"." + body,
    hashlib.sha256,
).hexdigest()
```

Send:

| Header | Value |
| --- | --- |
| `Content-Type` | `application/json` |
| `X-TT-Token` | The static token. |
| `X-TT-Timestamp` | The same `timestamp` string used for signing. |
| `X-TT-Signature` | The lowercase hex signature. |

Critical: sign the exact bytes you send. Do not serialize the body again after signing.

The timestamp must be within 5 minutes of the server clock. A request signed 6 minutes ago is rejected.

## Reference Python client

This repo already uses only the standard library for HTTP, so the example does the same.

```python
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

URL = os.environ["RECORDER_URL"]
TOKEN = os.environ["RECORDER_TOKEN"]
SECRET = os.environ["RECORDER_HMAC_SECRET"]


def send_players(players, duration_seconds=None):
    payload = {"players": players}
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = str(int(time.time()))
    signature = hmac.new(
        SECRET.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    request = urllib.request.Request(
        URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-TT-Token": TOKEN,
            "X-TT-Timestamp": timestamp,
            "X-TT-Signature": signature,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))["results"]
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {error.code}: {detail}") from error
```

At one or two requests per match, opening a new TLS connection is irrelevant. If the client ever sends many requests, ask before adding a dependency and reuse a session.

## Response

Successful requests return HTTP `200`:

```json
{
  "results": [
    {
      "username": "exact_twitch_handle",
      "platform": "twitch",
      "status": "starting",
      "duration_seconds": 1320,
      "recording_id": 42
    },
    {
      "username": "offline_user",
      "platform": "tiktok",
      "status": "offline",
      "duration_seconds": 1320
    }
  ]
}
```

Possible `status` values:

| Status | Meaning | Client action |
| --- | --- | --- |
| `starting` | Recording was queued. | None. |
| `recording` | That account already has an active recording. | None. |
| `offline` | Account is not live. | None. It may be reported to the user. |
| `error` | This player could not be queued. | Log `error`; do not retry the whole batch automatically. |

`recording_id` appears when a recording was queued or was already active.

## HTTP errors

| Code | Meaning | Client action |
| --- | --- | --- |
| `200` | Processed. Inspect per-player results. | Continue. |
| `400` | Invalid JSON, too many players, or invalid handle. | Fix the request. Do not retry unchanged. |
| `401` | Bad token, bad signature, stale timestamp, or clock skew. | Check credentials and system clock. Do not hammer the endpoint. |
| `503` | The webhook is not configured on the server. | Tell the user. Retrying will not help. |

Per-player problems such as low disk space appear inside a `200` response as `status: "error"` with an `error` field.

## Kill and knock events

`POST /api/game/events` records interactions between your squad and a recorded streamer's squad. It uses the same URL host, token, HMAC secret, headers and signing as the players batch. If `RECORDER_URL` ends in `/players`, replace that segment with `/events`.

Send one request per event as soon as the feed row arrives. Do not wait for the match to end.

```json
{
  "events": [
    {
      "platform": "twitch",
      "username": "bop123",
      "kind": "knock",
      "event_at": 1762361234.5,
      "actor": "Sophie123",
      "actor_role": "teammate",
      "victim": "TTV_bop123",
      "victim_role": "streamer"
    }
  ]
}
```

Rules:

- `kind` is `knock` or `elimination`.
- `actor_role` and `victim_role` are `self`, `teammate`, `streamer`, or `streamer_teammate`.
- Exactly one side must be your team (`self`/`teammate`) and the other the streamer's team (`streamer`/`streamer_teammate`). A request with both sides on one team is rejected with `400`.
- `username` is the recorded streamer's platform handle from the players request. `actor` and `victim` are the exact in-game names from the feed.
- `event_at` is the PC's Unix time when the feed row arrived. The server requires it within the last 24 hours and no more than 60 seconds in the future.
- Duplicates are ignored server-side by `(recording, event_at, kind, actor, victim)`, so a retry is safe.
- The client does not need to track recording IDs: the server resolves the recording for `(platform, username)` at `event_at`.

Response `200`:

```json
{
  "results": [
    {
      "platform": "twitch",
      "username": "bop123",
      "status": "stored",
      "recording_id": 42,
      "offset_seconds": 312.5
    }
  ]
}
```

`status` is `stored`, `duplicate`, or `ignored`. An `ignored` result has a `reason`; `no_recording` means the account was not being recorded around `event_at`. Treat `ignored` as informational and do not retry it.

The server computes `offset_seconds` from the recording start and the website seeks 10 seconds before the event. The record's own playback latency is not corrected.

## Retries and idempotency

The endpoint is effectively idempotent for active recordings: sending the same player again returns `recording` instead of creating a second capture.

For a network timeout or connection error, retry at most once with a fresh timestamp and signature. Do not automatically retry `400`, `401`, or `503`.

Do not block the game thread while the request runs. Put the request on a small background worker or queue so a slow network cannot affect gameplay.

## Clock and time

The client and server must agree on the time within 5 minutes. Use the operating system's automatic time sync. If requests suddenly return `401`, check the clock before changing credentials.

## Pre-flight checklist

Before reporting the client as complete:

1. The URL uses `https`.
2. The token and secret are loaded from configuration, not source code.
3. The client sends 1–20 players and skips entries that are not exact handles.
4. The signature is computed over the exact bytes sent.
5. A stale timestamp is rejected.
6. A wrong token is rejected.
7. A valid request returns `200` with one result per unique player.
8. Tokens and secrets never appear in logs.
9. The request runs off the game thread with a timeout.
10. Kill/knock events are sent immediately with an `event_at`, and a retry reuses the same `event_at`.
11. Each event names the side for both the actor and the victim, so "me killing the streamer" and "my teammate killing the streamer" are distinguishable.

## Questions for the PC-side AI

Answer these before adding features beyond the contract above:

1. Does the overlay provide exact platform handles, or only display names?
2. Can a single match produce more than 20 streamers?
3. Does each player need its own recording duration, or is one duration per request enough?
4. Should the response explicitly say `already_recording: true` instead of overloading `status: "recording"`?
5. Does the client need to poll recording status or receive a recording URL?
6. Should the client send an idempotency key, or is active-recording deduplication enough?
7. Does the client need a health endpoint to check credentials before a game?
8. Should the API accept a different field shape, such as a flat list of players?

Ask the user before implementing any of these additions; the current contract is deliberately small.
