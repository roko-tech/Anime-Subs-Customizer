"""Anime Subs Customizer — GUI entry point.

All non-GUI logic (config, DB, ASS engine, FFmpeg, fonts, folder monitor)
lives in anime_subs_core. This module is the Tk/customtkinter UI, the system
tray, and the process entry point.

    pythonw anime_subs_customizer.pyw [--minimized]
"""
from anime_subs_core import *  # noqa: F401,F403  (re-export the whole core API)
from anime_subs_core import _next_font_index  # underscore name: not re-exported by *

# Arabic shaping + bidi for the PIL-rendered preview. Pillow only shapes RTL
# text itself when built with libraqm (stock Windows wheels aren't); these
# pure-Python fallbacks cover that. Optional — without either, RTL preview
# text renders unshaped.
try:
    from PIL import features as _pil_features
    _PIL_RAQM = bool(_pil_features.check("raqm"))
except Exception:  # noqa: BLE001
    _PIL_RAQM = False
try:
    import arabic_reshaper as _arabic_reshaper_mod
    from bidi.algorithm import get_display as _bidi_display
    # Optional presentation-form ligatures (e.g. U+FDF2 "Allah") are missing
    # from many fonts and render as tofu; real subtitle renderers shape with
    # OpenType instead. Keep only the mandatory lam-alef handling.
    _ARABIC_RESHAPER = _arabic_reshaper_mod.ArabicReshaper(
        configuration={"support_ligatures": False}
    )
except ImportError:
    _ARABIC_RESHAPER = None
    _bidi_display = None

import io
import math
import random
import re
from collections import OrderedDict
from PIL import ImageChops, ImageFilter, ImageColor
from anime_subs.runtime import _subprocess_creation_flags

# Full OpenType shaping for the PIL preview. The reshaper above only reaches
# fonts that ship legacy presentation forms — ~45% of Arabic fonts don't and
# render as tofu. HarfBuzz (shaping) + FreeType (rasterizing) draw ANY font the
# way the real subtitle renderer (libass) does. Optional: without them the
# preview falls back to the reshaper path.
try:
    import uharfbuzz as _hb
    import freetype as _ft
    _HB_SHAPING = True
except Exception:  # noqa: BLE001
    _hb = None
    _ft = None
    _HB_SHAPING = False


# ----------------------------------------
# "Studio" design tokens (from the internal design spec)
# ----------------------------------------
C_WINDOW = "#1c1f24"        # window background
C_CARD = "#25292f"          # cards, fields, list rows
C_LIST = "#191c20"          # inset list background (font picker)
C_WELL = "#0c0d10"          # preview well (near-black)
C_BORDER = "#3a3f47"        # 1px borders, switch/progress track
C_TEXT = "#e7eaef"          # primary text
C_TEXT_2 = "#c3cad3"        # secondary text
C_MUTED = "#9aa3ad"         # muted text
C_DISABLED = "#6b7280"      # disabled / idle
C_ACCENT = "#4f8fdd"
C_ACCENT_HOVER = "#3f7cc4"
C_ON_ACCENT = "#0c0d10"
C_TINT_BG = "#2a3a4e"       # accent 14% on window
C_TINT_TEXT = "#7fb0e8"
C_TINT_HOVER = "#31435c"
C_QUIET = "#2a2f36"
C_QUIET_HOVER = "#333944"
C_OK = "#3fc27d"            # watching / success
C_WARN = "#e8b04b"          # warning / issues
C_DANGER = "#e07b6a"        # danger + decode ops
C_DANGER_HOVER = "#c96a5a"
C_ON_DANGER = "#1b1815"
C_EXTRACT = "#8fb7d9"
C_RESTORE = "#b08fe8"

# Op-pill tinted backgrounds: op color at ~12% opacity flattened onto a card.
OP_PILL = {
    "extract": (C_EXTRACT, "#323a43"),
    "apply":   (C_WARN,    "#3c3932"),
    "decode":  (C_DANGER,  "#3b3336"),
    "read":    (C_DANGER,  "#3b3336"),
    "restore": (C_RESTORE, "#363545"),
    "font":    (C_WARN,    "#3c3932"),  # coverage note (apply succeeded)
}

# Accent-tinted selection (flattened onto the window background).
C_SEL_BG = "#202833"        # accent 8% on window
C_SEL_BORDER = "#2e4665"    # accent 35% on window
C_CHIP_ON_BG = "#243142"    # accent 16% on window
C_CHIP_ON_BORDER = "#365781"  # accent 50% on window
C_CHIP_OFF_BORDER = "#3c3e43"  # white 14% on window
C_DANGER_ROW_BG = "#2a2529"     # danger 7% on window
C_DANGER_ROW_BORDER = "#573b39"  # danger 30% on window

MONO_FAMILY = "Consolas"


def _caption_family() -> str:
    """Family for PIL caption text. Consolas has no Arabic glyphs, so the
    translated captions need an Arabic-capable UI face in RTL mode."""
    return "Segoe UI" if is_rtl() else MONO_FAMILY


# Latin pangram shown under the font specimen's main sample — rendered in the
# selected font, so it only appears for fonts that actually cover Latin.
SPECIMEN_PANGRAM = "Sphinx of black quartz, judge my vow"


# ----------------------------------------
# RTL layout helpers — flip pack sides / anchors when the UI language is RTL.
# Tk renders Arabic TEXT correctly on its own; only the LAYOUT needs mirroring.
# ----------------------------------------
def SIDE_START() -> str:
    return "right" if is_rtl() else "left"


def SIDE_END() -> str:
    return "left" if is_rtl() else "right"


def ANCHOR_START() -> str:
    return "e" if is_rtl() else "w"


def ANCHOR_END() -> str:
    return "w" if is_rtl() else "e"


def JUSTIFY_START() -> str:
    return "right" if is_rtl() else "left"


def MPAD(left: int, right: int) -> Tuple[int, int]:
    """Mirror a (left, right) pad tuple for RTL layouts."""
    return (right, left) if is_rtl() else (left, right)


def _with_mark(text: str, mark: str, lead: bool) -> str:
    """`text` plus a symbol Segoe UI lacks (✓ ★ ⟳). Tk draws such a glyph from
    a fallback font as a separate piece, and lays the pieces out left to right
    in logical order whatever the text direction. So in RTL a mark that should
    lead (sit at the right) goes logically last, and a trailing mark (at the
    left) logically first."""
    if lead != is_rtl():
        return f"{mark} {text}"
    return f"{text} {mark}"


def CHECK_PAD(edge: int) -> Tuple[int, int]:
    """padx for a text-less CTkCheckBox packed at the START side, `edge` px
    from the edge. ctk keeps ~7 px of empty label room on the box's right, so
    in RTL that room sat against the edge and the text touched the box: move
    it to the text side."""
    return (7, max(0, edge - 7)) if is_rtl() else (edge, 0)


if IS_WINDOWS:
    class _WINRECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _WINRECT),
                    ("rcWork", _WINRECT), ("dwFlags", ctypes.c_ulong)]


def _window_graspable(child_hwnd: int) -> Optional[bool]:
    """Is enough of the window's titlebar on a real, visible monitor to grab it?

    Uses the actual monitor geometry (not a virtual-screen bounding box), so a
    window stranded in the dead space between monitors — or on a screen that a
    GPU disable/enable cycle (Fix-HDMI-Audio) tore down — is correctly detected.
    Returns None when it can't decide (non-Windows or an API hiccup), so the
    caller can fall back rather than move a window it isn't sure about."""
    if not IS_WINDOWS:
        return None
    try:
        u = ctypes.windll.user32
        hwnd = u.GetAncestor(child_hwnd, 2) or child_hwnd  # GA_ROOT → the frame
        rect = _WINRECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        MONITOR_DEFAULTTONULL, MONITOR_DEFAULTTONEAREST = 0, 2
        if not u.MonitorFromRect(ctypes.byref(rect), MONITOR_DEFAULTTONULL):
            return False  # frame intersects no monitor at all
        near = u.MonitorFromRect(ctypes.byref(rect), MONITOR_DEFAULTTONEAREST)
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not u.GetMonitorInfoW(near, ctypes.byref(mi)):
            return None
        w = mi.rcWork  # visible work area (excludes taskbar) of the nearest screen
        overlap_x = min(rect.right, w.right) - max(rect.left, w.left)
        title_on_screen = (w.top - 8) <= rect.top <= (w.bottom - 24)
        return overlap_x >= 100 and title_on_screen  # ~100 physical px to grab
    except Exception:  # noqa: BLE001
        return None


