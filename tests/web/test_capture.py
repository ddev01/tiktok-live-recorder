import signal
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from web import capture  # noqa: E402


def fake_streamlink(monkeypatch, streams):
    session = SimpleNamespace(
        http=SimpleNamespace(
            set_cookies_from_file=lambda path: None, close=lambda: None
        ),
        streams=streams,
    )
    monkeypatch.setitem(sys.modules, "streamlink", ModuleType("streamlink"))
    monkeypatch.setattr(
        sys.modules["streamlink"], "Streamlink", lambda: session, raising=False
    )
    return session


def test_live_check_uses_streamlink_with_configured_cookie_file(tmp_path, monkeypatch):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("test-only", encoding="utf-8")
    monkeypatch.setenv("TIKTOK_SESSION_SS_FILE", str(cookie_file))
    calls = []
    session = fake_streamlink(
        monkeypatch, lambda url: calls.append(url) or {"best": object()}
    )
    cookie_calls = []
    session.http.set_cookies_from_file = cookie_calls.append

    assert capture.is_live("creator") is True
    assert capture.is_live("test_user_01", "twitch") is True
    assert calls == [
        "https://www.tiktok.com/@creator/live",
        "https://www.twitch.tv/test_user_01",
    ]
    assert cookie_calls == [str(cookie_file)]


def test_live_check_treats_empty_streams_as_offline_and_propagates_errors(monkeypatch):
    fake_streamlink(monkeypatch, lambda _url: {})
    assert capture.is_live("creator") is False
    fake_streamlink(monkeypatch, lambda _url: {"h264_ao": object()})
    assert capture.is_live("creator") is False

    def fail(_url):
        raise RuntimeError("Streamlink failure")

    fake_streamlink(monkeypatch, fail)
    with pytest.raises(RuntimeError, match="Streamlink failure"):
        capture.is_live("creator")


def test_capture_uses_cookie_path_only_and_pipes_best_stream_to_copy(
    tmp_path, monkeypatch
):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("test-only-cookie-value", encoding="utf-8")
    monkeypatch.setenv("TIKTOK_SESSION_SS_FILE", str(cookie_file))
    output = tmp_path / "capture.mp4"
    partial = capture.partial_path(output)
    calls = []

    class Child:
        def __init__(self, pid, stdout=None):
            self.pid = pid
            self.stdout = stdout
            self.returncode = None
            self.signals = []

        def poll(self):
            return self.returncode

        def send_signal(self, value):
            self.signals.append(value)

        def wait(self):
            self.returncode = -signal.SIGINT if self.pid == 123 else 0
            if self.pid == 456:
                partial.write_bytes(b"captured ts")
            return self.returncode

    pipe = SimpleNamespace(close=lambda: calls.append(("close",)))

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Child(123, pipe) if len(calls) == 1 else Child(456)

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="h264\n")
        output.write_bytes(b"finalized mp4")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    monkeypatch.setattr(capture.subprocess, "run", run)
    process = capture.StreamCapture("creator", output)
    streamlink_args, streamlink_kwargs = calls[0]
    ffmpeg_args, ffmpeg_kwargs = calls[1]
    assert streamlink_args[0:4] == [sys.executable, "-m", "streamlink", "--stdout"]
    assert "--http-cookies-file" in streamlink_args
    assert streamlink_args[streamlink_args.index("--http-cookies-file") + 1] == str(
        cookie_file
    )
    assert "test-only-cookie-value" not in streamlink_args
    assert streamlink_args[-2:] == ["https://www.tiktok.com/@creator/live", "best"]
    assert "--stream-sorting-excludes" not in streamlink_args
    assert streamlink_kwargs.get("shell", False) is False
    assert ffmpeg_kwargs.get("shell", False) is False
    assert ffmpeg_args == [
        "ffmpeg",
        "-nostats",
        "-n",
        "-f",
        "flv",
        "-i",
        "pipe:0",
        "-c",
        "copy",
        str(partial),
    ]
    assert ffmpeg_kwargs["stdin"] is capture.subprocess.PIPE
    assert calls[2] == ("close",)

    process.send_signal(signal.SIGINT)
    assert process.streamlink.signals == [signal.SIGINT]
    process.wait()
    assert process.finalized is True
    assert process.interrupted is False
    assert output.read_bytes() == b"finalized mp4"
    assert not partial.exists()
    assert calls[-1][0][-1] == str(output)


def test_twitch_capture_uses_mpegts_without_tiktok_cookies(tmp_path, monkeypatch):
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("test-only-cookie-value", encoding="utf-8")
    monkeypatch.setenv("TIKTOK_SESSION_SS_FILE", str(cookie_file))
    output = tmp_path / "capture.mp4"
    calls = []

    class Child:
        def __init__(self, pid, stdout=None):
            self.pid = pid
            self.stdout = stdout
            self.returncode = 0

        def poll(self):
            return self.returncode

        def send_signal(self, value):
            pass

        def wait(self):
            if self.pid == 456:
                output.write_bytes(b"finalized mp4")
            return self.returncode

    pipe = SimpleNamespace(close=lambda: None)

    def popen(command, **kwargs):
        calls.append(command)
        return Child(123, pipe) if len(calls) == 1 else Child(456)

    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    capture.StreamCapture("test_user_01", output, platform="twitch")
    assert calls[0][-2:] == ["https://www.twitch.tv/test_user_01", "best"]
    assert "--http-cookies-file" not in calls[0]
    assert (
        calls[0][calls[0].index("--stream-sorting-excludes") + 1]
        == capture.TWITCH_QUALITY_CAP
    )
    assert calls[1][calls[1].index("-f") + 1] == "mpegts"
    assert calls[1][-1] == str(capture.partial_path(output))


