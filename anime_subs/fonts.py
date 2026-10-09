"""System font discovery + glyph-coverage filtering (FontLoader/FontSupport)."""
import io
import json
import os
import re
import struct
import tempfile
import threading
import unicodedata
import tkinter.font as tkFont
from tkinter import TclError
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional, Dict, List, Any, Set, Tuple, Callable

from PIL import ImageFont

# FreeType gives an authoritative cmap answer ("does this font have a glyph for
# this codepoint?"), which the PIL-getbbox heuristic below only approximates —
# and gets wrong for some real fonts (e.g. Cairo/Tajawal read as no-Arabic).
# Optional: when absent, coverage falls back to the PIL heuristic.
try:
    import freetype as _ft
except Exception:  # noqa: BLE001
    _ft = None

# HarfBuzz shapes Arabic into its connected running form so we can measure the
# reading BODY (what the eye reads) rather than isolated-glyph flourishes — the
# latter vary wildly between fonts and mis-sized the Arabic Fontsize. Optional:
# when absent, Arabic size normalization falls back to a neutral ratio (the
# preset is applied unscaled, i.e. one Fontsize per preset for all Arabic fonts).
try:
    import uharfbuzz as _hb
except Exception:  # noqa: BLE001
    _hb = None

from .runtime import *
from .runtime import _CACHE_MISSING

__all__ = ["FontLoader", "FontSupport", "FONT_SUPPORT_CACHE_PATH", "FONT_PATH_CACHE_PATH",
           "ft_face"]



def ft_face(path: str, index: int = 0) -> Any:
    """Open a FreeType face from the font file's bytes. freetype-py passes the
    path to FreeType's ANSI fopen, which can't open a non-ASCII path (an Arabic
    Windows username, an Arabic font file name); reading the bytes ourselves
    works for every path."""
    with open(path, "rb") as f:
        return _ft.Face(io.BytesIO(f.read()), index)


# ----------------------------------------
# Font Loader (Lazy Loading)
# ----------------------------------------
class FontLoader:
    """System-font list provider. Caches the result after first lookup."""
    _fonts: Optional[List[str]] = None
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def get_fonts(
        cls,
        callback: Optional[Callable[[List[str]], None]] = None,
    ) -> List[str]:
        """
        Return the sorted list of installed font families.

        tkFont.families() issues Tcl commands and must run on the Tk main
        thread (Tcl is not thread-safe), so this resolves synchronously on
        the caller rather than in a background worker. The call itself is
        cheap, so doing it inline at startup is fine. `callback`, if given,
        is invoked with the list once available (kept for call-site
        compatibility with the old async API).
        """
        with cls._lock:
            cached = cls._fonts
        if cached is not None:
            if callback:
                callback(cached)
            return cached.copy()

        try:
            # Drop Windows' "@"-prefixed vertical-writing variants: they're CJK
            # duplicates for top-to-bottom layout that render neither Arabic nor
            # Latin here, and only clutter the list (and font cycling).
            fonts = sorted(f for f in set(tkFont.families())
                           if not f.startswith("@"))
        except TclError as e:
            # Only swallow the *expected* failure: Tcl/display not available.
            # A broad `except Exception` here previously hid a programming error
            # (a missing tkFont import) as a silent empty font list — let those
            # propagate so they surface loudly instead.
            logger.error(f"Failed to load fonts: {e}")
            fonts = []

        with cls._lock:
            cls._fonts = fonts

        if callback:
            callback(fonts)
        return list(fonts)


# ----------------------------------------
# Font glyph-coverage filter
# ----------------------------------------
FONT_SUPPORT_CACHE_PATH = APPDATA_DIR / "font_support_cache.json"
FONT_PATH_CACHE_PATH = APPDATA_DIR / "font_path_cache.json"


