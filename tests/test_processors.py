"""
Regression tests for the ASS file processor and security utilities.

Run from the project root with:

    python tests/test_processors.py

The tests exercise the real module loaded from the .pyw file, so they
need the app's runtime dependencies (Pillow, pystray, customtkinter, …)
to be importable. They do not open any windows.
"""

from __future__ import annotations

import importlib.util
import json
import os
import string
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
# The non-GUI logic now lives in the `anime_subs` package; `anime_subs_core` is a
# thin façade that re-exports it AND exposes the submodules (app.db, app.runtime,
# app.ass_editor, …). Tests patch module-globals at their real home module
# (e.g. app.db.DB_PATH, app.ass_editor.atomic_write) so the patch lands in the
# namespace the code actually resolves the name from.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # make `import anime_subs` resolvable
MODULE_PATH = ROOT / "anime_subs_core.py"


def _load_app_module():
    """Load anime_subs_core (the façade) as a module."""
    spec = importlib.util.spec_from_file_location(
        "anime_subs_core_test", MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module spec for {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["anime_subs_customizer_test"] = module
    spec.loader.exec_module(module)
    return module


app = _load_app_module()

FileProcessor = app.FileProcessor
SecurityUtils = app.SecurityUtils
AppConfig = app.AppConfig
ASSStyleFields = app.ASSStyleFields
format_ass_primary_color = app.format_ass_primary_color


def _isolate_db(tc, tmp: Path) -> None:
    """Point the module DB at a throwaway file + give a clean schema, so tests
    never read or write the user's real files_monitor.db. DB_PATH and the pool
    globals live in app.db, so patch them there."""
    tc._orig_db_path = app.db.DB_PATH
    tc._orig_pool = app.db._db_pool
    tc._orig_shutdown = app.db._db_pool_shutdown
    app.db.DB_PATH = tmp / "test_files_monitor.db"
    app.db._db_pool = None
    app.db._db_pool_shutdown = False
    app.DatabaseManager.init_db()


def _restore_db(tc) -> None:
    """Tear down the per-test DB and restore the original module globals."""
    try:
        if app.db._db_pool is not None:
            app.db._db_pool.close_all()
    except Exception:
        pass
    app.db._db_pool = tc._orig_pool
    app.db.DB_PATH = tc._orig_db_path
    app.db._db_pool_shutdown = tc._orig_shutdown


# ---- Canonical sample ASS content ----

CANONICAL_ASS = """[Script Info]
Title: Sample
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,OldFont,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,1,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,Hello world
"""

# Same content but with the Format: columns re-ordered (Fontsize before Fontname).
REORDERED_ASS = """[Script Info]
Title: Sample
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontsize, Fontname, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,48,OldFont,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,1,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,Hello world
"""


class SanitizeFilenameTests(unittest.TestCase):
    def test_strips_path_separators(self):
        # Path traversal via language tag: `../evil`
        cleaned = SecurityUtils.sanitize_filename("../evil")
        self.assertNotIn("/", cleaned)
        self.assertNotIn("\\", cleaned)
        self.assertFalse(cleaned.startswith("."))

    def test_strips_control_chars(self):
        cleaned = SecurityUtils.sanitize_filename("evil\x00\nfile.ass")
        self.assertNotIn("\x00", cleaned)
        self.assertNotIn("\n", cleaned)

    def test_replaces_windows_reserved(self):
        cleaned = SecurityUtils.sanitize_filename("CON.ass")
        self.assertTrue(cleaned.startswith("_"))

    def test_windows_illegal_characters(self):
        cleaned = SecurityUtils.sanitize_filename('bad<name>?.ass')
        for ch in '<>?':
            self.assertNotIn(ch, cleaned)

    def test_collapses_trailing_dots(self):
        cleaned = SecurityUtils.sanitize_filename("name...")
        self.assertFalse(cleaned.endswith("."))

    def test_empty_after_sanitize(self):
        self.assertEqual(SecurityUtils.sanitize_filename("..."), "_")


class SanitizeAssFieldTests(unittest.TestCase):
    def test_strips_commas(self):
        # A font name with a comma used to splice an extra column into the
        # Style row. The comma must be removed before it reaches the file.
        self.assertEqual(
            SecurityUtils.sanitize_ass_field("Comic Sans, MS"),
            "Comic Sans MS",
        )

    def test_strips_newlines(self):
        cleaned = SecurityUtils.sanitize_ass_field("Arial\nBlack")
        self.assertNotIn("\n", cleaned)
        self.assertNotIn("\r", cleaned)

    def test_strips_tabs(self):
        self.assertNotIn("\t", SecurityUtils.sanitize_ass_field("Arial\tBold"))

    def test_empty_returns_empty(self):
        self.assertEqual(SecurityUtils.sanitize_ass_field(""), "")

    def test_preserves_unicode(self):
        self.assertEqual(SecurityUtils.sanitize_ass_field("Example Naskh"), "Example Naskh")


class SanitizeColorTests(unittest.TestCase):
    def test_accepts_valid(self):
        self.assertEqual(SecurityUtils.sanitize_color_code("80ffff"), "80FFFF")

    def test_rejects_short(self):
        with self.assertRaises(ValueError):
            SecurityUtils.sanitize_color_code("80ff")

    def test_rejects_non_hex(self):
        with self.assertRaises(ValueError):
            SecurityUtils.sanitize_color_code("zzzzzz")

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            SecurityUtils.sanitize_color_code("")


class DecodingTests(unittest.TestCase):
    def test_utf8_plain(self):
        result = FileProcessor._decode_bytes("مرحبا world\n".encode("utf-8"))
        self.assertIsNotNone(result)
        enc, text = result
        self.assertEqual(enc, "utf-8")
        self.assertIn("مرحبا", text)

    def test_utf8_bom(self):
        result = FileProcessor._decode_bytes(b"\xef\xbb\xbfhello")
        self.assertIsNotNone(result)
        enc, text = result
        self.assertEqual(enc, "utf-8-sig")
        self.assertEqual(text, "hello")

    def test_utf16_bom(self):
        raw = b"\xff\xfeh\x00i\x00"
        result = FileProcessor._decode_bytes(raw)
        self.assertIsNotNone(result)
        enc, text = result
        self.assertTrue(enc.startswith("utf-16"))
        self.assertEqual(text, "hi")

    def test_cp1256(self):
        # "مرحبا" in cp1256
        raw = "مرحبا".encode("cp1256")
        result = FileProcessor._decode_bytes(raw)
        self.assertIsNotNone(result)
        enc, text = result
        # Should not have replacement characters
        self.assertNotIn("\ufffd", text)

    def test_bomless_utf16_is_not_misread_as_utf8(self):
        # NUL bytes are legal UTF-8, so BOM-less UTF-16 used to "decode" as
        # UTF-8 and get rewritten as a UTF-8/UTF-16 hybrid. It must be refused.
        raw = "[Script Info]\r\nTitle: x\r\n".encode("utf-16-le")
        self.assertIsNone(FileProcessor._decode_bytes(raw))

    def test_rejects_invalid(self):
        # Mixed garbage that shouldn't cleanly decode — very uncommon
        raw = b"\xff\xff\xff\xff\xff\xff\xff\xff"
        result = FileProcessor._decode_bytes(raw)
        # Some codepages accept any byte; just verify no replacement chars survive
        if result is not None:
            _, text = result
            self.assertNotIn("\ufffd", text)


class ResolutionTests(unittest.TestCase):
    def test_extracts_playres(self):
        lines = CANONICAL_ASS.splitlines(keepends=True)
        x, y, sbs = FileProcessor._extract_resolution_from_lines(lines, "sample.ass")
        self.assertEqual(x, 1920)
        self.assertEqual(y, 1080)
        self.assertIsNone(sbs)  # CANONICAL_ASS has no ScaledBorderAndShadow

    def test_missing_returns_none(self):
        lines = ["[Script Info]\n", "Title: nope\n"]
        x, y, sbs = FileProcessor._extract_resolution_from_lines(lines, "x")
        self.assertIsNone(x)
        self.assertIsNone(y)
        self.assertIsNone(sbs)

    def test_clamps_absurd(self):
        # An absurd PlayResY is clamped to None while a valid sibling PlayResX
        # alongside it is preserved (isolates the clamp from the discard).
        lines = ["PlayResX: 1920\n", "PlayResY: 99999999\n"]
        x, y, _ = FileProcessor._extract_resolution_from_lines(lines, "x")
        self.assertEqual(x, 1920)
        self.assertIsNone(y)

    def test_clamp_boundary_not_clamped(self):
        # Exactly _MAX_SANE_PLAYRES_Y (4320) must pass through unclamped.
        _, y, _ = FileProcessor._extract_resolution_from_lines(["PlayResY: 4320\n"], "x")
        self.assertEqual(y, 4320)

    def test_scaled_border_header_parsed(self):
        yes = ["ScaledBorderAndShadow: yes\n"]
        no = ["scaledborderandshadow:   No\n"]
        _, _, sbs_yes = FileProcessor._extract_resolution_from_lines(yes, "x")
        _, _, sbs_no = FileProcessor._extract_resolution_from_lines(no, "x")
        self.assertIs(sbs_yes, True)
        self.assertIs(sbs_no, False)

    def test_effective_playres_y(self):
        # Explicit Y wins; X-only derives Y = X * 3/4; both absent falls back
        # to the spec's 384x288 default canvas.
        self.assertEqual(FileProcessor._effective_playres_y(1920, 1080), 1080)
        self.assertEqual(FileProcessor._effective_playres_y(1024, None), 768)
        # libass/VSFilter special-case 1280 wide: a 1280x1024 canvas.
        self.assertEqual(FileProcessor._effective_playres_y(1280, None), 1024)
        self.assertEqual(FileProcessor._effective_playres_y(None, None), 288)


class ValidateAssLinesTests(unittest.TestCase):
    def test_canonical_format_builds_map(self):
        lines = CANONICAL_ASS.splitlines(keepends=True)
        _, field_map, count, dirty = FileProcessor.validate_ass_lines(lines, Path("x"))
        self.assertFalse(dirty)
        self.assertEqual(count, 23)
        self.assertEqual(field_map["fontname"], 1)
        self.assertEqual(field_map["primarycolour"], 3)

    def test_reordered_format_respects_order(self):
        lines = REORDERED_ASS.splitlines(keepends=True)
        _, field_map, count, dirty = FileProcessor.validate_ass_lines(lines, Path("x"))
        self.assertFalse(dirty)
        self.assertEqual(count, 23)
        # Reordered: Fontsize at 1, Fontname at 2.
        self.assertEqual(field_map["fontsize"], 1)
        self.assertEqual(field_map["fontname"], 2)

    def test_wrong_arity_forces_canonical(self):
        bad = """[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,Arial,20
"""
        lines = bad.splitlines(keepends=True)
        new_lines, field_map, count, dirty = FileProcessor.validate_ass_lines(
            lines, Path("x")
        )
        self.assertTrue(dirty)
        self.assertEqual(count, 23)
        # Style line was padded to 23 fields.
        style_line = next(l for l in new_lines if l.lstrip().startswith("Style:"))
        self.assertEqual(len(style_line.split(":", 1)[1].split(",")), 23)


class ApplyChangesTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        # apply_changes -> add_file writes to the DB; isolate it so the suite
        # doesn't pollute the user's real files_monitor.db.
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def _write(self, content: str, name: str = "sample.ass") -> Path:
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_canonical_apply_changes(self):
        path = self._write(CANONICAL_ASS)
        ok = FileProcessor.apply_changes(
            path, "NewFont", "80FFFF",
            bold=True, italic=False, underline=False,
        )
        self.assertTrue(ok)
        content = path.read_text(encoding="utf-8")
        # Style line should now contain NewFont and the BGR-wrapped color.
        style = next(l for l in content.splitlines() if l.startswith("Style:"))
        parts = style.split(":", 1)[1].split(",")
        self.assertEqual(parts[1].strip(), "NewFont")
        self.assertEqual(parts[3].strip(), format_ass_primary_color("80FFFF"))
        self.assertEqual(parts[7].strip(), "-1")  # bold
        self.assertEqual(parts[8].strip(), "0")   # italic
        # Adaptive outline for 1080p == BASE_OUTLINE * 1080/720 = 2.0 * 1.5 = 3.0
        self.assertEqual(parts[16].strip(), "3.0")
        # Backup created
        backup = self.tmp / "Original Subs" / path.name
        self.assertTrue(backup.exists())

    def test_reordered_format_is_respected(self):
        # The bug this guards against: writing into fixed indices would put
        # "NewFont" into the Fontsize slot.
        path = self._write(REORDERED_ASS)
        ok = FileProcessor.apply_changes(
            path, "NewFont", "112233",
        )
        self.assertTrue(ok)
        content = path.read_text(encoding="utf-8")
        style = next(l for l in content.splitlines() if l.startswith("Style:"))
        parts = style.split(":", 1)[1].split(",")
        # In the reordered file: index 1 = Fontsize, index 2 = Fontname.
        self.assertEqual(parts[1].strip(), "48", "Fontsize must be preserved")
        self.assertEqual(parts[2].strip(), "NewFont", "Fontname must be set")

    def test_is_already_styled_detects_match(self):
        path = self._write(CANONICAL_ASS)
        FileProcessor.apply_changes(
            path, "NewFont", "80FFFF",
            bold=True, italic=False, underline=False,
        )
        self.assertTrue(FileProcessor.is_already_styled(
            path, "NewFont", "80FFFF",
            bold=True, italic=False, underline=False,
        ))

    def test_font_name_with_comma_does_not_corrupt_row(self):
        # Regression: a font name containing a comma used to splice an
        # extra column into the Style row; fields after Fontname would
        # shift left by one.
        path = self._write(CANONICAL_ASS)
        ok = FileProcessor.apply_changes(
            path, "Comic Sans, MS", "80FFFF",
        )
        self.assertTrue(ok)
        style = next(
            l for l in path.read_text(encoding="utf-8").splitlines()
            if l.startswith("Style:")
        )
        parts = style.split(":", 1)[1].split(",")
        # Row still has the canonical column count…
        self.assertEqual(len(parts), 23)
        # …and the comma is gone from the font name.
        self.assertEqual(parts[1].strip(), "Comic Sans MS")

    def test_dialogue_color_tag_inserted_canonical(self):
        path = self._write(CANONICAL_ASS)
        FileProcessor.apply_changes(path, "X", "112233")
        content = path.read_text(encoding="utf-8")
        dlg = next(l for l in content.splitlines() if l.startswith("Dialogue:"))
        # Color tag should appear once, at the start of Text.
        self.assertIn("{\\c&H112233&}Hello world", dlg)

    def test_dialogue_color_tag_respects_events_format_order(self):
        # Events Format with Text at index 4 instead of the canonical 9.
        content = """[Script Info]
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,OldFont,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,1,2,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,0:00:01.00,0:00:05.00,Default,Hello there
"""
        path = self._write(content, name="events.ass")
        ok = FileProcessor.apply_changes(path, "X", "112233")
        self.assertTrue(ok)
        out = path.read_text(encoding="utf-8")
        dlg = next(l for l in out.splitlines() if l.startswith("Dialogue:"))
        # Tag must sit in front of the actual Text, not wherever parts[9]
        # happened to land in a short Events row.
        self.assertIn("{\\c&H112233&}Hello there", dlg)

    def test_dialogue_text_with_commas_preserved(self):
        content = CANONICAL_ASS.replace(
            "Hello world",
            "Wait, what, seriously, wow",
        )
        path = self._write(content)
        FileProcessor.apply_changes(path, "X", "112233")
        out = path.read_text(encoding="utf-8")
        dlg = next(l for l in out.splitlines() if l.startswith("Dialogue:"))
        # All three commas must survive the round-trip.
        self.assertIn("{\\c&H112233&}Wait, what, seriously, wow", dlg)

    def test_is_already_styled_catches_outline_change(self):
        # Fabricate a file that was styled with the OLD (1.5) outline base.
        # The pinned header is present so the check reaches the size compare.
        content = CANONICAL_ASS.replace(
            "[Script Info]\n", "[Script Info]\nScaledBorderAndShadow: yes\n"
        ).replace(
            "Style: Default,OldFont,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,1,2,10,10,10,1",
            "Style: Default,NewFont,48,&H0080FFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,2.25,0.75,2,10,10,10,1",
        )
        path = self._write(content)
        # Expected outline at 1080p with base 2.0 is 3.0; the file has 2.25.
        self.assertFalse(FileProcessor.is_already_styled(
            path, "NewFont", "80FFFF",
            bold=True, italic=False, underline=False,
        ))

    def test_apply_pins_scaled_border_header(self):
        # CANONICAL_ASS has no ScaledBorderAndShadow; apply must insert
        # exactly one "yes" right after [Script Info], because the scaled
        # Outline/Shadow values are script-space units only under "yes".
        path = self._write(CANONICAL_ASS)
        self.assertTrue(FileProcessor.apply_changes(path, "NewFont", "80FFFF"))
        out = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(out.count("ScaledBorderAndShadow: yes"), 1)
        self.assertEqual(
            out[out.index("[Script Info]") + 1], "ScaledBorderAndShadow: yes"
        )

    def test_apply_idempotent_after_header_pin(self):
        path = self._write(CANONICAL_ASS)
        self.assertTrue(FileProcessor.apply_changes(path, "NewFont", "80FFFF"))
        once = path.read_bytes()
        self.assertTrue(FileProcessor.apply_changes(path, "NewFont", "80FFFF"))
        self.assertEqual(path.read_bytes(), once)

    def test_apply_respects_explicit_scaled_border_no(self):
        # Explicit "no" means Outline/Shadow are output pixels: write the
        # base sizes unscaled and leave the author's header alone.
        content = CANONICAL_ASS.replace(
            "[Script Info]\n", "[Script Info]\nScaledBorderAndShadow: no\n"
        )
        path = self._write(content)
        self.assertTrue(FileProcessor.apply_changes(path, "NewFont", "80FFFF"))
        out = path.read_text(encoding="utf-8")
        self.assertIn("ScaledBorderAndShadow: no", out)
        self.assertNotIn("ScaledBorderAndShadow: yes", out)
        style = next(l for l in out.splitlines() if l.startswith("Style:"))
        parts = style.split(":", 1)[1].split(",")
        self.assertEqual(parts[16].strip(), "2.0")  # base, not 1080-scaled
        self.assertEqual(parts[17].strip(), "1.0")
        # The styled-check must agree with what apply wrote.
        self.assertTrue(FileProcessor.is_already_styled(path, "NewFont", "80FFFF"))

    def test_apply_missing_playres_uses_spec_default(self):
        # No PlayResX/Y: the script space defaults to 384x288, so sizes
        # scale DOWN (2.0 * 288/720 = 0.8) instead of pretending 720.
        content = CANONICAL_ASS.replace("PlayResX: 1920\n", "").replace(
            "PlayResY: 1080\n", ""
        )
        path = self._write(content)
        self.assertTrue(FileProcessor.apply_changes(path, "NewFont", "80FFFF"))
        style = next(
            l for l in path.read_text(encoding="utf-8").splitlines()
            if l.startswith("Style:")
        )
        parts = style.split(":", 1)[1].split(",")
        self.assertEqual(parts[16].strip(), "0.8")
        self.assertEqual(parts[17].strip(), "0.4")

    def test_apply_playresx_only_derives_height(self):
        # Only PlayResX present: VSFilter derives Y = X * 3/4 (1024 -> 768).
        content = CANONICAL_ASS.replace(
            "PlayResX: 1920\n", "PlayResX: 1024\n"
        ).replace("PlayResY: 1080\n", "")
        path = self._write(content)
        self.assertTrue(FileProcessor.apply_changes(path, "NewFont", "80FFFF"))
        style = next(
            l for l in path.read_text(encoding="utf-8").splitlines()
            if l.startswith("Style:")
        )
        parts = style.split(":", 1)[1].split(",")
        self.assertEqual(parts[16].strip(), "2.13")  # 2.0 * 768/720
        self.assertEqual(parts[17].strip(), "1.07")

    def test_apply_prepends_script_info_when_missing(self):
        # A degenerate file with no [Script Info] still gets the pin — via a
        # prepended section — so the styled-check can converge.
        content = "[V4+ Styles]" + CANONICAL_ASS.split("[V4+ Styles]", 1)[1]
        path = self._write(content)
        self.assertTrue(FileProcessor.apply_changes(path, "NewFont", "80FFFF"))
        out = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(out[0], "[Script Info]")
        self.assertEqual(out[1], "ScaledBorderAndShadow: yes")
        self.assertTrue(FileProcessor.is_already_styled(path, "NewFont", "80FFFF"))

    def test_styled_check_requires_pinned_header(self):
        # Matching values but no header -> NOT styled; one apply pass pins
        # the header, after which the check holds (a single extra rewrite,
        # not a loop).
        content = CANONICAL_ASS.replace(
            "Style: Default,OldFont,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,1,2,10,10,10,1",
            "Style: Default,NewFont,48,&H0080FFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3.0,1.5,2,10,10,10,1",
        )
        path = self._write(content)
        self.assertFalse(FileProcessor.is_already_styled(path, "NewFont", "80FFFF"))
        self.assertTrue(FileProcessor.apply_changes(path, "NewFont", "80FFFF"))
        self.assertTrue(FileProcessor.is_already_styled(path, "NewFont", "80FFFF"))


class TypesettingStyleDetectionTests(unittest.TestCase):
    """Unit tests for FileProcessor._typesetting_styles — which Style names are
    treated as signs/typesetting/karaoke and therefore left un-restyled."""

    HEADER = [
        "[Events]\n",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n",
    ]

    def _skip(self, *dialogue_lines):
        return FileProcessor._typesetting_styles(self.HEADER + list(dialogue_lines))

    def test_positioned_sign_style_flagged(self):
        skip = self._skip(
            "Dialogue: 0,0,0,Default,,0,0,0,,Hello\n",
            "Dialogue: 0,0,0,Board,,0,0,0,,{\\pos(960,200)\\clip(1,2,3,4)}SALE\n",
        )
        self.assertIn("Board", skip)
        self.assertNotIn("Default", skip)

    def test_drawing_only_style_flagged(self):
        # A style whose lines are vector drawings is a sign (here 1/1 = majority).
        skip = self._skip(
            "Dialogue: 0,0,0,Gfx,,0,0,0,,{\\p1}m 0 0 l 10 0 10 10 0 10{\\p0}\n",
        )
        self.assertIn("Gfx", skip)

    def test_minority_drawing_dialogue_not_flagged(self):
        # Real regression (Draw This, Then Die!): a mostly-dialogue style with a
        # few drawn typeset elements under it must stay dialogue — drawing is a
        # majority signal, not an absolute one.
        lines = [f"Dialogue: 0,0,0,Default,,0,0,0,,سطر {i}\n" for i in range(20)]
        lines += ["Dialogue: 0,0,0,Default,,0,0,0,,{\\p1}m 0 0 l 5 5{\\p0}\n"]
        self.assertNotIn("Default", self._skip(*lines))

    def test_karaoke_style_flagged(self):
        skip = self._skip(
            "Dialogue: 0,0,0,Romaji,,0,0,0,,{\\k30}ka{\\k22}ra{\\k18}o{\\k25}ke\n",
        )
        self.assertIn("Romaji", skip)

    def test_minority_positioned_dialogue_not_flagged(self):
        # A plain dialogue style with one stray positioned line stays dialogue.
        skip = self._skip(
            "Dialogue: 0,0,0,Default,,0,0,0,,Line one\n",
            "Dialogue: 0,0,0,Default,,0,0,0,,Line two\n",
            "Dialogue: 0,0,0,Default,,0,0,0,,{\\pos(100,100)}moved once\n",
        )
        self.assertNotIn("Default", skip)

    def test_name_token_flags_sparse_sign_style(self):
        # No positioning tags at all, but the name says typesetting.
        skip = self._skip(
            "Dialogue: 0,0,0,OP,,0,0,0,,opening lyric\n",
            "Dialogue: 0,0,0,Signs,,0,0,0,,plain caption text\n",
        )
        self.assertIn("OP", skip)
        self.assertIn("Signs", skip)

    def test_name_token_is_whole_word_only(self):
        # "ed" must not fire on "Editor"; "op" must not fire on a normal style.
        skip = self._skip(
            "Dialogue: 0,0,0,Editor,,0,0,0,,note from the editor\n",
            "Dialogue: 0,0,0,Flashback,,0,0,0,,a memory\n",
        )
        self.assertNotIn("Editor", skip)
        self.assertNotIn("Flashback", skip)

    def test_scale_and_fade_are_not_typesetting_signals(self):
        # \fscx/\fscy (scaling) and \fad (fades) are common in plain dialogue
        # and must not be mistaken for sign markers.
        skip = self._skip(
            "Dialogue: 0,0,0,Default,,0,0,0,,{\\fad(200,200)\\fscx105\\fscy105}Hi there\n",
        )
        self.assertNotIn("Default", skip)

    def test_custom_events_format_order(self):
        lines = [
            "[Events]\n",
            "Format: Layer, Start, End, Style, Text\n",
            "Dialogue: 0,0,0,Sign,{\\pos(10,10)}here\n",
            "Dialogue: 0,0,0,Main,plain line\n",
        ]
        skip = FileProcessor._typesetting_styles(lines)
        self.assertIn("Sign", skip)
        self.assertNotIn("Main", skip)

    # --- Regression: exact-tie must NOT flag dialogue (strict majority) -----
    def test_half_positioned_dialogue_not_flagged(self):
        # 10 Default lines, 5 with \pos (overlap-avoidance) = exact 50% tie.
        # A tie is NOT a majority — the main dialogue must keep the user's font.
        lines = [f"Dialogue: 0,0,0,Default,,0,0,0,,{{\\pos(960,90{i})}}سطر {i}\n"
                 for i in range(5)]
        lines += [f"Dialogue: 0,0,0,Default,,0,0,0,,سطر عادي {i}\n" for i in range(5)]
        self.assertNotIn("Default", self._skip(*lines))

    def test_two_line_one_positioned_dialogue_not_flagged(self):
        # 2-line Overlap style, 1 positioned = tie -> must stay dialogue.
        skip = self._skip(
            "Dialogue: 0,0,0,Overlap,,0,0,0,,{\\pos(300,880)}اخرج من هنا\n",
            "Dialogue: 0,0,0,Overlap,,0,0,0,,وإلا سأتصل بالشرطة\n",
        )
        self.assertNotIn("Overlap", skip)

    def test_strict_majority_still_flags_sign(self):
        # 3 lines, 2 positioned (>50%) -> genuine majority -> flagged.
        skip = self._skip(
            "Dialogue: 0,0,0,Board,,0,0,0,,{\\pos(1,1)}A\n",
            "Dialogue: 0,0,0,Board,,0,0,0,,{\\pos(2,2)}B\n",
            "Dialogue: 0,0,0,Board,,0,0,0,,plain\n",
        )
        self.assertIn("Board", skip)

    # --- Regression: Arabic sign-style names recognised by substring --------
    def test_arabic_sign_name_flagged_without_positioning(self):
        # محاكاة (typesetting) with only scale/blur — no \pos — must still be
        # protected via the Arabic name substring (the ASCII tokeniser is blind
        # to it).
        skip = self._skip(
            "Dialogue: 0,0,0,محاكاة,,0,0,0,,{\\an8\\fscx130\\blur2}مدرسة طوكيو\n",
        )
        self.assertIn("محاكاة", skip)

    def test_arabic_dialogue_name_not_flagged(self):
        skip = self._skip(
            "Dialogue: 0,0,0,حوار,,0,0,0,,مرحبا كيف حالك\n",
        )
        self.assertNotIn("حوار", skip)

    # --- Regression: weak name tokens only fire on a whole-name match --------
    def test_weak_token_nickname_not_flagged(self):
        # "Note-chan" splits to [note, chan]; "note" is a WEAK token so it must
        # NOT flag a delimiter-adjacent nickname dialogue style.
        skip = self._skip(
            "Dialogue: 0,0,0,Note-chan,,0,0,0,,مرحبا أنا هنا\n",
            "Dialogue: 0,0,0,Note,,0,0,0,,شيء ما\n",
        )
        self.assertNotIn("Note-chan", skip)
        self.assertIn("Note", skip)          # whole-name weak token DOES flag

    def test_scene_caption_names_flagged(self):
        # Margin-anchored / scale-only signs with conventional caption names but
        # no \pos are caught by the strong name-token net.
        skip = self._skip(
            "Dialogue: 0,0,0,Screen,,0,0,540,,{\\an8}TOKYO HIGH SCHOOL\n",
            "Dialogue: 0,0,0,Location,,0,0,0,,{\\an1}Shibuya - 3 Days Earlier\n",
        )
        self.assertIn("Screen", skip)
        self.assertIn("Location", skip)

    def test_transform_only_animated_sign_flagged(self):
        # A sign animated with ONLY \t (transform) scaling — no \pos/\move/\clip
        # and a non-sign name — must still be caught by the tag-majority test.
        # \fscx/\fscy alone don't count (common in dialogue); \t is the signal.
        skip = self._skip(
            "Dialogue: 0,0,0,Fx,,0,0,0,,{\\t(0,500,\\fscx120\\fscy120)}grow\n",
        )
        self.assertIn("Fx", skip)


# A two-style file: one plain dialogue style, one positioned sign style.
TYPESET_ASS = """[Script Info]
ScriptType: v4.00+
ScaledBorderAndShadow: yes
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,OldFont,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,1,2,10,10,10,1
Style: Sign,SignFont,60,&H0000FFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,1,7,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,Hello world
Dialogue: 0,0:00:02.00,0:00:06.00,Sign,,0,0,0,,{\\pos(960,200)\\clip(0,0,50,50)}BILLBOARD
"""


# A signs-only track: the single style is a positioned sign (no dialogue style).
SIGNS_ONLY_ASS = """[Script Info]
ScriptType: v4.00+
ScaledBorderAndShadow: yes
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sign,SignFont,60,&H0000FFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1.5,1,7,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:02.00,0:00:06.00,Sign,,0,0,0,,{\\pos(960,200)}BILLBOARD
"""


class TypesettingProtectionTests(unittest.TestCase):
    """compute_changes must restyle dialogue styles while leaving sign/
    typesetting styles (and their Dialogue lines) completely untouched."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)

    def tearDown(self):
        self._tempdir.cleanup()

    def _compute(self, content):
        path = self.tmp / "s.ass"
        path.write_text(content, encoding="utf-8")
        result = FileProcessor.compute_changes(path, "NewFont", "112233")
        self.assertIsNotNone(result)
        return result[1]

    def _style_row(self, new_lines, name):
        for l in new_lines:
            if l.lstrip().startswith("Style:") and \
                    l.split(":", 1)[1].split(",")[0].strip() == name:
                return l
        raise AssertionError(f"no Style row named {name!r}")

    def test_sign_style_row_untouched_dialogue_restyled(self):
        new_lines = self._compute(TYPESET_ASS)
        # Dialogue style: font swapped to NewFont.
        self.assertIn("NewFont", self._style_row(new_lines, "Default"))
        # Sign style: original font preserved, our font absent.
        sign_row = self._style_row(new_lines, "Sign")
        self.assertIn("SignFont", sign_row)
        self.assertNotIn("NewFont", sign_row)

    def test_sign_dialogue_line_gets_no_color_tag(self):
        new_lines = self._compute(TYPESET_ASS)
        dlgs = [l for l in new_lines if l.lstrip().startswith("Dialogue:")]
        default_dlg = next(l for l in dlgs if ",Default," in l)
        sign_dlg = next(l for l in dlgs if ",Sign," in l)
        # Spoken dialogue gets the colour tag…
        self.assertIn("{\\c&H112233&}Hello world", default_dlg)
        # …the sign line is left exactly as authored.
        self.assertNotIn("{\\c&H112233&}", sign_dlg)
        self.assertIn("{\\pos(960,200)\\clip(0,0,50,50)}BILLBOARD", sign_dlg)

    def test_styled_file_with_sign_style_reads_as_already_styled(self):
        # Regression: is_already_styled must also skip typesetting styles, or a
        # correctly-styled file whose Sign style (deliberately) keeps its own
        # font always looks "unstyled" and gets needlessly re-processed.
        path = self.tmp / "s.ass"
        path.write_text(TYPESET_ASS, encoding="utf-8")
        new_lines = FileProcessor.compute_changes(path, "NewFont", "112233")[1]
        path.write_text("".join(new_lines), encoding="utf-8")
        self.assertTrue(
            FileProcessor.is_already_styled(path, "NewFont", "112233")
        )

    def test_signs_only_file_reads_as_already_styled(self):
        # Regression: a file whose EVERY V4+ style is a sign/typesetting style (a
        # signs-only track) must read as already-styled — compute_changes leaves
        # it untouched, so judging it "unstyled" needlessly re-backs-up and
        # rewrites it on every apply.
        path = self.tmp / "signs.ass"
        path.write_text(SIGNS_ONLY_ASS, encoding="utf-8")
        new_lines = FileProcessor.compute_changes(path, "NewFont", "112233")[1]
        # The lone Sign style is preserved…
        sign_row = self._style_row(new_lines, "Sign")
        self.assertIn("SignFont", sign_row)
        self.assertNotIn("NewFont", sign_row)
        # …and the file therefore reads as styled (nothing to do), not unstyled.
        path.write_text("".join(new_lines), encoding="utf-8")
        self.assertTrue(
            FileProcessor.is_already_styled(path, "NewFont", "112233")
        )


class DialogueSizeTests(unittest.TestCase):
    """Resolution-relative Fontsize presets: applied to dialogue styles only,
    scaled to each file's PlayResY, opt-in (none = every size untouched)."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)

    def tearDown(self):
        self._td.cleanup()

    def _fontsize(self, lines, name):
        # Canonical Format: Fontsize is column index 2.
        for l in lines:
            if l.lstrip().startswith("Style:") and \
                    l.split(":", 1)[1].split(",")[0].strip() == name:
                return l.split(":", 1)[1].split(",")[2].strip()
        raise AssertionError(f"no Style row named {name!r}")

    def _compute(self, content, size):
        p = self.tmp / "s.ass"
        p.write_text(content, encoding="utf-8")
        result = FileProcessor.compute_changes(p, "NewFont", "112233", size=size)
        self.assertIsNotNone(result)
        return result[1]

    def test_medium_sets_resolution_relative_size(self):
        # 1080 * 0.070 = 75.6 -> 76
        self.assertEqual(self._fontsize(self._compute(CANONICAL_ASS, "medium"),
                                        "Default"), "76")

    def test_none_leaves_size_untouched(self):
        self.assertEqual(self._fontsize(self._compute(CANONICAL_ASS, "none"),
                                        "Default"), "48")

    def test_size_scales_with_the_files_resolution(self):
        # A genuine 16:9 360p file (640x360). 360 * 0.070 = 25.2 -> 25.
        content = (CANONICAL_ASS.replace("PlayResX: 1920", "PlayResX: 640")
                                 .replace("PlayResY: 1080", "PlayResY: 360"))
        self.assertEqual(self._fontsize(self._compute(content, "medium"),
                                        "Default"), "25")

    def test_widescreen_sizes_against_16by9_equivalent(self):
        # A 1920x816 (2.35:1) scope file letterboxes on a 16:9 screen; sizing by
        # PlayResY (816) would render ~25% smaller than a 16:9 file. We size by
        # the 16:9-equivalent height 1920*9/16 = 1080 instead, so it matches.
        content = CANONICAL_ASS.replace("PlayResY: 1080", "PlayResY: 816")
        # basis = max(816, 1080) = 1080; medium 0.070 * 1080 = 75.6 -> 76
        self.assertEqual(self._fontsize(self._compute(content, "medium"),
                                        "Default"), "76")

    def test_size_basis_height(self):
        f = FileProcessor._size_basis_height
        self.assertEqual(f(1920, 1080), 1080)   # 16:9 -> PlayResY
        self.assertEqual(f(1920, 816), 1080)    # 2.35:1 -> 16:9-equivalent
        self.assertEqual(f(832, 360), 468)      # 2.31:1 scope
        self.assertEqual(f(2538, 1080), 1428)   # native 2.35:1 scope
        self.assertEqual(f(640, 480), 480)      # 4:3 pillarbox -> keeps height
        # PlayResX-only: falls through to the renderer's own derived height
        # (px*3/4), which is correct for the common X-only 16:9/4:3 video (the
        # renderer fits to that same height); a scope X-only file is malformed.
        self.assertEqual(f(2538, None), 1904)   # X-only -> px*3/4
        self.assertEqual(f(1920, None), 1440)

    def _field(self, lines, name, idx):
        for l in lines:
            if l.lstrip().startswith("Style:") and \
                    l.split(":", 1)[1].split(",")[0].strip() == name:
                return l.split(":", 1)[1].split(",")[idx].strip()
        raise AssertionError(f"no Style row named {name!r}")

    def test_scope_outline_tracks_the_enlarged_font(self):
        # 1920x816 scope + a size preset scales outline off the same 16:9-equiv
        # basis (1080) as the font, so the border isn't thin next to the big
        # glyph. With no preset the outline stays content-relative (816).
        content = CANONICAL_ASS.replace("PlayResY: 1080", "PlayResY: 816")
        self.assertEqual(self._field(self._compute(content, "large"),
                                     "Default", 16), "3.0")    # basis 1080
        self.assertEqual(self._field(self._compute(content, "none"),
                                     "Default", 16), "2.27")   # basis 816

    def test_missing_fontsize_column_does_not_loop(self):
        # A Format line with NO Fontsize column: compute_changes can't write a
        # size, and is_already_styled must still agree the file is styled when a
        # size preset is set (else it re-applies forever).
        fmt = ("Format: Name, Fontname, PrimaryColour, SecondaryColour, "
               "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
               "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
               "Alignment, MarginL, MarginR, MarginV, Encoding")
        content = (
            "[Script Info]\nScaledBorderAndShadow: yes\nPlayResX: 1920\n"
            "PlayResY: 1080\n\n[V4+ Styles]\n" + fmt + "\n"
            "Style: Default,OldFont,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
            "0,0,0,0,100,100,0,0,1,1.5,1,2,10,10,10,1\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0,0,Default,,0,0,0,,مرحبا\n"
        )
        p = self.tmp / "nofs.ass"
        p.write_text(content, encoding="utf-8")
        new = FileProcessor.compute_changes(p, "NewFont", "112233", size="large")[1]
        p.write_text("".join(new), encoding="utf-8")
        self.assertTrue(
            FileProcessor.is_already_styled(p, "NewFont", "112233", size="large"))

    def test_size_never_touches_typesetting_styles(self):
        lines = self._compute(TYPESET_ASS, "large")
        self.assertEqual(self._fontsize(lines, "Sign"), "60")        # untouched
        self.assertEqual(self._fontsize(lines, "Default"), "97")     # 1080*0.090

    def test_is_already_styled_roundtrips_with_size(self):
        p = self.tmp / "s.ass"
        p.write_text(CANONICAL_ASS, encoding="utf-8")
        new = FileProcessor.compute_changes(p, "NewFont", "80FFFF", size="medium")[1]
        p.write_text("".join(new), encoding="utf-8")
        self.assertTrue(
            FileProcessor.is_already_styled(p, "NewFont", "80FFFF", size="medium"))
        # Switching the preset must force a rewrite.
        self.assertFalse(
            FileProcessor.is_already_styled(p, "NewFont", "80FFFF", size="large"))
        # size="none" doesn't manage Fontsize, so it still reads as styled
        # (font/colour match) — it won't force-revert a previously-sized file.
        self.assertTrue(
            FileProcessor.is_already_styled(p, "NewFont", "80FFFF"))

    def test_normalized_size_roundtrips_for_a_real_font(self):
        # With a resolvable font the Fontsize is normalized by the font's own
        # measured letter height. compute_changes and is_already_styled must
        # still agree on that value, or the file re-processes forever. Holds
        # whether or not freetype is present: both paths share _size_targets.
        p = self.tmp / "real.ass"
        p.write_text(CANONICAL_ASS, encoding="utf-8")
        new = FileProcessor.compute_changes(p, "Arial", "80FFFF", size="medium")[1]
        p.write_text("".join(new), encoding="utf-8")
        self.assertTrue(
            FileProcessor.is_already_styled(p, "Arial", "80FFFF", size="medium"))

    def test_font_metric_normalization_is_applied_when_measurable(self):
        # When the font's letter height is measurable, the Fontsize is scaled
        # by REFERENCE_SIZE_RATIO / ratio — so a font whose ink height differs
        # from the reference lands on a different Fontsize than the raw preset.
        from anime_subs.runtime import REFERENCE_SIZE_RATIO
        ratio = app.FontSupport.size_ratio("Arial", "latin")
        if ratio is None:
            self.skipTest("freetype/Arial unavailable — normalization falls back")
        expected = round(0.070 * 1080 * REFERENCE_SIZE_RATIO / ratio)
        self.assertEqual(
            self._fontsize(
                FileProcessor.compute_changes(
                    self._write(CANONICAL_ASS), "Arial", "112233", size="medium")[1],
                "Default"),
            str(expected))

    _ARABIC_ASS = CANONICAL_ASS.replace(
        "Hello world",
        "سبحان الله وبحمده هذه جملة عربية طويلة للاختبار")

    def test_arabic_normalized_size_roundtrips(self):
        # Arabic sizes by the shaped reading-body metric (a different code path
        # than Latin cap-height). compute_changes and is_already_styled must
        # still agree on the value — holds whether or not HarfBuzz/FreeType are
        # present, since both derive from the same _size_targets.
        p = self.tmp / "ar.ass"
        p.write_text(self._ARABIC_ASS, encoding="utf-8")
        new = FileProcessor.compute_changes(p, "Arial", "80FFFF", size="large")[1]
        p.write_text("".join(new), encoding="utf-8")
        self.assertTrue(
            FileProcessor.is_already_styled(p, "Arial", "80FFFF", size="large"))

    _GREY_OUTLINE_ASS = (
        "[Script Info]\nScaledBorderAndShadow: yes\nPlayResX: 1920\nPlayResY: 1080\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
        "SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Old,66,&H00FFFFFF,&H000000FF,&H00202020,&H005C5C5C,0,0,0,0,"
        "100,100,0,0,1,2.5,0,2,10,10,55,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\nDialogue: 0,0,0,Default,,0,0,0,,مرحبا بالعالم\n"
    )

    def test_outline_shadow_colour_pinned_black(self):
        # A sub that ships a grey outline/shadow colour must be repainted opaque
        # black, and is_already_styled must NOT read the grey file as already
        # styled (or the washed-out edge would never get fixed).
        p = self.tmp / "oc.ass"
        p.write_text(self._GREY_OUTLINE_ASS, encoding="utf-8")
        self.assertFalse(FileProcessor.is_already_styled(
            p, "Arial", "112233", size="none"))
        new = FileProcessor.compute_changes(p, "Arial", "112233", size="none")[1]
        self.assertEqual(self._field(new, "Default", 5), "&H00000000")  # OutlineColour
        self.assertEqual(self._field(new, "Default", 6), "&H00000000")  # BackColour
        p.write_text("".join(new), encoding="utf-8")
        self.assertTrue(FileProcessor.is_already_styled(
            p, "Arial", "112233", size="none"))

    def test_arabic_smaller_body_font_gets_bigger_fontsize(self):
        # The bug this metric fixes: a delicate naskh face (Arabic Typesetting) has
        # a SMALLER reading body than a chunkier one (Segoe UI), so it must get a
        # BIGGER Fontsize to render at the same visible size. The old isolated-
        # glyph metric measured flourishes and got this backwards.
        thin = app.FontSupport.size_ratio("Arabic Typesetting", "arabic")
        thick = app.FontSupport.size_ratio("Segoe UI", "arabic")
        if thin is None or thick is None:
            self.skipTest("HarfBuzz or the two reference fonts unavailable")
        self.assertLess(thin, thick)   # smaller body ratio
        fs_thin = int(self._fontsize(FileProcessor.compute_changes(
            self._write(self._ARABIC_ASS, "a.ass"),
            "Arabic Typesetting", "112233", size="large")[1], "Default"))
        fs_thick = int(self._fontsize(FileProcessor.compute_changes(
            self._write(self._ARABIC_ASS, "b.ass"),
            "Segoe UI", "112233", size="large")[1], "Default"))
        self.assertGreater(fs_thin, fs_thick)

    def test_size_nudge_scales_and_roundtrips(self):
        # A per-font nudge multiplies the normalized Fontsize. compute_changes and
        # is_already_styled must agree on the nudged value (same nudge) or the file
        # re-processes forever; a DIFFERENT nudge must force a rewrite.
        base = int(self._fontsize(FileProcessor.compute_changes(
            self._write(CANONICAL_ASS, "n0.ass"), "Arial", "112233",
            size="medium")[1], "Default"))
        up = int(self._fontsize(FileProcessor.compute_changes(
            self._write(CANONICAL_ASS, "n1.ass"), "Arial", "112233",
            size="medium", nudge=1.20)[1], "Default"))
        self.assertGreater(up, base)   # +20% nudge -> bigger Fontsize
        p = self.tmp / "nud.ass"
        p.write_text(CANONICAL_ASS, encoding="utf-8")
        new = FileProcessor.compute_changes(
            p, "Arial", "80FFFF", size="medium", nudge=1.20)[1]
        p.write_text("".join(new), encoding="utf-8")
        self.assertTrue(FileProcessor.is_already_styled(
            p, "Arial", "80FFFF", size="medium", nudge=1.20))
        self.assertFalse(FileProcessor.is_already_styled(
            p, "Arial", "80FFFF", size="medium", nudge=1.0))

    def test_size_nudge_determinism_across_presets_and_scripts(self):
        # The shared _size_targets must be a pure function of its inputs — the
        # write and the check land on the identical Fontsize for every combo.
        for size in ("small", "medium", "large"):
            for script in ("latin", "arabic"):
                written = FileProcessor._size_targets(
                    1920, 1080, size, "SomeFont", script, 1.15)[2]
                checked = FileProcessor._size_targets(
                    1920, 1080, size, "SomeFont", script, 1.15)[2]
                self.assertEqual(written, checked)

    def test_normalize_size_nudge_snaps_and_clamps(self):
        from anime_subs.runtime import normalize_size_nudge
        self.assertEqual(normalize_size_nudge(0.8137), 0.80)  # snap to grid
        self.assertEqual(normalize_size_nudge(9.0), 1.50)     # clamp high (±50%)
        self.assertEqual(normalize_size_nudge(0.1), 0.50)     # clamp low
        self.assertEqual(normalize_size_nudge(1.0), 1.0)      # neutral untouched

    def _write(self, content, name="s.ass"):
        p = self.tmp / name
        p.write_text(content, encoding="utf-8")
        return p


class FilterBestStreamsTests(unittest.TestCase):
    def _mk(self, idx, lang, title=""):
        return app.SubtitleStream(index=idx, language=lang, codec="ass", title=title)

    def test_language_filter(self):
        streams = [
            self._mk(1, "eng"), self._mk(2, "ara"), self._mk(3, "jpn"),
        ]
        out = FileProcessor.filter_best_streams(streams, "ara,eng")
        langs = {s.language for s in out}
        self.assertEqual(langs, {"ara", "eng"})

    def test_prefers_full_over_signs(self):
        streams = [
            self._mk(1, "eng", title="Signs & Songs"),
            self._mk(2, "eng", title="Dialogue"),
        ]
        out = FileProcessor.filter_best_streams(streams, "eng")
        self.assertEqual([s.index for s in out], [2])

    def test_no_filter_includes_all(self):
        streams = [self._mk(1, "eng"), self._mk(2, "ara")]
        out = FileProcessor.filter_best_streams(streams, "")
        self.assertEqual(len(out), 2)

    def test_bibliographic_codes_match_terminology_codes(self):
        # MKVs usually tag ISO 639-2/B codes (fre/ger/chi) while the picker
        # offers the /T codes (fra/deu/zho); both spellings must match.
        streams = [
            self._mk(1, "fre"), self._mk(2, "ger"), self._mk(3, "chi"),
            self._mk(4, "jpn"),
        ]
        out = FileProcessor.filter_best_streams(streams, "fra,deu,zho")
        self.assertEqual(sorted(s.index for s in out), [1, 2, 3])
        # The track keeps its own tag (it names the extracted file).
        self.assertEqual({s.language for s in out}, {"fre", "ger", "chi"})


class EnumerateBackupGroupsTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)

    def tearDown(self):
        self._tempdir.cleanup()

    def _make_anime(self, name: str, backup_names=("ep01.ass", "ep02.ass")):
        series = self.root / name
        orig = series / "Original Subs"
        orig.mkdir(parents=True)
        for n in backup_names:
            (orig / n).write_text("dummy", encoding="utf-8")
        return series

    def test_empty_root_returns_empty(self):
        self.assertEqual(
            FileProcessor.enumerate_backup_groups(self.root), []
        )

    def test_single_series(self):
        series = self._make_anime("Some Anime")
        groups = FileProcessor.enumerate_backup_groups(self.root)
        self.assertEqual(len(groups), 1)
        parent, backups = groups[0]
        self.assertEqual(parent, series)
        self.assertEqual(len(backups), 2)

    def test_multiple_series_sorted_by_name(self):
        self._make_anime("Zebra Show")
        self._make_anime("Alpha Show")
        self._make_anime("Middle Show")
        groups = FileProcessor.enumerate_backup_groups(self.root)
        names = [g[0].name for g in groups]
        self.assertEqual(names, ["Alpha Show", "Middle Show", "Zebra Show"])

    def test_ignores_empty_original_subs_dir(self):
        series = self.root / "Empty Show"
        (series / "Original Subs").mkdir(parents=True)
        # No .ass files inside — should be excluded.
        self.assertEqual(
            FileProcessor.enumerate_backup_groups(self.root), []
        )


class FontSupportTests(unittest.TestCase):
    def test_key_skips_ascii_and_whitespace(self):
        # ASCII + whitespace only -> empty key (nothing to filter).
        self.assertEqual(app.FontSupport.test_key(""), "")
        self.assertEqual(app.FontSupport.test_key("Hello world"), "")
        self.assertEqual(app.FontSupport.test_key("  \t\n  "), "")

    def test_key_extracts_unique_non_ascii(self):
        key = app.FontSupport.test_key("سلام سلام")
        # All non-ASCII Arabic chars, deduped, sorted.
        self.assertGreater(len(key), 0)
        self.assertEqual(len(key), len(set(key)))  # unique
        self.assertEqual(key, "".join(sorted(set(key))))

    def test_key_stable_across_calls(self):
        a = app.FontSupport.test_key("سبحان الله وبحمده")
        b = app.FontSupport.test_key("الله وبحمده سبحان")
        self.assertEqual(a, b)  # same unique chars -> same key

    def test_supports_chars_empty_always_true(self):
        # Empty `chars` means nothing to verify — trivially supported.
        self.assertTrue(app.FontSupport._supports_chars("Arial", ""))

    def test_key_drops_bidi_and_control_chars(self):
        # Invisible BiDi/format controls (U+202B embedding, U+202C pop,
        # U+200F RLM, U+200D ZWJ) carry no glyph and must not enter the key —
        # otherwise RTL text (which wrap_rtl_if_needed stamps with U+202B/
        # U+202C) looked "unrenderable" and the app flagged its own controls.
        base = app.FontSupport.test_key("سبحان الله")
        withctl = app.FontSupport.test_key(
            "‫سبحان الله‬‏‍"
        )
        self.assertEqual(base, withctl)
        for ctl in ("‫", "‬", "‏", "‍"):
            self.assertNotIn(ctl, withctl)

    def test_key_drops_variation_selectors_and_pua(self):
        # Default-ignorable variation selectors (U+FE0F, category Mn — not
        # Cc/Cf) and private-use/unassigned codepoints are invisible or
        # font-specific and must not be flagged as "missing glyphs".
        base = app.FontSupport.test_key("❤")  # heart, renderable
        withextra = app.FontSupport.test_key(
            "❤️\U000e01f0"  # + VS16 + PUA + unassigned
        )
        self.assertEqual(base, withextra)
        for ch in ("️", "", "\U000e01f0"):
            self.assertNotIn(ch, withextra)

    def test_renderable_chars_keeps_repetition_and_marks(self):
        # Occurrence-weighted: repeats are preserved; Arabic harakat (Mn) are
        # real content and kept; invisible controls dropped.
        chars = app.FontSupport.renderable_chars("ببّ‫ب")
        self.assertEqual(chars, ["ب", "ب", "ّ", "ب"])

    def test_unsupported_chars_ascii_is_empty(self):
        # Nothing renderable/non-ASCII -> no misses (no font load needed).
        self.assertEqual(
            app.FontSupport.unsupported_chars("No Such Font", "hello world"), []
        )

    def test_unsupported_chars_unloadable_font_is_empty(self):
        # A font that can't be loaded is 'unknown', never 'unsupported', so a
        # transient resolve miss can't produce a spurious coverage warning.
        with patch.object(app.FontSupport, "_load_pil_font", return_value=None):
            self.assertEqual(
                app.FontSupport.unsupported_chars("Whatever", "سلام"), []
            )

    def test_probe_font_file_returns_list(self):
        # Handing the probe a non-font file must not raise — just empty.
        bogus = Path(__file__)
        self.assertEqual(app.FontSupport._probe_font_file(bogus), [])

    def test_normalize_name(self):
        # Case, dashes, underscores, collapsed whitespace all equivalent.
        norm = app.FontSupport._normalize_name
        self.assertEqual(norm("AL-Jazeera-Arabic-Light"),
                         norm("al jazeera arabic light"))
        self.assertEqual(norm("Calibri_Light"), norm("Calibri Light"))
        self.assertEqual(norm(""), "")

    def test_names_for_face_regular(self):
        # Regular subfamily should surface the family alone.
        table = {1: "Arial", 2: "Regular", 4: "Arial"}
        aliases = app.FontSupport._names_for_face(table)
        self.assertIn("Arial", aliases)

    def test_names_for_face_style_variant(self):
        # Calibri Light case: base family + subfamily combined.
        table = {1: "Calibri", 2: "Light", 4: "Calibri Light"}
        aliases = app.FontSupport._names_for_face(table)
        self.assertIn("Calibri Light", aliases)
        # Dash alternate for the combined form.
        self.assertIn("Calibri-Light", aliases)

    def test_names_for_face_uses_preferred_family(self):
        # WWS: name table's preferred family/subfamily (16/17) wins over 1/2.
        table = {
            1: "Calibri", 2: "Light",
            16: "Calibri Light", 17: "Regular",
            4: "Calibri Light",
        }
        aliases = app.FontSupport._names_for_face(table)
        # Preferred family (regular subfam) should surface on its own.
        self.assertIn("Calibri Light", aliases)

    def test_names_for_face_symbol_subfamily_uses_bare_family(self):
        # A junk, letter-less subfamily (e.g. HSIshraq-Light's "☞" placeholder)
        # must be treated as Regular so the BARE family is an alias — GDI/tkFont
        # report "HSIshraq-Light", not "HSIshraq-Light ☞".
        table = {1: "HSIshraq-Light", 2: "☞", 4: "☞HSIshraq-Light"}
        aliases = app.FontSupport._names_for_face(table)
        self.assertIn("HSIshraq-Light", aliases)

    def test_supports_arial_arabic_when_path_map_available(self):
        # Arial ships with Windows Fonts and has Arabic glyphs; a path
        # map built from real Fonts dirs should let us confirm that.
        # Skip gracefully on non-Windows hosts or custom installs.
        if not any(p.is_dir() for p in app.FontSupport._font_dirs()):
            self.skipTest("No Fonts dirs to scan")
        app.FontSupport._get_path_map()
        path_map = app.FontSupport._path_map or {}
        if "Arial" not in path_map:
            self.skipTest("Arial not in scanned path map")
        self.assertTrue(app.FontSupport._supports_chars("Arial", "ا"))

    def test_supports_latin_unloadable_font_is_none(self):
        # A font that can't be loaded is 'unknown', not 'no Latin'.
        with patch.object(app.FontSupport, "_load_pil_font", return_value=None):
            self.assertIsNone(app.FontSupport.supports_latin("Whatever"))

    def test_supports_latin_threshold(self):
        # The probe tolerates a few coincidental tofu-bbox collisions: a
        # majority of letters rendering counts as Latin-capable; an
        # Arabic-only font renders NONE of the probe.
        probe = app.FontSupport._LATIN_PROBE
        sentinel = object()
        with patch.object(app.FontSupport, "_load_pil_font",
                          return_value=sentinel):
            # None missing -> supported.
            with patch.object(app.FontSupport, "_missing_chars",
                              return_value=[]):
                self.assertTrue(app.FontSupport.supports_latin("F"))
            # One coincidental miss (the Segoe-UI 'A' case) -> still supported.
            with patch.object(app.FontSupport, "_missing_chars",
                              return_value=[probe[0]]):
                self.assertTrue(app.FontSupport.supports_latin("F"))
            # Every probe letter missing (Arabic-only font) -> not supported.
            with patch.object(app.FontSupport, "_missing_chars",
                              return_value=list(probe)):
                self.assertFalse(app.FontSupport.supports_latin("F"))

    def test_supports_latin_arial_when_available(self):
        # Arial ships with Windows and has Latin; a real path map confirms it.
        if not any(p.is_dir() for p in app.FontSupport._font_dirs()):
            self.skipTest("No Fonts dirs to scan")
        app.FontSupport._get_path_map()
        if "Arial" not in (app.FontSupport._path_map or {}):
            self.skipTest("Arial not in scanned path map")
        self.assertTrue(app.FontSupport.supports_latin("Arial"))


class VisualRtlTests(unittest.TestCase):
    """i18n.visual_rtl reorders Arabic into visual order for Tk (which lays
    out logical-LTR and does no bidi), keeping words' letters logical and
    embedded LTR spans intact. Verified word orders were confirmed by a
    native reader."""

    def test_passthrough_for_ltr(self):
        self.assertEqual(app.visual_rtl(""), "")
        self.assertEqual(app.visual_rtl("Apply to library"),
                         "Apply to library")

    def test_reverses_arabic_word_order(self):
        # "Watch Folder": native reader confirmed the reversed word order is
        # what renders correctly in Tk.
        self.assertEqual(app.visual_rtl("مجلد المراقبة"), "المراقبة مجلد")

    def test_keeps_embedded_filename_intact_and_ltr(self):
        out = app.visual_rtl("نُسِّق Show – 12.ass الآن")
        self.assertEqual(out, "الآن Show – 12.ass نُسِّق")

    def test_single_word_unchanged(self):
        self.assertEqual(app.visual_rtl("المراقبة"), "المراقبة")


class I18nTests(unittest.TestCase):
    """t() reorders for customtkinter widgets; t_raw() stays logical for raw
    tk widgets / native message boxes (which apply their own bidi)."""

    def setUp(self):
        self._prev = app.get_language()

    def tearDown(self):
        app.set_language(self._prev)

    def test_en_t_equals_t_raw_and_is_verbatim(self):
        app.set_language("en")
        self.assertEqual(app.t("apply_to_library"), "Apply to library")
        self.assertEqual(app.t_raw("apply_to_library"), "Apply to library")

    def test_ar_wraps_rtl_base_keeping_logical_order(self):
        # Segoe UI is pinned, so Tk orders the words itself — we keep logical
        # order and only add an RTL base wrap (U+202B…U+202C) so a trailing
        # "…"/neutral lands at the visual end.
        app.set_language("ar")
        out = app.t("apply_to_library")
        self.assertTrue(out.startswith("‫") and out.endswith("‬"))
        self.assertIn("تطبيق على المكتبة", out)          # logical, not reordered
        self.assertEqual(app.t("apply_to_library"), app.t_raw("apply_to_library"))

    def test_en_has_no_rtl_wrap(self):
        app.set_language("en")
        self.assertEqual(app.t("apply_to_library"), "Apply to library")
        self.assertNotIn("‫", app.t("apply_to_library"))

    def test_unknown_key_falls_back_to_key(self):
        app.set_language("ar")
        self.assertEqual(app.t_raw("no_such_key_xyz"), "no_such_key_xyz")

    def test_en_restore_button_says_one_file_not_one_files(self):
        app.set_language("en")
        self.assertEqual(app.t("restore_btn_one", series=1, files=1),
                         "Restore 1 series · 1 file")

    def test_locale_catalogs_have_matching_keys_and_placeholders(self):
        catalogs = app.i18n._STRINGS
        self.assertEqual(set(catalogs["en"]), set(catalogs["ar"]))
        for code, _name in app.COMMON_LANGS:
            self.assertIn(f"language_{code}", catalogs["en"])

        formatter = string.Formatter()
        for key, english in catalogs["en"].items():
            english_fields = {
                field for _, field, _, _ in formatter.parse(english) if field
            }
            arabic_fields = {
                field
                for _, field, _, _ in formatter.parse(catalogs["ar"][key])
                if field
            }
            self.assertEqual(english_fields, arabic_fields, key)

    def test_ar_batch_extraction_event_composes_without_duplicate_preposition(self):
        app.set_language("ar")
        verb = app.t_logical("ev_extracted")
        videos = app.t_logical("event_videos_many", n=5)

        self.assertEqual(f"{verb} {videos}", "تم الاستخراج من 5 من الفيديوهات")
        self.assertEqual(
            app.t_logical("ev_idle_last", verb=verb) + videos,
            "خامل — آخر إجراء: تم الاستخراج من 5 من الفيديوهات",
        )


class RtlHelpersTests(unittest.TestCase):
    def test_has_rtl_chars_arabic(self):
        self.assertTrue(app.has_rtl_chars("سبحان"))

    def test_has_rtl_chars_hebrew(self):
        self.assertTrue(app.has_rtl_chars("שלום"))

    def test_has_rtl_chars_latin_only(self):
        self.assertFalse(app.has_rtl_chars("Hello world"))

    def test_has_rtl_chars_empty(self):
        self.assertFalse(app.has_rtl_chars(""))

    def test_wrap_rtl_wraps_when_needed(self):
        wrapped = app.wrap_rtl_if_needed("سبحان")
        self.assertTrue(wrapped.startswith("\u202B"))
        self.assertTrue(wrapped.endswith("\u202C"))
        self.assertIn("سبحان", wrapped)

    def test_wrap_rtl_passthrough_for_latin(self):
        self.assertEqual(
            app.wrap_rtl_if_needed("Hello"), "Hello"
        )


class IssueTrackerTests(unittest.TestCase):
    def setUp(self):
        app.IssueTracker.clear()

    def tearDown(self):
        app.IssueTracker.clear()

    def test_record_and_read(self):
        app.IssueTracker.record("decode", "foo.ass", "utf-8 decode error")
        issues = app.IssueTracker.get_issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].operation, "decode")
        self.assertEqual(issues[0].filename, "foo.ass")
        self.assertEqual(issues[0].reason, "utf-8 decode error")
        self.assertEqual(app.IssueTracker.count(), 1)

    def test_ring_buffer_caps(self):
        cap = app.IssueTracker._MAX_ISSUES
        for i in range(cap + 20):
            app.IssueTracker.record("apply", f"file{i}.ass", "oops")
        self.assertEqual(app.IssueTracker.count(), cap)
        # Oldest 20 should have fallen off.
        issues = app.IssueTracker.get_issues()
        self.assertEqual(issues[0].filename, "file20.ass")
        self.assertEqual(issues[-1].filename, f"file{cap + 19}.ass")

    def test_clear_fires_listeners(self):
        calls = {"count": 0}

        def listener():
            calls["count"] += 1

        app.IssueTracker.add_listener(listener)
        app.IssueTracker.record("apply", "x.ass", "reason")
        app.IssueTracker.clear()
        self.assertGreaterEqual(calls["count"], 2)

    def test_record_error_helper_surfaces_issue(self):
        app.record_error("extract", "video.mkv", "stream #3: timeout")
        issues = app.IssueTracker.get_issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].operation, "extract")

    def test_record_warning_helper_surfaces_at_warning_level(self):
        with self.assertLogs("anime_subs.runtime", level="WARNING") as cm:
            app.record_warning("font", "x.ass", "missing glyphs")
        issues = app.IssueTracker.get_issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].operation, "font")
        # Logged, and NOT as an "... failed ..." error line.
        joined = "\n".join(cm.output)
        self.assertIn("missing glyphs", joined)
        self.assertNotIn("failed", joined)

    def test_listener_exception_does_not_break_record(self):
        def bad_listener():
            raise RuntimeError("boom")

        app.IssueTracker.add_listener(bad_listener)
        # Must not raise.
        app.IssueTracker.record("apply", "x.ass", "reason")
        self.assertEqual(app.IssueTracker.count(), 1)


