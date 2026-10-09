"""Backup and scan edge cases from the pre-launch review (data-safety-3/4,
concurrency-3/5, architecture-9): orphaned temp files, empty files, read-only
files, and failures that used to be logged but never shown to the user."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import db as db_mod  # noqa: E402
from anime_subs.ass_editor import FileProcessor  # noqa: E402
from anime_subs.runtime import IssueTracker  # noqa: E402

SAMPLE = (
    "[Script Info]\r\nScriptType: v4.00+\r\nPlayResY: 720\r\n\r\n[V4+ Styles]\r\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
    "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
    "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\r\n"
    "Style: Default,Arial,40,&H00FFFFFF,&H000000FF,&H00303030,&H80000000,0,0,0,0,"
    "100,100,0,0,1,2,1,2,20,20,20,1\r\n\r\n[Events]\r\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
    "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,Hello there\r\n"
)


class _TempLibrary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig_db = (db_mod.DB_PATH, db_mod._db_pool, db_mod._db_pool_shutdown)
        db_mod.DB_PATH = self.root / "index.db"
        db_mod._db_pool = None
        db_mod._db_pool_shutdown = False
        db_mod.DatabaseManager.init_db()
        IssueTracker.clear()
        self.lib = self.root / "Library" / "Show"
        self.lib.mkdir(parents=True)

    def tearDown(self):
        try:
            if db_mod._db_pool is not None:
                db_mod._db_pool.close_all()
        except Exception:
            pass
        db_mod.DB_PATH, db_mod._db_pool, db_mod._db_pool_shutdown = self._orig_db
        for p in self.root.rglob("*"):
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
        self.tmp.cleanup()


class ScanTests(_TempLibrary):
    def test_orphaned_atomic_write_temps_are_not_subtitles(self):
        (self.lib / "Show - 01.ass").write_bytes(SAMPLE.encode("utf-8"))
        (self.lib / ".tmp_k2j3.ass").write_bytes(SAMPLE.encode("utf-8"))
        (self.lib / ".bak_9xq1.ass").write_bytes(SAMPLE.encode("utf-8"))
        names = [p.name for p in FileProcessor.list_ass_files(str(self.root / "Library"))]
        self.assertEqual(names, ["Show - 01.ass"])


class EmptyFileTests(_TempLibrary):
    def test_an_empty_file_is_never_backed_up_or_styled(self):
        f = self.lib / "Show - 03.ass"
        f.write_bytes(b"")
        self.assertFalse(FileProcessor.apply_changes(f, "Arial", "00FFFF"))
        self.assertFalse((self.lib / "Original Subs" / f.name).exists())
        self.assertEqual(f.read_bytes(), b"")
        self.assertTrue(IssueTracker.get_issues(), "the skip should be visible in Problems")

    def test_copy_original_refuses_an_empty_source(self):
        f = self.lib / "Show - 04.ass"
        f.write_bytes(b"")
        self.assertFalse(FileProcessor.copy_original_file(f))
        self.assertFalse((self.lib / "Original Subs" / f.name).exists())


class ReadOnlyTests(_TempLibrary):
    def test_read_only_file_is_reported_and_leaves_no_temp_behind(self):
        f = self.lib / "Show - 05.ass"
        f.write_bytes(SAMPLE.encode("utf-8"))
        os.chmod(f, stat.S_IREAD)
        ok = FileProcessor.apply_changes(f, "Arial", "00FFFF")
        self.assertFalse(ok)
        backups = self.lib / "Original Subs"
        leftovers = [p.name for p in backups.glob("*.tmp")] if backups.exists() else []
        self.assertEqual(leftovers, [])
        self.assertTrue(IssueTracker.get_issues(), "a read-only failure must reach Problems")
        self.assertEqual(f.read_bytes(), SAMPLE.encode("utf-8"))

    def test_backup_of_a_read_only_original_is_a_normal_writable_copy(self):
        f = self.lib / "Show - 06.ass"
        f.write_bytes(SAMPLE.encode("utf-8"))
        os.chmod(f, stat.S_IREAD)
        self.assertTrue(FileProcessor.copy_original_file(f))
        backup = self.lib / "Original Subs" / f.name
        self.assertEqual(backup.read_bytes(), SAMPLE.encode("utf-8"))
        self.assertEqual([p.name for p in backup.parent.glob("*.tmp")], [])


class BackupFailureTests(_TempLibrary):
    def test_a_failed_backup_is_reported_not_just_logged(self):
        f = self.lib / "Show - 07.ass"
        f.write_bytes(SAMPLE.encode("utf-8"))
        # A FILE where the backup folder should be makes the backup impossible.
        (self.lib / "Original Subs").write_text("not a folder", encoding="utf-8")
        self.assertFalse(FileProcessor.apply_changes(f, "Arial", "00FFFF"))
        self.assertEqual(f.read_bytes(), SAMPLE.encode("utf-8"))
        ops = [i.operation for i in IssueTracker.get_issues()]
        self.assertIn("backup", ops)


if __name__ == "__main__":
    unittest.main()
