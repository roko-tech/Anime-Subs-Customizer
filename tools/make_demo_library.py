"""Generate a copyright-free synthetic anime library for live tests and docs.

Every series title, line of dialogue, sign and lyric here is invented for this
tool: no real show, fansub group or font is used. The embedded ``[Fonts]``
payload is RANDOM bytes from the seed, labelled as a fake font.

Usage:
    python tools/make_demo_library.py OUTDIR [--no-video] [--bulk N]
                                             [--seed S] [--ffmpeg PATH]

OUTDIR must be absent or empty. Output is deterministic for a given seed:
sidecars byte-for-byte (each file has its own RNG, so --no-video/--bulk do not
change them), videos as far as ffmpeg's bitexact mode allows.
``OUTDIR/manifest.json`` lists every file with its encoding, BOM, line ending,
the expected class of each style (dialogue / sign / song / karaoke) and its
special traits, so tests can assert against it.

Videos need ffmpeg + ffprobe (``--ffmpeg`` or PATH); they are only used to
GENERATE local test media. Stdlib only.
"""
import argparse
import codecs
import json
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ----------------------------------------
# Invented text (paired by index: EN[i] / AR[i] / JA[i] mean the same thing)
# ----------------------------------------
EN = [
    "The wind is changing direction again.",
    "Did you water the cloud ferns this morning?",
    "Hold on, the bridge is still drifting.",
    "We have until sunset to reach the upper terrace.",
    "I packed tea, rope, and one very small umbrella.",
    "That map is upside down.",
    "No, the garden is upside down.",
    "Listen. The bells only ring when it rains upward.",
    "Then we should hurry before the sky dries out.",
    "Grandmother said the gate opens for honest travelers.",
    "Honestly, I'm a little scared of heights.",
    "Me too. Let's not look down together.",
    "Is that a lighthouse floating over there?",
    "It's the post office. It moves on Tuesdays.",
    "Write it down, or we'll forget the way home.",
    "Next stop: the orchard of paper lanterns.",
]
AR = [
    "الرياح تغيّر اتجاهها مرة أخرى.",
    "هل سقيت سرخس الغيوم هذا الصباح؟",
    "انتظر، الجسر ما زال ينجرف.",
    "أمامنا حتى الغروب لنصل إلى الشرفة العليا.",
    "حزمت شايًا وحبلًا ومظلة صغيرة جدًا.",
    "تلك الخريطة مقلوبة.",
    "لا، الحديقة هي المقلوبة.",
    "اسمع. الأجراس لا تقرع إلا حين يهطل المطر إلى الأعلى.",
    "إذن علينا أن نسرع قبل أن تجفّ السماء.",
    "قالت جدتي إن البوابة تُفتح للمسافرين الصادقين.",
    "بصراحة، أخاف قليلًا من المرتفعات.",
    "وأنا أيضًا. لنتفق ألا ننظر إلى الأسفل.",
    "هل تلك منارة تطفو هناك؟",
    "إنه مكتب البريد. يتنقّل أيام الثلاثاء.",
    "دوّنه، وإلا نسينا طريق العودة.",
    "المحطة التالية: بستان الفوانيس الورقية.",
]
JA = [
    "また風向きが変わった。",
    "今朝、雲のシダに水をあげた？",
    "待って、橋がまだ流されてる。",
    "日没までに上のテラスへ行かないと。",
    "お茶とロープと、とても小さな傘を持ってきた。",
    "その地図、逆さまだよ。",
    "違う、庭のほうが逆さまなんだ。",
    "聞いて。雨が上に降るときだけ鐘が鳴るの。",
    "じゃあ空が乾く前に急がないと。",
    "おばあちゃんは、門は正直な旅人に開くって言ってた。",
    "正直、高いところはちょっと怖い。",
    "私も。一緒に下を見ないようにしよう。",
    "あそこに浮かんでるの、灯台？",
    "郵便局だよ。火曜日は移動するんだ。",
    "書いておいて。帰り道を忘れちゃうから。",
    "次は紙ちょうちんの果樹園だ。",
]
# Accent-heavy English for the cp1252 file, so encoding detection has signal.
EN_CP1252 = [
    "Zoë, the café on the façade is drifting again!",
    "Señora Ibáñez says it's déjà vu, nothing more.",
    "Don't be naïve — bridges don't just walk away.",
    "“Turn left at the crème brûlée stand,” she said.",
    "I'm sure the piñata was tied down… mostly.",
    "Rendezvous at the gazebo, señor. À bientôt!",
    "Müller’s map shows the garden façade upside down.",
    "That’s the coöperative orchard — très jolie.",
    "Ça va? You look pâle.",
    "The résumé says: “expert in loose gravity.”",
]
POOLS: Dict[str, List[str]] = {
    "en": EN, "ar": AR, "ja": JA, "en_cp1252": EN_CP1252,
    "fr": ["Le vent change encore de direction.",
           "As-tu arrosé les fougères des nuages ce matin ?",
           "Attends, le pont dérive toujours.",
           "Nous avons jusqu'au coucher du soleil."],
    "de": ["Der Wind dreht sich schon wieder.",
           "Hast du heute Morgen die Wolkenfarne gegossen?",
           "Warte, die Brücke treibt immer noch.",
           "Wir haben bis Sonnenuntergang Zeit."],
    "zh": ["风向又变了。", "你今天早上给云蕨浇水了吗？",
           "等等，桥还在漂。", "日落之前我们得赶到上面的露台。"],
}
SIGNS: Dict[str, List[str]] = {
    "en": ["UPPER TERRACE", "POST OFFICE - OPEN TUESDAYS", "CLOUD FERN NURSERY",
           "CAUTION: LOOSE GRAVITY", "LANTERN ORCHARD 2 KM", "TEA HOUSE"],
    "ar": ["الشرفة العليا", "مكتب البريد - مفتوح أيام الثلاثاء", "مشتل سرخس الغيوم",
           "تحذير: جاذبية غير مستقرة", "بستان الفوانيس 2 كم", "بيت الشاي"],
    "ja": ["上のテラス", "郵便局（火曜日営業）", "雲のシダ園", "注意：重力不安定"],
    "fr": ["TERRASSE HAUTE"], "de": ["OBERE TERRASSE"], "zh": ["上层露台"],
}
LYRICS: Dict[str, List[str]] = {
    "en": ["Fly, little kite, over the folded hills",
           "Carry the rain up to the garden in the sky"],
    "ar": ["حلّقي يا طائرة الورق فوق التلال المطوية", "احملي المطر إلى حديقة السماء"],
    "ja": ["折りたたんだ丘を越えて飛べ", "雨を空の庭まで運んで"],
}
KARAOKE = [  # invented romaji, (syllable, centiseconds)
    [("so", 22), ("ra", 28), (" no ", 18), ("ni", 24), ("wa", 30), (" e", 40)],
    [("ka", 20), ("ze", 26), (" ni ", 18), ("no", 22), ("t", 10), ("te", 36)],
]
ACTORS = ["Mira", "Tobin", "Grandmother Ise"]

