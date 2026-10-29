import signal
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from web import dev as dev_module  # noqa: E402


@pytest.fixture(autouse=True)
def reset_worker_proc():
    dev_module._worker_proc = None
    yield
    dev_module._worker_proc = None


def test_start_worker_spawns_once(monkeypatch):
    started = []

    class FakePopen:
        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        started.append((args, kwargs))
        return FakePopen()

    monkeypatch.setattr(dev_module.subprocess, "Popen", fake_popen)
    dev_module.start_worker()
    dev_module.start_worker()
    assert len(started) == 1
    assert started[0][0][0][-2:] == ["-m", "web.worker"]
    assert dev_module.REPO_ROOT / "src" == Path(
        started[0][1]["env"]["PYTHONPATH"].split(dev_module.os.pathsep)[0]
    )


def test_stop_worker_sends_sigint():
    signals = []

    class FakePopen:
        def poll(self):
            return None

        def send_signal(self, sig):
            signals.append(sig)

        def wait(self, timeout=None):
            return 0

    dev_module._worker_proc = FakePopen()
    dev_module.stop_worker()
    assert signals == [signal.SIGINT]
    assert dev_module._worker_proc is None
