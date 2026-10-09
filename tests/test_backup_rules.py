"""ADR-006 backup rules: whose file is it, and what may be saved as "the
original". Covers the P0 Restore data loss (ux-1), the Bazarr replacement
(data-safety-1), deleted backups (data-safety-2) and the checkpoint-2 cases
where a styled file carries no colour tags at all."""
import os
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

FORMAT = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
          "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
          "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
          "MarginR, MarginV, Encoding\r\n")
EV_FORMAT = "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"


def ass(dialogue, styles=("Default",), events_format=EV_FORMAT, font="Arial"):
    head = ("[Script Info]\r\nScriptType: v4.00+\r\nPlayResY: 720\r\n"
            "ScaledBorderAndShadow: yes\r\n\r\n[V4+ Styles]\r\n" + FORMAT)
    rows = "".join(f"Style: {s},{font},40,&H00FFFFFF,&H000000FF,&H00303030,&H80000000,"
                   f"0,0,0,0,100,100,0,0,1,2,1,2,20,20,20,1\r\n" for s in styles)
    events = "\r\n[Events]\r\n" + events_format + "".join(
        f"Dialogue: 0,0:00:{i:02d}.00,0:00:{i + 1:02d}.00,{style},,0,0,0,,{text}\r\n"
        for i, (style, text) in enumerate(dialogue))
    return (head + rows + events).encode("utf-8")


V1 = ass([("Default", "First release line one"), ("Default", "First release line two")])
V2 = ass([("Default", "Fixed release line one"), ("Default", "Fixed release line two")])