# ----------------------------------------
# Style sets: (name, expected class, line pattern, line count)
# ----------------------------------------
FULL_AR = [
    ("Default", "dialogue", "talk_drawn", 12),  # 2 of 12 lines are \p1 drawings
    ("Thoughts", "dialogue", "talk_fade", 3),   # \fad on dialogue is NOT a sign
    ("محاكاة", "sign", "sign_pos", 3),
    ("لافتة", "sign", "sign_an8", 2),           # caught by name only
    ("Shopfront", "sign", "sign_pos", 2),       # caught by tags only
    ("Ticker", "sign", "sign_move", 2),
    ("Window Mask", "sign", "sign_clip", 2),
    ("Emblem", "sign", "sign_draw", 2),
    ("Glow", "sign", "sign_t", 2),
    ("OP Romaji", "karaoke", "karaoke", 2),
    ("أغنية النهاية", "song", "lyric", 2),
]
FULL_EN = [
    ("Default", "dialogue", "talk_drawn", 12),
    ("Thoughts", "dialogue", "talk_fade", 3),
    ("Signe", "dialogue", "talk", 2),           # a character name containing "sign"
    ("Signs", "sign", "sign_an8", 2),
    ("Shopfront", "sign", "sign_pos", 2),
    ("Ticker", "sign", "sign_move", 2),
    ("Window Mask", "sign", "sign_clip", 2),
    ("Emblem", "sign", "sign_draw", 2),
    ("Glow", "sign", "sign_t", 2),
    ("OP Romaji", "karaoke", "karaoke", 2),
    ("OP English", "song", "lyric", 2),         # no positioning tags, weak name
    ("ED", "song", "lyric", 2),
]
BASIC_AR = [("Default", "dialogue", "talk", 12), ("Alt", "dialogue", "talk", 3),
            ("محاكاة", "sign", "sign_pos", 3), ("لافتة", "sign", "sign_an8", 2)]
BASIC_EN = [("Default", "dialogue", "talk", 12), ("Alt", "dialogue", "talk", 3),
            ("Signs", "sign", "sign_pos", 3), ("ED", "song", "lyric", 2)]
BASIC_JA = [("Default", "dialogue", "talk", 12), ("看板", "sign", "sign_pos", 3),
            ("OP", "karaoke", "karaoke", 2)]
MIXED_EN = [("Default", "dialogue", "talk", 12), ("Alt", "dialogue", "talk", 3),
            ("Shopfront", "sign", "sign_pos", 3), ("OP Romaji", "karaoke", "karaoke", 2)]
PLAIN_EN = [("Default", "dialogue", "talk", 12), ("Signs", "sign", "sign_an8", 2)]
TRACK_STYLES = [("Default", "dialogue", "talk", 8), ("Signs", "sign", "sign_pos", 1)]
BULK_STYLES = [("Default", "dialogue", "talk", 3), ("Signs", "sign", "sign_pos", 1)]

ASS_STYLE_FIELDS = [
    "Name", "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour",
    "OutlineColour", "BackColour", "Bold", "Italic", "Underline", "StrikeOut",
    "ScaleX", "ScaleY", "Spacing", "Angle", "BorderStyle", "Outline", "Shadow",
    "Alignment", "MarginL", "MarginR", "MarginV", "Encoding",
]
SHUFFLED_STYLE_FIELDS = [  # all 23 fields, non-canonical order
    "Name", "Fontsize", "Fontname", "PrimaryColour", "Bold", "Italic",
    "Underline", "StrikeOut", "SecondaryColour", "OutlineColour", "BackColour",
    "ScaleX", "ScaleY", "Spacing", "Angle", "BorderStyle", "Shadow", "Outline",
    "Alignment", "MarginL", "MarginR", "MarginV", "Encoding",
]
SSA_STYLE_FIELDS = [
    "Name", "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour",
    "TertiaryColour", "BackColour", "Bold", "Italic", "BorderStyle", "Outline",
    "Shadow", "Alignment", "MarginL", "MarginR", "MarginV", "AlphaLevel", "Encoding",
]
EVENT_FIELDS = ["Layer", "Start", "End", "Style", "Name", "MarginL", "MarginR",
                "MarginV", "Effect", "Text"]