def _relative_time(ts: float) -> str:
    """'just now' / '5m ago' / '2h ago' / '3d ago' for the footer line."""
    delta = max(0.0, time.time() - ts)
    # Logical (unwrapped): the footer composes this into a larger line and
    # orders its parts by packing, so the RTL wrap must not be nested here.
    if delta < 60:
        return t_logical("rel_just_now")
    if delta < 3600:
        return t_logical("rel_min", n=int(delta // 60))
    if delta < 86400:
        return t_logical("rel_hour", n=int(delta // 3600))
    return t_logical("rel_day", n=int(delta // 86400))


# Footer event verbs are stored in app_state.last_event as stable English keys
# ("Styled"/"Extracted"/"Restored") and translated at display time.
_EVENT_VERB_KEY = {"Styled": "ev_styled", "Extracted": "ev_extracted",
                   "Restored": "ev_restored"}


def _ellipsize(text: str, limit: int) -> str:
    """Middle-agnostic tail ellipsis, matching the old progress-dialog trim."""
    if len(text) <= limit:
        return text
    return "…" + text[-(limit - 1):]


def _issue_timestamp(ts: float) -> str:
    """Issue time: '21:14' today, else '2026-09-18 21:14'. Numbers only, so it
    reads the same in both languages (strftime's %a/%b names are English)."""
    then = datetime.fromtimestamp(ts)
    if then.date() == datetime.now().date():
        return then.strftime("%H:%M")
    return then.strftime("%Y-%m-%d %H:%M")


def _font_glyph_count(path: str, index: int = 0) -> Optional[int]:
    """numGlyphs from a TTF/OTF/TTC 'maxp' table (targeted reads, no full load)."""
    try:
        with open(path, "rb") as f:
            data = f.read(12)
            if len(data) < 12:
                return None
            offset = 0
            if data[:4] == b"ttcf":
                num_fonts = struct.unpack(">I", data[8:12])[0]
                if not (0 <= index < num_fonts):
                    return None
                f.seek(12 + 4 * index)
                offset = struct.unpack(">I", f.read(4))[0]
                f.seek(offset)
                data = f.read(12)
            num_tables = struct.unpack(">H", data[4:6])[0]
            f.seek(offset + 12)
            directory = f.read(16 * num_tables)
            for i in range(num_tables):
                entry = directory[16 * i: 16 * i + 16]
                if entry[:4] == b"maxp":
                    table_off = struct.unpack(">I", entry[8:12])[0]
                    f.seek(table_off + 4)
                    return struct.unpack(">H", f.read(2))[0]
    except (OSError, struct.error):
        return None
    return None


# ----------------------------------------
# Preview background — a real keyframe from the user's library behind the sub
# ----------------------------------------
def _preview_frame_paths(from_sub: bool) -> Tuple[Path, Path]:
    """(jpg, txt) cache paths — a SEPARATE slot per source mode so 'My text'
    and 'Subtitle line' each remember their own last frame."""
    tag = "sub" if from_sub else "text"
    return (APPDATA_DIR / f"preview_frame_{tag}.jpg",
            APPDATA_DIR / f"preview_frame_{tag}.txt")


class PreviewBackground:
    """Grab a random still frame from a random video in the watched folder, so
    the preview shows the subtitle over REAL content (the true legibility test)
    instead of a black placeholder. All work is blocking ffprobe/ffmpeg calls —
    run it off the GUI thread."""

    @staticmethod
    def _list_videos(folder: str, limit: int = 4000) -> List[Path]:
        vids: List[Path] = []
        try:
            for p in Path(folder).rglob("*"):
                if p.suffix.lower() in VIDEO_EXTENSIONS and p.is_file():
                    vids.append(p)
                    if len(vids) >= limit:
                        break
        except OSError as e:
            logger.debug(f"Preview bg: cannot walk {folder}: {e}")
        return vids

    @staticmethod
    def _duration(ffprobe: str, video: Path) -> Optional[float]:
        cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(video)]
        try:
            proc = run_tracked(
                cmd, encoding="utf-8", errors="replace", check=True, timeout=15,
            )
            return float((proc.stdout or "").strip())
        except (subprocess.SubprocessError, ValueError, OSError):
            return None

    @staticmethod
    def _extract_at(ffmpeg: str, video: Path, seek: float) -> Optional[Any]:
        """One PIL RGB frame from `video` at `seek` seconds (piped, no temp)."""
        cmd = [
            ffmpeg, "-nostdin", "-ss", f"{max(0.0, seek):.2f}", "-i", str(video),
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-",
        ]
        try:
            proc = run_tracked(cmd, check=True, timeout=30)
            if not proc.stdout:
                return None
            img = Image.open(io.BytesIO(proc.stdout))
            img.load()
            return img.convert("RGB")
        except (subprocess.SubprocessError, OSError, ValueError) as e:
            logger.debug(f"Preview bg: frame grab failed for {video}: {e}")
            return None

    @classmethod
    def grab_random_frame(cls, folder: str) -> Optional[Any]:
        """Return a PIL RGB frame from a random video, or None on any failure
        (no folder/videos/ffmpeg, probe/decode error). Blocking."""
        ffmpeg = FFmpegLocator.get_binary("ffmpeg")
        ffprobe = FFmpegLocator.get_binary("ffprobe")
        if not (folder and ffmpeg and ffprobe):
            return None
        vids = cls._list_videos(folder)
        if not vids:
            return None
        video = random.choice(vids)
        dur = cls._duration(ffprobe, video)
        # Seek into the body of the film: skip intros/logos (first 10%) and
        # end credits (last 15%), where random frames are black or useless.
        seek = random.uniform(0.10 * dur, 0.85 * dur) if dur and dur > 8 else 5.0
        return cls._extract_at(ffmpeg, video, seek)

    # ── Real subtitle line + the frame it plays on ──────────────────────
    _ASS_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})\.(\d{1,2})")
    _ASS_TAGS = re.compile(r"\{[^}]*\}")
    _ASS_DRAWING = re.compile(r"\\p[1-9]")
    _ARABIC_PROBE = "ابتث"  # does the selected font render Arabic at all?

    @staticmethod
    def _decode_sub(raw: bytes) -> str:
        # UTF-16 first (BOM, or the interleaved NULs of BOM-less ASCII-heavy
        # UTF-16) — else utf-8-sig decodes it into a NUL-riddled string with no
        # "Dialogue:" lines.
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            try:
                return raw.decode("utf-16")
            except UnicodeError:
                pass
        if raw[:400].count(0) > 40:
            for enc in ("utf-16-le", "utf-16-be"):
                try:
                    return raw.decode(enc)
                except UnicodeError:
                    continue
        for enc in ("utf-8-sig", "utf-8", "cp1256", "windows-1256"):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return raw.decode("latin-1", "replace")

    @classmethod
    def _ass_seconds(cls, field: str) -> Optional[float]:
        m = cls._ASS_TIME.match(field.strip())
        if not m:
            return None
        h, mi, s, cs = m.groups()
        return int(h) * 3600 + int(mi) * 60 + int(s) + int(cs) / (10 ** len(cs))

    @classmethod
    def _clean_line(cls, raw: str) -> str:
        """Strip ASS override tags; keep hard/soft breaks (\\N, \\n) as real
        newlines so a two-line subtitle previews as two lines (not one that
        overflows the well). \\h is a non-breaking space."""
        t = cls._ASS_TAGS.sub("", raw).replace(r"\h", " ")
        t = t.replace(r"\N", "\n").replace(r"\n", "\n")
        # Collapse runs of spaces per line, keep the newlines.
        return "\n".join(" ".join(ln.split()) for ln in t.split("\n")).strip()

    @staticmethod
    def _matching_sub(video: Path, prefer_lang: str = "") -> Optional[Path]:
        """A sidecar .ass next to `video`, matched on the FULL stem plus a "."
        boundary — so "Show 1" can't grab "Show 10.ass". Prefers the sidecar
        whose language suffix matches `prefer_lang` (an English line would tofu
        in an Arabic-only font, and vice-versa), else the largest. Name-prefix,
        NOT glob — the filenames contain `[...]` which glob reads as classes."""
        stem = video.stem
        cands: List[Tuple[Path, int]] = []
        try:
            for c in video.parent.iterdir():
                if (c.suffix.lower() == ".ass" and c.name.startswith(stem)
                        and c.name[len(stem):].startswith(".")
                        and c.is_file()):
                    try:
                        cands.append((c, c.stat().st_size))
                    except OSError:
                        continue  # vanished between iterdir and stat
        except OSError:
            return None
        if not cands:
            return None
        if prefer_lang:
            pref = [cs for cs in cands
                    if f".{prefer_lang}." in cs[0].name.lower()]
            if pref:
                cands = pref
        return max(cands, key=lambda cs: cs[1])[0]

    @classmethod
    def _parse_dialogue(cls, text: str) -> List[Tuple[float, float, str]]:
        """(start, end, clean_text) for every real Dialogue line in ASS `text`
        (skips signs/drawings, empties, bad timing)."""
        out: List[Tuple[float, float, str]] = []
        for ln in text.splitlines():
            if not ln.startswith("Dialogue:"):
                continue
            parts = ln.split(",", 9)
            if len(parts) < 10 or cls._ASS_DRAWING.search(parts[9]):
                continue  # skip signs / vector-drawing lines
            clean = cls._clean_line(parts[9])
            start = cls._ass_seconds(parts[1])
            end = cls._ass_seconds(parts[2])
            if len(clean) < 2 or start is None or end is None or end <= start:
                continue
            out.append((start, end, clean))
        return out

    @classmethod
    def _embedded_lines(
        cls, ffmpeg: str, ffprobe: Optional[str], video: Path, prefer_lang: str,
    ) -> List[Tuple[float, float, str]]:
        """Dialogue lines from the video's embedded subtitle stream — the
        naming-independent fallback when no sidecar matches. Prefers a stream
        tagged with the UI language (an English line tofus in an Arabic font)."""
        if not ffprobe:
            return []
        try:
            probe = run_tracked(
                [ffprobe, "-v", "error", "-select_streams", "s",
                 "-show_entries", "stream=index:stream_tags=language",
                 "-of", "json", str(video)],
                encoding="utf-8", errors="replace", timeout=20,
            )
            streams = json.loads(probe.stdout or "{}").get("streams", [])
        except (subprocess.SubprocessError, ValueError, OSError):
            return []
        if not streams:
            return []
        pref_tags = {"ar": ("ara", "ar"), "en": ("eng", "en")}.get(
            prefer_lang, ())

        def _is_pref(s: Dict[str, Any]) -> bool:
            return (s.get("tags", {}).get("language", "") or "").lower() \
                in pref_tags
        # UI-language streams first, then the rest; try a few (the first may be
        # a signs/songs track with no dialogue).
        ordered = ([s for s in streams if _is_pref(s)]
                   + [s for s in streams if not _is_pref(s)])
        for s in ordered[:3]:
            idx = s.get("index")
            if idx is None:
                continue
            try:
                dump = run_tracked(
                    [ffmpeg, "-nostdin", "-i", str(video), "-map", f"0:{idx}",
                     "-f", "ass", "-"],
                    timeout=45,
                )
            except (subprocess.SubprocessError, OSError):
                continue
            lines = cls._parse_dialogue(cls._decode_sub(dump.stdout or b""))
            if lines:
                return lines
        return []

    @classmethod
    def _preview_lang(cls, font: str) -> str:
        """Which subtitle language to preview: the UI language when the SELECTED
        font can render its script, else whichever script the font CAN render —
        so lines never tofu, whatever the font/UI combination (e.g. an
        Arabic-only font shows Arabic subs even on an English UI)."""
        ui = get_language()
        if not font:
            return ui
        renders_arabic = FontSupport.supports_text(font, cls._ARABIC_PROBE)
        renders_latin = FontSupport.supports_latin(font)
        renders_ui = renders_arabic if ui == "ar" else renders_latin
        if renders_ui is not False:      # font can render the UI language → keep it
            return ui
        return "en" if ui == "ar" else "ar"  # else flip to what the font can do

    @classmethod
    def grab_line_frame(cls, folder: str, font: str = "") -> \
            Optional[Tuple[Any, str]]:
        """Pick a random dialogue line + the frame it plays on. Tries a matching
        sidecar .ass first (fast, for Bazarr/Sonarr-style libraries), then the
        video's EMBEDDED subtitle stream — so it works even when subs aren't
        named after the video. Only returns lines in a script the `font` can
        render. Returns (frame, line_text) or None.

        Bounds the ffmpeg frame extractions so a run of bad/slow files can't pin
        the worker, and retries a few lines so one badly-timed line (seek past
        EOF) doesn't abandon a sub full of good ones."""
        ffmpeg = FFmpegLocator.get_binary("ffmpeg")
        ffprobe = FFmpegLocator.get_binary("ffprobe")
        if not (folder and ffmpeg):
            return None
        prefer = cls._preview_lang(font)
        prefer_rtl = (prefer == "ar")
        vids = cls._list_videos(folder)
        random.shuffle(vids)
        attempts = 0
        for video in vids[:40]:
            if attempts >= 6:
                break
            lines: List[Tuple[float, float, str]] = []
            sub = cls._matching_sub(video, prefer)
            if sub is not None:
                try:
                    lines = cls._parse_dialogue(cls._decode_sub(sub.read_bytes()))
                except OSError:
                    lines = []
            if not lines:  # no usable sidecar — read the embedded stream
                lines = cls._embedded_lines(ffmpeg, ffprobe, video, prefer)
            if not lines:
                continue
            # Keep only lines in the chosen script (the one the font renders).
            lines = [ln for ln in lines
                     if has_rtl_chars(ln[2]) == prefer_rtl]
            if not lines:
                continue
            random.shuffle(lines)
            for start, end, clean in lines[:3]:
                if attempts >= 6:
                    break
                attempts += 1
                frame = cls._extract_at(ffmpeg, video,
                                        start + (end - start) / 2)
                if frame is not None:
                    return frame, clean
        return None

    @staticmethod
    def save_frame(img: Any, line_text: str, from_sub: bool) -> None:
        """Persist the frame (+ its subtitle line) to the slot for its source
        mode, so each mode's exact preview returns next launch."""
        jpg, txt = _preview_frame_paths(from_sub)
        try:
            APPDATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = str(jpg) + ".tmp"
            img.convert("RGB").save(tmp, "JPEG", quality=88)
            os.replace(tmp, str(jpg))
            txt.write_text(line_text or "", encoding="utf-8")
        except (OSError, ValueError) as e:
            logger.debug(f"Preview bg: could not save frame: {e}")

    @staticmethod
    def load_saved_frame(from_sub: bool) -> Optional[Tuple[Any, str]]:
        """(frame, subtitle_line) for `from_sub`'s slot, or None if absent/
        unreadable. The line is "" for the My-text slot."""
        jpg, txt = _preview_frame_paths(from_sub)
        if not jpg.exists():
            return None
        try:
            img = Image.open(jpg)
            img.load()
            img = img.convert("RGB")
        except (OSError, ValueError) as e:
            logger.debug(f"Preview bg: could not load saved frame: {e}")
            return None
        line = ""
        try:
            if txt.exists():
                line = txt.read_text(encoding="utf-8")
        except OSError:
            pass
        return img, line


# ----------------------------------------
# PIL preview rendering (the "video frame" well + font-picker specimen)
# ----------------------------------------
class PreviewRenderer:
    """Render the dark 16:9 preview well with the styled subtitle over it.

    Pure PIL: Tk labels can't draw text outlines, so the subtitle gets a real
    black `stroke_width` outline here, like the styled .ass output. The same
    renderer draws the font picker's specimen pane.
    """

    WELL_BG = (12, 13, 16)      # #0c0d10
    STRIPE = (18, 19, 22)       # well bg + white 2.5%
    CHIP_BG = (29, 30, 33)      # white 7% on well
    CHIP_FG = (121, 122, 124)   # white 45% on well
    CAPTION_FG = (80, 81, 83)   # white 28% on well
    META_FG = (109, 110, 112)   # white 40% on well

    # (family, px) -> ImageFont; families resolve through the app's
    # font_path_cache so non-trivially-named files are found. LRU-bounded like
    # the face/mask caches below — browsing the 4,700-font picker would otherwise
    # grow them without limit, and _font_cache holds ImageFont objects (glyph
    # tables, MBs for CJK fonts), so an unbounded one is a real memory leak.
    _font_cache: "OrderedDict[Tuple[str, int], Any]" = OrderedDict()
    _file_cache: "OrderedDict[str, Optional[Tuple[str, int]]]" = OrderedDict()
    _FONT_CACHE_MAX = 192
    _FILE_CACHE_MAX = 2048
    # Flipped by the startup warmup thread once FontSupport's family→path map
    # is loaded; before that, resolution falls back to PIL's own name lookup
    # (never trigger the potentially slow first map build on the GUI thread).
    path_map_ready = False

    # HarfBuzz/FreeType faces keyed by (path, index, axes); shaped-line coverage
    # masks keyed by (text, path, index, px, axes). Both LRU-bounded so scrolling
    # the 4,700-font picker can't leak faces/handles or grow without limit.
    _hb_faces: "OrderedDict[Tuple[Any, ...], Tuple[Any, Any]]" = OrderedDict()
    _ft_faces: "OrderedDict[Tuple[Any, ...], Any]" = OrderedDict()
    _FACE_CACHE_MAX = 48
    _mask_cache: "OrderedDict[Tuple[Any, ...], Any]" = OrderedDict()
    _MASK_CACHE_MAX = 64

    @classmethod
    def _resolve_file(cls, family: str) -> Optional[Tuple[str, int]]:
        if family in cls._file_cache:
            cls._file_cache.move_to_end(family)
            return cls._file_cache[family]
        info = FontSupport.font_file(family) if cls.path_map_ready else None
        if cls.path_map_ready:
            cls._file_cache[family] = info
            if len(cls._file_cache) > cls._FILE_CACHE_MAX:
                cls._file_cache.popitem(last=False)
        return info

    @classmethod
    def _font(cls, family: str, px: int) -> Any:
        key = (family, px)
        cached = cls._font_cache.get(key)
        if cached is not None:
            cls._font_cache.move_to_end(key)
            return cached
        font = None
        info = cls._resolve_file(family)
        if info is not None:
            try:
                font = ImageFont.truetype(info[0], px, index=info[1])
                # Variable-font named instance → select its actual weight/axes,
                # so "Imbue 10pt Black" doesn't preview as the file's default.
                # Only apply when the axis COUNT matches this file's own axes,
                # so a coords lookup that ever resolved a different file is a
                # harmless no-op (default weight) rather than wrong axes.
                axes = FontSupport.variation_axes(family)
                if axes:
                    try:
                        if len(axes) == len(font.get_variation_axes()):
                            font.set_variation_by_axes([v for _, v in axes])
                    except Exception:  # noqa: BLE001
                        pass  # static font / PIL without VF support
            except Exception:  # noqa: BLE001
                font = None
        if font is None:
            try:
                font = ImageFont.truetype(family, px)
            except Exception:  # noqa: BLE001
                try:
                    font = ImageFont.truetype("segoeui.ttf", px)
                except Exception:  # noqa: BLE001
                    font = ImageFont.load_default()
        cls._font_cache[key] = font
        if len(cls._font_cache) > cls._FONT_CACHE_MAX:
            cls._font_cache.popitem(last=False)
        return font

    @classmethod
    def clear_font_cache(cls) -> None:
        """Drop cached fonts/paths (after the path map becomes available)."""
        cls._font_cache.clear()
        cls._file_cache.clear()
        cls._hb_faces.clear()
        cls._ft_faces.clear()
        cls._mask_cache.clear()

    @staticmethod
    def _shape(text: str) -> str:
        """Shape + reorder RTL text for PIL (which lays out LTR without raqm)."""
        if _PIL_RAQM or not has_rtl_chars(text):
            return text
        if _ARABIC_RESHAPER is not None and _bidi_display is not None:
            try:
                return _bidi_display(_ARABIC_RESHAPER.reshape(text))
            except Exception:  # noqa: BLE001
                return text
        return text

    @classmethod
    def _hb_ft_faces(
        cls, path: str, index: int,
        axes: Optional[Tuple[Tuple[str, float], ...]] = None,
    ) -> Tuple[Any, Any, Any]:
        """(hb.Face, hb.Font, freetype.Face) for a font file, LRU-cached. Keyed
        by (path, index, axes) so each variable-font weight gets its own faces
        with the right variation coords applied."""
        key = (path, index, axes)
        hb = cls._hb_faces.get(key)
        if hb is None:
            face = _hb.Face(_hb.Blob.from_file_path(path), index)
            hbfont = _hb.Font(face)
            if axes:
                try:
                    hbfont.set_variations({t: v for t, v in axes})
                except Exception:  # noqa: BLE001
                    pass
            hb = (face, hbfont)
            cls._hb_faces[key] = hb
            if len(cls._hb_faces) > cls._FACE_CACHE_MAX:
                cls._hb_faces.popitem(last=False)
        else:
            cls._hb_faces.move_to_end(key)
        ft = cls._ft_faces.get(key)
        if ft is None:
            ft = ft_face(path, index)
            if axes:
                try:
                    ft.set_var_design_coords([v for _, v in axes])
                except Exception:  # noqa: BLE001
                    pass
            cls._ft_faces[key] = ft
            if len(cls._ft_faces) > cls._FACE_CACHE_MAX:
                cls._ft_faces.popitem(last=False)
        else:
            cls._ft_faces.move_to_end(key)
        return hb[0], hb[1], ft

    @classmethod
    def _shape_mask(
        cls, text: str, path: str, index: int, px: int,
        axes: Optional[Tuple[Tuple[str, float], ...]] = None,
    ) -> Optional[Tuple[Any, Tuple[int, int, int, int]]]:
        """Shape `text` with HarfBuzz in the font FILE and rasterize it
        (FreeType) into a grayscale coverage mask — the way libass renders it,
        so OpenType-only Arabic fonts (no legacy presentation forms) draw
        correctly instead of as tofu. `axes`, if given, selects a variable-font
        instance's coords. Returns (mask_L, inked_bbox), or None if shaping is
        unavailable/failed (caller falls back to the PIL path)."""
        if not (_HB_SHAPING and path and text):
            return None
        ck = (text, path, index, px, axes)
        cached = cls._mask_cache.get(ck)
        if cached is not None:
            cls._mask_cache.move_to_end(ck)
            return cached
        try:
            hbface, hbfont, ftface = cls._hb_ft_faces(path, index, axes)
            upem = hbface.upem or 1000
            hbfont.scale = (upem, upem)
            ftface.set_pixel_sizes(0, px)
            sc = px / upem
            buf = _hb.Buffer()
            buf.add_str(text)
            buf.guess_segment_properties()
            _hb.shape(hbfont, buf)
            positions = buf.glyph_positions
            total = sum(p.x_advance for p in positions) * sc
            asc = ftface.size.ascender / 64.0
            desc = -ftface.size.descender / 64.0
            # Headroom so stacked diacritics above the ascender / a glyph's
            # side bearings aren't clipped out of the mask.
            pad = max(12, px // 3)
            W = max(1, int(math.ceil(total)) + 2 * pad)
            H = max(1, int(math.ceil(asc + desc)) + 2 * pad)
            mask = Image.new("L", (W, H), 0)
            penx = float(pad)
            baseline = pad + asc
            for info, pos in zip(buf.glyph_infos, positions):
                ftface.load_glyph(info.codepoint, _ft.FT_LOAD_RENDER)
                bmp = ftface.glyph.bitmap
                w, rows, pitch = bmp.width, bmp.rows, bmp.pitch
                if w and rows:
                    data = bytes(bmp.buffer)
                    if pitch != w:  # FreeType may pad each row to `pitch`
                        data = b"".join(
                            data[i * pitch:i * pitch + w] for i in range(rows)
                        )
                    glyph = Image.frombytes("L", (w, rows), data)
                    gx = int(round(penx + pos.x_offset * sc
                                   + ftface.glyph.bitmap_left))
                    gy = int(round(baseline - pos.y_offset * sc
                                   - ftface.glyph.bitmap_top))
                    # crop zero-fills out-of-bounds and paste clips, so this is
                    # safe at any gx/gy while merging overlapping (joined) glyphs.
                    region = mask.crop((gx, gy, gx + w, gy + rows))
                    mask.paste(ImageChops.lighter(region, glyph), (gx, gy))
                penx += pos.x_advance * sc
            bbox = mask.getbbox()
            if bbox is None:
                return None
            result = (mask, bbox)
            cls._mask_cache[ck] = result
            if len(cls._mask_cache) > cls._MASK_CACHE_MAX:
                cls._mask_cache.popitem(last=False)
            return result
        except Exception as e:  # noqa: BLE001
            logger.debug(f"HB shaping failed for {path}: {e}")
            return None

    @classmethod
    def _blit_mask(
        cls, img: Any, mask: Any, bbox: Tuple[int, int, int, int],
        center_x: float, top_y: float, fill: str, stroke: int,
        bold: bool = False, italic: bool = False, underline: bool = False,
    ) -> int:
        """Composite a shaped coverage `mask` onto `img`, centered on
        `center_x` with its inked top at `top_y`: black outline (stroke),
        colored fill, and synthetic bold/italic/underline. Returns the drawn
        height."""
        m = mask
        if bold:
            m = m.filter(ImageFilter.MaxFilter(3))
        if italic:
            # Lean the top to the RIGHT (a forward "/", like real italics):
            # input_x = x + shear*(y - H/2), so lower rows sample further right
            # and the top edge ends up right of the baseline. A positive shear
            # gives "/"; the negative one leaned the wrong way ("\"). Pivot at
            # mid-height so the slant stays centered within the mask padding.
            shear = 0.20
            m = m.transform(
                m.size, Image.AFFINE,
                (1, shear, -shear * m.size[1] / 2, 0, 1, 0),
                resample=Image.BILINEAR,
            )
        cw = bbox[2] - bbox[0]
        ox = int(round(center_x - cw / 2 - bbox[0]))
        oy = int(round(top_y - bbox[1]))
        rgb = ImageColor.getrgb(fill)
        if stroke > 0:
            outline = m.filter(ImageFilter.MaxFilter(2 * stroke + 1))
            img.paste((0, 0, 0), (ox, oy), outline)
        img.paste(rgb, (ox, oy), m)
        if underline:
            th = max(2, (bbox[3] - bbox[1]) // 14)
            uy = oy + bbox[3] - th
            ImageDraw.Draw(img).line(
                [(ox + bbox[0], uy), (ox + bbox[2], uy)], fill=rgb, width=th)
        return bbox[3] - bbox[1]

    @classmethod
    def _draw_sub_line(
        cls,
        img: Any,
        center_x: int,
        bottom_y: int,
        text: str,
        font: Any,
        fill: str,
        stroke: int,
        bold: bool,
        italic: bool,
        underline: bool,
        family: Optional[str] = None,
        px: Optional[int] = None,
    ) -> int:
        """Draw one outlined subtitle line anchored bottom-center; return its
        height. Bold is faked with a same-color stroke pass, italic with a
        shear of the line's own layer (PIL can't synthesize either)."""
        # HarfBuzz path: shape+rasterize RTL text so OpenType-only Arabic fonts
        # render correctly. Falls through to PIL on any miss.
        if _HB_SHAPING and family and px and has_rtl_chars(text):
            info = FontSupport.font_file(family)
            if info:
                res = cls._shape_mask(text, info[0], info[1], px,
                                      FontSupport.variation_axes(family))
                if res is not None:
                    mask, bbox = res
                    ch = bbox[3] - bbox[1]
                    cls._blit_mask(img, mask, bbox, center_x, bottom_y - ch,
                                   fill, stroke, bold, italic, underline)
                    return ch
        shaped = cls._shape(text)
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        pos = (center_x, bottom_y)
        try:
            bbox = draw.textbbox(pos, shaped, font=font, anchor="md",
                                 stroke_width=stroke + (1 if bold else 0))
        except (ValueError, OSError):
            return 0
        if bold:
            draw.text(pos, shaped, font=font, fill="black", anchor="md",
                      stroke_width=stroke + 1, stroke_fill="black")
            draw.text(pos, shaped, font=font, fill=fill, anchor="md",
                      stroke_width=1, stroke_fill=fill)
        else:
            draw.text(pos, shaped, font=font, fill=fill, anchor="md",
                      stroke_width=stroke, stroke_fill="black")
        if underline:
            thickness = max(2, (bbox[3] - bbox[1]) // 14)
            uy = bbox[3] - thickness
            draw.line([(bbox[0], uy), (bbox[2], uy)], fill=fill, width=thickness)
        if italic:
            shear = 0.18
            layer = layer.transform(
                layer.size, Image.AFFINE,
                (1, shear, -shear * bottom_y, 0, 1, 0),
                resample=Image.BICUBIC,
            )
        img.alpha_composite(layer)
        return bbox[3] - bbox[1]

    @classmethod
    def _line_width(cls, text: str, family: str, px: int, draw: Any) -> int:
        """Rendered pixel width of one line at `px` — via HarfBuzz for RTL,
        else PIL — so the well can shrink long subtitle lines to fit."""
        if _HB_SHAPING and family and has_rtl_chars(text):
            info = FontSupport.font_file(family)
            if info is not None:
                res = cls._shape_mask(text, info[0], info[1], max(8, px),
                                      FontSupport.variation_axes(family))
                if res is not None:
                    return res[1][2] - res[1][0]
        try:
            bb = draw.textbbox((0, 0), cls._shape(text),
                               font=cls._font(family, max(8, px)), stroke_width=2)
            return bb[2] - bb[0]
        except (ValueError, OSError):
            return 0

    @classmethod
    def _new_canvas(cls, w: int, h: int, stripes: bool = True) -> Any:
        img = Image.new("RGBA", (w, h), cls.WELL_BG + (255,))
        if stripes:
            draw = ImageDraw.Draw(img)
            step = max(8, int(28 * 1.414))
            width = 14
            for x in range(-h, w + h, step):
                draw.line([(x, h), (x + h, 0)], fill=cls.STRIPE, width=width)
        return img

    # (id(source), W, H) -> fitted RGBA copy; the source frame changes only on
    # shuffle, so re-fitting on every keystroke/resize is wasteful.
    _bg_cache: Tuple[Any, int, int, Any] = (None, 0, 0, None)

    @classmethod
    def _fit_cover(cls, src: Any, w: int, h: int) -> Any:
        """Resize `src` to fill w×h and centre-crop (cover fit), RGBA."""
        cid = id(src)
        if cls._bg_cache[:3] == (cid, w, h) and cls._bg_cache[3] is not None:
            return cls._bg_cache[3].copy()
        sw, sh = src.size
        scale = max(w / sw, h / sh)
        nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
        resized = src.resize((nw, nh), Image.LANCZOS)
        left, top = (nw - w) // 2, (nh - h) // 2
        fitted = resized.crop((left, top, left + w, top + h)).convert("RGBA")
        cls._bg_cache = (cid, w, h, fitted)
        return fitted.copy()

    @classmethod
    def render_well(
        cls,
        w: int,
        h: int,
        scale: float,
        *,
        family: str,
        color: str,
        bold: bool,
        italic: bool,
        underline: bool,
        text: str,
        latin_text: Optional[str] = None,
        background: Optional[Any] = None,
        note: Optional[str] = None,
    ) -> Any:
        """The main-window preview: chip, placeholder caption, subtitle block.

        `background`, when given, is a real keyframe used behind the subtitle
        instead of the striped placeholder — the true legibility test."""
        W, H = max(1, int(w * scale)), max(1, int(h * scale))
        s = scale
        has_bg = False
        if background is not None:
            try:
                img = cls._fit_cover(background, W, H)
                has_bg = True
            except Exception as e:  # noqa: BLE001 — never let a bad frame kill the preview
                logger.debug(f"Preview bg fit failed: {e}")
                img = cls._new_canvas(W, H)
        else:
            img = cls._new_canvas(W, H)
        draw = ImageDraw.Draw(img)

        # LIVE PREVIEW chip — top-left, or top-right in RTL (leading corner).
        chip_font = cls._font(_caption_family(), max(8, int(10 * s)))
        chip_text = cls._shape(t_logical("live_preview"))
        tb = draw.textbbox((0, 0), chip_text, font=chip_font)
        pad_x, pad_y = int(8 * s), int(3 * s)
        chip_w = (tb[2] - tb[0]) + 2 * pad_x
        chip_h = (tb[3] - tb[1]) + 2 * pad_y
        cy0 = int(10 * s)
        cx0 = (W - int(10 * s) - chip_w) if is_rtl() else int(10 * s)
        draw.rounded_rectangle(
            (cx0, cy0, cx0 + chip_w, cy0 + chip_h),
            radius=int(4 * s), fill=cls.CHIP_BG,
        )
        draw.text((cx0 + pad_x - tb[0], cy0 + pad_y - tb[1]), chip_text,
                  font=chip_font, fill=cls.CHIP_FG)

        # Centered placeholder caption — only when there's no real frame.
        if not has_bg:
            cap_font = cls._font(_caption_family(), max(8, int(11 * s)))
            draw.text((W // 2, H // 2), cls._shape(t_logical("video_frame")),
                      font=cap_font, fill=cls.CAPTION_FG, anchor="mm")

        # Subtitle block, anchored bottom-center 18px up
        y = H - int(18 * s)
        if latin_text:
            latin_font = cls._font("Segoe UI Semibold", max(8, int(16 * s)))
            lh = cls._draw_sub_line(
                img, W // 2, y, latin_text, latin_font, color,
                stroke=max(1, int(1.5 * s)), bold=bold, italic=italic,
                underline=underline,
            )
            y -= lh + int(6 * s)
        if note:
            # Subtitle mode found nothing — say so where the line would be,
            # instead of silently falling back to the user's preview text.
            note_font = cls._font(_caption_family(), max(8, int(12 * s)))
            draw.text((W // 2, H - int(34 * s)), cls._shape(note),
                      font=note_font, fill=cls.CAPTION_FG, anchor="mm")
        elif text:
            # Real subtitle lines are long + often two lines (\N). Keep the
            # breaks and shrink the whole block until the widest line fits.
            sub_lines = [ln for ln in text.split("\n") if ln.strip()]

            # Honest preview: draw a line only if the chosen font can render its
            # SCRIPT. Gate by a script probe (Arabic letters / the Latin probe),
            # NOT the line's own chars: coverage tests skip ASCII (so a pure-
            # Latin line would never be caught), and a real subtitle shouldn't
            # be dropped over one stray missing glyph. Only an explicit False
            # (definitely unsupported, vs None/unknown) hides the line.
            drawable, missing = [], []
            for ln in sub_lines:
                if has_rtl_chars(ln):
                    ok = (not family) or FontSupport.supports_text(
                        family, PreviewBackground._ARABIC_PROBE) is not False
                    scr = "ar"
                else:
                    ok = (not family) or \
                        FontSupport.supports_latin(family) is not False
                    scr = "en"
                if ok:
                    drawable.append(ln)
                else:
                    missing.append(scr)

            max_w = W - int(30 * s)
            px = max(8, int(27 * s))
            while px > int(12 * s):
                widest = max((cls._line_width(ln, family, px, draw)
                              for ln in drawable), default=0)
                if widest <= max_w:
                    break
                px = int(px * 0.9)
            stroke = max(2, int(2 * s))
            for ln in reversed(drawable):  # bottom-up so line 1 ends on top
                main_font = cls._font(family, px)
                lh = cls._draw_sub_line(
                    img, W // 2, y, ln, main_font, color, stroke=stroke,
                    bold=bold, italic=italic, underline=underline,
                    family=family, px=px,
                )
                y -= lh + int(5 * s)

            # Small note where a dropped line would have been. If the font
            # covers NEITHER script, say so in one line instead of stacking two.
            if missing:
                uniq = dict.fromkeys(missing)  # unique, order-preserving
                if len(uniq) > 1:
                    key = "font_no_neither"
                elif "ar" in uniq:
                    key = "font_no_arabic"
                else:
                    key = "font_no_latin"
                note_font = cls._font(_caption_family(), max(8, int(11 * s)))
                # Clear the subtitle block: it's outlined (stroke) and the note
                # is centre-anchored, so a small gap overlapped the glyphs.
                draw.text((W // 2, y - stroke - int(8 * s)),
                          cls._shape(t_logical(key)),
                          font=note_font, fill=cls.CAPTION_FG, anchor="mm")

        # Rounded corners (radius 10) — the label can't clip a square image.
        radius = int(10 * s)
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, W - 1, H - 1), radius=radius, fill=255
        )
        img.putalpha(mask)
        return img

    @classmethod
    def render_specimen(
        cls,
        w: int,
        h: int,
        scale: float,
        *,
        family: str,
        color: str,
        text: str,
        pangram: str = "",
        coverage: str = "",
    ) -> Any:
        """The font picker's right pane: caption, sample, pangram, coverage."""
        W, H = max(1, int(w * scale)), max(1, int(h * scale))
        s = scale
        img = cls._new_canvas(W, H)
        draw = ImageDraw.Draw(img)

        cap_font = cls._font(MONO_FAMILY, max(8, int(10 * s)))
        draw.text((int(12 * s), int(10 * s)), family.upper(),
                  font=cap_font, fill=cls.META_FG)

        # The pangram previews the font's Latin glyphs, so it must render in
        # the SELECTED family too (not a fixed face) — otherwise picking
        # different fonts left this line unchanged.
        pan_font = cls._font(family, max(8, int(15 * s)))
        meta_font = cls._font(_caption_family(), max(8, int(11 * s)))

        coverage = cls._shape(coverage)
        max_text_w = W - int(36 * s)
        px = int(30 * s)

        # Main sample — HarfBuzz shaping when available (renders OpenType-only
        # Arabic fonts), else the reshaper+PIL fallback. `sample` is
        # (mask, bbox) for the HB path, else None. Shrink until it fits.
        sample = None
        info = FontSupport.font_file(family) if (_HB_SHAPING and family) else None
        if info and text and has_rtl_chars(text):
            _axes = FontSupport.variation_axes(family)
            while True:
                res = cls._shape_mask(text, info[0], info[1], max(8, px), _axes)
                if res is None:
                    break
                if res[1][2] - res[1][0] <= max_text_w or px <= int(14 * s):
                    sample = res
                    break
                px = int(px * 0.85)

        shaped = ""
        main_font = None
        if sample is None:
            shaped = cls._shape(text)
            main_font = cls._font(family, max(8, px))
            while px > int(14 * s):
                try:
                    bb = draw.textbbox((0, 0), shaped, font=main_font,
                                       stroke_width=2)
                except (ValueError, OSError):
                    break
                if bb[2] - bb[0] <= max_text_w:
                    break
                px = int(px * 0.85)
                main_font = cls._font(family, max(8, px))

        gap = int(14 * s)
        # Heights: [sample, pangram, coverage].
        if sample is not None:
            sample_h = sample[1][3] - sample[1][1]
        elif shaped and main_font is not None:
            try:
                bb = draw.textbbox((0, 0), shaped, font=main_font,
                                   stroke_width=2)
                sample_h = bb[3] - bb[1]
            except (ValueError, OSError):
                sample_h = 0
        else:
            sample_h = 0
        heights = [sample_h]
        for f_, t_ in ((pan_font, pangram), (meta_font, coverage)):
            if not t_:
                heights.append(0)
                continue
            try:
                bb = draw.textbbox((0, 0), t_, font=f_, stroke_width=2)
            except (ValueError, OSError):
                bb = (0, 0, 0, 0)
            heights.append(bb[3] - bb[1])
        total = sum(hh for hh in heights if hh) + gap * (
            max(0, sum(1 for hh in heights if hh) - 1)
        )
        y = (H - total) // 2

        if heights[0]:
            if sample is not None:
                cls._blit_mask(img, sample[0], sample[1], W // 2, y, color,
                               stroke=max(2, int(2 * s)))
            else:
                draw.text((W // 2, y), shaped, font=main_font, fill=color,
                          anchor="ma", stroke_width=max(2, int(2 * s)),
                          stroke_fill="black")
            y += heights[0] + gap
        if heights[1]:
            draw.text((W // 2, y), pangram, font=pan_font, fill=color,
                      anchor="ma", stroke_width=max(1, int(1.5 * s)),
                      stroke_fill="black")
            y += heights[1] + gap
        if heights[2]:
            draw.text((W // 2, y), coverage, font=meta_font,
                      fill=cls.META_FG, anchor="ma")

        radius = int(8 * s)
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, W - 1, H - 1), radius=radius, fill=255
        )
        img.putalpha(mask)
        return img


# ----------------------------------------
# Native RTL text input (Windows)
# ----------------------------------------
def _first_strong_rtl(text: str) -> Optional[bool]:
    """Base direction of `text` from its first strong directional character:
    True (RTL) for Arabic/Hebrew, False (LTR) for Latin, None if it has none."""
    for ch in text:
        o = ord(ch)
        if (0x0590 <= o <= 0x08FF or 0xFB1D <= o <= 0xFDFF
                or 0xFE70 <= o <= 0xFEFF):
            return True
        if (0x41 <= o <= 0x5A or 0x61 <= o <= 0x7A or 0x00C0 <= o <= 0x024F):
            return False
    return None


class _NativeRTLEdit:
    """A multi-line native RichEdit hosted inside a Tk frame.

    Tk's Entry has no bidi caret/selection/hit-testing: with Arabic content
    the caret draws at the mirrored position and clicking into the text
    places the insertion point at the mirrored index. The native RichEdit
    does all of that correctly, styles darkly without subclassing the parent
    (EM_SETBKGNDCOLOR / EM_SETCHARFORMAT), and switches paragraph direction
    per content. Raises on any failure — callers fall back to a Tk entry.
    """

    _WM_KEYDOWN = 0x0100
    _WM_CHAR = 0x0102
    _VK_RETURN = 0x0D
    _VK_ESCAPE = 0x1B
    _EM_SETBKGNDCOLOR = 0x0443
    _EM_SETCHARFORMAT = 0x0444
    _EM_SETPARAFORMAT = 0x0447
    _SCF_ALL = 0x0004
    _CFM_COLOR = 0x40000000
    _CFM_FACE = 0x20000000
    _CFM_SIZE = 0x80000000
    _PFM_RTLPARA = 0x00010000
    _PFE_RTLPARA = 0x0001
    _EM_LINEFROMCHAR = 0x00C9
    _GWLP_WNDPROC = -4

    def __init__(
        self,
        frame: Any,
        initial: str,
        logical_px: int,
        text_color: str,
        bg_color: str,
    ) -> None:
        u32 = ctypes.windll.user32
        if not ctypes.windll.kernel32.LoadLibraryW("msftedit.dll"):
            raise OSError("msftedit.dll unavailable")
        self._u32 = u32
        self._last_para: Optional[Tuple[int, bool]] = None
        # Set by the wndproc, consumed by the owner's Tk-side poll.
        # The wndproc must NOT call into Tcl itself: it can run while
        # _tkinter has released the Python thread state (during message
        # dispatch), and a nested Tcl call then aborts the interpreter
        # ("PyEval_RestoreThread: thread state is NULL").
        self.pending_key: Optional[str] = None

        WS_CHILD, WS_VISIBLE = 0x40000000, 0x10000000
        ES_MULTILINE, ES_WANTRETURN, ES_AUTOVSCROLL = 0x0004, 0x1000, 0x0040
        ES_CENTER = 0x0001  # center every line (both languages read centered)
        self.hwnd = u32.CreateWindowExW(
            0, "RICHEDIT50W", "",
            WS_CHILD | WS_VISIBLE | ES_MULTILINE | ES_WANTRETURN
            | ES_AUTOVSCROLL | ES_CENTER,
            0, 0, 10, 10, frame.winfo_id(), 0, 0, 0,
        )
        if not self.hwnd:
            raise OSError("RichEdit creation failed")

        send = u32.SendMessageW
        send.restype = ctypes.c_ssize_t
        send.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p,
        ]
        self._send = send

        def colorref(hex_color: str) -> int:
            r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
            return r | (g << 8) | (b << 16)

        send(self.hwnd, self._EM_SETBKGNDCOLOR, 0, colorref(bg_color))

        class CHARFORMATW(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("dwMask", ctypes.c_uint32),
                ("dwEffects", ctypes.c_uint32),
                ("yHeight", ctypes.c_int32),
                ("yOffset", ctypes.c_int32),
                ("crTextColor", ctypes.c_uint32),
                ("bCharSet", ctypes.c_byte),
                ("bPitchAndFamily", ctypes.c_byte),
                ("szFaceName", ctypes.c_wchar * 32),
            ]

        cf = CHARFORMATW()
        cf.cbSize = ctypes.sizeof(CHARFORMATW)
        cf.dwMask = self._CFM_COLOR | self._CFM_FACE | self._CFM_SIZE
        cf.yHeight = logical_px * 15  # logical px -> twips at 96 dpi
        cf.crTextColor = colorref(text_color)
        cf.szFaceName = "Segoe UI"
        send(self.hwnd, self._EM_SETCHARFORMAT, self._SCF_ALL,
             ctypes.cast(ctypes.byref(cf), ctypes.c_void_p))

        u32.SetWindowTextW(self.hwnd, initial.replace("\n", "\r\n"))
        self.sync_direction()  # direction of the first line; others follow live

        # Subclass the control for Enter/Escape (Tk bindings can't see keys
        # while a native child owns the keyboard focus).
        is64 = ctypes.sizeof(ctypes.c_void_p) == 8
        LRESULT = ctypes.c_longlong if is64 else ctypes.c_long
        WPARAM = ctypes.c_ulonglong if is64 else ctypes.c_ulong
        LPARAM = LRESULT
        self._WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, ctypes.c_void_p, ctypes.c_uint, WPARAM, LPARAM
        )
        self._call_next = u32.CallWindowProcW
        self._call_next.restype = LRESULT
        self._call_next.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, WPARAM, LPARAM,
        ]
        self._set_wndproc = getattr(u32, "SetWindowLongPtrW",
                                    u32.SetWindowLongW)
        self._set_wndproc.restype = ctypes.c_void_p
        self._set_wndproc.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
        ]
        self._proc_ref = self._WNDPROC(self._wndproc)  # keep alive: GC = crash
        self._old_proc = self._set_wndproc(
            self.hwnd, self._GWLP_WNDPROC,
            ctypes.cast(self._proc_ref, ctypes.c_void_p),
        )

    def _wndproc(self, hwnd: Any, msg: int, wparam: int, lparam: int) -> int:
        # Plain attribute writes only — no Tcl calls here (see pending_key).
        # Enter now falls through to the control so it inserts a newline
        # (multi-line); the dialog's Save button commits. Only Escape is caught.
        if msg == self._WM_KEYDOWN and wparam == self._VK_ESCAPE:
            self.pending_key = "escape"
            return 0
        if msg == self._WM_CHAR and wparam == self._VK_ESCAPE:
            return 0  # swallow the beep
        return self._call_next(self._old_proc, hwnd, msg, wparam, lparam)

    def _caret_line_rtl(self) -> bool:
        """Base direction of the line holding the caret, from its first strong
        character — so an English line reads L-to-R even beside an Arabic one.
        A neutral/empty line follows the UI language."""
        idx = self._send(self.hwnd, self._EM_LINEFROMCHAR,
                         ctypes.c_size_t(-1).value, None)
        lines = self.get().split("\n")
        line = lines[idx] if 0 <= idx < len(lines) else ""
        d = _first_strong_rtl(line)
        return is_rtl() if d is None else d

    def sync_direction(self) -> None:
        """Center + set the CARET line's base direction (per paragraph). No-op
        while the caret stays on a same-direction line."""
        idx = self._send(self.hwnd, self._EM_LINEFROMCHAR,
                         ctypes.c_size_t(-1).value, None)
        rtl = self._caret_line_rtl()
        if (idx, rtl) == self._last_para:
            return
        self._last_para = (idx, rtl)

        class PARAFORMAT2(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_uint),
                ("dwMask", ctypes.c_uint32),
                ("wNumbering", ctypes.c_uint16),
                ("wEffects", ctypes.c_uint16),
                ("dxStartIndent", ctypes.c_int32),
                ("dxRightIndent", ctypes.c_int32),
                ("dxOffset", ctypes.c_int32),
                ("wAlignment", ctypes.c_uint16),
                ("cTabCount", ctypes.c_int16),
                ("rgxTabs", ctypes.c_int32 * 32),
                ("dySpaceBefore", ctypes.c_int32),
                ("dySpaceAfter", ctypes.c_int32),
                ("dyLineSpacing", ctypes.c_int32),
                ("sStyle", ctypes.c_int16),
                ("bLineSpacingRule", ctypes.c_byte),
                ("bOutlineLevel", ctypes.c_byte),
                ("wShadingWeight", ctypes.c_uint16),
                ("wShadingStyle", ctypes.c_uint16),
                ("wNumberingStart", ctypes.c_uint16),
                ("wNumberingStyle", ctypes.c_uint16),
                ("wNumberingTab", ctypes.c_uint16),
                ("wBorderSpace", ctypes.c_uint16),
                ("wBorderWidth", ctypes.c_uint16),
                ("wBorders", ctypes.c_uint16),
            ]

        pf = PARAFORMAT2()
        pf.cbSize = ctypes.sizeof(PARAFORMAT2)
        pf.dwMask = self._PFM_RTLPARA
        pf.wEffects = self._PFE_RTLPARA if rtl else 0
        self._send(self.hwnd, self._EM_SETPARAFORMAT, 0,
                   ctypes.cast(ctypes.byref(pf), ctypes.c_void_p))

    def place(self, x: int, y: int, w: int, h: int) -> None:
        self._u32.MoveWindow(self.hwnd, x, y, w, h, True)

    def focus(self) -> None:
        self._u32.SetFocus(self.hwnd)

    def get(self) -> str:
        n = self._u32.GetWindowTextLengthW(self.hwnd) + 1
        buf = ctypes.create_unicode_buffer(n)
        self._u32.GetWindowTextW(self.hwnd, buf, n)
        # RichEdit separates lines with \r\n (or bare \r) — normalize to \n so
        # callers (and the renderer's text.split("\n")) see plain newlines.
        return buf.value.replace("\r\n", "\n").replace("\r", "\n")

    def restore_wndproc(self) -> None:
        """Un-subclass before teardown so a late message can't hit a dead
        Python callback."""
        try:
            if self._old_proc:
                self._set_wndproc(
                    self.hwnd, self._GWLP_WNDPROC, self._old_proc
                )
        except Exception:  # noqa: BLE001
            pass


# ----------------------------------------
# System Tray Icon Management
# ----------------------------------------
class TrayIconManager:
    """Manage system tray icon and menu."""
    _icon_images: Dict[str, Optional[Image.Image]] = {"running": None, "stopped": None}

    # Settings > Apps > Startup (and Task Manager's Startup tab) keep a per-
    # shortcut on/off flag here, keyed by the .lnk file name.
    _STARTUP_APPROVED_KEY = (
        r"Software\Microsoft\Windows\CurrentVersion\Explorer"
        r"\StartupApproved\StartupFolder"
    )

    @staticmethod
    def _startup_shortcut_path() -> Optional[str]:
        """Get path to startup shortcut.

        A test instance (ASC_INSTANCE_TOKEN) keeps its "Startup folder" inside its
        own private data folder, so toggling autostart in a test build can never
        repoint or create the real user's login shortcut."""
        if INSTANCE_TOKEN:
            startup = APPDATA_DIR / "Startup"
            try:
                startup.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            return str(startup / f"{APP_NAME}.lnk")
        if not IS_WINDOWS:
            return None
        try:
            startup = winshell.startup()
        except Exception:
            startup = os.path.join(
                os.getenv("APPDATA", ""),
                "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
            )
        return os.path.join(startup, f"{APP_NAME}.lnk")

    @staticmethod
    def _autostart_command(
        frozen: bool, executable: str, script: str, minimized: bool
    ) -> Tuple[str, str, str]:
        """(target, arguments, working dir) for the login shortcut. A packaged
        build starts its own exe; a source run starts Python with the script."""
        if frozen:
            return (executable, "--minimized" if minimized else "",
                    os.path.dirname(executable))
        args = f'"{script}"' + (" --minimized" if minimized else "")
        return executable, args, os.path.dirname(script)

    @staticmethod
    def _shortcut_points_here(
        target: str, args: str, frozen: bool, executable: str, script: str
    ) -> bool:
        """Does an existing shortcut launch THIS copy of the app?"""
        if os.path.normcase(target.strip()) != os.path.normcase(executable):
            return False
        return frozen or script.lower() in args.lower()

    @staticmethod
    def _startup_entry_disabled(data: Optional[bytes]) -> bool:
        """True when Windows' Startup-apps switch is OFF for the shortcut (an odd
        first byte in its StartupApproved value; no value means on)."""
        return bool(data) and bool(data[0] & 1)

    @staticmethod
    def _read_startup_approved() -> Optional[bytes]:
        if INSTANCE_TOKEN or not IS_WINDOWS:
            return None
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                TrayIconManager._STARTUP_APPROVED_KEY) as key:
                value, _ = winreg.QueryValueEx(key, f"{APP_NAME}.lnk")
            return bytes(value) if value else None
        except OSError:
            return None

    @staticmethod
    def _clear_startup_disabled() -> None:
        """Turning autostart on in the app also turns Windows' switch back on
        for our shortcut (deleting the flag = on), or the shortcut would exist
        but never run."""
        if INSTANCE_TOKEN or not IS_WINDOWS:
            return
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                TrayIconManager._STARTUP_APPROVED_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, f"{APP_NAME}.lnk")
        except OSError:
            pass

    @staticmethod
    def is_autostart_enabled(item: Any = None) -> bool:
        """Check if autostart is enabled.

        Verifies not just that the .lnk exists but that it still points at THIS
        app (packaged: this exe; source: this Python + this script) and that
        Windows' Startup-apps switch hasn't turned it off. A shortcut left behind
        by a moved install otherwise reports "enabled" while launching the wrong
        (or a missing) path.
        """
        p = TrayIconManager._startup_shortcut_path()
        if not (p and os.path.exists(p)):
            return False
        if not IS_WINDOWS:
            return True
        if TrayIconManager._startup_entry_disabled(
                TrayIconManager._read_startup_approved()):
            return False
        try:
            shell = Dispatch("WScript.Shell")
            link = shell.CreateShortCut(p)  # reads an existing .lnk
            return TrayIconManager._shortcut_points_here(
                link.TargetPath or "", link.Arguments or "", is_frozen(),
                os.path.abspath(sys.executable), os.path.abspath(sys.argv[0]))
        except Exception as e:
            # Shortcut exists but is unreadable — assume enabled rather than
            # silently flipping the menu state.
            logger.debug(f"Could not inspect startup shortcut: {e}")
            return True

    @staticmethod
    def set_autostart(enabled: bool) -> None:
        """Enable or disable autostart."""
        if not IS_WINDOWS:
            return

        shortcut = TrayIconManager._startup_shortcut_path()
        if not shortcut:
            return

        try:
            if enabled:
                target, args, workdir = TrayIconManager._autostart_command(
                    is_frozen(), os.path.abspath(sys.executable),
                    os.path.abspath(sys.argv[0]),
                    app_state.config.start_minimized)
                shell = Dispatch("WScript.Shell")
                link = shell.CreateShortCut(shortcut)
                link.TargetPath = target
                link.Arguments = args
                link.WorkingDirectory = workdir
                try:
                    # The exe carries its own icon; a source run uses the .ico.
                    link.IconLocation = (f"{target},0" if is_frozen()
                                         else str(ICON_PATH))
                except Exception:
                    pass
                link.save()
                TrayIconManager._clear_startup_disabled()
                logger.info(f"Created startup shortcut: {shortcut}")
            else:
                if os.path.exists(shortcut):
                    os.remove(shortcut)
                logger.info(f"Removed startup shortcut: {shortcut}")
        except Exception as e:
            logger.error(f"Failed to update startup shortcut: {e}")

    @staticmethod
    def toggle_autostart(icon: Any, item: Any) -> None:
        """Toggle autostart setting."""
        def _apply():
            new_state = not TrayIconManager.is_autostart_enabled()
            TrayIconManager.set_autostart(new_state)
            try:
                if icon:
                    icon.update_menu()
            except Exception:
                pass

        # Marshal the shortcut COM I/O onto the GUI thread, mirroring
        # toggle_start_minimized — keeps all tray callbacks off the pystray thread.
        if app_state.root:
            app_state.root.after(0, _apply)
        else:
            _apply()

    @staticmethod
    def toggle_start_minimized(icon: Any, item: Any) -> None:
        """Toggle start-minimized setting."""
        def _apply():
            app_state.config.start_minimized = not app_state.config.start_minimized
            app_state.config.save()
            # Re-create shortcut with updated args if autostart is enabled
            if TrayIconManager.is_autostart_enabled():
                TrayIconManager.set_autostart(True)
            try:
                if icon:
                    icon.update_menu()
            except Exception:
                pass

        # Marshal the config mutation + save onto the GUI thread so it can't
        # race a concurrent ConfigManager.save_config() there (config writes are
        # not otherwise synchronized). Mirrors every other tray callback.
        if app_state.root:
            app_state.root.after(0, _apply)

    @staticmethod
    def _create_status_image(state: str = "stopped") -> Image.Image:
        """Create tray icon with status indicator."""
        try:
            base = Image.open(ICON_PATH).convert("RGBA").resize((64, 64), Image.LANCZOS)
        except Exception:
            base = Image.new("RGBA", (64, 64), "#2f3540")
        
        draw = ImageDraw.Draw(base)
        color = C_OK if state == "running" else C_DANGER
        draw.ellipse([44, 44, 62, 62], fill=color, outline="#111111")
        return base

    @staticmethod
    def _get_image(state: str) -> Image.Image:
        """Get cached tray icon image."""
        if TrayIconManager._icon_images[state] is None:
            TrayIconManager._icon_images[state] = TrayIconManager._create_status_image(state)
        return TrayIconManager._icon_images[state]

    @staticmethod
    def _build_menu() -> pystray.Menu:
        """Build tray icon menu."""
        running = app_state.monitoring_flag
        return pystray.Menu(
            # default=True → double-clicking the tray icon runs Show.
            MenuItem(t_raw("tray_show"), TrayIconManager.show_window, default=True),
            MenuItem(
                t_raw("tray_start_watching") if not running
                else t_raw("tray_stop_watching"),
                TrayIconManager.toggle_from_tray,
            ),
            MenuItem(t_raw("tray_change_folder"),
                     TrayIconManager.select_folder_from_tray),
            MenuItem(t_raw("apply_to_library"),
                     TrayIconManager.apply_ass_from_tray),
            MenuItem(
                t_raw("extract_all_subs"),
                TrayIconManager.extract_all_from_tray,
                enabled=app_state.ffmpeg_available,
            ),
            MenuItem(t_raw("restore_originals"),
                     TrayIconManager.restore_originals_from_tray),
            MenuItem(t_raw("open_log"), TrayIconManager.open_log_from_tray),
            MenuItem(t_raw("reset"), TrayIconManager.reset_from_tray),
            MenuItem(
                t_raw("autostart"),
                TrayIconManager.toggle_autostart,
                enabled=IS_WINDOWS,
                checked=lambda item: TrayIconManager.is_autostart_enabled(),
            ),
            MenuItem(
                t_raw("start_minimized"),
                TrayIconManager.toggle_start_minimized,
                # Callable so update_menu() refreshes its enabled state after
                # autostart is toggled (a constant would stay frozen).
                enabled=lambda item: IS_WINDOWS and TrayIconManager.is_autostart_enabled(),
                checked=lambda item: app_state.config.start_minimized,
            ),
            MenuItem(t_raw("tray_quit"), TrayIconManager.on_quit),
        )

    @staticmethod
    def update_tray_visuals() -> None:
        """Update tray icon appearance."""
        if app_state.tray_icon:
            try:
                state = "running" if app_state.monitoring_flag else "stopped"
                app_state.tray_icon.icon = TrayIconManager._get_image(state)
                app_state.tray_icon.menu = TrayIconManager._build_menu()
            except Exception:
                pass

    @staticmethod
    def on_quit(icon: Any, item: Any) -> None:
        """Handle quit from tray."""
        def _quit():
            app_state.shutdown()
            try:
                icon.stop()
            except Exception:
                pass
            try:
                if app_state.root:
                    app_state.root.destroy()
            except Exception:
                pass

        if app_state.root:
            app_state.root.after(0, _quit)
        else:
            # No Tk root (e.g. quit before the window was built) — tear down
            # directly so the tray "Quit" still exits instead of no-op'ing.
            _quit()

    @staticmethod
    def show_window(icon: Any, item: Any) -> None:
        """Show main window from tray."""
        def _show():
            try:
                if app_state.root:
                    root = app_state.root
                    root.deiconify()
                    root.attributes("-alpha", 1)
                    root.state("normal")
                    GUI._center_window(root, 780, 560)
                    root.lift()
                    root.focus_force()
                icon.stop()
                app_state.tray_icon = None
            except Exception as e:
                logger.error(f"Show window failed: {e}")

        if app_state.root:
            app_state.root.after(0, _show)
        else:
            # No window to show; at least stop the tray icon so the menu action
            # isn't a silent no-op.
            try:
                icon.stop()
            except Exception:
                pass
            app_state.tray_icon = None

    @staticmethod
    def minimize_to_tray() -> None:
        """Minimize window to system tray."""
        if app_state.root:
            app_state.root.withdraw()

        if app_state.tray_icon:
            TrayIconManager.update_tray_visuals()
            return

        try:
            state = "running" if app_state.monitoring_flag else "stopped"
            icon_image = TrayIconManager._get_image(state)
        except Exception as e:
            logger.error(f"Failed to build tray icon: {e}")
            icon_image = Image.new("RGBA", (64, 64), "blue")

        icon = Icon(
            APP_NAME,
            icon_image,
            APP_NAME,
            menu=TrayIconManager._build_menu()
        )

        def run_icon():
            try:
                icon.run()
            except Exception as e:
                logger.error(f"Tray icon died: {e}")

        threading.Thread(target=run_icon, daemon=True).start()
        app_state.tray_icon = icon

        # Windows 11 hides new tray icons, so the app seems to vanish. Say once
        # where it went.
        if not app_state.config.tray_hint_shown:
            app_state.config.tray_hint_shown = True
            if app_state.root is not None:
                app_state.root.after(0, lambda: ConfigManager.save_config(
                    app_state.monitored_folder))

            def _hint() -> None:
                time.sleep(1.5)  # the icon must be on screen before a balloon
                try:
                    icon.notify(t_logical("tray_hint"), APP_NAME)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"Tray hint failed: {e}")

            threading.Thread(target=_hint, name="tray-hint", daemon=True).start()

    @staticmethod
    def toggle_from_tray(icon: Any, item: Any) -> None:
        """Toggle monitoring from tray."""
        if app_state.root:
            app_state.root.after(
                0, 
                lambda: (GUI.toggle_monitoring(), TrayIconManager.update_tray_visuals())
            )

    @staticmethod
    def select_folder_from_tray(icon: Any, item: Any) -> None:
        """Select folder from tray."""
        def _select():
            try:
                if app_state.root:
                    app_state.root.deiconify()
                    app_state.root.attributes("-alpha", 1)
                    app_state.root.state("normal")
                    app_state.root.lift()
                    app_state.root.focus_force()
                GUI.select_folder()
                icon.stop()
                app_state.tray_icon = None
            except Exception as e:
                logger.error(f"Select folder from tray failed: {e}")
        
        if app_state.root:
            app_state.root.after(0, _select)

    @staticmethod
    def apply_ass_from_tray(icon: Any, item: Any) -> None:
        """Apply to ASS files from tray."""
        if app_state.root:
            app_state.root.after(0, GUI.process_existing_ass)

    @staticmethod
    def extract_all_from_tray(icon: Any, item: Any) -> None:
        """Extract all subtitles from tray."""
        if app_state.root:
            app_state.root.after(0, GUI.process_all_extraction)

    @staticmethod
    def restore_originals_from_tray(icon: Any, item: Any) -> None:
        """Restore originals from tray."""
        if app_state.root:
            app_state.root.after(0, GUI.restore_originals)

    @staticmethod
    def open_log_from_tray(icon: Any, item: Any) -> None:
        """Open log file from tray."""
        if app_state.root:
            app_state.root.after(0, GUI.open_log_file)

    @staticmethod
    def reset_from_tray(icon: Any, item: Any) -> None:
        """Reset app from tray."""
        if app_state.root:
            app_state.root.after(0, GUI.reset_app)