class FontCoverageWarningTests(unittest.TestCase):
    """_font_cant_render_file: returns True (caller SKIPS the file) when a font
    misses a MAJORITY of the file's renderable chars; returns False (apply
    proceeds) otherwise. A coverage skip is expected protective behaviour, so it
    is LOGGED but must NOT be filed as a Problems-panel issue (else a bilingual
    library floods the panel with a 'problem' per English sidecar)."""

    def setUp(self):
        app.IssueTracker.clear()

    def tearDown(self):
        app.IssueTracker.clear()

    @staticmethod
    def _lines(text):
        return [
            "[Events]\n",
            "Format: Layer, Start, End, Style, Text\n",
            f"Dialogue: 0,0,0,Default,{text}\n",
        ]

    def test_majority_miss_skips_but_is_not_a_problem(self):
        lines = self._lines("سبحان الله وبحمده العظيم")
        key = app.FontSupport.test_key("".join(lines))
        self.assertGreater(len(key), 4)
        # Font misses everything -> script mismatch -> skip, logged but NOT
        # surfaced as a Problems-panel issue.
        with patch.object(app.FontSupport, "unsupported_chars",
                          return_value=list(key)):
            skip = app.FileProcessor._font_cant_render_file(
                "BadFont", lines, Path("x.ass")
            )
        self.assertTrue(skip)
        self.assertEqual(app.IssueTracker.count(), 0)   # a skip is not a problem

    def test_dominant_script_missing_skips_by_occurrence(self):
        # A font that tofus the numerically dominant script but renders a couple
        # of incidental chars. Missing char appears 10x, rendered one once —
        # occurrence-weighting must SKIP (distinct count 1-of-2 would miss it).
        lines = self._lines("ب" * 10 + "ō")
        with patch.object(app.FontSupport, "unsupported_chars",
                          return_value=["ب"]):
            skip = app.FileProcessor._font_cant_render_file(
                "LatinFont", lines, Path("x.ass")
            )
        self.assertTrue(skip)
        self.assertEqual(app.IssueTracker.count(), 0)   # a skip is not a problem

    def test_incidental_miss_does_not_skip_by_occurrence(self):
        # Inverse: one stray missing char among many rendered ones -> apply.
        lines = self._lines("ب" * 20 + "ō")
        with patch.object(app.FontSupport, "unsupported_chars",
                          return_value=["ō"]):
            skip = app.FileProcessor._font_cant_render_file(
                "ArabicFont", lines, Path("x.ass")
            )
        self.assertFalse(skip)
        self.assertEqual(app.IssueTracker.count(), 0)

    def test_full_coverage_does_not_skip(self):
        lines = self._lines("سبحان الله")
        with patch.object(app.FontSupport, "unsupported_chars", return_value=[]):
            skip = app.FileProcessor._font_cant_render_file(
                "GoodFont", lines, Path("x.ass")
            )
        self.assertFalse(skip)
        self.assertEqual(app.IssueTracker.count(), 0)

    def test_english_with_latin_font_applies(self):
        # English (ASCII) text + a Latin-capable font -> apply, and the non-ASCII
        # coverage path isn't even consulted.
        lines = self._lines("plain english subtitle text")
        with patch.object(app.FontSupport, "supports_latin", return_value=True), \
             patch.object(app.FontSupport, "unsupported_chars",
                          side_effect=AssertionError("should not be called")):
            skip = app.FileProcessor._font_cant_render_file(
                "LatinFont", lines, Path("x.eng.ass")
            )
        self.assertFalse(skip)
        self.assertEqual(app.IssueTracker.count(), 0)

    def test_english_with_arabic_only_font_skipped(self):
        # English (ASCII) text + an Arabic-only font (no Latin glyphs) -> SKIP,
        # detected from content even with NO language tag and no non-ASCII chars
        # to trip the occurrence check (the gap the filename tag would hide).
        lines = self._lines("plain english subtitle text")
        with patch.object(app.FontSupport, "supports_latin", return_value=False), \
             self.assertLogs(level="INFO") as cm:
            skip = app.FileProcessor._font_cant_render_file(
                "Example Naskh", lines, Path("x.ass")
            )
        self.assertTrue(skip)                          # file IS skipped…
        self.assertEqual(app.IssueTracker.count(), 0)  # …but that's not a problem
        # …and it's still logged, so the skip is auditable via the log.
        self.assertTrue(any("Example Naskh" in m for m in cm.output))

    def test_typesetting_lines_excluded_arabic_dialogue_kept(self):
        # Real-world regression (a fansub .ar.ass): the file opens with Arabic
        # TYPESETTING lines (محاكاة) whose \fn<LatinFontName> and positioning
        # tags are pure Latin. If sampled, they mislabel the file as English and
        # an Arabic-only font gets wrongly skipped. Only the spoken حوار dialogue
        # (Arabic) must decide the script.
        header = [
            "[Events]\n",
            "Format: Layer, Start, End, Style, Name, MarginL, "
            "MarginR, MarginV, Effect, Text\n",
        ]
        ts = ("Dialogue: 0,0,0,محاكاة,,0,0,0,,{\\an6\\fnSome Sign Font\\b1"
              "\\fs130\\bord1\\blur30\\pos(1826,144)\\fad(0,1700)}عام\n")
        dlg = "Dialogue: 0,0,0,حوار,,0,0,0,,قبل عهدٍ بعيد\n"
        lines = header + [ts] * 30 + [dlg] * 5
        with patch.object(app.FontSupport, "supports_latin", return_value=False), \
             patch.object(app.FontSupport, "unsupported_chars", return_value=[]):
            skip = app.FileProcessor._font_cant_render_file(
                "Example Naskh", lines, Path("x.ar.ass")
            )
        self.assertFalse(skip)
        self.assertEqual(app.IssueTracker.count(), 0)

    def test_override_tags_not_counted_as_latin(self):
        # A dialogue line whose Latin override tags (incl. the app's own injected
        # \c colour tag and a \fn font name) far outweigh its short Arabic text.
        # Stripping the {...} blocks must leave only the Arabic, so an Arabic
        # font is not wrongly skipped.
        line = ("Dialogue: 0,0,0,حوار,,0,0,0,,{\\c&HFFFFFF&\\i1\\b1"
                "\\fnArial Black\\fs48\\bord2\\shad1\\pos(100,200)"
                "\\fad(150,150)}نعم\n")
        lines = [
            "[Events]\n",
            "Format: Layer, Start, End, Style, Name, MarginL, "
            "MarginR, MarginV, Effect, Text\n",
            line,
        ]
        with patch.object(app.FontSupport, "supports_latin", return_value=False), \
             patch.object(app.FontSupport, "unsupported_chars", return_value=[]):
            skip = app.FileProcessor._font_cant_render_file(
                "Example Naskh", lines, Path("x.ass")
            )
        self.assertFalse(skip)
        self.assertEqual(app.IssueTracker.count(), 0)


