"""Engine decisions checked against real players (Phase 4 row 5, mpv 0.41 and
PotPlayer on the synthetic demo library):

- A short Style row was padded with EMPTY fields; libass reads an empty
  ScaleX/ScaleY as 0, so that style's dialogue vanished in mpv.
- "sign" matched anywhere in a style name, so a dialogue style called
  "Signe" (or "Design") was left unstyled.
- A song style "OP English" was restyled as dialogue ("op" only counted as
  the whole name).
- With only PlayResX: 1280, libass (like VSFilter) uses a 1024 high canvas,
  not 960.
- --check-styled called a file with no dialogue to colour "would restyle",
  although Watch and Apply leave such files alone.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs.ass_editor import FileProcessor  # noqa: E402
from anime_subs.check_styled import check_styled  # noqa: E402
from anime_subs.runtime import AppConfig  # noqa: E402

FORMAT = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
          "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
          "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\r\n")
EVENTS = "[Events]\r\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
FULL = "0,0,0,0,100,100,0,0,1,2,1,2,20,20,20,1"


def ass(styles, dialogue, header="PlayResX: 1920\r\nPlayResY: 1080\r\n"):
    return ("[Script Info]\r\nScriptType: v4.00+\r\n" + header + "\r\n[V4+ Styles]\r\n" + FORMAT
            + "".join(styles) + "\r\n" + EVENTS + "".join(dialogue))


def styles_of(lines):
    return {ln.split(":", 1)[1].split(",")[0].strip(): ln.split(":", 1)[1].rstrip("\r\n").split(",")
            for ln in lines if ln.startswith("Style:")}


class _Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def write(self, text, name="x.ass"):
        p = Path(self.tmp.name) / name
        p.write_bytes(text.encode("utf-8"))
        return p


class ShortStyleRowTests(_Files):
    def test_missing_fields_get_renderer_defaults(self):
        p = self.write(ass(
            [f"Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,{FULL}\r\n",
             "Style: Alt,Arial,70,&H00FFFFFF\r\n"],
            ["Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,Hello\r\n",
             "Dialogue: 0,0:00:03.00,0:00:04.00,Alt,,0,0,0,,Hi again\r\n"]))
        new = FileProcessor.compute_changes(p, "Segoe UI", "00FFFF", size="none")[1]
        alt = styles_of(new)["Alt"]
        self.assertEqual(len(alt), 23)
        self.assertEqual(alt[11:15], ["100", "100", "0", "0"])   # ScaleX, ScaleY, Spacing, Angle
        self.assertEqual(alt[15], "1")                            # BorderStyle
        self.assertEqual(alt[18], "2")                            # Alignment
        self.assertEqual(alt[22], "1")                            # Encoding
        self.assertNotIn("", alt)


class StyleNameTests(unittest.TestCase):
    def skipped(self, name, text="Hello there"):
        lines = ass([], [f"Dialogue: 0,0:00:01.00,0:00:02.00,{name},,0,0,0,,{text}\r\n"]).splitlines(True)
        return name in FileProcessor._typesetting_styles(lines)

    def test_sign_styles_by_name(self):
        for name in ("Sign", "Signs", "SIGNS", "Signs-Top", "Sign_Bottom", "TopSign",
                     "Sign1", "signtop", "Signage", "Typesetting", "TitleCard"):
            with self.subTest(name=name):
                self.assertTrue(self.skipped(name))

    def test_dialogue_names_containing_sign_letters(self):
        for name in ("Signe", "Design", "Assignment", "Default", "Main"):
            with self.subTest(name=name):
                self.assertFalse(self.skipped(name))

    def test_song_styles_named_op_or_ed(self):
        for name in ("OP", "ED", "OP English", "ED Romaji", "OP_Eng", "OP1", "ED-TL", "OPEnglish"):
            with self.subTest(name=name):
                self.assertTrue(self.skipped(name))

    def test_names_starting_with_op_or_ed_letters_stay_dialogue(self):
        for name in ("Ed Elric", "Opera", "Edward", "Operator"):
            with self.subTest(name=name):
                self.assertFalse(self.skipped(name))


class PlayResTests(unittest.TestCase):
    def test_only_playresx(self):
        self.assertEqual(FileProcessor._effective_playres_y(1280, None), 1024)
        self.assertEqual(FileProcessor._effective_playres_y(1920, None), 1440)
        self.assertEqual(FileProcessor._effective_playres_y(None, None), 288)
        self.assertEqual(FileProcessor._effective_playres_y(1280, 720), 720)


class CheckStyledNothingToDoTests(_Files):
    def test_file_without_dialogue_to_colour_is_not_would_restyle(self):
        self.write(ass(
            [f"Style: Sign,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,{FULL}\r\n"],
            ["Dialogue: 0,0:00:01.00,0:00:02.00,Sign,,0,0,0,,{\\pos(10,10)}A SIGN\r\n"]),
            "signs only.ass")
        report = check_styled(self.tmp.name, AppConfig(font="Segoe UI", color="00FFFF"))
        self.assertEqual(report["counts"]["would_restyle"], 0)
        self.assertEqual(report["counts"]["no_dialogue"], 1)


if __name__ == "__main__":
    unittest.main()