# ----------------------------------------
# GUI Construction
# ----------------------------------------
class GUI:
    """Main GUI construction and handlers."""
    _save_timer_id: Optional[str] = None

    # Shared CTkFonts (created once after the root exists — Tk requirement).
    fonts: Dict[str, ctk.CTkFont] = {}

    # Studio-redesign widget refs that have no legacy app_state slot.
    _watch_switch: Optional[ctk.CTkSwitch] = None
    _footer_prefix: Optional[ctk.CTkLabel] = None
    _footer_name: Optional[ctk.CTkLabel] = None
    _footer_suffix: Optional[ctk.CTkLabel] = None
    _preview_image: Optional[ctk.CTkImage] = None
    _preview_size: Tuple[int, int] = (748, 300)
    _preview_resize_job: Optional[str] = None
    _style_cells: Dict[str, ctk.CTkButton] = {}
    _size_cells: Dict[str, ctk.CTkButton] = {}
    _nudge_minus: Optional[ctk.CTkButton] = None
    _nudge_plus: Optional[ctk.CTkButton] = None
    _nudge_readout: Optional[ctk.CTkLabel] = None
    # Real-keyframe preview background (a still from the watched library).
    # Off by default — the well shows the black placeholder until toggled on.
    _preview_bg: Optional[Any] = None
    _preview_bg_on: bool = False
    _preview_from_sub: bool = False       # frame source: subtitle line vs my text
    _preview_line_text: Optional[str] = None  # the current sub line, if any
    _preview_bg_gen: int = 0
    _shuffle_btn: Optional[ctk.CTkButton] = None
    _bg_switch: Optional[ctk.CTkSwitch] = None
    _bg_mode_seg: Optional[ctk.CTkSegmentedButton] = None
    _bg_sub_label: str = ""               # the "Subtitle line" segment's value
    # Restrict main-window font cycling (arrows / wheel) to favorites only.
    _cycle_favorites_only: bool = False
    _fav_cycle_btn: Optional[Any] = None
    _apply_scope_dlg: Optional[Any] = None  # the open "apply scope" chooser

    @staticmethod
    def _debounced_save() -> None:
        """Save config after a short delay to avoid writing on every keystroke."""
        if app_state.root:
            if GUI._save_timer_id:
                app_state.root.after_cancel(GUI._save_timer_id)
            GUI._save_timer_id = app_state.root.after(
                500, lambda: ConfigManager.save_config(app_state.monitored_folder)
            )

    @staticmethod
    def _debounced_preview() -> None:
        """Coalesce rapid preview re-renders (typing in the font box, wheel/arrow
        font cycling) into one render ~120ms after the last change. A cold
        render can spike >200ms at 250% DPI, so rendering synchronously on every
        keystroke/notch janks the GUI thread; this schedules a single catch-up
        render instead. Shares the resize debounce token — both just schedule
        update_preview, so the newest trigger wins."""
        if app_state.root:
            if GUI._preview_resize_job:
                app_state.root.after_cancel(GUI._preview_resize_job)
            GUI._preview_resize_job = app_state.root.after(120, GUI.update_preview)

    # ------------------------------------------------------------------
    # Studio building blocks
    # ------------------------------------------------------------------
    UI_FAMILY = "Segoe UI"  # spec's UI font; clean Arabic, unlike ctk's Roboto

    @staticmethod
    def _build_fonts() -> None:
        """Create the shared CTkFont set (idempotent; needs a live root).

        Arabic glyphs (dots + harakat) read poorly at the Latin UI sizes, so
        the text fonts get a couple of px in RTL. Mono/glyph fonts (Latin
        paths, counts, B/I/U) keep their size. The family is pinned to Segoe
        UI so Arabic doesn't fall back from ctk's bundled Roboto.
        """
        if GUI.fonts:
            return
        fam = GUI.UI_FAMILY
        d = 2 if is_rtl() else 0
        GUI.fonts = {
            "section": ctk.CTkFont(family=fam, size=10 + d, weight="bold"),
            "value": ctk.CTkFont(family=fam, size=14 + d, weight="bold"),
            "title": ctk.CTkFont(family=fam, size=13 + d, weight="bold"),
            "body": ctk.CTkFont(family=fam, size=13 + d),
            "body_med": ctk.CTkFont(family=fam, size=12 + d),
            "meta": ctk.CTkFont(family=fam, size=12 + d),
            "mono": ctk.CTkFont(family=MONO_FAMILY, size=11),
            "mono_small": ctk.CTkFont(family=MONO_FAMILY, size=10),
            "chip_mono": ctk.CTkFont(family=MONO_FAMILY, size=10, weight="bold"),
            "pill": ctk.CTkFont(family=fam, size=10, weight="bold"),
            "seg_b": ctk.CTkFont(size=13, weight="bold"),
            "seg_i": ctk.CTkFont(size=13, slant="italic"),
            "seg_u": ctk.CTkFont(size=13, underline=True),
        }

    @staticmethod
    def _wrap(text: str, font_key: str, wraplength: int) -> str:
        """Pre-break Arabic text for a label with `wraplength`, so every
        wrapped line keeps its right-to-left base (i18n.wrap_rtl). CTkFont.measure
        and wraplength are both unscaled; 95% leaves room so Tk never breaks a
        line again. English text is returned unchanged."""
        return wrap_rtl(text, GUI.fonts[font_key].measure, wraplength * 0.95)

    @staticmethod
    def _btn_primary(master: Any, text: str, command: Any, width: int = 0) -> ctk.CTkButton:
        return ctk.CTkButton(
            master, text=text, command=command, width=width, height=32,
            corner_radius=8, fg_color=C_ACCENT, hover_color=C_ACCENT_HOVER,
            text_color=C_ON_ACCENT, font=GUI.fonts["title"],
        )

    @staticmethod
    def _btn_quiet(master: Any, text: str, command: Any, width: int = 0,
                   height: int = 32) -> ctk.CTkButton:
        return ctk.CTkButton(
            master, text=text, command=command, width=width, height=height,
            corner_radius=8, fg_color=C_QUIET, hover_color=C_QUIET_HOVER,
            text_color=C_TEXT_2, font=GUI.fonts["body"],
        )

    @staticmethod
    def _btn_tinted(master: Any, text: str, command: Any, width: int = 0,
                    height: int = 32) -> ctk.CTkButton:
        return ctk.CTkButton(
            master, text=text, command=command, width=width, height=height,
            corner_radius=8, fg_color=C_TINT_BG, hover_color=C_TINT_HOVER,
            text_color=C_TINT_TEXT, font=GUI.fonts["body_med"],
        )

    @staticmethod
    def _btn_danger(master: Any, text: str, command: Any, width: int = 0) -> ctk.CTkButton:
        return ctk.CTkButton(
            master, text=text, command=command, width=width, height=32,
            corner_radius=8, fg_color=C_DANGER, hover_color=C_DANGER_HOVER,
            text_color=C_ON_DANGER, font=GUI.fonts["title"],
        )

    @staticmethod
    def _center_position(win: Any, width: int, height: int) -> None:
        """Center `win` on its nearest monitor's work area.

        On Windows this uses PURE physical Win32 coords (GetWindowRect for the
        real window size, GetMonitorInfo for the work area, SetWindowPos to move
        it) — deliberately NOT winfo_screenwidth()/ctk scaling. Those two can
        drift out of sync after a display change (a GPU disable/enable from an
        HDMI-audio fix, a resolution swap): winfo_screenwidth() starts reporting
        PHYSICAL px while the code still multiplies by the 2.5 scaling, computing
        an offset ~2.5× too large and flinging the window to the far corner.
        Physical coords are immune to that. Falls back to the logical math only
        off-Windows or if the Win32 path fails."""
        if IS_WINDOWS and GUI._center_position_win32(win):
            return
        try:
            scaling = ctk.ScalingTracker.get_window_scaling(win)
        except Exception:  # noqa: BLE001
            scaling = 1.0
        x = max(0, int((win.winfo_screenwidth() - width) * scaling / 2))
        y = max(0, int((win.winfo_screenheight() - height) * scaling / 2))
        win.geometry(f"+{x}+{y}")

    @staticmethod
    def _center_position_win32(win: Any) -> bool:
        """Center via physical Win32 geometry. Returns True on success."""
        try:
            u = ctypes.windll.user32
            hwnd = u.GetAncestor(win.winfo_id(), 2) or win.winfo_id()  # GA_ROOT
            rect = _WINRECT()
            if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
                return False
            ww, wh = rect.right - rect.left, rect.bottom - rect.top
            mon = u.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if not u.GetMonitorInfoW(mon, ctypes.byref(mi)):
                return False
            w = mi.rcWork
            cx = w.left + max(0, (w.right - w.left - ww) // 2)
            cy = w.top + max(0, (w.bottom - w.top - wh) // 2)
            # SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            return bool(u.SetWindowPos(hwnd, 0, cx, cy, 0, 0,
                                       0x0001 | 0x0004 | 0x0010))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _center_window(win: Any, width: int, height: int) -> None:
        """Commit a CTk window's logical size, then center it on screen."""
        win.geometry(f"{width}x{height}")
        win.update_idletasks()
        GUI._center_position(win, width, height)

    _onscreen_job: Optional[str] = None

    @staticmethod
    def _ensure_on_screen(event: Any = None) -> None:
        """Re-center the main window if a restore stranded it off-screen.

        Bound to the root's <Map> (fires on every deiconify: taskbar restore,
        tray Show, second-launch surface). Windows parks iconified windows at
        -32000,-32000, ctk's titlebar withdraw/deiconify cycles can drop the
        position, and a GPU disable/enable (Fix-HDMI-Audio) rebuilds the display
        topology — any of which can leave the window unreachable. The check is
        deferred a beat so it judges the SETTLED position (mid-restore Tk can
        still report the stale park). _check_on_screen tests real monitor
        geometry, so a window legitimately on a second monitor is left alone."""
        root = app_state.root
        if root is None or (event is not None and event.widget is not root):
            return
        if GUI._onscreen_job is not None:
            try:
                root.after_cancel(GUI._onscreen_job)
            except Exception:  # noqa: BLE001
                pass
        GUI._onscreen_job = root.after(120, GUI._check_on_screen)

    @staticmethod
    def _check_on_screen() -> None:
        GUI._onscreen_job = None
        root = app_state.root
        if root is None:
            return
        try:
            # Only judge a visible window — leave it alone while it's in the tray
            # (withdrawn) or minimized (iconic), where an off-screen park is
            # expected and re-centering would fight the hide.
            try:
                if root.state() not in ("normal", "zoomed"):
                    return
            except Exception:  # noqa: BLE001
                pass

            graspable = _window_graspable(root.winfo_id())
            if graspable is None:
                # Non-Windows / API hiccup: fall back to virtual-screen bounds.
                try:
                    sc = ctk.ScalingTracker.get_window_scaling(root)
                except Exception:  # noqa: BLE001
                    sc = 1.0
                x, y = root.winfo_x(), root.winfo_y()
                w, h = root.winfo_width(), root.winfo_height()
                vw = int(root.winfo_screenwidth() * sc)
                vh = int(root.winfo_screenheight() * sc)
                graspable = not (
                    x + w - 40 < 0 or x + 40 > vw
                    or y + 40 > vh or y + h - 40 < 0
                )

            if not graspable:
                logger.info(
                    "Window is off-screen (a display change likely stranded it) "
                    "— re-centering"
                )
                GUI._center_window(root, 780, 560)
        except Exception as e:  # noqa: BLE001 — a guard must never break restore
            logger.debug(f"_ensure_on_screen skipped: {e}")

    @staticmethod
    def _studio_dialog(title: str, width: int, height: int,
                       resizable: bool = False) -> ctk.CTkToplevel:
        """Common dialog scaffold: dark bg, centered, transient + modal grab."""
        dlg = ctk.CTkToplevel(app_state.root, fg_color=C_WINDOW)
        # CTk creates the native window before the caller can populate it.
        # Keep it unmapped until its final layout has been centered, otherwise
        # Windows briefly paints it at the default side position.
        dlg.withdraw()
        # Stop ctk's LATER Windows titlebar-color cycles (resizable() and
        # appearance changes re-trigger them): each runs a withdraw/deiconify
        # dance that drops the window position — and when two cycles overlap,
        # can strand the dialog permanently withdrawn. The construction-time
        # cycle has already applied the dark titlebar by this point.
        try:
            dlg._deactivate_windows_window_header_manipulation = True
        except Exception:  # noqa: BLE001 — private ctk attr; harmless if gone
            pass
        dlg.title(title)

        # ctk stamps its own logo onto every CTkToplevel ~200ms after
        # creation; re-assert the app icon after that.
        def _set_icon() -> None:
            try:
                if dlg.winfo_exists():
                    dlg.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass

        dlg.after(300, _set_icon)
        dlg.geometry(f"{width}x{height}")
        dlg.transient(app_state.root)
        dlg.grab_set()
        if not resizable:
            dlg.resizable(False, False)

        # ctk 5.2's Windows titlebar-color dance runs once at construction and
        # again for resizable(); when the cycles overlap, the second can
        # capture a stale "withdrawn" state and leave the dialog permanently
        # hidden — and the late re-map may land at a default position instead
        # of the requested one. Re-show and re-center if that race hits.
        def _ensure_visible() -> None:
            try:
                if not dlg.winfo_exists():
                    return
                # Re-center with the CURRENT size (dialogs may auto-fit), then
                # map it. Positioning while withdrawn prevents a visible jump.
                dlg.update_idletasks()
                size = dlg.geometry().split("+")[0]
                cur_w, cur_h = (int(v) for v in size.split("x"))
                GUI._center_position(dlg, cur_w, cur_h)
                if dlg.state() == "withdrawn":
                    dlg.deiconify()
                    dlg.lift()
            except (tk.TclError, ValueError):
                pass

        dlg.after_idle(_ensure_visible)
        dlg.after(400, _ensure_visible)
        dlg.after(1000, _ensure_visible)
        return dlg

    # ---- One bilingual message dialog (replaces tkinter.messagebox) ----------
    # tkinter's message boxes are native MessageBoxW calls: no right-to-left
    # reading order, left-aligned text, and buttons in the WINDOWS language
    # (English buttons in the Arabic UI on English Windows). This Studio dialog
    # shows text in the UI's reading direction, mirrors its layout in Arabic and
    # labels every button in the UI language (ADR-006 §5).
    _MSG_GLYPHS = {"info": ("i", C_ACCENT), "warning": ("!", C_WARN),
                   "error": ("×", C_DANGER), "question": ("?", C_ACCENT)}

    @staticmethod
    def message(
        title: str,
        body: str,
        *,
        kind: str = "info",
        buttons: Optional[List[Tuple[str, str]]] = None,
        default: Optional[str] = None,
        cancel: Optional[str] = None,
        danger: Optional[str] = None,
        width: int = 460,
        parent: Any = None,
    ) -> Optional[str]:
        """Show a modal message and return the key of the button pressed
        (`cancel` on Escape or the window's close box).

        `buttons` = [(key, label), ...] in reading order; the first is the main
        action unless `default` names another. `danger` marks a destructive
        button. With no Tk window yet (a crash at startup) it falls back to a
        native right-to-left-aware message box."""
        buttons = list(buttons or []) or [("ok", t("ok"))]
        default = default or buttons[0][0]
        root = app_state.root
        try:
            usable = root is not None and bool(root.winfo_exists())
        except tk.TclError:
            usable = False
        if not usable:
            GUI._native_message(title, body, kind)
            return default
        # A dialog owned by a window hidden in the tray would be invisible.
        try:
            if root.state() == "withdrawn":
                GUI._surface_window()
        except tk.TclError:
            pass

        result: Dict[str, Optional[str]] = {"key": cancel}
        dlg = GUI._studio_dialog(title, width, 160)

        row = ctk.CTkFrame(dlg, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=(20, 8))
        glyph, colour = GUI._MSG_GLYPHS.get(kind, GUI._MSG_GLYPHS["info"])
        ctk.CTkLabel(
            row, text=glyph, width=30, height=30, corner_radius=15,
            fg_color=C_CARD, text_color=colour, font=GUI.fonts["title"],
        ).pack(side=SIDE_START(), anchor="n", padx=MPAD(0, 14))
        ctk.CTkLabel(
            row, text=GUI._wrap(body, "body", width - 100), font=GUI.fonts["body"],
            text_color=C_TEXT, anchor=ANCHOR_START(), justify=JUSTIFY_START(),
            wraplength=width - 100,
        ).pack(side=SIDE_START(), fill="x", expand=True)

        footer = ctk.CTkFrame(dlg, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(12, 16))

        def _choose(key: Optional[str]) -> None:
            result["key"] = key
            GUI._close_dialog(dlg)

        widgets: Dict[str, Any] = {}
        # Packed from the far end inwards, so the list reads in order: right-
        # aligned [main][other] in English, mirrored in Arabic.
        for key, label in reversed(buttons):
            if key == danger:
                btn = GUI._btn_danger(footer, label, lambda k=key: _choose(k))
            elif key == default:
                btn = GUI._btn_primary(footer, label, lambda k=key: _choose(k))
            else:
                btn = GUI._btn_quiet(footer, label, lambda k=key: _choose(k))
            btn.pack(side=SIDE_END(), padx=MPAD(8, 0))
            widgets[key] = btn

        dlg.bind("<Return>", lambda e: _choose(default))
        dlg.bind("<Escape>", lambda e: _choose(cancel))
        dlg.protocol("WM_DELETE_WINDOW", lambda: _choose(cancel))

        # Fit the height to the text (winfo_req* report physical pixels; the
        # geometry string takes logical ones at the window's scaling).
        try:
            dlg.update_idletasks()
            scale = ctk.ScalingTracker.get_window_scaling(dlg) or 1.0
            dlg.geometry(f"{width}x{int(dlg.winfo_reqheight() / scale) + 4}")
        except (tk.TclError, ValueError):
            pass
        try:
            dlg.focus_force()
        except tk.TclError:
            pass
        dlg.wait_window()
        # Hand the modal grab back to the dialog that asked (e.g. Reset).
        if parent is not None:
            try:
                if parent.winfo_exists():
                    parent.grab_set()
                    parent.focus_force()
            except tk.TclError:
                pass
        return result["key"]

    @staticmethod
    def _native_message(title: str, body: str, kind: str = "info") -> None:
        """Last-resort message when Tk isn't usable (a crash during startup):
        MessageBoxW with right-to-left reading order in the Arabic UI."""
        if not IS_WINDOWS:
            return
        flags = {"info": 0x40, "warning": 0x30, "error": 0x10}.get(kind, 0x40)
        if is_rtl():
            flags |= 0x00100000 | 0x00080000  # MB_RTLREADING | MB_RIGHT
        try:
            ctypes.windll.user32.MessageBoxW(None, body, title, flags | 0x00010000)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def show_info(title: str, body: str) -> None:
        GUI.message(title, body, kind="info")

    @staticmethod
    def show_warning(title: str, body: str) -> None:
        GUI.message(title, body, kind="warning")

    @staticmethod
    def show_error(title: str, body: str) -> None:
        GUI.message(title, body, kind="error")

    @staticmethod
    def ask_yes_no(title: str, body: str, yes: Optional[str] = None,
                   no: Optional[str] = None, danger: bool = False,
                   parent: Any = None) -> bool:
        key = GUI.message(
            title, body, kind="question",
            buttons=[("yes", yes or t("yes")), ("no", no or t("no"))],
            default="no" if danger else "yes", cancel="no",
            danger="yes" if danger else None, parent=parent,
        )
        return key == "yes"

    @staticmethod
    def _close_dialog(dlg: Any) -> None:
        try:
            dlg.grab_release()
            dlg.destroy()
        except tk.TclError:
            pass

    @staticmethod
    def _set_folder_display(folder: Optional[str]) -> None:
        """Repaint the header WATCH FOLDER value."""
        if app_state.folder_label is None:
            return
        if folder:
            text = _ellipsize(str(folder).replace("\\", "/"), 58)
            app_state.folder_label.configure(text=text, text_color=C_TEXT)
        else:
            app_state.folder_label.configure(
                text=t("not_selected"), text_color=C_MUTED
            )

    @staticmethod
    def _refresh_status_ui() -> None:
        """Repaint watch switch + header status + footer status line together.
        Main thread only."""
        watching = app_state.monitoring_flag

        switch = GUI._watch_switch
        if switch is not None:
            try:
                if watching and not switch.get():
                    switch.select()
                elif not watching and switch.get():
                    switch.deselect()
            except tk.TclError:
                pass

        if app_state.status_label is not None:
            if watching:
                n = app_state.styled_count
                files = (t_logical("files_one") if n == 1
                         else t_logical("files_many"))
                text = t("watching_count", n=n, files=files) if n \
                    else t("watching")
                app_state.status_label.configure(text=text, text_color=C_OK)
            else:
                app_state.status_label.configure(
                    text=t("not_watching"), text_color=C_MUTED
                )

        if app_state.led_indicator is not None:
            app_state.led_indicator.configure(
                text_color=C_OK if watching else C_DISABLED
            )

        if GUI._footer_prefix is not None:
            event = app_state.last_event
            if event:
                ts, verb, name = event
                rel = _relative_time(ts)
                # Footer parts are composed + ordered by packing → logical
                # (unwrapped) so the RTL marks don't nest across pieces.
                verb_txt = t_logical(_EVENT_VERB_KEY.get(verb, "ev_styled"))
                if watching:
                    prefix = f"{verb_txt} "
                    suffix = f" {rel}" if rel == t_logical("rel_just_now") \
                        else f" · {rel}"
                else:
                    prefix = t_logical("ev_idle_last", verb=verb_txt)
                    suffix = f" · {rel}"
                # Each piece is its own label, so each gets its own RTL base:
                # without it the " · " before the time hung at the far end.
                # The file name reads like Explorer instead (a batch count is
                # RTL-embedded where it was noted and kept as it is).
                GUI._footer_prefix.configure(text=rtl_base(prefix))
                GUI._footer_name.configure(text=ltr_name(_ellipsize(name, 48)))
                GUI._footer_suffix.configure(text=rtl_base(suffix))
            else:
                GUI._footer_prefix.configure(
                    text=t("watching_for_files") if watching else t("idle")
                )
                GUI._footer_name.configure(text="")
                GUI._footer_suffix.configure(text="")

    @staticmethod
    def _status_tick() -> None:
        """Periodic refresh so the footer's relative time stays honest."""
        GUI._refresh_status_ui()
        if app_state.root is not None:
            app_state.root.after(30_000, GUI._status_tick)

    @staticmethod
    def _refresh_style_cells() -> None:
        """Repaint the B/I/U segmented cells from the style BooleanVars."""
        flags = dict(zip("biu", app_state.get_style_flags()))
        for key, cell in GUI._style_cells.items():
            try:
                if flags.get(key):
                    cell.configure(
                        fg_color=C_ACCENT, hover_color=C_ACCENT_HOVER,
                        text_color=C_ON_ACCENT,
                    )
                else:
                    cell.configure(
                        fg_color=C_CARD, hover_color=C_QUIET_HOVER,
                        text_color=C_MUTED,
                    )
            except tk.TclError:
                pass

    @staticmethod
    def _refresh_size_cells() -> None:
        """Highlight the single active dialogue-size cell (single-select)."""
        for key, cell in GUI._size_cells.items():
            try:
                if key == app_state.font_size:
                    cell.configure(
                        fg_color=C_ACCENT, hover_color=C_ACCENT_HOVER,
                        text_color=C_ON_ACCENT,
                    )
                else:
                    cell.configure(
                        fg_color=C_CARD, hover_color=C_QUIET_HOVER,
                        text_color=C_MUTED,
                    )
            except tk.TclError:
                pass

    @staticmethod
    def _select_size(key: str) -> None:
        """Pick a dialogue-size preset. Doesn't touch the preview (the on-screen
        size is per-file/resolution-relative), just persists the choice."""
        app_state.font_size = key
        GUI._refresh_size_cells()
        GUI._refresh_nudge_control()   # nudge is a no-op without a preset
        GUI._debounced_save()

    @staticmethod
    def _current_font() -> str:
        return (app_state.font_combobox.get().strip()
                if app_state.font_combobox else "")

    @staticmethod
    def _bump_nudge(delta: float) -> None:
        """Step the CURRENT font's per-font size correction and persist it. The
        map stays minimal — a neutral 1.0 is stored as absence."""
        font = GUI._current_font()
        if not font:
            return
        cur = app_state.config.font_size_nudges.get(font, 1.0)
        new = normalize_size_nudge(cur + delta)
        nudges = app_state.config.font_size_nudges
        if new == 1.0:
            nudges.pop(font, None)
        else:
            nudges[font] = new
        GUI._refresh_nudge_control()
        GUI._debounced_save()

    @staticmethod
    def _refresh_nudge_control() -> None:
        """Sync the nudge readout (+N% / −N%) and enabled state to the current
        font and preset. Disabled when no size preset is active (no effect)."""
        if GUI._nudge_readout is None or GUI._nudge_minus is None \
                or GUI._nudge_plus is None:
            return
        active = app_state.font_size != "none" and bool(GUI._current_font())
        mult = app_state.config.font_size_nudges.get(GUI._current_font(), 1.0)
        pct = round((mult - 1.0) * 100)
        try:
            GUI._nudge_readout.configure(
                text=(f"+{pct}%" if pct > 0 else f"{pct}%"),
                text_color=C_TEXT if active else C_MUTED,
            )
            state = "normal" if active else "disabled"
            GUI._nudge_minus.configure(state=state)
            GUI._nudge_plus.configure(state=state)
        except tk.TclError:
            pass

    @staticmethod
    def update_font_list(event: Any) -> None:
        """Update preview when font entry changes."""
        if not app_state.font_combobox:
            return
        GUI._refresh_nudge_control()   # the nudge is per-font
        GUI._debounced_save()
        GUI._debounced_preview()  # coalesce per-keystroke renders (janky at 250% DPI)

    @staticmethod
    def _cycle_font(delta: int) -> str:
        """
        Move the font entry forward (delta=+1) or backward (delta=-1) through
        the loaded system-font list and update the preview live. Bound to
        arrow keys and the mouse wheel so the user can browse fonts without
        opening the picker dialog.
        """
        if not app_state.all_fonts or not app_state.font_combobox:
            return "break"

        # Favorites-only mode: walk just the starred fonts so the user can skip
        # past everything that doesn't fit their script. If the last favorite
        # was unstarred meanwhile, fall back to all fonts and drop the mode.
        fonts = app_state.all_fonts
        if GUI._cycle_favorites_only:
            favs = set(app_state.config.favorite_fonts or [])
            pool = [f for f in fonts if f in favs]
            if pool:
                fonts = pool
            else:
                GUI._cycle_favorites_only = False
                GUI._refresh_fav_cycle_btn()

        new_idx = _next_font_index(
            fonts,
            app_state.font_combobox.get(),
            delta,
        )
        if new_idx < 0:
            return "break"

        new_font = fonts[new_idx]
        try:
            app_state.font_combobox.delete(0, "end")
            app_state.font_combobox.insert(0, new_font)
        except tk.TclError:
            return "break"

        GUI._refresh_nudge_control()   # the nudge is per-font
        GUI._debounced_preview()  # coalesce rapid wheel/arrow cycling into one render
        GUI._debounced_save()
        return "break"  # don't let the arrow/wheel also move the cursor/scroll

    @staticmethod
    def _on_font_wheel(event: Any) -> str:
        # Windows fires MouseWheel with event.delta in ±120 steps; scroll
        # "up" (away from the user) should step to the previous font.
        delta = -1 if getattr(event, "delta", 0) > 0 else 1
        return GUI._cycle_font(delta)

    @staticmethod
    def _toggle_cycle_favorites(event: Any = None) -> str:
        """Toggle 'cycle favorites only' for the main-window font arrows/wheel."""
        favs = [f for f in (app_state.all_fonts or [])
                if f in set(app_state.config.favorite_fonts or [])]
        if not GUI._cycle_favorites_only and not favs:
            return "break"  # nothing starred yet — leave the mode off
        GUI._cycle_favorites_only = not GUI._cycle_favorites_only
        GUI._refresh_fav_cycle_btn()
        return "break"

    @staticmethod
    def _refresh_fav_cycle_btn() -> None:
        """Sync the star toggle's look to the current favorites-only state."""
        btn = GUI._fav_cycle_btn
        if btn is None:
            return
        on = GUI._cycle_favorites_only
        try:
            btn.configure(text="★" if on else "☆",
                          text_color="#f5b301" if on else C_TEXT_2)
        except tk.TclError:
            pass

    @staticmethod
    def choose_color() -> None:
        """Open color chooser dialog."""
        color = colorchooser.askcolor(title=t_raw("choose_color_title"))
        if color[0]:
            hex_color = color[1][1:]
            app_state.selected_color_code = rgb_to_bgr_hex(hex_color)

            if app_state.color_indicator:
                c = bgr_to_rgb_hex(app_state.selected_color_code)
                app_state.color_indicator.configure(fg_color=c, hover_color=c)
            
            ConfigManager.save_config(app_state.monitored_folder)
            logger.info(f"Selected color: {app_state.selected_color_code}")
            GUI.update_preview()

    @staticmethod
    def update_preview() -> None:
        """Re-render the preview well image from the current style settings."""
        label = app_state.preview_entry
        if label is None:
            return

        w, h = GUI._preview_size
        try:
            scale = ctk.ScalingTracker.get_widget_scaling(label)
        except Exception:  # noqa: BLE001
            scale = 1.0

        family = (
            app_state.font_combobox.get().strip()
            if app_state.font_combobox else ""
        ) or "Segoe UI"
        color = (
            bgr_to_rgb_hex(app_state.selected_color_code)
            if app_state.selected_color_code else "#ffffff"
        )
        bold, italic, underline = app_state.get_style_flags()
        # In "Subtitle line" mode the frame carries its own real line — show that
        # over it; otherwise the user's preview text. If that mode found no
        # subtitle at all (rare), show an honest note over the fallback frame
        # instead of silently rendering the preview text like "My text" mode.
        note = None
        sub_mode = GUI._preview_bg_on and GUI._preview_from_sub
        if sub_mode and GUI._preview_line_text:
            text = GUI._preview_line_text
        elif sub_mode and GUI._preview_bg is not None:
            text = ""
            note = t("no_sub_found")
        else:
            text = (app_state.preview_text or DEFAULT_PREVIEW_TEXT).strip() \
                or DEFAULT_PREVIEW_TEXT

        try:
            img = PreviewRenderer.render_well(
                w, h, scale, family=family, color=color,
                bold=bold, italic=italic, underline=underline, text=text,
                background=GUI._preview_bg if GUI._preview_bg_on else None,
                note=note,
            )
        except Exception as e:  # noqa: BLE001 — a render bug must not kill the UI
            logger.error(f"Preview render failed: {e}")
            return

        GUI._preview_image = ctk.CTkImage(
            light_image=img, dark_image=img, size=(w, h)
        )
        try:
            label.configure(image=GUI._preview_image, text="")
        except tk.TclError:
            pass

    @staticmethod
    def _shuffle_label() -> str:
        return _with_mark(t("shuffle_bg"), "⟳", lead=True)

    @staticmethod
    def _refresh_bg_controls() -> None:
        """The shuffle button + mode picker only matter while the toggle is on."""
        state = "normal" if GUI._preview_bg_on else "disabled"
        if GUI._shuffle_btn is not None:
            try:
                GUI._shuffle_btn.configure(state=state, text=GUI._shuffle_label())
            except tk.TclError:
                pass
        if GUI._bg_mode_seg is not None:
            try:
                # Off: keep the choice visible but grey. A bright accent segment
                # with ctk's dim disabled text was barely readable.
                selected = C_ACCENT_HOVER if GUI._preview_bg_on else C_QUIET_HOVER
                GUI._bg_mode_seg.configure(state=state, selected_color=selected,
                                           selected_hover_color=selected)
            except (tk.TclError, ValueError):
                pass

    @staticmethod
    def _load_bg_for_current_mode() -> bool:
        """Point _preview_bg / _preview_line_text at the current mode's saved
        frame. True if one was restored, False if that slot is empty."""
        saved = PreviewBackground.load_saved_frame(GUI._preview_from_sub)
        if saved is None:
            return False
        GUI._preview_bg, line = saved
        GUI._preview_line_text = line or None
        return True

    @staticmethod
    def _show_bg_for_current_mode() -> None:
        """Show the current mode's remembered frame, or fetch a fresh one if
        that mode never had a frame."""
        if GUI._load_bg_for_current_mode():
            GUI.update_preview()
            GUI._refresh_bg_controls()
        else:
            GUI.shuffle_preview_background()

    @staticmethod
    def toggle_preview_bg() -> None:
        """Switch the preview well between the black placeholder and a real
        library keyframe (the current mode's remembered one)."""
        GUI._preview_bg_on = bool(GUI._bg_switch.get()) if GUI._bg_switch \
            else not GUI._preview_bg_on
        app_state.config.preview_bg_enabled = GUI._preview_bg_on
        GUI._debounced_save()  # non-blocking: a synchronous fsync here janks the toggle
        GUI._preview_bg_gen += 1  # supersede any in-flight grab
        if GUI._preview_bg_on:
            GUI._show_bg_for_current_mode()
        else:
            GUI.update_preview()
            GUI._refresh_bg_controls()

    @staticmethod
    def _on_bg_mode(value: str) -> None:
        """Segmented picker changed source: 'My text' vs 'Subtitle line'. Each
        mode keeps its OWN last frame, so restore it (grab only if empty)."""
        GUI._preview_from_sub = (value == GUI._bg_sub_label)
        app_state.config.preview_bg_from_sub = GUI._preview_from_sub
        GUI._debounced_save()  # non-blocking: a synchronous fsync here janks the click
        GUI._preview_bg_gen += 1  # supersede the other mode's in-flight grab
        if GUI._preview_bg_on:
            GUI._show_bg_for_current_mode()

    @staticmethod
    def shuffle_preview_background() -> None:
        """Pull a fresh keyframe behind the preview (async — the ffprobe/ffmpeg
        calls block). In 'Subtitle line' mode it's a real line + the frame it
        plays on; otherwise a random frame. No-op without a folder + ffmpeg; a
        superseded or failed grab leaves the preview as-is."""
        folder = app_state.monitored_folder
        if not (folder and app_state.ffmpeg_available):
            return
        GUI._preview_bg_gen += 1
        gen = GUI._preview_bg_gen
        from_sub = GUI._preview_from_sub
        # Capture the selected font on the main thread — grab_line_frame keys
        # the subtitle language off what this font can render (no tofu).
        font = (app_state.font_combobox.get().strip()
                if app_state.font_combobox else "") or "Segoe UI"
        if GUI._shuffle_btn is not None:
            try:
                GUI._shuffle_btn.configure(state="disabled", text="…")
            except tk.TclError:
                pass

        def worker() -> None:
            line = ""
            img = None
            try:
                if from_sub:
                    res = PreviewBackground.grab_line_frame(folder, font)
                    if res is not None:
                        img, line = res
                    else:  # no sub lines found — fall back to a random frame
                        img = PreviewBackground.grab_random_frame(folder)
                else:
                    img = PreviewBackground.grab_random_frame(folder)
            except Exception as e:  # noqa: BLE001 — never let the worker die,
                logger.debug(f"Preview bg worker failed: {e}")  # or the button
                img = None                                      # stays stuck "…"

            def apply() -> None:
                if gen != GUI._preview_bg_gen:
                    return  # a newer shuffle superseded this one
                if img is not None:
                    GUI._preview_bg = img
                    GUI._preview_line_text = line or None
                    # Persist here (main thread, after the gen guard) so a
                    # superseded worker can't overwrite the disk with stale data
                    # and two workers can't interleave the .jpg/.txt writes.
                    # `from_sub` is this shuffle's mode → the right slot (a mode
                    # change bumps the gen, so a mismatched apply returns above).
                    PreviewBackground.save_frame(img, line, from_sub)
                    if GUI._preview_bg_on:
                        GUI.update_preview()
                GUI._refresh_bg_controls()

            if app_state.root is not None:
                try:
                    app_state.root.after(0, apply)
                except (tk.TclError, RuntimeError):
                    pass

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _on_preview_resized(event: Any) -> None:
        """Debounced re-render when the preview well changes size."""
        label = app_state.preview_entry
        if label is None:
            return
        try:
            scale = ctk.ScalingTracker.get_widget_scaling(label)
        except Exception:  # noqa: BLE001
            scale = 1.0
        w = max(int(event.width / scale), 40)
        h = max(int(event.height / scale), 40)
        if (abs(w - GUI._preview_size[0]) < 6
                and abs(h - GUI._preview_size[1]) < 6):
            return
        GUI._preview_size = (w, h)
        if app_state.root:
            if GUI._preview_resize_job:
                app_state.root.after_cancel(GUI._preview_resize_job)
            GUI._preview_resize_job = app_state.root.after(
                120, GUI.update_preview
            )

    @staticmethod
    def update_preview_and_save() -> None:
        """Update preview and save config."""
        ConfigManager.save_config(app_state.monitored_folder)
        GUI.update_preview()

    @staticmethod
    def on_close_request() -> None:
        """Prompt user to minimize to tray, quit, or cancel."""
        choice = GUI.message(
            t_raw("exit_title"), t_raw("minimize_to_tray_q"), kind="question",
            buttons=[("tray", t("keep_in_tray")), ("quit", t("quit_app")),
                     ("cancel", t("cancel"))],
            cancel="cancel",
        )
        if choice == "tray":
            TrayIconManager.minimize_to_tray()
        elif choice == "quit":
            app_state.shutdown()
            if app_state.tray_icon:
                try:
                    app_state.tray_icon.stop()
                except Exception:
                    pass
            if app_state.root:
                app_state.root.destroy()
        # "cancel" → do nothing

    @staticmethod
    def restart_app() -> None:
        """Relaunch the app as a fresh process, then exit this one.

        The UI language threads through every widget at build time, so a
        switch only takes effect on a restart — do it for the user instead
        of asking them to relaunch by hand.

        The single-instance mutex is the catch: the child runs its own
        "already running?" check at startup, so if this (about-to-die)
        instance still holds the named mutex, the child would just surface
        our window and exit instead of starting fresh. So we release the
        handle BEFORE spawning, and re-acquire it if the spawn fails so
        this instance keeps its singleton protection.
        """
        # Rebuild our own launch command. Drop --minimized: switching the
        # language is a deliberate in-window action, so the app should come
        # back visible even if it was originally autostarted into the tray.
        extra = [a for a in sys.argv[1:] if a.lower() != "--minimized"]
        argv = [sys.executable]
        if not getattr(sys, "frozen", False):
            argv.append(os.path.abspath(sys.argv[0]))
        argv += extra

        _instance_guard.release()

        try:
            kwargs: Dict[str, Any] = {"close_fds": True}
            if IS_WINDOWS:
                kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(argv, **kwargs)
        except Exception as e:  # noqa: BLE001
            logger.error(f"Restart failed to spawn a new instance: {e}")
            # Restore singleton protection so a later manual launch still
            # detects us, then fall back to the manual-restart prompt.
            _instance_guard.acquire()
            GUI.show_info(t_raw("restart_to_apply_title"),
                                t_raw("restart_to_apply"))
            return

        logger.info("Restarting to apply UI language change")
        app_state.shutdown()
        if app_state.tray_icon:
            try:
                app_state.tray_icon.stop()
            except Exception:
                pass
        if app_state.root:
            try:
                app_state.root.destroy()
            except Exception:
                pass

    @staticmethod
    def select_folder() -> None:
        """Open folder selection dialog."""
        if app_state.operation_in_progress:
            GUI.show_info(
                t_raw("busy_title"), t_raw("busy_body")
            )
            return

        # Switching folders silently stops monitoring; confirm so the user knows
        # the watch is ending rather than discovering it later.
        if app_state.monitoring_flag:
            if not GUI.ask_yes_no(
                t_raw("switch_folder_title"), t_raw("switch_folder_body"),
            ):
                return
            GUI.toggle_monitoring()

        folder = filedialog.askdirectory(
            initialdir=app_state.monitored_folder or None,
            title=t_raw("pick_folder_title"))
        if not folder:
            return

        app_state.monitored_folder = folder
        GUI._set_folder_display(folder)
        ConfigManager.save_config(folder)
        # Index existing .ass files off the UI thread so picking a large
        # folder doesn't freeze the window.
        threading.Thread(
            target=DatabaseManager.add_existing_files,
            args=(folder,),
            daemon=True,
        ).start()
        logger.info(f"Selected folder: {folder}")

    @staticmethod
    def toggle_monitoring(auto: bool = False) -> None:
        """Toggle folder monitoring on/off. `auto` = the start at launch: it
        never asks, so an unconfirmed folder simply isn't watched."""
        if app_state.monitoring_flag:
            # Stop monitoring
            app_state.monitoring_flag = False
            app_state.stop_event.set()
            # Break the monitor loop's change-wait so it stops promptly (it may
            # otherwise be parked until the next filesystem event / poll).
            app_state.wake_event.set()

            if app_state.monitoring_thread and app_state.monitoring_thread.is_alive():
                app_state.monitoring_thread.join(timeout=3.0)
            if app_state.monitoring_thread and app_state.monitoring_thread.is_alive():
                # Still inside a slow step (a NAS, a spun-down disk). Its stop
                # event stays set, so it exits when that step returns; keep the
                # reference so a quick re-Start waits for it.
                logger.warning("Monitoring thread did not stop within timeout")
            else:
                app_state.monitoring_thread = None

            GUI._refresh_status_ui()
            TrayIconManager.update_tray_visuals()
            logger.info("Monitoring stopped")
        else:
            # Start monitoring
            if not app_state.monitored_folder:
                GUI.show_warning(t_raw("warn_title"), t_raw("select_folder_first"))
                GUI._refresh_status_ui()  # snap the switch back off
                return

            # A watch with no font+colour (and extraction off) would flip to
            # "Watching" but restyle nothing — every apply is skipped for an
            # empty font. Mirror the auto-start guard (see create_gui) so the
            # switch can't start a silent do-nothing session.
            font = app_state.font_combobox.get() if app_state.font_combobox else ""
            extract = app_state.extract_enabled.get() if app_state.extract_enabled else False
            if not (extract or (font and app_state.selected_color_code)):
                GUI.show_warning(t_raw("warn_title"), t_raw("nothing_to_watch_for"))
                GUI._refresh_status_ui()  # snap the switch back off
                return

            # The first restyle of a folder rewrites every subtitle already in
            # it: confirm once per folder (with a count) before that happens.
            if font and app_state.selected_color_code and not GUI._folder_consented(
                    app_state.monitored_folder):
                if auto:
                    logger.info("Not starting the watch: this folder hasn't been confirmed yet")
                else:
                    GUI._ask_first_watch(app_state.monitored_folder)
                GUI._refresh_status_ui()  # stays off until confirmed
                return

            # Defense: if a previous monitor thread is still alive (e.g. a
            # stop didn't finish within the 3s join timeout), refuse to spawn
            # a second one against the same DB.
            if (
                app_state.monitoring_thread is not None
                and app_state.monitoring_thread.is_alive()
            ):
                logger.warning(
                    "Previous monitoring thread still alive; ignoring start request."
                )
                GUI.show_info(t_raw("busy_title"), t_raw("still_stopping"))
                GUI._refresh_status_ui()  # snap the switch back off
                return

            # Pre-existing videos are marked as processed inside the monitor
            # thread (see FolderMonitor.run) so this potentially-large folder
            # scan no longer runs on — and freezes — the UI thread.
            # A fresh stop event per watch session: the previous session's
            # event stays set forever, so its loop can never be revived.
            app_state.stop_event = threading.Event()
            app_state.wake_event.clear()  # drop any stale wake from a prior stop
            app_state.monitoring_flag = True
            app_state.styled_count = 0  # header count is per watching session

            # Construct the (dependency-injected) monitor bound to the global
            # app_state and run its loop on the background thread.
            _monitor = FolderMonitor()
            app_state.monitoring_thread = threading.Thread(
                target=_monitor.run,
                daemon=True
            )
            app_state.monitoring_thread.start()

            GUI._refresh_status_ui()
            TrayIconManager.update_tray_visuals()
            logger.info("Monitoring started")

    _first_watch_pending = False

    @staticmethod
    def _folder_consented(folder: Optional[str]) -> bool:
        return bool(folder) and folder_key(folder) in app_state.config.consented_folders

    @staticmethod
    def _risky_folder(folder: str) -> bool:
        """A whole drive, the user's profile, or a Windows/program folder —
        almost certainly picked by mistake, and every subtitle anywhere under
        it would be rewritten."""
        try:
            p = Path(folder).resolve()
        except OSError:
            return False
        if p.parent == p:
            return True
        try:
            if p == Path.home().resolve():
                return True
        except (OSError, RuntimeError):
            pass
        for var in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData",
                    "APPDATA", "LOCALAPPDATA"):
            value = os.environ.get(var)
            if not value:
                continue
            try:
                sysdir = Path(value).resolve()
            except OSError:
                continue
            if p == sysdir or sysdir in p.parents:
                return True
        return False

    @staticmethod
    def _ask_first_watch(folder: str) -> None:
        """Count what the first restyle would touch (off the UI thread: a big
        library on a NAS takes a while), then ask; on Yes remember the folder
        and start watching."""
        if GUI._first_watch_pending:
            return
        GUI._first_watch_pending = True

        def _count() -> None:
            n = 0
            try:
                known = DatabaseManager.get_monitored_files()
                done = {FileStatus.PROCESSED.value, FileStatus.RESTORED.value,
                        FileStatus.FOREIGN.value}
                for p in FileProcessor.list_ass_files(folder):
                    if known.get(str(p), {}).get("status") not in done:
                        n += 1
            except Exception as e:  # noqa: BLE001 — still ask, without a count
                logger.warning(f"Could not count subtitle files in {folder}: {e}")
                n = -1
            if app_state.root is not None:
                app_state.root.after(0, lambda: _ask(n))

        def _ask(n: int) -> None:
            GUI._first_watch_pending = False
            if n == 0:
                body = t_raw("first_watch_none", folder=folder)
            else:
                body = t_raw("first_watch_body", n=max(n, 0) if n >= 0 else "?",
                             folder=folder)
            risky = GUI._risky_folder(folder)
            if risky:
                body += "\n\n" + t_raw("first_watch_risky")
            choice = GUI.message(
                t_raw("first_watch_title"), body,
                kind="warning" if risky else "question",
                buttons=[("start", t("first_watch_start")), ("cancel", t("cancel"))],
                default="cancel" if risky else "start", cancel="cancel",
                danger="start" if risky else None, width=500,
            )
            if choice != "start":
                return
            key = folder_key(folder)
            if key not in app_state.config.consented_folders:
                app_state.config.consented_folders.append(key)
            ConfigManager.save_config(app_state.monitored_folder)
            if not app_state.monitoring_flag and app_state.monitored_folder == folder:
                GUI.toggle_monitoring()

        threading.Thread(target=_count, name="first-watch-count", daemon=True).start()

    # ---- Welcome, help and updates (ADR-006 §4/§7, ADR-008) ------------------
    _REPO_URL = "https://github.com/roko-tech/Anime-Subs-Customizer"

    @staticmethod
    def _open_url(url: str) -> None:
        try:
            os.startfile(url)
        except OSError as e:
            logger.error(f"Could not open {url}: {e}")

    @staticmethod
    def open_user_guide() -> None:
        page = "guide.ar.md" if is_rtl() else "guide.md"
        GUI._open_url(f"{GUI._REPO_URL}/blob/master/docs/{page}")

    @staticmethod
    def report_problem() -> None:
        """Open the bug form with the app and Windows versions filled in. Logs
        are never attached automatically (they contain folder names)."""
        import platform
        from urllib.parse import urlencode
        query = urlencode({
            "template": "bug.yml",
            "version": f"{APP_VERSION} ({'installed' if is_frozen() else 'source'})",
            "windows": f"Windows {platform.release()} ({platform.version()})",
        })
        GUI._open_url(f"{GUI._REPO_URL}/issues/new?{query}")

    @staticmethod
    def show_about() -> None:
        body = t_raw("about_body", version=APP_VERSION)
        latest = app_state.config.notified_update
        if latest and updates.recommended_version(latest):
            body += "\n\n" + t_raw("update_available_body", version=latest,
                                     current=APP_VERSION)
        choice = GUI.message(
            t_raw("menu_about"), body, kind="info",
            buttons=[("site", t("about_website")), ("close", t("close"))],
            default="close", cancel="close",
        )
        if choice == "site":
            url = updates.release_url(latest) if latest else None
            GUI._open_url(url or GUI._REPO_URL)

    @staticmethod
    def _maybe_welcome() -> None:
        """First run (no folder yet): what the app does, where originals go,
        and the opt-in update check. Shown once."""
        cfg = app_state.config
        if cfg.welcome_done or app_state.monitored_folder or SELF_TEST or START_MINIMIZED:
            return
        cfg.welcome_done = True
        dlg = GUI._studio_dialog(t_raw("welcome_title"), 540, 360)
        ctk.CTkLabel(
            dlg, text=t("welcome_title"), font=GUI.fonts["title"], text_color=C_TEXT,
            anchor=ANCHOR_START(), justify=JUSTIFY_START(),
        ).pack(fill="x", padx=22, pady=(20, 8))
        ctk.CTkLabel(
            dlg, text=GUI._wrap(t("welcome_body"), "body", 490), font=GUI.fonts["body"],
            text_color=C_TEXT_2, anchor=ANCHOR_START(), justify=JUSTIFY_START(),
            wraplength=490,
        ).pack(fill="x", padx=22)
        updates_var = tk.BooleanVar(dlg, value=False)
        # Box and text as two widgets: a CTkCheckBox keeps its box on the left
        # and never wraps its text, which ran off the dialog in Arabic.
        opt_in = ctk.CTkFrame(dlg, fg_color="transparent")
        opt_in.pack(fill="x", padx=22, pady=(14, 0))
        opt_box = ctk.CTkCheckBox(opt_in, text="", variable=updates_var, width=24)
        opt_box.pack(side=SIDE_START(), anchor="n", padx=CHECK_PAD(0))
        opt_text = ctk.CTkLabel(
            opt_in, text=GUI._wrap(t("update_opt_in"), "meta", 455), font=GUI.fonts["meta"],
            text_color=C_MUTED, anchor=ANCHOR_START(), justify=JUSTIFY_START(),
            wraplength=455, cursor="hand2",
        )
        opt_text.pack(side=SIDE_START(), fill="x", expand=True, padx=MPAD(2, 0))
        opt_text.bind("<Button-1>", lambda e: opt_box.toggle())

        footer = ctk.CTkFrame(dlg, fg_color="transparent")
        footer.pack(fill="x", padx=22, pady=(18, 16), side="bottom")

        def _close(pick: bool) -> None:
            cfg.update_check = bool(updates_var.get())
            GUI._close_dialog(dlg)
            ConfigManager.save_config(app_state.monitored_folder)
            if pick:
                GUI.select_folder()

        GUI._btn_quiet(footer, t("welcome_later"), lambda: _close(False)).pack(
            side=SIDE_END())
        GUI._btn_primary(footer, t("welcome_choose"), lambda: _close(True)).pack(
            side=SIDE_END(), padx=MPAD(0, 8))
        dlg.bind("<Escape>", lambda e: _close(False))
        dlg.protocol("WM_DELETE_WINDOW", lambda: _close(False))
        try:
            dlg.update_idletasks()
            scale = ctk.ScalingTracker.get_window_scaling(dlg) or 1.0
            dlg.geometry(f"540x{int(dlg.winfo_reqheight() / scale) + 4}")
        except (tk.TclError, ValueError):
            pass

    @staticmethod
    def _maybe_check_updates() -> None:
        """Daily, opt-in, packaged builds only (a source checkout is updated with
        git). Never blocks the UI; every failure is silent."""
        root = app_state.root
        if root is None:
            return
        root.after(6 * 3600 * 1000, GUI._maybe_check_updates)
        cfg = app_state.config
        now = time.time()
        if not is_frozen() or not updates.check_due(cfg.update_check,
                                                    cfg.last_update_check, now):
            return

        def _fetch() -> None:
            tag = updates.fetch_latest_tag()
            root.after(0, lambda: _done(tag))

        def _done(tag: Optional[str]) -> None:
            cfg.last_update_check = now
            recommended = updates.recommended_version(tag)
            notify = recommended and recommended != cfg.notified_update
            if recommended:
                cfg.notified_update = recommended
            ConfigManager.save_config(app_state.monitored_folder)
            if not notify:
                return
            body = t_raw("update_available_body", version=recommended, current=APP_VERSION)
            if app_state.tray_icon is not None:
                try:
                    app_state.tray_icon.notify(t_logical("update_available_tray",
                                                         version=recommended), APP_NAME)
                except Exception:  # noqa: BLE001
                    pass
                return
            choice = GUI.message(
                t_raw("update_available_title"), body, kind="info",
                buttons=[("open", t("update_open")), ("later", t("welcome_later"))],
                cancel="later",
            )
            if choice == "open":
                GUI._open_url(updates.release_url(recommended) or GUI._REPO_URL)

        threading.Thread(target=_fetch, name="update-check", daemon=True).start()

    @staticmethod
    def _clear_operation_flag() -> None:
        """Clear the app-level 'operation in progress' guard on the GUI thread.
        Safe to call from a worker thread (marshals via root.after)."""
        def _clear() -> None:
            app_state.operation_in_progress = False
        if app_state.root is not None:
            app_state.root.after(0, _clear)
        else:
            app_state.operation_in_progress = False

    @staticmethod
    def _surface_window() -> None:
        """Bring the main window to the foreground (restoring from the tray)."""
        root = app_state.root
        if root is None:
            return
        try:
            if app_state.tray_icon:
                try:
                    app_state.tray_icon.stop()
                except Exception:
                    pass
                app_state.tray_icon = None
            was_hidden = root.state() == "withdrawn"
            root.deiconify()
            root.attributes("-alpha", 1)
            root.state("normal")
            if was_hidden:
                # A window started in the tray was never placed: size and
                # centre it like the tray's Show item does.
                GUI._center_window(root, 780, 560)
            # Briefly topmost so it comes above the window that had focus
            # (the second launch grants the foreground, see main()).
            root.attributes("-topmost", True)
            root.after(200, lambda: root.attributes("-topmost", False))
            root.lift()
            root.focus_force()
        except Exception as e:
            logger.error(f"Could not surface window: {e}")

    @staticmethod
    def _poll_show_request() -> None:
        """Poll for a second-instance 'show' request and surface the window."""
        try:
            if SHOW_REQUEST_PATH.exists():
                try:
                    SHOW_REQUEST_PATH.unlink()
                except OSError:
                    pass
                GUI._surface_window()
        except OSError:
            pass
        # Also self-heal a window a DISPLAY CHANGE stranded off-screen while it
        # was visible (e.g. a GPU disable/enable from an HDMI-audio fix) — there's
        # no map event for that, so catch it on the poll. Cheap; only acts when
        # the window is genuinely off all monitors.
        GUI._check_on_screen()
        if app_state.root is not None:
            app_state.root.after(1000, GUI._poll_show_request)

    @staticmethod
    def _build_progress_dialog(
        title: str,
        initial_text: str = "Starting\u2026",
    ) -> Tuple[Any, Callable[[int, int, str], None], Callable[[], None]]:
        """
        Build a determinate progress dialog and return (window, update, close).

        update(current, total, filename) is safe to call from any thread.
        close() tears the dialog down on the main thread.
        """
        win = GUI._studio_dialog(title, 460, 118)
        # The window-manager X must not dismiss the modal while the worker is
        # running — that would release the grab and let a second operation
        # start. close() (fired by the worker on completion) is the only way out.
        win.protocol("WM_DELETE_WINDOW", lambda: None)

        status_lbl = ctk.CTkLabel(
            win, text=initial_text, font=GUI.fonts["body"],
            text_color=C_TEXT, anchor=ANCHOR_START(),
        )
        status_lbl.pack(fill="x", padx=16, pady=(14, 8))

        prog = ctk.CTkProgressBar(
            win, mode="determinate", height=6, corner_radius=3,
            progress_color=C_ACCENT, fg_color=C_BORDER,
        )
        prog.set(0)
        prog.pack(fill="x", padx=16)

        counts_row = ctk.CTkFrame(win, fg_color="transparent")
        counts_row.pack(fill="x", padx=16, pady=(8, 0))
        count_lbl = ctk.CTkLabel(
            counts_row, text="",
            font=GUI.fonts["body"] if is_rtl() else GUI.fonts["mono"],
            text_color=C_MUTED, anchor=ANCHOR_START(),
        )
        count_lbl.pack(side=SIDE_START())
        pct_lbl = ctk.CTkLabel(
            counts_row, text="", font=GUI.fonts["mono"],
            text_color=C_MUTED, anchor=ANCHOR_END(),
        )
        pct_lbl.pack(side=SIDE_END())

        def _update_main(current: int, total: int, filename: str) -> None:
            try:
                if total > 0:
                    prog.set(current / total)
                    pct_lbl.configure(text=f"{current * 100 // total}%")
                count_lbl.configure(
                    text=t("prog_count", current=current, total=total))
                if filename:
                    status_lbl.configure(text=_ellipsize(filename, 64))
            except Exception:
                pass

        def update(current: int, total: int, filename: str = "") -> None:
            if app_state.root is None:
                return
            app_state.root.after(0, _update_main, current, total, filename)

        def _close_main() -> None:
            try:
                win.grab_release()
                win.destroy()
            except Exception:
                pass

        def close() -> None:
            if app_state.root is None:
                return
            app_state.root.after(0, _close_main)

        return win, update, close

    @staticmethod
    def process_existing_ass() -> None:
        """Restyle existing .ass subtitles — after choosing the whole library
        or a single series subfolder."""
        if not app_state.monitored_folder:
            GUI.show_warning(t_raw("warn_title"), t_raw("select_folder_first"))
            return

        if app_state.monitoring_flag:
            GUI.show_warning(
                t_raw("warn_title"), t_raw("stop_before_apply")
            )
            return

        if app_state.operation_in_progress:
            GUI.show_info(
                t_raw("busy_title"), t_raw("busy_body")
            )
            return

        font = app_state.font_combobox.get() if app_state.font_combobox else ""
        if not (font and app_state.selected_color_code):
            GUI.show_warning(t_raw("warn_title"), t_raw("select_font_color"))
            return

        # Don't open a second chooser on a stray double-click — bring the open
        # one forward instead. (The worker itself is guarded in _run_apply, so a
        # double-click on a chooser button can't start two runs either.)
        dlg = GUI._apply_scope_dlg
        if dlg is not None:
            try:
                if dlg.winfo_exists():
                    dlg.lift()
                    return
            except tk.TclError:
                pass
        GUI._choose_apply_scope()

    @staticmethod
    def _choose_apply_scope() -> None:
        """Ask whether to restyle the whole watched library or one series
        subfolder (libraries are usually one folder per anime)."""
        library = app_state.monitored_folder
        dlg = GUI._studio_dialog(t_raw("apply_to_library"), 460, 246)
        GUI._apply_scope_dlg = dlg

        ctk.CTkLabel(
            dlg, text=GUI._wrap(t("apply_scope_prompt"), "title", 416),
            font=GUI.fonts["title"], text_color=C_TEXT, anchor=ANCHOR_START(),
            justify=JUSTIFY_START(), wraplength=416,
        ).pack(fill="x", padx=20, pady=(18, 12))

        box = ctk.CTkFrame(dlg, fg_color="transparent")
        box.pack(fill="both", expand=True, padx=20)

        def _close() -> None:
            GUI._apply_scope_dlg = None
            GUI._close_dialog(dlg)

        def _whole() -> None:
            _close()
            GUI._run_apply(library)

        def _pick() -> None:
            _close()
            chosen = filedialog.askdirectory(
                initialdir=library, title=t_raw("apply_pick_title"))
            if chosen:
                GUI._run_apply(chosen)

        GUI._btn_primary(box, t("apply_whole"), _whole).pack(fill="x")
        ctk.CTkLabel(
            box, text=library, font=GUI.fonts["meta"], text_color=C_MUTED,
            anchor=ANCHOR_START(),
        ).pack(fill="x", padx=2, pady=(5, 16))
        GUI._btn_quiet(box, t("apply_subfolder"), _pick).pack(fill="x")
        ctk.CTkLabel(
            box, text=GUI._wrap(t("apply_subfolder_hint"), "meta", 416),
            font=GUI.fonts["meta"], text_color=C_MUTED, anchor=ANCHOR_START(),
            justify=JUSTIFY_START(), wraplength=416,
        ).pack(fill="x", padx=2, pady=(5, 0))

        footer = ctk.CTkFrame(dlg, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=(0, 14))
        GUI._btn_quiet(footer, t("cancel"), _close, width=90).pack(
            side=SIDE_END())

        dlg.bind("<Escape>", lambda e: _close())
        dlg.protocol("WM_DELETE_WINDOW", _close)

    @staticmethod
    def _run_apply(folder: str) -> None:
        """Restyle every existing .ass file under `folder` (whole library or a
        chosen series subfolder) in a background worker with a progress dialog."""
        # Hard guard against a second run (e.g. a chooser button double-firing):
        # only ever one apply worker at a time. Runs on the main thread, so this
        # check-and-set can't race.
        if app_state.operation_in_progress:
            return
        app_state.operation_in_progress = True
        _win, update, close = GUI._build_progress_dialog(
            t("prog_apply"), t("prog_scanning")
        )

        # Snapshot GUI state on the main thread before spawning the worker.
        font_name = app_state.font_combobox.get() if app_state.font_combobox else ""
        color = app_state.selected_color_code or ""
        bold, italic, underline = app_state.get_style_flags()
        size = app_state.font_size
        nudge = app_state.config.font_size_nudges.get(font_name, 1.0)

        def task():
            count = 0
            total = 0
            current = 0
            started = time.time()
            try:
                try:
                    files = FileProcessor.list_ass_files(folder)
                except Exception as e:
                    logger.error(f"Failed to list .ass files: {e}")
                    files = []

                total = len(files)

                for i, fp in enumerate(files, start=1):
                    if app_state._shutting_down:
                        break
                    update(i - 1, total, fp.name)
                    # Already styled with these settings: nothing to rewrite
                    # (ux-10: Apply used to rewrite every file regardless).
                    if FileProcessor.is_already_styled(
                        fp, font_name, color,
                        bold=bold, italic=italic, underline=underline, size=size,
                        nudge=nudge,
                    ):
                        current += 1
                        continue
                    if FileProcessor.apply_changes(
                        fp, font_name, color,
                        bold=bold, italic=italic, underline=underline, size=size,
                        nudge=nudge,
                    ):
                        # apply_changes already recorded the file (mtime+hash)
                        # via add_file; no extra DB write needed here.
                        count += 1

                update(total, total, t("done"))
            finally:
                close()
                GUI._clear_operation_flag()

            if count:
                event_key = "event_files_one" if count == 1 else "event_files_many"
                app_state.note_event("Styled", rtl_base(t_logical(event_key, n=count)))

            problems = sum(1 for i in IssueTracker.get_issues() if i.timestamp >= started)
            left = max(0, total - count - current)
            summary = t_raw("apply_summary", styled=count, current=current, left=left)
            if problems:
                summary += "\n\n" + t_raw("apply_summary_problems", n=problems)
            if app_state.root:
                app_state.root.after(
                    0, lambda: GUI.show_info(t_raw("done_title"), summary))

            logger.info(f"Apply: {count} restyled, {current} already up to date, "
                        f"{left} left as they are, {problems} problem(s), of {total}")

        threading.Thread(target=task, daemon=True).start()

    @staticmethod
    def process_all_extraction() -> None:
        """Extract subtitles from all video files."""
        if not app_state.ffmpeg_available:
            GUI.show_warning(t_raw("missing_tools_title"), t_raw("ffmpeg_not_found"))
            return

        if not app_state.monitored_folder:
            GUI.show_warning(t_raw("warn_title"), t_raw("select_folder_first"))
            return

        if app_state.monitoring_flag:
            GUI.show_warning(
                t_raw("warn_title"), t_raw("stop_before_extract_all")
            )
            return

        if app_state.operation_in_progress:
            GUI.show_info(
                t_raw("busy_title"), t_raw("busy_body")
            )
            return

        if not GUI.ask_yes_no(
            t_raw("confirm_title"), t_raw("reextract_confirm"),
            danger=True,
        ):
            return

        app_state.operation_in_progress = True
        _win, update, close = GUI._build_progress_dialog(
            t("prog_extract"), t("prog_scanning")
        )

        folder = app_state.monitored_folder
        extract_langs = app_state.config.extract_langs or ""

        def task():
            count = 0
            total = 0
            monitor = FolderMonitor()  # for marking videos processed (DI instance)
            try:
                # First pass: collect videos so we can show determinate progress.
                videos: List[Path] = []
                try:
                    for item in Path(folder).rglob("*"):
                        if item.is_file() and FileProcessor.is_video_file(item.name):
                            videos.append(item)
                except Exception as e:
                    logger.error(f"Failed to enumerate videos: {e}")

                total = len(videos)

                for i, item in enumerate(videos, start=1):
                    if app_state._shutting_down:
                        break
                    update(i - 1, total, item.name)

                    abs_path = str(item.resolve())
                    streams = FileProcessor.list_subtitle_streams(abs_path)
                    if streams is None:
                        continue

                    to_extract = FileProcessor.filter_best_streams(streams, extract_langs)
                    extracted_any = False
                    out_folder = str(item.parent)
                    lang_counts = Counter(s.language.lower() for s in to_extract)

                    for s in to_extract:
                        try:
                            FileProcessor.extract_subtitle(
                                abs_path, s.index, s.language, s.codec, out_folder,
                                disambiguate=lang_counts[s.language.lower()] > 1,
                            )
                            extracted_any = True
                        except Exception as e:  # noqa: BLE001
                            record_error(
                                "extract", item.name,
                                f"stream #{s.index}: {e}",
                            )

                    if extracted_any or not streams:
                        monitor.mark_video_processed(abs_path)
                        count += 1

                update(total, total, t("done"))
            finally:
                # Always close the modal dialog — a raise (e.g. DB error while
                # marking) must not leave it grabbed and un-closeable forever.
                close()
                GUI._clear_operation_flag()

            if count:
                event_key = "event_videos_one" if count == 1 else "event_videos_many"
                app_state.note_event("Extracted", rtl_base(t_logical(event_key, n=count)))

            if app_state.root:
                app_state.root.after(
                    0,
                    lambda: GUI.show_info(
                        t_raw("done_title"),
                        t_raw("extracted_n", done=count, total=total)
                    )
                )

            logger.info(f"Extracted subtitles from {count}/{total} video files")

        threading.Thread(target=task, daemon=True).start()

    @staticmethod
    def open_log_file() -> None:
        """Open log file in default application."""
        if not LOG_PATH.exists():
            GUI.show_error(t_raw("error_title"), t_raw("log_not_found"))
            return

        try:
            if IS_WINDOWS:
                os.startfile(LOG_PATH)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(LOG_PATH)])
            else:
                subprocess.Popen(["xdg-open", str(LOG_PATH)])
        except Exception as e:
            # The plain cause when we know it, else the OS message; the path on
            # its own line as a left-to-right value (str(e) repeats it escaped).
            detail = friendly_reason(str(e)) or getattr(e, "strerror", None) or str(e)
            GUI.show_error(t_raw("error_title"), t_raw(
                "cant_open_log", err=detail, path=ltr_name(LOG_PATH)))

    @staticmethod
    def _refresh_issues_label() -> None:
        """Repaint the status-bar issues badge. Main thread only."""
        label = getattr(app_state, "issues_label", None)
        if label is None:
            return
        count = IssueTracker.count()
        try:
            if count == 0:
                label.configure(text=t("no_issues"), text_color=C_DISABLED)
            else:
                text = t("issues_one", n=count) if count == 1 \
                    else t("issues_many", n=count)
                label.configure(text=_with_mark(text, "⚠", lead=True), text_color=C_WARN)
        except tk.TclError:
            pass

    @staticmethod
    def show_issues_panel() -> None:
        """Modal dialog showing the IssueTracker ring buffer."""
        issues = IssueTracker.get_issues()

        dlg = GUI._studio_dialog(t_raw("issues_title"), 640, 460,
                                 resizable=True)

        intro = ctk.CTkLabel(
            dlg, text="", font=GUI.fonts["body"], text_color=C_MUTED,
            anchor=ANCHOR_START(), justify=JUSTIFY_START(),
        )
        intro.pack(fill="x", padx=16, pady=(12, 8))

        # A styled read-only TEXT list, NOT one CTk card per issue. Building ~6
        # CTk widgets per issue x up to 50 issues froze the GUI thread for 2-4s
        # at 250% DPI (CTk widget construction is ~6ms each — measured). A single
        # Text widget renders the whole list in a few ms, and the error text
        # becomes selectable/copyable — the right shape for a problems log.
        box = ctk.CTkTextbox(
            dlg, fg_color=C_CARD, border_width=1, border_color=C_BORDER,
            corner_radius=8, wrap="word", font=GUI.fonts["body"],
        )
        box.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        txt = box._textbox  # underlying tk.Text, for coloured tags

        # A coloured "pill" per operation (bright fg on a subtle tint), a bright
        # filename, and a muted, indented reason — colours reuse OP_PILL so it
        # still reads like the old cards.
        for op, (fg, tint) in OP_PILL.items():
            txt.tag_config(f"op_{op}", foreground=fg, background=tint)
        txt.tag_config("op_", foreground=C_MUTED, background=C_QUIET)
        txt.tag_config("name", foreground=C_TEXT)
        txt.tag_config("meta", foreground=C_MUTED)
        txt.tag_config("reason", foreground=C_TEXT_2,
                       lmargin1=16, lmargin2=16, rmargin=16, spacing1=2)
        txt.tag_config("detail", foreground=C_MUTED,
                       lmargin1=16, lmargin2=16, rmargin=16, spacing3=10)
        if is_rtl():
            # tk.Text has no right-to-left mode: right-align every line, and
            # lay each header line out in reverse order (Tk places tagged runs
            # left to right) so it reads pill, file, time from the right.
            for tag in [f"op_{op}" for op in OP_PILL] + ["op_", "name", "meta",
                                                       "reason", "detail"]:
                txt.tag_config(tag, justify="right")

        def _repaint() -> None:
            current = IssueTracker.get_issues()
            intro.configure(
                text=t("issues_intro", n=len(current)) if current
                else t("issues_none")
            )
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            for issue in reversed(current):
                tag = (f"op_{issue.operation}"
                       if issue.operation in OP_PILL else "op_")
                header = [
                    (f" {operation_label(issue.operation)} ", tag),
                    ("  " + ltr_name(_ellipsize(issue.filename, 64)) + "  ", "name"),
                    (" " + _issue_timestamp(issue.timestamp), "meta"),
                ]
                for text, run_tag in (reversed(header) if is_rtl() else header):
                    txt.insert("end", text, run_tag)
                txt.insert("end", "\n")
                friendly = friendly_reason(issue.reason)
                if friendly:
                    # Pre-broken for the dialog's opening width (tk.Text
                    # drops the RTL base on lines it wraps itself).
                    txt.insert("end", GUI._wrap(friendly, "body", 540) + "\n",
                               "reason")
                    txt.insert("end", GUI._wrap(t_raw(
                        "issue_details", detail=issue.reason), "body", 540)
                               + "\n", "detail")
                else:
                    txt.insert("end", issue.reason + "\n", "detail")
            txt.configure(state="disabled")

        _repaint()

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=(8, 14))

        def _clear() -> None:
            IssueTracker.clear()
            _repaint()

        GUI._btn_quiet(btns, t("open_log"), GUI.open_log_file, width=90).pack(
            side=SIDE_START()
        )
        GUI._btn_primary(
            btns, t("close"), lambda: GUI._close_dialog(dlg), width=80
        ).pack(side=SIDE_END())
        if issues:
            GUI._btn_quiet(btns, t("clear"), _clear, width=80).pack(
                side=SIDE_END(), padx=MPAD(0, 8)
            )

        dlg.bind("<Escape>", lambda e: GUI._close_dialog(dlg))
        dlg.protocol("WM_DELETE_WINDOW", lambda: GUI._close_dialog(dlg))

    @staticmethod
    def _show_restore_picker(
        groups: List[Tuple[Path, List[Path]]],
    ) -> List[Path]:
        """
        Modal dialog — let the user check which series folders to restore.
        Returns the list of selected parent folders (empty if canceled).
        The primary button carries the live selection count and is disabled
        at zero (replacing the old "Nothing selected" warning popup).
        """
        selected: List[Path] = []

        rows_h = 42 * min(len(groups), 8)
        dh = 150 + rows_h
        dlg = GUI._studio_dialog(t_raw("restore_title"), 520, dh)

        ctk.CTkLabel(
            dlg,
            text=GUI._wrap(t("restore_intro", n=len(groups)), "body", 488),
            font=GUI.fonts["body"], text_color=C_MUTED,
            wraplength=488, justify=JUSTIFY_START(), anchor=ANCHOR_START(),
        ).pack(fill="x", pady=(12, 10), padx=16)

        if len(groups) > 8:
            body = ctk.CTkScrollableFrame(
                dlg, fg_color="transparent", height=rows_h
            )
        else:
            body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12)

        vars_: List[Tuple[tk.BooleanVar, Path]] = []
        row_widgets: List[Tuple[ctk.CTkFrame, ctk.CTkLabel]] = []
        total_files: Dict[int, int] = {}
        f_row = GUI.fonts["body"]
        # The count noun ("files"/"ملف") lands in this font; Consolas has no
        # Arabic glyphs, so fall back to the UI face in RTL.
        f_count = GUI.fonts["body"] if is_rtl() else GUI.fonts["mono"]

        primary = [None]  # set after the footer exists

        def _repaint() -> None:
            n_sel = 0
            n_files = 0
            for i, (var, _parent) in enumerate(vars_):
                row, name_lbl = row_widgets[i]
                if var.get():
                    n_sel += 1
                    n_files += total_files[i]
                    row.configure(fg_color=C_SEL_BG, border_color=C_SEL_BORDER)
                    name_lbl.configure(text_color=C_TEXT)
                else:
                    row.configure(fg_color="transparent", border_color=C_WINDOW)
                    name_lbl.configure(text_color=C_TEXT_2)
            btn = primary[0]
            if btn is not None:
                if n_sel:
                    btn.configure(
                        text=t("restore_btn_one" if n_files == 1 else "restore_btn",
                               series=n_sel, files=n_files),
                        state="normal", fg_color=C_ACCENT,
                        text_color=C_ON_ACCENT,
                    )
                else:
                    btn.configure(
                        text=t("restore_btn_idle"), state="disabled",
                        fg_color=C_QUIET, text_color=C_DISABLED,
                    )

        for i, (parent, backups) in enumerate(groups):
            var = tk.BooleanVar(value=False)
            total_files[i] = len(backups)

            row = ctk.CTkFrame(
                body, corner_radius=8, fg_color="transparent",
                border_width=1, border_color=C_WINDOW,
            )
            row.pack(fill="x", pady=2)

            box = ctk.CTkCheckBox(
                row, text="", variable=var, command=_repaint,
                width=18, checkbox_width=18, checkbox_height=18,
                corner_radius=5, border_width=2, border_color="#5a626d",
                fg_color=C_ACCENT, hover_color=C_ACCENT_HOVER,
                checkmark_color=C_ON_ACCENT,
            )
            box.pack(side=SIDE_START(), padx=CHECK_PAD(10), pady=9)

            # Folder names read the way File Explorer shows them, even in
            # Arabic (the Arabic review's choice for every file/folder name).
            name_lbl = ctk.CTkLabel(
                row, text=ltr_name(parent.name), font=f_row, text_color=C_TEXT_2,
                anchor=ANCHOR_START(),
            )
            name_lbl.pack(side=SIDE_START(), fill="x", expand=True,
                          padx=MPAD(2, 8))

            ctk.CTkLabel(
                row, text=t("restore_files_one" if len(backups) == 1
                            else "restore_files", n=len(backups)), font=f_count,
                text_color=C_MUTED,
            ).pack(side=SIDE_END(), padx=MPAD(0, 10))

            def _toggle_row(_e: Any = None, v: tk.BooleanVar = var) -> None:
                v.set(not v.get())
                _repaint()

            for w in (row, name_lbl):
                w.bind("<Button-1>", _toggle_row)
                w.configure(cursor="hand2")

            vars_.append((var, parent))
            row_widgets.append((row, name_lbl))

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(fill="x", pady=(12, 14), padx=16)

        def on_select_all(_e: Any = None) -> None:
            all_on = all(var.get() for var, _ in vars_)
            for var, _ in vars_:
                var.set(not all_on)
            select_all_lbl.configure(
                text=t("clear_all") if not all_on else t("select_all")
            )
            _repaint()

        def on_ok() -> None:
            for var, p in vars_:
                if var.get():
                    selected.append(p)
            if not selected:
                return  # button is disabled at zero; belt and braces
            GUI._close_dialog(dlg)

        def on_cancel() -> None:
            selected.clear()
            GUI._close_dialog(dlg)

        select_all_lbl = ctk.CTkLabel(
            btns, text=t("select_all"), font=GUI.fonts["body_med"],
            text_color=C_ACCENT, cursor="hand2",
        )
        select_all_lbl.pack(side=SIDE_START())
        select_all_lbl.bind("<Button-1>", on_select_all)

        primary[0] = GUI._btn_primary(btns, t("restore_btn_idle"), on_ok)
        primary[0].pack(side=SIDE_END())
        GUI._btn_quiet(btns, t("cancel"), on_cancel, width=80).pack(
            side=SIDE_END(), padx=MPAD(0, 8)
        )
        _repaint()

        dlg.bind("<Escape>", lambda e: on_cancel())
        dlg.protocol("WM_DELETE_WINDOW", on_cancel)
        dlg.wait_window()
        return selected

    @staticmethod
    def restore_originals() -> None:
        """
        Copy backups from `Original Subs/` back over their current ASS files.
        When the monitored folder contains multiple series (each with its own
        Original Subs/ directory) the user picks which ones to restore.
        """
        if not app_state.monitored_folder:
            GUI.show_warning(t_raw("warn_title"), t_raw("select_folder_first"))
            return

        if app_state.monitoring_flag:
            GUI.show_warning(
                t_raw("warn_title"), t_raw("stop_before_restore")
            )
            return

        if app_state.operation_in_progress:
            GUI.show_info(
                t_raw("busy_title"), t_raw("busy_body")
            )
            return

        app_state.operation_in_progress = True
        base = Path(app_state.monitored_folder)

        def _continue(groups: List[Tuple[Path, List[Path]]]) -> None:
            # Back on the GUI thread after the (potentially slow) recursive scan.
            if not groups:
                GUI.show_info(
                    t_raw("no_backups_title"),
                    t_raw("no_backups_body", path=ltr_name(base))
                )
                GUI._clear_operation_flag()
                return

            if len(groups) == 1:
                parent, backups = groups[0]
                if not GUI.ask_yes_no(
                    t_raw("confirm_restore_title"),
                    t_raw("overwrite_styled_q", n=len(backups),
                          name=ltr_name(parent.name)),
                    danger=True,
                ):
                    GUI._clear_operation_flag()
                    return
                selected_parents: List[Path] = [parent]
            else:
                selected_parents = GUI._show_restore_picker(groups)
                if not selected_parents:
                    GUI._clear_operation_flag()
                    return

            _win, update, close = GUI._build_progress_dialog(
                t("prog_restore"), t("prog_scanning")
            )

            def task():
                restored = 0
                failed = 0
                try:
                    pairs: List[Tuple[Path, Path]] = []
                    for parent in selected_parents:
                        orig_dir = parent / "Original Subs"
                        if not orig_dir.is_dir():
                            continue
                        try:
                            backups = sorted({
                                b
                                for pat in BACKUP_SUBTITLE_GLOBS
                                for b in orig_dir.glob(pat)
                            })
                            for backup in backups:
                                target = parent / backup.name
                                pairs.append((backup, target))
                        except OSError as e:
                            logger.error(f"Failed to list backups in {orig_dir}: {e}")

                    total = len(pairs)

                    restored_targets, failures = FileProcessor.restore_backups(
                        pairs,
                        on_each=lambda i, t, tgt: update(i, t, tgt.name),
                        should_stop=lambda: app_state._shutting_down,
                    )

                    for target in restored_targets:
                        # Mark it RESTORED: Watch leaves it original until its
                        # content changes (a new release, an edit); Apply to
                        # library restyles it on request (ADR-006 §3).
                        try:
                            DatabaseManager.set_status(target, FileStatus.RESTORED)
                        except (OSError, sqlite3.Error) as e:
                            logger.warning(f"Could not refresh DB for {target}: {e}")

                    for target, reason in failures:
                        record_error("restore", target.name, reason)

                    restored = len(restored_targets)
                    failed = len(failures)

                    update(total, total, t("done"))
                finally:
                    # Always close the modal dialog, even if the work raises.
                    close()
                    GUI._clear_operation_flag()

                if restored:
                    event_key = ("event_files_one" if restored == 1
                                 else "event_files_many")
                    app_state.note_event("Restored", rtl_base(t_logical(event_key, n=restored)))

                if app_state.root:
                    msg = t_raw("restored_n", n=restored)
                    if failed:
                        msg += t_raw("restored_failed", n=failed)
                    app_state.root.after(0, lambda: GUI.show_info(t_raw("done_title"), msg))
                logger.info(
                    f"Restore originals across {len(selected_parents)} folder(s): "
                    f"{restored} restored, {failed} failed"
                )

            threading.Thread(target=task, daemon=True).start()

        def _scan() -> None:
            # enumerate_backup_groups recursively walks the whole tree (slow on
            # large/network libraries); keep it off the GUI thread, then resume
            # the dialog flow on the main loop.
            try:
                groups = FileProcessor.enumerate_backup_groups(base)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Restore scan failed: {e}")
                GUI._clear_operation_flag()
                return
            if app_state.root:
                app_state.root.after(0, lambda: _continue(groups))
            else:
                # No UI to resume into — release the guard so it isn't stuck.
                GUI._clear_operation_flag()

        threading.Thread(target=_scan, daemon=True).start()

    @staticmethod
    def _apply_blank_config() -> None:
        """Clear the UI to mirror a freshly-deleted config.json."""
        app_state.config = AppConfig()
        app_state.selected_color_code = None
        app_state.monitored_folder = None
        app_state.preview_text = DEFAULT_PREVIEW_TEXT

        if app_state.font_combobox is not None:
            try:
                app_state.font_combobox.delete(0, "end")
            except tk.TclError:
                pass
        if app_state.font_bold is not None:
            app_state.font_bold.set(False)
        if app_state.font_italic is not None:
            app_state.font_italic.set(False)
        if app_state.font_underline is not None:
            app_state.font_underline.set(False)
        if app_state.extract_enabled is not None:
            app_state.extract_enabled.set(False)
        if app_state.extract_langs is not None:
            app_state.extract_langs.set("")
        if app_state.color_indicator is not None:
            try:
                app_state.color_indicator.configure(
                    fg_color=C_DISABLED, hover_color=C_DISABLED
                )
            except tk.TclError:
                pass
        GUI._set_folder_display(None)
        GUI._refresh_style_cells()
        GUI.update_extraction_ui_state()
        GUI.update_preview()

    @staticmethod
    def reset_app() -> None:
        """Show reset options dialog."""
        if app_state.monitoring_flag:
            GUI.show_warning(t_raw("warn_title"), t_raw("stop_before_reset"))
            return
        
        dlg = GUI._studio_dialog(t_raw("reset_title"), 440, 320)

        header = ctk.CTkLabel(
            dlg, text=GUI._wrap(t("reset_intro"), "body", 408),
            font=GUI.fonts["body"], text_color=C_MUTED,
            wraplength=408, justify=JUSTIFY_START(), anchor=ANCHOR_START(),
        )
        header.pack(fill="x", pady=(12, 10), padx=16)

        reset_config_var = tk.BooleanVar(dlg, value=False)
        reset_subs_db_var = tk.BooleanVar(dlg, value=False)
        reset_videos_db_var = tk.BooleanVar(dlg, value=False)

        body = ctk.CTkFrame(dlg, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12)

        rows: List[Tuple[ctk.CTkFrame, ctk.CTkLabel, tk.BooleanVar, bool]] = []
        reset_btn = [None]

        def _repaint() -> None:
            any_on = False
            for row, desc_lbl, var, danger in rows:
                on = var.get()
                any_on = any_on or on
                if on and danger:
                    row.configure(
                        fg_color=C_DANGER_ROW_BG,
                        border_color=C_DANGER_ROW_BORDER,
                    )
                    desc_lbl.configure(text_color=C_DANGER)
                elif on:
                    row.configure(fg_color=C_SEL_BG, border_color=C_SEL_BORDER)
                    desc_lbl.configure(text_color=C_MUTED)
                else:
                    row.configure(fg_color="transparent", border_color=C_WINDOW)
                    desc_lbl.configure(
                        text_color=C_DANGER if danger else C_MUTED
                    )
            btn = reset_btn[0]
            if btn is not None:
                if any_on:
                    btn.configure(
                        state="normal", fg_color=C_DANGER,
                        text_color=C_ON_DANGER,
                    )
                else:
                    btn.configure(
                        state="disabled", fg_color=C_QUIET,
                        text_color=C_DISABLED,
                    )

        def _add_row(title: str, desc: str, var: tk.BooleanVar,
                     danger: bool = False) -> None:
            row = ctk.CTkFrame(
                body, corner_radius=8, fg_color="transparent",
                border_width=1, border_color=C_WINDOW,
            )
            row.pack(fill="x", pady=2)
            box = ctk.CTkCheckBox(
                row, text="", variable=var, command=_repaint,
                width=18, checkbox_width=18, checkbox_height=18,
                corner_radius=5, border_width=2, border_color="#5a626d",
                fg_color=C_DANGER if danger else C_ACCENT,
                hover_color=C_DANGER_HOVER if danger else C_ACCENT_HOVER,
                checkmark_color=C_ON_DANGER if danger else C_ON_ACCENT,
            )
            box.pack(side=SIDE_START(), padx=CHECK_PAD(10), pady=9, anchor="n")
            col = ctk.CTkFrame(row, fg_color="transparent")
            col.pack(side=SIDE_START(), fill="x", expand=True,
                     padx=MPAD(2, 10), pady=8)
            title_lbl = ctk.CTkLabel(
                col, text=title, font=GUI.fonts["body"],
                text_color=C_TEXT, anchor=ANCHOR_START(),
            )
            title_lbl.pack(fill="x")
            desc_lbl = ctk.CTkLabel(
                col, text=GUI._wrap(desc, "meta", 340), font=GUI.fonts["meta"],
                text_color=C_DANGER if danger else C_MUTED,
                anchor=ANCHOR_START(), justify=JUSTIFY_START(), wraplength=340,
            )
            desc_lbl.pack(fill="x")

            def _toggle(_e: Any = None, v: tk.BooleanVar = var) -> None:
                v.set(not v.get())
                _repaint()

            for w in (row, col, title_lbl, desc_lbl):
                w.bind("<Button-1>", _toggle)
            row.configure(cursor="hand2")
            rows.append((row, desc_lbl, var, danger))

        _add_row(t("reset_config"), t("reset_config_desc"), reset_config_var)
        _add_row(t("reset_subs"), t("reset_subs_desc"), reset_subs_db_var)
        _add_row(t("reset_video"), t("reset_video_desc"),
                 reset_videos_db_var, danger=True)

        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_frame.pack(fill="x", pady=(4, 14), padx=16)

        def perform_reset():
            # Confirm with an explicit recap of exactly what will be removed —
            # this is the single most expensive accidental click in the app
            # (clearing the video index forces a full re-extraction of the
            # library) and it sits one tap away in the tray menu too.
            chosen: List[str] = []
            if reset_config_var.get():
                chosen.append(t_raw("reset_item_config"))
            if reset_subs_db_var.get():
                chosen.append(t_raw("reset_item_subs"))
            if reset_videos_db_var.get():
                chosen.append(t_raw("reset_item_videos"))
            if not chosen:
                return  # button is disabled at zero selection
            if not GUI.ask_yes_no(
                t_raw("confirm_reset_title"),
                t_raw("reset_recap_head") + "\n".join(chosen)
                + t_raw("reset_recap_tail"),
                parent=dlg,
                danger=True,
            ):
                return

            errors: List[str] = []
            config_reset = False

            if reset_config_var.get():
                try:
                    if CONFIG_FILE_PATH.exists():
                        CONFIG_FILE_PATH.unlink()
                    config_reset = True
                    logger.info("Configuration file deleted")
                except OSError as e:
                    errors.append(t_raw("reset_err_config", err=str(e)))

            if reset_subs_db_var.get():
                try:
                    DatabaseManager.clear_monitored_files()
                except Exception as e:
                    errors.append(t_raw("reset_err_subs", err=str(e)))

            if reset_videos_db_var.get():
                try:
                    DatabaseManager.clear_processed_videos()
                    # Clear the in-memory set on SUCCESS so it matches the now
                    # empty table (this was previously in the except branch —
                    # the wrong place, leaving stale state after a clear). Hold
                    # the lock so the rebind can't race a monitor/worker .add().
                    with app_state.processed_lock:
                        app_state.processed_videos = set()
                except Exception as e:
                    errors.append(t_raw("reset_err_videos", err=str(e)))

            # Reflect the reset in the live UI so the user sees it immediately
            # instead of stale values that would re-create config.json on next save.
            if config_reset:
                GUI._apply_blank_config()

            GUI._close_dialog(dlg)

            if errors:
                GUI.show_error(t_raw("reset_errors_title"),
                                     "\n".join(errors))
            else:
                GUI.show_info(t_raw("reset_title"), t_raw("reset_done"))

            logger.info("Reset operation completed")

        reset_btn[0] = GUI._btn_danger(btn_frame, t("reset_selected_btn"),
                                       perform_reset)
        reset_btn[0].pack(side=SIDE_END())
        GUI._btn_quiet(
            btn_frame, t("cancel"), lambda: GUI._close_dialog(dlg), width=80
        ).pack(side=SIDE_END(), padx=MPAD(0, 8))
        _repaint()

        dlg.bind("<Escape>", lambda e: GUI._close_dialog(dlg))
        dlg.protocol("WM_DELETE_WINDOW", lambda: GUI._close_dialog(dlg))
        # Fit the height to the rows: the Arabic text is larger and wraps onto
        # more lines, which pushed the buttons out of the fixed 320 px.
        try:
            dlg.update_idletasks()
            scale = ctk.ScalingTracker.get_window_scaling(dlg) or 1.0
            dlg.geometry(f"440x{max(320, int(dlg.winfo_reqheight() / scale) + 4)}")
        except (tk.TclError, ValueError):
            pass

    @staticmethod
    def _selected_lang_codes() -> List[str]:
        """Selected extraction language codes, in COMMON_LANGS order."""
        raw = app_state.extract_langs.get() if app_state.extract_langs else ""
        saved = {c.strip().lower() for c in raw.split(",") if c.strip()}
        return [c for c, _ in COMMON_LANGS if c in saved]

    @staticmethod
    def update_extraction_ui_state() -> None:
        """Repaint the Extract-subs button + langs chip for ffmpeg
        availability and the extract-enabled toggle state."""
        widgets = app_state.extraction_widgets
        if not widgets:
            return

        codes = GUI._selected_lang_codes()
        chip_text = t("no_langs_selected") if not codes else (
            codes[0] if len(codes) == 1 else f"{codes[0]} +{len(codes) - 1}"
        )

        enabled = bool(
            app_state.extract_enabled and app_state.extract_enabled.get()
        )
        try:
            # Language codes read best in the mono face; the "none" word does
            # not (Consolas has no Arabic: the words came out reversed).
            widgets["chip"].configure(
                font=GUI.fonts["chip_mono"] if codes else GUI.fonts["pill"])
            if not app_state.ffmpeg_available:
                widgets["frame"].configure(fg_color=C_QUIET)
                widgets["label"].configure(text_color=C_DISABLED)
                widgets["chip"].configure(
                    text=chip_text, text_color=C_DISABLED, fg_color="#333840"
                )
                widgets["caret"].configure(text_color=C_DISABLED)
                widgets["divider"].configure(fg_color="#333840")
            elif enabled:
                widgets["frame"].configure(fg_color=C_TINT_BG)
                widgets["label"].configure(text_color=C_TINT_TEXT)
                widgets["chip"].configure(
                    text=chip_text, text_color=C_TINT_TEXT, fg_color="#31435c"
                )
                widgets["caret"].configure(text_color=C_TINT_TEXT)
                widgets["divider"].configure(fg_color=C_TINT_HOVER)
            else:
                widgets["frame"].configure(fg_color=C_QUIET)
                widgets["label"].configure(text_color=C_TEXT_2)
                widgets["chip"].configure(
                    text=chip_text, text_color=C_MUTED, fg_color="#3f444a"
                )
                widgets["caret"].configure(text_color=C_MUTED)
                widgets["divider"].configure(fg_color="#3f444a")
        except (tk.TclError, KeyError) as e:
            logger.error(f"Failed to repaint extraction UI: {e}")

        refresh = widgets.get("dialog_refresh")
        if refresh is not None:
            try:
                refresh()
            except tk.TclError:
                pass

    @staticmethod
    def _on_fonts_loaded(fonts: List[str]) -> None:
        """Callback when fonts are loaded. FontLoader.get_fonts now resolves
        synchronously and invokes this on the GUI thread (create_gui), so no
        cross-thread marshaling is needed."""
        app_state.all_fonts = fonts
        logger.info(f"Loaded {len(fonts)} fonts")

    @staticmethod
    def create_gui() -> ctk.CTk:
        """Create and return the main GUI window (Studio redesign)."""
        ensure_default_icon()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        root = ctk.CTk(fg_color=C_WINDOW)
        root.overrideredirect(True)
        root.attributes("-alpha", 0)
        root.title(f"{APP_NAME} [TEST {INSTANCE_TOKEN}]" if INSTANCE_TOKEN else APP_NAME)
        root.minsize(760, 540)
        app_state.root = root
        app_state.initialize_variables(root)

        # Load configuration. Capture whether this is a genuine first run (no
        # config file yet) BEFORE loading — used to seed a default font without
        # leaking it into a returning user's intentionally-blank-font profile.
        first_launch = not CONFIG_FILE_PATH.exists()
        config = ConfigManager.load_config()

        # Folder + extraction settings are independent of the styling font: an
        # extraction-only user has no font but still needs these restored, and
        # monitoring auto-start treats `extract` independently of `font`. Gating
        # them on config.font wiped them on every relaunch.
        app_state.monitored_folder = config.folder
        if app_state.extract_enabled:
            app_state.extract_enabled.set(config.extract_enabled)
        if app_state.extract_langs:
            app_state.extract_langs.set(config.extract_langs)

        # Restore color + style flags UNCONDITIONALLY (not gated on config.font).
        # save_config persists them independently of the font, so gating their
        # restore on config.font silently dropped a saved color/bold/etc whenever
        # the font entry was empty — the same bug already fixed for folder/extract
        # above.
        app_state.selected_color_code = config.color
        if app_state.font_bold:
            app_state.font_bold.set(config.font_bold)
        if app_state.font_italic:
            app_state.font_italic.set(config.font_italic)
        if app_state.font_underline:
            app_state.font_underline.set(config.font_underline)
        app_state.font_size = config.font_size
        app_state.preview_text = (
            (config.preview_text or DEFAULT_PREVIEW_TEXT).strip()
            or DEFAULT_PREVIEW_TEXT
        )

        # UI language governs every t() call and the layout mirroring below.
        # First run (no stored choice): the installer's language, then the
        # Windows display language. Stored so later runs don't re-decide.
        config.ui_language = resolve_ui_language(
            config.ui_language, installer_language(), windows_ui_language_id())
        set_language(config.ui_language)
        # After the language: the Arabic UI gets slightly larger text fonts.
        GUI._build_fonts()

        # Load the system font list (synchronous on the Tk thread).
        FontLoader.get_fonts(GUI._on_fonts_loaded)

        # ══ Header row: WATCH FOLDER + Change… | status + watch switch ══
        header = ctk.CTkFrame(root, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(12, 10))

        head_left = ctk.CTkFrame(header, fg_color="transparent")
        head_left.pack(side=SIDE_START(), fill="x", expand=True)
        ctk.CTkLabel(
            head_left, text=t("watch_folder"), font=GUI.fonts["section"],
            text_color=C_MUTED, anchor=ANCHOR_START(), height=14,
        ).pack(fill="x")
        folder_row = ctk.CTkFrame(head_left, fg_color="transparent")
        folder_row.pack(fill="x", pady=(2, 0))
        app_state.folder_label = ctk.CTkLabel(
            folder_row, text="", font=GUI.fonts["value"],
            text_color=C_TEXT, anchor=ANCHOR_START(), height=20,
        )
        app_state.folder_label.pack(side=SIDE_START())
        change_link = ctk.CTkLabel(
            folder_row, text=t("change"), font=GUI.fonts["meta"],
            text_color=C_ACCENT, cursor="hand2", height=20,
        )
        change_link.pack(side=SIDE_START(), padx=(10, 0) if not is_rtl()
                         else (0, 10))
        change_link.bind("<Button-1>", lambda e: GUI.select_folder())

        head_right = ctk.CTkFrame(header, fg_color="transparent")
        head_right.pack(side=SIDE_END())
        app_state.status_label = ctk.CTkLabel(
            head_right, text=t("not_watching"), font=GUI.fonts["body_med"],
            text_color=C_MUTED,
        )
        app_state.status_label.pack(side=SIDE_START(), padx=(0, 10))
        GUI._watch_switch = ctk.CTkSwitch(
            head_right, text="", width=48, switch_width=46, switch_height=26,
            corner_radius=999, progress_color=C_OK, fg_color=C_BORDER,
            button_color="#ffffff", button_hover_color="#e7eaef",
            command=GUI.toggle_monitoring,
        )
        GUI._watch_switch.pack(side=SIDE_START())

        # ══ Preview well — the hero (PIL-rendered image; click = edit text) ══
        # The well shows a CTkImage sized to fill it, and a CTkLabel's requested
        # size equals its image size. Packed directly under `root`, that request
        # would steal pack space from the control rows below: after a maximize→
        # restore the still-maximized image kept the well window-tall and pushed
        # every row off the bottom ("misses everything"). Wrap it in a holder
        # with pack_propagate(False) (height=0 so it reserves nothing and only
        # takes its expand share) — the image can no longer feed back into the
        # layout, so the rows always keep their space.
        well_holder = ctk.CTkFrame(root, fg_color="transparent", height=0)
        well_holder.pack(fill="both", expand=True, padx=16, pady=(2, 0))
        well_holder.pack_propagate(False)
        app_state.preview_entry = ctk.CTkLabel(
            well_holder, text="", corner_radius=10, fg_color=C_WELL,
            cursor="hand2",
        )
        app_state.preview_entry.pack(fill="both", expand=True)
        app_state.preview_entry.bind(
            "<Button-1>", lambda e: _edit_preview_text()
        )
        app_state.preview_entry.bind("<Configure>", GUI._on_preview_resized)

        # Background-source controls, in a thin row UNDER the well (not over the
        # image — a rounded button placed over a picture shows its dark corners).
        # Default: black placeholder; the switch reveals a real library frame.
        bg_row = ctk.CTkFrame(root, fg_color="transparent")
        bg_row.pack(fill="x", padx=18, pady=(6, 0))
        GUI._shuffle_btn = GUI._btn_quiet(
            bg_row, GUI._shuffle_label(), GUI.shuffle_preview_background,
            width=110,
        )
        GUI._shuffle_btn.configure(state="disabled")  # enabled with the toggle
        GUI._shuffle_btn.pack(side=SIDE_END())
        # Source picker: user's preview text vs a real subtitle line + its frame.
        _mytext_label, GUI._bg_sub_label = t("bg_mode_mytext"), t("bg_mode_subtitle")
        _modes = [_mytext_label, GUI._bg_sub_label]
        # Segments run left to right in list order: reverse them in RTL. The
        # light text was barely readable on the bright accent (about 2:1), so
        # the selected segment uses the darker accent and brighter text.
        GUI._bg_mode_seg = ctk.CTkSegmentedButton(
            bg_row, values=_modes[::-1] if is_rtl() else _modes, height=24,
            font=GUI.fonts["meta"], corner_radius=8, command=GUI._on_bg_mode,
            selected_color=C_ACCENT_HOVER, selected_hover_color=C_ACCENT_HOVER,
            unselected_color=C_QUIET, unselected_hover_color=C_QUIET_HOVER,
            fg_color=C_CARD, text_color=C_TEXT, text_color_disabled=C_MUTED,
        )
        GUI._bg_mode_seg.pack(side=SIDE_END(), padx=MPAD(0, 10))
        GUI._bg_switch = ctk.CTkSwitch(
            bg_row, text=t("real_bg"), font=GUI.fonts["meta"],
            text_color=C_MUTED, command=GUI.toggle_preview_bg,
            switch_width=38, switch_height=20, corner_radius=999,
            progress_color=C_ACCENT, fg_color=C_BORDER,
        )
        GUI._bg_switch.pack(side=SIDE_END(), padx=MPAD(0, 12))
        # "Preview text…" lives here (the START side is free), beside the "My
        # text" source it edits — the style row has no room for it at high DPI.
        # (Clicking the preview text in the well opens the same editor.)
        ctk.CTkButton(
            bg_row, text=t("preview_text_btn"), height=28, corner_radius=8,
            fg_color="transparent", hover_color=C_QUIET,
            border_width=1, border_color=C_BORDER, text_color=C_MUTED,
            font=GUI.fonts["body"],
            command=lambda: _edit_preview_text(),
        ).pack(side=SIDE_START())
        # Restore persisted state: toggle, source mode, and the exact last frame
        # (+ its line) from disk — no ffmpeg needed. If nothing was saved, a
        # fresh frame loads once ffmpeg is ready (locate_ffmpeg_background).
        # select()/set() don't fire the widgets' commands.
        GUI._preview_bg_on = bool(app_state.config.preview_bg_enabled)
        GUI._preview_from_sub = bool(app_state.config.preview_bg_from_sub)
        GUI._bg_mode_seg.set(
            GUI._bg_sub_label if GUI._preview_from_sub else _mytext_label)
        if GUI._preview_bg_on:
            GUI._bg_switch.select()
            GUI._load_bg_for_current_mode()  # the current mode's saved frame
        GUI._refresh_bg_controls()

        # ══ Style row: Font field · Color field · B/I/U · Preview text… ══
        style_row = ctk.CTkFrame(root, fg_color="transparent")
        style_row.pack(fill="x", padx=16, pady=(12, 4))

        font_field = ctk.CTkFrame(
            style_row, fg_color=C_CARD, border_width=1,
            border_color=C_BORDER, corner_radius=8,
        )
        font_field.pack(side=SIDE_START(), fill="x", expand=True)
        font_hint = ctk.CTkLabel(
            font_field, text=t("font"), height=20, font=GUI.fonts["body"],
            text_color=C_MUTED, cursor="hand2",
        )
        font_hint.pack(side=SIDE_START(), padx=MPAD(10, 8), pady=7)
        app_state.font_combobox = ctk.CTkEntry(
            font_field, height=26, fg_color="transparent", border_width=0,
            font=GUI.fonts["body"], text_color=C_TEXT,
            justify=JUSTIFY_START(),
        )
        # Genuine first run → default to Segoe UI, which covers both Arabic and
        # Latin, so the bilingual preview renders honestly out of the box. Only
        # seed on first run: a returning user who left the font blank keeps it
        # blank (so the auto-start / apply gates still treat it as "no font").
        app_state.font_combobox.insert(
            0, config.font or ("Segoe UI" if first_launch else ""))
        app_state.font_combobox.pack(
            side=SIDE_START(), fill="x", expand=True, pady=4
        )
        app_state.font_combobox.bind("<KeyRelease>", GUI.update_font_list)

        # Scroll through installed fonts with arrow keys / mouse wheel so
        # the preview updates live while browsing — requires focus in the
        # font entry.
        app_state.font_combobox.bind("<Up>", lambda e: GUI._cycle_font(-1))
        app_state.font_combobox.bind("<Down>", lambda e: GUI._cycle_font(1))
        app_state.font_combobox.bind("<MouseWheel>", GUI._on_font_wheel)
        # Linux-style scroll events (ignored on Windows).
        app_state.font_combobox.bind("<Button-4>", lambda e: GUI._cycle_font(-1))
        app_state.font_combobox.bind("<Button-5>", lambda e: GUI._cycle_font(1))

        # The spec's 9px caret is invisible once Tk renders it — use a
        # readable size with a hover brighten so it reads as clickable.
        caret = ctk.CTkLabel(
            font_field, text="▾", height=20, width=18,
            font=ctk.CTkFont(size=14), text_color=C_TEXT_2, cursor="hand2",
        )
        caret.pack(side=SIDE_END(), padx=MPAD(4, 10), pady=7)
        font_hint.bind("<Button-1>", lambda e: _open_font_picker())
        caret.bind("<Button-1>", lambda e: _open_font_picker())
        caret.bind("<Enter>", lambda e: caret.configure(text_color=C_TEXT))
        caret.bind("<Leave>", lambda e: caret.configure(text_color=C_TEXT_2))

        # Star toggle: limit the arrow/wheel font-cycling to favorites, so the
        # user can browse just their (script-appropriate) picks and skip past
        # fonts that don't fit. Sits just inside the caret.
        GUI._fav_cycle_btn = ctk.CTkLabel(
            font_field, text="☆", height=20, width=18,
            font=ctk.CTkFont(size=15), text_color=C_TEXT_2, cursor="hand2",
        )
        GUI._fav_cycle_btn.pack(side=SIDE_END(), padx=MPAD(4, 2), pady=7)
        GUI._fav_cycle_btn.bind("<Button-1>", GUI._toggle_cycle_favorites)
        GUI._refresh_fav_cycle_btn()

        color_field = ctk.CTkFrame(
            style_row, fg_color=C_CARD, border_width=1,
            border_color=C_BORDER, corner_radius=8, cursor="hand2",
        )
        color_field.pack(side=SIDE_START(), padx=MPAD(8, 0))
        color_hint = ctk.CTkLabel(
            color_field, text=t("color"), height=20, font=GUI.fonts["body"],
            text_color=C_MUTED, cursor="hand2",
        )
        color_hint.pack(side=SIDE_START(), padx=MPAD(10, 8), pady=7)
        initial_swatch = (
            bgr_to_rgb_hex(app_state.selected_color_code)
            if app_state.selected_color_code else C_DISABLED
        )
        app_state.color_indicator = ctk.CTkButton(
            color_field, text="", width=18, height=18, corner_radius=5,
            fg_color=initial_swatch, hover_color=initial_swatch,
            border_width=1, border_color="#15171b",
            command=GUI.choose_color,
        )
        app_state.color_indicator.pack(side=SIDE_START(), padx=MPAD(0, 10),
                                       pady=8)
        color_field.bind("<Button-1>", lambda e: GUI.choose_color())
        color_hint.bind("<Button-1>", lambda e: GUI.choose_color())

        # B / I / U segmented control (3 toggle cells in a bordered frame —
        # CTkSegmentedButton can't multi-select). The B/I/U glyphs are the
        # same in both languages; keep their internal L→R order.
        biu = ctk.CTkFrame(
            style_row, fg_color=C_CARD, border_width=1,
            border_color=C_BORDER, corner_radius=8,
        )
        biu.pack(side=SIDE_START(), padx=MPAD(8, 0))

        def _toggle_style(key: str) -> None:
            var = {
                "b": app_state.font_bold,
                "i": app_state.font_italic,
                "u": app_state.font_underline,
            }[key]
            if var is not None:
                var.set(not var.get())
            GUI._refresh_style_cells()
            GUI.update_preview_and_save()

        GUI._style_cells = {}
        for key, glyph, cell_font, padx in (
            ("b", "B", GUI.fonts["seg_b"], (3, 0)),
            ("i", "I", GUI.fonts["seg_i"], (2, 0)),
            ("u", "U", GUI.fonts["seg_u"], (2, 3)),
        ):
            cell = ctk.CTkButton(
                biu, text=glyph, width=32, height=28, corner_radius=6,
                fg_color=C_CARD, hover_color=C_QUIET_HOVER,
                text_color=C_MUTED, font=cell_font,
                command=lambda k=key: _toggle_style(k),
            )
            cell.pack(side="left", padx=padx, pady=3)
            GUI._style_cells[key] = cell

        # Dialogue-size preset (— / S / M / L), single-select. Resizes ONLY
        # dialogue styles (never signs/typesetting), scaled to each file's own
        # resolution so the on-screen size is consistent library-wide. "—"
        # (none) leaves every file's Fontsize untouched. Keep the internal
        # ascending L→R order in both languages, like the B/I/U cells.
        size_frame = ctk.CTkFrame(
            style_row, fg_color=C_CARD, border_width=1,
            border_color=C_BORDER, corner_radius=8,
        )
        size_frame.pack(side=SIDE_START(), padx=MPAD(8, 0))
        ctk.CTkLabel(
            size_frame, text=t("size"), height=20, font=GUI.fonts["body"],
            text_color=C_MUTED,
        ).pack(side=SIDE_START(), padx=MPAD(10, 4), pady=7)

        GUI._size_cells = {}
        for skey, glyph, spad in (
            ("none", "—", (0, 0)),
            ("small", "S", (2, 0)),
            ("medium", "M", (2, 0)),
            ("large", "L", (2, 4)),
        ):
            scell = ctk.CTkButton(
                size_frame, text=glyph, width=28, height=28, corner_radius=6,
                fg_color=C_CARD, hover_color=C_QUIET_HOVER,
                text_color=C_MUTED, font=GUI.fonts["body_med"],
                command=lambda k=skey: GUI._select_size(k),
            )
            scell.pack(side="left", padx=spad, pady=3)
            GUI._size_cells[skey] = scell
        GUI._refresh_size_cells()

        # A thin divider, then the per-font NUDGE — merged into the Size group
        # (both are "size"): the S/M/L preset is global; the ±% stepper fine-tunes
        # the CURRENT font on top of it. Kept in physical −…+ order to match the
        # S/M/L order. Greyed out when the preset is "none" (nothing to scale).
        ctk.CTkFrame(size_frame, width=1, height=18, fg_color=C_BORDER).pack(
            side="left", padx=7, pady=5)
        GUI._nudge_minus = ctk.CTkButton(
            size_frame, text="−", width=26, height=28, corner_radius=6,
            fg_color=C_CARD, hover_color=C_QUIET_HOVER, text_color=C_TEXT,
            font=GUI.fonts["body_med"],
            command=lambda: GUI._bump_nudge(-SIZE_NUDGE_STEP),
        )
        GUI._nudge_minus.pack(side="left", pady=3)
        GUI._nudge_readout = ctk.CTkLabel(
            size_frame, text="0%", width=38, height=20,
            font=GUI.fonts["body_med"], text_color=C_TEXT,
        )
        GUI._nudge_readout.pack(side="left", pady=3)
        GUI._nudge_plus = ctk.CTkButton(
            size_frame, text="+", width=26, height=28, corner_radius=6,
            fg_color=C_CARD, hover_color=C_QUIET_HOVER, text_color=C_TEXT,
            font=GUI.fonts["body_med"],
            command=lambda: GUI._bump_nudge(SIZE_NUDGE_STEP),
        )
        GUI._nudge_plus.pack(side="left", padx=(0, 6), pady=3)
        GUI._refresh_nudge_control()

        # ══ Actions row: Apply · Extract subs [chip] · Restore · spacer · ⋯ ══
        actions = ctk.CTkFrame(root, fg_color="transparent")
        actions.pack(fill="x", padx=16, pady=(8, 12))

        GUI._btn_tinted(
            actions, t("apply_to_library"), GUI.process_existing_ass, height=30
        ).pack(side=SIDE_START(), padx=MPAD(0, 6))

        # Split button: one half toggles monitor-time extraction, the other
        # (divider · chip · caret) opens the languages dialog — the divider +
        # caret make the second click target visible.
        ext_frame = ctk.CTkFrame(
            actions, fg_color=C_QUIET, corner_radius=8, cursor="hand2",
        )
        ext_frame.pack(side=SIDE_START(), padx=MPAD(0, 6))
        ext_lbl = ctk.CTkLabel(
            ext_frame, text=t("extract_subs"), height=18,
            font=GUI.fonts["body_med"], text_color=C_TEXT_2, cursor="hand2",
        )
        ext_lbl.pack(side=SIDE_START(), padx=MPAD(12, 10), pady=6)
        ext_divider = ctk.CTkFrame(
            ext_frame, width=1, height=16, fg_color="#3f444a",
        )
        ext_divider.pack(side=SIDE_START(), pady=7)
        ext_chip = ctk.CTkLabel(
            ext_frame, text="", height=18, font=GUI.fonts["chip_mono"],
            text_color=C_MUTED, fg_color="#3f444a", corner_radius=4,
            cursor="hand2",
        )
        ext_chip.pack(side=SIDE_START(), padx=MPAD(8, 2), pady=6)
        ext_caret = ctk.CTkLabel(
            ext_frame, text="▾", height=18, width=14,
            font=ctk.CTkFont(size=12), text_color=C_MUTED, cursor="hand2",
        )
        ext_caret.pack(side=SIDE_START(), padx=MPAD(0, 8), pady=6)

        def _toggle_extract(_event: Any = None) -> None:
            # The old "Extract Subs" checkbox folds into this button: clicking
            # it toggles monitor-time extraction; the chip opens the languages
            # dialog.
            if not app_state.ffmpeg_available:
                GUI.show_warning(t_raw("missing_tools_title"),
                                       t_raw("ffmpeg_not_found"))
                return
            if app_state.extract_enabled is not None:
                app_state.extract_enabled.set(not app_state.extract_enabled.get())
            ConfigManager.save_config(app_state.monitored_folder)
            GUI.update_extraction_ui_state()

        ext_frame.bind("<Button-1>", _toggle_extract)
        ext_lbl.bind("<Button-1>", _toggle_extract)
        for w in (ext_chip, ext_caret, ext_divider):
            w.bind("<Button-1>", lambda e: _open_langs_dialog())
        for w in (ext_chip, ext_caret):
            w.bind("<Enter>", lambda e: ext_caret.configure(text_color=C_TEXT))
            w.bind(
                "<Leave>", lambda e: GUI.update_extraction_ui_state()
            )

        app_state.extraction_widgets = {
            "frame": ext_frame,
            "label": ext_lbl,
            "chip": ext_chip,
            "caret": ext_caret,
            "divider": ext_divider,
        }

        GUI._btn_quiet(
            actions, t("restore_originals"), GUI.restore_originals,
            height=30,
        ).pack(side=SIDE_START())

        more_btn = GUI._btn_quiet(
            actions, "⋯", lambda: _post_overflow(), width=34, height=30
        )
        more_btn.pack(side=SIDE_END())

        # ══ Footer status bar ══
        ctk.CTkFrame(root, fg_color=C_BORDER, height=1).pack(fill="x")
        footer = ctk.CTkFrame(root, fg_color="transparent", height=34)
        footer.pack(fill="x")
        footer.pack_propagate(False)

        app_state.led_indicator = ctk.CTkLabel(
            footer, text="●", font=ctk.CTkFont(size=10),
            text_color=C_DISABLED, width=10,
        )
        app_state.led_indicator.pack(side=SIDE_START(), padx=MPAD(16, 6))
        GUI._footer_prefix = ctk.CTkLabel(
            footer, text=t("idle"), font=GUI.fonts["meta"], text_color=C_MUTED
        )
        GUI._footer_prefix.pack(side=SIDE_START())
        GUI._footer_name = ctk.CTkLabel(
            footer, text="", font=GUI.fonts["meta"], text_color=C_TEXT
        )
        # Small pad so the filename never visually touches the prefix/suffix
        # (RTL collapses the string-level spaces to the wrong side).
        GUI._footer_name.pack(side=SIDE_START(), padx=3)
        GUI._footer_suffix = ctk.CTkLabel(
            footer, text="", font=GUI.fonts["meta"], text_color=C_MUTED
        )
        GUI._footer_suffix.pack(side=SIDE_START())

        app_state.issues_label = ctk.CTkLabel(
            footer, text=t("no_issues"), font=GUI.fonts["meta"],
            text_color=C_DISABLED, cursor="hand2",
        )
        app_state.issues_label.pack(side=SIDE_END(), padx=MPAD(0, 16))
        app_state.issues_label.bind(
            "<Button-1>", lambda e: GUI.show_issues_panel()
        )

        # ── ⋯ overflow menu ──
        # A custom dark popup instead of tk.Menu: Windows draws tk popup menus
        # with an unstylable light border, and tk_popup at the button's left
        # edge spills past the window. Plain tk.Toplevel on purpose — a
        # CTkToplevel would run the titlebar-color withdraw dance on it.
        _overflow_pop: List[Optional[tk.Toplevel]] = [None]

        def _dismiss_overflow() -> None:
            pop = _overflow_pop[0]
            _overflow_pop[0] = None
            if pop is not None:
                try:
                    pop.grab_release()
                    pop.destroy()
                except tk.TclError:
                    pass

        def _post_overflow() -> None:
            if _overflow_pop[0] is not None:  # second ⋯ click closes it
                _dismiss_overflow()
                return

            pop = tk.Toplevel(root, bg=C_BORDER)  # 1px border via bg + padding
            pop.overrideredirect(True)
            pop.attributes("-topmost", True)
            _overflow_pop[0] = pop

            body = tk.Frame(pop, bg=C_CARD)
            body.pack(fill="both", expand=True, padx=1, pady=1)

            try:
                scale = ctk.ScalingTracker.get_widget_scaling(root)
            except Exception:  # noqa: BLE001
                scale = 1.0
            row_font = ("Segoe UI", -int(13 * scale))
            pad_x = int(14 * scale)

            def _run(command: Callable[[], None]) -> None:
                _dismiss_overflow()
                # Let the grab release settle before opening dialogs/boxes.
                root.after(10, command)

            item_side = "right" if is_rtl() else "left"
            item_anchor = "e" if is_rtl() else "w"

            def add_item(label: str, command: Callable[[], None],
                         enabled: bool = True,
                         checked: Optional[bool] = None) -> None:
                row = tk.Frame(body, bg=C_CARD)
                row.pack(fill="x")
                check = tk.Label(
                    row, bg=C_CARD, font=row_font, width=2, anchor=item_anchor,
                    text="✓" if checked else "",
                    fg=C_TINT_TEXT if enabled else C_DISABLED,
                )
                check.pack(side=item_side,
                           padx=(pad_x, 0) if not is_rtl() else (0, pad_x),
                           pady=int(5 * scale))
                lbl = tk.Label(
                    row, text=label, bg=C_CARD, font=row_font,
                    anchor=item_anchor,
                    fg=C_TEXT if enabled else C_DISABLED,
                )
                lbl.pack(side=item_side, fill="x", expand=True,
                         padx=(0, pad_x) if not is_rtl() else (pad_x, 0),
                         pady=int(5 * scale))
                if not enabled:
                    return

                def _hover(on: bool) -> None:
                    color = C_TINT_BG if on else C_CARD
                    for w in (row, check, lbl):
                        try:
                            w.configure(bg=color)
                        except tk.TclError:
                            pass

                for w in (row, check, lbl):
                    w.bind("<Enter>", lambda e: _hover(True))
                    w.bind("<Leave>", lambda e: _hover(False))
                    w.bind("<Button-1>", lambda e: _run(command))
                    w.configure(cursor="hand2")

            def add_separator() -> None:
                tk.Frame(body, bg=C_BORDER, height=1).pack(
                    fill="x", padx=int(8 * scale), pady=int(4 * scale)
                )

            autostart_on = TrayIconManager.is_autostart_enabled()

            def _flip_autostart() -> None:
                TrayIconManager.set_autostart(not autostart_on)

            def _flip_minimized() -> None:
                app_state.config.start_minimized = \
                    not app_state.config.start_minimized
                app_state.config.save()
                # Re-create the shortcut with updated args if autostart is on.
                if TrayIconManager.is_autostart_enabled():
                    TrayIconManager.set_autostart(True)

            def _switch_language(lang: str) -> None:
                if lang == get_language():
                    return
                # A restart wipes any in-flight batch; don't yank the app out
                # from under an Apply/Extract/Restore that's running.
                if app_state.operation_in_progress:
                    GUI.show_info(t_raw("busy_title"), t_raw("busy_body"))
                    return
                app_state.config.ui_language = lang
                ConfigManager.save_config(app_state.monitored_folder)
                GUI.restart_app()

            # The popup is built from raw tk.Label rows, which apply their own
            # bidi — so use t_raw (plain), NOT t() (which reorders for ctk).
            add_item(t_raw("extract_all_subs"), GUI.process_all_extraction,
                     enabled=app_state.ffmpeg_available)
            add_item(t_raw("open_log"), GUI.open_log_file)
            add_item(t_raw("reset"), GUI.reset_app)
            add_separator()
            add_item(t_raw("autostart"), _flip_autostart,
                     enabled=IS_WINDOWS, checked=autostart_on)
            add_item(t_raw("start_minimized"), _flip_minimized,
                     enabled=IS_WINDOWS and autostart_on,
                     checked=app_state.config.start_minimized)
            add_separator()
            # Language toggle — one row per available language, checked = active.
            for _code, _name in LANGUAGES:
                add_item(
                    t_raw("language_item", name=_name),
                    lambda c=_code: _switch_language(c),
                    checked=(_code == get_language()),
                )

            def _flip_updates() -> None:
                app_state.config.update_check = not app_state.config.update_check
                ConfigManager.save_config(app_state.monitored_folder)
                if app_state.config.update_check:
                    GUI._maybe_check_updates()

            add_separator()
            add_item(t_raw("menu_updates"), _flip_updates,
                     checked=app_state.config.update_check)
            add_item(t_raw("menu_guide"), GUI.open_user_guide)
            add_item(t_raw("menu_report"), GUI.report_problem)
            add_item(t_raw("menu_about"), GUI.show_about)

            # Right-align to the button and open UPWARD when the menu can't
            # fit inside the window below it (the ⋯ button sits at the
            # window's bottom edge, so this is the usual case — dangling
            # over the desktop/taskbar looks broken). Physical px throughout
            # (raw Toplevel).
            pop.update_idletasks()
            pw, ph = pop.winfo_reqwidth(), pop.winfo_reqheight()
            gap = int(4 * scale)
            # Anchor the menu's edge to the button: right edge in LTR (button
            # sits at the window's right), left edge in RTL (button on the
            # left) — otherwise the menu hangs off the window edge.
            if is_rtl():
                x = more_btn.winfo_rootx()
            else:
                x = more_btn.winfo_rootx() + more_btn.winfo_width() - pw
            y = more_btn.winfo_rooty() + more_btn.winfo_height() + gap
            window_bottom = root.winfo_rooty() + root.winfo_height()
            screen_bottom = int(root.winfo_screenheight() * scale)
            if y + ph > min(window_bottom, screen_bottom):
                y = more_btn.winfo_rooty() - ph - gap
            pop.geometry(f"+{max(0, x)}+{max(0, y)}")

            # Dismiss on outside click (global grab routes it here), Escape,
            # or focus loss.
            def _on_click(event: Any) -> None:
                if not (0 <= event.x_root - pop.winfo_rootx() <= pw
                        and 0 <= event.y_root - pop.winfo_rooty() <= ph):
                    _dismiss_overflow()

            pop.bind("<Button-1>", _on_click)
            pop.bind("<Escape>", lambda e: _dismiss_overflow())
            pop.bind("<FocusOut>", lambda e: _dismiss_overflow())
            pop.focus_set()
            try:
                pop.grab_set()
            except tk.TclError:
                pass

        # ── Preview text dialog ──
        def _edit_preview_text(event: Any = None) -> None:
            dlg = GUI._studio_dialog(t_raw("preview_text_title"), 460, 220)

            field = ctk.CTkFrame(
                dlg, height=90, fg_color=C_CARD, border_width=1,
                border_color=C_BORDER, corner_radius=8,
            )
            field.pack(fill="x", padx=16, pady=(14, 6))
            field.pack_propagate(False)

            _entry_scale = ctk.ScalingTracker.get_widget_scaling(dlg)
            native: List[Optional[_NativeRTLEdit]] = [None]
            entry: Optional[tk.Text] = None

            def _current_text() -> str:
                if native[0] is not None:
                    return native[0].get()
                return entry.get("1.0", "end-1c") if entry is not None else ""

            def _apply(event: Any = None) -> None:
                new_text = _current_text().strip()
                if new_text:
                    app_state.preview_text = new_text
                    ConfigManager.save_config(app_state.monitored_folder)
                    GUI.update_preview()
                _close()

            def _close(event: Any = None) -> None:
                if native[0] is not None:
                    native[0].restore_wndproc()
                GUI._close_dialog(dlg)

            if IS_WINDOWS:
                # Tk's Entry has no bidi caret/hit-testing (clicking into
                # Arabic text mirrors the insertion point) — use a native
                # RichEdit when possible. It lives inside a plain packed
                # tk.Frame: Tk keeps packed widgets above the CTkFrame's
                # background canvas, which would cover a direct native child.
                host = tk.Frame(field, bg=C_CARD, highlightthickness=0, bd=0)
                host.pack(fill="both", expand=True, padx=10, pady=8)
                try:
                    native[0] = _NativeRTLEdit(
                        host, app_state.preview_text or "",
                        logical_px=14, text_color=C_TEXT, bg_color=C_CARD,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"Native RTL edit unavailable: {e}")
                    native[0] = None
                    host.destroy()

            if native[0] is not None:
                field.configure(border_color=C_ACCENT)  # the only input

                def _place_native(_event: Any = None) -> None:
                    if native[0] is None:
                        return
                    hw, hh = host.winfo_width(), host.winfo_height()
                    if hw <= 1:
                        return
                    native[0].place(0, 0, hw, hh)  # fill the multi-line field

                host.bind("<Configure>", _place_native)

                def _poll_native() -> None:
                    # Tk-side pump: dispatch Escape recorded by the wndproc
                    # (which itself may not call into Tcl) and keep the
                    # paragraph direction following the content. Enter is left
                    # to the control so it inserts a newline; Save commits.
                    n = native[0]
                    if n is None:
                        return
                    try:
                        if not dlg.winfo_exists():
                            return
                        key, n.pending_key = n.pending_key, None
                        if key == "escape":
                            _close()
                            return
                        n.sync_direction()
                        dlg.after(80, _poll_native)
                    except tk.TclError:
                        pass

                dlg.after(150, _place_native)
                dlg.after(120, _poll_native)
                dlg.after(500, lambda: native[0] and native[0].focus())
            else:
                # Fallback (native RichEdit unavailable): a multi-line tk.Text.
                # It lacks the native bidi caret, but supports two-line previews.
                entry = tk.Text(
                    field, font=("Segoe UI", -int(14 * _entry_scale)),
                    bg=C_CARD, fg=C_TEXT, insertbackground="white",
                    relief="flat", bd=0, highlightthickness=0,
                    wrap="word", height=3, undo=True,
                )
                entry.pack(fill="both", expand=True, padx=10, pady=8)
                entry.bind(
                    "<FocusIn>",
                    lambda e: field.configure(border_color=C_ACCENT),
                )
                entry.bind(
                    "<FocusOut>",
                    lambda e: field.configure(border_color=C_BORDER),
                )

                # Center the text — it reads centered in both languages, and
                # the well shows subtitles centered too.
                def _sync_justify(_event: Any = None) -> None:
                    try:
                        entry.tag_configure("dir", justify="center")
                        entry.tag_add("dir", "1.0", "end")
                    except tk.TclError:
                        pass

                entry.bind("<KeyRelease>", _sync_justify)
                entry.bind("<<Paste>>", lambda e: entry.after(1, _sync_justify))
                entry.bind("<<Cut>>", lambda e: entry.after(1, _sync_justify))

                entry.insert("1.0", app_state.preview_text or "")
                entry.focus_set()
                _sync_justify()
                # Enter inserts a newline; Escape cancels, the Save button
                # commits — so no <Return> → _apply binding here.
                entry.bind("<Escape>", _close)

            ctk.CTkLabel(
                dlg,
                text=t("preview_text_helper"),
                font=GUI.fonts["meta"], text_color=C_MUTED,
                anchor=ANCHOR_START(), justify=JUSTIFY_START(),
            ).pack(fill="x", padx=16, pady=(0, 10))

            btns = ctk.CTkFrame(dlg, fg_color="transparent")
            btns.pack(fill="x", padx=16)

            GUI._btn_primary(btns, t("save"), _apply, width=80).pack(
                side=SIDE_END())
            GUI._btn_quiet(btns, t("cancel"), _close, width=80).pack(
                side=SIDE_END(), padx=MPAD(0, 8)
            )

            # Escape / window-X dismiss without committing, like the other modals.
            dlg.bind("<Escape>", _close)
            dlg.protocol("WM_DELETE_WINDOW", _close)

        # ── Extract languages dialog (toggle chips; saves on every toggle) ──
        _langs_dialog: List[Optional[ctk.CTkToplevel]] = [None]

        def _open_langs_dialog() -> None:
            if _langs_dialog[0] and _langs_dialog[0].winfo_exists():
                _langs_dialog[0].lift()
                return

            dlg = GUI._studio_dialog(t_raw("langs_title"), 440, 258)
            _langs_dialog[0] = dlg

            ctk.CTkLabel(
                dlg,
                text=GUI._wrap(t("langs_intro"), "body", 408),
                font=GUI.fonts["body"], text_color=C_MUTED,
                wraplength=408, justify=JUSTIFY_START(), anchor=ANCHOR_START(),
            ).pack(fill="x", padx=16, pady=(12, 10))

            chips_box = ctk.CTkFrame(dlg, fg_color="transparent")
            chips_box.pack(fill="both", expand=True, padx=16)

            HEAD_N = 10
            tail = COMMON_LANGS[HEAD_N:]
            saved_now = set(GUI._selected_lang_codes())
            expanded = [any(code in saved_now for code, _ in tail)]

            def _toggle_lang(code: str) -> None:
                if not app_state.ffmpeg_available:
                    return
                current = set(GUI._selected_lang_codes())
                if code in current:
                    current.discard(code)
                else:
                    current.add(code)
                codes = [c for c, _ in COMMON_LANGS if c in current]
                if app_state.extract_langs:
                    app_state.extract_langs.set(",".join(codes))
                # Saves on every toggle (as today); repaints main chip + dialog.
                ConfigManager.save_config(app_state.monitored_folder)
                GUI.update_extraction_ui_state()

            def _expand() -> None:
                expanded[0] = True
                _rebuild()

            def _rebuild() -> None:
                for child in chips_box.winfo_children():
                    child.destroy()
                available = app_state.ffmpeg_available
                selected = set(GUI._selected_lang_codes())
                visible = list(COMMON_LANGS[:HEAD_N])
                if expanded[0]:
                    visible += tail

                chips: List[ctk.CTkButton] = []
                for code, _name in visible:
                    on = code in selected
                    text = t(
                        "language_chip", code=code,
                        name=t_logical(f"language_{code}"), check="",
                    )
                    if on:
                        text = _with_mark(text, "✓", lead=False)
                    chip = ctk.CTkButton(
                        chips_box, text=text, width=0, height=28,
                        corner_radius=999, border_width=1,
                        font=GUI.fonts["body_med"],
                        command=lambda c=code: _toggle_lang(c),
                    )
                    if not available:
                        chip.configure(
                            fg_color="transparent",
                            border_color=C_CHIP_OFF_BORDER,
                            text_color=C_DISABLED, hover_color=C_WINDOW,
                            state="disabled",
                        )
                    elif on:
                        chip.configure(
                            fg_color=C_CHIP_ON_BG,
                            border_color=C_CHIP_ON_BORDER,
                            text_color=C_TINT_TEXT, hover_color=C_TINT_BG,
                        )
                    else:
                        chip.configure(
                            fg_color="transparent",
                            border_color=C_CHIP_OFF_BORDER,
                            text_color=C_MUTED, hover_color=C_QUIET,
                        )
                    chips.append(chip)

                if not expanded[0] and tail:
                    more = ctk.CTkButton(
                        chips_box, text=t("langs_more", n=len(tail)), width=0,
                        height=28, corner_radius=999, border_width=1,
                        fg_color="transparent", border_color=C_CHIP_OFF_BORDER,
                        text_color=C_MUTED, hover_color=C_QUIET,
                        font=GUI.fonts["body_med"], command=_expand,
                    )
                    chips.append(more)

                # Flow layout: wrap chips into rows by measured width.
                # winfo_reqwidth is physical px — convert to logical.
                chips_box.update_idletasks()
                try:
                    scaling = ctk.ScalingTracker.get_widget_scaling(dlg)
                except Exception:  # noqa: BLE001
                    scaling = 1.0
                max_w = 408
                row: Optional[ctk.CTkFrame] = None
                used = 0.0
                for chip in chips:
                    w = chip.winfo_reqwidth() / scaling + 6
                    if row is None or used + w > max_w:
                        row = ctk.CTkFrame(chips_box, fg_color="transparent")
                        row.pack(fill="x", anchor=ANCHOR_START())
                        used = 0.0
                    chip.pack(in_=row, side=SIDE_START(), padx=MPAD(0, 6),
                              pady=(0, 6))
                    # The row frames were created after the chips, so they sit
                    # above them in the stacking order and would hide them.
                    chip.lift()
                    used += w

                # Fit the dialog to its content (reqheight is physical px).
                dlg.update_idletasks()
                req_h = int(dlg.winfo_reqheight() / scaling)
                dlg.geometry(f"440x{min(520, req_h)}")

            footer_row = ctk.CTkFrame(dlg, fg_color="transparent")
            footer_row.pack(fill="x", side="bottom", padx=16, pady=(4, 14))

            ffmpeg_lbl = ctk.CTkLabel(
                footer_row, text="",
                font=GUI.fonts["body"] if is_rtl() else GUI.fonts["mono"],
                text_color=C_DISABLED, anchor=ANCHOR_START(),
            )
            ffmpeg_lbl.pack(side=SIDE_START())

            def _refresh_footer() -> None:
                if app_state.ffmpeg_available:
                    path = FFmpegLocator.get_binary("ffmpeg") or ""
                    try:
                        shown = str(Path(path).relative_to(app_root()))
                    except (ValueError, OSError):
                        shown = Path(path).name if path else ""
                    ffmpeg_lbl.configure(
                        text=f"ffmpeg ✓ {shown.replace(chr(92), '/')}",
                        text_color=C_DISABLED,
                    )
                else:
                    ffmpeg_lbl.configure(
                        text=t("ffmpeg_disabled"),
                        text_color=C_DANGER,
                    )

            def _close() -> None:
                app_state.extraction_widgets.pop("dialog_refresh", None)
                _langs_dialog[0] = None
                GUI._close_dialog(dlg)

            GUI._btn_primary(footer_row, t("done"), _close, width=80).pack(
                side=SIDE_END()
            )

            def _dialog_refresh() -> None:
                _refresh_footer()
                _rebuild()

            # Let update_extraction_ui_state repaint us while open (e.g. the
            # background ffmpeg locate finishing).
            app_state.extraction_widgets["dialog_refresh"] = _dialog_refresh

            _refresh_footer()
            _rebuild()

            dlg.bind("<Escape>", lambda e: _close())
            dlg.protocol("WM_DELETE_WINDOW", _close)

        # ── Font picker (620×470: search + filter chip, list + specimen) ──
        _font_dialog: List[Optional[ctk.CTkToplevel]] = [None]

        def _open_font_picker() -> None:
            if _font_dialog[0] and _font_dialog[0].winfo_exists():
                _font_dialog[0].lift()
                return

            dlg = GUI._studio_dialog(t_raw("choose_font_title"), 620, 470)
            _font_dialog[0] = dlg

            _preview_full = (
                (app_state.preview_text or DEFAULT_PREVIEW_TEXT).strip()
                or DEFAULT_PREVIEW_TEXT
            )
            # Key the coverage filter off a single line, so a bilingual sample
            # doesn't force the picker to demand a font covering every script at
            # once. Prefer the first line with a real (non-ASCII) coverage key —
            # coverage tests skip ASCII, so a pure-Latin line filters nothing.
            _lines = [ln for ln in _preview_full.split("\n") if ln.strip()]
            preview_source = next(
                (ln for ln in _lines if FontSupport.test_key(ln)),
                _lines[0] if _lines else _preview_full,
            )
            _coverage_key = FontSupport.test_key(preview_source)

            # Favorites: a persisted set of family names. Each list row carries
            # a clickable star (★ on / ☆ off) and a chip filters to favorites.
            STAR_ON, STAR_OFF = "★ ", "☆ "
            favorites: Set[str] = set(app_state.config.favorite_fonts or [])
            fav_filter_var = tk.BooleanVar(value=False)

            # Script label — Arabic/Hebrew/Latin… — gives the user a
            # concrete idea of what the filter is actually checking.
            def _script_label(chars: str) -> str:
                for ch in chars:
                    cp = ord(ch)
                    if 0x0590 <= cp <= 0x05FF:
                        return "Hebrew"
                    if 0x0600 <= cp <= 0x08FF:
                        return "Arabic"
                    if 0x0E00 <= cp <= 0x0E7F:
                        return "Thai"
                    if 0x3040 <= cp <= 0x30FF:
                        return "Japanese kana"
                    if 0x3400 <= cp <= 0x9FFF:
                        return "CJK"
                    if 0xAC00 <= cp <= 0xD7AF:
                        return "Hangul"
                return "Script"

            # English name for the PIL specimen badge (drawn in Consolas,
            # which has no Arabic glyphs, so it must stay Latin); a separate
            # translated name feeds the CTk filter chip.
            _SCRIPT_KEY = {
                "Hebrew": "script_hebrew", "Arabic": "script_arabic",
                "Thai": "script_thai", "Japanese kana": "script_kana",
                "CJK": "script_cjk", "Hangul": "script_hangul",
                "Script": "script_generic",
            }
            script = _script_label(_coverage_key) if _coverage_key else ""
            script_disp = t_logical(_SCRIPT_KEY[script]) if script else ""

            def _cap_label() -> str:
                return t_logical("capable_chip", script=script_disp)

            # ── Search + coverage filter chip ─────────────────────────
            top_row = ctk.CTkFrame(dlg, fg_color="transparent")
            top_row.pack(fill="x", padx=14, pady=(12, 10))

            # Not bound as the entry's textvariable: CTkEntry then never shows
            # its placeholder. The entry copies its text in on every edit.
            search_var = tk.StringVar()
            search_entry = ctk.CTkEntry(
                top_row,
                placeholder_text=t("search_fonts_ph",
                                   n=f"{len(app_state.all_fonts):,}"),
                height=32, corner_radius=8, fg_color=C_CARD,
                border_width=1, border_color=C_BORDER,
                font=GUI.fonts["body"], text_color=C_TEXT,
                placeholder_text_color=C_MUTED, justify=JUSTIFY_START(),
            )
            search_entry.pack(side=SIDE_START(), fill="x", expand=True)
            search_entry.focus_set()

            def _sync_search(_event: Any = None) -> None:
                text = search_entry.get()  # "" while the placeholder shows
                if text != search_var.get():
                    search_var.set(text)

            search_entry.bind("<KeyRelease>", _sync_search)
            search_entry.bind("<<Paste>>", lambda e: search_entry.after(1, _sync_search))
            search_entry.bind("<<Cut>>", lambda e: search_entry.after(1, _sync_search))

            filter_var = tk.BooleanVar(value=False)
            coverage_count: List[Optional[int]] = [None]
            # Each coverage scan gets its OWN cancel Event; starting a new scan
            # sets the previous one (so a superseded worker actually stops) and
            # bumps a generation counter so only the latest scan repaints.
            _filter_active_cancel: List[Optional[threading.Event]] = [None]
            _filter_gen = [0]

            filter_chip: Optional[ctk.CTkButton] = None
            if _coverage_key:
                # The chip names the FILTER in both states ("Arabic-capable");
                # tint + count + ✓ show whether it's active. The spec's
                # off-state label ("All scripts") read as a status, not a
                # clickable filter.
                filter_chip = ctk.CTkButton(
                    top_row, text=rtl_base(_cap_label()), width=0, height=32,
                    corner_radius=999, border_width=1,
                    font=GUI.fonts["body_med"],
                    fg_color="transparent", border_color=C_CHIP_OFF_BORDER,
                    text_color=C_MUTED, hover_color=C_QUIET,
                )
                filter_chip.pack(side=SIDE_START(), padx=MPAD(8, 0))

            def _paint_chip(status: Optional[str] = None) -> None:
                if filter_chip is None:
                    return
                if status is not None:
                    filter_chip.configure(text=rtl_base(status))
                    return
                if filter_var.get():
                    text = _cap_label()
                    if coverage_count[0] is not None:
                        text += f" · {coverage_count[0]:,}"
                    filter_chip.configure(
                        text=_with_mark(rtl_base(text), "✓", lead=False),
                        fg_color=C_CHIP_ON_BG,
                        border_color=C_CHIP_ON_BORDER, text_color=C_TINT_TEXT,
                        hover_color=C_CHIP_ON_BG,
                    )
                else:
                    filter_chip.configure(
                        text=rtl_base(_cap_label()), fg_color="transparent",
                        border_color=C_CHIP_OFF_BORDER, text_color=C_MUTED,
                        hover_color=C_QUIET,
                    )

            # Favorites filter chip — always shown (favorites are script-
            # independent). Wired to _toggle_fav_filter after _populate exists.
            def _fav_label(count: str = "") -> str:
                return _with_mark(rtl_base(t_logical("favorites") + count), "★",
                                  lead=True)

            fav_chip = ctk.CTkButton(
                top_row, text=_fav_label(), width=0, height=32,
                corner_radius=999, border_width=1, font=GUI.fonts["body_med"],
                fg_color="transparent", border_color=C_CHIP_OFF_BORDER,
                text_color=C_MUTED, hover_color=C_QUIET,
            )
            fav_chip.pack(side=SIDE_START(), padx=MPAD(8, 0))

            def _paint_fav_chip() -> None:
                if fav_filter_var.get():
                    fav_chip.configure(
                        text=_with_mark(_fav_label(f" · {len(favorites):,}"), "✓",
                                        lead=False),
                        fg_color=C_CHIP_ON_BG, border_color=C_CHIP_ON_BORDER,
                        text_color=C_TINT_TEXT, hover_color=C_CHIP_ON_BG,
                    )
                else:
                    fav_chip.configure(
                        text=_fav_label(), fg_color="transparent",
                        border_color=C_CHIP_OFF_BORDER, text_color=C_MUTED,
                        hover_color=C_QUIET,
                    )

            # ── Body: font list (own-typeface rows) + PIL specimen pane ──
            body = ctk.CTkFrame(dlg, fg_color="transparent")
            body.pack(fill="both", expand=True, padx=14, pady=(0, 12))

            list_frame = ctk.CTkFrame(
                body, width=230, fg_color=C_LIST, border_width=1,
                border_color=C_BORDER, corner_radius=8,
            )
            list_frame.pack(side=SIDE_START(), fill="y")
            list_frame.pack_propagate(False)

            # No scrollbar, like the spec's list — the wheel, arrows and
            # PgUp/PgDn scroll it (and CTkScrollbar livelocks against a
            # tk.Text's yscrollcommand: its _draw pumps update_idletasks).

            _scale = ctk.ScalingTracker.get_widget_scaling(dlg)
            _row_px = max(10, int(13 * _scale))

            text_widget = tk.Text(
                list_frame,
                # yscrollcommand gets hooked below so we can trigger lazy
                # tagging on every scroll.
                bg=C_LIST, fg=C_TEXT_2,
                cursor="hand2",
                wrap="none",
                relief="flat", borderwidth=0, highlightthickness=0,
                spacing1=5, spacing3=5,
                padx=10, pady=4,
                takefocus=True,
                font=("Segoe UI", -_row_px),
            )
            text_widget.pack(side="left", fill="both", expand=True, padx=2, pady=2)
            text_widget.tag_configure(
                "highlight", background=C_ACCENT, foreground=C_ON_ACCENT
            )
            # Star colours (created after "highlight" so they out-priority its
            # foreground — the star keeps its colour on the selected blue row).
            text_widget.tag_configure("fav_on", foreground=C_WARN)
            text_widget.tag_configure("fav_off", foreground=C_DISABLED)

            specimen_lbl = ctk.CTkLabel(
                body, text="", fg_color=C_WELL, corner_radius=8
            )
            specimen_lbl.pack(side=SIDE_START(), fill="both", expand=True,
                              padx=MPAD(10, 0))

            _filtered: List[str] = []
            _selected_idx = [-1]  # index into _filtered, -1 when empty
            # Tk resolves a font the moment a tag using it is attached to a
            # line, because line-height metrics feed the scrollbar. Tagging
            # every row up-front therefore loaded 4700 typefaces and stalled
            # the app for ~30s. Instead, keep a set of lines whose font tag
            # has already been applied and only tag rows that are actually
            # about to be shown — see _upgrade_visible_rows() below.
            _tagged_lines: Set[int] = set()
            _lazy_tag_scheduled = [False]

            # ── Specimen rendering (debounced on highlight change) ──
            _specimen_job: List[Optional[str]] = [None]
            _specimen_img: List[Optional[ctk.CTkImage]] = [None]
            _specimen_info: Dict[str, Tuple[Optional[bool], Optional[int]]] = {}
            _specimen_size: List[Tuple[int, int]] = [(0, 0)]

            def _render_specimen() -> None:
                _specimen_job[0] = None
                try:
                    if not specimen_lbl.winfo_exists():
                        return
                    sw = max(specimen_lbl.winfo_width(), 60)
                    sh = max(specimen_lbl.winfo_height(), 60)
                except tk.TclError:
                    return
                try:
                    scale = ctk.ScalingTracker.get_widget_scaling(specimen_lbl)
                except Exception:  # noqa: BLE001
                    scale = 1.0
                sw, sh = int(sw / scale), int(sh / scale)

                if not _filtered or _selected_idx[0] < 0:
                    img = PreviewRenderer.render_specimen(
                        sw, sh, scale, family="", color=C_MUTED, text="",
                        pangram="", coverage=t_logical("no_matching_fonts"),
                    )
                else:
                    name = _filtered[_selected_idx[0]]
                    if name not in _specimen_info:
                        sup = (
                            FontSupport.supports_text(name, preview_source)
                            if _coverage_key else None
                        )
                        # Real Latin check: an Arabic-only font
                        # has no Latin glyphs, so its pangram would be tofu.
                        latin_ok = FontSupport.supports_latin(name)
                        info = (
                            FontSupport.font_file(name)
                            if PreviewRenderer.path_map_ready else None
                        )
                        glyphs = _font_glyph_count(*info) if info else None
                        _specimen_info[name] = (sup, glyphs, latin_ok)
                    sup, glyphs, latin_ok = _specimen_info[name]
                    parts = []
                    # √/× rather than ✓/✗ — the mono font PIL draws this line
                    # with (Consolas) has no glyphs for the latter.
                    if script:
                        mark = "√" if sup else ("×" if sup is False else "?")
                        parts.append(f"{script_disp} {mark}")
                    latin_mark = "√" if latin_ok else (
                        "×" if latin_ok is False else "?")
                    parts.append(f"{t_logical('spec_latin')} {latin_mark}")
                    if glyphs:
                        parts.append(t_logical("spec_glyphs", n=glyphs))
                    color = (
                        bgr_to_rgb_hex(app_state.selected_color_code)
                        if app_state.selected_color_code else "#ffffff"
                    )
                    # Draw the pangram in the SELECTED font so it previews that
                    # font's Latin — but omit it for Latin-less fonts (tofu).
                    pangram = "" if latin_ok is False else SPECIMEN_PANGRAM
                    # Likewise, omit the Arabic sample when the font can't
                    # render it (e.g. a Latin-only font viewed with the filter
                    # off) — the "Arabic ×" badge says why, no tofu.
                    sample = "" if sup is False else preview_source
                    img = PreviewRenderer.render_specimen(
                        sw, sh, scale, family=name, color=color,
                        text=sample, pangram=pangram,
                        coverage=" · ".join(parts),
                    )
                _specimen_img[0] = ctk.CTkImage(
                    light_image=img, dark_image=img, size=(sw, sh)
                )
                try:
                    specimen_lbl.configure(image=_specimen_img[0], text="")
                except tk.TclError:
                    pass

            def _schedule_specimen() -> None:
                if _specimen_job[0]:
                    dlg.after_cancel(_specimen_job[0])
                _specimen_job[0] = dlg.after(60, _render_specimen)

            def _on_specimen_resize(event: Any) -> None:
                # Only re-render on a REAL size change — configure(image=…)
                # itself fires <Configure>, and rounding under DPI scaling can
                # oscillate, so an ungated loop would re-render forever.
                try:
                    scale = ctk.ScalingTracker.get_widget_scaling(specimen_lbl)
                except Exception:  # noqa: BLE001
                    scale = 1.0
                w, h = int(event.width / scale), int(event.height / scale)
                last_w, last_h = _specimen_size[0]
                if abs(w - last_w) < 6 and abs(h - last_h) < 6:
                    return
                _specimen_size[0] = (w, h)
                _schedule_specimen()

            specimen_lbl.bind("<Configure>", _on_specimen_resize)

            def _upgrade_visible_rows() -> None:
                """Lazy-render each visible row in its own typeface.

                Fires on first layout and on every scroll. Fonts already
                loaded in earlier passes are cheap; new fonts cost a few
                ms each, amortized across scroll movement instead of
                paid all at once at open-time.
                """
                _lazy_tag_scheduled[0] = False
                if not _filtered:
                    return
                try:
                    top = text_widget.index("@0,0")
                    bottom = text_widget.index(
                        f"@0,{max(1, text_widget.winfo_height() - 1)}"
                    )
                    first_line = int(top.split(".")[0])
                    last_line = int(bottom.split(".")[0])
                except (tk.TclError, ValueError):
                    return

                # A small buffer above/below keeps the face stable while
                # the scroll view settles.
                first_line = max(1, first_line - 2)
                last_line = min(len(_filtered), last_line + 2)

                # Which visible lines still need tagging? If NONE, return before
                # touching config(state=…): that toggle dirties the Text, which
                # re-fires yscrollcommand → _schedule_lazy_tag → this again — a
                # perpetual after_idle loop that starves paint/input (hard hang).
                todo = [ln for ln in range(first_line, last_line + 1)
                        if ln not in _tagged_lines and 0 <= ln - 1 < len(_filtered)]
                if not todo:
                    return

                text_widget.config(state="normal")
                try:
                    for line_num in todo:
                        idx = line_num - 1
                        name = _filtered[idx]
                        # Colour the leading star (col 0) gold/dim.
                        try:
                            text_widget.tag_add(
                                "fav_on" if name in favorites else "fav_off",
                                f"{line_num}.0", f"{line_num}.1",
                            )
                        except tk.TclError:
                            pass
                        # The NAME (past the 2-char star prefix) gets its own
                        # typeface; the star stays in the readable default face.
                        tag = f"f{idx}"
                        try:
                            text_widget.tag_configure(
                                tag, font=(name, -_row_px)
                            )
                            text_widget.tag_add(
                                tag, f"{line_num}.2", f"{line_num}.end"
                            )
                        except tk.TclError:
                            # Skip fonts Tk refuses to build; the row
                            # stays in the default face.
                            pass
                        _tagged_lines.add(line_num)
                finally:
                    text_widget.config(state="disabled")

            def _schedule_lazy_tag() -> None:
                if _lazy_tag_scheduled[0]:
                    return
                _lazy_tag_scheduled[0] = True
                # after_idle coalesces bursts during continuous scrolling.
                text_widget.after_idle(_upgrade_visible_rows)

            _last_scroll: List[Any] = [None]

            def _on_scroll(first: str, last: str) -> None:
                # Only react to a REAL scroll — a redisplay from an unrelated
                # edit (star swap, tagging) re-fires this with the same fraction
                # and must NOT re-arm the lazy-tag pass (see the loop note above).
                if (first, last) == _last_scroll[0]:
                    return
                _last_scroll[0] = (first, last)
                _schedule_lazy_tag()

            text_widget.config(yscrollcommand=_on_scroll)

            def _highlight(idx: int, ensure_visible: bool = True) -> None:
                text_widget.tag_remove("highlight", "1.0", "end")
                if not (0 <= idx < len(_filtered)):
                    _selected_idx[0] = -1
                    _schedule_specimen()
                    return
                _selected_idx[0] = idx
                line = idx + 1
                text_widget.tag_add(
                    "highlight", f"{line}.0", f"{line}.end+1c"
                )
                if ensure_visible:
                    text_widget.see(f"{line}.0")
                _schedule_specimen()
                # The highlighted row should always be rendered in its
                # own face even if it hasn't been scrolled past yet.
                _schedule_lazy_tag()

            def _render_list(matched: List[str]) -> None:
                """Paint `matched` into the text widget. Main thread only."""
                text_widget.config(state="normal")
                text_widget.delete("1.0", "end")
                _filtered.clear()
                _tagged_lines.clear()

                _filtered.extend(matched)
                if matched:
                    # One batched insert — no per-row font tags yet. With
                    # the widget's default font set on the Text itself,
                    # line metrics are computed from one typeface, not
                    # 4700. Per-row faces (and star colours) are attached
                    # lazily as they scroll into view. The star GLYPH goes in
                    # now so the ★/☆ shows immediately.
                    lines = [(STAR_ON if n in favorites else STAR_OFF) + n
                             for n in matched]
                    text_widget.insert("end", "\n".join(lines) + "\n")
                text_widget.config(state="disabled")

                # Seed the highlight: current font entry if it matches,
                # else the first row.
                current = ""
                if app_state.font_combobox:
                    try:
                        current = app_state.font_combobox.get().strip().lower()
                    except tk.TclError:
                        current = ""
                start = 0
                if current:
                    for i, name in enumerate(_filtered):
                        if name.lower() == current:
                            start = i
                            break
                if _filtered:
                    _highlight(start)
                    _schedule_lazy_tag()
                else:
                    _highlight(-1)

            def _apply_coverage_filter(
                candidates: List[str],
                search_text: str,
            ) -> None:
                """
                Run the glyph-coverage filter over `candidates`. Uses the
                cache synchronously when complete; otherwise spawns a
                worker that updates the list when done.
                """
                if not _coverage_key or not filter_var.get():
                    _render_list(candidates)
                    _paint_chip()
                    return

                # Fast path: every candidate already cached.
                entry = FontSupport._cache.get(_coverage_key) or {}
                if all(f in entry for f in candidates):
                    filtered = [f for f in candidates if entry[f]]
                    coverage_count[0] = len(filtered)
                    _render_list(filtered)
                    _paint_chip()
                    return

                # Cache miss — run in a background worker. Show the
                # current (unfiltered) candidates immediately so the
                # dialog isn't empty, and update once the scan completes.
                _render_list(candidates)
                _paint_chip(t_logical("capable_scanning", script=script_disp))

                # Supersede any in-flight scan: signal its own cancel event so
                # it actually stops, then start a fresh generation with a new
                # token (the old one is never cleared, so it stays cancelled).
                if _filter_active_cancel[0] is not None:
                    _filter_active_cancel[0].set()
                cancel_token = threading.Event()
                _filter_active_cancel[0] = cancel_token
                _filter_gen[0] += 1
                my_gen = _filter_gen[0]

                candidates_snapshot = list(candidates)
                search_snapshot = search_text

                def worker() -> None:
                    def _post_status(text: str) -> None:
                        try:
                            dlg.after(0, lambda t=text: _paint_chip(t))
                        except tk.TclError:
                            pass

                    def scan_progress(i: int, total: int) -> None:
                        if cancel_token.is_set():
                            return
                        pct = (i * 100 // total) if total else 100
                        _post_status(t_logical("scan_indexing", pct=pct))

                    def progress(i: int, total: int) -> None:
                        if cancel_token.is_set():
                            return
                        pct = (i * 100 // total) if total else 100
                        _post_status(t_logical("scan_scanning", pct=pct))

                    try:
                        result = FontSupport.filter_supporting(
                            candidates_snapshot, preview_source,
                            on_progress=progress,
                            cancel=cancel_token,
                            on_scan_progress=scan_progress,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"Font coverage scan failed: {e}")
                        result = candidates_snapshot

                    def finish() -> None:
                        # A newer scan superseded this one — ignore our result
                        # so a stale worker can't repaint over the latest.
                        if my_gen != _filter_gen[0]:
                            return
                        # Dialog may have closed in the meantime.
                        try:
                            if not dlg.winfo_exists():
                                return
                        except tk.TclError:
                            return
                        # Skip the UI update if the user changed filter
                        # state or search text while we were scanning.
                        if cancel_token.is_set():
                            _paint_chip()
                            return
                        if search_var.get() != search_snapshot:
                            _paint_chip()
                            return
                        if not filter_var.get():
                            _paint_chip()
                            return
                        coverage_count[0] = len(result)
                        _render_list(result)
                        _paint_chip()

                    try:
                        dlg.after(0, finish)
                    except tk.TclError:
                        pass

                threading.Thread(target=worker, daemon=True).start()

            def _populate(filter_text: str = "") -> None:
                ft = filter_text.lower()
                if ft:
                    matched = [
                        n for n in app_state.all_fonts if ft in n.lower()
                    ]
                else:
                    matched = list(app_state.all_fonts)
                if fav_filter_var.get():
                    matched = [n for n in matched if n in favorites]
                _apply_coverage_filter(matched, filter_text)

            # Wire the chip to toggle the filter against the current search.
            if filter_chip is not None:
                def _toggle_filter() -> None:
                    filter_var.set(not filter_var.get())
                    _paint_chip()
                    _populate(search_var.get())
                filter_chip.configure(command=_toggle_filter)

            def _toggle_fav_filter() -> None:
                fav_filter_var.set(not fav_filter_var.get())
                _paint_fav_chip()
                _populate(search_var.get())
            fav_chip.configure(command=_toggle_fav_filter)

            def _apply_selection() -> None:
                if _selected_idx[0] < 0 or not _filtered:
                    return
                name = _filtered[_selected_idx[0]]
                if app_state.font_combobox:
                    app_state.font_combobox.delete(0, "end")
                    app_state.font_combobox.insert(0, name)
                    GUI.update_font_list(None)
                GUI._close_dialog(dlg)

            def _cancel() -> None:
                # Tell any running coverage scan to stop updating the UI.
                if _filter_active_cancel[0] is not None:
                    _filter_active_cancel[0].set()
                GUI._close_dialog(dlg)

            def _nav_relative(delta: int) -> str:
                if not _filtered:
                    return "break"
                new_idx = (_selected_idx[0] + delta) % len(_filtered)
                _highlight(new_idx)
                return "break"

            def _nav_clamped(delta: int) -> str:
                if not _filtered:
                    return "break"
                target = _selected_idx[0] + delta
                target = max(0, min(len(_filtered) - 1, target))
                _highlight(target)
                return "break"

            def _toggle_fav(name: str, line: int) -> None:
                on = name not in favorites
                if on:
                    favorites.add(name)
                else:
                    favorites.discard(name)
                app_state.config.favorite_fonts = sorted(favorites)
                # Debounced — a synchronous fsync'd write on every star click
                # froze the UI while marking favorites quickly.
                GUI._debounced_save()
                _paint_fav_chip()
                if fav_filter_var.get():
                    # Un-favoriting removes the row from the filtered view.
                    _populate(search_var.get())
                    return
                # Otherwise swap just this row's star glyph + colour in place.
                text_widget.config(state="normal")
                try:
                    text_widget.delete(f"{line}.0", f"{line}.1")
                    text_widget.insert(
                        f"{line}.0", STAR_ON[0] if on else STAR_OFF[0],
                        "fav_on" if on else "fav_off",
                    )
                    if _selected_idx[0] == line - 1:
                        text_widget.tag_add(
                            "highlight", f"{line}.0", f"{line}.end+1c")
                finally:
                    text_widget.config(state="disabled")

            def _on_click(event: Any) -> bool:
                """Return True if this click selected a font (a name click),
                False for a star toggle — so double-click only applies on a
                name, never on the star."""
                try:
                    idx_str = text_widget.index(f"@{event.x},{event.y}")
                    line_num, col = (int(p) for p in idx_str.split("."))
                    idx = line_num - 1
                    if not (0 <= idx < len(_filtered)):
                        return False
                    if col <= 1:  # the star column → toggle favorite
                        _toggle_fav(_filtered[idx], line_num)
                        return False
                    _highlight(idx, ensure_visible=False)
                    return True
                except (ValueError, IndexError, tk.TclError):
                    return False

            def _on_wheel(event: Any) -> str:
                # One font per wheel notch. Moves the highlight (and thus
                # the specimen) rather than just scrolling the view, so the
                # user sees each font as they scroll through.
                step = -1 if getattr(event, "delta", 0) > 0 else 1
                return _nav_relative(step)

            # Debounced search filtering.
            _search_after: List[Optional[str]] = [None]

            def _on_search(*_args: Any) -> None:
                if _search_after[0]:
                    dlg.after_cancel(_search_after[0])
                _search_after[0] = dlg.after(
                    150, lambda: _populate(search_var.get())
                )

            search_var.trace_add("write", _on_search)

            text_widget.bind(
                "<Button-1>",
                lambda e: (text_widget.focus_set(), _on_click(e)),
            )
            text_widget.bind(
                "<Double-Button-1>",
                lambda e: _apply_selection() if _on_click(e) else None,
            )
            text_widget.bind("<Up>",    lambda e: _nav_relative(-1))
            text_widget.bind("<Down>",  lambda e: _nav_relative(1))
            text_widget.bind("<Prior>", lambda e: _nav_clamped(-15))
            text_widget.bind("<Next>",  lambda e: _nav_clamped(15))
            text_widget.bind("<Return>", lambda e: _apply_selection())
            text_widget.bind("<MouseWheel>", _on_wheel)
            # Linux wheel
            text_widget.bind("<Button-4>", lambda e: _nav_relative(-1))
            text_widget.bind("<Button-5>", lambda e: _nav_relative(1))

            # Same bindings on the search entry so Up/Down/Return work
            # without losing focus from the search box.
            search_entry.bind("<Up>",     lambda e: _nav_relative(-1))
            search_entry.bind("<Down>",   lambda e: _nav_relative(1))
            search_entry.bind("<Prior>",  lambda e: _nav_clamped(-15))
            search_entry.bind("<Next>",   lambda e: _nav_clamped(15))
            search_entry.bind("<Return>", lambda e: _apply_selection())

            dlg.bind("<Escape>", lambda e: _cancel())
            dlg.protocol("WM_DELETE_WINDOW", _cancel)

            # ── Footer actions ──────────────────────────────────────
            btns = ctk.CTkFrame(dlg, fg_color="transparent")
            btns.pack(fill="x", padx=14, pady=(0, 14))
            GUI._btn_primary(btns, t("use_this_font"), _apply_selection).pack(
                side=SIDE_END()
            )
            GUI._btn_quiet(btns, t("cancel"), _cancel, width=80).pack(
                side=SIDE_END(), padx=MPAD(0, 8)
            )

            _populate()

        # ══ Listeners + initial state ══
        # Listener hops from whichever thread called record_error back to
        # the Tk main loop so configure() is always called in the right
        # thread.
        def _issues_changed_from_any_thread() -> None:
            if app_state.root:
                app_state.root.after(0, GUI._refresh_issues_label)

        IssueTracker.add_listener(_issues_changed_from_any_thread)
        GUI._refresh_issues_label()

        # Footer/status events from the monitor + batch workers (any thread).
        def _event_from_any_thread() -> None:
            if app_state.root:
                app_state.root.after(0, GUI._refresh_status_ui)

        app_state.event_listener = _event_from_any_thread

        root.protocol("WM_DELETE_WINDOW", GUI.on_close_request)
        # Every restore path funnels through a map event — catch a restore that
        # stranded the window off-screen (user can't see or drag it back).
        root.bind("<Map>", GUI._ensure_on_screen, add="+")

        GUI._set_folder_display(app_state.monitored_folder)
        if app_state.monitored_folder:
            threading.Thread(
                target=DatabaseManager.add_existing_files,
                args=(app_state.monitored_folder,),
                daemon=True,
            ).start()

            font = app_state.font_combobox.get() if app_state.font_combobox else ""
            extract = app_state.extract_enabled.get() if app_state.extract_enabled else False

            if extract or (font and app_state.selected_color_code):
                GUI.toggle_monitoring(auto=True)

        GUI._refresh_style_cells()
        GUI.update_extraction_ui_state()
        GUI._status_tick()  # paints now + keeps relative times fresh
        GUI.update_preview()

        # Warm the family→path map off-thread (first-ever build scans every
        # installed font), then re-render the preview with real font files.
        def _warm_font_map() -> None:
            try:
                FontSupport.font_file("Segoe UI")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Font path map warmup failed: {e}")
            PreviewRenderer.path_map_ready = True
            PreviewRenderer.clear_font_cache()
            # Hide fonts with no usable outline file — bitmap faces (8514oem,
            # Terminal…), Windows charset duplicates (Arial CYR), and dead legacy
            # entries. They can't be previewed and libass can't render them as
            # subtitles anyway, so they'd only clutter the list and cycling. The
            # path map is immutable once built, so this read is thread-safe;
            # the assignment hops to the GUI thread.
            usable = ([f for f in app_state.all_fonts
                       if FontSupport.font_file(f) is not None]
                      if app_state.all_fonts else None)

            def _finish() -> None:
                if usable:
                    app_state.all_fonts = usable
                GUI.update_preview()

            if app_state.root:
                app_state.root.after(0, _finish)

        threading.Thread(target=_warm_font_map, daemon=True).start()

        # Restore normal window state
        root.overrideredirect(False)
        try:
            root.iconbitmap(str(ICON_PATH))
        except tk.TclError:
            pass

        if START_MINIMIZED:
            root.withdraw()
            TrayIconManager.minimize_to_tray()
        else:
            root.update_idletasks()
            GUI._center_window(root, 780, 560)
            root.attributes("-alpha", 1)
            root.lift()
            root.focus_force()

        return root


# Claim on the single-instance mutex (+ the installer's quit signal). Module
# level so restart_app can hand the name over to the relaunched process.
_instance_guard = SingleInstance()

# A second launch drops this flag; the running instance polls for it and
# surfaces its window, so re-launching the app focuses the existing one instead
# of silently doing nothing (the window may be hidden in the tray).
SHOW_REQUEST_PATH = APPDATA_DIR / "show_request.flag"


def _cli_value(name: str) -> Optional[str]:
    """Value of `--name=VALUE` or `--name VALUE` on the command line."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
        if a == name and i + 1 < len(args):
            return args[i + 1]
    return None


SELF_TEST = "--self-test" in sys.argv[1:]
_self_test_exit_code = 0

# A small bilingual subtitle for the self-test's engine round trip: UTF-8 with a
# BOM and CRLF line endings, one dialogue style and one sign style.
_SELF_TEST_ASS = (
    "﻿[Script Info]\r\nScriptType: v4.00+\r\nPlayResX: 1920\r\n"
    "PlayResY: 1080\r\n\r\n[V4+ Styles]\r\nFormat: Name, Fontname, Fontsize, "
    "PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
    "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
    "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\r\n"
    "Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00303030,&H80000000,0,0,0,0,"
    "100,100,0,0,1,3,1,2,40,40,40,1\r\n"
    "Style: Sign,Arial,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,"
    "100,100,0,0,1,2,0,8,10,10,10,1\r\n\r\n[Events]\r\nFormat: Layer, Start, End, "
    "Style, Name, MarginL, MarginR, MarginV, Effect, Text\r\n"
    "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,سبحان الله وبحمده\r\n"
    "Dialogue: 0,0:00:04.00,0:00:08.00,Default,,0,0,0,,So remember Me\r\n"
    "Dialogue: 0,0:00:01.00,0:00:08.00,Sign,,0,0,0,,{\\pos(960,200)}لافتة\r\n"
)


def _window_screenshot(root: Any, path: Path) -> bool:
    """Save a PNG of the main window only. PrintWindow(PW_RENDERFULLCONTENT)
    renders the window's own pixels, so another window on top of it (or a
    window that never got focus) can't bleed into the image."""
    try:
        from ctypes import wintypes
        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        hwnd = wintypes.HWND(int(root.wm_frame(), 16))
        rect = _WINRECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        hdc = user32.GetWindowDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        gdi32.SelectObject(mdc, bmp)
        user32.PrintWindow(hwnd, mdc, 2)  # 2 = PW_RENDERFULLCONTENT
        header = (ctypes.c_uint32 * 10)(40, w, (-h) & 0xFFFFFFFF, 1 | (32 << 16),
                                        0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, header, 0)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
        Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "BGRX", 0, 1).save(path)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"self-test screenshot failed: {e}")
        return False


