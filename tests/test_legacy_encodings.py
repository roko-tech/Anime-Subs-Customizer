"""Golden decoding fixtures for subtitles without a BOM (tests-ci-1,
data-safety-5). Each file is a realistic .ass (ASCII header + dialogue) and must
decode to its exact text, so a change of chardet version or of the detection
rules can't silently turn Arabic or Japanese into mojibake.

The app pins chardet 5.2.0, which has no Arabic model (it calls cp1256 Arabic
"MacRoman"); Arabic is recognised by a direct check instead."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import ass_editor  # noqa: E402
from anime_subs.ass_editor import FileProcessor  # noqa: E402

HEADER = (
    "[Script Info]\r\nScriptType: v4.00+\r\nPlayResX: 1920\r\nPlayResY: 1080\r\n\r\n"
    "[V4+ Styles]\r\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
    "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, "
    "Encoding\r\nStyle: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,"
    "0,0,0,0,100,100,0,0,1,2,1,2,40,40,40,1\r\n\r\n[Events]\r\nFormat: Layer, Start, End, "
    "Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
)
TEXT = {
    "cp1256": ["هل تتذكر ما قلته لك بالأمس؟", "لا أريد أن أذهب إلى هناك وحدي.",
               "السماء جميلة الليلة، أليس كذلك؟", "انتظرني عند البوابة الشرقية.",
               "سأعود قبل غروب الشمس، أعدك."],
    "cp1252": ["Où est passé le café, déjà ?", "Je ne veux pas y aller tout seul.",
               "Le ciel est très beau ce soir, non ?", "Attends-moi près de la porte à l’est.",
               "Je reviendrai avant le coucher du soleil — promis."],
    "shift_jis": ["昨日言ったこと、覚えてる？", "一人で行きたくないんだ。", "今夜の空はきれいだね。",
                  "東の門で待っていて。", "日が沈む前に戻るよ、約束する。"],
    "cp1251": ["Ты помнишь, что я сказал вчера?", "Я не хочу идти туда один.",
               "Небо сегодня такое красивое.", "Жди меня у восточных ворот.",
               "Я вернусь до заката, обещаю."],
    "gb18030": ["你还记得我昨天说的话吗？", "我不想一个人去那里。", "今晚的天空真美啊。",
                "在东门等我。", "我保证在日落前回来。"],
    "big5": ["你還記得我昨天說的話嗎？", "我不想一個人去那裡。", "今晚的天空真美啊。",
             "在東門等我。", "我保證在日落前回來。"],
}


def build(enc: str, n: int) -> str:
    rows = [f"Dialogue: 0,0:00:{i:02d}.00,0:00:{i + 1:02d}.00,Default,,0,0,0,,"
            f"{TEXT[enc][i % 5]}\r\n" for i in range(n)]
    return HEADER + "".join(rows)


@unittest.skipIf(ass_editor.chardet is None, "chardet not installed")
class GoldenLegacyEncodingTests(unittest.TestCase):
    def test_chardet_is_the_pinned_lgpl_line(self):
        self.assertTrue(ass_editor.chardet.__version__.startswith("5."),
                        ass_editor.chardet.__version__)

    def test_every_fixture_decodes_to_its_exact_text(self):
        for enc in TEXT:
            for n in (1, 3, 12, 40, 200):
                with self.subTest(encoding=enc, lines=n):
                    text = build(enc, n)
                    got = FileProcessor._decode_bytes(text.encode(enc))
                    self.assertIsNotNone(got)
                    self.assertEqual(got[1], text)
                    # Re-encoding with the detected codec must give the same bytes.
                    self.assertEqual(got[1].encode(got[0]), text.encode(enc))


class NoChardetFallbackTests(unittest.TestCase):
    def test_arabic_still_decodes_without_chardet(self):
        text = build("cp1256", 12)
        orig = ass_editor.chardet
        ass_editor.chardet = None
        try:
            got = FileProcessor._decode_bytes(text.encode("cp1256"))
        finally:
            ass_editor.chardet = orig
        self.assertEqual(got, ("cp1256", text))


if __name__ == "__main__":
    unittest.main()
