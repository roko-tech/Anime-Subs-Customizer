"""Single-instance mutex and the --quit signal the installer relies on.

A fake "running app" child process owns the mutex and waits for the quit event;
the test asks it to quit the way the installer does and checks it really exits.
Every name carries a unique token so nothing here can touch a real running copy.
"""
import subprocess
import sys
import textwrap
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import instance  # noqa: E402

WINDOWS = sys.platform.startswith("win")

CHILD = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, sys.argv[1])
    from anime_subs import instance
    guard = instance.SingleInstance(token=sys.argv[2])
    if not guard.acquire():
        sys.exit(3)
    guard.listen_for_quit(lambda: os._exit(0))
    print("ready", flush=True)
    time.sleep(60)
    sys.exit(4)
""")


@unittest.skipUnless(WINDOWS, "named kernel objects are Windows-only")
class SingleInstanceTests(unittest.TestCase):
    def setUp(self):
        self.token = "pytest-" + uuid.uuid4().hex[:10]

    def _spawn(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(ROOT), self.token],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        return proc

    def test_nothing_running_means_quit_succeeds_immediately(self):
        self.assertFalse(instance.is_running(self.token))
        t0 = time.monotonic()
        self.assertEqual(instance.request_quit(self.token, timeout=5), 0)
        self.assertLess(time.monotonic() - t0, 1.0)

    def test_second_acquire_in_same_name_fails(self):
        first = instance.SingleInstance(token=self.token)
        self.assertTrue(first.acquire())
        self.addCleanup(first.release)
        second = instance.SingleInstance(token=self.token)
        self.assertFalse(second.acquire())
        second.release()

    def test_quit_request_makes_the_running_instance_exit(self):
        proc = self._spawn()
        self.assertTrue(instance.is_running(self.token))
        self.assertEqual(instance.request_quit(self.token, timeout=10), 0)
        self.assertEqual(proc.wait(timeout=5), 0)
        self.assertFalse(instance.is_running(self.token))

    def test_tokens_isolate_instances(self):
        proc = self._spawn()
        other = "pytest-other-" + uuid.uuid4().hex[:6]
        self.assertFalse(instance.is_running(other))
        self.assertEqual(instance.request_quit(other, timeout=2), 0)
        self.assertIsNone(proc.poll(), "quitting another token stopped this instance")

    def test_object_names_match_the_installer(self):
        # The installer's AppMutex uses the bare name; a token only suffixes it.
        self.assertEqual(instance.mutex_name(""), "ASS-Subs-Changer-Singleton")
        self.assertEqual(instance.mutex_name("t"), "ASS-Subs-Changer-Singleton-t")


if __name__ == "__main__":
    unittest.main()
