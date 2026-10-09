"""The ASS/SSA file engine: decode, parse/validate, compute/apply styling,
backup/restore, idempotency check, and embedded-subtitle extraction."""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Optional, Dict, List, Any, Set, Tuple, Callable

from .runtime import *
from .runtime import (
    chardet, file_hash, _subprocess_creation_flags, _TEXT_SUBTITLE_SUFFIXES,
    _BOM_PREFIXED_ENCODINGS, _warn_chardet_missing,
)
from .db import DatabaseManager, get_db_connection
from .fonts import FontSupport
from .media import FFmpegLocator

__all__ = ["FileProcessor"]

# MKVs usually tag subtitle tracks with ISO 639-2/B codes, while the language
# picker (media.COMMON_LANGS) offers the /T spelling; fold B onto T to compare.
_LANG_ALIASES: Dict[str, str] = {"fre": "fra", "ger": "deu", "chi": "zho"}



# ----------------------------------------
# File Processing
# ----------------------------------------
class FileProcessor:
    """Process ASS subtitle files and extract embedded subtitles."""

    @staticmethod
    def list_ass_files(folder_path: str) -> List[Path]:
        """List all ASS files in folder, excluding backups."""
        base = Path(folder_path)
        # Resolve the base ONCE. Calling validate_path(fp, base) per file
        # re-resolves the base inside every call — a redundant syscall x N files
        # x every monitor tick — so inline the same containment check (resolve
        # each fp, confirm it's under the pre-resolved base) against the base we
        # already resolved. Resolving fp still guards against an in-tree symlink
        # pointing outside, exactly as validate_path did.
        try:
            resolved_base = base.resolve()
        except OSError:
            return []
        out: List[Path] = []
        for fp in base.rglob("*.ass"):
            if "Original Subs" in fp.parts:
                continue
            # A crash can orphan our own atomic-write/backup temps (named
            # .tmp_XXXX.ass); they are never real subtitles.
            if fp.name.startswith((".tmp_", ".bak_")):
                continue
            try:
                fp.resolve().relative_to(resolved_base)
            except (OSError, ValueError):
                continue
            out.append(fp)
        return out

    # The app's own temp files: atomic writes (.tmp_XXXX.ass), extraction
    # (.tmp_XXXX.srt/...), restore copies (.tmp_XXXX.tmp) and backups
    # (.bak_XXXX.tmp, .bak_<pid>_<ns>.tmp).
    _ORPHAN_TEMP = re.compile(
        r"^\.(?:tmp|bak)_[a-z0-9_]{6,40}\.(?:ass|ssa|srt|sup|vtt|tmp)$", re.IGNORECASE)

    @staticmethod
    def cleanup_orphan_temps(folder: str, min_age: float = 600.0) -> int:
        """Delete temp files of ours that a crash or power cut left anywhere
        under `folder` (a styled ".tmp_XXXX.ass" copy is visible to players and
        media servers). Only files older than `min_age` seconds: a write in
        progress takes milliseconds. Links are not followed."""
        removed = 0
        cutoff = time.time() - min_age
        for dirpath, _dirs, names in os.walk(folder, followlinks=False):
            for name in names:
                if not FileProcessor._ORPHAN_TEMP.match(name):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    if os.path.getmtime(path) >= cutoff:
                        continue
                    os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                    os.unlink(path)
                    removed += 1
                except OSError as e:
                    logger.debug(f"Could not remove leftover {path}: {e}")
        if removed:
            logger.info(f"Removed {removed} leftover temp file(s) under {folder}")
        return removed

    @staticmethod
    def file_hash(file_path: Path) -> Optional[str]:
        """Cheap content fingerprint (sha1 hex) of a subtitle file, or None on
        error / oversized file. Used to skip a full decode+parse when an mtime
        change leaves the bytes unchanged.

        Delegates to runtime.file_hash (the single implementation the db layer
        also uses) so the two can't drift, and so this module doesn't depend on
        `hashlib` leaking through `from .runtime import *`."""
        return file_hash(file_path)

    # chardet names -> the Python codec we decode with. Latin-1 is read as
    # cp1252 (its printable superset, as browsers do); GB2312 as GB18030.
    _CODEC_ALIASES: Dict[str, str] = {
        "iso-8859-1": "cp1252", "windows-1252": "cp1252", "ascii": "cp1252",
        "gb2312": "gb18030", "shift-jis": "shift_jis", "windows-1251": "cp1251",
        "windows-1256": "cp1256", "cp932": "cp932", "euc-kr": "euc-kr",
    }
    _MULTIBYTE_GUESSES = frozenset({
        "shift_jis", "cp932", "euc-jp", "iso-2022-jp", "gb18030", "hz-gb-2312",
        "big5", "euc-tw", "euc-kr", "cp949", "johab", "iso-2022-kr",
    })
    # Single-byte scripts chardet 5 recognises reliably; trusted only when
    # very sure, and BEFORE the Arabic check (cp1256 decodes Cyrillic bytes as
    # Arabic-looking letters).
    _NON_LATIN_SINGLE_BYTE = frozenset({
        "cp1251", "koi8-r", "iso-8859-5", "ibm866", "ibm855", "maccyrillic",
        "windows-1253", "iso-8859-7", "tis-620",
    })

    @staticmethod
    def _arabic_share(raw: bytes) -> float:
        """Share of the letters among the non-ASCII characters that are Arabic
        when `raw` is read as cp1256 (0.0 when it doesn't decode)."""
        try:
            text = raw.decode("cp1256")
        except UnicodeDecodeError:
            return 0.0
        letters = [c for c in text if ord(c) > 0x7F and c.isalpha()]
        if not letters:
            return 0.0
        return sum(1 for c in letters if 0x0600 <= ord(c) <= 0x06FF) / len(letters)

    @staticmethod
    def _japanese_share(raw: bytes) -> float:
        """Share of the non-ASCII characters that are full-width Japanese
        (kana, kanji, CJK punctuation) when `raw` is read as Shift-JIS. Arabic
        cp1256 bytes read as Shift-JIS turn into HALF-width katakana, which
        don't count, so real Japanese scores ~1.0 and Arabic far less."""
        for codec in ("shift_jis", "cp932"):
            try:
                text = raw.decode(codec)
                break
            except UnicodeDecodeError:
                continue
        else:
            return 0.0
        chars = [c for c in text if ord(c) > 0x7F]
        if not chars:
            return 0.0
        def full_width_japanese(c: str) -> bool:
            o = ord(c)
            return (0x3000 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF
                    or 0xFF01 <= o <= 0xFF5E)
        return sum(1 for c in chars if full_width_japanese(c)) / len(chars)

    @staticmethod
    def _legacy_candidates(raw: bytes) -> List[str]:
        """Ordered codecs to try for a no-BOM, non-UTF-8 subtitle.

        chardet judges only the lines that contain non-ASCII bytes (the ASCII
        [Script Info]/[V4+ Styles] header otherwise dilutes its confidence).
        The app pins chardet 5.x, which has no Arabic model and calls cp1256
        Arabic "MacRoman"/"MacCyrillic", so Arabic gets a direct check:

        1. a multibyte CJK guess at >= 0.8;
        2. a non-Latin single-byte guess (Cyrillic, Greek, Thai) at >= 0.9;
        3. Shift-JIS when the text reads as full-width Japanese better than it
           reads as Arabic (one-line files give chardet too little to go on),
           else cp1256 when most non-ASCII letters it yields are Arabic;
        4. any other guess at >= 0.7, except the Mac code pages;
        then the caller appends SUPPORTED_ENCODINGS (cp1256 first)."""
        guess, confidence = None, 0.0
        if chardet:
            try:
                sample = b"\n".join(
                    line for line in raw[:262144].split(b"\n")
                    if any(b >= 0x80 for b in line)
                )[:65536]
                result = chardet.detect(sample or raw[:65536])
                name = (result.get("encoding") or "").lower().replace("_", "-")
                guess = FileProcessor._CODEC_ALIASES.get(name, name) or None
                confidence = result.get("confidence", 0) or 0
            except Exception as e:  # noqa: BLE001
                logger.debug(f"chardet failed: {e}")
        out: List[str] = []
        if guess in FileProcessor._MULTIBYTE_GUESSES and confidence >= 0.8:
            out.append(guess)
        elif guess in FileProcessor._NON_LATIN_SINGLE_BYTE and confidence >= 0.9:
            out.append(guess)
        arabic = FileProcessor._arabic_share(raw)
        japanese = FileProcessor._japanese_share(raw)
        if japanese >= 0.9 and japanese > arabic:
            out += ["shift_jis", "cp932"]
        elif arabic >= 0.6:
            out.append("cp1256")
        if (guess and guess not in out and confidence >= 0.7
                and not guess.startswith("mac") and guess != "utf-8"):
            out.append(guess)
        return out

    @staticmethod
    def _decode_bytes(raw: bytes) -> Optional[Tuple[str, str]]:
        """
        Decode raw file bytes using strict decoding only — never produces
        replacement characters, so we never round-trip garbage back to disk.

        Returns (encoding, text) or None if no candidate worked.
        """
        # BOM dispatch. Decode with the EXPLICIT-endian codec (after stripping
        # the BOM bytes) so the file's real byte order is captured and survives
        # the round-trip — the generic "utf-16"/"utf-32" encoders always emit
        # little-endian, which silently flipped big-endian files to LE on write.
        # atomic_write re-emits the BOM for these explicit names. utf-8-sig
        # handles its own BOM on both decode and encode.
        # utf-32 must be tried before utf-16 because utf-32-LE starts with the
        # same two bytes as utf-16-LE.
        bom_table = (
            (b"\x00\x00\xfe\xff", "utf-32-be", True),
            (b"\xff\xfe\x00\x00", "utf-32-le", True),
            (b"\xef\xbb\xbf",     "utf-8-sig", False),
            (b"\xff\xfe",         "utf-16-le", True),
            (b"\xfe\xff",         "utf-16-be", True),
        )
        for bom, enc, strip_bom in bom_table:
            if raw.startswith(bom):
                try:
                    payload = raw[len(bom):] if strip_bom else raw
                    return enc, payload.decode(enc)
                except UnicodeDecodeError:
                    # BOM lied; keep going.
                    break

        # Subtitle text never contains NUL. Without a BOM, NULs mean UTF-16/32
        # — which also "decodes" as UTF-8 (NUL is valid there) and would be
        # rewritten as a mixed-encoding file. Refuse rather than corrupt it.
        if b"\x00" in raw:
            return None

        # Fast path: ASCII-only or valid UTF-8.
        try:
            return "utf-8", raw.decode("utf-8")
        except UnicodeDecodeError:
            pass

        # Build the candidate list (see _legacy_candidates), then our ordered
        # fallbacks.
        if not chardet:
            # Without chardet, detection of no-BOM legacy encodings degrades to
            # the Arabic check plus "first codec that decodes wins". Surface
            # the degradation so it's diagnosable; chardet is a declared
            # dependency and should normally be present.
            _warn_chardet_missing()
        candidates: List[str] = FileProcessor._legacy_candidates(raw)

        for enc in SUPPORTED_ENCODINGS:
            if enc not in candidates and enc != "utf-8":
                candidates.append(enc)

        # Try each strictly. First one that decodes without error AND
        # without replacement characters wins.
        for enc in candidates:
            try:
                text = raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
            if "\ufffd" in text:
                # Strict decode but replacement char is present — happens
                # with codepages that accept any byte (e.g. cp1252) but
                # the content is really something else.
                continue
            return enc, text

        return None

    @staticmethod
    def read_text(file_path: Path) -> Optional[Tuple[str, List[str]]]:
        """
        Read an ASS file and return (encoding, lines). Returns None if the
        file can't be decoded with any known encoding — callers must skip
        those files instead of corrupting them.
        """
        try:
            # A real subtitle file is kilobytes. Reject anything absurd before
            # reading it whole into memory (and re-decoding the whole buffer per
            # candidate encoding) — the unattended monitor would otherwise thrash
            # on a malformed/hostile multi-GB file dropped into the folder.
            if file_path.stat().st_size > MAX_ASS_FILE_SIZE:
                record_error(
                    "read", file_path.name,
                    f"File too large to be a subtitle ({file_path.stat().st_size} bytes); skipped",
                )
                return None
            with open(file_path, "rb") as f:
                raw = f.read()
        except OSError as e:
            record_error("read", file_path.name, str(e))
            return None

        decoded = FileProcessor._decode_bytes(raw)
        if decoded is None:
            record_error(
                "decode",
                file_path.name,
                "No supported encoding matched this file (tried utf-8, "
                "cp1256, cp1252, shift_jis, …)",
            )
            return None

        encoding, text = decoded
        # Split on real ASS line terminators only (preserving them), NOT on the
        # extra separators str.splitlines() treats as breaks (U+2028/2029/NEL/
        # VT/FF) — those can occur inside a Dialogue Text field.
        return encoding, splitlines_keepends_ass(text)

    # Sanity cap for adaptive scaling — absurdly large PlayResY values
    # (malformed or malicious) must not explode outline/shadow sizes.
    _MAX_SANE_PLAYRES_Y = 4320  # 8K; anything above is ignored.

    @staticmethod
    def _extract_resolution_from_lines(
        lines: List[str],
        source_name: str = "",
    ) -> Tuple[Optional[int], Optional[int], Optional[bool]]:
        """Scan parsed lines for PlayResX / PlayResY / ScaledBorderAndShadow.

        The third element is True for an explicit "yes", False for any other
        explicit value, and None when the header is absent (renderer defaults
        for the absent case disagree, which is why apply pins it).
        """
        playres_x = playres_y = None
        scaled_border: Optional[bool] = None
        for line in lines:
            if playres_x is None:
                match = PLAYRES_X_PATTERN.search(line)
                if match:
                    try:
                        playres_x = int(match.group(1))
                    except ValueError:
                        pass
            if playres_y is None:
                match = PLAYRES_Y_PATTERN.search(line)
                if match:
                    try:
                        playres_y = int(match.group(1))
                    except ValueError:
                        pass
            if scaled_border is None:
                match = SCALED_BORDER_PATTERN.search(line)
                if match:
                    scaled_border = match.group(1).lower() == "yes"
            if playres_x and playres_y and scaled_border is not None:
                break

        # Clamp absurd values.
        if playres_y and playres_y > FileProcessor._MAX_SANE_PLAYRES_Y:
            logger.warning(
                f"Ignoring implausible PlayResY={playres_y} in {source_name}"
            )
            playres_y = None
        if playres_x and playres_x > FileProcessor._MAX_SANE_PLAYRES_Y * 2:
            playres_x = None

        if playres_x and playres_y and source_name:
            logger.info(
                f"get_resolution: {source_name} -> "
                f"PlayResX={playres_x}, PlayResY={playres_y}"
            )
        return playres_x, playres_y, scaled_border

    # Script-space height when the header omits it: the spec/VSFilter default
    # canvas is 384×288, and with only PlayResX present VSFilter derives the
    # height as X × 3/4. Pretending 720 here (the old behavior) made
    # header-less files render ~2.5× thicker borders than intended.
    _DEFAULT_PLAYRES_Y = 288

    @staticmethod
    def _effective_playres_y(
        playres_x: Optional[int], playres_y: Optional[int]
    ) -> int:
        """The script-space height a renderer would actually use."""
        if playres_y:
            return playres_y
        if playres_x:
            # libass and VSFilter special-case 1280 (a 1280×1024 canvas).
            return 1024 if playres_x == 1280 else round(playres_x * 3 / 4)
        return FileProcessor._DEFAULT_PLAYRES_Y

    @staticmethod
    def _size_basis_height(
        playres_x: Optional[int], playres_y: Optional[int]
    ) -> int:
        """Height (script units) to size the dialogue Fontsize against, so the
        on-screen size stays CONSTANT across aspect ratios on a 16:9 display.

        A wider-than-16:9 (scope/cinemascope) script is letterboxed on a 16:9
        screen, so its content occupies less vertical space and a Fontsize scaled
        to PlayResY renders physically smaller. Sizing against the 16:9-EQUIVALENT
        frame height (PlayResX·9/16) instead compensates for the letterbox, so a
        2.35:1 file and a 16:9 file end up the same visual size. 16:9 and taller
        (e.g. 4:3, which pillarboxes and fills the height) keep PlayResY.
        """
        if playres_x and playres_y:
            return max(playres_y, round(playres_x * 9 / 16))
        # PlayResX-only: fall through to the renderer's OWN derived height
        # (PlayResX*3/4). That is correct for the common X-only case (a 16:9 or
        # 4:3 video — the renderer squashes/fits to that same derived height, so
        # a 16:9-equivalent basis would actually mis-size it). An X-only file on
        # a scope video is malformed and can't be compensated via Fontsize.
        return FileProcessor._effective_playres_y(playres_x, playres_y)

    @staticmethod
    def _size_targets(
        playres_x: Optional[int],
        playres_y: Optional[int],
        size: str,
        font_name: str = "",
        script: str = "latin",
        nudge: float = 1.0,
    ) -> Tuple[int, int, Optional[int]]:
        """Shared dialogue-sizing math for compute_changes and is_already_styled.

        Returns (adaptive_basis, size_basis, target_fontsize):
        • adaptive_basis — the height outline/shadow scale against (16:9-equiv
          with a preset, content-relative without). NOT font-normalized: it is
          frame-relative, and since the glyphs are now a consistent visible
          height across fonts, a frame-relative border is consistent too.
        • size_basis — the 16:9-equivalent height the Fontsize preset is a
          fraction of (also used for the log line).
        • target_fontsize — the rounded Fontsize for the active preset, or None
          for size='none'. Normalized by the font's visible letter-height ratio
          (`font_name`, `script`) so a preset is the SAME on-screen height in
          every font — the em-square Fontsize alone renders very differently per
          font. A font whose letters fill more of the em gets a smaller Fontsize;
          REFERENCE_SIZE_RATIO anchors a typical font to its pre-normalization
          size, and both scripts share it so Arabic and Latin also match.

        BOTH callers must derive their targets from here (and pass the same
        font_name + script + nudge): the write path and the idempotency check
        disagreeing would re-process a file on every scan.
        """
        eff_y = FileProcessor._effective_playres_y(playres_x, playres_y)
        size_frac = DIALOGUE_SIZE_FRACTIONS.get(size)
        size_basis = FileProcessor._size_basis_height(playres_x, playres_y)
        adaptive_basis = size_basis if size_frac else eff_y
        if size_frac:
            # Each script has its own reading metric + anchor (they're not on the
            # same scale): Arabic = body-band, Latin/other = cap-height. An
            # unmeasurable font (ratio None) falls back to its anchor, so the
            # preset stays one Fontsize for every font of that script.
            reference = (
                REFERENCE_SIZE_RATIO_ARABIC if script == "arabic"
                else REFERENCE_SIZE_RATIO
            )
            ratio = FontSupport.size_ratio(font_name, script) or reference
            # `nudge` is the user's per-font correction (1.0 = none). It's a
            # grid-snapped constant, so it adds no nondeterminism: both callers
            # pass the same value and land on the same rounded Fontsize.
            target_fontsize = round(size_frac * size_basis * reference / ratio * nudge)
        else:
            target_fontsize = None
        return adaptive_basis, size_basis, target_fontsize

    @staticmethod
    def _line_ending(line: str) -> str:
        """The line's own terminator, or "\\n" when it has none."""
        return line[len(line.rstrip("\r\n")):] or "\n"

    @staticmethod
    def _field_map_from_format_fields(fields: List[str]) -> Dict[str, int]:
        """Map normalized field name (lowercase, stripped) to its column index."""
        return {name.strip().lower(): idx for idx, name in enumerate(fields) if name.strip()}

    @staticmethod
    def _canonical_field_map() -> Dict[str, int]:
        return FileProcessor._field_map_from_format_fields(list(ASSStyleFields.FIELD_NAMES))

    @staticmethod
    def _resolve_style_field_map(
        fields: List[str],
    ) -> Tuple[Dict[str, int], int, bool]:
        """Decide the Style field map from a Format line's field names.

        Single source of truth shared by validate_ass_lines and
        is_already_styled so the two can never disagree on the same file (they
        used to: validate repaired a broken Format line while is_already_styled
        trusted it verbatim). Honours the file's own order/count when it is
        well-formed (every field named) AND declares every field we write; falls
        back to the canonical 23-field ASS V4+ layout otherwise.

        Returns (field_map, field_count, used_file_order).
        """
        have = {f.strip().lower() for f in fields}
        if all(f.strip() for f in fields) and set(
            FileProcessor._REQUIRED_STYLE_FIELDS
        ).issubset(have):
            return (
                FileProcessor._field_map_from_format_fields(fields),
                len(fields),
                True,
            )
        canonical = list(ASSStyleFields.FIELD_NAMES)
        return (
            FileProcessor._field_map_from_format_fields(canonical),
            len(canonical),
            False,
        )

    @staticmethod
    def _is_ssa_v4(lines: List[str]) -> bool:
        """True if `lines` is a genuine SubStation Alpha v4 file.

        SSA v4 uses a `[V4 Styles]` header (NOT ASS's `[V4+ Styles]`) and a
        different Style field set: it has TertiaryColour/AlphaLevel and has NO
        Underline/StrikeOut/ScaleX/etc. Rewriting such a file into the 23-column
        ASS V4+ layout scrambles every column, so callers must detect and skip
        it rather than "repair" it.
        """
        in_v4 = False  # specifically [V4 Styles], not [V4+ Styles]
        for line in lines:
            sl = line.lstrip().lower()
            if sl.startswith("[v4+ styles]"):
                in_v4 = False
                continue
            if sl.startswith("[v4 styles]"):
                in_v4 = True
                continue
            if in_v4 and sl.startswith("["):
                in_v4 = False
            if in_v4 and sl.startswith("format:"):
                have = {
                    f.strip().lower()
                    for f in line.split(":", 1)[1].split(",")
                }
                if (
                    "tertiarycolour" in have
                    or "alphalevel" in have
                    or "underline" not in have
                ):
                    return True
        return False

    # VSFilter's defaults for a Style field (Aegisub's "Default").
    _STYLE_FIELD_DEFAULTS: Dict[str, str] = {
        "name": "Default", "fontname": "Arial", "fontsize": "20",
        "primarycolour": "&H00FFFFFF", "secondarycolour": "&H000000FF",
        "outlinecolour": "&H00000000", "backcolour": "&H00000000",
        "bold": "0", "italic": "0", "underline": "0", "strikeout": "0",
        "scalex": "100", "scaley": "100", "spacing": "0", "angle": "0",
        "borderstyle": "1", "outline": "2", "shadow": "2", "alignment": "2",
        "marginl": "10", "marginr": "10", "marginv": "10", "encoding": "1",
    }

    @staticmethod
    def validate_ass_lines(
        lines: List[str],
        file_path: Path,
    ) -> Tuple[List[str], Dict[str, int], int, bool]:
        """
        Walk the [V4+ Styles] section and repair broken Format:/Style: rows.

        Returns (possibly-modified lines, field_map, field_count, dirty).
        The field_map uses the file's actual Format: field order when it's
        well-formed; falls back to canonical order otherwise.
        """
        new_lines: List[str] = []
        field_map: Optional[Dict[str, int]] = None
        field_count = 0
        in_styles = False
        dirty = False

        for line in lines:
            stripped_lower = line.lstrip().lower()

            if stripped_lower.startswith("[v4+ styles]") or stripped_lower.startswith("[v4 styles]"):
                in_styles = True
                new_lines.append(line)
                continue

            if in_styles and line.lstrip().startswith("["):
                in_styles = False

            if in_styles:
                if stripped_lower.startswith("format:"):
                    fields = [
                        fld.strip()
                        for fld in line.split(":", 1)[1].split(",")
                    ]
                    # Honour the file's own Format order/count whenever it is
                    # well-formed (all field names present) AND declares every
                    # style field we actually write — even if the count isn't the
                    # canonical 23 (legacy/SSA-derived .ass files exist in the
                    # wild). Only fall back to canonical when the line is broken
                    # or missing required fields, so we never mangle a valid but
                    # non-canonical section.
                    field_map, field_count, used_file_order = (
                        FileProcessor._resolve_style_field_map(fields)
                    )
                    if not used_file_order:
                        logger.warning(
                            f"Invalid Format line in {file_path} "
                            f"({len(fields)} fields, expected "
                            f"{ASSStyleFields.FIELD_COUNT}): {line.strip()}"
                        )
                        line = "Format: " + ", ".join(
                            ASSStyleFields.FIELD_NAMES
                        ) + FileProcessor._line_ending(line)
                        dirty = True

                elif stripped_lower.startswith("style:"):
                    if field_map is None:
                        canonical = list(ASSStyleFields.FIELD_NAMES)
                        field_map = FileProcessor._field_map_from_format_fields(canonical)
                        field_count = len(canonical)

                    prefix, fields_str = line.split(":", 1)
                    eol = line[len(line.rstrip("\r\n")):]
                    parts = fields_str.rstrip("\r\n").split(",")

                    if len(parts) != field_count:
                        if len(parts) < field_count:
                            # Fill what the row leaves out with VSFilter's
                            # defaults (what PotPlayer shows for the original).
                            # Empty fields made libass read ScaleX/ScaleY as 0,
                            # so the style's dialogue vanished in mpv.
                            names = {idx: name for name, idx in field_map.items()}
                            parts.extend(
                                FileProcessor._STYLE_FIELD_DEFAULTS.get(names.get(i, ""), "")
                                for i in range(len(parts), field_count)
                            )
                        else:
                            parts = parts[:field_count]
                        line = f"{prefix}:{','.join(parts)}" + eol
                        dirty = True

            new_lines.append(line)

        if field_map is None:
            field_map = FileProcessor._canonical_field_map()
            field_count = len(ASSStyleFields.FIELD_NAMES)

        return new_lines, field_map, field_count, dirty

    @staticmethod
    def _compute_adaptive_sizes(res_y: Optional[int]) -> Tuple[float, float]:
        """Compute outline/shadow sizes for a given PlayResY."""
        if res_y and res_y > 0:
            scale = res_y / REFERENCE_RES_Y
            return (
                round(BASE_OUTLINE_SIZE * scale, 2),
                round(BASE_SHADOW_SIZE * scale, 2),
            )
        return BASE_OUTLINE_SIZE, BASE_SHADOW_SIZE

    @staticmethod
    def enumerate_backup_groups(
        folder: Path,
    ) -> List[Tuple[Path, List[Path]]]:
        """
        Find every `Original Subs/` directory under `folder` and list the
        backup files inside. Returns pairs of (parent_folder, backup_files).

        The parent folder (e.g. an individual anime series) is what the
        user picks from in the restore dialog — one entry per anime.
        """
        groups: List[Tuple[Path, List[Path]]] = []
        try:
            for orig_dir in folder.rglob("Original Subs"):
                if not orig_dir.is_dir():
                    continue
                try:
                    backups = sorted({
                        b
                        for pat in BACKUP_SUBTITLE_GLOBS
                        for b in orig_dir.glob(pat)
                    })
                except OSError as e:
                    logger.warning(f"Cannot list {orig_dir}: {e}")
                    continue
                if backups:
                    groups.append((orig_dir.parent, backups))
        except Exception as e:  # noqa: BLE001
            logger.error(f"Failed to enumerate backups under {folder}: {e}")
        # Sort by series folder name for a stable, browsable list.
        groups.sort(key=lambda g: g[0].name.lower())
        return groups

    @staticmethod
    def _atomic_copy_file(src: Path, dst: Path, tmp_prefix: str = ".tmp_") -> None:
        """Copy `src` onto `dst` atomically: copy to a temp in dst's directory,
        fsync it, then os.replace onto dst.

        Mirrors atomic_write's durability so an interrupted copy (power loss,
        disk-full, AV lock, mapped-drive drop) can NEVER leave dst torn — the
        point both when dst is the user's ONLY live file (restore_backups) and
        when a crash-orphaned temp must not masquerade as a valid backup
        (copy_original_file). suffix=".tmp" (NOT .ass) so an orphan isn't globbed
        as a restorable backup; prefix ".tmp_"/".bak_" is what the monitor's
        watchdog filter ignores.
        """
        fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=tmp_prefix, suffix=".tmp")
        os.close(fd)
        try:
            # Content + timestamps only. copy2 would also copy a read-only
            # attribute onto the temp, which then can't be fsync'd or deleted.
            shutil.copyfile(src, tmp)
            st = os.stat(src)
            os.utime(tmp, (st.st_atime, st.st_mtime))
            # fsync the copied bytes before the rename. Use r+b: a read-only
            # handle can't be fsync'd on Windows ("Bad file descriptor").
            with open(tmp, "r+b") as _f:
                _f.flush()
                os.fsync(_f.fileno())
            os.replace(tmp, dst)
            tmp = None
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.chmod(tmp, stat.S_IWRITE | stat.S_IREAD)
                    os.unlink(tmp)
                except OSError:
                    pass

    @staticmethod
    def copy_original_file(file_path: Path) -> bool:
        """
        Create backup of original file.

        Args:
            file_path: Path to file to backup

        Returns:
            True if backup was created or already exists
        """
        try:
            # An empty file has nothing to preserve, and an empty "original"
            # would be kept forever by the write-once rule (Restore then refuses
            # it). Typical cause: a half-finished extraction or download.
            if file_path.stat().st_size == 0:
                record_error("backup", file_path.name,
                             "the file is empty; nothing to back up")
                return False
            orig_dir = file_path.parent / "Original Subs"
            orig_dir.mkdir(exist_ok=True)
            dest = orig_dir / file_path.name

            if not dest.exists():
                # Atomic backup (temp + fsync + os.replace). A bare shutil copy is
                # create-then-stream, so a crash/disk-full mid-copy would leave a
                # truncated "original" that the dest.exists() guard then treats as
                # a valid backup forever (the real data-loss case). prefix=".bak_"
                # keeps a crash-orphaned temp out of the backup globs + watchdog.
                FileProcessor._atomic_copy_file(file_path, dest, tmp_prefix=".bak_")
                logger.info(f"Copied original to {dest}")

            return True
        except Exception as e:  # noqa: BLE001 — every failure must reach Problems
            # Without a backup the file is left unstyled; say so in the Problems
            # panel instead of only the log (Controlled Folder Access, a full
            # disk, permissions and AV locks all land here).
            record_error("backup", file_path.name, f"could not back up the original: {e}")
            return False

    @staticmethod
    def restore_backups(
        pairs: List[Tuple[Path, Path]],
        on_each: Optional[Callable[[int, int, Path], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> Tuple[List[Path], List[Tuple[Path, str]]]:
        """
        Copy each (backup, target) pair back over its live file.

        Pure file I/O — no GUI, no DB — so the data-loss-critical restore loop
        can be unit-tested in isolation. Returns (restored_targets, failures)
        where failures is a list of (target, reason). on_each(done, total,
        target), if given, is called before each copy for progress reporting.
        should_stop(), if given, is checked before each item — when it returns
        True the loop stops cleanly (e.g. on app shutdown), matching the
        interruptibility of the other batch operations.
        """
        restored: List[Path] = []
        failures: List[Tuple[Path, str]] = []
        total = len(pairs)
        for i, (backup, target) in enumerate(pairs):
            if should_stop is not None and should_stop():
                break
            if on_each:
                on_each(i, total, target)
            try:
                # Restore is irreversible and overwrites the only live copy, so
                # refuse to clobber a good file with a backup that is empty or no
                # longer a decodable subtitle (e.g. a truncated/AV-zeroed backup).
                if backup.stat().st_size == 0:
                    failures.append((target, "backup is empty (0 bytes); not restored"))
                    continue
                # Decodability is the right guard for text subtitles (.ass/.ssa/
                # .srt) — a truncated/AV-zeroed text backup must not clobber a
                # good file. But .sup (PGS) is binary and never "decodes", so we
                # only require non-empty for binary formats.
                if (
                    backup.suffix.lower() in _TEXT_SUBTITLE_SUFFIXES
                    and FileProcessor.read_text(backup) is None
                ):
                    failures.append(
                        (target, "backup is not a decodable subtitle; not restored")
                    )
                    continue
                # Overwrite the live file ATOMICALLY, not with a bare copy2:
                # restore is the only path that clobbers the user's sole live
                # subtitle, so an interrupted copy must never leave it torn —
                # mirror apply_changes' atomic_write / copy_original_file.
                FileProcessor._atomic_copy_file(backup, target)
                restored.append(target)
            except Exception as e:  # noqa: BLE001
                failures.append((target, str(e)))
        return restored, failures

    # Style field names we actively write; used to validate Format: line.
    _REQUIRED_STYLE_FIELDS: Tuple[str, ...] = (
        "fontname", "primarycolour", "bold", "italic",
        "underline", "outline", "shadow",
    )

    # --- Typesetting / sign detection -------------------------------------
    # Override tags that mark a Dialogue line as a sign / typesetting / karaoke
    # rather than plain spoken dialogue: it's absolutely positioned, animated,
    # clipped, rotated, sheared, drawn as a vector shape, or time-synced to a
    # song. Such lines are crafted to match the on-screen artwork or music, so
    # restyling their font/colour would break that match.
    # (\p1..\p9 = vector drawing mode is included here too — it's part of the
    # majority signal, not an absolute one: a mostly-dialogue style with a few
    # drawn typeset elements under it must stay dialogue.)
    # \t(...) = animated transform: a strong typesetting/animation signal on the
    # same footing as \move (a sign that fades/rotates/scales over time). \fscx/
    # \fscy scaling is deliberately NOT matched — it's common in plain dialogue.
    _TYPESETTING_TAG = re.compile(
        r"\\(?:pos|move|i?clip|org|fa[xy]|fr[xyz0-9-]|p[1-9]|k[fo]?\d|t\()",
        re.IGNORECASE,
    )
    # Style-name markers that conventionally denote non-dialogue styles — a
    # safety net for sign styles that carry few positioning tags in the sampled
    # lines. Three tiers so the net catches real signs without flagging dialogue:
    #   • substrings (any position), including the standard Arabic sign-style
    #     words — an Arabic-first tool must recognise محاكاة/آيات/… by name, as
    #     the ASCII tokeniser below can't see them.
    #   • strong tokens: unambiguous sign words, flagged as any whole token.
    #   • weak tokens: short/common words (op, ed, note, …) flagged ONLY when
    #     they are the entire name, so a nickname like "Note-chan" stays dialogue.
    #     "op"/"ed" also count as the first word before a song qualifier
    #     ("OP English", "ED Romaji", "OP1"), but not before a name ("Ed Elric").
    # "sign" is a token, not a substring: "Signe" and "Design" are dialogue.
    _SIGN_NAME_SUBSTRINGS: Tuple[str, ...] = (
        "typeset",
        "محاكاة",           # typesetting / simulation (canonical Arabic sign style)
        "لافتة",            # sign / placard
        "حاشية",            # margin note / annotation
        "شعار",             # logo
        "عنوان",            # title card
        "آيات", "ايات",     # (Quranic) verse overlay
        "حقوق",             # credits / rights
        "أغنية", "اغنية",   # song
    )
    _TYPESETTING_STRONG_TOKENS: frozenset = frozenset({
        "signs", "typesetting", "karaoke", "kara", "oped",
        "song", "songs", "title", "titles", "caption", "captions",
        "credit", "credits", "overlay", "location", "locations",
        "scene", "screen", "board", "banner", "card", "cards",
        "chyron", "lyric", "lyrics",
    })
    _TYPESETTING_WEAK_TOKENS: frozenset = frozenset({
        "op", "ed", "ts", "note", "notes",
    })
    # A sign word, alone or glued to a position: sign, signs, signtop, sign2.
    _SIGN_TOKEN = re.compile(
        r"^signs?(?:top|bot|bottom|low|high|mid|middle|left|right|alt|age)?\d*$")
    _SONG_QUALIFIERS: frozenset = frozenset({
        "english", "eng", "en", "romaji", "romanji", "roma", "kanji", "jp", "jpn",
        "japanese", "tl", "trans", "translation", "lyric", "lyrics", "kara",
        "karaoke", "ar", "arabic", "sub", "subs", "top", "bottom", "main",
    })

    @staticmethod
    def _name_tokens(name: str) -> List[str]:
        """Lower-case words of a style name, split at camelCase too:
        "SignTop" -> sign, top; "OP_Eng1" -> op, eng, 1."""
        return [t.lower() for t in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)]

    # Matches an ASS override block {...} and the \N \n \h line-break/space
    # escapes — stripped before judging a line's script so Latin tag names and
    # \fn font names don't masquerade as English text.
    _OVERRIDE_BLOCK = re.compile(r"\{[^}]*\}")
    _LINE_ESCAPES = re.compile(r"\\[Nnh]")

    @staticmethod
    def _strip_ass_tags(text: str) -> str:
        """Return `text` with override blocks and line/space escapes removed, so
        only the visible on-screen characters remain for script detection."""
        return FileProcessor._LINE_ESCAPES.sub(
            " ", FileProcessor._OVERRIDE_BLOCK.sub("", text)
        )

    @staticmethod
    def _iter_dialogue_text(lines: List[str]):
        """Yield (style_name, text) for every Dialogue line in [Events],
        honouring the file's own Format order (falling back to the canonical
        Style=3, Text=9). Text keeps any commas it legally contains."""
        CANON_STYLE, CANON_TEXT = 3, 9
        style_i, text_i = CANON_STYLE, CANON_TEXT
        in_events = False
        for line in lines:
            low = line.lstrip().lower()
            if low.startswith("[events]"):
                in_events = True
                style_i, text_i = CANON_STYLE, CANON_TEXT
            elif line.lstrip().startswith("["):
                in_events = False
            elif in_events and low.startswith("format:"):
                fields = [
                    f.strip().lower()
                    for f in line.split(":", 1)[1].split(",")
                ]
                if "style" in fields:
                    style_i = fields.index("style")
                if "text" in fields:
                    text_i = fields.index("text")
            elif in_events and low.startswith("dialogue:"):
                body = line.split(":", 1)[1].rstrip("\r\n")
                parts = body.split(",", text_i)
                if text_i <= 0 or style_i >= len(parts) or text_i >= len(parts):
                    continue
                yield parts[style_i].strip(), parts[text_i]

    @staticmethod
    def _typesetting_styles(lines: List[str]) -> Set[str]:
        """Return the set of Style *names* used for signs/typesetting/karaoke.

        These styles are left completely untouched when restyling: they are
        positioned/clipped/drawn/rotated or synced to a song to match the video,
        so changing their font or colour would break that match. A style is
        flagged when the majority of its Dialogue lines carry positioning/
        drawing/rotation/karaoke tags, or when its name follows a sign/
        typesetting naming convention. Purely spoken-dialogue styles are not
        returned, so they still get the user's font.
        """
        total: Dict[str, int] = {}
        tsish: Dict[str, int] = {}

        for name, text in FileProcessor._iter_dialogue_text(lines):
            total[name] = total.get(name, 0) + 1
            if FileProcessor._TYPESETTING_TAG.search(text):
                tsish[name] = tsish.get(name, 0) + 1

        skip: Set[str] = set()
        for name, n in total.items():
            # A real majority (>50%) of positioned/rotated/karaoke lines marks a
            # sign. An exact tie does NOT: a dialogue style that uses \pos on
            # half its lines for overlap-avoidance must keep the user's font.
            if tsish.get(name, 0) * 2 > n:
                skip.add(name)
        for name in total:
            low = name.lower()
            if any(s in low for s in FileProcessor._SIGN_NAME_SUBSTRINGS):
                skip.add(name)
                continue
            tokens = FileProcessor._name_tokens(name)
            if any(t in FileProcessor._TYPESETTING_STRONG_TOKENS
                   or FileProcessor._SIGN_TOKEN.match(t) for t in tokens):
                skip.add(name)
            elif len(tokens) == 1 and tokens[0] in FileProcessor._TYPESETTING_WEAK_TOKENS:
                skip.add(name)
            elif (len(tokens) >= 2 and tokens[0] in ("op", "ed")
                  and (tokens[1] in FileProcessor._SONG_QUALIFIERS or tokens[1].isdigit())):
                skip.add(name)
        return skip

    # Unicode blocks that mark a character as Arabic script (for size-preset
    # normalization: pick the Arabic base-band reference vs the Latin cap).
    _ARABIC_RANGES: Tuple[Tuple[int, int], ...] = (
        (0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
        (0xFB50, 0xFDFF), (0xFE70, 0xFEFF),
    )

    @staticmethod
    def _dominant_script(
        lines: List[str], skip_styles: Optional[Set[str]] = None
    ) -> str:
        """The file's dominant spoken-dialogue script: 'arabic' | 'latin' |
        'other'. Chooses which reference the Fontsize normalization measures the
        font against (Arabic base-band vs Latin cap-height). Signs are excluded
        and override tags stripped — same as the font-coverage check — so a few
        Latin sign/font-name tags don't mislabel an Arabic file."""
        if skip_styles is None:
            skip_styles = FileProcessor._typesetting_styles(lines)
        latin = arabic = 0
        for style, raw_text in FileProcessor._iter_dialogue_text(lines):
            if style in skip_styles:
                continue
            for ch in FileProcessor._strip_ass_tags(raw_text):
                o = ord(ch)
                if 0x41 <= o <= 0x5A or 0x61 <= o <= 0x7A:
                    latin += 1
                elif any(a <= o <= b for a, b in FileProcessor._ARABIC_RANGES):
                    arabic += 1
            if latin + arabic >= 500:
                break
        if arabic > latin:
            return "arabic"
        if latin:
            return "latin"
        return "other"

    # ---- Writer parity: the one dialogue colour pass + "styled by us" -------
    @staticmethod
    def _tag_dialogue_line(
        line: str, text_index: int, style_index: int,
        skip_styles: Set[str], new_tag: str,
    ) -> Tuple[str, bool]:
        """The ONE implementation of the dialogue colour pass, shared by the
        writer (compute_changes) and the "styled by us" check, so the two can't
        drift. Returns (line, taggable): a taggable Dialogue row gets `new_tag`
        at the start of its Text, replacing a leading colour block a previous
        run wrote (never stacking); anything else is returned unchanged.

        Not taggable: a sign/typesetting style, a row too short to have a Text
        field, or a malformed [Events] Format that puts Text first (tagging
        there would glue the tag onto the whole Layer,Start,End,... row).
        Mid-text {\\c&H..&} overrides the author placed are left alone."""
        prefix, fields_str = line.split(":", 1)
        eol = line[len(line.rstrip("\r\n")):]
        # Text can legally contain commas: split only up to the Text column.
        parts = fields_str.rstrip("\r\n").split(",", text_index)
        style = parts[style_index].strip() if 0 <= style_index < len(parts) else ""
        if not (text_index > 0 and len(parts) > text_index and style not in skip_styles):
            return line, False
        text = parts[text_index]
        leading = COLOR_TAG_PATTERN.match(text)
        parts[text_index] = new_tag + (text[leading.end():] if leading else text)
        return f"{prefix}:{','.join(parts)}" + eol, True

    @staticmethod
    def _dialogue_rows(lines: List[str]):
        """Yield (index, text_index, style_index) for every Dialogue row, with
        the [Events] Format tracked exactly as compute_changes tracks it."""
        CANON_TEXT, CANON_STYLE = 9, 3
        text_i, style_i = CANON_TEXT, CANON_STYLE
        in_events = False
        for i, line in enumerate(lines):
            low = line.lstrip().lower()
            if low.startswith("[events]"):
                in_events = True
                text_i, style_i = CANON_TEXT, CANON_STYLE
            elif line.lstrip().startswith("["):
                in_events = False
            elif in_events and low.startswith("format:"):
                fields = [x.strip().lower() for x in line.split(":", 1)[1].split(",")]
                if "text" in fields:
                    text_i = fields.index("text")
                if "style" in fields:
                    style_i = fields.index("style")
            elif low.startswith("dialogue:"):
                yield i, text_i, style_i

    @staticmethod
    def dialogue_signature(lines: List[str], skip_styles: Set[str]) -> Dict[str, Any]:
        """Whose file is this? (ADR-006 §1)

        taggable = Dialogue rows the writer would colour; tagged = those whose
        Text already starts with a standalone colour block. `ours` is writer
        parity: re-running the colour pass with the colour found on the first
        taggable row changes nothing (and that tag is exactly what the writer
        emits: upper-case hex). A file with no taggable row is neither ours nor
        an original — it carries no signal."""
        taggable = tagged = 0
        rows = []
        first_tag: Optional[str] = None
        for i, ti, si in FileProcessor._dialogue_rows(lines):
            _, can_tag = FileProcessor._tag_dialogue_line(lines[i], ti, si, skip_styles, "")
            if not can_tag:
                continue
            taggable += 1
            rows.append((i, ti, si))
            text = lines[i].split(":", 1)[1].rstrip("\r\n").split(",", ti)[ti]
            m = COLOR_TAG_PATTERN.match(text)
            if m:
                tagged += 1
                if first_tag is None:
                    first_tag = m.group(0)
        ours = False
        if taggable and tagged == taggable and first_tag is not None:
            writer_tag = "{\\c&H" + first_tag[5:11].upper() + "&}"  # {\c&H<6 hex>&}
            ours = first_tag == writer_tag and all(
                FileProcessor._tag_dialogue_line(lines[i], ti, si, skip_styles, writer_tag)[0]
                == lines[i]
                for i, ti, si in rows
            )
        return {"taggable": taggable, "tagged": tagged, "ours": ours}

    @staticmethod
    def looks_styled_by_us(file_path: Path) -> bool:
        """Read-only: does this file's content look like the app's output
        (any settings)? Used by the monitor's "foreign" decision (ADR-007)."""
        read = FileProcessor.read_text(file_path)
        if read is None:
            return False
        lines = read[1]
        return FileProcessor.dialogue_signature(
            lines, FileProcessor._typesetting_styles(lines))["ours"]

    @staticmethod
    def compute_changes(
        file_path: Path,
        font_name: str,
        color_code: str,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        size: str = "none",
        nudge: float = 1.0,
        content: Optional[Tuple[str, List[str]]] = None,
    ) -> Optional[Tuple[str, List[str], Set[str]]]:
        """
        Compute the restyled line list for `file_path` WITHOUT touching disk.

        `content` = (encoding, lines) already read by the caller (apply_changes
        reads the bytes once so the backup is made from exactly what was
        styled); otherwise the file is read here.

        Pure read-and-transform — no backup, no write, no DB. Returns
        (encoding, new_lines, skip_styles) on success, or None if the file is
        skipped (the reason is recorded). skip_styles is the sign/typesetting
        set already computed here, returned so apply_changes' font-coverage check
        can reuse it instead of re-scanning the whole file. apply_changes calls
        this and then writes the result.
        """
        def _skip(op: str, reason: str) -> None:
            record_error(op, file_path.name, reason)
            return None

        try:
            safe_color = SecurityUtils.sanitize_color_code(color_code)
        except ValueError as e:
            logger.error(f"Invalid color code: {e}")
            return None

        safe_font = SecurityUtils.sanitize_ass_field(font_name)
        if not safe_font:
            logger.error("Font name is empty after sanitization")
            return None

        read = content if content is not None else FileProcessor.read_text(file_path)
        if read is None:
            # read_text already logged/recorded the reason
            return None
        encoding, lines = read[0], list(read[1])

        # Genuine SSA v4 ([V4 Styles]) uses a different Style field set; the
        # repair path would rewrite it into the ASS V4+ layout and scramble
        # every column, so skip it rather than corrupt it.
        if FileProcessor._is_ssa_v4(lines):
            return _skip(
                "apply",
                "SubStation Alpha v4 ([V4 Styles]) format is not supported for "
                "restyling; skipped to avoid corrupting it",
            )

        # Adaptive outline/shadow sizes from the file's own script space.
        res_x, res_y, scaled_border = FileProcessor._extract_resolution_from_lines(
            lines, file_path.name
        )
        # Signs / typesetting / karaoke styles are left untouched (they match the
        # on-screen artwork). Computed here — BEFORE sizing — because the Fontsize
        # normalization needs the dominant DIALOGUE script (sign lines excluded).
        # Reused by the Style/Dialogue loops below and returned to the caller so
        # apply_changes' font-coverage check doesn't re-scan the file.
        skip_styles = FileProcessor._typesetting_styles(lines)
        if skip_styles:
            logger.info(
                f"{file_path.name}: leaving {len(skip_styles)} typesetting/sign "
                f"style(s) untouched: {', '.join(sorted(skip_styles))}"
            )
        script = FileProcessor._dominant_script(lines, skip_styles)
        # Optional dialogue Fontsize + the outline/shadow basis, sized so the
        # on-screen size is consistent library-wide — against the 16:9-equivalent
        # frame height (scope letterbox) AND normalized by the font's visible
        # letter height for `script`, so a preset is the same size across fonts.
        # Shared with is_already_styled so the two can't disagree. size_target is
        # None = leave every Fontsize untouched (the default).
        adaptive_basis, size_basis, size_target = FileProcessor._size_targets(
            res_x, res_y, size, font_name, script, nudge
        )
        if scaled_border is False:
            # Explicit "ScaledBorderAndShadow: no": Outline/Shadow are output
            # pixels, so scaling them by the script resolution would be
            # meaningless. Write the base sizes and leave the header alone.
            file_outline, file_shadow = BASE_OUTLINE_SIZE, BASE_SHADOW_SIZE
            pin_scaled_border = False
        else:
            file_outline, file_shadow = FileProcessor._compute_adaptive_sizes(
                adaptive_basis
            )
            # Our values are in script-space units, which is only guaranteed
            # under "ScaledBorderAndShadow: yes" — pin it when the header is
            # absent (renderer defaults disagree; Aegisub does the same).
            pin_scaled_border = scaled_border is None
            logger.info(
                f"Adaptive sizes for {adaptive_basis}p script space: "
                f"outline={file_outline}, shadow={file_shadow}"
            )

        target_fontsize = str(size_target) if size_target is not None else None
        if target_fontsize is not None:
            logger.info(
                f"{file_path.name}: dialogue size '{size}' -> Fontsize "
                f"{target_fontsize} ({size_basis}p 16:9-equiv, {script} "
                f"'{font_name}')"
            )

        lines, field_map, field_count, dirty = FileProcessor.validate_ass_lines(
            lines, file_path
        )

        missing = [
            f for f in FileProcessor._REQUIRED_STYLE_FIELDS
            if f not in field_map
        ]
        if missing:
            return _skip(
                "apply",
                f"Format: line is missing required field(s): {', '.join(missing)}",
            )

        i_name        = field_map.get("name", 0)
        i_fontname    = field_map["fontname"]
        i_primary     = field_map["primarycolour"]
        i_bold        = field_map["bold"]
        i_italic      = field_map["italic"]
        i_underline   = field_map["underline"]
        i_outline     = field_map["outline"]
        i_shadow      = field_map["shadow"]
        i_fontsize    = field_map.get("fontsize")
        # Optional colour columns (present in canonical V4+): pinned to black so
        # the outline/shadow the app owns render as a crisp black edge, not the
        # tint the source shipped (some releases use a dark-grey outline).
        i_outline_col = field_map.get("outlinecolour")
        i_shadow_col  = field_map.get("backcolour")
        min_parts_needed = max(
            i_name, i_fontname, i_primary, i_bold, i_italic,
            i_underline, i_outline, i_shadow,
        ) + 1

        # (skip_styles was computed above, before sizing.)

        # Canonical Events section has Text at index 9. If the file declares
        # its own `[Events] Format:` line we honor that instead so we don't
        # prepend a color tag into the wrong column (same class of bug as
        # the Styles case).
        CANONICAL_TEXT_INDEX = 9
        CANONICAL_STYLE_INDEX = 3
        events_text_index = CANONICAL_TEXT_INDEX
        events_style_index = CANONICAL_STYLE_INDEX
        in_events = False
        in_styles = False

        SBS_HEADER = "ScaledBorderAndShadow: yes"
        sbs_pending = pin_scaled_border

        try:
            new_lines: List[str] = []
            for line in lines:
                current_line = line
                stripped_lower = line.lstrip().lower()

                if stripped_lower.startswith("[events]"):
                    in_events = True
                    in_styles = False
                    events_text_index = CANONICAL_TEXT_INDEX
                    events_style_index = CANONICAL_STYLE_INDEX
                elif (
                    stripped_lower.startswith("[v4+ styles]")
                    or stripped_lower.startswith("[v4 styles]")
                ):
                    in_styles = True
                    in_events = False
                elif stripped_lower.startswith("[script info]"):
                    in_events = False
                    in_styles = False
                    if sbs_pending:
                        new_lines.append(current_line)
                        new_lines.append(
                            SBS_HEADER + FileProcessor._line_ending(line)
                        )
                        sbs_pending = False
                        continue
                elif line.lstrip().startswith("["):
                    in_events = False
                    in_styles = False

                if in_styles and stripped_lower.startswith("style:"):
                    prefix, fields_str = line.split(":", 1)
                    eol = line[len(line.rstrip("\r\n")):]
                    parts = fields_str.rstrip("\r\n").split(",")

                    if (
                        len(parts) >= min_parts_needed
                        and parts[i_name].strip() not in skip_styles
                    ):
                        parts[i_fontname]  = safe_font
                        parts[i_primary]   = format_ass_primary_color(safe_color)
                        parts[i_bold]      = "-1" if bold else "0"
                        parts[i_italic]    = "-1" if italic else "0"
                        parts[i_underline] = "-1" if underline else "0"
                        parts[i_outline]   = str(file_outline)
                        parts[i_shadow]    = str(file_shadow)
                        if i_outline_col is not None and i_outline_col < len(parts):
                            parts[i_outline_col] = DIALOGUE_OUTLINE_COLOUR
                        if i_shadow_col is not None and i_shadow_col < len(parts):
                            parts[i_shadow_col] = DIALOGUE_SHADOW_COLOUR
                        if (
                            target_fontsize is not None
                            and i_fontsize is not None
                            and i_fontsize < len(parts)
                        ):
                            parts[i_fontsize] = target_fontsize
                        current_line = f"{prefix}:{','.join(parts)}" + eol

                elif in_events and stripped_lower.startswith("format:"):
                    events_fields = [
                        f.strip().lower()
                        for f in line.split(":", 1)[1].split(",")
                    ]
                    if "text" in events_fields:
                        events_text_index = events_fields.index("text")
                    if "style" in events_fields:
                        events_style_index = events_fields.index("style")

                elif stripped_lower.startswith("dialogue:"):
                    # Note: Comment: rows are intentionally left alone — only
                    # visible Dialogue lines get the color tag. We normalise the
                    # tag at the START of the Text column (replacing one a prior
                    # run prepended) and otherwise prepend a new one. Mid-text
                    # {\c&H..&} overrides the author placed for individual
                    # words/signs are left untouched, and re-styling is
                    # idempotent (the leading tag is replaced, never stacked).
                    current_line, _ = FileProcessor._tag_dialogue_line(
                        line, events_text_index, events_style_index,
                        skip_styles, f"{{\\c&H{safe_color}&}}",
                    )

                new_lines.append(current_line)

            if sbs_pending:
                # No [Script Info] section anywhere — prepend one so the
                # semantics of the scaled values we just wrote are still
                # pinned.
                ending = (
                    FileProcessor._line_ending(lines[0]) if lines else "\n"
                )
                new_lines[:0] = [
                    "[Script Info]" + ending,
                    SBS_HEADER + ending,
                    ending,
                ]
        except Exception as e:  # noqa: BLE001
            return _skip("apply", str(e))

        return encoding, new_lines, skip_styles

    @staticmethod
    def apply_changes(
        file_path: Path,
        font_name: str,
        color_code: str,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        size: str = "none",
        nudge: float = 1.0,
    ) -> bool:
        """
        Apply font and color changes to an ASS file (backup + atomic write).

        Returns True if the file was restyled. Backups follow ADR-006 §2:
        the original is saved from exactly the bytes that were read; a
        downloader's replacement becomes the new original (the old one is kept
        in Original Subs/.older/); styled or empty content is never saved as an
        original; nothing is written if the file changed while it was being
        processed (the next pass retries).
        """
        raw_and_stat = FileProcessor._read_raw(file_path)
        if raw_and_stat is None:
            return False
        raw, st0 = raw_and_stat
        if not raw:
            record_error("apply", file_path.name, "the file is empty; left as it is")
            return False
        decoded = FileProcessor._decode_bytes(raw)
        if decoded is None:
            record_error(
                "decode", file_path.name,
                "No supported encoding matched this file (tried utf-8, "
                "cp1256, cp1252, shift_jis, …)",
            )
            return False
        orig_lines = splitlines_keepends_ass(decoded[1])

        computed = FileProcessor.compute_changes(
            file_path, font_name, color_code,
            bold=bold, italic=italic, underline=underline, size=size, nudge=nudge,
            content=(decoded[0], orig_lines),
        )
        if computed is None:
            # compute_changes already logged/recorded the reason
            return False
        encoding, new_lines, skip_styles = computed

        signature = FileProcessor.dialogue_signature(orig_lines, skip_styles)
        if signature["taggable"] == 0:
            # Nothing the writer would colour (a signs/songs-only track, an
            # OP/ED file, malformed rows): no visible change is possible, so
            # don't back up or rewrite it — just remember we've seen it.
            logger.info(f"{file_path.name}: no dialogue lines to restyle; left as it is")
            try:
                DatabaseManager.add_file(file_path)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Could not record {file_path.name}: {e}")
            return False

        # If the chosen font can't render the file's script (e.g. an Arabic-only
        # font over an English sidecar in a bilingual library), SKIP it — leave
        # it untouched rather than restyle its text into tofu. Reuse the
        # skip_styles compute_changes already derived so this doesn't re-scan.
        if FileProcessor._font_cant_render_file(
            font_name, new_lines, file_path, skip_styles=skip_styles
        ):
            return False

        if not FileProcessor._prepare_backup(file_path, raw, signature):
            return False

        # Nothing may have changed the file since it was read (a download still
        # landing, a player or editor saving): writing now would overwrite
        # content we never saw. Leave it for the next pass.
        try:
            st1 = file_path.stat()
        except OSError as e:
            FileProcessor._note_transient(file_path, e)
            record_error("apply", file_path.name, f"could not read the file: {e}")
            return False
        if (st1.st_size, st1.st_mtime_ns) != (st0.st_size, st0.st_mtime_ns):
            logger.info(f"{file_path.name} changed while it was being styled; will retry")
            return False

        try:
            atomic_write(file_path, new_lines, encoding)
            logger.info(f"Applied changes to {file_path}")
        except IOError as e:
            FileProcessor._note_transient(file_path, e)
            record_error("apply", file_path.name, f"IO error: {e}")
            return False
        except Exception as e:  # noqa: BLE001
            record_error("apply", file_path.name, str(e))
            return False

        # The styled file is durably on disk: atomic_write is all-or-nothing via
        # os.replace, so on any write failure above the original file is left
        # untouched. Recording the file in the DB is bookkeeping, NOT part of
        # the write: a transient DB error here must not revert the styling.
        try:
            DatabaseManager.add_file(file_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Styled {file_path.name} but could not record it in the DB: {e}"
            )
        return True

    # Folders already told "the originals here were deleted" this session.
    _noted_missing_originals: Set[str] = set()

    # Files whose last apply failed for a reason that can clear up by itself
    # (held open by a player, access denied, disk full, drive unreachable).
    # The monitor retries those even when the file itself hasn't changed.
    _transient_failures: Set[str] = set()

    @staticmethod
    def _note_transient(file_path: Path, error: BaseException) -> None:
        if isinstance(error, OSError):
            FileProcessor._transient_failures.add(str(file_path))

    @staticmethod
    def take_transient_failure(file_path: Path) -> bool:
        """True (once) if the last apply of `file_path` failed transiently."""
        try:
            FileProcessor._transient_failures.remove(str(file_path))
            return True
        except KeyError:
            return False

    @staticmethod
    def _read_raw(file_path: Path) -> Optional[Tuple[bytes, Any]]:
        """(bytes, stat) of a subtitle, read once; None (recorded) on failure."""
        try:
            st = file_path.stat()
            if st.st_size > MAX_ASS_FILE_SIZE:
                record_error(
                    "read", file_path.name,
                    f"File too large to be a subtitle ({st.st_size} bytes); skipped",
                )
                return None
            with open(file_path, "rb") as fh:
                return fh.read(), st
        except OSError as e:
            FileProcessor._note_transient(file_path, e)
            record_error("read", file_path.name, str(e))
            return None

    @staticmethod
    def _is_link(path: Path) -> bool:
        """True for a symbolic link or junction (any reparse point)."""
        try:
            st = os.lstat(path)
        except OSError:
            return False
        return bool(getattr(st, "st_file_attributes", 0) & 0x400) or stat.S_ISLNK(st.st_mode)

    @staticmethod
    def _write_bytes_atomic(dst: Path, data: bytes, tmp_prefix: str = ".bak_") -> None:
        """Write `data` to `dst` via a temp in the same folder + fsync + rename."""
        fd, tmp = tempfile.mkstemp(dir=str(dst.parent), prefix=tmp_prefix, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dst)
            tmp = None
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @staticmethod
    def _older_path(backup: Path) -> Path:
        """A free name in Original Subs/.older/ for a superseded copy."""
        older = backup.parent / ".older"
        older.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while True:
            candidate = older / f"{backup.stem}.{stamp}-{n}{backup.suffix}"
            if not candidate.exists():
                return candidate
            n += 1

    @staticmethod
    def _prepare_backup(file_path: Path, raw: bytes, signature: Dict[str, Any]) -> bool:
        """Apply the ADR-006 backup table before a file is restyled. False
        (recorded) when the file must be left alone."""
        backup_dir = file_path.parent / "Original Subs"
        backup = backup_dir / file_path.name
        try:
            if FileProcessor._is_link(backup_dir):
                record_error(
                    "backup", file_path.name,
                    "the Original Subs folder is a link to somewhere else; "
                    "left as it is so no backup lands outside the library",
                )
                return False
            ours = signature["ours"]
            if backup.exists():
                if ours:
                    return True  # never replace an original with styled content
                current = backup.read_bytes()
                if current == raw:
                    return True
                if signature["tagged"] == 0:
                    # Clearly an unstyled original that differs from the backup:
                    # a downloader replaced the file (Bazarr upgrade, re-download).
                    # The new bytes become the original; the old one is kept.
                    staged = backup_dir / f".bak_{os.getpid()}_{time.monotonic_ns()}.tmp"
                    FileProcessor._write_bytes_atomic(staged, raw)
                    older = FileProcessor._older_path(backup)
                    try:
                        os.replace(backup, older)
                    except OSError:
                        os.unlink(staged)
                        raise
                    try:
                        os.replace(staged, backup)
                    except OSError:
                        os.replace(older, backup)  # put the old original back
                        raise
                    logger.info(
                        f"{file_path.name}: replaced by a new release; it is now "
                        f"the original (previous original kept as {older.name})"
                    )
                    return True
                # Partly tagged: can't tell a hand edit of our output from a new
                # release. Keep the backup, and keep the incoming bytes too.
                FileProcessor._write_bytes_atomic(FileProcessor._older_path(backup), raw)
                return True
            if ours and DatabaseManager.was_processed(str(file_path)):
                # The app styled this file before and its original was deleted.
                # Saving styled content as the "original" would be a lie, and
                # refusing would block the file forever: restyle, no backup.
                folder = str(file_path.parent)
                if folder not in FileProcessor._noted_missing_originals:
                    FileProcessor._noted_missing_originals.add(folder)
                    record_error(
                        "backup", file_path.name,
                        "the originals for this folder were deleted, so Restore "
                        "can't bring them back; files here are restyled without a backup",
                    )
                return True
            backup_dir.mkdir(exist_ok=True)
            FileProcessor._write_bytes_atomic(backup, raw)
            logger.info(f"Copied original to {backup}")
            return True
        except Exception as e:  # noqa: BLE001 — every failure must reach Problems
            FileProcessor._note_transient(file_path, e)
            record_error("backup", file_path.name, f"could not back up the original: {e}")
            return False

    @staticmethod
    def _font_cant_render_file(
        font_name: str,
        lines: List[str],
        file_path: Path,
        skip_styles: Optional[Set[str]] = None,
    ) -> bool:
        """True if `font_name` can't render this file's dominant script — the
        caller then SKIPS the file (leaves it untouched) and this LOGS why (at
        info level, not as a Problems-panel issue — a coverage skip is expected
        protective behaviour, not a failure), so a bilingual library keeps its
        English subtitles instead of restyling them into tofu.

        Detection is by the file's ACTUAL TEXT, not the filename's language tag
        (.eng/.ar are unreliable and often absent). It samples up to 50 Dialogue
        lines — but only from the styles we would actually restyle: sign/
        typesetting/karaoke lines are excluded (they keep their own font, so
        whether the user's font covers them is irrelevant, and their heavy
        `\\fnLatinFontName`/positioning tags would otherwise swamp the sample
        and mislabel an Arabic file as English). Override blocks are stripped so
        only the visible on-screen text — Arabic or Latin — decides the script:

        • Mostly Latin (English): the glyph-coverage test can't judge it — it
          skips ASCII, assuming every font has it — but an Arabic-only font
          genuinely lacks Latin, so check supports_latin explicitly.
        • Mostly non-ASCII (Arabic, …): occurrence-weighted — skip when the font
          would tofu a MAJORITY of the visible non-ASCII glyphs. So the dominant
          script is caught even when it uses few distinct letters, while a single
          incidental 'ō' among hundreds of Arabic glyphs stays quiet (renders
          fine — apply proceeds). Invisible controls/selectors are excluded, so
          the app never trips on the U+202B its own RTL-wrapping inserts.
        """
        try:
            if skip_styles is None:
                skip_styles = FileProcessor._typesetting_styles(lines)
            sample: List[str] = []
            for style, raw_text in FileProcessor._iter_dialogue_text(lines):
                if style in skip_styles:
                    continue  # a sign keeps its own font — not our concern
                sample.append(FileProcessor._strip_ass_tags(raw_text))
                if len(sample) >= 50:
                    break
            text = "".join(sample)
            latin = sum(1 for c in text if "A" <= c <= "Z" or "a" <= c <= "z")
            occurrences = FontSupport.renderable_chars(text)  # renderable non-ASCII

            # Predominantly-Latin (English) file: only the explicit Latin check
            # can catch an Arabic-only font here (ASCII is invisible to the
            # coverage test). Incidental non-ASCII in an otherwise-Latin file
            # that the font DOES cover is tolerated.
            if latin > len(occurrences):
                if FontSupport.supports_latin(font_name) is False:
                    # Expected, not a problem: leaving a file the chosen font
                    # can't render (an English sidecar under an Arabic-only font)
                    # untouched is the whole point of this check. Log it for the
                    # record, but do NOT surface it in the Problems panel — it
                    # would flood out real failures in a bilingual library.
                    logger.info(
                        f"font: {file_path.name}: left untouched — '{font_name}' "
                        f"can't render this file's Latin (English) text"
                    )
                    return True
                return False

            if not occurrences:
                return False  # nothing renderable/non-ASCII to miss
            missing_set = set(FontSupport.unsupported_chars(font_name, text))
            if not missing_set:
                return False
            missing_count = sum(1 for ch in occurrences if ch in missing_set)
            # Skip only when the majority of visible glyphs would be tofu — a
            # genuine wrong-font, not a stray incidental character.
            if missing_count * 2 <= len(occurrences):
                return False
            distinct = sorted(missing_set)
            shown = " ".join(distinct[:8])
            more = "" if len(distinct) <= 8 else f" (+{len(distinct) - 8} more)"
            # Same reasoning as the Latin case above: a deliberate skip to avoid
            # tofu is expected behaviour, not a failure — log it, don't file it
            # as a problem.
            logger.info(
                f"font: {file_path.name}: left untouched — '{font_name}' can't "
                f"render this file ({missing_count} of {len(occurrences)} "
                f"characters): {shown}{more}"
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug(f"font coverage check failed for {file_path.name}: {e}")
            return False

    @staticmethod
    def is_already_styled(
        file_path: Path,
        font_name: str,
        color_code: str,
        bold: bool = False,
        italic: bool = False,
        underline: bool = False,
        size: str = "none",
        nudge: float = 1.0,
    ) -> bool:
        """
        Check whether every Style: row already matches the target settings,
        including adaptive outline/shadow derived from the file's PlayResY.

        NOTE: "styled" here is defined purely by the [V4+ Styles] rows; this
        intentionally does NOT inspect the inline Dialogue {\\c&H..&} colour
        tags that apply_changes also writes. Dialogue tags reuse the same
        colour as PrimaryColour, so an untagged line still renders correctly;
        the skip check therefore keys on Style rows only.
        """
        try:
            safe_color = SecurityUtils.sanitize_color_code(color_code)
        except ValueError:
            return False

        read = FileProcessor.read_text(file_path)
        if read is None:
            return False
        _, lines = read

        # Mirror compute_changes: sign/typesetting styles are left untouched, so
        # they keep their ORIGINAL font/colour and must NOT be judged here — else
        # a correctly-styled file with any typesetting style always looks
        # "unstyled" and gets needlessly re-processed.
        skip_styles = FileProcessor._typesetting_styles(lines)
        # Same dominant-script detection compute_changes used, so the font-size
        # normalization (and thus the target Fontsize) matches exactly. Override
        # tags are stripped, so the leading colour tag compute_changes injected
        # doesn't change the verdict.
        script = FileProcessor._dominant_script(lines, skip_styles)

        target_primary = format_ass_primary_color(safe_color)
        target_bold = "-1" if bold else "0"
        target_italic = "-1" if italic else "0"
        target_underline = "-1" if underline else "0"
        # Compare against the sanitized form — apply_changes writes this, not
        # the raw user string, so the check must be symmetric.
        target_font = SecurityUtils.sanitize_ass_field(font_name)
        if not target_font:
            return False

        res_x, res_y, scaled_border = FileProcessor._extract_resolution_from_lines(
            lines, file_path.name
        )
        if scaled_border is None:
            # apply_changes pins `ScaledBorderAndShadow: yes` on files that
            # lack the header; until it exists the file isn't fully styled.
            return False
        # Shared with compute_changes so the write path and this check can't
        # disagree on the size target (which would re-process the file forever) —
        # same font_name + script + nudge, so the font-normalized Fontsize matches.
        adaptive_basis, _size_basis, target_fontsize = FileProcessor._size_targets(
            res_x, res_y, size, font_name, script, nudge
        )
        if scaled_border is False:
            target_outline, target_shadow = BASE_OUTLINE_SIZE, BASE_SHADOW_SIZE
        else:
            target_outline, target_shadow = FileProcessor._compute_adaptive_sizes(
                adaptive_basis
            )

        field_map: Optional[Dict[str, int]] = None
        in_styles = False
        found_style = False

        for line in lines:
            stripped_lower = line.lstrip().lower()

            if stripped_lower.startswith("[v4+ styles]") or stripped_lower.startswith("[v4 styles]"):
                in_styles = True
                continue
            if in_styles and line.lstrip().startswith("["):
                break  # Past styles section.

            if not in_styles:
                continue

            if stripped_lower.startswith("format:"):
                fields = [
                    f.strip()
                    for f in line.split(":", 1)[1].split(",")
                ]
                # Resolve the field map the SAME way apply_changes does (via the
                # shared resolver) so the skip-check and the rewriter can't
                # disagree on a broken-but-present Format line.
                field_map, _fc, _used = FileProcessor._resolve_style_field_map(
                    fields
                )
                continue

            if not stripped_lower.startswith("style:"):
                continue

            if field_map is None:
                field_map = FileProcessor._canonical_field_map()

            _, fields_str = line.split(":", 1)
            parts = fields_str.rstrip("\r\n").split(",")

            i_name = field_map.get("name", 0)
            if i_name < len(parts) and parts[i_name].strip() in skip_styles:
                # Typesetting style — compute_changes leaves it as-is. Count it as
                # a satisfied row so a file whose styles are ALL signs still reads
                # as styled (nothing to do) instead of being judged unstyled and
                # needlessly re-backed-up + rewritten on every scan.
                found_style = True
                continue  # compute_changes leaves it as-is

            vals: Dict[str, Optional[str]] = {}
            for name in FileProcessor._REQUIRED_STYLE_FIELDS:
                idx = field_map.get(name)
                vals[name] = (
                    parts[idx].strip()
                    if idx is not None and idx < len(parts)
                    else None
                )

            if any(v is None for v in vals.values()):
                # Style row too short to judge — treat as mismatch to force a rewrite.
                return False

            if (
                vals["fontname"] != target_font
                or vals["primarycolour"] != target_primary
                or vals["bold"] != target_bold
                or vals["italic"] != target_italic
                or vals["underline"] != target_underline
            ):
                return False

            try:
                cur_outline = float(vals["outline"] or "0")
                cur_shadow = float(vals["shadow"] or "0")
            except ValueError:
                return False

            # Tolerance for float formatting differences (e.g. "2" vs "2.0").
            if abs(cur_outline - target_outline) > 0.05:
                return False
            if abs(cur_shadow - target_shadow) > 0.05:
                return False

            # Outline/shadow COLOUR must be the black the app pins — else a sub
            # that shipped a grey outline (font/colour/size otherwise matching)
            # would read as "already styled" and never get its edge fixed. Only
            # judged when the column exists (compute_changes only writes it then).
            i_oc = field_map.get("outlinecolour")
            if i_oc is not None and i_oc < len(parts) \
                    and parts[i_oc].strip() != DIALOGUE_OUTLINE_COLOUR:
                return False
            i_sc = field_map.get("backcolour")
            if i_sc is not None and i_sc < len(parts) \
                    and parts[i_sc].strip() != DIALOGUE_SHADOW_COLOUR:
                return False

            i_fs = field_map.get("fontsize")
            # Only judge size when a preset is set AND the file actually has a
            # Fontsize column — compute_changes can't write size without one, so
            # requiring it here would flag the file "unstyled" forever.
            if target_fontsize is not None and i_fs is not None:
                cur_fs = parts[i_fs].strip() if i_fs < len(parts) else None
                try:
                    if cur_fs is None or round(float(cur_fs)) != target_fontsize:
                        return False
                except ValueError:
                    return False

            found_style = True

        return found_style

    @staticmethod
    def is_video_file(filename: str) -> bool:
        """Check if filename is a video file."""
        _, ext = os.path.splitext(filename)
        return ext.lower() in VIDEO_EXTENSIONS

    @staticmethod
    def list_subtitle_streams(file_path: str) -> Optional[List[SubtitleStream]]:
        """
        List subtitle streams in video file using ffprobe.
        
        Args:
            file_path: Path to video file
            
        Returns:
            List of SubtitleStream objects or None on error
        """
        bin_ffprobe = FFmpegLocator.get_binary("ffprobe")
        if not bin_ffprobe:
            logger.error("ffprobe not found; cannot list subtitle streams.")
            return None
        
        # Validate file path
        video_path = Path(file_path).resolve()
        if not video_path.exists():
            logger.error(f"Video file not found: {file_path}")
            return None
        
        cmd = [
            bin_ffprobe, 
            "-v", "error", 
            "-print_format", "json", 
            "-show_streams", 
            str(video_path)
        ]
        
        try:
            proc = run_tracked(
                cmd,
                # ffprobe prints UTF-8 JSON. Python's text=True default would
                # decode it with the ANSI code page and fail on any non-ASCII
                # stream title on English/Western/CJK Windows.
                encoding="utf-8",
                errors="replace",
                check=True,
                timeout=30,
            )
            info = json.loads(proc.stdout or "{}")
        except subprocess.TimeoutExpired:
            logger.error(f"ffprobe timed out on '{file_path}'")
            return None
        except subprocess.CalledProcessError as e:
            logger.error(f"ffprobe error on '{file_path}': {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from ffprobe: {e}")
            return None
        
        streams = info.get("streams", [])
        subs: List[SubtitleStream] = []
        
        for s in streams:
            if s.get("codec_type") == "subtitle":
                subs.append(SubtitleStream.from_dict(s))
        
        return subs

    @staticmethod
    def filter_best_streams(
        streams: List[SubtitleStream], 
        selected_langs_str: str
    ) -> List[SubtitleStream]:
        """
        Filter streams to extract based on language preferences.
        
        Args:
            streams: List of available streams
            selected_langs_str: Comma-separated language codes
            
        Returns:
            Filtered list of streams
        """
        wanted = {
            _LANG_ALIASES.get(lang.strip().lower(), lang.strip().lower())
            for lang in selected_langs_str.split(",")
            if lang.strip()
        }

        grouped: Dict[str, List[SubtitleStream]] = {}
        for s in streams:
            lang = _LANG_ALIASES.get(s.language.lower(), s.language.lower())
            if wanted and lang not in wanted:
                continue
            grouped.setdefault(lang, []).append(s)
        
        final_list: List[SubtitleStream] = []
        for lang, s_list in grouped.items():
            if len(s_list) == 1:
                final_list.extend(s_list)
                continue
            
            # Prefer full subtitle tracks over signs/songs
            full_tracks = []
            for s in s_list:
                title = s.title.lower()
                if "sign" not in title and "song" not in title:
                    full_tracks.append(s)
            
            if full_tracks:
                final_list.extend(full_tracks)
            else:
                final_list.extend(s_list)
        
        return final_list

    @staticmethod
    def extract_subtitle(
        file_path: str,
        stream_index: int,
        lang: str,
        codec: str,
        output_dir: str,
        disambiguate: bool = False,
    ) -> str:
        """
        Extract subtitle stream from video file.
        
        Args:
            file_path: Path to video file
            stream_index: Stream index to extract
            lang: Language code for filename
            codec: Codec name for extension
            output_dir: Output directory
            
        Returns:
            Path to extracted subtitle file
            
        Raises:
            RuntimeError: If ffmpeg not found
            subprocess.TimeoutExpired: If extraction times out
        """
        # Validate and sanitize inputs
        input_path = Path(file_path).resolve()
        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {file_path}")
        
        output_path = Path(output_dir).resolve()
        output_path.mkdir(parents=True, exist_ok=True)

        # Sanitize language for filename (attacker-controllable — comes from
        # the MKV's metadata; see SecurityUtils.sanitize_filename).
        safe_lang = SecurityUtils.sanitize_filename(
            lang.replace(" ", "_").lower()
        )
        safe_base = SecurityUtils.sanitize_filename(input_path.stem)

        # Ensure stream_index is an integer to prevent injection (also used in
        # the disambiguated filename below).
        stream_idx = int(stream_index)

        # Pick an extension/container that matches the codec.
        codec_l = codec.lower()
        is_image_sub = any(
            k in codec_l
            for k in ("pgs", "hdmv", "dvd_sub", "dvdsub", "vobsub", "xsub")
        )
        if "ass" in codec_l or "ssa" in codec_l:
            ext = ".ass"
        elif "pgs" in codec_l or "hdmv" in codec_l:
            # PGS/HDMV stream-copy cleanly into their native .sup container.
            ext = ".sup"
        elif is_image_sub:
            # DVD/VobSub/XSUB have no single-file container ffmpeg can
            # stream-copy into (the .sub muxer is text-only microdvd), so an
            # attempt just leaves a 0-byte file and an opaque error. Refuse
            # cleanly with a message the user can act on.
            raise ValueError(
                f"Image-based subtitle codec '{codec}' can't be extracted to a "
                f"standalone file"
            )
        else:
            ext = ".srt"

        # Multiple kept streams of the same language would otherwise collide on
        # one filename and silently overwrite each other; include the stream
        # index to keep them distinct.
        if disambiguate:
            out_name = SecurityUtils.sanitize_filename(
                f"{safe_base}.{safe_lang}.{stream_idx}{ext}"
            )
        else:
            out_name = SecurityUtils.sanitize_filename(f"{safe_base}.{safe_lang}{ext}")
        out_path = (output_path / out_name).resolve()

        # Defense-in-depth: refuse to write outside the declared output dir.
        try:
            out_path.relative_to(output_path)
        except ValueError:
            raise ValueError(
                f"Refusing to write subtitle outside output dir: {out_path}"
            )

        # The new file replaces any existing one at the end. If a file already
        # exists at this path (e.g. a hand-edited / manually-downloaded sub the
        # user keeps next to the video), back it up first so the overwrite is
        # recoverable — the same safety model apply_changes uses for restyles.
        if out_path.exists():
            existing_backup = out_path.parent / "Original Subs" / out_path.name
            if existing_backup.exists():
                # This sidecar was already extracted once and its content is
                # captured in Original Subs/. copy_original_file is write-once, so
                # a re-extract here would overwrite the CURRENT file (possibly
                # hand-edited since) WITHOUT a fresh backup — an unrecoverable loss
                # of those edits. Re-extraction of the same stream is byte-identical
                # anyway, so skip it and keep the on-disk file intact. (Delete the
                # file to force a fresh extract.)
                logger.info(
                    f"{out_path.name}: already extracted (backup present); "
                    f"skipping re-extraction to preserve the existing file"
                )
                return str(out_path)
            # First overwrite of a pre-existing sidecar (e.g. a hand-downloaded
            # sub): ffmpeg -y truncates the target at open, so a backup MUST
            # succeed before we run it — otherwise it is destroyed with no
            # recoverable copy. Treat backup failure as fatal.
            if not FileProcessor.copy_original_file(out_path):
                raise RuntimeError(
                    f"Refusing to overwrite {out_path.name}: backup failed"
                )

        ffmpeg_bin = FFmpegLocator.get_binary("ffmpeg")
        if not ffmpeg_bin:
            raise RuntimeError("ffmpeg not found")

        # ffmpeg writes to a temp file in the same folder (".tmp_" names are
        # ignored by the scan and the watcher; the real extension keeps ffmpeg's
        # muxer choice). Only a successful, non-empty result replaces the
        # sidecar, so a killed, timed-out or failed extraction never leaves a
        # 0-byte or half-written subtitle and never destroys an existing one.
        fd, tmp_name = tempfile.mkstemp(dir=str(output_path), prefix=".tmp_", suffix=ext)
        os.close(fd)
        tmp_path = Path(tmp_name)
        cmd = [
            ffmpeg_bin,
            "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(input_path),
            "-map", f"0:{stream_idx}",
        ]
        if is_image_sub:
            # Bitmap subtitles can't be transcoded to text; copy the stream
            # verbatim into its native container.
            cmd += ["-c", "copy"]
        cmd.append(str(tmp_path))

        try:
            run_tracked(cmd, check=True, timeout=FFMPEG_TIMEOUT)
            if tmp_path.stat().st_size == 0:
                raise RuntimeError("ffmpeg produced an empty file; nothing was saved")
            os.replace(tmp_path, out_path)
            return str(out_path)
        except subprocess.TimeoutExpired:
            logger.error(f"Extraction timed out for {out_path}")
            raise
        except subprocess.CalledProcessError as e:
            # Re-raise WITH the captured stderr so the recorded issue says why.
            stderr = e.stderr
            if isinstance(stderr, (bytes, bytearray)):
                stderr = stderr.decode("utf-8", "replace")
            stderr = (stderr or "").strip()
            raise RuntimeError(
                f"ffmpeg failed (exit {e.returncode}): "
                f"{stderr or 'no error output'}"
            ) from e
        finally:
            # A just-killed ffmpeg can still briefly hold the handle on Windows;
            # an orphaned temp is harmless (ignored everywhere) but try twice.
            for _ in range(2):
                if not tmp_path.exists():
                    break
                try:
                    tmp_path.unlink()
                except OSError:
                    time.sleep(0.2)