def _run_self_test(root: Any) -> None:
    """--self-test: check the packaged app end to end, write a JSON report to
    --self-test-out (default %TEMP%\\asc-self-test.json) and exit 0 (all
    required checks passed) or 1. Runs in its own throwaway data folder (the
    runtime gives a self-test its own instance token), never touches settings,
    and gives up with exit code 2 after 180 s."""
    out_path = Path(_cli_value("--self-test-out")
                    or Path(tempfile.gettempdir()) / "asc-self-test.json")
    started = time.monotonic()
    checks: Dict[str, Any] = {}
    report: Dict[str, Any] = {
        "app": APP_NAME, "version": APP_VERSION, "frozen": is_frozen(),
        "python": sys.version.split()[0], "executable": sys.executable,
        "data_dir": str(APPDATA_DIR), "instance_token": INSTANCE_TOKEN,
        "checks": checks,
    }

    def write_report(ok: bool, code: int) -> None:
        report["ok"] = ok
        report["exit_code"] = code
        report["elapsed_s"] = round(time.monotonic() - started, 2)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                encoding="utf-8")
        except OSError as e:
            logger.error(f"self-test could not write {out_path}: {e}")

    def watchdog() -> None:
        time.sleep(180)
        checks["timeout"] = {"ok": False, "detail": "self-test did not finish in 180 s"}
        write_report(False, 2)
        os._exit(2)

    threading.Thread(target=watchdog, name="self-test-watchdog", daemon=True).start()

    def check(name: str, required: bool, fn: Callable[[], Any]) -> None:
        try:
            value = fn()
            ok = bool(value) if required else True
            checks[name] = {"ok": ok, "required": required, "detail": value}
        except Exception as e:  # noqa: BLE001
            checks[name] = {"ok": not required, "required": required,
                            "detail": f"{type(e).__name__}: {e}"}

    def engine_roundtrip() -> Dict[str, Any]:
        work = APPDATA_DIR / "self-test-library" / "Show"
        work.mkdir(parents=True, exist_ok=True)
        sample = work / "Show - 01.ar.ass"
        original = _SELF_TEST_ASS.encode("utf-8")
        sample.write_bytes(original)
        applied = FileProcessor.apply_changes(sample, "Segoe UI", "00FFFF", size="medium")
        styled = sample.read_bytes()
        backup = work / "Original Subs" / sample.name
        result = {
            "applied": applied,
            "already_styled_after": FileProcessor.is_already_styled(
                sample, "Segoe UI", "00FFFF", size="medium"),
            "backup_is_original": backup.exists() and backup.read_bytes() == original,
            "crlf_kept": b"\n" not in styled.replace(b"\r\n", b""),
            "bom_kept": styled.startswith(b"\xef\xbb\xbf"),
            "sign_untouched": "Style: Sign,Arial,50" in styled.decode("utf-8-sig"),
            "recorded_in_db": DatabaseManager.was_processed(str(sample)),
        }
        result["ok"] = all(result.values())
        return result

    def watchdog_observer() -> str:
        # The monitor's filesystem watcher picks its Windows backend at import
        # time; a packaged build missing that module would fall back to polling.
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
        obs = Observer()
        obs.schedule(FileSystemEventHandler(), str(APPDATA_DIR), recursive=False)
        obs.start()
        obs.stop()
        obs.join(timeout=5)
        return type(obs).__name__

    def long_paths() -> str:
        # Libraries nest deeply; past 260 characters Windows needs the exe's
        # longPathAware manifest AND the LongPathsEnabled policy. Informational:
        # the policy is a per-PC setting.
        deep = APPDATA_DIR / "self-test-library" / ("Long Series Name " * 4).strip()
        while len(str(deep)) < 280:
            deep = deep / "Season 01 [1080p][Dual Audio]"
        try:
            deep.mkdir(parents=True, exist_ok=True)
            sample = deep / "Long Series Name - 01.ass"
            sample.write_bytes(_SELF_TEST_ASS.encode("utf-8"))
            ok = FileProcessor.apply_changes(sample, "Segoe UI", "00FFFF")
            return f"ok ({len(str(sample))} chars)" if ok else "write failed"
        except OSError as e:
            return f"not available on this PC ({e.__class__.__name__})"

    def run_checks() -> None:
        global _self_test_exit_code
        check("harfbuzz_freetype", True, lambda: _HB_SHAPING)
        check("fonts_listed", True, lambda: len(app_state.all_fonts))
        check("size_ratio_arabic", True,
              lambda: FontSupport.size_ratio("Segoe UI", "arabic"))
        check("size_ratio_latin", True,
              lambda: FontSupport.size_ratio("Segoe UI", "latin"))
        check("preview_render", True, lambda: PreviewRenderer.render_well(
            640, 360, 1.0, family="Segoe UI", color="#ffff00", bold=False,
            italic=False, underline=False, text=DEFAULT_PREVIEW_TEXT).size == (640, 360))
        roundtrip: Dict[str, Any] = {}

        def roundtrip_ok() -> bool:
            roundtrip.update(engine_roundtrip())
            return roundtrip["ok"]

        check("engine_roundtrip", True, roundtrip_ok)
        if roundtrip:
            checks["engine_roundtrip"]["detail"] = roundtrip
        check("strings_complete", True, lambda: set(i18n._STRINGS["en"])
              == set(i18n._STRINGS["ar"]))
        check("single_instance_held", True, lambda: is_running())
        check("watchdog_observer", True, watchdog_observer)
        check("long_paths", False, long_paths)
        check("ffmpeg", False, lambda: FFmpegLocator.get_binary("ffmpeg"))
        check("ffprobe", False, lambda: FFmpegLocator.get_binary("ffprobe"))
        shot = out_path.with_suffix(".png")
        check("screenshot", False, lambda: str(shot) if _window_screenshot(root, shot)
              else None)
        ok = all(c["ok"] for c in checks.values())
        _self_test_exit_code = 0 if ok else 1
        write_report(ok, _self_test_exit_code)
        root.after(0, root.destroy)

    def wait_for_fonts(tries: int = 0) -> None:
        # The font list loads on a background thread; give it up to 60 s.
        if not app_state.all_fonts and tries < 120:
            root.after(500, wait_for_fonts, tries + 1)
            return
        root.update_idletasks()
        root.after(300, run_checks)

    root.after(500, wait_for_fonts)


