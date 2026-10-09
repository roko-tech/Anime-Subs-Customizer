"""Arabic text direction and first-run language.

- A bidi embedding ends at a paragraph break, so each paragraph of an Arabic
  message needs its own right-to-left base; otherwise every paragraph after the
  first renders scrambled (seen in the Restore and Reset confirmations).
- Paths, file names and other left-to-right values inside an Arabic sentence
  must stay in their own order (D:\\Videos\\Show (2024) used to come out as
  "(2024) D:\\Videos\\Show"). Isolate marks (U+2066-2069) are NOT used: GDI on
  Windows 10 draws them as visible boxes and python-bidi rejects them.
- The first-run language follows the installer's choice, then the Windows
  display language.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import i18n  # noqa: E402

RLE, LRE, PDF = "\u202b", "\u202a", "\u202c"


class RtlParagraphTests(unittest.TestCase):
    def setUp(self):
        self._prev = i18n.get_language()
        i18n.set_language("ar")

    def tearDown(self):
        i18n.set_language(self._prev)

    def test_each_arabic_paragraph_gets_its_own_rtl_base(self):
        out = i18n._rtl_base("السطر الأول\n\nالسطر الثاني.")
        paras = out.split("\n")
        self.assertEqual(paras[0], RLE + "السطر الأول" + PDF)
        self.assertEqual(paras[1], "")
        self.assertEqual(paras[2], RLE + "السطر الثاني." + PDF)

    def test_ltr_values_are_embedded_left_to_right(self):
        i18n._STRINGS["ar"]["__probe__"] = "لا توجد نسخ في {path}."
        try:
            out = i18n.t_raw("__probe__", path="D:\\Videos\\Show (2024)")
        finally:
            del i18n._STRINGS["ar"]["__probe__"]
        self.assertIn(LRE + "D:\\Videos\\Show (2024)" + PDF, out)

    def test_english_ui_is_untouched(self):
        i18n.set_language("en")
        self.assertEqual(i18n._rtl_base("Line one\nLine two"), "Line one\nLine two")
        self.assertEqual(i18n.t_raw("busy_title"), i18n._STRINGS["en"]["busy_title"])

    def test_no_isolate_marks_in_any_string(self):
        for lang, table in i18n._STRINGS.items():
            for key, s in table.items():
                self.assertFalse(any(0x2066 <= ord(c) <= 0x2069 for c in s),
                                 f"{lang}:{key} contains an isolate mark")


def _plain(s):
    return "".join(c for c in s if c not in (RLE, LRE, PDF))


def _balanced(line):
    depth = 0
    for c in line:
        if c in (RLE, LRE):
            depth += 1
        elif c == PDF:
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


class WrapRtlTests(unittest.TestCase):
    """Tk breaks a long label itself and draws every line after the first
    without the paragraph's RTL embedding, so wrapped Arabic lines came out
    left-to-right. wrap_rtl breaks the lines first and gives each its own."""

    def setUp(self):
        self._prev = i18n.get_language()
        i18n.set_language("ar")

    def tearDown(self):
        i18n.set_language(self._prev)

    def test_english_ui_is_untouched(self):
        i18n.set_language("en")
        text = "A long English sentence that would wrap."
        self.assertEqual(i18n.wrap_rtl(text, len, 10), text)

    def test_wraps_greedily_and_embeds_every_line(self):
        out = i18n.wrap_rtl("أب جد هوز حطي", len, 6)
        self.assertEqual(out.split("\n"),
                         [RLE + "أب جد" + PDF, RLE + "هوز" + PDF, RLE + "حطي" + PDF])

    def test_outer_embedding_from_rtl_base_is_not_doubled(self):
        out = i18n.wrap_rtl(i18n._rtl_base("أب جد هوز حطي"), len, 6)
        for line in out.split("\n"):
            self.assertEqual(line.count(RLE), 1)
            self.assertTrue(line.startswith(RLE) and line.endswith(PDF))

    def test_a_line_that_fits_is_what_rtl_base_gives(self):
        text = i18n._rtl_base("سطر قصير.\n\nوسطر آخر.")
        self.assertEqual(i18n.wrap_rtl(text, len, 100), text)

    def test_paragraph_breaks_and_pure_ltr_lines_are_kept(self):
        out = i18n.wrap_rtl("أب جد\n\nD:/Videos/Anime", len, 100)
        self.assertEqual(out.split("\n"), [RLE + "أب جد" + PDF, "", "D:/Videos/Anime"])

    def test_measure_gets_text_without_marks(self):
        seen = []

        def measure(s):
            seen.append(s)
            return len(s)

        i18n.wrap_rtl(RLE + "أب " + LRE + "C:\\x y" + PDF + " جد" + PDF, measure, 5)
        self.assertTrue(seen)
        self.assertFalse(any(c in s for s in seen for c in (RLE, LRE, PDF)))

    def test_ltr_run_crossing_a_break_is_closed_and_reopened(self):
        value = LRE + "C:\\Users\\Demo Folder\\x.log" + PDF
        out = i18n.wrap_rtl("تعذّر فتح " + value + " الآن", len, 20)
        lines = out.split("\n")
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertTrue(_balanced(line), repr(line))
        # Each piece of the path sits in its own LTR run on its own line.
        for line in lines:
            if "Demo" in line or "Folder" in line:
                self.assertIn(LRE, line)
        self.assertEqual(" ".join(_plain(ln) for ln in lines),
                         "تعذّر فتح C:\\Users\\Demo Folder\\x.log الآن")

    def test_a_word_longer_than_the_width_stays_whole(self):
        out = i18n.wrap_rtl("أ " + "ب" * 30 + " ج", len, 10)
        self.assertIn(RLE + "ب" * 30 + PDF, out.split("\n"))

    def test_rtl_base_is_public_for_composed_text(self):
        self.assertEqual(i18n.rtl_base("أب · 5 ✓"), RLE + "أب · 5 ✓" + PDF)
        i18n.set_language("en")
        self.assertEqual(i18n.rtl_base("Arabic · 5 ✓"), "Arabic · 5 ✓")


class FirstRunLanguageTests(unittest.TestCase):
    def test_stored_choice_wins(self):
        self.assertEqual(i18n.resolve_ui_language("en", installer="ar", windows_lang_id=0x0401), "en")
        self.assertEqual(i18n.resolve_ui_language("ar", installer=None, windows_lang_id=0x0409), "ar")

    def test_installer_choice_seeds_first_run(self):
        self.assertEqual(i18n.resolve_ui_language("", installer="ar", windows_lang_id=0x0409), "ar")
        self.assertEqual(i18n.resolve_ui_language("", installer="en", windows_lang_id=0x0401), "en")

    def test_windows_arabic_display_language(self):
        for lang_id in (0x0401, 0x0C01, 0x1401, 0x3801):  # ar-SA, ar-EG, ar-DZ, ar-AE
            self.assertEqual(i18n.resolve_ui_language("", installer=None, windows_lang_id=lang_id), "ar")

    def test_other_windows_languages_get_english(self):
        for lang_id in (0x0409, 0x040C, 0x0411, 0):
            self.assertEqual(i18n.resolve_ui_language("", installer=None, windows_lang_id=lang_id), "en")

    def test_garbage_values_are_ignored(self):
        self.assertEqual(i18n.resolve_ui_language(["ar"], installer="fr", windows_lang_id=0x0409), "en")
        self.assertEqual(i18n.resolve_ui_language(None, installer=5, windows_lang_id=0x0401), "ar")

    def test_set_language_tolerates_non_strings(self):
        i18n.set_language(["ar"])
        self.assertEqual(i18n.get_language(), "en")


if __name__ == "__main__":
    unittest.main()