class _Lib(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._orig = (db_mod.DB_PATH, db_mod._db_pool, db_mod._db_pool_shutdown)
        db_mod.DB_PATH = self.root / "index.db"
        db_mod._db_pool = None
        db_mod._db_pool_shutdown = False
        db_mod.DatabaseManager.init_db()
        IssueTracker.clear()
        self.show = self.root / "Show"
        self.show.mkdir()
        self.f = self.show / "Show - 01.ass"
        self.backup = self.show / "Original Subs" / self.f.name

    def tearDown(self):
        try:
            if db_mod._db_pool is not None:
                db_mod._db_pool.close_all()
        except Exception:
            pass
        db_mod.DB_PATH, db_mod._db_pool, db_mod._db_pool_shutdown = self._orig
        self.tmp.cleanup()

    def style(self, font="Segoe UI", color="00FFFF"):
        return FileProcessor.apply_changes(self.f, font, color)

    def older(self):
        d = self.show / "Original Subs" / ".older"
        return sorted(p.read_bytes() for p in d.iterdir()) if d.exists() else []


class SignatureTests(_Lib):
    def sig(self, data):
        lines = data.decode("utf-8").splitlines(keepends=True)
        return FileProcessor.dialogue_signature(lines, FileProcessor._typesetting_styles(lines))

    def test_our_output_is_recognised(self):
        self.f.write_bytes(V1)
        self.assertTrue(self.style())
        self.assertTrue(self.sig(self.f.read_bytes())["ours"])

    def test_an_original_is_not_ours(self):
        s = self.sig(V1)
        self.assertFalse(s["ours"])
        self.assertEqual(s["taggable"], 2)
        self.assertEqual(s["tagged"], 0)

    def test_mixed_colours_are_not_ours(self):
        data = ass([("Default", "{\\c&H00FFFF&}one"), ("Default", "{\\c&H0000FF&}two")])
        self.assertFalse(self.sig(data)["ours"])

    def test_lowercase_tags_are_not_written_by_us(self):
        data = ass([("Default", "{\\c&h00ffff&}one"), ("Default", "{\\c&h00ffff&}two")])
        self.assertFalse(self.sig(data)["ours"])


class NoTaggableDialogueTests(_Lib):
    """Checkpoint-2 critical case: files the writer can't tag must not be
    'styled' (and must never get a backup decision based on a missing tag)."""

    def assert_nothing_to_do(self, data):
        self.f.write_bytes(data)
        self.assertFalse(self.style())
        self.assertEqual(self.f.read_bytes(), data)
        self.assertFalse(self.backup.exists())
        self.assertEqual(IssueTracker.get_issues(), [])
        self.assertTrue(db_mod.DatabaseManager.was_processed(str(self.f)))

    def test_signs_only_track(self):
        self.assert_nothing_to_do(ass(
            [("Sign", "{\\pos(100,100)}SHOP"), ("Sign", "{\\pos(200,100)}CAFE")],
            styles=("Default", "Sign")))

    def test_op_ed_only_track(self):
        self.assert_nothing_to_do(ass(
            [("OP", "{\\k20}la {\\k20}la"), ("OP", "{\\k30}lu")], styles=("Default", "OP")))

    def test_text_first_events_format(self):
        bad_format = "Format: Text, Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect\r\n"
        self.assert_nothing_to_do(ass([("Default", "line")], events_format=bad_format))

    def test_only_short_malformed_rows(self):
        data = V1.replace(b"Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,First release line one",
                          b"Dialogue: 0,0:00:00.00").replace(
            b"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,First release line two",
            b"Dialogue: 0,0:00:01.00")
        self.assert_nothing_to_do(data)


class BackupTableTests(_Lib):
    def test_first_style_backs_up_the_exact_original_bytes(self):
        self.f.write_bytes(V1)
        self.assertTrue(self.style())
        self.assertEqual(self.backup.read_bytes(), V1)

    def test_restyling_our_own_output_keeps_the_original_backup(self):
        self.f.write_bytes(V1)
        self.assertTrue(self.style("Segoe UI", "00FFFF"))
        self.assertTrue(self.style("Tahoma", "FF00FF"))
        self.assertEqual(self.backup.read_bytes(), V1)
        self.assertEqual(self.older(), [])

    def test_downloader_replacement_becomes_the_new_original(self):
        # P0 ux-1 / data-safety-1: style v1, a downloader drops v2 in place.
        self.f.write_bytes(V1)
        self.assertTrue(self.style())
        self.f.write_bytes(V2)
        self.assertTrue(self.style())
        self.assertEqual(self.backup.read_bytes(), V2, "Restore must bring back v2")
        self.assertEqual(self.older(), [V1], "the older original is kept, not lost")

    def test_restore_after_replacement_returns_the_newer_release(self):
        self.f.write_bytes(V1)
        self.style()
        self.f.write_bytes(V2)
        self.style()
        FileProcessor.restore_backups([(self.backup, self.f)])
        self.assertEqual(self.f.read_bytes(), V2)

    def test_partially_tagged_file_keeps_the_backup_and_saves_the_incoming_copy(self):
        self.f.write_bytes(V1)
        self.style()
        mixed = ass([("Default", "{\\c&H00FFFF&}tagged"), ("Default", "added by hand")])
        self.f.write_bytes(mixed)
        self.style()
        self.assertEqual(self.backup.read_bytes(), V1)
        self.assertIn(mixed, self.older(), "ambiguous incoming bytes are never lost")

    def test_deleted_backup_with_history_restyles_without_saving_styled_content(self):
        self.f.write_bytes(V1)
        self.style()
        self.backup.unlink()
        self.assertTrue(self.style("Tahoma", "FF00FF"))
        self.assertFalse(self.backup.exists(), "styled content must not become the 'original'")
        self.assertIn("Tahoma", self.f.read_text(encoding="utf-8"))
        self.assertEqual([i.operation for i in IssueTracker.get_issues()], ["backup"])

    def test_look_alike_without_history_is_still_backed_up(self):
        data = ass([("Default", "{\\c&H00FFFF&}one"), ("Default", "{\\c&H00FFFF&}two")])
        self.f.write_bytes(data)
        self.assertTrue(self.style())
        self.assertEqual(self.backup.read_bytes(), data)

    def test_junctioned_backup_folder_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlink support")
        outside = self.root / "Elsewhere"
        outside.mkdir()
        try:
            os.symlink(outside, self.show / "Original Subs", target_is_directory=True)
        except OSError:
            self.skipTest("creating a directory symlink needs Developer Mode/admin")
        self.f.write_bytes(V1)
        self.assertFalse(self.style())
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.f.read_bytes(), V1)


if __name__ == "__main__":
    unittest.main()