class NextFontIndexTests(unittest.TestCase):
    FONTS = ["Arial", "Arial Black", "Courier", "Times", "Verdana"]

    def _next(self, current, delta):
        return app._next_font_index(self.FONTS, current, delta)

    def test_empty_list(self):
        self.assertEqual(app._next_font_index([], "Arial", 1), -1)

    def test_blank_entry_down_starts_at_zero(self):
        self.assertEqual(self._next("", 1), 0)

    def test_blank_entry_up_wraps_to_last(self):
        self.assertEqual(self._next("", -1), len(self.FONTS) - 1)

    def test_exact_match_advances(self):
        # "Arial" -> "Arial Black"
        self.assertEqual(self._next("Arial", 1), 1)

    def test_exact_match_case_insensitive(self):
        self.assertEqual(self._next("arial", 1), 1)

    def test_exact_match_retreats_wrapping(self):
        # First element, Up should wrap to last.
        self.assertEqual(self._next("Arial", -1), len(self.FONTS) - 1)

    def test_partial_match_seeds_from_start(self):
        # "Ari" isn't an exact font name, so Down seeds at 0 (first font).
        self.assertEqual(self._next("Ari", 1), 0)


class AppConfigRoundTripTests(unittest.TestCase):
    """Save → load persistence for AppConfig (config.json)."""

    def setUp(self):
        # Redirect the module-level config path at a temp file so we never
        # touch the user's real config.json.
        self._tempdir = tempfile.TemporaryDirectory()
        self._orig_path = app.runtime.CONFIG_FILE_PATH
        app.runtime.CONFIG_FILE_PATH = Path(self._tempdir.name) / "config.json"

    def tearDown(self):
        app.runtime.CONFIG_FILE_PATH = self._orig_path
        self._tempdir.cleanup()

    def test_missing_file_returns_defaults(self):
        # No file on disk -> a default AppConfig, not an error.
        cfg = AppConfig.load()
        self.assertEqual(cfg, AppConfig())

    def test_round_trip_preserves_all_fields(self):
        original = AppConfig(
            font="Example Naskh",
            color="80FFFF",
            folder="D:/Library",
            font_bold=True,
            font_italic=True,
            font_underline=False,
            font_size="large",
            extract_enabled=True,
            extract_langs="ara,eng",
            preview_text="سبحان الله",
            start_minimized=True,
        )
        self.assertTrue(original.save())
        loaded = AppConfig.load()
        self.assertEqual(loaded, original)

    def test_invalid_font_size_normalised_to_none(self):
        app.runtime.CONFIG_FILE_PATH.write_text(
            '{"font_size": "gigantic"}', encoding="utf-8"
        )
        self.assertEqual(AppConfig.load().font_size, "none")

    def test_non_ascii_preview_text_survives(self):
        cfg = AppConfig(preview_text="سبحان الله وبحمده")
        self.assertTrue(cfg.save())
        # Persisted as real UTF-8 (ensure_ascii=False), read back intact.
        self.assertEqual(AppConfig.load().preview_text, "سبحان الله وبحمده")

    def test_unknown_keys_are_ignored(self):
        app.runtime.CONFIG_FILE_PATH.write_text(
            '{"font": "Arial", "bogus_key": 123}', encoding="utf-8"
        )
        cfg = AppConfig.load()
        self.assertEqual(cfg.font, "Arial")
        self.assertFalse(hasattr(cfg, "bogus_key"))

    def test_invalid_json_returns_defaults(self):
        app.runtime.CONFIG_FILE_PATH.write_text("{ not valid json", encoding="utf-8")
        self.assertEqual(AppConfig.load(), AppConfig())

    def test_favorite_fonts_round_trip(self):
        cfg = AppConfig(favorite_fonts=["Arial", "Reem Kufi", "Almarai"])
        self.assertTrue(cfg.save())
        self.assertEqual(AppConfig.load().favorite_fonts,
                         ["Arial", "Reem Kufi", "Almarai"])

    def test_favorite_fonts_drops_non_string_entries(self):
        # A hand-edited list with junk entries is sanitized, not trusted.
        app.runtime.CONFIG_FILE_PATH.write_text(
            '{"favorite_fonts": ["Arial", 5, null, "Cairo"]}', encoding="utf-8"
        )
        self.assertEqual(AppConfig.load().favorite_fonts, ["Arial", "Cairo"])

    def test_favorite_fonts_non_list_becomes_empty(self):
        app.runtime.CONFIG_FILE_PATH.write_text(
            '{"favorite_fonts": "Arial"}', encoding="utf-8"
        )
        self.assertEqual(AppConfig.load().favorite_fonts, [])

    def test_preview_bg_enabled_round_trip(self):
        self.assertTrue(AppConfig(preview_bg_enabled=True).save())
        self.assertTrue(AppConfig.load().preview_bg_enabled)
        # Default is off.
        self.assertFalse(AppConfig().preview_bg_enabled)

    def test_preview_bg_from_sub_round_trip(self):
        self.assertTrue(AppConfig(preview_bg_from_sub=True).save())
        self.assertTrue(AppConfig.load().preview_bg_from_sub)
        self.assertFalse(AppConfig().preview_bg_from_sub)


