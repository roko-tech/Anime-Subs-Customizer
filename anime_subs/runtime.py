"""Foundation layer: constants, paths, logging, IO helpers, security,
dataclasses (AppConfig/SubtitleStream/Issue), BiDi helpers. No dependencies on
other anime_subs modules."""
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque, Counter
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import Optional, Set, Dict, List, Any, Callable, Tuple, Deque
from enum import Enum

try:
    import chardet
except ImportError:
    chardet = None

from PIL import Image, ImageDraw, ImageFont

# Explicit public API. This bounds `from .runtime import *` to runtime's OWN
# domain names — it deliberately does NOT re-export the stdlib/PIL modules
# imported above. Consequence: a sibling module that uses e.g. `hashlib`, `os`,
# or `Counter` must import it itself; relying on it leaking through the star now
# fails LOUDLY at import time instead of as a delayed runtime NameError (the
# class of bug the package split introduced — see tkFont/ffmpeg/hashlib). Keep
# this list in sync when adding a public runtime symbol; underscore helpers are
# imported explicitly by the modules that need them.
__all__ = [
    "APP_NAME", "APP_VERSION", "IS_WINDOWS", "START_MINIMIZED",
    "DEFAULT_PREVIEW_TEXT",
    "BASE_OUTLINE_SIZE", "BASE_SHADOW_SIZE", "REFERENCE_RES_Y",
    "DIALOGUE_OUTLINE_COLOUR", "DIALOGUE_SHADOW_COLOUR",
    "DIALOGUE_SIZE_FRACTIONS", "REFERENCE_SIZE_RATIO",
    "REFERENCE_SIZE_RATIO_ARABIC",
    "SIZE_NUDGE_MIN", "SIZE_NUDGE_MAX", "SIZE_NUDGE_STEP", "normalize_size_nudge",
    "DB_POOL_SIZE", "DB_TIMEOUT", "BATCH_SIZE", "MAX_ASS_FILE_SIZE",
    "FILE_STABLE_TIMEOUT", "FILE_STABLE_CHECK_INTERVAL", "FFMPEG_TIMEOUT",
    "VIDEO_EXTENSIONS", "BACKUP_SUBTITLE_GLOBS", "SUPPORTED_ENCODINGS",
    "COLOR_TAG_PATTERN", "COLOR_CODE_PATTERN", "PLAYRES_X_PATTERN", "PLAYRES_Y_PATTERN",
    "SCALED_BORDER_PATTERN",
    "APPDATA_DIR", "DB_PATH", "CONFIG_FILE_PATH", "ICON_PATH", "LOG_PATH",
    "FFMPEG_CACHE_PATH", "logger", "configure_logging", "migrate_legacy_appdata",
    "INSTANCE_TOKEN", "PORTABLE_MARKER_NAME", "instance_object_name", "is_frozen",
    "FileStatus", "ASSStyleFields", "AppConfig", "SubtitleStream", "SecurityUtils",
    "Issue", "IssueTracker", "record_error", "record_warning",
    "app_root", "atomic_write", "run_tracked", "terminate_tracked_processes", "splitlines_keepends_ass", "file_hash",
    "ensure_default_icon", "folder_key", "bgr_to_rgb_hex", "rgb_to_bgr_hex", "format_ass_primary_color",
    "has_rtl_chars", "wrap_rtl_if_needed",
]


