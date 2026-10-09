"""One-time import of settings from another layout (ADR-003).

`--import-from <folder>` copies the settings, font caches and file index from
an older layout (a source checkout, a portable copy) into the current data
folder. The source is never modified: the index and its write-ahead log are
first copied to a temp folder and read from there with SQLite's backup API
(never a raw copy of a live -wal file). Anything already in the destination is
moved to import-backup-<time>\\ first.
"""
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from .runtime import logger

__all__ = ["import_settings"]

# Settings and caches worth carrying over. ffmpeg_cache.json is left out on
# purpose: it points at the OLD layout's FFmpeg, not the installed one.
_PLAIN_FILES = ("config.json", "font_path_cache.json", "font_support_cache.json")
_DB = "files_monitor.db"


def import_settings(src: Path, dst: Path, app_running: bool) -> Dict[str, Any]:
    """Import from `src` into `dst`; returns a report dict ("ok", "imported",
    "previous_data_kept_in", "error")."""
    report: Dict[str, Any] = {"ok": False, "source": str(src), "destination": str(dst),
                              "imported": [], "previous_data_kept_in": None}
    try:
        src, dst = Path(src).resolve(), Path(dst).resolve()
    except OSError as e:
        report["error"] = f"bad path: {e}"
        return report
    if app_running:
        report["error"] = "Anime Subs Customizer is running; quit it first (tray icon > Quit)"
        return report
    if src == dst:
        report["error"] = "the source is the current data folder"
        return report
    if not (src / "config.json").is_file():
        report["error"] = f"no config.json in {src}"
        return report

    dst.mkdir(parents=True, exist_ok=True)
    present = [n for n in (*_PLAIN_FILES, _DB, _DB + "-wal", _DB + "-shm")
               if (dst / n).exists()]
    if present:
        keep = dst / f"import-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        keep.mkdir()
        for name in present:
            shutil.move(str(dst / name), str(keep / name))
        report["previous_data_kept_in"] = str(keep)

    for name in _PLAIN_FILES:
        if (src / name).is_file():
            shutil.copy2(src / name, dst / name)
            report["imported"].append(name)

    if (src / _DB).is_file():
        with tempfile.TemporaryDirectory() as tmp:
            # Copy the db with its -wal/-shm (a consistent set while the old app
            # is stopped), open the COPY, and write the destination through the
            # backup API, which folds the log in.
            for suffix in ("", "-wal", "-shm"):
                if (src / (_DB + suffix)).is_file():
                    shutil.copy2(src / (_DB + suffix), Path(tmp) / (_DB + suffix))
            source = sqlite3.connect(Path(tmp) / _DB)
            target = sqlite3.connect(dst / _DB)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
        report["imported"].append(_DB)

    report["ok"] = True
    logger.info(f"Imported {', '.join(report['imported'])} from {src}")
    return report
