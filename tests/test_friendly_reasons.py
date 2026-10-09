"""Friendly, translated Problems-panel reasons (rtl-4, ux-9, ADR-006 §6)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import i18n  # noqa: E402

CASES = [
    ("could not back up the original: [WinError 5] Access is denied: 'x'", "reason_access_denied"),
    ("IO error: [WinError 32] The process cannot access the file because it is being used by another process", "reason_locked"),
    ("[WinError 53] The network path was not found", "reason_unreachable"),
    ("[WinError 112] There is not enough space on the disk", "reason_disk_full"),
    ("File too large to be a subtitle (99999999 bytes); skipped", "reason_too_large"),
    ("No supported encoding matched this file (tried utf-8, cp1256, ...)", "reason_undecodable"),
    ("SubStation Alpha v4 ([V4 Styles]) format is not supported for restyling", "reason_ssa_v4"),
    ("the file is empty; left as it is", "reason_empty"),
    ("the originals for this folder were deleted, so Restore can't bring them back", "reason_originals_deleted"),
    ("changed by another program after it was styled, so it was left as it is", "reason_foreign"),
    ("backup is empty (0 bytes); not restored", "reason_bad_backup"),
    ("stream #2: ffmpeg timed out after 300s", "reason_timeout"),
    ("[WinError 1155] No application is associated with the specified file for "
     "this operation: 'C:\\\\Users\\\\x\\\\monitor.log'", "reason_no_app"),
]


class FriendlyReasonTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("en")

    def test_known_causes_map_to_plain_words_in_both_languages(self):
        for lang in ("en", "ar"):
            i18n.set_language(lang)
            for reason, key in CASES:
                with self.subTest(lang=lang, key=key):
                    self.assertEqual(i18n.friendly_reason(reason), i18n.t_raw(key))

    def test_unknown_reason_has_no_rewording(self):
        self.assertIsNone(i18n.friendly_reason("something odd happened"))

    def test_operation_labels_are_translated(self):
        i18n.set_language("ar")
        self.assertEqual(i18n.operation_label("apply"), i18n.t_raw("op_apply"))
        self.assertEqual(i18n.operation_label("mystery"), "MYSTERY")


if __name__ == "__main__":
    unittest.main()
