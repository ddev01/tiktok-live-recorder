import os
import re
import signal
import subprocess
import sys
from pathlib import Path


HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,24}$")
DEFAULT_COOKIE_FILE = "/run/secrets/tiktok_session_ss"


def cookie_file():
    path = os.environ.get("TIKTOK_SESSION_SS_FILE", DEFAULT_COOKIE_FILE)
    return path if Path(path).is_file() else None


def is_live(username):
    if not HANDLE_RE.fullmatch(username):
        raise ValueError("Invalid TikTok username")

    from streamlink import Streamlink

    session = Streamlink()
    cookies = cookie_file()
    try:
        if cookies:
            session.http.set_cookies_from_file(cookies)
        streams = session.streams(f"https://www.tiktok.com/@{username}/live")
        return any(not quality.endswith("_ao") for quality in streams)
    finally:
        session.http.close()


class StreamCapture:
    """Pipe Streamlink's origin stream directly into FFmpeg without re-encoding."""

    def __init__(self, username, output):
        if not HANDLE_RE.fullmatch(username):
            raise ValueError("Invalid TikTok username")
        self.output = Path(output)
        self._streamlink_signalled = False
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
        cookies = cookie_file()
        if cookies:
            command.extend(("--http-cookies-file", cookies))
        command.extend((f"https://www.tiktok.com/@{username}/live", "best"))

        self.streamlink = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        self.pid = self.streamlink.pid
        try:
            self.ffmpeg = subprocess.Popen(
                [
                    "ffmpeg",
                    "-n",
                    "-f",
                    "flv",
                    "-i",
                    "pipe:0",
                    "-c",
                    "copy",
                    str(self.output),
                ],
                stdin=self.streamlink.stdout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        except Exception:
            self.streamlink.send_signal(signal.SIGINT)
            self.streamlink.stdout.close()
            self.streamlink.wait()
            raise
        else:
            self.streamlink.stdout.close()

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
        if not self.finalized:
            self.output.unlink(missing_ok=True)
        return self.ffmpeg.returncode

    @property
    def interrupted(self):
        return self.streamlink.returncode not in (0, -signal.SIGINT)

    @property
    def finalized(self):
        return (
            self.ffmpeg.returncode == 0
            and self.output.is_file()
            and self.output.stat().st_size > 0
        )
