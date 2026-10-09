"""FFmpeg/ffprobe location + common languages for extraction."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple, Set

from .runtime import *
from .runtime import _subprocess_creation_flags

__all__ = ["FFmpegLocator", "COMMON_LANGS"]




# ----------------------------------------
# FFmpeg Locator
# ----------------------------------------
_FFMPEG_NOT_FOUND_TTL = 60.0  # Re-search after 60 seconds
VERIFY_TIMEOUT_S = 30


class FFmpegLocator:
    """Locate ffmpeg and ffprobe in the script folder or system PATH."""
    _cached: Dict[str, Optional[str]] = {}
    _not_found_ts: Dict[str, float] = {}  # timestamp of last "not found" per binary
    _verified: Set[str] = set()  # paths verified this process; skip re-probing
    _cache_loaded: bool = False
    _lock: threading.Lock = threading.Lock()

    @classmethod
    def _load_cache(cls) -> None:
        """Load cached FFmpeg paths from file."""
        with cls._lock:
            if cls._cache_loaded:
                return
            cls._cache_loaded = True
            
            if FFMPEG_CACHE_PATH.exists():
                try:
                    with open(FFMPEG_CACHE_PATH, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    # A valid-but-non-object cache (null/list/number) would make
                    # the later cls._cached.get(name) raise AttributeError and
                    # kill the locate thread / extraction worker. Coerce to {}.
                    if isinstance(loaded, dict):
                        cls._cached = loaded
                    else:
                        logger.warning("ffmpeg cache is not a JSON object; ignoring")
                        cls._cached = {}
                    logger.info(f"Loaded ffmpeg cache from {FFMPEG_CACHE_PATH}")
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON in ffmpeg cache: {e}")
                    cls._cached = {}
                except IOError as e:
                    logger.error(f"Failed to load ffmpeg cache: {e}")
                    cls._cached = {}

    @classmethod
    def _save_cache(cls) -> None:
        """Save FFmpeg paths to cache file (atomic; snapshot taken under lock)."""
        try:
            with cls._lock:
                snapshot = dict(cls._cached)
            # Unique temp per call: get_binary runs concurrently (locate thread +
            # extraction workers), so a fixed temp name would let two savers
            # clobber each other's staging file or collide on os.replace.
            fd, tmp = tempfile.mkstemp(
                dir=str(FFMPEG_CACHE_PATH.parent), prefix=".ffcache_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f, indent=2)
                os.replace(tmp, str(FFMPEG_CACHE_PATH))
                tmp = None
            finally:
                if tmp and os.path.exists(tmp):
                    os.unlink(tmp)
            logger.info(f"Saved ffmpeg cache to {FFMPEG_CACHE_PATH}")
        except IOError as e:
            logger.error(f"Failed to save ffmpeg cache: {e}")

    @classmethod
    def _verify_binary(cls, path: str) -> bool:
        """Verify that a binary is valid and executable."""
        try:
            subprocess.run(
                [path, "-version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                creationflags=_subprocess_creation_flags(),
                # The first start of a freshly installed ffmpeg (eight new
                # files, scanned by the antivirus) took longer than 5 s in a
                # clean Windows Sandbox; a short timeout reported the bundled
                # FFmpeg as missing.
                timeout=VERIFY_TIMEOUT_S,
            )
            return True
        except subprocess.TimeoutExpired:
            logger.warning(f"{path} -version did not answer within {VERIFY_TIMEOUT_S} s")
            return False
        except (subprocess.CalledProcessError, OSError):
            return False

    @classmethod
    def get_binary(cls, name: str) -> Optional[str]:
        """
        Get path to FFmpeg binary.
        
        Args:
            name: Binary name ('ffmpeg' or 'ffprobe')
            
        Returns:
            Path to binary or None if not found
        """
        cls._load_cache()

        # Check cache first. Mutations of the shared class state are guarded by
        # _lock (get_binary runs concurrently from the startup locate thread and
        # extraction workers); the slow subprocess verify runs outside the lock.
        with cls._lock:
            cached_path = cls._cached.get(name)
            already_verified = cached_path in cls._verified if cached_path else False
        if cached_path:
            # A path already verified this process is trusted without
            # re-spawning `<bin> -version` on every call (per-file hot path).
            if already_verified or cls._verify_binary(cached_path):
                with cls._lock:
                    cls._verified.add(cached_path)
                return cached_path
            logger.warning(f"Cached path for {name} is invalid, re-searching...")
            with cls._lock:
                # Only evict if another thread hasn't already written a fresh,
                # valid path while we were verifying outside the lock — an
                # unconditional pop would erase that good value.
                if cls._cached.get(name) == cached_path:
                    cls._cached.pop(name, None)
                cls._verified.discard(cached_path)

        # Negative-result TTL: skip re-search if recently checked. No default
        # timestamp: monotonic() counts from boot, so a 0.0 "never missed"
        # blocked every lookup in the first minute after Windows started.
        with cls._lock:
            last_miss = cls._not_found_ts.get(name)
        if last_miss is not None and time.monotonic() - last_miss < _FFMPEG_NOT_FOUND_TTL:
            return None

        exe_name = f"{name}.exe" if IS_WINDOWS else name
        # Search the app root (where the .pyw launcher and bundled bin/ live) and
        # its bin/ subdir. app_root() is the shared resolver in runtime — it
        # handles both the source-run (package parent) and frozen-build cases, so
        # this stays correct after the package split that moved __file__ down a
        # level (the regression this replaced searched anime_subs/bin/).
        root = app_root()
        for search_dir in (root, root / "bin"):
            candidate = search_dir / exe_name
            if candidate.exists() and cls._verify_binary(str(candidate)):
                logger.info(f"{name} found at: {candidate}")
                with cls._lock:
                    cls._cached[name] = str(candidate)
                    cls._verified.add(str(candidate))
                cls._save_cache()
                return str(candidate)

        # Check system PATH — resolve to an ABSOLUTE path via shutil.which and
        # reject a hit in the current working directory. Launching the bare name
        # ("ffmpeg.exe") would let Windows' CreateProcess search the CWD first,
        # so a malicious ffmpeg.exe dropped next to a downloaded video (CWD =
        # that folder) could be executed. Resolving + rejecting CWD closes that.
        resolved = shutil.which(exe_name)
        if resolved:
            resolved_abs = os.path.abspath(resolved)
            try:
                in_cwd = (
                    os.path.dirname(resolved_abs) == os.path.abspath(os.getcwd())
                )
            except OSError:
                in_cwd = False
            if in_cwd:
                logger.warning(
                    f"Ignoring {name} found in the current working directory "
                    f"({resolved_abs}); refusing to run a binary from CWD."
                )
            elif cls._verify_binary(resolved_abs):
                logger.info(f"{name} found in system PATH: {resolved_abs}")
                with cls._lock:
                    cls._cached[name] = resolved_abs
                    cls._verified.add(resolved_abs)
                cls._save_cache()
                return resolved_abs

        logger.warning(f"{name} not found in script folder, bin/, or system PATH.")
        with cls._lock:
            cls._not_found_ts[name] = time.monotonic()
        return None


# ----------------------------------------
# Common Languages for Extraction (ISO-639-2 codes)
# ----------------------------------------
COMMON_LANGS: List[Tuple[str, str]] = [
    ("eng", "English"), ("ara", "Arabic"), ("spa", "Spanish"), ("fra", "French"),
    ("deu", "German"), ("ita", "Italian"), ("jpn", "Japanese"), ("kor", "Korean"),
    ("zho", "Chinese"), ("rus", "Russian"), ("tur", "Turkish"), ("por", "Portuguese"),
    ("hin", "Hindi"), ("urd", "Urdu"), ("tha", "Thai"), ("vie", "Vietnamese"),
]
