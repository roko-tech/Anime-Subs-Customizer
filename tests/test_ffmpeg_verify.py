"""FFmpegLocator._verify_binary: the first start of a freshly installed ffmpeg
can take several seconds (antivirus scan of new files). Found in a clean
Windows Sandbox: a 5 s limit reported the bundled FFmpeg as missing."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import media  # noqa: E402


class VerifyTimeoutTests(unittest.TestCase):
    def test_slow_first_start_is_waited_for(self):
        with patch.object(media.subprocess, "run") as run:
            self.assertTrue(media.FFmpegLocator._verify_binary("ffmpeg.exe"))
        self.assertGreaterEqual(run.call_args.kwargs["timeout"], 30)

    def test_timeout_is_logged_and_reported_missing(self):
        boom = subprocess.TimeoutExpired(["ffmpeg.exe", "-version"], 30)
        with patch.object(media.subprocess, "run", side_effect=boom), \
                self.assertLogs(media.logger, level="WARNING") as logs:
            self.assertFalse(media.FFmpegLocator._verify_binary("ffmpeg.exe"))
        self.assertIn("did not answer", "".join(logs.output))


class EarlyBootLookupTests(unittest.TestCase):
    """time.monotonic() counts from boot on Windows. A "never missed" default
    of 0.0 made every lookup in the first 60 s after boot return None without
    searching (found in a freshly started Windows Sandbox; an app autostarted
    at sign-in hits it too)."""

    def setUp(self):
        L = media.FFmpegLocator
        saved = (L._cached, L._not_found_ts, L._verified, L._cache_loaded)
        self.addCleanup(lambda: setattr_all(L, saved))
        L._cached, L._not_found_ts, L._verified, L._cache_loaded = {}, {}, set(), True
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "bin").mkdir()
        self.exe = self.tmp / "bin" / ("ffmpeg.exe" if media.IS_WINDOWS else "ffmpeg")
        self.exe.write_bytes(b"")
        patches = [patch.object(media, "app_root", lambda: self.tmp),
                   patch.object(media.FFmpegLocator, "_verify_binary", classmethod(lambda cls, p: True)),
                   patch.object(media.FFmpegLocator, "_save_cache", classmethod(lambda cls: None)),
                   patch.object(media.shutil, "which", lambda name: None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_found_within_a_minute_of_boot(self):
        with patch.object(media.time, "monotonic", return_value=12.0):
            self.assertEqual(media.FFmpegLocator.get_binary("ffmpeg"), str(self.exe))

    def test_recent_miss_still_skips_the_search(self):
        media.FFmpegLocator._not_found_ts["ffmpeg"] = 500.0
        with patch.object(media.time, "monotonic", return_value=510.0):
            self.assertIsNone(media.FFmpegLocator.get_binary("ffmpeg"))


def setattr_all(L, saved):
    L._cached, L._not_found_ts, L._verified, L._cache_loaded = saved


if __name__ == "__main__":
    unittest.main()
