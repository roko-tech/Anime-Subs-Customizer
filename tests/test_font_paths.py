"""Fonts stored under a non-ASCII path (an Arabic Windows username, an Arabic
font file name) must still load in FreeType. freetype-py hands the path to
FreeType's ANSI fopen, which fails for such paths, so coverage checks, size
normalization and the HarfBuzz preview silently fell back."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import fonts  # noqa: E402

SYSTEM_FONT = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "arial.ttf"


@unittest.skipUnless(fonts._ft is not None and SYSTEM_FONT.exists(),
                     "needs freetype-py and arial.ttf")
class NonAsciiFontPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        folder = Path(self.tmp.name) / "محمد" / "خطوط"
        folder.mkdir(parents=True)
        self.font = folder / "خط تجريبي.ttf"
        shutil.copyfile(SYSTEM_FONT, self.font)

    def tearDown(self):
        self.tmp.cleanup()

    def test_face_loads_from_a_non_ascii_path(self):
        face = fonts.ft_face(str(self.font), 0)
        self.assertGreater(face.num_glyphs, 0)

    def test_coverage_and_size_work_for_a_non_ascii_path(self):
        with patch.object(fonts.FontSupport, "font_file",
                          return_value=(str(self.font), 0)):
            self.assertEqual(fonts.FontSupport._missing_chars_ft("Test", "Abc"), [])
            self.assertIsNotNone(fonts.FontSupport._measure_size_ratio("Test", "latin"))


if __name__ == "__main__":
    unittest.main()
