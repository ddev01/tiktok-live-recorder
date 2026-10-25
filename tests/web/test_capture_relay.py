import io
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from web import capture  # noqa: E402


def flv_tag(payload):
    size = len(payload)
    header = (
        b"\x09" + size.to_bytes(3, "big") + b"\x00\x00\x00" + b"\x00" + b"\x00\x00\x00"
    )
    return header + payload + (11 + size).to_bytes(4, "big")


class Sink:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, chunk):
        self.data += chunk

    def flush(self):
        pass

    def close(self):
        self.closed = True


def test_relay_drops_incomplete_final_tag():
    flv_header = b"FLV\x01\x05\x00\x00\x00\x09" + b"\x00\x00\x00\x00"
    complete = flv_tag(b"complete-payload")
    partial = flv_tag(b"truncated-payload")[:-6]
    sink = Sink()

    capture.relay_flv(io.BytesIO(flv_header + complete + partial), sink)

    assert bytes(sink.data) == flv_header + complete
    assert sink.closed


def test_relay_rejects_non_flv_input():
    sink = Sink()
    capture.relay_flv(io.BytesIO(b"not-a-flv-stream"), sink)
    assert bytes(sink.data) == b""
    assert sink.closed


def test_retag_hevc_rewrites_hev1_for_quicktime(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"original")
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="hevc,hev1\n")
        Path(args[-1]).write_bytes(b"retagged")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    capture.retag_hevc(path)

    assert path.read_bytes() == b"retagged"
    assert calls[-1][-1].endswith(".hvc1.tmp.mp4")
    assert calls[-1][calls[-1].index("-tag:v") + 1] == "hvc1"


def test_retag_hevc_leaves_h264_recording_alone(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"original")
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="h264,avc1\n")

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    capture.retag_hevc(path)

    assert path.read_bytes() == b"original"
    assert len(calls) == 1