def _check_styled_cli(folder: str) -> int:
    """--check-styled <folder> [--config <config.json>] [--json <out.json>]:
    report, without writing anything, whether the settings would restyle any
    file in `folder`. Exit 0 = nothing would be rewritten, 1 = something would
    (or failed to read)."""
    from anime_subs.check_styled import check_styled
    cfg_path = _cli_value("--config")
    if cfg_path:
        runtime.CONFIG_FILE_PATH = Path(cfg_path)
    report = check_styled(folder, AppConfig.load())
    text = json.dumps(report, indent=2, ensure_ascii=False)
    out = _cli_value("--json")
    if out:
        Path(out).write_text(text, encoding="utf-8")
    elif sys.stdout is not None:
        print(text)
    counts = report["counts"]
    return 0 if counts["would_restyle"] == 0 and counts["error"] == 0 else 1


# ----------------------------------------
# Crash handler (no telemetry: a local report + a dialog)
# ----------------------------------------
# A windowed build has no console, so an uncaught error in a button handler or
# a worker thread used to vanish: the button did nothing and the log was empty.
_crash_dialogs_shown = 0
_crash_dialog_open = False


def _write_crash_report(exc_type: Any, exc: Any, tb: Any, where: str) -> Tuple[Optional[Path], str]:
    """Log the traceback and save it as crash-<time>.txt in the data folder."""
    import platform
    import traceback
    details = (
        f"{APP_NAME} {APP_VERSION} ({'packaged' if is_frozen() else 'source'}), "
        f"Python {sys.version.split()[0]}, Windows {platform.version()}\n"
        f"Where: {where}\nWhen: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        + "".join(traceback.format_exception(exc_type, exc, tb))
    )
    logger.error(f"Unexpected error ({where})", exc_info=(exc_type, exc, tb))
    path: Optional[Path] = APPDATA_DIR / f"crash-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    try:
        path.write_text(details, encoding="utf-8")
    except OSError:
        path = None
    return path, details