class FontSupport:
    """
    Test which installed fonts can render a given text, using Pillow's
    FreeType backend. Results are cached on disk keyed by the set of
    non-ASCII characters in the text, so the 4700-font scan only runs
    once per unique script.
    """

    # Bump whenever the coverage-test logic changes, so stale "False"
    # entries (e.g. from the era when PIL couldn't resolve most family
    # names to files and mis-flagged them as unsupported) are discarded
    # on the next load.
    # v5: test_key now excludes invisible controls/format/selectors/PUA, so
    # old keys (which could embed U+202B, variation selectors, etc.) are
    # stale — discard them so their spurious False verdicts don't linger.
    _CACHE_VERSION = 6  # bumped: coverage now uses FreeType's cmap, not getbbox
    # Bump whenever the path-scan name-extraction changes its output, so
    # stale path maps (missing Calibri Light / dash-named Arabic faces)
    # get rebuilt on next launch.
    # v3: letter-less (symbol) subfamilies are treated as Regular, so the bare
    # family resolves (e.g. HSIshraq-Light, whose subfamily is "☞").
    _PATH_CACHE_VERSION = 5  # bumped: store per-instance variation coords too

    # { test_key: { family_name: supported_bool } }
    _cache: Dict[str, Dict[str, bool]] = {}
    _lock = threading.Lock()
    _loaded = False

    # { display_name: (font_file_path, ttc_index) } — built lazily by
    # scanning the Fonts directories. PIL can't resolve most Windows
    # family names ("Segoe UI" etc.) to their files via name alone, so
    # we do the lookup ourselves and hand PIL a full path instead.
    _path_map: Optional[Dict[str, Tuple[str, int]]] = None
    # Same data keyed by normalized (case-/punctuation-agnostic) names
    # so user-facing family names whose casing or separators differ
    # from the name-table entry can still find their file.
    _normalized_path_map: Optional[Dict[str, Tuple[str, int]]] = None
    # Variable-font named-instance design coords, keyed the same two ways as
    # the path map: family name -> ((axisTag, value), …). The renderer applies
    # these so each weight of a variable font previews at its actual weight.
    _var_coords: Dict[str, Tuple[Tuple[str, float], ...]] = {}
    _normalized_var_coords: Dict[str, Tuple[Tuple[str, float], ...]] = {}
    _path_map_loaded = False

    # ASCII always renders, so we skip it in the coverage test. A font's
    # tofu baseline is inferred from high-plane Private Use Area — no
    # real font has glyphs there.
    _TOFU_PROBE_CODEPOINTS = (0x10FFFD, 0x10FFFC, 0x10FFFB)
    _PROBE_SIZE = 20

    @classmethod
    def _load_cache(cls) -> None:
        with cls._lock:
            if cls._loaded:
                return
            cls._loaded = True
            if not FONT_SUPPORT_CACHE_PATH.exists():
                return
            try:
                with open(FONT_SUPPORT_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Could not load font support cache: {e}")
                return
            if data.get("version") != cls._CACHE_VERSION:
                return
            entries = data.get("entries", {})
            if isinstance(entries, dict):
                # Defensive: only keep well-typed inner maps.
                cls._cache = {
                    k: {f: bool(v) for f, v in e.items()}
                    for k, e in entries.items()
                    if isinstance(e, dict)
                }
                logger.info(
                    f"Font support cache loaded: "
                    f"{sum(len(v) for v in cls._cache.values())} entries"
                )

    @classmethod
    def _save_cache(cls) -> None:
        try:
            with cls._lock:
                # Deep-copy the nested entry dicts under the lock; json.dump
                # below runs unlocked and would otherwise raise "dictionary
                # changed size during iteration" if a worker inserts mid-dump.
                payload = {
                    "version": cls._CACHE_VERSION,
                    "entries": {k: dict(v) for k, v in cls._cache.items()},
                }
            # Unique temp per call: filter_supporting can run on more than one
            # thread (preview + picker), so a FIXED temp name would let two savers
            # truncate/interleave the same staging file before os.replace. mkstemp
            # gives each saver its own temp — the pattern media/runtime already use.
            fd, tmp = tempfile.mkstemp(
                dir=str(FONT_SUPPORT_CACHE_PATH.parent),
                prefix=".fontcache_", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False)
                os.replace(tmp, str(FONT_SUPPORT_CACHE_PATH))
                tmp = None
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as e:
            logger.warning(f"Could not save font support cache: {e}")

    @staticmethod
    def _is_renderable(ch: str) -> bool:
        """True if `ch` is a non-ASCII character a font is expected to draw a
        visible glyph for — i.e. worth coverage-testing.

        Excludes ASCII, whitespace, and every invisible/undrawable class: the
        control/format/surrogate/private-use/unassigned categories (BiDi
        controls like U+202B — which wrap_rtl_if_needed itself injects —
        joiners, etc.) plus the default-ignorable variation selectors and tag
        characters that fall outside those categories. Combining marks (Arabic
        harakat and the like) are KEPT: they're real content a proper font
        renders.
        """
        cp = ord(ch)
        if cp < 0x80 or ch.isspace():
            return False
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            return False
        # Default-ignorables not caught by the categories above: variation
        # selectors (FE00–FE0F, E0100–E01EF) and tag chars (E0000–E007F).
        if (0xFE00 <= cp <= 0xFE0F or 0xE0100 <= cp <= 0xE01EF
                or 0xE0000 <= cp <= 0xE007F):
            return False
        return True

    @classmethod
    def renderable_chars(cls, text: str) -> List[str]:
        """Every renderable non-ASCII character in `text`, WITH repetition —
        for occurrence-weighted coverage. See _is_renderable."""
        return [ch for ch in text if cls._is_renderable(ch)]

    @classmethod
    def test_key(cls, text: str) -> str:
        """
        Return a stable cache key: the *distinct* renderable non-ASCII
        characters in `text`, sorted. Empty if nothing renderable/non-ASCII
        remains. (Invisible controls/selectors are excluded — see
        _is_renderable — so RTL text no longer looks "unrenderable" over the
        U+202B/U+202C that wrap_rtl_if_needed stamps into it.)
        """
        if not text:
            return ""
        return "".join(sorted(set(cls.renderable_chars(text))))

    @classmethod
    def _font_dirs(cls) -> List[Path]:
        """System-wide and per-user font directories to scan."""
        dirs: List[Path] = []
        if IS_WINDOWS:
            windir = os.environ.get("WINDIR", "C:\\Windows")
            dirs.append(Path(windir) / "Fonts")
            local = os.environ.get("LOCALAPPDATA")
            if local:
                dirs.append(
                    Path(local) / "Microsoft" / "Windows" / "Fonts"
                )
        else:
            # Best-effort for other platforms.
            for candidate in (
                Path("/usr/share/fonts"),
                Path("/usr/local/share/fonts"),
                Path.home() / ".fonts",
                Path.home() / "Library" / "Fonts",
            ):
                dirs.append(candidate)
        return [d for d in dirs if d.is_dir()]

    @classmethod
    def _dir_fingerprint(cls) -> str:
        """Stable identity of fonts dirs for cache invalidation.

        Includes each dir's mtime AND, per dir, the font-file count and the
        newest font-file mtime. The dir mtime alone misses an *in-place* font
        replacement (same filename, new bytes) — Windows updates the file's
        mtime but not the directory's — which would otherwise serve a stale
        cached path for that family.
        """
        parts = []
        for d in cls._font_dirs():
            try:
                dir_mtime = d.stat().st_mtime_ns
                count = 0
                newest = 0
                for p in d.iterdir():
                    if p.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"):
                        count += 1
                        try:
                            newest = max(newest, p.stat().st_mtime_ns)
                        except OSError:
                            pass
                parts.append(f"{d}|{dir_mtime}|{count}|{newest}")
            except OSError:
                parts.append(f"{d}|?")
        return ";".join(parts)

    @classmethod
    def _load_path_cache(
        cls,
    ) -> Optional[Tuple[Dict[str, Tuple[str, int]],
                        Dict[str, Tuple[Tuple[str, float], ...]]]]:
        if not FONT_PATH_CACHE_PATH.exists():
            return None
        try:
            with open(FONT_PATH_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not load font path cache: {e}")
            return None
        if data.get("version") != cls._PATH_CACHE_VERSION:
            return None
        if data.get("fingerprint") != cls._dir_fingerprint():
            return None
        raw = data.get("paths", {})
        if not isinstance(raw, dict):
            return None
        paths = {
            k: (v[0], int(v[1]))
            for k, v in raw.items()
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)
        }
        coords: Dict[str, Tuple[Tuple[str, float], ...]] = {}
        raw_c = data.get("coords", {})
        if isinstance(raw_c, dict):
            for k, v in raw_c.items():
                try:
                    coords[k] = tuple(
                        (str(t), float(val)) for t, val in v
                    )
                except (TypeError, ValueError):
                    continue
        return paths, coords

    @classmethod
    def _save_path_cache(
        cls,
        paths: Dict[str, Tuple[str, int]],
        coords: Dict[str, Tuple[Tuple[str, float], ...]],
    ) -> None:
        try:
            data = {
                "version": cls._PATH_CACHE_VERSION,
                "fingerprint": cls._dir_fingerprint(),
                "paths": {k: [v[0], v[1]] for k, v in paths.items()},
                "coords": {k: [[t, val] for t, val in ax]
                           for k, ax in coords.items()},
            }
            # Unique temp per call (see _save_cache): two concurrent scans must
            # not clobber a shared fixed staging file before os.replace.
            fd, tmp = tempfile.mkstemp(
                dir=str(FONT_PATH_CACHE_PATH.parent),
                prefix=".fontpath_", suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                os.replace(tmp, str(FONT_PATH_CACHE_PATH))
                tmp = None
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as e:
            logger.warning(f"Could not save font path cache: {e}")

    # Name IDs defined by the OpenType 'name' table spec.
    _NID_FAMILY = 1
    _NID_SUBFAMILY = 2
    _NID_FULL = 4
    _NID_POSTSCRIPT = 6
    _NID_PREF_FAMILY = 16
    _NID_PREF_SUBFAMILY = 17

    _REGULAR_STYLES = frozenset({
        "", "regular", "normal", "book", "roman", "plain",
    })

    @staticmethod
    def _read_name_table(
        path: Path, face_index: int = 0
    ) -> Dict[int, str]:
        """
        Parse just the OpenType 'name' table out of a TTF/OTF/TTC file
        and return {name_id: string} for Windows/Unicode BMP records,
        preferring English (lang=0x0409) when available.

        Purely-structural parser: never decodes glyph data, so it's
        fast (~1 ms/file) and tolerant of font-format oddities.
        Returns {} on any parse failure.
        """
        try:
            with open(path, "rb") as f:
                header = f.read(12)
                if len(header) < 12:
                    return {}

                # TTC/OTC: jump to the requested face's offset table.
                if header[:4] == b"ttcf":
                    num_fonts = struct.unpack(">I", header[8:12])[0]
                    if face_index >= num_fonts:
                        return {}
                    f.seek(12 + face_index * 4)
                    face_off = struct.unpack(">I", f.read(4))[0]
                    f.seek(face_off)
                    header = f.read(12)

                if header[:4] not in (
                    b"\x00\x01\x00\x00",  # TrueType
                    b"OTTO",              # CFF OpenType
                    b"true",              # Apple TrueType
                    b"typ1",              # PostScript Type1 sfnt
                ):
                    return {}

                num_tables = struct.unpack(">H", header[4:6])[0]

                # Locate the 'name' table.
                name_off = None
                for _ in range(num_tables):
                    entry = f.read(16)
                    if len(entry) < 16:
                        return {}
                    if entry[:4] == b"name":
                        name_off = struct.unpack(">I", entry[8:12])[0]
                        break
                if name_off is None:
                    return {}

                f.seek(name_off)
                nm_hdr = f.read(6)
                if len(nm_hdr) < 6:
                    return {}
                _fmt, count, str_start = struct.unpack(">HHH", nm_hdr)

                records = []
                for _ in range(count):
                    rec = f.read(12)
                    if len(rec) < 12:
                        break
                    records.append(struct.unpack(">HHHHHH", rec))

                # Prefer English (US) Windows Unicode BMP; accept any
                # other Windows-Unicode record as a fallback.
                best: Dict[int, Tuple[int, int, int]] = {}
                for plat, enc, lang, nid, length, off in records:
                    if plat != 3 or enc != 1:
                        continue
                    score = 2 if lang == 0x0409 else 1
                    prev = best.get(nid)
                    if prev is not None and prev[0] >= score:
                        continue
                    best[nid] = (score, length, off)

                result: Dict[int, str] = {}
                for nid, (_sc, length, off) in best.items():
                    f.seek(name_off + str_start + off)
                    raw = f.read(length)
                    try:
                        result[nid] = raw.decode("utf-16-be")
                    except UnicodeDecodeError:
                        continue
                return result
        except (OSError, struct.error):
            return {}

    @staticmethod
    def _read_fvar_instances(
        path: Path, face_index: int = 0
    ) -> Tuple[List[str], List[Tuple[int, Tuple[float, ...]]]]:
        """Parse a variable font's 'fvar': return (axis_tags, [(subfamilyNameID,
        design_coords), …]). tkFont enumerates these named instances (e.g.
        "Advent Pro Black", the 9 weights of "Imbue") as separate families, so we
        index them AND record each one's design coordinates so the renderer can
        show the actual weight. Empty for static fonts. Pure struct parse —
        thread-safe for the parallel scan."""
        try:
            with open(path, "rb") as f:
                header = f.read(12)
                if len(header) < 12:
                    return [], []
                if header[:4] == b"ttcf":
                    num_fonts = struct.unpack(">I", header[8:12])[0]
                    if face_index >= num_fonts:
                        return [], []
                    f.seek(12 + face_index * 4)
                    face_off = struct.unpack(">I", f.read(4))[0]
                    f.seek(face_off)
                    header = f.read(12)
                if header[:4] not in (b"\x00\x01\x00\x00", b"OTTO",
                                      b"true", b"typ1"):
                    return [], []
                num_tables = struct.unpack(">H", header[4:6])[0]
                fvar_off = None
                for _ in range(num_tables):
                    entry = f.read(16)
                    if len(entry) < 16:
                        return [], []
                    if entry[:4] == b"fvar":
                        fvar_off = struct.unpack(">I", entry[8:12])[0]
                        break
                if fvar_off is None:
                    return [], []
                f.seek(fvar_off)
                fhdr = f.read(16)
                if len(fhdr) < 16:
                    return [], []
                (_maj, _min, axes_off, _pairs, axis_count, axis_size,
                 inst_count, inst_size) = struct.unpack(">HHHHHHHH", fhdr)
                # Guard against absurd headers before seeking around the file.
                if (inst_count > 2000 or axis_count > 64 or axis_size < 20
                        or inst_size < 4 + axis_count * 4):
                    return [], []
                tags: List[str] = []
                for i in range(axis_count):
                    f.seek(fvar_off + axes_off + i * axis_size)
                    tag = f.read(4)
                    if len(tag) < 4:
                        return [], []
                    tags.append(tag.decode("latin-1", "replace"))
                inst_start = fvar_off + axes_off + axis_count * axis_size
                insts: List[Tuple[int, Tuple[float, ...]]] = []
                for i in range(inst_count):
                    f.seek(inst_start + i * inst_size)
                    rec = f.read(4 + axis_count * 4)  # nameID + flags + coords
                    if len(rec) < 4 + axis_count * 4:
                        break
                    sub_nid = struct.unpack(">H", rec[:2])[0]
                    coords = tuple(
                        struct.unpack(">l", rec[4 + j * 4:8 + j * 4])[0] / 65536.0
                        for j in range(axis_count)
                    )
                    insts.append((sub_nid, coords))
                return tags, insts
        except (OSError, struct.error):
            return [], []

    @classmethod
    def _names_for_face(cls, table: Dict[int, str]) -> Set[str]:
        """
        Produce every plausible display name tkFont/GDI might report for
        this face. Called with one font-face's 'name' table.

        Covers:
          • NID 1 alone (if subfamily is Regular)
          • NID 1 + " " + NID 2 (for e.g. "Arial Black", "Calibri Light")
          • NID 4 (full font name — what GDI usually registers)
          • Preferred family (NID 16) with/without preferred subfamily 17
          • Dash-for-space variants so "AI-Jazeera-Arabic-Light" matches
            an internal "AI-Jazeera-Arabic Light" too.
        """
        family = (table.get(cls._NID_FAMILY) or "").strip()
        subfamily = (table.get(cls._NID_SUBFAMILY) or "").strip()
        full = (table.get(cls._NID_FULL) or "").strip()
        pref_family = (table.get(cls._NID_PREF_FAMILY) or "").strip()
        pref_sub = (table.get(cls._NID_PREF_SUBFAMILY) or "").strip()

        aliases: Set[str] = set()

        def _add(name: str) -> None:
            name = name.strip()
            if not name:
                return
            aliases.add(name)
            # Some Arabic fonts register with dashes (e.g.
            # "AI-Jazeera-Arabic-Light") while the name table holds a
            # space-joined form — emit both.
            if " " in name:
                aliases.add(name.replace(" ", "-"))

        # A subfamily with no letters (e.g. HSIshraq-Light's "☞" placeholder)
        # is junk — GDI/tkFont report the bare family, so treat it as Regular
        # rather than gluing the symbol on ("HSIshraq-Light ☞", which nothing
        # looks up).
        def _regular(sub: str) -> bool:
            return (sub.lower() in cls._REGULAR_STYLES
                    or not any(c.isalpha() for c in sub))

        if family:
            if _regular(subfamily):
                _add(family)
            else:
                _add(f"{family} {subfamily}")

        _add(full)

        if pref_family:
            if _regular(pref_sub):
                _add(pref_family)
            else:
                _add(f"{pref_family} {pref_sub}")

        return aliases

    @staticmethod
    def _probe_font_file(
        path: Path,
    ) -> List[Tuple[str, Tuple[str, int], Optional[Tuple[Tuple[str, float], ...]]]]:
        """
        Return [(alias_name, (path, index), coords_or_None), …] for every face
        in the file, covering all reasonable name variants (family alone,
        family+style, full name, preferred WWS family, dashed forms) plus every
        variable-font named instance. `coords` is that instance's design
        coordinates (((axisTag, value), …)) so the renderer can show its actual
        weight; None for a plain static face. Tolerant of collections (.ttc) and
        malformed files.
        """
        ext = path.suffix.lower()
        if ext not in (".ttf", ".otf", ".ttc", ".otc"):
            return []

        is_collection = ext in (".ttc", ".otc")
        indices = range(64) if is_collection else (0,)

        out: List[Tuple[str, Tuple[str, int],
                        Optional[Tuple[Tuple[str, float], ...]]]] = []
        for index in indices:
            table = FontSupport._read_name_table(path, index)
            if not table:
                # Collection: no more faces — stop iterating.
                # Single file: table missing = unreadable, bail.
                break

            aliases = FontSupport._names_for_face(table)

            # Variable-font named instances (e.g. "Advent Pro Black", the 9
            # weights of "Imbue") — tkFont lists each as its own family, so map
            # every one onto this file WITH its design coordinates so the
            # renderer can select the right weight instead of the file default.
            coords_by_alias: Dict[str, Tuple[Tuple[str, float], ...]] = {}
            tags, insts = FontSupport._read_fvar_instances(path, index)
            if insts:
                pref = (table.get(FontSupport._NID_PREF_FAMILY)
                        or table.get(FontSupport._NID_FAMILY) or "").strip()
                for sub_nid, coords in insts:
                    sub = (table.get(sub_nid) or "").strip()
                    if not (pref and sub and any(c.isalpha() for c in sub)):
                        continue
                    axes = tuple(zip(tags, coords))
                    if sub.lower() in FontSupport._REGULAR_STYLES:
                        names = [pref]  # tkFont shows the bare family here
                    else:
                        names = [f"{pref} {sub}",
                                 f"{pref} {sub}".replace(" ", "-")]
                    for nm in names:
                        aliases.add(nm)
                        coords_by_alias[nm] = axes

            if not aliases:
                # No name records we recognize. Last-resort fallback:
                # ask PIL directly.
                try:
                    f = ImageFont.truetype(str(path), size=10, index=index)
                    fam, sty = f.getname()
                except Exception:  # noqa: BLE001
                    if is_collection:
                        break
                    continue
                if fam:
                    if (sty or "").lower() in FontSupport._REGULAR_STYLES:
                        aliases = {fam}
                    else:
                        aliases = {f"{fam} {sty}"}

            path_info = (str(path), index)
            for alias in aliases:
                out.append((alias, path_info, coords_by_alias.get(alias)))

        return out

    @staticmethod
    def _normalize_name(s: str) -> str:
        """
        Collapse a font name to a case-insensitive, whitespace-and-
        punctuation-agnostic form, used only as a fallback lookup key
        so "AI-Jazeera-Arabic_Light" matches "AI Jazeera Arabic Light".
        """
        if not s:
            return ""
        cleaned = "".join(
            " " if ch in "-_" else ch
            for ch in s
        )
        return " ".join(cleaned.lower().split())

    @staticmethod
    def _safe_probe_font_file(
        path: Path,
    ) -> List[Tuple[str, Tuple[str, int],
                    Optional[Tuple[Tuple[str, float], ...]]]]:
        """_probe_font_file wrapped so one malformed/hostile font file can never
        abort the whole parallel scan (pool.map re-raises on iteration)."""
        try:
            return FontSupport._probe_font_file(path)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"Skipping unreadable font {path}: {e}")
            return []

    @classmethod
    def _scan_font_files(
        cls,
        on_progress: Optional[Callable[[int, int], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> Tuple[Dict[str, Tuple[str, int]],
               Dict[str, Tuple[Tuple[str, float], ...]], bool]:
        """
        Walk every Fonts directory and build a family_name → (path, index)
        map (plus a family_name → variation-coords map for named instances).
        Parallelized across files since PIL's native font loader releases the
        GIL.

        Returns (result, coords, completed) where completed is False if the scan
        was cancelled partway — the caller must NOT persist a partial result as
        if it were a full scan.
        """
        files: List[Path] = []
        for d in cls._font_dirs():
            try:
                for p in d.iterdir():
                    if p.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc"):
                        files.append(p)
            except OSError as e:
                logger.warning(f"Cannot list {d}: {e}")

        result: Dict[str, Tuple[str, int]] = {}
        coords: Dict[str, Tuple[Tuple[str, float], ...]] = {}
        total = len(files)
        if total == 0:
            return result, coords, True

        processed = 0
        completed = True
        # Up to 8 workers; I/O + native font parsing scale well with threads.
        with ThreadPoolExecutor(max_workers=8) as pool:
            future_iter = pool.map(cls._safe_probe_font_file, files)
            for entries in future_iter:
                if cancel is not None and cancel.is_set():
                    completed = False
                    break
                for family, path_info, axes in entries:
                    # First writer wins — keeps behavior stable when a
                    # family has multiple face files.
                    if family not in result:
                        result[family] = path_info
                        if axes:
                            coords[family] = axes
                processed += 1
                if on_progress is not None:
                    try:
                        on_progress(processed, total)
                    except Exception:  # noqa: BLE001
                        pass
        return result, coords, completed

    @classmethod
    def _install_path_map(
        cls,
        scanned: Dict[str, Tuple[str, int]],
        coords: Dict[str, Tuple[Tuple[str, float], ...]],
    ) -> None:
        """Publish the scanned path map + its normalized index + var coords."""
        normalized: Dict[str, Tuple[str, int]] = {}
        norm_coords: Dict[str, Tuple[Tuple[str, float], ...]] = {}
        for name, info in scanned.items():
            key = cls._normalize_name(name)
            if key and key not in normalized:
                normalized[key] = info
                # Bind the normalized coords entry to the SAME winning name as
                # the normalized path, so variation_axes() and font_file() can
                # never resolve one normalized name to two different files (the
                # renderer applies coords positionally against the resolved
                # file's own axes).
                ax = coords.get(name)
                if ax is not None:
                    norm_coords[key] = ax
        with cls._lock:
            cls._path_map = scanned
            cls._normalized_path_map = normalized
            cls._var_coords = coords
            cls._normalized_var_coords = norm_coords
            cls._path_map_loaded = True

    @classmethod
    def _get_path_map(
        cls,
        on_scan_progress: Optional[Callable[[int, int], None]] = None,
        cancel: Optional[threading.Event] = None,
    ) -> Dict[str, Tuple[str, int]]:
        """Return the cached family→path map, building it on first use."""
        with cls._lock:
            if cls._path_map_loaded and cls._path_map is not None:
                return cls._path_map

        cached = cls._load_path_cache()
        if cached is not None:
            paths, coords = cached
            cls._install_path_map(paths, coords)
            logger.info(
                f"Font path cache hit: {len(paths)} aliases"
            )
            return paths

        logger.info("Scanning font files to build family→path map…")
        scanned, coords, completed = cls._scan_font_files(
            on_progress=on_scan_progress, cancel=cancel,
        )
        # Only persist AND install a scan that ran to completion. A cancelled
        # (partial) map must not be written under the current valid dir
        # fingerprint (it would be accepted on every later launch, permanently
        # hiding every font not yet scanned before the cancel) NOR installed +
        # marked loaded in-memory (it would hide them for the rest of this
        # session too). The only caller, filter_supporting, early-returns on
        # cancel, so leaving the map un-installed just makes the next call
        # re-scan from scratch.
        if completed and scanned:
            cls._save_path_cache(scanned, coords)
            cls._install_path_map(scanned, coords)
            logger.info(f"Font path map built: {len(scanned)} aliases")
        else:
            logger.info(
                "Font scan was cancelled or empty; not caching/installing it."
            )
        return scanned

    @classmethod
    def font_file(cls, family: str) -> Optional[Tuple[str, int]]:
        """Public: resolve a family name to (font_file_path, ttc_index).

        Uses the disk-cached family→path map, building it on first use — a
        fresh build scans every installed font, so warm this off the GUI
        thread before rendering previews with it.
        """
        if not family:
            return None
        cls._get_path_map()
        return cls._lookup_path(family)

    @classmethod
    def variation_axes(
        cls, family: str
    ) -> Optional[Tuple[Tuple[str, float], ...]]:
        """Public: variable-font design coords for a named-instance family name
        (((axisTag, value), …)), or None for a static font / unknown name. Same
        exact → normalized → optical-size-stripped resolution as font_file."""
        if not family:
            return None
        cls._get_path_map()
        ax = cls._var_coords.get(family)
        if ax is not None:
            return ax
        norm = cls._normalized_var_coords or {}
        key = cls._normalize_name(family)
        ax = norm.get(key) if key else None
        if ax is not None:
            return ax
        stripped = re.sub(r"\s*\b\d+\s*pt\b", "", family, flags=re.I).strip()
        if stripped and stripped != family:
            ax = cls._var_coords.get(stripped)
            if ax is not None:
                return ax
            k2 = cls._normalize_name(stripped)
            ax = norm.get(k2) if k2 else None
            if ax is not None:
                return ax
        # Same prefix winner as _lookup_path (norm coords are bound to the same
        # winning name as the norm path map), so coords never come from a file
        # different than font_file() resolves.
        pk = cls._prefix_key(key, family)
        return norm.get(pk) if pk else None

    @classmethod
    def supports_text(cls, family: str, text: str) -> Optional[bool]:
        """Public: best-effort 'does family render text's non-ASCII chars?'.

        True/False when judged; None when the font itself couldn't be loaded.
        """
        return cls._supports_chars(family, cls.test_key(text))

    @classmethod
    def _prefix_key(cls, key: str, orig: str) -> Optional[str]:
        """Shortest normalized path-map key that `key` is a proper prefix of,
        accepted only when the completion starts at a WORD boundary (a whole
        extra word like ' ExtraBold') OR `orig` is a Windows-truncated name (GDI
        caps font enumeration at 31 chars). Recovers 'Alyoum' -> 'Alyoum
        ExtraBold' and 'Asap … ExtraB' -> '… ExtraBold' without letting a bare
        stem like 'F' grab an unrelated 'Franklin …'. Shortest wins so the
        closest (usually upright/regular) face is chosen. Only reached after
        exact/normalized/optical-size all miss, so it never overrides a real
        match, and a name with no completion (a bitmap font) stays unresolved."""
        if not key:
            return None
        truncated = len(orig) >= 31
        best = None
        for k in (cls._normalized_path_map or {}):
            if len(k) <= len(key) or not k.startswith(key):
                continue
            if not (truncated or k[len(key)] == " "):
                continue
            if best is None or len(k) < len(best):
                best = k
        return best

    @classmethod
    def _lookup_path(cls, family: str) -> Optional[Tuple[str, int]]:
        """Exact, then normalized, then optical-size-stripped, then prefix."""
        if not cls._path_map:
            return None
        info = cls._path_map.get(family)
        if info is not None:
            return info
        normalized = cls._normalized_path_map or {}
        key = cls._normalize_name(family)
        info = normalized.get(key) if key else None
        if info is not None:
            return info
        # Windows names a variable font's instances with its optical-size axis
        # value ("Imbue 10pt Thin"), a token the file itself doesn't carry —
        # strip it and retry so the indexed instance alias ("Imbue Thin") wins.
        stripped = re.sub(r"\s*\b\d+\s*pt\b", "", family, flags=re.I).strip()
        if stripped and stripped != family:
            info = cls._path_map.get(stripped)
            if info is not None:
                return info
            k2 = cls._normalize_name(stripped)
            info = normalized.get(k2) if k2 else None
            if info is not None:
                return info
        pk = cls._prefix_key(key, family)
        return normalized.get(pk) if pk else None

    @classmethod
    def _load_pil_font(cls, family: str) -> Optional[Any]:
        """
        Load a PIL font for `family`, preferring the prebuilt path map so
        we actually find non-trivially-named files ("Segoe UI" →
        segoeui.ttf, "Arial Black" → ariblk.ttf, …). Falls back through
        a case/punctuation-agnostic lookup, then PIL's own best-effort
        name resolution.
        """
        path_info = cls._lookup_path(family)
        if path_info is not None:
            path, index = path_info
            try:
                return ImageFont.truetype(
                    path, cls._PROBE_SIZE, index=index
                )
            except (OSError, IOError, ValueError):
                pass
            except Exception:  # noqa: BLE001
                pass
        # Fallback: let PIL try to locate by family name (works for fonts
        # whose filename happens to match the family name).
        try:
            return ImageFont.truetype(family, cls._PROBE_SIZE)
        except (OSError, IOError, ValueError):
            return None
        except Exception:  # noqa: BLE001
            return None

    @classmethod
    def _missing_chars(cls, font: Any, chars: str) -> List[str]:
        """Given an already-loaded PIL font, the subset of `chars` it can't
        render (in the order given). Shared core of _supports_chars and
        unsupported_chars — load the font once, judge every char."""
        # Derive a tofu baseline from multiple high-PUA codepoints.
        # If they all render with the same bbox, that's the .notdef
        # glyph and we can use it to detect missing-glyph cases.
        # If they disagree (font has PUA content), skip the comparison
        # and fall back to width > 0.
        tofu_bbox: Optional[Tuple[int, int, int, int]] = None
        probe_bboxes = []
        for cp in cls._TOFU_PROBE_CODEPOINTS:
            try:
                b = font.getbbox(chr(cp))
                if b is not None and b[2] > b[0]:
                    probe_bboxes.append(b)
            except Exception:  # noqa: BLE001
                pass
        if len(probe_bboxes) >= 2 and all(
            b == probe_bboxes[0] for b in probe_bboxes
        ):
            tofu_bbox = probe_bboxes[0]

        missing: List[str] = []
        for ch in chars:
            try:
                bbox = font.getbbox(ch)
            except Exception:  # noqa: BLE001
                missing.append(ch)
                continue
            if bbox is None:
                missing.append(ch)
                continue
            width = bbox[2] - bbox[0]
            if width <= 0:
                missing.append(ch)
                continue
            if tofu_bbox is not None and bbox == tofu_bbox:
                missing.append(ch)
        return missing

    @classmethod
    def _missing_chars_ft(cls, family: str, chars: str) -> Optional[List[str]]:
        """Authoritative coverage via FreeType's cmap: the subset of `chars` the
        font maps to no glyph (glyph index 0). Returns None when FreeType is
        unavailable or the face can't be loaded, so the caller falls back to the
        PIL-getbbox heuristic. This is what fixes real Arabic/Latin fonts (Cairo,
        Tajawal, …) being misjudged as unsupported by the heuristic."""
        if _ft is None:
            return None
        info = cls.font_file(family)
        if info is None:
            return None
        path, index = info
        try:
            face = ft_face(path, index)
            return [c for c in chars if face.get_char_index(ord(c)) == 0]
        except Exception:  # noqa: BLE001
            return None

    # ---- Fontsize normalization (consistent visible size across fonts) -------
    # A diverse line of connected Arabic; its shaped ink concentrates in the
    # reading BODY band, which is what we measure. Latin uses cap-height ('H').
    _ARABIC_BODY_CORPUS = "لكن وعدت أن أتبناها سبحان الله وبحمده هذه جملة عربية طويلة"
    _BODY_EM = 200
    _size_ratio_cache: Dict[Tuple[str, str], Optional[float]] = {}

    @classmethod
    def size_ratio(cls, family: str, script: str) -> Optional[float]:
        """Visible reading-height / em for `family` in `script` ('arabic' |
        'latin'), cached. The em-square Fontsize alone renders very different
        visible sizes per font; dividing by this makes a size preset the same
        on-screen height in every font. Arabic = the body band of shaped running
        text (HarfBuzz+FreeType); Latin = cap-height (FreeType). Returns None when
        it can't be measured (a shaper/raster absent, font unresolved, glyphs
        missing) — the caller then uses that script's neutral reference (a preset
        stays one Fontsize for every font of that script, i.e. no per-font
        scaling). NOTE: the two scripts are NOT on the same scale — Arabic body
        (~0.2) and Latin cap (~0.7) each pair with their own reference constant."""
        key = (family, script)
        cached = cls._size_ratio_cache.get(key, _CACHE_MISSING)
        if cached is not _CACHE_MISSING:
            return cached
        val = cls._measure_size_ratio(family, script)
        cls._size_ratio_cache[key] = val
        return val

    @classmethod
    def _measure_size_ratio(cls, family: str, script: str) -> Optional[float]:
        info = cls.font_file(family)
        if info is None:
            return None
        path, index = info
        if script == "arabic":
            return cls._measure_arabic_body(path, index)
        if script == "latin":
            return cls._measure_cap_height(path, index)
        return None

    @classmethod
    def _measure_cap_height(cls, path: str, index: int) -> Optional[float]:
        """Latin cap-height ('H' ink rows) / em — a stable, standard size metric."""
        if _ft is None:
            return None
        EM = 1000
        try:
            face = ft_face(path, index)
            face.set_char_size(EM * 64)
            gi = face.get_char_index(ord("H"))
            if not gi:
                return None
            face.load_glyph(gi, _ft.FT_LOAD_RENDER)
            rows = face.glyph.bitmap.rows
            return rows / EM if rows else None
        except Exception:  # noqa: BLE001
            return None

    @classmethod
    def _measure_arabic_body(cls, path: str, index: int) -> Optional[float]:
        """Fraction of the em taken by the Arabic reading body — the tightest
        horizontal band holding half the ink of a shaped reference line. Isolated
        glyphs mis-measure this: a font like Scheherazade New has small letter
        bodies but long flourishes/deep bowls, so its isolated-glyph height reads
        big while its running text reads small. Shaping into connected form and
        taking the dense band ignores the sparse ascenders/descenders/flourishes
        and captures the body the eye actually reads. Needs HarfBuzz + FreeType;
        returns None if either is absent or the shape/raster fails."""
        if _ft is None or _hb is None:
            return None
        EM = cls._BODY_EM
        try:
            hbfont = _hb.Font(_hb.Face(_hb.Blob.from_file_path(path), index))
            hbfont.scale = (EM * 64, EM * 64)
            buf = _hb.Buffer()
            buf.add_str(cls._ARABIC_BODY_CORPUS)
            buf.direction, buf.script, buf.language = "rtl", "arab", "ar"
            _hb.shape(hbfont, buf)
            face = ft_face(path, index)
            face.set_pixel_sizes(0, EM)
        except Exception:  # noqa: BLE001
            return None
        height = EM * 4
        baseline = int(EM * 2.6)
        prof = [0.0] * height          # ink per pixel-row across the whole line
        try:
            for info, gpos in zip(buf.glyph_infos, buf.glyph_positions):
                try:
                    face.load_glyph(info.codepoint, _ft.FT_LOAD_RENDER)
                except Exception:  # noqa: BLE001
                    continue
                bmp = face.glyph.bitmap
                rows, width, pitch = bmp.rows, bmp.width, bmp.pitch
                if not rows or not width:
                    continue
                data = bmp.buffer
                top = baseline - int(gpos.y_offset / 64) - face.glyph.bitmap_top
                for r in range(rows):
                    y = top + r
                    if 0 <= y < height:
                        start = r * pitch
                        prof[y] += sum(data[start:start + width])
        except Exception:  # noqa: BLE001
            return None
        return cls._tightest_ink_band(prof, 0.5)

    @staticmethod
    def _tightest_ink_band(prof: List[float], frac: float) -> Optional[float]:
        """Height (as a fraction of _BODY_EM) of the shortest contiguous row band
        that holds `frac` of the profile's total ink — the dense reading body."""
        total = sum(prof)
        if total <= 0:
            return None
        target = total * frac
        n = len(prof)
        best = n
        left = 0
        run = 0.0
        for right in range(n):
            run += prof[right]
            while run >= target:
                if right - left + 1 < best:
                    best = right - left + 1
                run -= prof[left]
                left += 1
        return best / FontSupport._BODY_EM

    @classmethod
    def _supports_chars(cls, family: str, chars: str) -> Optional[bool]:
        """Best-effort: does this font render every char in `chars`?

        Returns True/False when the font could be loaded and judged, or None
        when the font itself could not be loaded (a transient/unresolved load
        failure, NOT a definitive "unsupported"). Callers must not cache a None
        as a permanent "unsupported" verdict.
        """
        if not chars:
            return True
        ft_missing = cls._missing_chars_ft(family, chars)
        if ft_missing is not None:
            return not ft_missing
        font = cls._load_pil_font(family)
        if font is None:
            return None
        return not cls._missing_chars(font, chars)

    # A spread of common Latin letters. A font with a real Latin cmap renders
    # (nearly) all of them; an Arabic-only font renders NONE. We require only a
    # majority so the odd coincidental tofu-bbox collision (a narrow cap can
    # share the .notdef box) doesn't misjudge a genuine Latin font.
    _LATIN_PROBE = "AaBbEeGgMmRrSsWw"

    @classmethod
    def supports_latin(cls, family: str) -> Optional[bool]:
        """Best-effort: does this font actually render basic Latin letters?

        The coverage tests skip ASCII (assuming every font has it), but
        Arabic-only fonts genuinely lack Latin glyphs — the specimen's Latin
        pangram needs the real answer so it isn't drawn as tofu. Returns
        True/False when judged, or None when the font couldn't be loaded.
        """
        probe = cls._LATIN_PROBE
        ft_missing = cls._missing_chars_ft(family, probe)
        if ft_missing is not None:
            return (len(probe) - len(ft_missing)) >= len(probe) // 2
        font = cls._load_pil_font(family)
        if font is None:
            return None
        missing = cls._missing_chars(font, probe)
        return (len(probe) - len(missing)) >= len(probe) // 2

    @classmethod
    def unsupported_chars(cls, family: str, text: str) -> List[str]:
        """The distinct renderable non-ASCII characters in `text` that
        `family` cannot render (sorted). Empty when the font renders them all
        OR could not be loaded to judge — a load failure is 'unknown', never
        reported as unsupported (mirrors _supports_chars' None contract, so a
        transient resolve miss can't produce a spurious 'missing glyphs')."""
        key = cls.test_key(text)
        if not key:
            return []
        ft_missing = cls._missing_chars_ft(family, key)
        if ft_missing is not None:
            return ft_missing
        font = cls._load_pil_font(family)
        if font is None:
            return []
        return cls._missing_chars(font, key)

    @classmethod
    def filter_supporting(
        cls,
        families: List[str],
        preview_text: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
        cancel: Optional[threading.Event] = None,
        on_scan_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[str]:
        """
        Return the subset of `families` that renders `preview_text`.

        Threading: safe to call from any thread. Progress callbacks run
        on the caller's thread — hop back to the UI thread yourself
        with `root.after` if you plan to touch widgets.

        `on_scan_progress(done, total)` fires during the initial
        font-file scan that builds the family→path map (once per
        Fonts-dir fingerprint).
        """
        cls._load_cache()
        key = cls.test_key(preview_text)
        if not key:
            return list(families)

        # Build the family→path map up-front so every coverage test
        # hands PIL an absolute font-file path (this is what fixes
        # "Arial / Segoe UI / Tahoma don't pass the filter" — their
        # filenames don't match their family names, and PIL's own
        # name lookup can't find them otherwise).
        cls._get_path_map(
            on_scan_progress=on_scan_progress, cancel=cancel,
        )
        if cancel is not None and cancel.is_set():
            return list(families)

        with cls._lock:
            entry = cls._cache.setdefault(key, {})

        result: List[str] = []
        total = len(families)
        newly_checked = 0
        for i, family in enumerate(families):
            if cancel is not None and cancel.is_set():
                break
            if on_progress is not None:
                try:
                    on_progress(i, total)
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"font filter progress cb failed: {e}")

            with cls._lock:
                cached = entry.get(family, _CACHE_MISSING)

            if cached is _CACHE_MISSING:
                verdict = cls._supports_chars(family, key)
                if verdict is None:
                    # Font couldn't be loaded this run (transient/unresolved):
                    # treat as unsupported for now but DON'T persist it, so a
                    # later run can re-judge instead of dropping it forever.
                    supported = False
                else:
                    supported = verdict
                    with cls._lock:
                        entry[family] = verdict
                    newly_checked += 1
            else:
                supported = bool(cached)

            if supported:
                result.append(family)

        if on_progress is not None:
            try:
                on_progress(total, total)
            except Exception:  # noqa: BLE001
                pass

        if newly_checked > 0:
            cls._save_cache()

        return result


def _next_font_index(
    fonts: List[str],
    current: str,
    delta: int,
) -> int:
    """
    Pure helper for GUI._cycle_font. Given the font list, the entry's current
    text, and a step direction, return the index of the next font to display.
    Returns -1 if the font list is empty.

    Exact match wins; anything else (blank / partial / unknown font) seeds the
    cycle from one end of the list so the first scroll lands somewhere useful.
    """
    if not fonts:
        return -1
    current_lower = current.strip().lower()
    idx = -1
    if current_lower:
        for i, name in enumerate(fonts):
            if name.lower() == current_lower:
                idx = i
                break
    if idx == -1:
        return 0 if delta >= 0 else len(fonts) - 1
    return (idx + delta) % len(fonts)
