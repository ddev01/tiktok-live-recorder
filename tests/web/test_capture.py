import signal
import shutil
import subprocess
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
    assert calls == ["https://www.tiktok.com/@creator/live"]
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
                output.write_bytes(b"finalized mp4")
            return self.returncode

    pipe = SimpleNamespace(close=lambda: calls.append(("close",)))

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return Child(123, pipe) if len(calls) == 1 else Child(456)

    monkeypatch.setattr(capture.subprocess, "Popen", popen)
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
    assert streamlink_kwargs.get("shell", False) is False
    assert ffmpeg_kwargs.get("shell", False) is False
    assert ffmpeg_args == [
        "ffmpeg",
        "-n",
        "-f",
        "flv",
        "-i",
        "pipe:0",
        "-c",
        "copy",
        str(output),
    ]
    assert ffmpeg_kwargs["stdin"] is pipe
    assert calls[-1] == ("close",)

    process.send_signal(signal.SIGINT)
    assert process.streamlink.signals == [signal.SIGINT]
    process.wait()
    assert process.finalized is True
    assert process.interrupted is False


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


def test_failed_ffmpeg_capture_removes_unfinalized_mp4(tmp_path, monkeypatch):
    output = tmp_path / "capture.mp4"

    class Child:
        def __init__(self, pid, stdout=None):
            self.pid = pid
            self.stdout = stdout
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self):
            self.returncode = 1 if self.pid == 456 else 0
            if self.pid == 456:
                output.write_bytes(b"incomplete")
            return self.returncode

    children = iter((Child(123, SimpleNamespace(close=lambda: None)), Child(456)))
    monkeypatch.setattr(
        capture.subprocess, "Popen", lambda *_args, **_kwargs: next(children)
    )
    monkeypatch.delenv("TIKTOK_SESSION_SS_FILE", raising=False)
    process = capture.StreamCapture("creator", output)
    process.wait()
    assert process.finalized is False
    assert not output.exists()


def test_synthetic_flv_is_captured_and_remuxed_without_tiktok(tmp_path, monkeypatch):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the local capture pipeline test")
    source = tmp_path / "fixture.flv"
    output = tmp_path / "recording.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=32x32:rate=1:duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-f",
            "flv",
            "-y",
            str(source),
        ],
        check=True,
    )
    original_popen = capture.subprocess.Popen

    def fake_streamlink(command, **kwargs):
        assert command[:4] == [sys.executable, "-m", "streamlink", "--stdout"]
        return original_popen(
            [
                sys.executable,
                "-c",
                "import pathlib, sys; sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())",
                str(source),
            ],
            **kwargs,
        )

    def pipeline_popen(command, **kwargs):
        if command[:3] == [sys.executable, "-m", "streamlink"]:
            return fake_streamlink(command, **kwargs)
        return original_popen(command, **kwargs)

    monkeypatch.setattr(capture.subprocess, "Popen", pipeline_popen)
    monkeypatch.delenv("TIKTOK_SESSION_SS_FILE", raising=False)

    process = capture.StreamCapture("creator", output)
    assert process.wait() == 0
    assert process.finalized
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(output),
            "-f",
            "null",
            "-",
        ],
        check=True,
    )
