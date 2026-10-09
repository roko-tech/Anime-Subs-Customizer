"""A damaged file index must not stop the app from starting (architecture-6).
The index is bookkeeping: the originals live in Original Subs/ and the
"styled by us" rules don't depend on it, so it is moved aside and rebuilt."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import db as db_mod  # noqa: E402
from anime_subs.runtime import IssueTracker  # noqa: E402


class CorruptIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._orig = (db_mod.DB_PATH, db_mod._db_pool, db_mod._db_pool_shutdown)
        db_mod.DB_PATH = self.dir / "files_monitor.db"
        db_mod._db_pool = None
        db_mod._db_pool_shutdown = False
        IssueTracker.clear()

    def tearDown(self):
        try:
            if db_mod._db_pool is not None:
                db_mod._db_pool.close_all()
        except Exception:
            pass
        db_mod.DB_PATH, db_mod._db_pool, db_mod._db_pool_shutdown = self._orig
        self.tmp.cleanup()

    def test_garbage_database_is_moved_aside_and_rebuilt(self):
        db_mod.DB_PATH.write_bytes(b"this is not a sqlite database" * 100)
        db_mod.DatabaseManager.init_db()  # must not raise
        aside = list(self.dir.glob("files_monitor.db.corrupt-*"))
        self.assertEqual(len(aside), 1)
        self.assertTrue(aside[0].read_bytes().startswith(b"this is not"))
        # The rebuilt index works.
        self.assertEqual(db_mod.DatabaseManager.get_monitored_files(), {})
        self.assertEqual([i.operation for i in IssueTracker.get_issues()], ["database"])

    def test_healthy_database_is_left_alone(self):
        db_mod.DatabaseManager.init_db()
        db_mod.DatabaseManager.init_db()
        self.assertEqual(list(self.dir.glob("files_monitor.db.corrupt-*")), [])
        self.assertEqual(IssueTracker.get_issues(), [])


if __name__ == "__main__":
    unittest.main()