def test_capture_stops_streamlink_if_ffmpeg_cannot_start(tmp_path, monkeypatch):
    events = []

    class StreamlinkChild:
        pid = 123
        stdout = SimpleNamespace(close=lambda: events.append("close"))

        def send_signal(self, value):
            events.append(("signal", value))

        def wait(self):
            events.append("wait")

    children = iter((StreamlinkChild(),))

    def popen(*_args, **_kwargs):
        try:
            return next(children)
        except StopIteration:
            raise FileNotFoundError("ffmpeg missing")

    monkeypatch.setattr(capture.subprocess, "Popen", popen)
    monkeypatch.delenv("TIKTOK_SESSION_SS_FILE", raising=False)
    with pytest.raises(FileNotFoundError, match="ffmpeg missing"):
        capture.StreamCapture("creator", tmp_path / "capture.mp4")
    assert events == [("signal", signal.SIGINT), "close", "wait"]


def test_capture_without_remuxable_data_leaves_no_mp4(tmp_path, monkeypatch):
    output = tmp_path / "capture.mp4"

    class Child:
        def __init__(self, pid, stdout=None):
            self.pid = pid
            self.stdout = stdout
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self):
            self.returncode = 1
            return self.returncode

    children = iter((Child(123, SimpleNamespace(close=lambda: None)), Child(456)))
    monkeypatch.setattr(
        capture.subprocess, "Popen", lambda *_args, **_kwargs: next(children)
    )
    monkeypatch.setattr(
        capture.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("must not remux an empty capture"),
    )
    monkeypatch.delenv("TIKTOK_SESSION_SS_FILE", raising=False)
    process = capture.StreamCapture("creator", output)
    process.wait()
    assert process.finalized is False
    assert not output.exists()
    assert not capture.partial_path(output).exists()


def test_remux_saves_playable_mp4_when_the_capture_exits_badly(tmp_path, monkeypatch):
    output = tmp_path / "capture.mp4"
    partial = capture.partial_path(output)

    class Child:
        def __init__(self, pid, stdout=None):
            self.pid = pid
            self.stdout = stdout
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self):
            self.returncode = 1
            if self.pid == 456:
                partial.write_bytes(b"captured ts")
            return self.returncode

    children = iter((Child(123, SimpleNamespace(close=lambda: None)), Child(456)))

    def run(command, **_kwargs):
        if command[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="h264\n")
        output.write_bytes(b"playable mp4")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(
        capture.subprocess, "Popen", lambda *_args, **_kwargs: next(children)
    )
    monkeypatch.setattr(capture.subprocess, "run", run)
    monkeypatch.delenv("TIKTOK_SESSION_SS_FILE", raising=False)
    process = capture.StreamCapture("creator", output)
    process.wait()
    assert process.finalized is True
    assert process.interrupted is True
    assert output.read_bytes() == b"playable mp4"
    assert not partial.exists()


def test_failed_remux_keeps_the_raw_capture_for_recovery(tmp_path, monkeypatch):
    output = tmp_path / "capture.mp4"
    partial = capture.partial_path(output)

    class Child:
        def __init__(self, pid, stdout=None):
            self.pid = pid
            self.stdout = stdout
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self):
            self.returncode = 0
            if self.pid == 456:
                partial.write_bytes(b"captured ts")
            return self.returncode

    children = iter((Child(123, SimpleNamespace(close=lambda: None)), Child(456)))

    def run(command, **_kwargs):
        if command[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="h264\n")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(
        capture.subprocess, "Popen", lambda *_args, **_kwargs: next(children)
    )
    monkeypatch.setattr(capture.subprocess, "run", run)
    monkeypatch.delenv("TIKTOK_SESSION_SS_FILE", raising=False)
    process = capture.StreamCapture("creator", output)
    process.wait()
    assert process.finalized is False
    assert partial.read_bytes() == b"captured ts"
    assert not output.exists()


def test_capture_keeps_the_last_stderr_lines_for_the_error(tmp_path, monkeypatch):
    output = tmp_path / "capture.mp4"
    log = output.with_suffix(".log")

    class Child:
        def __init__(self, pid, stdout=None):
            self.pid = pid
            self.stdout = stdout
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self):
            self.returncode = 1
            if self.pid == 456:
                log.write_bytes(b"warning\nError opening input file pipe:0\n")
            return self.returncode

    children = iter((Child(123, SimpleNamespace(close=lambda: None)), Child(456)))
    monkeypatch.setattr(
        capture.subprocess, "Popen", lambda *_args, **_kwargs: next(children)
    )
    monkeypatch.delenv("TIKTOK_SESSION_SS_FILE", raising=False)
    process = capture.StreamCapture("creator", output)
    process.wait()
    assert process.finalized is False
    assert "Error opening input file pipe:0" in process.error_detail
    assert not log.exists()
