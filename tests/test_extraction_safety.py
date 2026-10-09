"""Extraction writes to a temp file and only replaces the sidecar after ffmpeg
succeeded with non-empty output (data-safety-3, concurrency-3): a killed or
failed extraction never leaves a 0-byte or half-written subtitle, and never
destroys an existing one. ffmpeg/ffprobe run as tracked child processes so a
quit (or an installer's --quit) can stop them (checkpoint-2 #12)."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import ass_editor, runtime  # noqa: E402
from anime_subs.ass_editor import FileProcessor  # noqa: E402

OUTPUT = b"[Script Info]\r\nScriptType: v4.00+\r\n\r\n[Events]\r\n"


def fake_ffmpeg(write: bytes = OUTPUT, fail: bool = False):
    def run(cmd, **kwargs):
        out = Path(cmd[-1])
        if write is not None:
            out.write_bytes(write)
        if fail:
            raise ass_editor.subprocess.CalledProcessError(1, cmd, b"", b"boom")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    return run


class ExtractionTempFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.video = self.dir / "Show - 01.mkv"
        self.video.write_bytes(b"\x1aE\xdf\xa3 fake mkv")
        self.target = self.dir / "Show - 01.ara.ass"
        self._loc = patch.object(ass_editor.FFmpegLocator, "get_binary", return_value="ffmpeg")
        self._loc.start()

    def tearDown(self):
        self._loc.stop()
        self.tmp.cleanup()

    def _temps(self):
        return [p.name for p in self.dir.iterdir() if p.name.startswith(".tmp_")]

    def _extract(self):
        return FileProcessor.extract_subtitle(str(self.video), 2, "ara", "ass", str(self.dir))

    def test_success_lands_atomically_with_no_temp_left(self):
        with patch.object(ass_editor, "run_tracked", side_effect=fake_ffmpeg()):
            self.assertEqual(Path(self._extract()), self.target.resolve())
        self.assertEqual(self.target.read_bytes(), OUTPUT)
        self.assertEqual(self._temps(), [])

    def test_ffmpeg_writes_to_a_temp_name_not_the_sidecar(self):
        seen = []

        def run(cmd, **kwargs):
            seen.append(Path(cmd[-1]).name)
            return fake_ffmpeg()(cmd, **kwargs)

        with patch.object(ass_editor, "run_tracked", side_effect=run):
            self._extract()
        self.assertTrue(seen[0].startswith(".tmp_") and seen[0].endswith(".ass"), seen)

    def test_failure_leaves_an_existing_sidecar_untouched(self):
        self.target.write_bytes(b"hand-made subtitle")
        with patch.object(ass_editor, "run_tracked", side_effect=fake_ffmpeg(write=b"partial", fail=True)):
            with self.assertRaises(RuntimeError):
                self._extract()
        self.assertEqual(self.target.read_bytes(), b"hand-made subtitle")
        self.assertEqual(self._temps(), [])

    def test_empty_output_never_replaces_anything(self):
        with patch.object(ass_editor, "run_tracked", side_effect=fake_ffmpeg(write=b"")):
            with self.assertRaises(RuntimeError):
                self._extract()
        self.assertFalse(self.target.exists())
        self.assertEqual(self._temps(), [])

    def test_timeout_removes_only_the_temp(self):
        def slow(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"half")
            raise ass_editor.subprocess.TimeoutExpired(cmd, 1)

        self.target.write_bytes(b"older sidecar")
        with patch.object(ass_editor, "run_tracked", side_effect=slow):
            with self.assertRaises(ass_editor.subprocess.TimeoutExpired):
                self._extract()
        self.assertEqual(self.target.read_bytes(), b"older sidecar")
        self.assertEqual(self._temps(), [])


class TrackedProcessTests(unittest.TestCase):
    def test_terminate_stops_a_running_child(self):
        result = {}

        def worker():
            try:
                runtime.run_tracked([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
            except Exception as e:  # noqa: BLE001
                result["error"] = e
            result["done"] = time.monotonic()

        t = threading.Thread(target=worker)
        start = time.monotonic()
        t.start()
        for _ in range(100):
            if runtime._tracked_processes:
                break
            time.sleep(0.05)
        runtime.terminate_tracked_processes()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())
        self.assertLess(result["done"] - start, 10)
        self.assertEqual(runtime._tracked_processes, set())

    def test_output_and_check_behave_like_subprocess_run(self):
        ok = runtime.run_tracked([sys.executable, "-c", "print('héllo')"], timeout=30,
                                 encoding="utf-8", errors="replace")
        self.assertEqual(ok.returncode, 0)
        self.assertIn("llo", ok.stdout)
        with self.assertRaises(runtime.subprocess.CalledProcessError):
            runtime.run_tracked([sys.executable, "-c", "raise SystemExit(3)"], timeout=30, check=True)


if __name__ == "__main__":
    unittest.main()
