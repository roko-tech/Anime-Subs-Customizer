"""Monitor decisions with the real engine (ADR-006 §3, ADR-007).

- Two copies of the app with DIFFERENT settings on one library used to restyle
  each other's output forever (reproduced in the review: every file rewritten
  every ~2.3 s). Now the second writer's output is "foreign" to the first, and
  each file settles after at most one write per copy, in either start order.
- A restored file stays original across Watch passes until its content changes.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import ass_editor, monitor  # noqa: E402
from anime_subs.ass_editor import FileProcessor  # noqa: E402
from anime_subs.runtime import AppConfig, FileStatus, IssueTracker, file_hash  # noqa: E402

SAMPLE = (
    "[Script Info]\r\nScriptType: v4.00+\r\nPlayResY: 720\r\nScaledBorderAndShadow: yes\r\n\r\n"
    "[V4+ Styles]\r\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
    "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\r\n"
    "Style: Default,Times New Roman,40,&H00FFFFFF,&H000000FF,&H00303030,&H80000000,"
    "0,0,0,0,100,100,0,0,1,2,1,2,20,20,20,1\r\n\r\n[Events]\r\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
    "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Hello there\r\n"
    "Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,General Kenobi\r\n"
).encode("utf-8")


class MemoryDB:
    """Per-copy file index (each copy of the app has its own)."""

    def __init__(self):
        self.rows = {}

    def _row(self, path, status):
        p = Path(path)
        self.rows[str(p)] = {"last_modified": p.stat().st_mtime, "status": status,
                             "content_hash": file_hash(p)}

    def get_monitored_files(self):
        return {k: dict(v) for k, v in self.rows.items()}

    def update_file_timestamp(self, path, last_modified, content_hash=None, status=None):
        self.rows[str(path)] = {"last_modified": last_modified,
                                "status": status or FileStatus.PROCESSED.value,
                                "content_hash": content_hash}
        return True

    def set_status(self, path, status):
        self._row(path, status.value)
        return True

    def add_file(self, path):
        self._row(path, FileStatus.PROCESSED.value)
        return True

    def was_processed(self, path):
        return path in self.rows and self.rows[path]["status"] != FileStatus.INDEXED.value

    def add_processed_videos_batch(self, videos):
        pass

    def get_processed_videos(self):
        return []


class State:
    def __init__(self, folder):
        self.monitored_folder = folder
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self._shutting_down = False
        self.writes = 0

    def note_event(self, verb, name, styled=False):
        if styled:
            self.writes += 1


class Copy:
    """One copy of the app: its own settings, index and monitor."""

    def __init__(self, folder, font, color):
        self.db = MemoryDB()
        self.state = State(folder)
        self.cfg = AppConfig(font=font, color=color)
        self.mon = monitor.FolderMonitor(self.state, self.db, FileProcessor)

    def tick(self):
        with patch.object(ass_editor, "DatabaseManager", self.db):
            self.mon.process_ass_files(self.db.get_monitored_files(), {}, self.cfg)
        time.sleep(0.02)  # distinct mtimes between writers


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.lib = Path(self.tmp.name)
        self.files = []
        for n in (1, 2, 3):
            p = self.lib / f"Show - 0{n}.ass"
            p.write_bytes(SAMPLE)
            old = time.time() - 3600
            os.utime(p, (old, old))
            self.files.append(p)
        IssueTracker.clear()
        self._settle = monitor._ASS_SETTLE_INTERVAL
        monitor._ASS_SETTLE_INTERVAL = 0.01  # the real 1 s size check, sped up

    def tearDown(self):
        monitor._ASS_SETTLE_INTERVAL = self._settle
        self.tmp.cleanup()


class NoPingPongTests(_Base):
    def _run(self, first, second):
        for _ in range(6):
            first.tick()
            second.tick()
        return first.state.writes, second.state.writes

    def test_two_copies_with_different_settings_settle(self):
        a = Copy(str(self.lib), "Arial", "80FFFF")
        b = Copy(str(self.lib), "Segoe UI", "00FFFF")
        wa, wb = self._run(a, b)
        self.assertLessEqual(wa, 3)   # at most one write per file per copy
        self.assertLessEqual(wb, 3)
        statuses = [r["status"] for r in a.db.rows.values()]
        self.assertEqual(statuses.count(FileStatus.FOREIGN.value), 3)
        foreign_notes = [i for i in IssueTracker.get_issues() if i.operation == "foreign"]
        self.assertEqual(len(foreign_notes), 1, "one note per folder, not per file")
        for p in self.files:  # originals untouched throughout
            self.assertEqual((p.parent / "Original Subs" / p.name).read_bytes(), SAMPLE)

    def test_other_start_order_also_settles(self):
        a = Copy(str(self.lib), "Arial", "80FFFF")
        b = Copy(str(self.lib), "Segoe UI", "00FFFF")
        wb, wa = self._run(b, a)
        self.assertLessEqual(wa + wb, 6)

    def test_same_settings_never_become_foreign(self):
        a = Copy(str(self.lib), "Arial", "80FFFF")
        b = Copy(str(self.lib), "Arial", "80FFFF")
        self._run(a, b)
        self.assertEqual(b.state.writes, 0)
        for db in (a.db, b.db):
            self.assertNotIn(FileStatus.FOREIGN.value, [r["status"] for r in db.rows.values()])


class RestoredStaysOriginalTests(_Base):
    def test_restored_file_is_left_alone_until_its_content_changes(self):
        a = Copy(str(self.lib), "Arial", "80FFFF")
        a.tick()
        target = self.files[0]
        backup = target.parent / "Original Subs" / target.name
        FileProcessor.restore_backups([(backup, target)])
        a.db.set_status(target, FileStatus.RESTORED)
        writes = a.state.writes
        for _ in range(3):
            a.tick()
        self.assertEqual(target.read_bytes(), SAMPLE, "Watch must not restyle a restored file")
        self.assertEqual(a.state.writes, writes)
        # A new release replaces it: now it is styled again.
        target.write_bytes(SAMPLE.replace(b"Hello there", b"Hello there (v2)"))
        a.tick()
        self.assertIn(b"Arial", target.read_bytes())


class TransientFailureRetryTests(_Base):
    """A failed file used to be skipped until its mtime changed. A player
    holding a subtitle open during the atomic replace doesn't change the
    mtime when it lets go, so that episode stayed unstyled until the app was
    restarted (found while planning the Sandbox robustness row)."""

    def _tick(self, copy, failed):
        with patch.object(ass_editor, "DatabaseManager", copy.db):
            copy.mon.process_ass_files(copy.db.get_monitored_files(), failed, copy.cfg)

    def test_file_held_open_is_styled_after_it_is_released(self):
        a = Copy(str(self.lib), "Arial", "80FFFF")
        failed = {}
        target = self.files[0]
        with open(target, "rb"):  # like a player: no delete sharing, so the replace fails
            self._tick(a, failed)
        self.assertEqual(target.read_bytes(), SAMPLE)
        self.assertIn(str(target), failed)
        issues = IssueTracker.count()
        self._tick(a, failed)  # right away: still waiting, no new issue
        self.assertEqual(target.read_bytes(), SAMPLE)
        self.assertEqual(IssueTracker.count(), issues)
        later = time.monotonic() + 3600
        with patch.object(monitor.time, "monotonic", return_value=later):
            self._tick(a, failed)
        self.assertIn(b"Arial", target.read_bytes())
        self.assertNotIn(str(target), failed)
        self.assertEqual((target.parent / "Original Subs" / target.name).read_bytes(), SAMPLE)

    def test_broken_file_is_not_reparsed_until_it_changes(self):
        a = Copy(str(self.lib), "Arial", "80FFFF")
        failed = {}
        broken = self.lib / "Broken.ass"
        broken.write_bytes(b"[Script Info]\r\n\r\n[V4+ Styles]\r\nStyle: x\r\n")
        old = time.time() - 3600
        os.utime(broken, (old, old))
        calls = []
        real = FileProcessor.apply_changes

        def spy(path, *args, **kwargs):
            calls.append(Path(path).name)
            return real(path, *args, **kwargs)

        with patch.object(FileProcessor, "apply_changes", side_effect=spy):
            self._tick(a, failed)
            with patch.object(monitor.time, "monotonic", return_value=time.monotonic() + 3600):
                self._tick(a, failed)
        self.assertEqual(calls.count("Broken.ass"), 1)


class RepeatedIssueTests(unittest.TestCase):
    def setUp(self):
        IssueTracker.clear()
        self.addCleanup(IssueTracker.clear)

    def test_identical_repeat_is_shown_once(self):
        for _ in range(3):
            IssueTracker.record("apply", "Show - 01.ass", "IO error: [WinError 32] in use")
        self.assertEqual(IssueTracker.count(), 1)

    def test_different_file_or_reason_is_kept(self):
        IssueTracker.record("apply", "Show - 01.ass", "IO error: [WinError 32] in use")
        IssueTracker.record("apply", "Show - 02.ass", "IO error: [WinError 32] in use")
        IssueTracker.record("apply", "Show - 01.ass", "IO error: [WinError 112] disk full")
        self.assertEqual(IssueTracker.count(), 3)

    def test_same_issue_much_later_is_shown_again(self):
        IssueTracker.record("apply", "Show - 01.ass", "locked")
        with patch("anime_subs.runtime.time.time", return_value=time.time() + 3 * 3600):
            IssueTracker.record("apply", "Show - 01.ass", "locked")
        self.assertEqual(IssueTracker.count(), 2)


if __name__ == "__main__":
    unittest.main()