def _show_crash_dialog(details: str) -> None:
    """Bilingual "something went wrong" dialog with Copy details / Open log
    folder. At most one at a time and three per session (no dialog storms)."""
    global _crash_dialogs_shown, _crash_dialog_open
    if _crash_dialog_open or _crash_dialogs_shown >= 3:
        return
    _crash_dialogs_shown += 1
    _crash_dialog_open = True
    try:
        while True:
            choice = GUI.message(
                t_raw("crash_title"), t_raw("crash_body"), kind="error",
                buttons=[("copy", t("crash_copy")), ("folder", t("crash_open_folder")),
                         ("close", t("close"))],
                default="close", cancel="close",
            )
            if choice == "copy" and app_state.root is not None:
                try:
                    app_state.root.clipboard_clear()
                    app_state.root.clipboard_append(details)
                except tk.TclError:
                    pass
                continue  # back to the dialog: the user may also open the folder
            if choice == "folder":
                try:
                    os.startfile(str(APPDATA_DIR))
                except OSError:
                    pass
            break
    finally:
        _crash_dialog_open = False


def _handle_unexpected(exc_type: Any, exc: Any, tb: Any, where: str) -> None:
    _, details = _write_crash_report(exc_type, exc, tb, where)
    root = app_state.root
    if root is not None and threading.current_thread() is threading.main_thread():
        _show_crash_dialog(details)
    elif root is not None:
        try:
            root.after(0, lambda: _show_crash_dialog(details))
        except (tk.TclError, RuntimeError):
            pass
    else:
        GUI._native_message(t_raw("crash_title"), t_raw("crash_body"), "error")