SHUFFLED_EVENT_FIELDS = ["Layer", "Start", "End", "Name", "Style", "Effect",
                         "MarginL", "MarginR", "MarginV", "Text"]
TEXT_FIRST_EVENT_FIELDS = ["Text"] + EVENT_FIELDS[:-1]

BOMS = {"utf-8": codecs.BOM_UTF8, "utf-16-le": codecs.BOM_UTF16_LE,
        "utf-16-be": codecs.BOM_UTF16_BE}
SIDECAR_ONLY_DURATION = 12.0

# ----------------------------------------
# Library layout (all names invented)
# ----------------------------------------
SKY = "Sky Garden (2031)"
SKY01 = "[DemoSubs] Sky Garden - 01 [1080p]"
SKY02 = "[DemoSubs] Sky Garden - 02 [720p]"
HAD = "حديقة السماء (2030)"
HAD01 = "حديقة السماء - الحلقة 01"
HAD02 = "حديقة السماء - الحلقة 02"
LAN = "Lantern Harbor Diaries"
LAN01 = "Lantern Harbor Diaries - 01"
VCP = "[DemoSubs] Velvet Comet Patrol [BD 1080p]"
LONG_DIR = ("Long Path Test/"
            "Pocket Orbit Chronicles - The Unreasonably Long Collector's Shelf Edition (Demo Library)/"
            "Season 01 - Twelve Episodes About Very Small Satellites That Keep Getting Lost/"
            "Specials, Extras and Bonus Material")
LONG_STEM = ("[DemoSubs] Pocket Orbit Chronicles - S01E01 - "
             "The Satellite Who Forgot Which Way Was Up [720p]")

VIDEOS: List[Dict[str, Any]] = [
    {"path": f"{SKY}/{SKY01}.mkv", "source": "testsrc2", "size": (1920, 1080),
     "duration": 12.0, "tone": 440, "title": "SKY GARDEN - EP 01 - DEMO", "episode": 1,
     "tracks": [("ara", "ass", "Arabic"), ("eng", "ass", "English"),
                ("jpn", "ass", "Japanese"), ("fre", "ass", "French"),
                ("ger", "ass", "German"), ("chi", "ass", "Chinese"),
                (None, "ass", "Untagged"), ("eng", "pgs", "English (PGS image)")]},
    {"path": f"{SKY}/{SKY02}.mkv", "source": "smptebars", "size": (1280, 720),
     "duration": 10.0, "tone": None, "title": "SKY GARDEN - EP 02 - DEMO", "episode": 2,
     "tracks": [("ara", "ass", "Arabic"), ("eng", "ass", "English"),
                ("eng", "ass", "Signs & Songs")]},
    {"path": f"{HAD}/{HAD01}.mkv", "source": "color", "size": (1280, 720),
     "duration": 8.0, "tone": 330, "title": "HADIQAT AS-SAMA - EP 01 - DEMO", "episode": 1,
     "tracks": [("ara", "ass", "Arabic")]},
    {"path": f"{LAN}/{LAN01}.mp4", "source": "color", "size": (1280, 720),
     "duration": 10.0, "tone": 523, "title": "LANTERN HARBOR DIARIES - EP 01 - DEMO",
     "episode": 3, "tracks": [("eng", "mov_text", "English")]},
    {"path": f"{LONG_DIR}/{LONG_STEM}.mkv", "source": "testsrc2", "size": (1280, 720),
     "duration": 6.0, "tone": None, "title": "POCKET ORBIT - SPECIAL 01 - DEMO", "episode": 5,
     "tracks": [("ara", "ass", "Arabic")]},
]
TRACK_LANG = {"ara": "ar", "eng": "en", "jpn": "ja", "fre": "fr", "ger": "de",
              "chi": "zh", None: "en"}
CODEC_NAMES = {"ass": "ass", "pgs": "hdmv_pgs_subtitle", "mov_text": "mov_text"}


def _sc(path: str, lang: str, styles: list, encoding: str, bom: bool, eol: str,
        res: Tuple[int, Optional[int]], duration: float, episode: int,
        **extra: Any) -> Dict[str, Any]:
    """One sidecar spec (see SIDECARS)."""
    return {"path": path, "lang": lang, "styles": styles, "encoding": encoding,
            "bom": bom, "eol": eol, "res": res, "duration": duration,
            "episode": episode, **extra}


