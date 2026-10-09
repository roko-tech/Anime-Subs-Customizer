"""--import-from (ADR-003): bring settings and the file index from an older
layout (a source checkout, a portable copy) into the current data folder,
without touching the source and without losing what's already there."""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import migrate  # noqa: E402


def digest(folder: Path) -> dict:
    return {p.name: hashlib.sha1(p.read_bytes()).hexdigest()
            for p in sorted(folder.iterdir()) if p.is_file()}


class ImportSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.src = base / "old layout"
        self.dst = base / "data"
        self.src.mkdir()
        self.dst.mkdir()
        cfg = {"font": "Example Naskh", "color": "00FFFF", "folder": "D:/Library",
               "favorite_fonts": ["A", "B"], "font_size_nudges": {"Example Naskh": 1.1}}
        (self.src / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        (self.src / "font_path_cache.json").write_text('{"v": 1}', encoding="utf-8")
        (self.src / "font_support_cache.json").write_text('{"s": 1}', encoding="utf-8")
        (self.src / "ffmpeg_cache.json").write_text('{"ffmpeg": "D:/old/bin"}', encoding="utf-8")
        # A WAL-mode index with rows still in the -wal file (not checkpointed).
        conn = sqlite3.connect(self.src / "files_monitor.db")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE monitored_files (id INTEGER PRIMARY KEY, file_path TEXT UNIQUE,"
                     " last_modified REAL, status TEXT, content_hash TEXT)")
        conn.executemany("INSERT INTO monitored_files (file_path, last_modified, status, content_hash)"
                         " VALUES (?, ?, 'processed', 'h')", [(f"D:/Library/{i}.ass", i) for i in range(50)])
        conn.commit()
        self._hold = conn  # keep the WAL un-checkpointed, like a stopped app's leftovers
        (self.dst / "config.json").write_text('{"font": "Arial"}', encoding="utf-8")

    def tearDown(self):
        self._hold.close()
        self.tmp.cleanup()

    def test_import_brings_settings_and_index_and_keeps_the_source_untouched(self):
        before = digest(self.src)
        report = migrate.import_settings(self.src, self.dst, app_running=False)
        self.assertTrue(report["ok"], report)
        self.assertEqual(digest(self.src), before, "the source must not change")
        cfg = json.loads((self.dst / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["favorite_fonts"], ["A", "B"])
        self.assertEqual(cfg["font_size_nudges"], {"Example Naskh": 1.1})
        conn = sqlite3.connect(self.dst / "files_monitor.db")
        try:
            rows = conn.execute("SELECT COUNT(*) FROM monitored_files").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(rows, 50)
        self.assertTrue((self.dst / "font_path_cache.json").exists())
        # The old layout's FFmpeg location is not carried over.
        self.assertFalse((self.dst / "ffmpeg_cache.json").exists())

    def test_existing_data_is_set_aside_not_overwritten(self):
        report = migrate.import_settings(self.src, self.dst, app_running=False)
        backups = list(self.dst.glob("import-backup-*/config.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), '{"font": "Arial"}')
        self.assertEqual(Path(report["previous_data_kept_in"]), backups[0].parent.resolve())

    def test_refuses_while_the_app_is_running(self):
        report = migrate.import_settings(self.src, self.dst, app_running=True)
        self.assertFalse(report["ok"])
        self.assertEqual((self.dst / "config.json").read_text(encoding="utf-8"), '{"font": "Arial"}')

    def test_refuses_a_folder_without_settings(self):
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertFalse(migrate.import_settings(empty, self.dst, app_running=False)["ok"])

    def test_importing_into_the_same_folder_is_refused(self):
        self.assertFalse(migrate.import_settings(self.src, self.src, app_running=False)["ok"])


if __name__ == "__main__":
    unittest.main()