def _install_crash_handlers(root: Any = None) -> None:
    """Route every uncaught error to the log, a crash report and a dialog."""
    sys.excepthook = lambda et, e, tb: _handle_unexpected(et, e, tb, "main thread")

    def _thread_hook(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        name = getattr(args.thread, "name", "?")
        _handle_unexpected(args.exc_type, args.exc_value, args.exc_traceback,
                           f"thread {name}")

    threading.excepthook = _thread_hook
    if root is not None:
        root.report_callback_exception = (
            lambda et, e, tb: _handle_unexpected(et, e, tb, "window"))


def _quit_on_request() -> None:
    """The installer or uninstaller asked us to exit (`--quit` from another
    process): tear down like tray Quit, on the Tk thread. Called from the quit
    listener thread."""
    def _quit() -> None:
        terminate_tracked_processes()
        app_state.shutdown()
        if app_state.tray_icon:
            try:
                app_state.tray_icon.stop()
            except Exception:
                pass
        if app_state.root:
            try:
                app_state.root.destroy()
            except Exception:
                pass

    if app_state.root is not None:
        app_state.root.after(0, _quit)

    # Backstop: a stuck modal loop or worker must not leave an upgrade waiting
    # on our files. Writes are atomic and the DB is WAL, so a hard exit is safe.
    def _backstop() -> None:
        time.sleep(10)
        logger.warning("Quit request not finished after 10 s; exiting now")
        os._exit(0)

    threading.Thread(target=_backstop, name="quit-backstop", daemon=True).start()


def main() -> None:
    """Main entry point."""
    if "--quit" in sys.argv[1:]:
        # Used by the installer/uninstaller: close a running copy, wait until
        # it has exited, and report via the exit code (0 = not running now).
        sys.exit(request_quit())

    configure_logging()

    check_folder = _cli_value("--check-styled")
    if check_folder is not None:
        sys.exit(_check_styled_cli(check_folder))

    import_from = _cli_value("--import-from")
    if import_from is not None:
        # One-time move of settings + file index from an older layout (ADR-003).
        from anime_subs.migrate import import_settings
        report = import_settings(Path(import_from), APPDATA_DIR, app_running=is_running())
        out = _cli_value("--json") or str(APPDATA_DIR / "import-report.json")
        Path(out).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                             encoding="utf-8")
        sys.exit(0 if report["ok"] else 1)
    migrate_legacy_appdata()

    # One identity for the taskbar, notifications and the installer's shortcuts
    # (they carry the same AppUserModelID).
    if IS_WINDOWS:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "RokoTech.AnimeSubsCustomizer")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Could not set the AppUserModelID: {e}")

    # Single instance check on Windows
    if not _instance_guard.acquire():
        # Already running — ask that instance to surface its window,
        # then exit. (The window is often hidden in the tray, so a bare
        # return looked like "nothing happened".)
        if IS_WINDOWS:
            # This process was just started by the user, so it may set the
            # foreground; hand that right to the running copy (ASFW_ANY), or
            # Windows keeps its window behind the current one.
            try:
                ctypes.windll.user32.AllowSetForegroundWindow(-1)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"AllowSetForegroundWindow failed: {e}")
        try:
            SHOW_REQUEST_PATH.write_text("show", encoding="utf-8")
        except OSError as e:
            logger.debug(f"Could not write show-request flag: {e}")
        return
    _instance_guard.listen_for_quit(_quit_on_request)

    # Clear any stale show-request left by a previous run before we start polling.
    try:
        SHOW_REQUEST_PATH.unlink()
    except OSError:
        pass

    logger.info(f"{APP_NAME} v{APP_VERSION} starting")
    _install_crash_handlers()
    try:
        DatabaseManager.init_db()
        root = GUI.create_gui()
    except Exception:  # noqa: BLE001 — a failed start must say why, not vanish
        _write_crash_report(*sys.exc_info(), "startup")
        GUI._native_message(t_raw("crash_title"), t_raw(
            "crash_start_body", path="%LOCALAPPDATA%\\Anime Subs Customizer"), "error")
        sys.exit(1)
    _install_crash_handlers(root)
    root.after(1000, GUI._poll_show_request)
    root.after(800, GUI._maybe_welcome)
    root.after(20000, GUI._maybe_check_updates)
    if SELF_TEST:
        _run_self_test(root)

    def locate_ffmpeg_background() -> None:
        """Locate FFmpeg tools in background thread."""
        ffmpeg_path = FFmpegLocator.get_binary("ffmpeg")
        ffprobe_path = FFmpegLocator.get_binary("ffprobe")
        logger.info("ffmpeg path: %s", ffmpeg_path)
        logger.info("ffprobe path: %s", ffprobe_path)
        app_state.ffmpeg_available = bool(ffmpeg_path and ffprobe_path)

        def update_ui():
            GUI.update_extraction_ui_state()
            # If the real-frame preview was left on last session, load a frame
            # now that ffmpeg is available.
            if GUI._preview_bg_on and GUI._preview_bg is None:
                GUI.shuffle_preview_background()

        if app_state.root:
            app_state.root.after(0, update_ui)

    threading.Thread(target=locate_ffmpeg_background, daemon=True).start()
    
    try:
        root.mainloop()
    finally:
        app_state.shutdown()
        # The mutex is deliberately NOT closed here: the OS releases it when
        # the process is gone, and an installer waiting on it (--quit) must not
        # start replacing files while this process is still shutting down.
    if SELF_TEST:
        # The self-test's throwaway data folder (its own token) goes away; the
        # report and screenshot were written outside it.
        if APPDATA_DIR.name.startswith("asc-instance-selftest-"):
            logging.shutdown()
            shutil.rmtree(APPDATA_DIR, ignore_errors=True)
        sys.exit(_self_test_exit_code)


if __name__ == "__main__":
    main()
