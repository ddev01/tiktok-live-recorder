import io
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

from utils.utils import cookie_file


HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,24}$")
TWITCH_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{3,25}$")
PLATFORMS = ("tiktok", "twitch")
# Streamlink weighs frame rate as well, so ">1080p60" keeps 1080p/1080p60 renditions
# while dropping 1440p and above.
TWITCH_QUALITY_CAP = ">1080p60"
ERROR_TAIL_CHARS = 400


def stream_url(username, platform="tiktok"):
    if platform == "twitch":
        if not TWITCH_HANDLE_RE.fullmatch(username):
            raise ValueError("Invalid Twitch username")
        return f"https://www.twitch.tv/{username}"
    if not HANDLE_RE.fullmatch(username):
        raise ValueError("Invalid TikTok username")
    return f"https://www.tiktok.com/@{username}/live"


def relay_bytes(source, sink):
    reader = io.BufferedReader(source)
    try:
        while True:
            chunk = reader.read(64 * 1024)
            if not chunk:
                return
            sink.write(chunk)
            sink.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            sink.close()
        except OSError:
            pass
        reader.close()


def relay_flv(source, sink):
    """Copy complete FLV tags and drop a final tag cut short by stopping Streamlink."""
    reader = io.BufferedReader(source)
    try:
        header = reader.read(9)
        if len(header) != 9 or header[:3] != b"FLV":
            return
        data_offset = int.from_bytes(header[5:9], "big")
        if data_offset < 9:
            return
        padding = reader.read(data_offset - 9 + 4)
        if len(padding) != data_offset - 9 + 4:
            return
        sink.write(header + padding)
        sink.flush()
        while True:
            tag_header = reader.read(11)
            if len(tag_header) != 11:
                return
            size = int.from_bytes(tag_header[1:4], "big")
            body = reader.read(size + 4)
            if len(body) != size + 4:
                return
            sink.write(tag_header + body)
            sink.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            sink.close()
        except OSError:
            pass
        reader.close()


def is_live(username, platform="tiktok"):
    url = stream_url(username, platform)

    from streamlink import Streamlink

    session = Streamlink()
    cookies = cookie_file() if platform == "tiktok" else None
    try:
        if cookies:
            session.http.set_cookies_from_file(cookies)
        streams = session.streams(url)
        return any(
            quality != "audio_only" and not quality.endswith("_ao")
            for quality in streams
        )
    finally:
        session.http.close()


def partial_path(output):
    """Captures land in TS so a stop that breaks the trailer still leaves usable data."""
    return Path(output).with_suffix(".part.ts")


def video_codec(path):
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return ""
    # A TS reports the same stream once per program, so only the first line counts.
    codecs = probe.stdout.strip().splitlines() if probe.returncode == 0 else []
    return codecs[0] if codecs else ""


def remux(source, output, stderr):
    """Write the finished MP4 from the capture file; True only for a playable result."""
    output = Path(output)
    command = ["ffmpeg", "-nostats", "-n", "-i", str(source), "-c", "copy"]
    if video_codec(source) == "hevc":
        command.extend(("-tag:v", "hvc1"))  # QuickTime only plays HEVC tagged hvc1
    command.append(str(output))
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=stderr)
    except OSError:
        result = None
    if (
        not result
        or result.returncode != 0
        or not output.is_file()
        or output.stat().st_size == 0
    ):
        output.unlink(missing_ok=True)
        return False
    return True


class StreamCapture:
    """Copy Streamlink's stream to TS, then remux once to MP4 when the capture ends."""

    def __init__(self, username, output, platform="tiktok"):
        url = stream_url(username, platform)
        self.output = Path(output)
        self.partial = partial_path(self.output)
        self.log = self.output.with_suffix(".log")
        self.error_detail = None
        self._streamlink_signalled = False
        self._stderr = self.log.open("wb")
        command = [
            sys.executable,
            "-m",
            "streamlink",
            "--stdout",
            "--http-timeout",
            "10",
            "--retry-open",
            "2",
            "--loglevel",
            "warning",
        ]
        cookies = cookie_file() if platform == "tiktok" else None
        if cookies:
            command.extend(("--http-cookies-file", cookies))
        if platform == "twitch":
            command.extend(("--stream-sorting-excludes", TWITCH_QUALITY_CAP))
        command.extend((url, "best"))
        ffmpeg_format = "mpegts" if platform == "twitch" else "flv"
        relay = relay_bytes if platform == "twitch" else relay_flv

        try:
            self.streamlink = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                stderr=self._stderr,
                close_fds=True,
            )
        except OSError:
            self._discard_artifacts()
            raise
        self.pid = self.streamlink.pid
        self._relay = None
        try:
            self.ffmpeg = subprocess.Popen(
                [
                    "ffmpeg",
                    "-nostats",
                    "-n",
                    "-f",
                    ffmpeg_format,
                    "-i",
                    "pipe:0",
                    "-c",
                    "copy",
                    str(self.partial),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr,
                close_fds=True,
            )
        except Exception:
            self.streamlink.send_signal(signal.SIGINT)
            self.streamlink.stdout.close()
            self.streamlink.wait()
            self._discard_artifacts()
            raise
        else:
            sink = getattr(self.ffmpeg, "stdin", None)
            if sink is None:
                self.streamlink.stdout.close()
            else:
                self._relay = threading.Thread(
                    target=relay,
                    args=(self.streamlink.stdout, sink),
                    daemon=True,
                )
                self._relay.start()

    def send_signal(self, sig):
        if not self._streamlink_signalled and self.streamlink.poll() is None:
            self.streamlink.send_signal(sig)
            self._streamlink_signalled = True

    def poll(self):
        streamlink_status = self.streamlink.poll()
        ffmpeg_status = self.ffmpeg.poll()
        if ffmpeg_status is not None and streamlink_status is None:
            self.send_signal(signal.SIGINT)
            streamlink_status = self.streamlink.poll()
        return (
            ffmpeg_status
            if streamlink_status is not None and ffmpeg_status is not None
            else None
        )

    def wait(self):
        self.streamlink.wait()
        self.ffmpeg.wait()
        if self._relay is not None:
            self._relay.join(timeout=5)
        captured = self.partial.is_file() and self.partial.stat().st_size > 0
        # A failed remux keeps the raw capture; it is still recoverable by hand.
        if not captured or remux(self.partial, self.output, self._stderr):
            self.partial.unlink(missing_ok=True)
        self._close_stderr()
        if not self.finalized:
            self.error_detail = self._error_tail()
        self.log.unlink(missing_ok=True)
        return self.ffmpeg.returncode

    def _close_stderr(self):
        try:
            self._stderr.close()
        except OSError:
            pass

    def _discard_artifacts(self):
        self._close_stderr()
        self.partial.unlink(missing_ok=True)
        self.log.unlink(missing_ok=True)

    def _error_tail(self):
        try:
            lines = [
                line.strip()
                for line in self.log.read_text(errors="replace").splitlines()
                if line.strip()
            ]
        except OSError:
            return None
        return " | ".join(lines[-3:])[-ERROR_TAIL_CHARS:] or None

    @property
    def interrupted(self):
        return (
            self.streamlink.returncode not in (0, -signal.SIGINT)
            or self.ffmpeg.returncode != 0
        )

    @property
    def finalized(self):
        return self.output.is_file() and self.output.stat().st_size > 0
