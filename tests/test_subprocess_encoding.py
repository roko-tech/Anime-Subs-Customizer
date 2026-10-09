"""FFmpeg tools print UTF-8. Decoding their output with the Windows ANSI code
page (Python's default for text=True) breaks on any non-ASCII stream title or
path on English/Western/CJK Windows, and a failed probe used to mark the video
"processed" with nothing extracted.

The fake subprocess below behaves like the real one on a cp1252 machine: it
decodes with the encoding the caller asked for, or cp1252 when the caller only
said text=True.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anime_subs import ass_editor  # noqa: E402

PROBE_JSON = json.dumps({"streams": [
    {"index": 2, "codec_type": "subtitle", "codec_name": "ass",
     "tags": {"language": "jpn", "title": "日本語 — ‘Signs & Songs’"}},
    {"index": 3, "codec_type": "subtitle", "codec_name": "ass",
     "tags": {"language": "ara", "title": "العربية"}},
]}, ensure_ascii=False).encode("utf-8")


def fake_run_on_cp1252(cmd, **kwargs):
    raw = PROBE_JSON
    if kwargs.get("encoding"):
        out = raw.decode(kwargs["encoding"], kwargs.get("errors", "strict"))
    elif kwargs.get("text") or kwargs.get("universal_newlines"):
        out = raw.decode("cp1252")  # the ANSI default: raises on these bytes
    else:
        out = raw
    return SimpleNamespace(stdout=out, stderr="", returncode=0)


class FfprobeDecodingTests(unittest.TestCase):
    def test_streams_with_non_ascii_titles_are_read_on_a_cp1252_machine(self):
        video = ROOT / "tests" / "test_subprocess_encoding.py"  # any existing file
        with patch.object(ass_editor.FFmpegLocator, "get_binary", return_value="ffprobe"), \
             patch.object(ass_editor, "run_tracked", side_effect=fake_run_on_cp1252):
            streams = ass_editor.FileProcessor.list_subtitle_streams(str(video))
        self.assertIsNotNone(streams)
        self.assertEqual([s.language for s in streams], ["jpn", "ara"])
        self.assertEqual(streams[1].title, "العربية")


if __name__ == "__main__":
    unittest.main()
