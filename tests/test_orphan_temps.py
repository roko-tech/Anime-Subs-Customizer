"""Temp files a crash leaves behind (Phase 4 row 10: 25 forced kills of a
watching instance left 9 ".tmp_*.ass" files in show folders, 8 of them full
styled copies a media server could pick up, and 4 ".bak_*.tmp" files in
Original Subs; the indexer also listed the .tmp_ files as library files)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import db  # noqa: E402
from anime_subs.ass_editor import FileProcessor  # noqa: E402

ORPHANS = ["Show/.tmp_ab12cd_3.ass", "Show/.tmp_x9y8z7w6.srt", "Show/.tmp_q1w2e3r4.tmp",
           "Show/Original Subs/.bak_k5l6m7n8.tmp", "Show/Original Subs/.bak_4242_1234567890.tmp"]
KEEP = ["Show/Show - 01.ass", "Show/Original Subs/Show - 01.ass", "Show/.tmp_notes.txt",
        "Show/.tmp-ab12cd34.ass", "Show/tmp_ab12cd34.ass"]


class OrphanTempTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        old = time.time() - 3600
        for rel in ORPHANS + KEEP:
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x")
            os.utime(p, (old, old))

    def test_old_orphans_are_removed_and_nothing_else(self):
        os.chmod(self.root / ORPHANS[3], 0o444)  # read-only leftovers too
        self.assertEqual(FileProcessor.cleanup_orphan_temps(str(self.root)), len(ORPHANS))
        for rel in ORPHANS:
            self.assertFalse((self.root / rel).exists(), rel)
        for rel in KEEP:
            self.assertTrue((self.root / rel).exists(), rel)

    def test_a_write_in_progress_is_left_alone(self):
        fresh = self.root / "Show/.tmp_inflight.ass"
        fresh.write_bytes(b"x")
        FileProcessor.cleanup_orphan_temps(str(self.root))
        self.assertTrue(fresh.exists())

    def test_indexer_skips_temp_names(self):
        rows = []

        class Cursor:
            def executemany(self, sql, batch):
                rows.extend(batch)

            def execute(self, *a, **k):
                return self

        class Conn:
            def cursor(self):
                return Cursor()

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch.object(db, "get_db_connection", lambda: Conn()):
            db.DatabaseManager.add_existing_files(str(self.root))
        names = [Path(r[0]).name for r in rows]
        self.assertIn("Show - 01.ass", names)
        self.assertFalse([n for n in names if n.startswith((".tmp_", ".bak_"))], names)


if __name__ == "__main__":
    unittest.main()