_S = SIDECAR_ONLY_DURATION
SIDECARS: List[Dict[str, Any]] = [
    _sc(f"{SKY}/{SKY01}.ar.ass", "ar", FULL_AR, "utf-8", True, "CRLF", (1920, 1080),
        12.0, 1, fonts=True, extradata=True),
    _sc(f"{SKY}/{SKY01}.en.ass", "en", FULL_EN, "utf-8", False, "LF", (1920, 1080),
        12.0, 1),
    _sc(f"{SKY}/{SKY02}.ar.ass", "ar", BASIC_AR, "cp1256", False, "CRLF", (1280, 720),
        10.0, 2),
    _sc(f"{SKY}/{SKY02}.en.ass", "en", BASIC_EN, "cp1252", False, "CRLF", (1280, 720),
        10.0, 2, pool="en_cp1252"),
    _sc(f"{HAD}/{HAD01}.ar.ass", "ar", BASIC_AR, "utf-16-le", True, "CRLF", (1280, 720),
        8.0, 1),
    _sc(f"{HAD}/{HAD01}.en.ass", "en", BASIC_EN, "utf-16-be", True, "LF", (1280, 720),
        8.0, 1),
    _sc(f"{HAD}/{HAD02}.ar.ass", "ar", BASIC_AR, "utf-16-le", False, "CRLF", (1280, 720),
        _S, 2, expect={"decodable": False, "restyled": False}),
    _sc(f"{LAN}/{LAN01}.ja.ass", "ja", BASIC_JA, "shift_jis", False, "CRLF", (1280, 720),
        10.0, 3),
    _sc(f"{LAN}/{LAN01}.en.ass", "en", PLAIN_EN, "utf-8", False, "LF", (1920, 816),
        10.0, 3, traits={"scope": "2.35:1"}),
    _sc(f"{VCP}/{{vcp}} - 01 [BD 1080p][{{crc}}].en.ass", "en", PLAIN_EN, "utf-8", True,
        "CRLF", (1280, None), _S, 4, sbs=False,
        traits={"missing_playresy": True, "missing_scaled_border": True}),
    _sc(f"{VCP}/{{vcp}} - 02 [BD 1080p][{{crc}}].en.ass", "en", BASIC_EN, "utf-8", False,
        "LF", (1920, 1080), _S, 5, variant="malformed",
        traits={"malformed_rows": ["short_style_row", "dialogue_missing_fields",
                                   "orphan_text_line_at_column_0"]}),
    _sc(f"{VCP}/{{vcp}} - 03 [BD 1080p][{{crc}}].ass", "en", PLAIN_EN, "utf-8", False,
        "CRLF", (640, 480), _S, 6, variant="ssa_v4", traits={"ssa_v4": True},
        expect={"decodable": True, "restyled": False}),
    _sc(f"{VCP}/{{vcp}} - 04 [BD 1080p][{{crc}}].en.ass", "en", MIXED_EN, "utf-8", True,
        "LF", (1920, 1080), _S, 7, variant="shuffled",
        traits={"non_canonical_format_order": ["styles", "events"]}),
    _sc(f"{VCP}/{{vcp}} - 05 [BD 1080p][{{crc}}].en.ass", "en", PLAIN_EN, "utf-8", False,
        "LF", (1920, 1080), _S, 8, variant="text_first",
        traits={"malformed_rows": ["events_format_text_at_column_0"]}),
    _sc(f"{LONG_DIR}/{LONG_STEM}.ar.ass", "ar", BASIC_AR, "utf-8", True, "CRLF",
        (1280, 720), 6.0, 5, traits={"long_path": True}),
    _sc(f"{LONG_DIR}/{LONG_STEM}.en.ass", "en", BASIC_EN, "utf-8", False, "LF",
        (1280, 720), 6.0, 5, traits={"long_path": True}),
]


# ----------------------------------------
# Helpers
# ----------------------------------------
def _fs(path: Path) -> str:
    """OS path string that also works past MAX_PATH on Windows (\\\\?\\ prefix)."""
    s = os.path.abspath(str(path))
    if os.name == "nt" and not s.startswith("\\\\?\\"):
        s = "\\\\?\\UNC\\" + s[2:] if s.startswith("\\\\") else "\\\\?\\" + s
    return s


def _write(path: Path, data: bytes) -> None:
    os.makedirs(_fs(path.parent), exist_ok=True)
    with open(_fs(path), "wb") as f:
        f.write(data)


def _ts(seconds: float) -> str:
    """ASS timestamp H:MM:SS.cc."""
    cs = int(round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass_uuencode(data: bytes) -> List[str]:
    """ASS attachment encoding: 6-bit groups + 33, 80-char lines."""
    out: List[str] = []
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3]
        n = int.from_bytes(chunk.ljust(3, b"\0"), "big")
        chars = [chr(((n >> s) & 63) + 33) for s in (18, 12, 6, 0)]
        out.extend(chars[:len(chunk) + 1])
    text = "".join(out)
    return [text[i:i + 80] for i in range(0, len(text), 80)]


def encode_doc(text: str, encoding: str, bom: bool, eol: str) -> bytes:
    """`text` (LF-joined) -> file bytes with the requested EOL / BOM / codec."""
    if eol == "CRLF":
        text = text.replace("\n", "\r\n")
    return (BOMS[encoding] if bom else b"") + text.encode(encoding)


