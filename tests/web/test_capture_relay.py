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


def test_remux_tags_hevc_as_hvc1_for_quicktime(tmp_path, monkeypatch):
    source = tmp_path / "clip.part.ts"
    source.write_bytes(b"ts")
    output = tmp_path / "clip.mp4"
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="hevc\n\nhevc\n")
        output.write_bytes(b"remuxed")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    assert capture.remux(source, output, None) is True

    assert output.read_bytes() == b"remuxed"
    assert calls[-1][-1] == str(output)
    assert calls[-1][calls[-1].index("-tag:v") + 1] == "hvc1"


def test_remux_leaves_h264_untagged_and_drops_failed_output(tmp_path, monkeypatch):
    source = tmp_path / "clip.part.ts"
    source.write_bytes(b"ts")
    output = tmp_path / "clip.mp4"
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        if args[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="h264\n")
        output.write_bytes(b"broken")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)
    assert capture.remux(source, output, None) is False

    assert "-tag:v" not in calls[-1]
    assert not output.exists()
