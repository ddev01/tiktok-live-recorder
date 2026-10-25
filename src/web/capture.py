import io
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path

from utils.utils import cookie_file


HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,24}$")


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


def retag_hevc(path):
    """QuickTime only plays HEVC tagged hvc1; FFmpeg's pipe muxer writes hev1."""
    path = Path(path)
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,codec_tag_string",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
    except OSError:
        return
    if probe.returncode != 0 or probe.stdout.strip() != "hevc,hev1":
        return

    temp = path.with_name(f"{path.stem}.hvc1.tmp{path.suffix}")
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-c",
                "copy",
                "-tag:v",
                "hvc1",
                str(temp),
            ],
            capture_output=True,
        )
    except OSError:
        return
    if result.returncode == 0 and temp.is_file() and temp.stat().st_size > 0:
        temp.replace(path)
    else:
        temp.unlink(missing_ok=True)


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
        self._relay = None
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
                stdin=subprocess.PIPE,
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
            sink = getattr(self.ffmpeg, "stdin", None)
            if sink is None:
                self.streamlink.stdout.close()
            else:
                self._relay = threading.Thread(
                    target=relay_flv,
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
        if self.ffmpeg.returncode == 0 and self.output.is_file():
            retag_hevc(self.output)
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