# ----------------------------------------
# ASS builder
# ----------------------------------------
def _style_values(name: str, kind: str, pattern: str, lang: str, res_y: int,
                  rng: random.Random) -> Dict[str, str]:
    """Field values for one Style row (superset of ASS V4+ and SSA v4 names)."""
    dialogue = kind == "dialogue"
    font = {"ja": "Yu Gothic"}.get(lang, "Arial") if dialogue else (
        "Tahoma" if lang == "ar" else "Georgia" if kind == "sign" else "Trebuchet MS")
    colour = "&H00FFFFFF" if dialogue else f"&H00{rng.randrange(0x1000000):06X}"
    align = {"sign": "7", "song": "8", "karaoke": "8"}.get(kind, "2")
    return {
        "Name": name, "Fontname": font,
        "Fontsize": str(round(res_y * (0.065 if dialogue else 0.05))),
        "PrimaryColour": colour, "SecondaryColour": "&H000000FF",
        "OutlineColour": "&H00302A26", "BackColour": "&H80000000",
        "TertiaryColour": "&H00302A26", "AlphaLevel": "0",
        "Bold": "0", "Italic": "-1" if pattern == "talk_fade" else "0",
        "Underline": "0", "StrikeOut": "0", "ScaleX": "100", "ScaleY": "100",
        "Spacing": "0", "Angle": "0", "BorderStyle": "1",
        "Outline": f"{res_y / 400:.1f}", "Shadow": f"{res_y / 1080:.1f}",
        "Alignment": align, "MarginL": "60", "MarginR": "60",
        "MarginV": str(round(res_y / 27)),
        "Encoding": {"ar": "178", "ja": "128"}.get(lang, "1"),
    }


def _ssa_colour(ass: str) -> str:
    """&HAABBGGRR -> SSA decimal BGR."""
    return str(int(ass[4:], 16))