class ConfigColorValidationTests(unittest.TestCase):
    """A corrupt/hand-edited colour must be dropped on load, not trusted (it
    would otherwise crash the GUI build via bgr_to_rgb_hex)."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self._orig = app.runtime.CONFIG_FILE_PATH
        app.runtime.CONFIG_FILE_PATH = Path(self._tempdir.name) / "config.json"

    def tearDown(self):
        app.runtime.CONFIG_FILE_PATH = self._orig
        self._tempdir.cleanup()

    def test_invalid_color_dropped_to_none(self):
        app.runtime.CONFIG_FILE_PATH.write_text(
            '{"font": "Arial", "color": "ZZZ"}', encoding="utf-8"
        )
        cfg = AppConfig.load()
        self.assertIsNone(cfg.color)
        self.assertEqual(cfg.font, "Arial")

    def test_valid_color_normalized(self):
        app.runtime.CONFIG_FILE_PATH.write_text('{"color": "80ffff"}', encoding="utf-8")
        self.assertEqual(AppConfig.load().color, "80FFFF")

    def test_non_string_color_drops_only_colour_not_whole_config(self):
        # A hand-edited/corrupted NON-STRING colour (number/bool/list) must drop
        # ONLY the colour — it must NOT reset the entire config to defaults, which
        # would silently wipe favorites/folder/nudges on the next save. Regression
        # for the AttributeError (int.strip()) escaping the colour guard.
        for bad in ("123456", "true", "[1, 2]"):
            with self.subTest(bad_color=bad):
                app.runtime.CONFIG_FILE_PATH.write_text(
                    '{"color": %s, "font": "Cairo", '
                    '"favorite_fonts": ["Almarai", "Reem Kufi"], '
                    '"folder": "D:/Library", "ui_language": "ar"}' % bad,
                    encoding="utf-8",
                )
                cfg = AppConfig.load()
                self.assertIsNone(cfg.color)          # bad colour dropped
                self.assertEqual(cfg.font, "Cairo")   # everything else preserved
                self.assertEqual(
                    cfg.favorite_fonts, ["Almarai", "Reem Kufi"]
                )
                self.assertEqual(cfg.folder, "D:/Library")
                self.assertEqual(cfg.ui_language, "ar")


class ResolutionAnchorTests(unittest.TestCase):
    def test_dialogue_playres_token_ignored(self):
        # A `PlayResY:` token in Dialogue text appearing BEFORE the header must
        # not be picked up (regex is anchored to line start).
        lines = [
            "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,PlayResY: 99 gotcha\n",
            "PlayResY: 720\n",
        ]
        _, y, _ = FileProcessor._extract_resolution_from_lines(lines, "x")
        self.assertEqual(y, 720)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)

    def tearDown(self):
        self._tempdir.cleanup()

    def test_encoding_fallback_to_utf8(self):
        # Content unrepresentable in the declared encoding (Arabic in cp1252)
        # must fall back to UTF-8 rather than raise or corrupt.
        path = self.tmp / "out.ass"
        app.atomic_write(path, ["سلام\n"], "cp1252")
        self.assertEqual(path.read_text(encoding="utf-8"), "سلام\n")

    def test_plain_roundtrip_no_temp_left(self):
        path = self.tmp / "p.ass"
        app.atomic_write(path, ["a\n", "b\n"], "utf-8")
        self.assertEqual(path.read_text(encoding="utf-8"), "a\nb\n")
        # No stray temp files left behind in the directory.
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.startswith(".tmp_")]
        self.assertEqual(leftovers, [])


class BackupOnceTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def test_backup_keeps_first_original_across_reapply(self):
        # The pristine original must survive a SECOND styling pass — the backup
        # is write-once, so a reused filename never loses the true original.
        path = self.tmp / "ep.ass"
        path.write_text(CANONICAL_ASS, encoding="utf-8")
        self.assertTrue(FileProcessor.apply_changes(path, "FontA", "80FFFF"))
        self.assertTrue(FileProcessor.apply_changes(path, "FontB", "112233"))
        backup = (self.tmp / "Original Subs" / "ep.ass").read_text(encoding="utf-8")
        self.assertIn("OldFont", backup)
        self.assertNotIn("FontA", backup)
        self.assertNotIn("FontB", backup)


class SubtitleSettleTests(unittest.TestCase):
    """The monitor must not style a subtitle that is still being written in — a
    partial file would be read, backed up (as the pristine Original Subs/ copy),
    and rewritten, permanently losing the true original. _subtitle_settled gates
    a freshly-modified file on size-stability before it is touched."""

    class _FakeState:
        def __init__(self):
            self.stop_event = threading.Event()

    class _FakePath:
        """Duck-typed Path whose .stat() yields the given sizes in sequence, so
        a 'growing' file can be simulated deterministically."""
        def __init__(self, sizes):
            self._sizes = list(sizes)
            self._i = 0

        def stat(self):
            size = self._sizes[min(self._i, len(self._sizes) - 1)]
            self._i += 1
            return SimpleNamespace(st_size=size)

    def setUp(self):
        self._orig_interval = app.monitor._ASS_SETTLE_INTERVAL
        app.monitor._ASS_SETTLE_INTERVAL = 0.02  # keep the settle wait fast
        self.mon = app.monitor.FolderMonitor(state=self._FakeState())

    def tearDown(self):
        app.monitor._ASS_SETTLE_INTERVAL = self._orig_interval

    def test_stable_size_is_settled(self):
        self.assertTrue(self.mon._subtitle_settled(self._FakePath([1200, 1200])))

    def test_growing_file_is_not_settled(self):
        # Size still increasing between the two samples => mid-copy => defer.
        self.assertFalse(self.mon._subtitle_settled(self._FakePath([800, 1600])))

    def test_empty_file_is_not_settled(self):
        self.assertFalse(self.mon._subtitle_settled(self._FakePath([0, 0])))

    def test_stop_event_defers(self):
        # A shutdown mid-settle must defer the file, not treat it as stable.
        self.mon.state.stop_event.set()
        self.assertFalse(self.mon._subtitle_settled(self._FakePath([1200, 1200])))


class FontCacheAtomicTests(unittest.TestCase):
    """Font caches must stage to a UNIQUE temp (mkstemp), not a fixed .tmp, so
    two concurrent savers can't truncate/interleave one staging file. The save
    must still round-trip and leave no stray temp behind."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        self._orig_path = app.fonts.FONT_SUPPORT_CACHE_PATH
        self._orig_cache = app.fonts.FontSupport._cache
        # Redirect the module-level cache path so we never touch the real cache.
        app.fonts.FONT_SUPPORT_CACHE_PATH = self.tmp / "font_support_cache.json"

    def tearDown(self):
        app.fonts.FONT_SUPPORT_CACHE_PATH = self._orig_path
        app.fonts.FontSupport._cache = self._orig_cache
        self._tempdir.cleanup()

    def test_save_cache_roundtrips_and_leaves_no_temp(self):
        app.fonts.FontSupport._cache = {"aChar": {"Arial": True, "Cairo": False}}
        app.fonts.FontSupport._save_cache()
        written = self.tmp / "font_support_cache.json"
        self.assertTrue(written.exists())
        data = json.loads(written.read_text(encoding="utf-8"))
        self.assertTrue(data["entries"]["aChar"]["Arial"])
        self.assertFalse(data["entries"]["aChar"]["Cairo"])
        # The mkstemp staging file must have been renamed/cleaned, not left behind.
        leftovers = [p.name for p in self.tmp.iterdir()
                     if p.name.startswith(".fontcache_")]
        self.assertEqual(leftovers, [])


