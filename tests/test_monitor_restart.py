"""Watch OFF then ON while the monitor is stuck in a slow step (a NAS or a
spun-down disk) must not leave two monitor loops running (concurrency-2).

Each watch session owns its stop event; starting a new session replaces the
state's event instead of clearing the old one, so the old loop can never be
"revived" and exits as soon as its slow step returns."""
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import monitor  # noqa: E402


class _State:
    def __init__(self):
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.monitored_folder = "X:\\library"
        self.processed_lock = threading.Lock()
        self.processed_videos = set()
        self._shutting_down = False
        self.ffmpeg_available = False
        self.config = monitor.AppConfig(font="Arial", color="00FFFF")


class _DB:
    def add_processed_videos_batch(self, videos):
        pass

    def get_processed_videos(self):
        return []

    def get_monitored_files(self):
        return {}


class _SlowProcessor:
    """list_ass_files takes `delay` seconds and records which monitor scanned."""
    def __init__(self, delay):
        self.delay = delay
        self.scans = []

    def list_ass_files(self, folder):
        self.scans.append(threading.current_thread().name)
        time.sleep(self.delay)
        return []

    def is_video_file(self, name):
        return False


class MonitorRestartTests(unittest.TestCase):
    def setUp(self):
        self._orig = (monitor._WATCHDOG_AVAILABLE, monitor._POLL_NOWATCH)
        monitor._WATCHDOG_AVAILABLE = False  # no real observer in a unit test
        monitor._POLL_NOWATCH = 0.2

    def tearDown(self):
        monitor._WATCHDOG_AVAILABLE, monitor._POLL_NOWATCH = self._orig

    def test_restart_during_a_slow_scan_never_runs_two_loops(self):
        state, db, proc = _State(), _DB(), _SlowProcessor(delay=1.0)

        old = threading.Thread(target=monitor.FolderMonitor(state, db, proc).run,
                               name="monitor-1", daemon=True)
        old.start()
        time.sleep(0.3)                      # old loop is now inside the slow scan

        # Watch OFF: stop the current session; the join times out.
        state.stop_event.set()
        state.wake_event.set()
        old.join(timeout=0.1)
        self.assertTrue(old.is_alive())

        # Watch ON: a NEW session gets a NEW stop event (the old one stays set).
        state.stop_event = threading.Event()
        state.wake_event.clear()
        new = threading.Thread(target=monitor.FolderMonitor(state, db, proc).run,
                               name="monitor-2", daemon=True)
        new.start()

        old.join(timeout=3.0)
        self.assertFalse(old.is_alive(), "the stopped loop must exit, not be revived")
        time.sleep(1.5)
        later = proc.scans[2:]
        self.assertTrue(later, "the new session should keep scanning")
        self.assertEqual(set(later), {"monitor-2"})

        state.stop_event.set()
        state.wake_event.set()
        new.join(timeout=3.0)


if __name__ == "__main__":
    unittest.main()