def file_hash(file_path: Path) -> Optional[str]:
    """Cheap content fingerprint (sha1 hex) of a subtitle file, or None on
    error / oversized file. Lives here (not on FileProcessor) so the db layer
    can use it without importing the ASS editor."""
    try:
        if file_path.stat().st_size > MAX_ASS_FILE_SIZE:
            return None
        with open(file_path, "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return None



# Sentinel distinguishing "no cached verdict yet" from a cached False in the
# font-support cache (so a transient load failure isn't mistaken for "checked
# and unsupported").
_CACHE_MISSING = object()

_chardet_warned = False


def _warn_chardet_missing() -> None:
    """Log a one-time warning that encoding detection is degraded."""
    global _chardet_warned
    if not _chardet_warned:
        _chardet_warned = True
        logger.warning(
            "chardet is not installed — encoding detection is degraded; "
            "no-BOM non-UTF-8 subtitles may be mis-decoded. "
            "Install it: pip install chardet"
        )

# ----------------------------------------
# Constants and Configuration
# ----------------------------------------
APP_NAME = "Anime Subs Customizer"
APP_VERSION = "1.2.0"
IS_WINDOWS = sys.platform.startswith("win")
START_MINIMIZED = "--minimized" in sys.argv

# Default preview text (centralized): a bilingual sample — the Arabic phrase
# and its English translation. The preview draws each line in the chosen font
# and hides (with a note) any line the font can't cover, so it stays an honest
# picture of the font. The font picker keys its coverage filter off line 1 only.
DEFAULT_PREVIEW_TEXT = (
    "سبحان الله وبحمده، سبحان الله العظيم\n"
    "So remember Me; I will remember you"
)

# Base outline/shadow sizes at 720p reference
BASE_OUTLINE_SIZE = 2.0
BASE_SHADOW_SIZE = 1.0
REFERENCE_RES_Y = 720

# Dialogue outline + shadow COLOUR (ASS &HAABBGGRR). The app owns the fill colour
# and the outline/shadow SIZE; it also pins these to opaque black so restyled
# subs get a crisp, uniform black edge instead of whatever tint they shipped with
# (e.g. some releases use a dark-grey outline that reads washed-out).
DIALOGUE_OUTLINE_COLOUR = "&H00000000"
DIALOGUE_SHADOW_COLOUR = "&H00000000"

# Optional dialogue-Fontsize presets, expressed as a fraction of the script's
# PlayResY, so the SAME on-screen size lands regardless of each file's
# resolution. Applied only to dialogue styles (never signs/typesetting).
# "none" (the default) leaves every file's own Fontsize untouched.
#
# Fraction of the script's PlayResY. "medium" ≈ the real library's dialogue
# median (7.2% of frame height), so switching a library to Medium keeps its
# familiar size while making every file consistent. Small/large are spaced
# ~geometrically around it (ratio ≈1.28) so each step is a clearly *perceptible*
# size change rather than a near-linear nudge.
DIALOGUE_SIZE_FRACTIONS = {
    "small": 0.055,
    "medium": 0.070,
    "large": 0.090,
}

# Fonts fill their em-square with visible ink to very different degrees, so the
# SAME ASS Fontsize renders very different visible sizes per font. The sizer
# divides out each font's measured reading-height ratio (FontSupport.size_ratio)
# so a preset is the same on-screen height in every font, anchored to a "typical"
# ratio so a typical font keeps ~its size and only outliers move.
#
# The two scripts use DIFFERENT reading metrics on different scales, each with its
# own anchor: Latin = cap-height (~0.70 of em); Arabic = the reading BODY band of
# shaped running text (~0.215 of em — much smaller, since the body excludes the
# ascenders/descenders/flourishes). The Arabic anchor is the median body across
# installed Arabic fonts, so a typical Arabic font keeps its size while delicate
# small-body faces (e.g. Scheherazade New) grow to match. Because Arabic is sized
# by its body and Latin by its cap, Arabic renders somewhat larger than Latin on
# screen — intended for an Arabic-first library.
REFERENCE_SIZE_RATIO = 0.70          # Latin cap-height anchor
REFERENCE_SIZE_RATIO_ARABIC = 0.215  # Arabic body-band anchor

# Per-font size nudge: a manual multiplier the user sets once per font family to
# correct the part of "how big it feels" that no single Fontsize can equalize —
# stroke weight, width, and flourish length differ between typefaces and are
# beyond a 1-D size knob (this is exactly what CSS font-size-adjust leaves to the
# author). It multiplies the normalized Fontsize. 1.0 = neutral (no change).
SIZE_NUDGE_MIN = 0.50
SIZE_NUDGE_MAX = 1.50
SIZE_NUDGE_STEP = 0.05


def normalize_size_nudge(mult: float) -> float:
    """Clamp a nudge to [MIN, MAX] and snap it to the STEP grid, so a stored
    value is always one of the discrete stops. A hand-edited float that fell
    between stops could otherwise cross a round() boundary differently in the
    write vs the idempotency-check path and re-process the file forever."""
    m = min(SIZE_NUDGE_MAX, max(SIZE_NUDGE_MIN, float(mult)))
    return round(round(m / SIZE_NUDGE_STEP) * SIZE_NUDGE_STEP, 2)

# Database connection pool settings
DB_POOL_SIZE = 5
DB_TIMEOUT = 30.0

# File processing settings
BATCH_SIZE = 100
# Upper bound for a file we'll treat as an ASS subtitle. Real ones are
# kilobytes; this guards the unattended monitor against a huge/hostile file.
MAX_ASS_FILE_SIZE = 64 * 1024 * 1024  # 64 MB
FILE_STABLE_TIMEOUT = 60
FILE_STABLE_CHECK_INTERVAL = 5
FFMPEG_TIMEOUT = 300

# Video file extensions
VIDEO_EXTENSIONS: frozenset = frozenset({
    ".mkv", ".mp4", ".avi", ".mov", ".flv",
    ".wmv", ".m4v", ".mpg", ".mpeg"
})

# Subtitle extensions that copy_original_file may back up into "Original Subs/"
# (.ass/.ssa from restyling; .srt/.sup from extraction-collision backups). The
# restore enumeration must glob ALL of these, not just .ass, or a backed-up
# .srt/.sup is invisible to — and unrecoverable by — Restore Originals.
BACKUP_SUBTITLE_GLOBS: Tuple[str, ...] = ("*.ass", "*.ssa", "*.srt", "*.sup")
# Of those, the text formats whose restore we gate on decodability; .sup is
# binary (PGS) so the decodability check would wrongly reject it.
_TEXT_SUBTITLE_SUFFIXES: frozenset = frozenset({".ass", ".ssa", ".srt"})

# Supported encodings for detection, ordered by "preferred for subtitles".
# cp1256 (Arabic) leads by deliberate bias toward this app's primary
# workload. Detection accuracy for non-Arabic legacy encodings (CJK,
# Cyrillic) rests on chardet — a declared dependency that resolves those at
# high confidence; this list is the best-effort fallback for when chardet is
# unsure or unavailable. utf-8 is always tried first implicitly; each codec
# is listed once (Python aliases like cp1256 / windows-1256 resolve to the
# same decoder). UTF-16/32 variants without a BOM are handled by the BOM
# dispatch path.
SUPPORTED_ENCODINGS: Tuple[str, ...] = (
    "utf-8",
    "cp1256",          # Arabic
    "cp1252",          # Western European
    "cp1251",          # Cyrillic
    "cp1250",          # Central European
    "cp874",           # Thai
    "shift_jis",       # Japanese
    "euc-kr",          # Korean
    "gb18030",         # Chinese (simplified + traditional supported)
    "big5",            # Chinese (traditional)
)


class FileStatus(Enum):
    """Status values for monitored files."""
    INDEXED = "indexed"
    PROCESSED = "processed"
    # Put back by Restore: Watch leaves it alone until its content changes.
    RESTORED = "restored"
    # Changed by another program into styled output with other settings: left
    # alone (no tug-of-war) until its content changes again (ADR-007).
    FOREIGN = "foreign"


class ASSStyleFields:
    """Canonical ASS V4+ Style field layout.

    The index constants describe the *canonical* order used when an ASS file
    has no valid Format: line; real files may reorder the fields, in which
    case the code indexes via a name→column map built from the file's own
    Format: line. See FileProcessor._field_map_from_format_fields.
    """
    NAME = 0
    FONTNAME = 1
    FONTSIZE = 2
    PRIMARY_COLOR = 3
    SECONDARY_COLOR = 4
    OUTLINE_COLOR = 5
    BACK_COLOR = 6
    BOLD = 7
    ITALIC = 8
    UNDERLINE = 9
    STRIKEOUT = 10
    SCALE_X = 11
    SCALE_Y = 12
    SPACING = 13
    ANGLE = 14
    BORDER_STYLE = 15
    OUTLINE = 16
    SHADOW = 17
    ALIGNMENT = 18
    MARGIN_L = 19
    MARGIN_R = 20
    MARGIN_V = 21
    ENCODING = 22

    FIELD_COUNT = 23

    # Canonical Format line field names (order matches indices above).
    FIELD_NAMES: Tuple[str, ...] = (
        "Name", "Fontname", "Fontsize", "PrimaryColour",
        "SecondaryColour", "OutlineColour", "BackColour",
        "Bold", "Italic", "Underline", "StrikeOut", "ScaleX",
        "ScaleY", "Spacing", "Angle", "BorderStyle",
        "Outline", "Shadow", "Alignment", "MarginL",
        "MarginR", "MarginV", "Encoding",
    )


# Compiled regex patterns (cached at module level for performance)
COLOR_TAG_PATTERN = re.compile(r"\{\\c&H[0-9A-Fa-f]{6}&\}")
COLOR_CODE_PATTERN = re.compile(r"^[0-9A-Fa-f]{6}$")
# Anchored to the start of the (lstripped) line so a `PlayResY:` token
# embedded in Dialogue/Comment text can't masquerade as the header value.
PLAYRES_X_PATTERN = re.compile(r"^\s*playresx\s*:\s*(\d+)", re.IGNORECASE)
PLAYRES_Y_PATTERN = re.compile(r"^\s*playresy\s*:\s*(\d+)", re.IGNORECASE)
SCALED_BORDER_PATTERN = re.compile(
    r"^\s*scaledborderandshadow\s*:\s*(\S+)", re.IGNORECASE
)


# ----------------------------------------
# Path Configuration
# ----------------------------------------
def is_frozen() -> bool:
    """True when running as a packaged (PyInstaller) build rather than from source."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """The application root — the directory that holds the .pyw launcher and the
    bundled bin/ folder.

    Single source of truth for "where the app lives", used by both the data-dir
    resolver and the FFmpeg locator. runtime.py sits one level down inside the
    anime_subs package, so the source-run case is ``__file__.parent.parent``
    (before the package split this file was at the repo root). Under a frozen
    build (PyInstaller) __file__ points inside the bundle, so use the executable's
    directory instead.
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


# Test-only isolation. When ASC_INSTANCE_TOKEN is set, the data folder moves to a
# private temp dir and the single-instance objects get the token as a suffix, so a
# test build can run next to a real copy without sharing its settings, database or
# mutex. Never set by the installer; --self-test makes up its own token.
_TOKEN_ENV = "ASC_INSTANCE_TOKEN"
_TOKEN_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")

# A file with this name beside the exe keeps a packaged build's data next to it
# (the portable ZIP ships one); without it an installed build uses LOCALAPPDATA.
PORTABLE_MARKER_NAME = "portable.marker"


def _read_instance_token(environ: Any = os.environ, argv: Any = sys.argv) -> str:
    """The sanitized instance token ("" when unset). --self-test invents one so a
    self-check never touches a real install's data."""
    token = _TOKEN_UNSAFE.sub("", environ.get(_TOKEN_ENV, ""))[:40]
    if not token and "--self-test" in argv:
        token = f"selftest-{os.getpid()}"
    return token


INSTANCE_TOKEN = _read_instance_token()


def instance_object_name(base: str, token: Optional[str] = None) -> str:
    """Name of a per-instance kernel object (mutex/event): `base`, plus the
    instance token as a suffix when one is set."""
    tok = INSTANCE_TOKEN if token is None else token
    return f"{base}-{tok}" if tok else base


def _data_dir_candidates(
    *, frozen: bool, root: Path, token: str, local: Optional[str], temp: str
) -> List[Path]:
    """Data folders to try, in order (the first writable one wins).

    - token set: a private temp folder only, so tests never share real data;
    - packaged build: %LOCALAPPDATA% (the install folder is replaced on every
      upgrade), unless a portable marker sits beside the exe;
    - source run: beside the app (the developer layout), then %LOCALAPPDATA%.
    The temp folder is the last resort so the app still starts.
    """
    if token:
        return [Path(temp) / f"asc-instance-{token}"]
    candidates: List[Path] = []
    if not frozen or (root / PORTABLE_MARKER_NAME).exists():
        candidates.append(root)
    if local:
        candidates.append(Path(local) / APP_NAME)
    candidates.append(Path(temp) / APP_NAME)
    return candidates


def _resolve_appdata_dir() -> Path:
    """Pick a writable directory for the app's data files (see
    _data_dir_candidates for the order)."""
    candidates = _data_dir_candidates(
        frozen=is_frozen(),
        root=app_root(),
        token=INSTANCE_TOKEN,
        local=os.getenv("LOCALAPPDATA") if IS_WINDOWS else None,
        temp=tempfile.gettempdir(),
    )
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return d
        except OSError:
            continue
    # Nothing was writable; return the first choice and let later writes
    # surface their own errors.
    return candidates[0]


APPDATA_DIR = _resolve_appdata_dir()

DB_PATH = APPDATA_DIR / "files_monitor.db"
CONFIG_FILE_PATH = APPDATA_DIR / "config.json"


def _icon_path() -> Path:
    """The app icon: the copy shipped with the app (beside the .pyw, or in a
    packaged build's resource folder); a generated one in the data folder only
    when none was shipped."""
    shipped = Path(getattr(sys, "_MEIPASS", app_root())) / "icon.ico"
    return shipped if shipped.exists() else APPDATA_DIR / "icon.ico"


ICON_PATH = _icon_path()
LOG_PATH = APPDATA_DIR / "monitor.log"
FFMPEG_CACHE_PATH = APPDATA_DIR / "ffmpeg_cache.json"


def migrate_legacy_appdata() -> None:
    """One-time move of data left in the very old %APPDATA% location. Called by
    the app's entry point, never at import, and skipped for token (test) runs so
    a test can't move a real user's files into its throwaway folder."""
    appdata = os.getenv("APPDATA")
    # Without APPDATA the "old" folder would be a RELATIVE path in the current
    # directory, and files there would be moved into the data folder.
    if not IS_WINDOWS or INSTANCE_TOKEN or not appdata:
        return
    old_dir = Path(appdata) / APP_NAME
    if not old_dir.is_dir() or old_dir == APPDATA_DIR:
        return
    for fname in ("files_monitor.db", "config.json", "icon.ico",
                  "monitor.log", "ffmpeg_cache.json"):
        src = old_dir / fname
        dst = APPDATA_DIR / fname
        if src.exists() and not dst.exists():
            try:
                shutil.move(str(src), str(dst))
            except Exception:
                pass
    # Remove old directory if empty
    try:
        old_dir.rmdir()
    except OSError:
        pass


# ----------------------------------------
# Logging Configuration
# ----------------------------------------
logger = logging.getLogger(__name__)
_logging_configured = False


def configure_logging() -> None:
    """Send log records to the rotating monitor.log (and to the console when
    there is one). Called once by the app's entry point, not at import, so
    importing the engine (tests, --quit, tools) never opens or creates the log.
    A windowed build has no stderr, so the console handler is added only when
    one exists."""
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True
    handlers: List[logging.Handler] = [
        RotatingFileHandler(
            LOG_PATH,
            maxBytes=1_000_000,
            backupCount=5,
            encoding="utf-8"
        )
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )
    if INSTANCE_TOKEN:
        logger.warning(
            f"TEST INSTANCE '{INSTANCE_TOKEN}': data folder is {APPDATA_DIR}"
        )


# ----------------------------------------
# Issue Tracker (recent-issues panel backend)
# ----------------------------------------
@dataclass
class Issue:
    """One user-visible processing failure."""
    timestamp: float        # wall-clock seconds since epoch
    operation: str          # "decode" | "apply" | "extract" | "restore" | ...
    filename: str
    reason: str


class IssueTracker:
    """
    Thread-safe ring buffer of recent processing failures, backing the
    "Recent Issues" button in the status bar. Keeps the UI decoupled from
    the monitor/extraction threads — anyone can append from any thread;
    the UI polls via on_change callbacks dispatched to the Tk main loop.
    """
    _MAX_ISSUES = 50
    # The same failure again for the same file (a retry of a file a player
    # still holds open) is not a new problem; show it once per half hour.
    _REPEAT_WINDOW = 1800.0
    _issues: Deque[Issue] = deque(maxlen=_MAX_ISSUES)
    _lock: threading.Lock = threading.Lock()
    _listeners: List[Callable[[], None]] = []  # noqa: RUF012

    @classmethod
    def record(cls, operation: str, filename: str, reason: str) -> None:
        """Append one issue and notify any registered listeners."""
        issue = Issue(time.time(), operation, filename, reason)
        with cls._lock:
            for prev in reversed(cls._issues):
                if (prev.operation, prev.filename, prev.reason) == (operation, filename, reason):
                    if issue.timestamp - prev.timestamp < cls._REPEAT_WINDOW:
                        return
                    break
            cls._issues.append(issue)
            listeners = list(cls._listeners)
        for cb in listeners:
            try:
                cb()
            except Exception as e:  # noqa: BLE001
                # Don't let a flaky listener block issue recording.
                logger.debug(f"IssueTracker listener failed: {e}")

    @classmethod
    def get_issues(cls) -> List[Issue]:
        with cls._lock:
            return list(cls._issues)

    @classmethod
    def count(cls) -> int:
        with cls._lock:
            return len(cls._issues)

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._issues.clear()
            listeners = list(cls._listeners)
        for cb in listeners:
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass

    @classmethod
    def add_listener(cls, callback: Callable[[], None]) -> None:
        with cls._lock:
            cls._listeners.append(callback)


def record_error(operation: str, filename: str, reason: str) -> None:
    """Log an error AND surface it in the UI's issues panel."""
    logger.error(f"{operation} failed for {filename}: {reason}")
    IssueTracker.record(operation, filename, reason)


def record_warning(operation: str, filename: str, reason: str) -> None:
    """Log a WARNING and surface it in the issues panel — for a non-fatal
    issue where the operation itself SUCCEEDED (e.g. a font-coverage note),
    unlike record_error which is for outright failures."""
    logger.warning(f"{operation}: {filename}: {reason}")
    IssueTracker.record(operation, filename, reason)


# ----------------------------------------
# Data Classes for Configuration
# ----------------------------------------
@dataclass
class AppConfig:
    """Application configuration persisted to config.json.

    load() filters unknown keys and validates the colour code; other fields
    are taken as-is from the JSON (no full type coercion).
    """
    font: Optional[str] = None
    color: Optional[str] = None
    folder: Optional[str] = None
    font_bold: bool = False
    font_italic: bool = False
    font_underline: bool = False
    font_size: str = "none"          # "none" | "small" | "medium" | "large"
    extract_enabled: bool = False
    extract_langs: str = ""
    preview_text: str = DEFAULT_PREVIEW_TEXT
    start_minimized: bool = False
    ui_language: str = ""            # "" = not chosen yet (first run decides)
    favorite_fonts: List[str] = field(default_factory=list)
    preview_bg_enabled: bool = False
    preview_bg_from_sub: bool = False
    font_size_nudges: Dict[str, float] = field(default_factory=dict)  # {family: multiplier}
    # Folders the user confirmed restyling (normalised paths): Watch asks once
    # per folder before the first restyle, and autostart never asks.
    consented_folders: List[str] = field(default_factory=list)
    # config.json layout version (1 = adds consented_folders). Configs without
    # it come from an earlier release: see AppConfig.load.
    schema_version: int = 1
    welcome_done: bool = False          # the first-run welcome was shown
    tray_hint_shown: bool = False       # the one-time "still running" balloon
    update_check: bool = False          # opt-in daily update check (ADR-008)
    last_update_check: float = 0.0
    notified_update: str = ""           # last version the user was told about

    @classmethod
    def load(cls) -> 'AppConfig':
        """Load configuration from file."""
        if not CONFIG_FILE_PATH.exists():
            return cls()
        try:
            with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
            filtered_data = {k: v for k, v in data.items() if k in valid_keys}
            cfg = cls(**filtered_data)
            if not isinstance(cfg.consented_folders, list):
                cfg.consented_folders = []
            cfg.consented_folders = [p for p in cfg.consented_folders if isinstance(p, str)]
            if "schema_version" not in data:
                # A config from before the first-watch confirmation existed:
                # its folder was already being watched, so don't nag after the
                # upgrade.
                if cfg.folder and isinstance(cfg.folder, str) and (
                        cfg.extract_enabled or (cfg.font and cfg.color)):
                    key = folder_key(cfg.folder)
                    if key not in cfg.consented_folders:
                        cfg.consented_folders.append(key)
                cfg.schema_version = 1
            # A hand-edited / partially-corrupted colour would otherwise flow
            # unvalidated into bgr_to_rgb_hex and crash the GUI build with a
            # Tcl colour error. Drop it to the default rather than trust it.
            if cfg.color is not None:
                try:
                    cfg.color = SecurityUtils.sanitize_color_code(cfg.color)
                except (ValueError, TypeError, AttributeError):
                    # AttributeError: a non-string colour (JSON number/bool/list)
                    # makes sanitize_color_code call .strip() on a non-str. Without
                    # catching it here it would escape to the outer handler and
                    # reset the ENTIRE config to defaults — silently wiping
                    # favorites/folder/nudges on the next save. Drop just the colour.
                    logger.warning(f"Ignoring invalid colour in config: {cfg.color!r}")
                    cfg.color = None
            # A hand-edited/unknown size preset must be one we recognise, or the
            # apply path would treat it as "no resize" anyway — normalise it.
            if cfg.font_size not in ("none", *DIALOGUE_SIZE_FRACTIONS):
                cfg.font_size = "none"
            # An unknown or non-string language means "not chosen": the GUI then
            # follows the installer / Windows display language.
            if cfg.ui_language not in ("", "en", "ar"):
                cfg.ui_language = ""
            # A hand-edited favorites list must stay a list of strings — the
            # font picker iterates it and builds a set from it.
            if isinstance(cfg.favorite_fonts, list):
                cfg.favorite_fonts = [f for f in cfg.favorite_fonts
                                      if isinstance(f, str)]
            else:
                cfg.favorite_fonts = []
            # Per-font size nudges: a hand-edited map must be {str: number}.
            # Snap each to the grid and drop neutral 1.0 entries so the stored
            # dict is minimal and can't drift across a rounding boundary.
            if isinstance(cfg.font_size_nudges, dict):
                snapped = {}
                for fam, mult in cfg.font_size_nudges.items():
                    if isinstance(fam, str) and isinstance(mult, (int, float)):
                        m = normalize_size_nudge(mult)
                        if m != 1.0:
                            snapped[fam] = m
                cfg.font_size_nudges = snapped
            else:
                cfg.font_size_nudges = {}
            return cfg
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file: {e}")
            return cls()
        except Exception as e:
            logger.error(f"Error loading configuration: {e}")
            return cls()
    
    def save(self) -> bool:
        """Save configuration durably (unique temp file + fsync + os.replace).

        Mirrors atomic_write's durability guarantees: a mkstemp temp (so two
        saves can't collide on a fixed name) is fsync'd before the rename, so a
        crash/power-loss right after the replace can't leave a 0-length or torn
        config.json.
        """
        fd = None
        tmp = None
        try:
            CONFIG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(CONFIG_FILE_PATH.parent),
                prefix=".cfg_",
                suffix=".json.tmp",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None  # fd is now owned by the file object
                json.dump(asdict(self), f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(CONFIG_FILE_PATH))
            tmp = None
            logger.info("Configuration saved")
            return True
        except IOError as e:
            logger.error(f"Error saving configuration: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error saving configuration: {e}")
            return False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


@dataclass
class SubtitleStream:
    """Represents a subtitle stream in a video file."""
    index: int
    language: str
    codec: str
    title: str = ""
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SubtitleStream':
        """Create from ffprobe stream data."""
        tags = data.get("tags", {}) or {}
        # ffprobe output is external/untrusted: coerce the index to a
        # non-negative int (it flows into an `-map 0:<idx>` argument and the
        # output filename) and fall back to 0 on a missing/garbage value.
        try:
            index = int(data.get("index", 0))
            if index < 0:
                index = 0
        except (TypeError, ValueError):
            index = 0
        return cls(
            index=index,
            language=tags.get("language", f"stream_{index}"),
            codec=data.get("codec_name", "unknown"),
            title=tags.get("title", "")
        )


# ----------------------------------------
# Security Utilities
# ----------------------------------------
class SecurityUtils:
    """Security-related utility functions."""
    
    @staticmethod
    def validate_path(file_path: Path, base_folder: Optional[Path] = None) -> bool:
        """
        Validate file path to prevent path traversal attacks.

        IMPORTANT: containment is only enforced when `base_folder` is given.
        With `base_folder=None` this performs NO traversal check — it merely
        confirms the path is resolvable — and returns True for any such path.
        Always pass `base_folder` on any path built from untrusted input
        (filenames, archive entries, ffprobe metadata); the None mode is a
        resolve-only convenience, not a security boundary.

        Args:
            file_path: Path to validate
            base_folder: Base folder to restrict paths to (required for any
                traversal-sensitive caller)

        Returns:
            True if path is valid and (when base_folder is given) contained
        """
        try:
            resolved_path = file_path.resolve()

            # If base folder is provided, ensure path is within it. Resolve
            # the base unconditionally: file_path is always resolved above, so
            # an absolute-but-unresolved base (e.g. reached through a Windows
            # junction/symlink) would otherwise mismatch and spuriously reject
            # valid files.
            if base_folder is not None:
                resolved_base = base_folder.resolve()
                try:
                    resolved_path.relative_to(resolved_base)
                except ValueError:
                    logger.warning(
                        f"Path {resolved_path} is outside base folder {resolved_base}"
                    )
                    return False

            return True
        except (OSError, ValueError) as e:
            logger.error(f"Path validation error: {e}")
            return False
    
    @staticmethod
    def sanitize_color_code(color: str) -> str:
        """
        Validate and sanitize color code.
        
        Args:
            color: BGR color code (6 hex characters)
            
        Returns:
            Sanitized uppercase color code
            
        Raises:
            ValueError: If color code is invalid
        """
        if not color:
            raise ValueError("Color code cannot be empty")
        
        color = color.strip().upper()
        if not COLOR_CODE_PATTERN.match(color):
            raise ValueError(f"Invalid color code format: {color}")
        
        return color
    
    # Windows reserved device names
    _WINDOWS_RESERVED = frozenset(
        {"CON", "PRN", "AUX", "NUL"} |
        {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}
    )

    # Characters that have no business appearing in a filename component
    # (control chars, path separators, Windows-reserved punctuation).
    _FILENAME_FORBIDDEN: frozenset = frozenset(
        {chr(c) for c in range(32)} | set('/\\:*?"<>|')
    )

    @staticmethod
    def sanitize_ass_field(value: str) -> str:
        """
        Strip characters that would break an ASS Style/Dialogue row when
        written as one comma-separated column: commas, newlines, tabs,
        and NULLs. Used for attacker-controllable-ish values like the
        user's chosen font name.
        """
        if not value:
            return ""
        cleaned = value.replace(",", "").replace("\n", " ").replace("\r", " ")
        cleaned = cleaned.replace("\t", " ").replace("\x00", "")
        return cleaned.strip()

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """
        Sanitize a single filename *component* — never a path. Strips
        control chars, path separators, and characters that are illegal
        on Windows. Also collapses leading/trailing dots and spaces so the
        result can't escape its directory or become hidden/weird.
        """
        # Drop every forbidden character.
        cleaned = "".join(
            "_" if ch in SecurityUtils._FILENAME_FORBIDDEN else ch
            for ch in filename
        )

        # Strip leading/trailing dots and whitespace (Windows trims these on
        # save, and a leading dot would make the file hidden on Unix).
        cleaned = cleaned.strip(". \t")
        if not cleaned:
            cleaned = "_"

        # Block Windows reserved device names.
        name, ext = os.path.splitext(cleaned)
        if name.upper() in SecurityUtils._WINDOWS_RESERVED:
            name = f"_{name}"
            cleaned = name + ext

        # Limit length.
        max_length = 255
        if len(cleaned) > max_length:
            cleaned = name[:max_length - len(ext)] + ext

        return cleaned


# ----------------------------------------
# Utilities
# ----------------------------------------
def folder_key(folder: str) -> str:
    """A stable comparison key for a folder path (case- and slash-insensitive
    on Windows)."""
    return os.path.normcase(os.path.abspath(folder))


def bgr_to_rgb_hex(bgr: str) -> str:
    """Convert BGR hex string (e.g. 'FF00AA') to '#RGB' hex for display."""
    return f"#{bgr[4:6]}{bgr[2:4]}{bgr[0:2]}"


def rgb_to_bgr_hex(rgb_no_hash: str) -> str:
    """Convert RGB hex string (no '#', e.g. 'AA00FF') to BGR hex for ASS format."""
    return rgb_no_hash[4:] + rgb_no_hash[2:4] + rgb_no_hash[:2]


def format_ass_primary_color(bgr_hex: str) -> str:
    """Build ASS PrimaryColour value from a sanitized BGR hex string."""
    return f"&H00{bgr_hex}"


def _subprocess_creation_flags() -> int:
    """Get subprocess creation flags for hidden window on Windows."""
    if not IS_WINDOWS:
        return 0
    return subprocess.CREATE_NO_WINDOW


# Child processes (ffmpeg/ffprobe) currently running for the app. A quit or an
# installer's --quit stops them: a still-running ffmpeg.exe would keep the
# app's bin\ folder locked and break an upgrade.
_tracked_processes: Set[Any] = set()
_tracked_lock = threading.Lock()


def run_tracked(
    cmd: List[str],
    *,
    timeout: float,
    check: bool = False,
    capture_output: bool = True,
    encoding: Optional[str] = None,
    errors: Optional[str] = None,
    creationflags: Optional[int] = None,
) -> "subprocess.CompletedProcess":
    """subprocess.run with the child registered for terminate_tracked_processes
    (same result, timeout and check semantics)."""
    pipe = subprocess.PIPE if capture_output else None
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=pipe, stderr=pipe,
        encoding=encoding, errors=errors,
        creationflags=_subprocess_creation_flags() if creationflags is None else creationflags,
    )
    with _tracked_lock:
        _tracked_processes.add(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            raise subprocess.TimeoutExpired(cmd, timeout, out, err)
    finally:
        with _tracked_lock:
            _tracked_processes.discard(proc)
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def terminate_tracked_processes() -> None:
    """Kill every running ffmpeg/ffprobe the app started (quit, --quit)."""
    with _tracked_lock:
        procs = list(_tracked_processes)
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass


def ensure_default_icon() -> None:
    """Create a clean app icon at ICON_PATH if it doesn't exist."""
    if ICON_PATH.exists():
        return
    try:
        size = 256
        img = Image.new("RGBA", (size, size), "#22262c")
        draw = ImageDraw.Draw(img)
        radius = 42
        draw.rounded_rectangle(
            [16, 16, size - 16, size - 16], 
            radius, 
            fill="#2f3540"
        )
        draw.rounded_rectangle(
            [size - 120, size - 120, size - 20, size - 20], 
            28, 
            fill="#c9a227"
        )
        try:
            font_main = ImageFont.truetype("seguisb.ttf", 96)
            font_badge = ImageFont.truetype("seguisb.ttf", 56)
        except OSError:
            font_main = ImageFont.load_default()
            font_badge = ImageFont.load_default()
        
        text = "ASC"
        tw = draw.textlength(text, font=font_main)
        draw.text(((size - tw) / 2, 72), text, fill="#e6e6e6", font=font_main)
        draw.text((size - 110, size - 108), "+", fill="#151515", font=font_badge)
        img.save(
            ICON_PATH, 
            format="ICO", 
            sizes=[(16,16), (24,24), (32,32), (48,48), (64,64), (128,128), (256,256)]
        )
        logger.info(f"Created default icon at {ICON_PATH}")
    except Exception as e:
        logger.error(f"Failed creating default icon: {e}")


# UTF-16/32 LE/BE codecs do NOT emit a BOM on encode (only the generic
# "utf-16"/"utf-32"/"utf-8-sig" names do). _decode_bytes returns these explicit
# endian names so the original byte order round-trips; atomic_write re-emits the
# BOM for them by writing a leading U+FEFF, which the explicit codec encodes to
# the correct-endian BOM bytes.
_BOM_PREFIXED_ENCODINGS: frozenset = frozenset(
    {"utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"}
)


# The extra separators str.splitlines() breaks on beyond \r \n \r\n: VT, FF,
# FS, GS, RS, NEL, LINE/PARAGRAPH SEPARATOR. All can legally appear inside an
# ASS Dialogue Text field, so the careful splitter must not break on them — but
# when a file contains NONE of them the C builtin is byte-identical and far
# faster than the per-char Python loop (~30x on a multi-MB file).
_EXTRA_LINE_SEPARATORS_RE = re.compile("[  ]")


def splitlines_keepends_ass(text: str) -> List[str]:
    """Split `text` on real ASS/SSA line terminators only (\\r\\n, \\r, \\n),
    keeping the terminator on each line.

    Unlike str.splitlines(keepends=True), this does NOT treat U+2028/U+2029
    (LINE/PARAGRAPH SEPARATOR), U+0085 (NEL), \\x0b (VT), \\x0c (FF) or the
    FS/GS/RS controls as line breaks. Those characters can legally appear inside
    a Dialogue Text field; splitting on them would mis-place the colour tag (or
    split a logical line in two), so the ASS rewriters must use this stricter
    splitter.
    """
    # Fast path: when none of those extra separators are present (the
    # overwhelmingly common case), str.splitlines(keepends=True) yields exactly
    # the same list and is ~30x faster (a C loop vs the per-char Python one below
    # — measured ~970ms -> ~30ms on a 26MB file). One C-level regex scan gates it.
    if _EXTRA_LINE_SEPARATORS_RE.search(text) is None:
        return text.splitlines(keepends=True)

    out: List[str] = []
    n = len(text)
    start = 0
    i = 0
    while i < n:
        c = text[i]
        if c == "\n":
            out.append(text[start:i + 1])
            i += 1
            start = i
        elif c == "\r":
            if i + 1 < n and text[i + 1] == "\n":
                out.append(text[start:i + 2])
                i += 2
            else:
                out.append(text[start:i + 1])
                i += 1
            start = i
        else:
            i += 1
    if start < n:
        out.append(text[start:n])
    return out


def atomic_write(
    file_path: Path,
    content: List[str],
    encoding: str,
    _max_replace_retries: int = 3,
) -> None:
    """
    Atomically write content to file using secure temporary file.

    Lines are written byte-faithfully: the temp file is opened with newline=''
    so the OS performs NO newline translation. `content` already carries the
    source file's own line terminators (read_text preserves them), so a CRLF
    file round-trips as CRLF instead of becoming '\\r\\r\\n' — and an LF file
    stays LF — on Windows.

    Args:
        file_path: Target file path
        content: Lines to write
        encoding: File encoding
        _max_replace_retries: Max retries for os.replace on Windows lock

    Raises:
        IOError: If write fails
    """
    # Re-emit a BOM for explicit-endian UTF-16/32 so big-endian files don't
    # silently flip to little-endian (and the BOM is preserved). utf-8-sig and
    # the generic utf-16/utf-32 names handle their own BOM via the codec.
    if encoding in _BOM_PREFIXED_ENCODINGS:
        write_content: List[str] = ["\ufeff", *content]
    else:
        write_content = content

    fd = None
    temp_path = None
    try:
        # Create temp file in same directory for atomic move
        fd, temp_path = tempfile.mkstemp(
            dir=file_path.parent,
            prefix='.tmp_',
            suffix=file_path.suffix
        )
        try:
            with os.fdopen(fd, 'w', encoding=encoding, newline='') as f:
                fd = None  # fd is now owned by the file object
                f.writelines(write_content)
                # Force the data to disk before the rename so a crash can't
                # leave the new directory entry pointing at unflushed contents.
                f.flush()
                os.fsync(f.fileno())
        except UnicodeEncodeError:
            # Encoding can't handle the content — retry with UTF-8. Surface the
            # silent charset change so it's diagnosable (the file's effective
            # encoding switches; a player assuming the old charset could mojibake).
            logger.warning(
                f"Content not encodable as {encoding}; "
                f"writing {file_path.name} as UTF-8 instead"
            )
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            fd = None
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
            fd, temp_path = tempfile.mkstemp(
                dir=file_path.parent,
                prefix='.tmp_',
                suffix=file_path.suffix
            )
            # Fallback writes the plain content (no BOM prefix) as UTF-8.
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
                fd = None
                f.writelines(content)
                f.flush()
                os.fsync(f.fileno())

        # Atomic replace on same filesystem (retry on Windows file lock)
        last_err: Optional[Exception] = None
        for attempt in range(_max_replace_retries):
            try:
                os.replace(temp_path, file_path)
                temp_path = None  # Successfully moved
                break
            except PermissionError as e:
                last_err = e
                if attempt < _max_replace_retries - 1:
                    time.sleep(0.1 * (2 ** attempt))
        else:
            raise last_err  # type: ignore[misc]

    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        # Clean up temp file if it still exists
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


# ----------------------------------------
# BiDi helpers
# ----------------------------------------
# Any char in these Unicode blocks forces right-to-left rendering.
_RTL_RANGES: Tuple[Tuple[int, int], ...] = (
    (0x0590, 0x05FF),  # Hebrew
    (0x0600, 0x06FF),  # Arabic
    (0x0700, 0x074F),  # Syriac
    (0x0750, 0x077F),  # Arabic Supplement
    (0x0780, 0x07BF),  # Thaana
    (0x08A0, 0x08FF),  # Arabic Extended-A
    (0xFB1D, 0xFDFF),  # Hebrew + Arabic Presentation Forms-A
    (0xFE70, 0xFEFF),  # Arabic Presentation Forms-B
)


def has_rtl_chars(text: str) -> bool:
    """True if `text` contains any RTL script character."""
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        for lo, hi in _RTL_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def wrap_rtl_if_needed(text: str) -> str:
    """
    Wrap `text` in U+202B (RLE) / U+202C (PDF) when it contains RTL
    characters, so widgets that skip Tk's BiDi pass (e.g. labels forced
    to use a font without script-direction metadata) still display
    Arabic / Hebrew in the correct visual order.
    """
    if has_rtl_chars(text):
        return "\u202B" + text + "\u202C"
    return text