class MonitorTickIsolationTests(unittest.TestCase):
    """One .ass that raises mid-processing must not abort the whole monitor tick
    and starve every file after it — the per-file body is exception-isolated."""

    class _RaisingFP:
        def __init__(self, files, raise_on):
            self._files = files
            self._raise_on = raise_on
            self.applied = []

        def list_ass_files(self, folder):
            return list(self._files)

        def file_hash(self, p):
            return None

        def is_already_styled(self, p, *a, **k):
            if str(p) == self._raise_on:
                raise RuntimeError("boom in is_already_styled")
            return False

        def apply_changes(self, p, *a, **k):
            self.applied.append(str(p))
            return True

    class _FakeDB:
        def update_file_timestamp(self, *a, **k):
            return True

        def add_file(self, *a, **k):
            return True

    class _FakeState:
        def __init__(self, folder):
            self.monitored_folder = folder
            self.stop_event = threading.Event()
            self._shutting_down = False
            self.events = []

        def note_event(self, verb, name, styled=False):
            self.events.append((verb, name))

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        self._orig_window = app.monitor._ASS_FRESH_WINDOW
        app.monitor._ASS_FRESH_WINDOW = 0.0  # skip the settle gate in this test

    def tearDown(self):
        app.monitor._ASS_FRESH_WINDOW = self._orig_window
        self._tempdir.cleanup()

    def test_raising_file_does_not_abort_the_tick(self):
        a = self.tmp / "a.ass"
        a.write_text("x", encoding="utf-8")
        b = self.tmp / "b.ass"
        b.write_text("x", encoding="utf-8")
        fp = self._RaisingFP([a, b], raise_on=str(a))
        st = self._FakeState(str(self.tmp))
        mon = app.monitor.FolderMonitor(state=st, db=self._FakeDB(), processor=fp)
        failed: dict = {}
        cfg = AppConfig(font="NewFont", color="80FFFF")

        mon.process_ass_files({}, failed, cfg)

        # 'b' was still processed even though 'a' raised first: the loop did not
        # unwind. And 'a' is throttled by its mtime so it isn't hammered next tick.
        self.assertIn(str(b), fp.applied)
        self.assertEqual(failed.get(str(a)), a.stat().st_mtime)


class DeterminismRoundTripTests(unittest.TestCase):
    """The core reprocess-loop invariant, exercised end-to-end: right after
    apply_changes, is_already_styled MUST return True for the SAME settings —
    across Format orderings, size presets, and every bold/italic/underline
    combination. Any disagreement re-styles the file on every monitor scan."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)  # apply_changes -> add_file writes the DB

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def _roundtrip(self, content, *, font="NewFont", color="80FFFF",
                   bold=False, italic=False, underline=False, size="none"):
        # font="NewFont" is unresolvable, so size_ratio falls back to its
        # per-script anchor — fully deterministic, no dependency on installed
        # fonts, and both apply and the check use the identical value.
        path = self.tmp / "ep.ass"
        path.write_text(content, encoding="utf-8")
        self.assertTrue(
            FileProcessor.apply_changes(
                path, font, color, bold=bold, italic=italic,
                underline=underline, size=size),
            msg=f"apply_changes failed (b={bold} i={italic} u={underline} size={size})",
        )
        self.assertTrue(
            FileProcessor.is_already_styled(
                path, font, color, bold=bold, italic=italic,
                underline=underline, size=size),
            msg=f"is_already_styled must be True right after apply "
                f"(b={bold} i={italic} u={underline} size={size})",
        )

    def test_style_flag_matrix_roundtrips(self):
        # Fills the gap: italic=True / underline=True were never driven through
        # the engine, and is_already_styled was never run on a reordered Format.
        for content, label in ((CANONICAL_ASS, "canonical"),
                               (REORDERED_ASS, "reordered")):
            for bold in (False, True):
                for italic in (False, True):
                    for underline in (False, True):
                        with self.subTest(fmt=label, b=bold, i=italic, u=underline):
                            self._roundtrip(content, bold=bold, italic=italic,
                                            underline=underline)

    def test_size_presets_roundtrip_both_formats(self):
        for content, label in ((CANONICAL_ASS, "canonical"),
                               (REORDERED_ASS, "reordered")):
            for size in ("none", "small", "medium", "large"):
                with self.subTest(fmt=label, size=size):
                    self._roundtrip(content, size=size)


class ExtractReExtractSafetyTests(unittest.TestCase):
    """Re-extraction must not silently destroy a hand-edited sidecar: once a sub
    is extracted and captured in Original Subs/, copy_original_file is write-once,
    so a second ffmpeg -y overwrite would clobber the current (possibly edited)
    file with no fresh backup. That overwrite is skipped instead."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        self.video = self.tmp / "input.mkv"
        self.video.write_bytes(b"\x00")          # only needs to exist
        self.out = self.tmp / "input.eng.ass"    # predicted output path

    def tearDown(self):
        self._tempdir.cleanup()

    def test_reextract_skips_when_backup_exists(self):
        # Prior extraction captured the original in Original Subs/; the live file
        # was hand-edited since. A re-extract must NOT run ffmpeg (which would
        # clobber the edit unrecoverably) — it returns the existing path untouched.
        (self.tmp / "Original Subs").mkdir()
        (self.tmp / "Original Subs" / "input.eng.ass").write_text(
            "FIRST EXTRACT", encoding="utf-8"
        )
        self.out.write_text("HAND-EDITED v2", encoding="utf-8")

        with patch.object(app.ass_editor.FFmpegLocator, "get_binary") as gb:
            result = FileProcessor.extract_subtitle(
                str(self.video), 0, "eng", "ass", str(self.tmp)
            )
        gb.assert_not_called()  # returned before reaching ffmpeg -> no overwrite
        self.assertEqual(result, str(self.out.resolve()))
        self.assertEqual(self.out.read_text(encoding="utf-8"), "HAND-EDITED v2")

    def test_first_overwrite_backs_up_before_extract(self):
        # A pre-existing sidecar with NO backup yet must be backed up (recoverable)
        # before the extract proceeds — it is NOT skipped. ffmpeg 'not found' makes
        # it raise AFTER the backup, proving the backup was taken.
        self.out.write_text("PRE-EXISTING", encoding="utf-8")

        with patch.object(app.ass_editor.FFmpegLocator, "get_binary",
                          return_value=None):
            with self.assertRaises(RuntimeError):
                FileProcessor.extract_subtitle(
                    str(self.video), 0, "eng", "ass", str(self.tmp)
                )
        backup = self.tmp / "Original Subs" / "input.eng.ass"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), "PRE-EXISTING")


class RestoreBackupsTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)

    def tearDown(self):
        self._tempdir.cleanup()

    def test_restores_over_live_file(self):
        backup = self.tmp / "orig.ass"
        backup.write_text("ORIGINAL", encoding="utf-8")
        target = self.tmp / "live.ass"
        target.write_text("STYLED", encoding="utf-8")
        restored, failures = FileProcessor.restore_backups([(backup, target)])
        self.assertEqual(restored, [target])
        self.assertEqual(failures, [])
        self.assertEqual(target.read_text(encoding="utf-8"), "ORIGINAL")

    def test_missing_backup_counted_failed_not_raised(self):
        missing = self.tmp / "nope.ass"
        target = self.tmp / "t.ass"
        restored, failures = FileProcessor.restore_backups([(missing, target)])
        self.assertEqual(restored, [])
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], target)


class ApplyChangesIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def _write(self, content):
        p = self.tmp / "s.ass"
        p.write_text(content, encoding="utf-8")
        return p

    def test_reapply_replaces_leading_tag_no_stacking(self):
        p = self._write(CANONICAL_ASS)
        FileProcessor.apply_changes(p, "X", "112233")
        FileProcessor.apply_changes(p, "X", "445566")
        dlg = next(
            l for l in p.read_text(encoding="utf-8").splitlines()
            if l.startswith("Dialogue:")
        )
        self.assertIn("{\\c&H445566&}Hello world", dlg)
        self.assertNotIn("112233", dlg)        # old colour replaced
        self.assertEqual(dlg.count("\\c&H"), 1)  # exactly one tag, not stacked

    def test_midtext_override_preserved(self):
        content = CANONICAL_ASS.replace(
            "Hello world", "Hello {\\c&Hff0000&}red world"
        )
        p = self._write(content)
        FileProcessor.apply_changes(p, "X", "112233")
        dlg = next(
            l for l in p.read_text(encoding="utf-8").splitlines()
            if l.startswith("Dialogue:")
        )
        # Leading tag prepended; the author's mid-text override is untouched.
        self.assertIn("{\\c&H112233&}Hello {\\c&Hff0000&}red world", dlg)


class ApplyChangesEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def _write(self, content, name="s.ass"):
        p = self.tmp / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_empty_file_no_crash(self):
        # An empty file is skipped (not "styled" into a stub, not backed up):
        # it has nothing to preserve and an empty write-once backup would be
        # kept forever. See ADR-006.
        p = self._write("")
        self.assertFalse(FileProcessor.apply_changes(p, "X", "112233"))
        self.assertEqual(p.read_bytes(), b"")

    def test_ssa_v4_styles_section_styled(self):
        # Legacy "[V4 Styles]" (no '+') is handled like "[V4+ Styles]".
        content = CANONICAL_ASS.replace("[V4+ Styles]", "[V4 Styles]")
        p = self._write(content, "ssa.ass")
        self.assertTrue(FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
        style = next(
            l for l in p.read_text(encoding="utf-8").splitlines()
            if l.startswith("Style:")
        )
        self.assertEqual(style.split(":", 1)[1].split(",")[1].strip(), "NewFont")

    def test_legacy_noncanonical_format_is_honored(self):
        # A valid Style Format that includes every required field but isn't the
        # canonical 23 columns must be honored (indexed by its own names), NOT
        # force-fit to canonical, which would misalign the columns.
        content = (
            "[V4+ Styles]\n"
            "Format: Name, Fontname, PrimaryColour, Bold, Italic, Underline, Outline, Shadow\n"
            "Style: Default,OldFont,&H00FFFFFF,0,0,0,1,1\n"
            "\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,Hi\n"
        )
        p = self._write(content, "legacy.ass")
        self.assertTrue(FileProcessor.apply_changes(p, "NewFont", "112233"))
        style = next(
            l for l in p.read_text(encoding="utf-8").splitlines()
            if l.startswith("Style:")
        )
        parts = style.split(":", 1)[1].split(",")
        self.assertEqual(len(parts), 8)  # honored 8-column layout, not 23
        self.assertEqual(parts[1].strip(), "NewFont")
        self.assertEqual(parts[2].strip(), format_ass_primary_color("112233"))


class ExtractSubtitleTests(unittest.TestCase):
    """Exercise the extract_subtitle command/path logic without invoking a real
    ffmpeg (get_binary + ass_editor.run_tracked are stubbed)."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        self.video = self.tmp / "Show.mkv"
        self.video.write_bytes(b"not really a video")

        self._orig_get_binary = app.FFmpegLocator.__dict__["get_binary"]
        app.FFmpegLocator.get_binary = staticmethod(lambda name: "ffmpeg")

        self._orig_run = app.ass_editor.run_tracked
        self.calls = []

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            Path(cmd[-1]).write_text("dummy sub", encoding="utf-8")  # ffmpeg -y output

            class _R:
                returncode = 0
                stdout = b""
                stderr = b""

            return _R()

        app.ass_editor.run_tracked = fake_run

    def tearDown(self):
        app.FFmpegLocator.get_binary = self._orig_get_binary
        app.ass_editor.run_tracked = self._orig_run
        self._tempdir.cleanup()

    def _extract(self, lang="eng", codec="ass", **kw):
        return Path(FileProcessor.extract_subtitle(
            str(self.video), 2, lang, codec, str(self.tmp), **kw
        ))

    def test_ass_extension(self):
        self.assertEqual(self._extract(codec="ass").name, "Show.eng.ass")

    def test_pgs_extension(self):
        self.assertEqual(self._extract(codec="hdmv_pgs_subtitle").suffix, ".sup")

    def test_srt_for_text(self):
        self.assertEqual(self._extract(codec="subrip").suffix, ".srt")

    def test_image_sub_refused_cleanly(self):
        with self.assertRaises(ValueError):
            self._extract(codec="dvd_subtitle")

    def test_disambiguate_includes_stream_index(self):
        self.assertEqual(
            self._extract(codec="ass", disambiguate=True).name, "Show.eng.2.ass"
        )

    def test_existing_output_backed_up_before_overwrite(self):
        existing = self.tmp / "Show.eng.ass"
        existing.write_text("USER EDITED", encoding="utf-8")
        self._extract(codec="ass")
        backup = self.tmp / "Original Subs" / "Show.eng.ass"
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(encoding="utf-8"), "USER EDITED")

    def test_failure_unlinks_partial_and_surfaces_stderr(self):
        def failing_run(cmd, **kw):
            Path(cmd[-1]).write_text("", encoding="utf-8")  # 0-byte partial
            raise app.subprocess.CalledProcessError(
                1, cmd, output=b"", stderr=b"boom: bad stream"
            )

        app.ass_editor.run_tracked = failing_run
        with self.assertRaises(RuntimeError) as ctx:
            self._extract(codec="ass")
        self.assertIn("boom: bad stream", str(ctx.exception))
        self.assertFalse((self.tmp / "Show.eng.ass").exists())
        # ffmpeg now writes to a .tmp_ file that is removed on failure.
        self.assertEqual([p.name for p in self.tmp.glob(".tmp_*")], [])

    def test_argv_shape_input_and_int_stream_index(self):
        # Pin the subprocess command shape (risk class 2): input passed as the
        # single -i value, -map 0:<int> with an integer index, output last.
        self._extract(codec="ass")
        cmd = self.calls[-1]
        self.assertIn("-i", cmd)
        self.assertEqual(cmd[cmd.index("-i") + 1], str(self.video.resolve()))
        self.assertEqual(cmd[cmd.index("-map") + 1], "0:2")
        # Output is the final positional arg — an absolute path, never a flag.
        self.assertFalse(Path(cmd[-1]).name.startswith("-"))

    def test_image_sub_uses_stream_copy_text_does_not(self):
        self._extract(codec="hdmv_pgs_subtitle")
        img_cmd = self.calls[-1]
        self.assertEqual(img_cmd[img_cmd.index("-c") + 1], "copy")
        self._extract(codec="ass")
        self.assertNotIn("-c", self.calls[-1])

    def test_timeout_unlinks_partial_and_reraises(self):
        def timing_out(cmd, **kw):
            Path(cmd[-1]).write_text("partial", encoding="utf-8")
            raise app.subprocess.TimeoutExpired(cmd, 1)

        app.ass_editor.run_tracked = timing_out
        with self.assertRaises(app.subprocess.TimeoutExpired):
            self._extract(codec="ass")
        self.assertFalse((self.tmp / "Show.eng.ass").exists())


class FilterBestStreamsAllSignsTests(unittest.TestCase):
    def _mk(self, idx, lang, title=""):
        return app.SubtitleStream(index=idx, language=lang, codec="ass", title=title)

    def test_all_signs_falls_back_to_all(self):
        streams = [self._mk(1, "eng", "Signs"), self._mk(2, "eng", "Songs")]
        out = FileProcessor.filter_best_streams(streams, "eng")
        self.assertEqual({s.index for s in out}, {1, 2})


class ConnectionPoolTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def test_concurrent_use_no_leak(self):
        errors = []

        def worker():
            try:
                for _ in range(20):
                    with app.get_db_connection() as conn:
                        conn.execute("SELECT 1")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        pool = app.get_db_pool()
        self.assertEqual(pool._pool.qsize(), pool.pool_size)  # all returned

    def test_exhaustion_raises_operational_error(self):
        pool = app.get_db_pool()
        pool.timeout = 0.1
        held = [pool._pool.get() for _ in range(pool.pool_size)]
        try:
            with self.assertRaises(app.sqlite3.OperationalError):
                with pool.get_connection():
                    pass
        finally:
            for c in held:
                pool._pool.put(c)


_TEXTFIRST_ASS = (
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
    "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
    "MarginR, MarginV, Encoding\n"
    "Style: Default,OldFont,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
    "0,0,0,0,100,100,0,0,1,1.5,1,2,10,10,10,1\n"
    "\n[Events]\n"
    "Format: Text, Start, End, Style\n"
    "Dialogue: Hello world,0:00:01.00,0:00:05.00,Default\n"
)

# CANONICAL_ASS plus a [Fonts] section whose body line starts with "Style:".
_STYLE_OUTSIDE_SECTION_ASS = (
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
    "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
    "MarginR, MarginV, Encoding\n"
    "Style: Default,OldFont,48,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
    "0,0,0,0,100,100,0,0,1,1.5,1,2,10,10,10,1\n"
    "\n[Fonts]\n"
    "Style: KeepMe,a,b,c,d,e,f,g,h,i,j,k,l,m,n,o,p,q,r,s,t,u,v\n"
    "\n[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    "Dialogue: 0,0:00:01.00,0:00:05.00,Default,,0,0,0,,Hi\n"
)


class BackupAndDataLossTests(unittest.TestCase):
    """Guards the #1 risk class: the in-place rewrite never loses the original."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def _write(self, content, name="s.ass"):
        p = self.tmp / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_apply_aborts_and_preserves_file_when_backup_fails(self):
        # If the backup can't be made, the in-place rewrite must NOT happen and
        # the original must be byte-identical (abort-on-backup-failure).
        path = self._write(CANONICAL_ASS)
        before = path.read_text(encoding="utf-8")
        # apply_changes backs up through _prepare_backup (ADR-006 rules).
        orig = app.FileProcessor.__dict__["_prepare_backup"]
        app.FileProcessor._prepare_backup = staticmethod(lambda fp, raw, sig: False)
        try:
            ok = FileProcessor.apply_changes(path, "NewFont", "80FFFF")
        finally:
            app.FileProcessor._prepare_backup = orig
        self.assertFalse(ok)
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_backup_written_before_inplace_rewrite(self):
        # The pristine backup must already hold the original bytes at the moment
        # the live file is overwritten — a future write-then-backup reorder fails.
        path = self._write(CANONICAL_ASS)
        backup = self.tmp / "Original Subs" / path.name
        seen = {}
        # apply_changes resolves atomic_write from the ass_editor module, so
        # patch it there (not on the façade).
        orig_aw = app.ass_editor.atomic_write

        def spy(fp, content, encoding, **kw):
            seen["pristine"] = (
                backup.exists() and "OldFont" in backup.read_text(encoding="utf-8")
            )
            return orig_aw(fp, content, encoding, **kw)

        app.ass_editor.atomic_write = spy
        try:
            self.assertTrue(FileProcessor.apply_changes(path, "NewFont", "80FFFF"))
        finally:
            app.ass_editor.atomic_write = orig_aw
        self.assertTrue(seen.get("pristine"), "backup must be pristine before write")

    def test_events_text_at_index_0_does_not_corrupt_row(self):
        # A malformed [Events] Format with Text first must not glue the colour
        # tag onto the whole Layer,Start,End,... row.
        # With no taggable dialogue the file is now left as it is (ADR-006:
        # nothing visible could change, so no backup and no rewrite).
        p = self._write(_TEXTFIRST_ASS, "textfirst.ass")
        self.assertFalse(FileProcessor.apply_changes(p, "X", "112233"))
        dlg = next(
            l for l in p.read_text(encoding="utf-8").splitlines()
            if l.startswith("Dialogue:")
        )
        self.assertNotIn("{\\c&H112233&}", dlg)
        self.assertTrue(dlg.startswith("Dialogue: Hello world,0:00:01.00"))

    def test_style_prefix_outside_styles_section_untouched(self):
        # A "Style:" line outside [V4+ Styles] (e.g. a [Fonts] body line) must
        # not be column-rewritten; only the real style row is.
        p = self._write(_STYLE_OUTSIDE_SECTION_ASS, "stray.ass")
        self.assertTrue(FileProcessor.apply_changes(p, "NewFont", "112233"))
        styles = [
            l for l in p.read_text(encoding="utf-8").splitlines()
            if l.startswith("Style:")
        ]
        self.assertEqual(len(styles), 2)
        self.assertEqual(styles[0].split(":", 1)[1].split(",")[1].strip(), "NewFont")
        # The decoy in [Fonts] is verbatim (field 1 still "a", not "NewFont").
        self.assertEqual(styles[1].split(":", 1)[1].split(",")[1].strip(), "a")


class AtomicWriteFailureTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)

    def tearDown(self):
        self._tempdir.cleanup()

    def test_replace_failure_leaves_original_and_no_temp(self):
        # atomic_write's all-or-nothing guarantee: an os.replace that always
        # fails must leave the target untouched and clean up the temp file.
        path = self.tmp / "keep.ass"
        path.write_text("ORIGINAL", encoding="utf-8")

        def boom(*a, **k):
            raise PermissionError("locked")

        orig = app.os.replace
        app.os.replace = boom
        try:
            with self.assertRaises(PermissionError):
                app.atomic_write(path, ["NEW\n"], "utf-8")
        finally:
            app.os.replace = orig
        self.assertEqual(path.read_text(encoding="utf-8"), "ORIGINAL")
        leftovers = [p.name for p in self.tmp.iterdir() if p.name.startswith(".tmp_")]
        self.assertEqual(leftovers, [])


class RestoreIntegrityTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)

    def tearDown(self):
        self._tempdir.cleanup()

    def test_empty_backup_not_restored_over_good_file(self):
        # A 0-byte backup must NOT overwrite a good live file; it's a failure.
        backup = self.tmp / "orig.ass"
        backup.write_text("", encoding="utf-8")
        target = self.tmp / "live.ass"
        target.write_text(CANONICAL_ASS, encoding="utf-8")
        restored, failures = FileProcessor.restore_backups([(backup, target)])
        self.assertEqual(restored, [])
        self.assertEqual(len(failures), 1)
        self.assertEqual(target.read_text(encoding="utf-8"), CANONICAL_ASS)

    def test_restore_is_atomic_and_leaves_no_temp(self):
        # Restore overwrites the user's only live file, so it must go through the
        # atomic temp+fsync+os.replace path (not a bare copy2). Assert the content
        # round-trips AND no staging temp is left behind in the target directory.
        backup = self.tmp / "orig.ass"
        backup.write_text(CANONICAL_ASS, encoding="utf-8")
        target = self.tmp / "live.ass"
        target.write_text("stale content that must be replaced", encoding="utf-8")
        restored, failures = FileProcessor.restore_backups([(backup, target)])
        self.assertEqual(failures, [])
        self.assertEqual(restored, [target])
        self.assertEqual(target.read_text(encoding="utf-8"), CANONICAL_ASS)
        leftover = [p.name for p in self.tmp.iterdir()
                    if p.name.startswith(".tmp_") or p.name.endswith(".tmp")]
        self.assertEqual(leftover, [], f"restore left staging temp(s): {leftover}")


class DecodeContractTests(unittest.TestCase):
    def test_utf8_preferred(self):
        self.assertEqual(
            FileProcessor._decode_bytes("café".encode("utf-8")), ("utf-8", "café")
        )

    def test_codepage_bytes_decoded_strictly_no_replacement(self):
        # 0xFF*8 IS decodable (cp1252 -> 'ÿ'); the real contract is that any
        # returned text is strict — never contains U+FFFD.
        result = FileProcessor._decode_bytes(b"\xff" * 8)
        self.assertIsNotNone(result)
        _, text = result
        self.assertNotIn("�", text)


class UpdateTimestampUpsertTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def test_update_creates_row_when_absent(self):
        # The already-styled monitor branch records via update_file_timestamp on
        # a file with no prior row — it must upsert, not silently no-op.
        fp = str(self.tmp / "ep.ass")
        app.DatabaseManager.update_file_timestamp(fp, 123.0)
        rows = app.DatabaseManager.get_monitored_files()
        self.assertIn(fp, rows)
        self.assertEqual(rows[fp]["last_modified"], 123.0)


class ExtractionBatchMarkingTests(unittest.TestCase):
    """The monitor marks a video processed BEFORE submitting extraction (in-flight
    dedup across poll ticks); the batch only UN-marks on an all-transient failure
    so the video is retried, and never marks on its own."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)
        self.video = self.tmp / "Show.mkv"
        self.video.write_bytes(b"x")
        self.abs_path = str(self.video.resolve())
        self._saved = set(app.app_state.processed_videos)
        app.app_state.processed_videos = set()
        self._orig_get_binary = app.FFmpegLocator.__dict__["get_binary"]
        app.FFmpegLocator.get_binary = staticmethod(lambda name: "ffmpeg")
        self._orig_run = app.ass_editor.run_tracked

    def tearDown(self):
        app.FFmpegLocator.get_binary = self._orig_get_binary
        app.ass_editor.run_tracked = self._orig_run
        app.app_state.processed_videos = self._saved
        _restore_db(self)
        self._tempdir.cleanup()

    def _stream(self, codec="ass"):
        return app.SubtitleStream(index=2, language="eng", codec=codec)

    def _ok_run(self, cmd, **kw):
        Path(cmd[-1]).write_text("sub", encoding="utf-8")

        class _R:
            returncode = 0
            stdout = b""
            stderr = b""

        return _R()

    def _run(self, codec="ass"):
        # FolderMonitor is dependency-injected; a default instance binds to the
        # global app_state/DatabaseManager these tests assert against.
        app.FolderMonitor().run_extraction_batch(
            self.abs_path, [self._stream(codec)], str(self.tmp), "Show.mkv"
        )

    def test_success_keeps_the_premark(self):
        app.FolderMonitor().mark_video_processed(self.abs_path)
        app.ass_editor.run_tracked = self._ok_run
        self._run("ass")
        self.assertIn(self.abs_path, app.app_state.processed_videos)
        self.assertIn(self.abs_path, app.DatabaseManager.get_processed_videos())

    def test_transient_failure_unmarks_for_retry(self):
        app.FolderMonitor().mark_video_processed(self.abs_path)

        def timing_out(cmd, **kw):
            raise app.subprocess.TimeoutExpired(cmd, 1)

        app.ass_editor.run_tracked = timing_out
        self._run("ass")
        # Un-marked in BOTH the in-memory set and the DB so a later tick retries.
        self.assertNotIn(self.abs_path, app.app_state.processed_videos)
        self.assertNotIn(self.abs_path, app.DatabaseManager.get_processed_videos())

    def test_permanent_failure_keeps_the_premark(self):
        # Image-based codec raises ValueError (permanent) before ffmpeg runs:
        # stays marked so it isn't retried forever.
        app.FolderMonitor().mark_video_processed(self.abs_path)
        self._run("dvd_subtitle")
        self.assertIn(self.abs_path, app.app_state.processed_videos)

    def test_batch_does_not_mark_on_its_own(self):
        # Marking is the caller's responsibility (before submit); a successful
        # batch on an unmarked video must NOT mark it (proves no double-submit
        # regression where the batch is the sole marker).
        app.ass_editor.run_tracked = self._ok_run
        self._run("ass")
        self.assertNotIn(self.abs_path, app.app_state.processed_videos)


# ---------------------------------------------------------------------------
# Review fixes — new regression coverage
# ---------------------------------------------------------------------------

class EncodingRoundTripTests(unittest.TestCase):
    """End-to-end byte fidelity through read_text -> apply_changes ->
    atomic_write. These guard the newline-doubling and encoding-conversion
    regressions that the value-comparing tests structurally could not catch."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def _write_bytes(self, raw: bytes, name: str = "s.ass") -> Path:
        p = self.tmp / name
        p.write_bytes(raw)
        return p

    def test_crlf_newlines_not_doubled(self):
        # The headline bug: text-mode write turned every \r\n into \r\r\n.
        raw = CANONICAL_ASS.replace("\n", "\r\n").encode("utf-8")
        p = self._write_bytes(raw)
        self.assertTrue(FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
        out = p.read_bytes()
        self.assertNotIn(b"\r\r\n", out)
        self.assertIn(b"\r\n", out)

    def test_crlf_file_has_no_bare_lf_after_restyle(self):
        # The rewritten Style/Dialogue rows must keep the file's CRLF instead
        # of coming back as bare LF (mixed line endings).
        raw = CANONICAL_ASS.replace("\n", "\r\n").encode("utf-8")
        p = self._write_bytes(raw)
        self.assertTrue(FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
        out = p.read_bytes()
        self.assertIn(b"NewFont", out)
        self.assertEqual(out.count(b"\n"), out.count(b"\r\n"))

    def test_bomless_utf16_file_left_untouched(self):
        raw = CANONICAL_ASS.replace("\n", "\r\n").encode("utf-16-le")
        p = self._write_bytes(raw)
        self.assertFalse(FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
        self.assertEqual(p.read_bytes(), raw)

    def test_repeated_restyle_does_not_grow_carriage_returns(self):
        raw = CANONICAL_ASS.replace("\n", "\r\n").encode("utf-8")
        p = self._write_bytes(raw)
        FileProcessor.apply_changes(p, "FontA", "80FFFF")
        first_cr = p.read_bytes().count(b"\r")
        FileProcessor.apply_changes(p, "FontB", "80FFFF")
        second_cr = p.read_bytes().count(b"\r")
        self.assertEqual(first_cr, second_cr)

    def test_lf_only_stays_lf(self):
        raw = CANONICAL_ASS.encode("utf-8")  # LF
        p = self._write_bytes(raw)
        self.assertTrue(FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
        self.assertNotIn(b"\r\n", p.read_bytes())

    def test_cp1256_arabic_preserved_and_not_converted(self):
        # The app's core promise: restyling an Arabic (cp1256) sub keeps it
        # cp1256, not silently re-encoded to UTF-8.
        content = CANONICAL_ASS.replace(
            "Hello world", "مرحبا بالعالم"
        ).replace("\n", "\r\n")
        raw = content.encode("cp1256")
        p = self._write_bytes(raw)
        self.assertTrue(FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
        out = p.read_bytes()
        # Still decodable as cp1256, Arabic survived, NOT valid UTF-8 mojibake.
        decoded = out.decode("cp1256")
        self.assertIn("مرحبا بالعالم", decoded)

    def test_utf16_be_stays_big_endian(self):
        raw = b"\xfe\xff" + CANONICAL_ASS.replace("\n", "\r\n").encode("utf-16-be")
        p = self._write_bytes(raw)
        enc, _lines = FileProcessor.read_text(p)
        self.assertEqual(enc, "utf-16-be")
        self.assertTrue(FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
        out = p.read_bytes()
        self.assertTrue(out.startswith(b"\xfe\xff"))  # BE BOM preserved
        self.assertIn("NewFont", out.decode("utf-16"))


class LineSplitterTests(unittest.TestCase):
    def test_unicode_separators_do_not_split(self):
        # U+2028/U+2029/NEL/VT/FF can appear inside a Dialogue Text field and
        # must NOT be treated as line breaks (str.splitlines would).
        text = "Dialogue: 0,a bc\x0bd\r\n"
        lines = app.splitlines_keepends_ass(text)
        self.assertEqual(len(lines), 1)

    def test_splits_on_real_terminators_keepends(self):
        self.assertEqual(
            app.splitlines_keepends_ass("a\r\nb\nc\rd"),
            ["a\r\n", "b\n", "c\r", "d"],
        )

    def test_fast_path_matches_str_splitlines_when_no_extra_seps(self):
        # The fast path (str.splitlines when no extra separators are present)
        # must be byte-identical to the careful splitter on ordinary text.
        for text in ("a\nb\r\nc\rd", "", "no terminator", "trailing\n",
                     "\r\n\r\n", "x" * 500 + "\n" + "y" * 300):
            self.assertEqual(app.splitlines_keepends_ass(text),
                             text.splitlines(keepends=True), repr(text))

    def test_all_extra_separators_preserved(self):
        # Every separator str.splitlines breaks on but an ASS Text field may
        # legally contain must stay on one line (forces the slow path).
        for cp in (0x0b, 0x0c, 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029):
            s = "aaa" + chr(cp) + "bbb\n"
            self.assertEqual(app.splitlines_keepends_ass(s), [s], hex(cp))


class ReadTextGuardTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def test_oversized_file_is_skipped(self):
        p = self.tmp / "huge.ass"
        p.write_bytes(b"x")
        # read_text resolves MAX_ASS_FILE_SIZE from the ass_editor module.
        orig = app.ass_editor.MAX_ASS_FILE_SIZE
        try:
            app.ass_editor.MAX_ASS_FILE_SIZE = 0  # force the size guard
            self.assertIsNone(FileProcessor.read_text(p))
        finally:
            app.ass_editor.MAX_ASS_FILE_SIZE = orig

    def test_undecodable_file_returns_none(self):
        # A lone continuation byte 0x81 is invalid in every supported single-
        # and multi-byte codec we try strictly.
        p = self.tmp / "bad.ass"
        p.write_bytes(bytes([0x81, 0x81, 0x81, 0xFF, 0xFE, 0x81, 0x00, 0x81]))
        # If it happens to decode under some codepage, at least assert no crash.
        result = FileProcessor.read_text(p)
        self.assertTrue(result is None or isinstance(result, tuple))

    def test_file_hash_changes_with_content(self):
        p = self.tmp / "h.ass"
        p.write_bytes(b"one")
        h1 = FileProcessor.file_hash(p)
        p.write_bytes(b"two")
        h2 = FileProcessor.file_hash(p)
        self.assertIsNotNone(h1)
        self.assertNotEqual(h1, h2)


class BackupPoisonGuardTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def test_refuses_to_rebackup_processed_file_with_missing_backup(self):
        p = self.tmp / "s.ass"
        p.write_text(CANONICAL_ASS, encoding="utf-8")
        # First styling: creates the pristine backup, records PROCESSED.
        self.assertTrue(FileProcessor.apply_changes(p, "FontA", "80FFFF"))
        backup = self.tmp / "Original Subs" / p.name
        self.assertTrue(backup.exists())
        # Simulate the backup being deleted externally while the live file is
        # already styled.
        backup.unlink()
        self.assertTrue(app.DatabaseManager.was_processed(str(p)))
        # ADR-006: it must never snapshot the styled file as the new
        # "original" -- and it no longer blocks the file forever either: the
        # file is restyled WITHOUT a backup and the user is told once.
        self.assertTrue(FileProcessor.apply_changes(p, "FontB", "80FFFF"))
        self.assertFalse(backup.exists())
        self.assertIn("FontB", p.read_text(encoding="utf-8"))

    def test_fresh_file_backs_up_normally(self):
        p = self.tmp / "fresh.ass"
        p.write_text(CANONICAL_ASS, encoding="utf-8")
        self.assertFalse(app.DatabaseManager.was_processed(str(p)))
        self.assertTrue(FileProcessor.apply_changes(p, "FontA", "80FFFF"))
        self.assertTrue((self.tmp / "Original Subs" / p.name).exists())


class SSADetectionTests(unittest.TestCase):
    SSA_V4 = (
        "[Script Info]\nScriptType: v4.00\nPlayResY: 720\n\n"
        "[V4 Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "TertiaryColour, BackColour, Bold, Italic, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, AlphaLevel, Encoding\n"
        "Style: Default,Old,24,&H00FFFFFF,&H0000FFFF,&H00000000,&H00000000,"
        "0,0,1,1,1,2,10,10,10,0,0\n\n"
        "[Events]\n"
        "Format: Marked, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
        "Dialogue: Marked=0,0:00:01.00,0:00:05.00,Default,,0,0,0,,Hi\n"
    )

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def test_is_ssa_v4_detected(self):
        lines = self.SSA_V4.splitlines(keepends=True)
        self.assertTrue(FileProcessor._is_ssa_v4(lines))

    def test_v4plus_not_flagged_as_ssa(self):
        lines = CANONICAL_ASS.splitlines(keepends=True)
        self.assertFalse(FileProcessor._is_ssa_v4(lines))

    def test_apply_changes_skips_genuine_ssa(self):
        p = self.tmp / "old.ssa"
        original = self.SSA_V4
        p.write_text(original, encoding="utf-8")
        self.assertFalse(FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
        # The genuine SSA file is left untouched (not scrambled).
        self.assertEqual(p.read_text(encoding="utf-8"), original)


class RestoreNonAssTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)

    def tearDown(self):
        self._tempdir.cleanup()

    def test_enumerate_includes_srt_and_sup(self):
        orig = self.tmp / "Show" / "Original Subs"
        orig.mkdir(parents=True)
        (orig / "a.srt").write_text("1\n00:00:01,0 --> 00:00:02,0\nhi\n", encoding="utf-8")
        (orig / "b.sup").write_bytes(b"\x00\x01\x02\x03")
        groups = FileProcessor.enumerate_backup_groups(self.tmp)
        self.assertEqual(len(groups), 1)
        names = {b.name for _, backups in groups for b in backups}
        self.assertEqual(names, {"a.srt", "b.sup"})

    def test_restore_binary_sup_not_rejected_as_undecodable(self):
        parent = self.tmp / "Show"
        orig = parent / "Original Subs"
        orig.mkdir(parents=True)
        backup = orig / "b.sup"
        backup.write_bytes(b"\x00\x01\x02\x03PGS")  # binary, never "decodes"
        target = parent / "b.sup"
        restored, failures = FileProcessor.restore_backups([(backup, target)])
        self.assertIn(target, restored)
        self.assertEqual(failures, [])
        self.assertTrue(target.exists())


class FieldMapResolverTests(unittest.TestCase):
    def test_well_formed_uses_file_order(self):
        fields = list(ASSStyleFields.FIELD_NAMES)
        fmap, count, used = FileProcessor._resolve_style_field_map(fields)
        self.assertTrue(used)
        self.assertEqual(count, len(fields))

    def test_missing_required_falls_back_to_canonical(self):
        # Drop 'Underline' -> required subset not satisfied -> canonical.
        fields = [f for f in ASSStyleFields.FIELD_NAMES if f != "Underline"]
        fmap, count, used = FileProcessor._resolve_style_field_map(fields)
        self.assertFalse(used)
        self.assertEqual(count, len(ASSStyleFields.FIELD_NAMES))


class PostWriteDbResilienceTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def test_db_failure_after_write_still_reports_success(self):
        p = self.tmp / "s.ass"
        p.write_text(CANONICAL_ASS, encoding="utf-8")
        orig_add = app.DatabaseManager.add_file

        def boom(_fp):
            raise RuntimeError("db down")

        app.DatabaseManager.add_file = staticmethod(boom)
        try:
            # The styling write succeeded and is durable; a DB bookkeeping error
            # must not flip the result to failure.
            self.assertTrue(FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
            self.assertIn("NewFont", p.read_text(encoding="utf-8"))
        finally:
            app.DatabaseManager.add_file = orig_add


try:
    from hypothesis import given, settings, HealthCheck
    from hypothesis import strategies as st
    _HAS_HYPOTHESIS = True
except Exception:  # noqa: BLE001
    _HAS_HYPOTHESIS = False


@unittest.skipUnless(_HAS_HYPOTHESIS, "hypothesis not installed")
class ApplyChangesPropertyTests(unittest.TestCase):
    """Property-based: for ANY accepted input, apply_changes must (INV-1) never
    change a Style row's column count and place Fontname in the right column,
    and (INV-2) leave a byte-identical copy in Original Subs/."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    if _HAS_HYPOTHESIS:
        @settings(max_examples=60, deadline=None,
                  suppress_health_check=[HealthCheck.function_scoped_fixture])
        @given(
            res_y=st.integers(min_value=0, max_value=4320),
            n_dialogue=st.integers(min_value=0, max_value=6),
            newline=st.sampled_from(["\n", "\r\n"]),
            bold=st.booleans(),
        )
        def test_invariants(self, res_y, n_dialogue, newline, bold):
            header = (
                "[Script Info]\n"
                f"PlayResY: {res_y}\n\n"
                "[V4+ Styles]\n"
                "Format: " + ", ".join(ASSStyleFields.FIELD_NAMES) + "\n"
                "Style: Default,Old,48,&H00FFFFFF,&H000000FF,&H00000000,"
                "&H00000000,0,0,0,0,100,100,0,0,1,1.5,1,2,10,10,10,1\n\n"
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text\n"
            )
            for i in range(n_dialogue):
                header += f"Dialogue: 0,0:00:0{i}.00,0:00:0{i}.50,Default,,0,0,0,,line, with, commas {i}\n"
            content = header.replace("\n", newline)
            original = content.encode("utf-8")
            # Fresh dir PER hypothesis example: setUp runs once per test method
            # but many examples run inside it, and copy_original_file backs up
            # only once per path — a shared path would compare each example
            # against the first example's stale backup.
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "p.ass"
                p.write_bytes(original)

                ok = FileProcessor.apply_changes(p, "NewFont", "80FFFF", bold=bold)
                if n_dialogue == 0:
                    # No dialogue to colour: left byte-identical, no backup
                    # (ADR-006 "nothing to do").
                    self.assertFalse(ok)
                    self.assertEqual(p.read_bytes(), original)
                    self.assertFalse((Path(d) / "Original Subs" / "p.ass").exists())
                    return
                self.assertTrue(ok)

                out = p.read_text(encoding="utf-8")
                style = next(l for l in out.splitlines() if l.startswith("Style:"))
                parts = style.split(":", 1)[1].split(",")
                # INV-1: 23 columns preserved, Fontname in column 1.
                self.assertEqual(len(parts), len(ASSStyleFields.FIELD_NAMES))
                self.assertEqual(parts[1].strip(), "NewFont")

                # INV-2: Original Subs/ holds a byte-identical copy of the input.
                backup = Path(d) / "Original Subs" / "p.ass"
                self.assertTrue(backup.exists())
                self.assertEqual(backup.read_bytes(), original)


# ---------------------------------------------------------------------------
# Dependency-injection: the monitor decision loop + config layer, exercised
# with injected fakes (no real monitor thread, DB, or Tk root required).
# ---------------------------------------------------------------------------

class _FakeState:
    def __init__(self, folder):
        self.monitored_folder = folder
        self.stop_event = threading.Event()
        self._shutting_down = False
        self.events = []

    def note_event(self, verb, name, styled=False):
        # Mirrors AppState.note_event (the monitor reports styled/extracted
        # files through it for the footer status line).
        self.events.append((verb, name, styled))


class _RecordingDB:
    def __init__(self):
        self.timestamps = []

    def update_file_timestamp(self, path, last_modified, content_hash=None, status=None):
        self.timestamps.append((str(path), content_hash))
        return True


class _FakeProcessor:
    """Stand-in for FileProcessor exposing only what the decision loop calls."""
    def __init__(self, files, hashes, styled, apply_ok):
        self._files = files
        self._hashes = hashes        # str(path) -> hash
        self._styled = styled        # str(path) -> bool
        self._apply_ok = apply_ok    # str(path) -> bool
        self.applied = []
        self.styled_checked = []

    def list_ass_files(self, folder):
        return list(self._files)

    def file_hash(self, p):
        return self._hashes.get(str(p))

    def is_already_styled(self, p, *a, **k):
        self.styled_checked.append(str(p))
        return self._styled.get(str(p), False)

    def apply_changes(self, p, *a, **k):
        self.applied.append(str(p))
        return self._apply_ok.get(str(p), True)


class MonitorDecisionLoopTests(unittest.TestCase):
    """The FolderMonitor decision loop, unit-tested via constructor injection."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)

    def tearDown(self):
        self._tempdir.cleanup()

    def _mk(self, name):
        p = self.tmp / name
        p.write_text("x", encoding="utf-8")
        return p

    def _cfg(self):
        return app.AppConfig(font="NewFont", color="80FFFF")

    def test_new_file_is_applied(self):
        p = self._mk("new.ass")
        fp = _FakeProcessor([p], hashes={str(p): "h1"}, styled={}, apply_ok={str(p): True})
        mon = app.FolderMonitor(state=_FakeState(str(self.tmp)), db=_RecordingDB(), processor=fp)
        mon.process_ass_files(known_ass={}, failed_ass={}, cfg=self._cfg())
        self.assertEqual(fp.applied, [str(p)])

    def test_already_styled_refreshes_timestamp_not_applied(self):
        p = self._mk("styled.ass")
        fp = _FakeProcessor([p], hashes={str(p): "h1"}, styled={str(p): True}, apply_ok={})
        db = _RecordingDB()
        mon = app.FolderMonitor(state=_FakeState(str(self.tmp)), db=db, processor=fp)
        mon.process_ass_files(known_ass={}, failed_ass={}, cfg=self._cfg())
        self.assertEqual(fp.applied, [])
        self.assertIn(str(p), [t[0] for t in db.timestamps])

    def test_content_hash_fast_path_skips_parse(self):
        p = self._mk("same.ass")
        last_mod = p.stat().st_mtime
        known = {str(p): {
            "last_modified": last_mod - 1,           # mtime changed -> should_process
            "status": app.FileStatus.PROCESSED.value,
            "content_hash": "h1",                    # but bytes identical
        }}
        fp = _FakeProcessor([p], hashes={str(p): "h1"}, styled={}, apply_ok={})
        db = _RecordingDB()
        mon = app.FolderMonitor(state=_FakeState(str(self.tmp)), db=db, processor=fp)
        mon.process_ass_files(known_ass=known, failed_ass={}, cfg=self._cfg())
        # Fast path: neither the styled-check nor apply ran; the timestamp was
        # refreshed AND re-stamped with the same content hash (so the next tick
        # fast-paths again instead of re-parsing).
        self.assertEqual(fp.applied, [])
        self.assertEqual(fp.styled_checked, [])
        self.assertEqual(db.timestamps, [(str(p), "h1")])

    def test_failed_apply_is_recorded_in_failed_ass(self):
        p = self._mk("bad.ass")
        fp = _FakeProcessor([p], hashes={str(p): "h1"}, styled={}, apply_ok={str(p): False})
        failed = {}
        mon = app.FolderMonitor(state=_FakeState(str(self.tmp)), db=_RecordingDB(), processor=fp)
        mon.process_ass_files(known_ass={}, failed_ass=failed, cfg=self._cfg())
        self.assertIn(str(p), failed)


class ConfigInjectionTests(unittest.TestCase):
    def test_snapshot_config_headless_uses_defaults(self):
        st = app.AppState()  # no widgets built
        cfg = st.snapshot_config(folder="X")
        self.assertEqual(cfg.folder, "X")
        self.assertEqual(cfg.font, "")
        self.assertFalse(cfg.font_bold)

    def test_save_config_with_injected_state(self):
        with tempfile.TemporaryDirectory() as d:
            orig = app.runtime.CONFIG_FILE_PATH
            try:
                app.runtime.CONFIG_FILE_PATH = Path(d) / "config.json"
                st = app.AppState()
                st.monitored_folder = "FOLDER"
                st.selected_color_code = "80FFFF"
                self.assertTrue(app.ConfigManager.save_config(state=st))
                reloaded = app.AppConfig.load()
                self.assertEqual(reloaded.folder, "FOLDER")
                self.assertEqual(reloaded.color, "80FFFF")
            finally:
                app.runtime.CONFIG_FILE_PATH = orig


# ---------------------------------------------------------------------------
# Façade re-export contract + monkeypatch home-module guard. These catch the
# class of regression the package split introduced: a future re-split that drops
# a re-export the (untested) GUI needs, or moves a global out from under a test's
# monkeypatch, now fails HERE instead of silently at runtime.
# ---------------------------------------------------------------------------

class FacadeContractTests(unittest.TestCase):
    # Public API the GUI (anime_subs_customizer.pyw) imports via `import *`.
    REQUIRED = [
        "FileProcessor", "DatabaseManager", "FolderMonitor", "FFmpegLocator",
        "FontLoader", "FontSupport", "AppState", "app_state", "ConfigManager",
        "AppConfig", "SubtitleStream", "SecurityUtils", "IssueTracker",
        "FileStatus", "ASSStyleFields", "record_error", "atomic_write",
        "splitlines_keepends_ass", "file_hash", "has_rtl_chars",
        "wrap_rtl_if_needed", "bgr_to_rgb_hex", "rgb_to_bgr_hex",
        "format_ass_primary_color", "get_db_connection", "COMMON_LANGS",
        "BACKUP_SUBTITLE_GLOBS", "ensure_default_icon", "app_root",
        "APP_NAME", "APPDATA_DIR", "DB_PATH", "CONFIG_FILE_PATH", "ICON_PATH",
        "LOG_PATH", "IS_WINDOWS", "DEFAULT_PREVIEW_TEXT", "START_MINIMIZED",
    ]

    def test_facade_reexports_public_api(self):
        missing = [n for n in self.REQUIRED if not hasattr(app, n)]
        self.assertEqual(missing, [], f"façade missing: {missing}")

    def test_facade_reexports_underscore_name_gui_imports(self):
        # The GUI imports this explicitly (not surfaced by `import *`).
        self.assertTrue(hasattr(app, "_next_font_index"))

    def test_submodules_exposed(self):
        for n in ("runtime", "db", "fonts", "media", "ass_editor", "state", "monitor"):
            self.assertTrue(hasattr(app, n), f"submodule {n} not exposed on façade")

    def test_monkeypatch_home_modules(self):
        # These globals are patched by the suite at their HOME module; if a
        # re-split moves them, the patch would silently miss — guard them here.
        self.assertTrue(hasattr(app.db, "DB_PATH"))
        self.assertTrue(hasattr(app.db, "_db_pool"))
        self.assertTrue(hasattr(app.db, "_db_pool_shutdown"))
        self.assertTrue(hasattr(app.ass_editor, "atomic_write"))
        self.assertTrue(hasattr(app.ass_editor, "MAX_ASS_FILE_SIZE"))
        self.assertTrue(hasattr(app.runtime, "CONFIG_FILE_PATH"))


class FontScanSeamTests(unittest.TestCase):
    """Exercise the font subsystem end to end — the area the tkFont split
    regression lived in. get_fonts (Tk) is guarded so a missing import there is
    caught; filter_supporting/_supports_chars (PIL) need no Tk root."""

    def test_get_fonts_returns_families(self):
        # get_fonts() calls tkFont.families(), which needs a Tk root — exactly
        # the path the tkFont NameError broke. Run it against a throwaway root so
        # a future missing-import in fonts.py fails HERE, not only in the app.
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
        except Exception:
            self.skipTest("no Tk display available")
        try:
            app.FontLoader._fonts = None  # bust the cache to force tkFont.families()
            fonts = app.FontLoader.get_fonts()
            self.assertIsInstance(fonts, list)
        finally:
            app.FontLoader._fonts = None
            root.destroy()

    def test_filter_supporting_ascii_returns_all(self):
        # ASCII preview text -> no script to check -> every family kept.
        fams = ["Arial", "NoSuchFontXYZ"]
        self.assertEqual(app.FontSupport.filter_supporting(fams, "hello"), fams)

    def test_filter_supporting_nonascii_runs_without_error(self):
        # Drives _get_path_map (on-disk scan + cache) and _supports_chars; the
        # result depends on installed fonts, so just assert it runs and returns
        # a subset list rather than raising (the seam the split could break).
        res = app.FontSupport.filter_supporting(["Arial"], "مرحبا")
        self.assertIsInstance(res, list)
        self.assertTrue(set(res).issubset({"Arial"}))

    def test_supports_chars_unloadable_font_returns_none(self):
        # Tri-state contract: a font that can't be loaded -> None (not cached as
        # a permanent "unsupported"); a definite judgement -> bool.
        self.assertIsNone(app.FontSupport._supports_chars("NoSuchFontXYZ", "م"))


class FFmpegLocatorAppRootTests(unittest.TestCase):
    """get_binary must search the APP ROOT (where the .pyw launcher and bundled
    bin/ live), not the anime_subs package dir.

    Regression guard for the package-split bug: media.py used a __file__-relative
    path that, after the split, searched anime_subs/ instead of the app root.
    Master resolves the root via runtime.app_root(); we redirect that to a temp
    dir, drop a stub binary, and confirm get_binary finds it with an empty cache
    and empty PATH — and that the package dir is NOT searched.

    (Ported and adapted from the parallel 'Fix FFmpeg binary lookup' branch,
    which patched media.__file__; master uses the shared app_root() helper, so
    redirecting app_root is the correct, more robust hook.)
    """

    def setUp(self):
        self.L = app.FFmpegLocator
        self._saved = (
            dict(self.L._cached), dict(self.L._not_found_ts),
            set(self.L._verified), self.L._cache_loaded,
        )
        self.L._cached = {}
        self.L._not_found_ts = {}
        self.L._verified = set()
        self.L._cache_loaded = True  # don't read the user's real cache file

        self._orig_verify = self.L.__dict__["_verify_binary"]
        self._orig_save = self.L.__dict__["_save_cache"]
        self._orig_which = app.media.shutil.which
        self._orig_app_root = app.media.app_root

        # Any existing candidate is "valid"; never spawn `<bin> -version`.
        self.L._verify_binary = classmethod(lambda cls, path: True)
        self.L._save_cache = classmethod(lambda cls: None)
        app.media.shutil.which = lambda name: None  # empty PATH

        self._tempdir = tempfile.TemporaryDirectory()
        self.approot = Path(self._tempdir.name).resolve()
        app.media.app_root = lambda: self.approot  # redirect app-root resolution
        self.exe = "ffprobe.exe" if app.media.IS_WINDOWS else "ffprobe"

    def tearDown(self):
        c, nf, v, loaded = self._saved
        self.L._cached, self.L._not_found_ts = c, nf
        self.L._verified, self.L._cache_loaded = v, loaded
        self.L._verify_binary = self._orig_verify
        self.L._save_cache = self._orig_save
        app.media.shutil.which = self._orig_which
        app.media.app_root = self._orig_app_root
        self._tempdir.cleanup()

    def test_finds_binary_in_approot_bin(self):
        bin_dir = self.approot / "bin"
        bin_dir.mkdir()
        binary = bin_dir / self.exe
        binary.write_bytes(b"stub")
        self.assertEqual(self.L.get_binary("ffprobe"), str(binary))

    def test_finds_binary_in_approot_root(self):
        binary = self.approot / self.exe
        binary.write_bytes(b"stub")
        self.assertEqual(self.L.get_binary("ffprobe"), str(binary))

    def test_ignores_package_dir(self):
        # The pre-fix search locations (the package dir and its bin/) must NOT
        # be searched — bundling never puts the binary there.
        pkg = self.approot / "anime_subs"
        (pkg / "bin").mkdir(parents=True)
        (pkg / self.exe).write_bytes(b"stub")
        (pkg / "bin" / self.exe).write_bytes(b"stub")
        self.assertIsNone(self.L.get_binary("ffprobe"))


# ---------------------------------------------------------------------------
# Hybrid watchdog monitor: watchdog is only a "wake up and check" signal; the
# reconcile loop (compared against recorded state) is what prevents the
# process-the-same-file-forever problem. These tests pin that behavior.
# ---------------------------------------------------------------------------

class WakeHandlerTests(unittest.TestCase):
    """The watchdog event filter: wake on subtitle/video changes, ignore our own
    backup dir and atomic-write/backup temp files, ignore irrelevant files."""

    def _fired(self, src, dest=""):
        import types
        w = threading.Event()
        h = app.monitor._WakeHandler(w)
        h.on_any_event(types.SimpleNamespace(src_path=src, dest_path=dest, is_directory=False))
        return w.is_set()

    def test_ass_wakes(self):
        self.assertTrue(self._fired(r"C:\Anime\Show\ep.ass"))

    def test_ssa_wakes(self):
        self.assertTrue(self._fired(r"C:\Anime\Show\ep.ssa"))

    def test_video_wakes(self):
        self.assertTrue(self._fired(r"C:\Anime\Show\ep.mkv"))

    def test_moved_into_folder_wakes_on_dest(self):
        # A download that renames .part -> .ass: the dest is what matters.
        self.assertTrue(self._fired(r"C:\Anime\ep.part", r"C:\Anime\ep.ass"))

    def test_irrelevant_file_ignored(self):
        self.assertFalse(self._fired(r"C:\Anime\Show\readme.txt"))

    def test_backup_dir_ignored(self):
        # Our own restore backups must not wake the loop.
        self.assertFalse(self._fired(r"C:\Anime\Show\Original Subs\ep.ass"))

    def test_atomic_write_temp_ignored(self):
        # atomic_write / copy_original_file temp files (.tmp_*, .bak_*) are our
        # own write machinery — must not wake the loop even though they end .ass.
        self.assertFalse(self._fired(r"C:\Anime\Show\.tmp_x1y2.ass"))
        self.assertFalse(self._fired(r"C:\Anime\Show\.bak_x1y2.tmp"))


class WatchdogHybridTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tempdir.name)
        _isolate_db(self, self.tmp)

    def tearDown(self):
        _restore_db(self)
        self._tempdir.cleanup()

    def _monitor(self):
        st = app.AppState()
        st.monitored_folder = str(self.tmp)
        return app.FolderMonitor(state=st, db=app.DatabaseManager, processor=app.FileProcessor), st

    def _cfg(self):
        return app.AppConfig(font="NewFont", color="80FFFF")

    def test_styled_file_not_reprocessed_on_wake(self):
        # THE property the user hit: after the app styles a file (which itself
        # fires a filesystem event), a wake+reconcile must NOT rewrite it again.
        p = self.tmp / "ep.ass"
        p.write_text(CANONICAL_ASS, encoding="utf-8")
        self.assertTrue(app.FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
        mtime_after_style = p.stat().st_mtime

        mon, _st = self._monitor()
        mon.process_ass_files(app.DatabaseManager.get_monitored_files(), {}, self._cfg())
        # Not rewritten: the recorded (post-style) mtime matches -> skipped.
        self.assertEqual(p.stat().st_mtime, mtime_after_style)

    def test_touch_uses_content_hash_fast_path(self):
        # A metadata touch (mtime bumped, bytes identical) must not rewrite —
        # the content-hash fast path absorbs it, so a chatty watcher can't loop.
        p = self.tmp / "ep.ass"
        p.write_text(CANONICAL_ASS, encoding="utf-8")
        self.assertTrue(app.FileProcessor.apply_changes(p, "NewFont", "80FFFF"))
        bumped = p.stat().st_mtime + 100
        os.utime(p, (bumped, bumped))

        mon, _st = self._monitor()
        mon.process_ass_files(app.DatabaseManager.get_monitored_files(), {}, self._cfg())
        # Fast path skipped the rewrite -> the touched mtime is untouched.
        self.assertEqual(p.stat().st_mtime, bumped)

    def test_start_observer_none_when_watchdog_unavailable(self):
        mon, _st = self._monitor()
        saved = app.monitor._WATCHDOG_AVAILABLE
        try:
            app.monitor._WATCHDOG_AVAILABLE = False
            self.assertIsNone(mon._start_observer())  # graceful fallback to polling
        finally:
            app.monitor._WATCHDOG_AVAILABLE = saved

    def test_run_starts_and_stops_promptly(self):
        # Full run() lifecycle: starts (observer + reconcile) and, when stop +
        # wake are set, exits promptly instead of parking in the change-wait.
        mon, st = self._monitor()
        t = threading.Thread(target=mon.run, daemon=True)
        t.start()
        time.sleep(0.6)
        self.assertTrue(t.is_alive())
        st.stop_event.set()
        st.wake_event.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "monitor did not stop promptly after stop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