def _line_text(pattern: str, k: int, lang: str, pool: List[str], start: int,
               w: int, h: int, rng: random.Random) -> str:
    """The Text column for line `k` of a style using `pattern`."""
    line = pool[(start + k) % len(pool)]
    sign = SIGNS[lang][(start + k) % len(SIGNS[lang])]
    x, y = rng.randrange(w // 10, w * 6 // 10), rng.randrange(h // 10, h * 6 // 10)
    if pattern in ("talk", "talk_drawn"):
        return line
    if pattern == "talk_fade":
        return "{\\fad(150,150)}" + line
    if pattern == "sign_an8":
        return "{\\an8}" + sign
    if pattern == "sign_pos":
        return f"{{\\an7\\pos({x},{y})\\fad(200,200)}}{sign}"
    if pattern == "sign_move":
        return f"{{\\an5\\move({x},{y},{x + w // 5},{y},0,1500)}}{sign}"
    if pattern == "sign_clip":
        return f"{{\\an5\\clip({w // 4},{h // 3},{w * 3 // 4},{h // 2})}}{sign}"
    if pattern == "sign_draw":
        s = h // 10
        return f"{{\\an7\\p1}}m 0 0 l {s} 0 {s} {s} 0 {s}{{\\p0}}"
    if pattern == "sign_t":
        return "{\\an8\\t(0,600,\\fscx130\\fscy130\\blur2)}" + sign
    if pattern == "karaoke":
        return "".join(f"{{\\k{d}}}{syl}" for syl, d in KARAOKE[k % len(KARAOKE)])
    if pattern == "lyric":
        lyrics = LYRICS.get(lang, LYRICS["en"])
        return "{\\an8\\fad(300,300)}" + lyrics[k % len(lyrics)]
    raise ValueError(f"unknown line pattern {pattern!r}")


def build_ass(spec: Dict[str, Any], rng: random.Random) -> str:
    """Render one synthetic subtitle document (LF line endings)."""
    lang, variant = spec["lang"], spec.get("variant")
    pool = POOLS[spec.get("pool", lang)]
    w, h = spec["res"]
    res_y = h or round(w * 3 / 4)
    dur = spec["duration"]
    ssa = variant == "ssa_v4"
    style_fields = (SSA_STYLE_FIELDS if ssa else
                    SHUFFLED_STYLE_FIELDS if variant == "shuffled" else ASS_STYLE_FIELDS)
    event_fields = (SHUFFLED_EVENT_FIELDS if variant == "shuffled" else
                    TEXT_FIRST_EVENT_FIELDS if variant == "text_first" else EVENT_FIELDS)
    if ssa:
        event_fields = ["Marked"] + EVENT_FIELDS[1:]
    offset = spec["episode"] * 4

    out = ["[Script Info]",
           "; Synthetic demo subtitle - every line is invented (tools/make_demo_library.py).",
           f"Title: {spec.get('title', Path(spec['path']).stem)}",
           f"ScriptType: {'v4.00' if ssa else 'v4.00+'}", "WrapStyle: 0"]
    if spec.get("sbs", True) and not ssa:
        out.append("ScaledBorderAndShadow: yes")
    out.append(f"PlayResX: {w}")
    if h:
        out.append(f"PlayResY: {h}")
    if spec.get("fonts"):
        out.append("; The [Fonts] payload is RANDOM BYTES labelled as a fake font, not a real font.")
    out.append("")
    if spec.get("extradata"):
        out += ["[Aegisub Project Garbage]",
                f"Video File: ?dummy:24.000000:{int(dur * 24)}:{w}:{res_y}:47:163:254:",
                "Video Position: 0", ""]

    out += ["[V4 Styles]" if ssa else "[V4+ Styles]", "Format: " + ", ".join(style_fields)]
    for order, (name, kind, pattern, _n) in enumerate(spec["styles"]):
        vals = _style_values(name, kind, pattern, lang, res_y, rng)
        if ssa:
            for key in ("PrimaryColour", "SecondaryColour", "TertiaryColour", "BackColour"):
                vals[key] = _ssa_colour(vals[key])
            vals["Alignment"] = "6" if kind != "dialogue" else "2"
        row = ",".join(vals[f] for f in style_fields)
        if variant == "malformed" and order == 1:
            row = ",".join(vals[f] for f in style_fields[:4])  # short Style row
        out.append(f"Style: {row}")
    out.append("")

    if spec.get("fonts"):
        out += ["[Fonts]", "fontname: DemoFake-NotARealFont_0.ttf"]
        out += ass_uuencode(rng.randbytes(1536)) + [""]

    events: List[Tuple[int, int, int, str]] = []
    for order, (name, kind, pattern, count) in enumerate(spec["styles"]):
        layer = {"dialogue": 0, "sign": 1}.get(kind, 2)
        for k in range(count):
            if order == 0:  # main dialogue: contiguous slots, so mid-video shows a line
                slot = (dur - 0.6) / count
                start, end = 0.4 + k * slot, 0.4 + (k + 1) * slot - 0.05
            else:
                start = round(rng.uniform(0.2, max(0.3, dur - 2.2)), 2)
                end = min(dur - 0.05, start + rng.uniform(1.5, 2.0))
            text = _line_text(pattern, k, lang, pool, offset + 7 * order, w, res_y, rng)
            if pattern == "talk_drawn" and k in (3, 8):
                text = "{\\p1}m 0 0 l 40 0 40 40 0 40{\\p0}"
            if variant == "text_first":
                text = text.replace(",", "")
            if order == 2 and k == 0 and spec.get("extradata"):
                text = "{=1}" + text
            vals = {"Layer": str(layer), "Marked": "Marked=0", "Start": _ts(start),
                    "End": _ts(end), "Style": name,
                    "Name": ACTORS[k % len(ACTORS)] if kind == "dialogue" else "",
                    "MarginL": "0", "MarginR": "0", "MarginV": "0", "Effect": "",
                    "Text": text}
            row = "Dialogue: " + ",".join(vals[f] for f in event_fields)
            events.append((int(round(start * 100)), layer, order, row))
    rows = [row for *_key, row in sorted(events)]
    if variant == "malformed":
        rows[1:1] = ["and then the bridge simply walked away."]  # text at column 0
        rows += [f"Dialogue: 0,{_ts(dur - 2)},{_ts(dur - 1)},Default",
                 f"Dialogue: 0,{_ts(dur - 1)},{_ts(dur - 0.5)},Default,,0,0"]
    out += ["[Events]", "Format: " + ", ".join(event_fields)] + rows + [""]

    if spec.get("extradata"):
        blob = "".join(ass_uuencode(rng.randbytes(48)))
        out += ["[Aegisub Extradata]", "Data: 1,demo_note,eSynthetic demo extradata",
                f"Data: 2,demo_blob,u{blob}", ""]
    return "\n".join(out)


# ----------------------------------------
# PGS (.sup) writer - ffmpeg has no PGS encoder
# ----------------------------------------
def _pgs_rle_row(row: List[int]) -> bytes:
    """Run-length encode one bitmap row (HDMV PGS RLE), with end-of-line."""
    out = bytearray()
    i = 0
    while i < len(row):
        c, n = row[i], 1
        while i + n < len(row) and row[i + n] == c and n < 16383:
            n += 1
        if c == 0:
            out += bytes([0, n]) if n < 64 else bytes([0, 0x40 | n >> 8, n & 0xFF])
        elif n < 3:
            out += bytes([c]) * n
        elif n < 64:
            out += bytes([0, 0x80 | n, c])
        else:
            out += bytes([0, 0xC0 | n >> 8, n & 0xFF, c])
        i += n
    return bytes(out) + b"\0\0"


def make_pgs(width: int, height: int, start: float, end: float) -> bytes:
    """Minimal valid .sup: one display set showing a bordered caption box at
    `start` (PCS, WDS, PDS, ODS, END) and one clearing it at `end`."""
    def seg(kind: int, pts: int, payload: bytes) -> bytes:
        return b"PG" + struct.pack(">IIBH", pts, 0, kind, len(payload)) + payload

    bw, bh = 480, 64
    x, y = (width - bw) // 2, height - bh - height // 12
    rows = []
    for r in range(bh):
        if r < 4 or r >= bh - 4:
            rows.append([2] * bw)
        else:
            mid = [3 if bh // 3 <= r < 2 * bh // 3 and (c // 24) % 2 else 1
                   for c in range(bw - 8)]
            rows.append([2] * 4 + mid + [2] * 4)
    rle = b"".join(_pgs_rle_row(r) for r in rows)
    palette = bytes([0, 16, 128, 128, 0,      # id, Y, Cr, Cb, alpha: transparent
                     1, 235, 128, 128, 255,   # white fill
                     2, 32, 128, 128, 255,    # near-black border
                     3, 170, 165, 44, 255])   # amber stripes
    t_on, t_off = round(start * 90000), round(end * 90000)
    window = struct.pack(">BBHHHH", 1, 0, x, y, bw, bh)
    ods_data = struct.pack(">HH", bw, bh) + rle
    show = (
        seg(0x16, t_on, struct.pack(">HHBHBBBB", width, height, 0x10, 0, 0x80, 0, 0, 1)
            + struct.pack(">HBBHH", 0, 0, 0, x, y))
        + seg(0x17, t_on, window)
        + seg(0x14, t_on, b"\0\0" + palette)
        + seg(0x15, t_on, struct.pack(">HBB", 0, 0, 0xC0)
              + len(ods_data).to_bytes(3, "big") + ods_data)
        + seg(0x80, t_on, b"")
    )
    clear = (
        seg(0x16, t_off, struct.pack(">HHBHBBBB", width, height, 0x10, 1, 0, 0, 0, 0))
        + seg(0x17, t_off, window)
        + seg(0x80, t_off, b"")
    )
    return show + clear


# ----------------------------------------
# Videos
# ----------------------------------------
def _run(cmd: List[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode:
        err = proc.stderr.decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"{Path(cmd[0]).name} failed ({proc.returncode}):\n{err}")
    return proc


def make_video(spec: Dict[str, Any], outdir: Path, staging: Path, seed: str,
               ffmpeg: str, ffprobe: str) -> Dict[str, Any]:
    """Encode one short H.264 video with muxed subtitle tracks; verify with
    ffprobe; move it into place (staging keeps ffmpeg away from long/Arabic paths)."""
    (w, h), dur, tone = spec["size"], spec["duration"], spec["tone"]
    mp4 = spec["path"].endswith(".mp4")
    vsrc = {"testsrc2": f"testsrc2=s={w}x{h}:r=24:d={dur}",
            "smptebars": f"smptebars=s={w}x{h}:r=24:d={dur}",
            "color": f"color=c=0x24365a:s={w}x{h}:r=24:d={dur}"}[spec["source"]]
    vsrc += (f",drawtext=text='{spec['title']}':fontsize={h // 14}:fontcolor=white"
             f":borderw=3:bordercolor=black:x=(w-tw)/2:y=h/10")
    # -copyts: keep the authored subtitle times (ffmpeg otherwise rebases each
    # subtitle input so its FIRST event lands at 0).
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-copyts",
           "-f", "lavfi", "-i", vsrc]
    if tone:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000:duration={dur}"]
    for k, (lang, codec, title) in enumerate(spec["tracks"]):
        src = staging / f"track{k}.{'sup' if codec == 'pgs' else 'ass'}"
        if codec == "pgs":
            data = make_pgs(w, h, 1.0, dur - 1.0)
        else:
            track = _sc(spec["path"], TRACK_LANG[lang], TRACK_STYLES, "utf-8", False, "LF",
                        (w, h), dur, spec["episode"], title=f"{spec['title']} ({title})")
            data = encode_doc(build_ass(track, random.Random(f"{seed}:{spec['path']}:{k}")),
                              "utf-8", False, "LF")
        _write(src, data)
        cmd += ["-i", str(src)]
    first_sub = 2 if tone else 1
    cmd += ["-map", "0:v"] + (["-map", "1:a"] if tone else [])
    for k in range(len(spec["tracks"])):
        cmd += ["-map", f"{first_sub + k}:s"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-pix_fmt", "yuv420p",
            "-g", "48", "-c:s", "mov_text" if mp4 else "copy"]
    if tone:
        cmd += ["-c:a", "aac", "-b:a", "96k"]
    for k, (lang, _codec, title) in enumerate(spec["tracks"]):
        if lang:
            cmd += [f"-metadata:s:s:{k}", f"language={lang}"]
        cmd += [f"-metadata:s:s:{k}", f"title={title}"]
    staged = staging / ("out.mp4" if mp4 else "out.mkv")
    cmd += ["-metadata", f"title={spec['title']}", "-t", str(dur), "-fflags", "+bitexact",
            "-flags:v", "+bitexact", "-flags:a", "+bitexact", str(staged)]
    _run(cmd)

    probe = json.loads(_run([ffprobe, "-v", "error", "-print_format", "json",
                             "-show_streams", str(staged)]).stdout)
    subs = [s for s in probe.get("streams", []) if s.get("codec_type") == "subtitle"]
    got = [(s.get("tags", {}).get("language"), s.get("codec_name")) for s in subs]
    want = [(lang or ("und" if mp4 else None), CODEC_NAMES[codec])
            for lang, codec, _t in spec["tracks"]]
    if got != want:
        raise RuntimeError(f"{spec['path']}: ffprobe tracks {got} != expected {want}")

    final = outdir / spec["path"]
    os.makedirs(_fs(final.parent), exist_ok=True)
    os.replace(_fs(staged), _fs(final))
    return {
        "path": spec["path"], "kind": "video", "container": "mp4" if mp4 else "mkv",
        "size": os.path.getsize(_fs(final)), "resolution": [w, h], "fps": 24,
        "duration": dur, "video_source": spec["source"],
        "audio": f"sine {tone} Hz" if tone else "none",
        "traits": {"embedded_tracks": [
            {"index": s["index"], "codec": s.get("codec_name"),
             "language": s.get("tags", {}).get("language"),
             "title": s.get("tags", {}).get("title")} for s in subs]},
    }


# ----------------------------------------
# Sidecars, bulk, manifest, self-check
# ----------------------------------------
def write_sidecar(spec: Dict[str, Any], outdir: Path, seed: str, kind: str) -> Dict[str, Any]:
    """Build, encode and write one .ass; return its manifest entry."""
    data = encode_doc(build_ass(spec, random.Random(f"{seed}:{spec['path']}")),
                      spec["encoding"], spec["bom"], spec["eol"])
    _write(outdir / spec["path"], data)
    w, h = spec["res"]
    return {
        "path": spec["path"], "kind": kind, "language": spec["lang"],
        "encoding": spec["encoding"], "bom": spec["bom"], "line_ending": spec["eol"],
        "size": len(data), "playres": [w, h],
        "styles": {name: cls for name, cls, _p, _n in spec["styles"]},
        "traits": {**spec.get("traits", {}),
                   **({"embedded_fonts": True} if spec.get("fonts") else {}),
                   **({"extradata": True} if spec.get("extradata") else {})},
        "expect": spec.get("expect", {"decodable": True, "restyled": True}),
    }


def self_check(outdir: Path, entries: List[Dict[str, Any]]) -> List[str]:
    """Re-read every subtitle file: BOM, strict decode and line endings must
    match the manifest. Returns a list of failures (empty = OK)."""
    failures = []
    for e in entries:
        p = outdir / e["path"]
        if e["kind"] == "video":
            if not os.path.isfile(_fs(p)):
                failures.append(f"{e['path']}: missing")
            continue
        with open(_fs(p), "rb") as f:
            raw = f.read()
        bom = BOMS.get(e["encoding"], b"")
        if bool(bom) and raw.startswith(bom) != e["bom"]:
            failures.append(f"{e['path']}: BOM presence != {e['bom']}")
            continue
        try:
            text = raw[len(bom) if e["bom"] else 0:].decode(e["encoding"])
        except UnicodeDecodeError as exc:
            failures.append(f"{e['path']}: does not decode as {e['encoding']}: {exc}")
            continue
        crlf, lf, cr = text.count("\r\n"), text.count("\n"), text.count("\r")
        ok = (crlf == lf == cr > 0) if e["line_ending"] == "CRLF" else (cr == 0 < lf)
        if not ok:
            failures.append(f"{e['path']}: line endings are not pure {e['line_ending']}")
    return failures


def _resolve_tools(ffmpeg_arg: Optional[str]) -> Tuple[str, str]:
    ffmpeg = ffmpeg_arg or shutil.which("ffmpeg")
    if not ffmpeg or not (os.path.isfile(ffmpeg) or shutil.which(ffmpeg)):
        sys.exit("ffmpeg not found: pass --ffmpeg PATH, put it on PATH, or use --no-video")
    ffmpeg = shutil.which(ffmpeg) or ffmpeg
    sibling = Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)
    ffprobe = str(sibling) if sibling.is_file() else shutil.which("ffprobe")
    if not ffprobe:
        sys.exit("ffprobe not found next to ffmpeg or on PATH")
    return ffmpeg, ffprobe


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("outdir", type=Path, help="output folder (must be absent or empty)")
    ap.add_argument("--no-video", action="store_true", help="skip ffmpeg videos")
    ap.add_argument("--bulk", type=int, default=0, metavar="N",
                    help="also write N small UTF-8 .ass files (~50 per folder)")
    ap.add_argument("--seed", default="1", help="RNG seed (default 1)")
    ap.add_argument("--ffmpeg", metavar="PATH", help="ffmpeg binary (default: PATH)")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Arabic paths on a piped cp125x console
        stream.reconfigure(errors="backslashreplace")

    t0 = time.perf_counter()
    outdir = args.outdir.resolve()
    if outdir.exists() and any(outdir.iterdir()):
        sys.exit(f"refusing to write into non-empty {outdir}")
    tools = None if args.no_video else _resolve_tools(args.ffmpeg)
    outdir.mkdir(parents=True, exist_ok=True)
    seed = str(args.seed)
    crc = f"{random.Random(f'{seed}:crc').getrandbits(32):08X}"

    entries: List[Dict[str, Any]] = []
    for spec in SIDECARS:
        spec = {**spec, "path": spec["path"].format(vcp="[DemoSubs] Velvet Comet Patrol",
                                                    crc=crc)}
        entries.append(write_sidecar(spec, outdir, seed, "sidecar"))
    long_rel = next(e["path"] for e in entries if e["traits"].get("long_path"))
    if len(long_rel) <= 260:
        raise RuntimeError(f"long-path fixture is only {len(long_rel)} chars")

    if tools:
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=outdir) as tmp:
            for spec in VIDEOS:
                entries.append(make_video(spec, outdir, Path(tmp), seed, *tools))

    folders = max(1, math.ceil(args.bulk / 50))
    for i in range(args.bulk):
        show = f"Bulk Show {i % folders:03d}"
        spec = _sc(f"Bulk Library/{show}/{show} - {i // folders + 1:03d}.ass", "en",
                   BULK_STYLES, "utf-8", False, "LF", (1920, 1080), 24.0, i)
        entries.append(write_sidecar(spec, outdir, seed, "bulk"))

    manifest = {
        "generator": "tools/make_demo_library.py", "seed": seed,
        "note": "Synthetic, copyright-free demo library: all titles, dialogue, signs "
                "and lyrics are invented; [Fonts] payloads are random bytes, not fonts.",
        "classes": "styles map name -> dialogue | sign | song | karaoke; the app "
                   "should restyle only 'dialogue' styles",
        "files": entries,
    }
    _write(outdir / "manifest.json",
           json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8") + b"\n")

    failures = self_check(outdir, entries)
    for msg in failures:
        print(f"SELF-CHECK FAIL: {msg}", file=sys.stderr)
    counts = {k: sum(e["kind"] == k for e in entries) for k in ("video", "sidecar", "bulk")}
    total = sum(e["size"] for e in entries)
    print(f"demo library: {counts['video']} videos, {counts['sidecar']} sidecars, "
          f"{counts['bulk']} bulk | {total / 1e6:.2f} MB | "
          f"{time.perf_counter() - t0:.1f} s | self-check "
          f"{'FAILED' if failures else 'OK'} -> {outdir}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
