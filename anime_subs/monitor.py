"""Folder monitoring thread + extraction batching.

FolderMonitor is dependency-injected: its state (AppState), database, and file
processor are passed to the constructor and default to the global singletons.
This lets the data-critical decision loop (`process_ass_files`) be unit-tested
with fakes instead of standing up a real monitor thread, DB, and Tk root.

Change detection is HYBRID. A `watchdog` observer (when installed) wakes the
loop within ~a second of a filesystem change; otherwise the loop falls back to
periodic polling. Crucially, watchdog is only a "wake up and check" signal — it
never decides what to process. The decision stays in `process_ass_files`, which
reconciles each file against its RECORDED state (mtime + content-hash + styled
status). That is what makes repeated/duplicate events (and the app's own styled
writes) harmless: naive watchdog loops because it reacts to every raw OS event;
here a duplicate event just triggers one cheap reconcile that finds nothing to
do. A short settle delay after a wake coalesces the burst of events a single
save/copy emits into one reconcile.
"""
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Optional, Dict, List, Any, Set, Tuple

from .runtime import *
from .db import DatabaseManager
from .ass_editor import FileProcessor
from .media import FFmpegLocator
from .state import app_state

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _WATCHDOG_AVAILABLE = True
except Exception:  # noqa: BLE001 — optional dependency
    _WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # so _WakeHandler can still be defined

__all__ = ["FolderMonitor"]

# Loop timing. When watchdog drives wakes we poll rarely (idle CPU ~zero, react
# in ~1.5s); without it we poll on the legacy 5s cadence.
_WATCH_SETTLE = 1.5     # let a burst of write events settle -> one reconcile
_POLL_WATCH = 30.0      # fallback poll interval while watchdog is active
_POLL_NOWATCH = 5.0     # poll interval when watchdog is unavailable
# A subtitle modified within this window may still be mid-copy; its size is
# confirmed stable before styling (see _subtitle_settled). Files with an older
# mtime are settled, so the initial library scan skips the check entirely.
_ASS_FRESH_WINDOW = 10.0    # seconds since mtime that counts a file as "fresh"
_ASS_SETTLE_INTERVAL = 1.0  # gap between the two size samples for a fresh .ass
_RETRY_FIRST = 60.0         # first retry of a transient failure (file in use, ...)
_RETRY_MAX = 1800.0         # the delay doubles up to this


class _WakeHandler(FileSystemEventHandler):
    """Sets the wake Event on any relevant filesystem change. It deliberately
    does almost nothing — the reconcile loop decides what (if anything) to do,
    so a storm of events, or the app's own styled write, just wakes it once and
    it settles. Ignores our own backup dir and atomic-write/backup temp files so
    we don't wake on our own machinery."""

    def __init__(self, wake_event: threading.Event) -> None:
        super().__init__()
        self._wake = wake_event

    def on_any_event(self, event: Any) -> None:
        try:
            paths = [getattr(event, "src_path", "") or ""]
            dest = getattr(event, "dest_path", "")
            if dest:
                paths.append(dest)
            for raw in paths:
                p = raw.lower()
                base = p.replace("\\", "/").rsplit("/", 1)[-1]
                if "original subs" in p or base.startswith((".tmp_", ".bak_")):
                    continue
                if p.endswith((".ass", ".ssa")) or any(
                    p.endswith(ext) for ext in VIDEO_EXTENSIONS
                ):
                    self._wake.set()
                    return
        except Exception:  # noqa: BLE001
            # A handler error must never kill the observer thread; wake to be safe.
            self._wake.set()


_SETTLED_STATUSES = frozenset({
    FileStatus.PROCESSED.value, FileStatus.RESTORED.value, FileStatus.FOREIGN.value,
})


class FolderMonitor:
    """Monitor a folder for new/modified subtitle files and styled/extracted."""

    def __init__(self, state=None, db=None, processor=None):
        # Inject dependencies; default to the app-wide singletons so existing
        # callers (and the static shims below) behave exactly as before.
        self.state = state if state is not None else app_state
        self.db = db if db is not None else DatabaseManager
        self.fp = processor if processor is not None else FileProcessor
        # This watch session's own stop signal, captured once. Starting a new
        # session gives the state a NEW event instead of clearing this one, so
        # a loop still finishing a slow step after Stop can't be revived and
        # end up running beside the new loop (two monitors on one library).
        self.stop_event: threading.Event = self.state.stop_event
        # Folders already told about files another program restyled (one
        # Problems entry per folder per session, not one per file).
        self._foreign_noted: Set[str] = set()
        # Files that failed for a reason that can clear up by itself (a player
        # holding them open, disk full, an unplugged drive): path -> (retry at
        # monotonic time, current delay). Their mtime doesn't change when the
        # cause goes away, so the failed-at-this-mtime skip would wait forever.
        self._retry_at: Dict[str, Tuple[float, float]] = {}

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        """Main monitoring loop (runs on the monitor thread)."""
        state, db, fp = self.state, self.db, self.fp
        # Mark videos that already exist when monitoring starts as "processed"
        # so only videos appearing AFTER this point trigger extraction. Done
        # here (in the monitor thread) rather than in toggle_monitoring so a
        # large folder scan never freezes the UI.
        folder = state.monitored_folder
        if folder:
            # Temp files a crash left behind (a styled ".tmp_XXXX.ass" copy is
            # visible to players and media servers).
            cleanup = getattr(fp, "cleanup_orphan_temps", None)
            if cleanup is not None:
                try:
                    cleanup(folder)
                except Exception as e:  # noqa: BLE001 — never block watching
                    logger.warning(f"Leftover temp cleanup failed: {e}")
            videos_to_add: List[str] = []
            for item in Path(folder).rglob("*"):
                if self.stop_event.is_set():
                    break
                if item.is_file() and fp.is_video_file(item.name):
                    videos_to_add.append(str(item.resolve()))
            if videos_to_add:
                db.add_processed_videos_batch(videos_to_add)

        processed_videos = db.get_processed_videos()
        with state.processed_lock:
            state.processed_videos = set(processed_videos)
        # Files whose apply_changes failed at a given mtime — suppresses
        # re-reading/re-erroring the same broken file every tick until it
        # actually changes on disk.
        failed_ass: Dict[str, float] = {}

        observer = self._start_observer()
        poll_interval = _POLL_WATCH if observer is not None else _POLL_NOWATCH
        logger.info("Monitoring thread started.")

        try:
            while not self.stop_event.is_set():
                try:
                    # Capture a snapshot once per loop iteration. state.config is
                    # replaced atomically on the main thread, so holding the
                    # reference gives us a consistent view even if the user
                    # changes settings mid-scan.
                    cfg = state.config

                    # Reload the known-file cache from the DB each tick so files
                    # the GUI styled/restored (which write the DB directly) are
                    # seen here immediately, instead of being re-parsed against a
                    # stale cache loaded once at thread start.
                    known_ass = db.get_monitored_files()

                    self.process_ass_files(known_ass, failed_ass, cfg)

                    if cfg.extract_enabled and state.ffmpeg_available:
                        self.process_video_files(cfg)

                except Exception:
                    # Keep the thread alive but capture the traceback so a
                    # recurring failure can actually be located (str(e) alone
                    # can't say which subsystem raised).
                    logger.exception("Error during monitoring")

                # Wait for the next filesystem change (watchdog sets wake_event)
                # or the fallback poll interval. Stop/shutdown also set
                # wake_event, so this returns promptly on teardown.
                woke = state.wake_event.wait(poll_interval)
                state.wake_event.clear()
                if self.stop_event.is_set():
                    break
                if woke:
                    # Let a burst of write events (one save/copy emits many)
                    # settle into a single reconcile. Interruptible by stop.
                    self.stop_event.wait(_WATCH_SETTLE)
        finally:
            if observer is not None:
                try:
                    observer.stop()
                    observer.join(timeout=2.0)
                except Exception:  # noqa: BLE001
                    pass

        logger.info("Monitoring thread stopped.")

    def _start_observer(self) -> Optional[Any]:
        """Start a watchdog observer that wakes the loop on changes under the
        monitored folder. Returns the observer, or None if watchdog is
        unavailable or the observer can't start (e.g. a network share where
        ReadDirectoryChangesW is unreliable) — the caller then relies on the
        periodic poll, so monitoring keeps working either way."""
        if not _WATCHDOG_AVAILABLE:
            logger.info("watchdog not installed; using periodic polling.")
            return None
        folder = self.state.monitored_folder
        if not folder:
            return None
        try:
            observer = Observer()
            observer.schedule(
                _WakeHandler(self.state.wake_event), folder, recursive=True
            )
            observer.start()
            logger.info(
                f"watchdog watching {folder} "
                f"(poll fallback every {int(_POLL_WATCH)}s)"
            )
            return observer
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"watchdog observer failed to start ({e}); polling only."
            )
            return None

    def _subtitle_settled(self, path: Path) -> bool:
        """True if `path`'s size has stopped changing — a cheap "is this .ass
        done being written?" check for a file that was modified very recently.

        A subtitle still being copied in (e.g. over a share, where the writer
        permits shared reads) must not be read, backed up, and rewritten while
        partial: copy_original_file would snapshot the truncated file as the
        pristine Original Subs/ backup, and the write-once guard never refreshes
        it — permanently losing the true original. Two size samples a beat apart;
        a still-growing (or empty) file returns False and is deferred to a later
        tick rather than blocking the loop for the full FILE_STABLE_TIMEOUT.
        """
        try:
            first = path.stat().st_size
        except OSError:
            return False
        if first <= 0:
            return False
        # Interruptible settle so shutdown isn't delayed; a stop mid-wait defers
        # the file (treated as not-yet-settled) rather than styling it.
        if self.stop_event.wait(_ASS_SETTLE_INTERVAL):
            return False
        try:
            second = path.stat().st_size
        except OSError:
            return False
        return second > 0 and second == first

    def process_ass_files(
        self,
        known_ass: Dict[str, Dict[str, Any]],
        failed_ass: Dict[str, float],
        cfg: AppConfig,
    ) -> None:
        """Process ASS files in the monitored folder (the decision loop)."""
        state, db, fp = self.state, self.db, self.fp
        if not state.monitored_folder:
            return

        font = (cfg.font or "").strip()
        color = cfg.color
        bold = cfg.font_bold
        italic = cfg.font_italic
        underline = cfg.font_underline
        size = cfg.font_size
        # Per-font size nudge for the active font — looked up once so the check
        # and the write below use the identical value (else the file re-processes).
        nudge = cfg.font_size_nudges.get(font, 1.0)

        seen: Set[str] = set()
        interrupted = False
        for file_path in fp.list_ass_files(state.monitored_folder):
            if self.stop_event.is_set() or state._shutting_down:
                interrupted = True
                break

            str_path = str(file_path)
            seen.add(str_path)

            try:
                last_mod = file_path.stat().st_mtime
            except OSError:
                continue

            # Isolate per-file failures: a raise from file_hash / is_already_styled
            # / apply_changes / the DB update for ONE file must not unwind the whole
            # tick and starve every file after it (a file that fails deterministically
            # would otherwise abort the loop at the same point on every scan). Log it
            # with context and throttle the file by mtime so it isn't hammered.
            try:
                meta = known_ass.get(str_path)
                status = meta.get("status") if meta else None
                # Statuses the app sets after deciding about a file: unchanged
                # since then means nothing to do (a restored file stays
                # original, a foreign one stays as the other program left it).
                settled = status in _SETTLED_STATUSES
                if meta and settled and last_mod == meta["last_modified"]:
                    continue

                # Skip a file that already failed at this exact mtime so we don't
                # re-read/re-parse/re-error it every tick. It retries automatically
                # when the file changes on disk (its mtime no longer matches), and
                # after a growing delay when the failure was transient.
                if failed_ass.get(str_path) == last_mod:
                    retry = self._retry_at.get(str_path)
                    if retry is None or time.monotonic() < retry[0]:
                        continue

                if not (font and color):
                    continue

                # A subtitle still being copied in must not be styled while partial
                # (see _subtitle_settled): the partial bytes would be captured as the
                # pristine Original Subs/ backup. Only a recently-modified file can be
                # mid-write, so confirm size-stability for those; the established
                # library (older mtime) skips the check, and a still-growing file is
                # retried on a later tick.
                if (
                    time.time() - last_mod < _ASS_FRESH_WINDOW
                    and not self._subtitle_settled(file_path)
                ):
                    continue

                # Content-hash fast path: an mtime change with byte-identical
                # content (a touch/copy/no-op save) means the file is still the one
                # we already styled — refresh the timestamp and skip the expensive
                # decode+parse that is_already_styled/apply_changes would do.
                content_hash = fp.file_hash(file_path)
                if (
                    content_hash
                    and settled
                    and meta.get("content_hash") == content_hash
                ):
                    failed_ass.pop(str_path, None)
                    db.update_file_timestamp(str_path, last_mod, content_hash, status=status)
                    continue

                if fp.is_already_styled(
                    file_path, font, color,
                    bold=bold, italic=italic, underline=underline, size=size,
                    nudge=nudge,
                ):
                    logger.info(f"Already styled, skipping: {file_path.name}")
                    failed_ass.pop(str_path, None)
                    db.update_file_timestamp(str_path, last_mod, content_hash)
                elif settled and fp.looks_styled_by_us(file_path):
                    # A file we had decided about changed into styled output
                    # with OTHER settings: another copy of the app, or a hand
                    # edit in a subtitle editor. Don't fight it (ADR-007).
                    failed_ass.pop(str_path, None)
                    db.set_status(file_path, FileStatus.FOREIGN)
                    folder = str(file_path.parent)
                    if folder not in self._foreign_noted:
                        self._foreign_noted.add(folder)
                        # A decision, not a failure: a warning in the log.
                        record_warning(
                            "foreign", file_path.name,
                            "changed by another program after it was styled, so it "
                            "was left as it is; Apply to library restyles it",
                        )
                elif fp.apply_changes(
                    file_path, font, color,
                    bold=bold, italic=italic, underline=underline, size=size,
                    nudge=nudge,
                ):
                    # apply_changes -> add_file already recorded the post-style
                    # mtime + content hash, so no extra DB write is needed here.
                    failed_ass.pop(str_path, None)
                    self._retry_at.pop(str_path, None)
                    state.note_event("Styled", file_path.name, styled=True)
                else:
                    # apply_changes failed (bad colour, missing Format fields, etc.).
                    # Remember the mtime so we don't hammer the same broken file.
                    failed_ass[str_path] = last_mod
                    if fp.take_transient_failure(file_path):
                        prev = self._retry_at.get(str_path)
                        delay = min(prev[1] * 2, _RETRY_MAX) if prev else _RETRY_FIRST
                        self._retry_at[str_path] = (time.monotonic() + delay, delay)
                    else:
                        self._retry_at.pop(str_path, None)
            except Exception:
                logger.exception(f"Error processing {file_path.name}")
                failed_ass[str_path] = last_mod

        # Drop per-thread failed_ass entries for files that no longer exist so it
        # doesn't grow without bound over a long-running monitor session.
        # known_ass is reloaded from the DB each tick, so it needs no pruning
        # here. Skip if we broke out early (incomplete `seen`).
        if not interrupted:
            for stale in [k for k in failed_ass if k not in seen]:
                failed_ass.pop(stale, None)
            for stale in [k for k in self._retry_at if k not in seen]:
                self._retry_at.pop(stale, None)

    def process_video_files(self, cfg: AppConfig) -> None:
        """Process video files for subtitle extraction."""
        state, db, fp = self.state, self.db, self.fp
        if not state.monitored_folder:
            return

        extract_langs = cfg.extract_langs or ""

        for item in Path(state.monitored_folder).rglob("*"):
            if self.stop_event.is_set():
                break

            if not item.is_file() or not fp.is_video_file(item.name):
                continue

            abs_path = str(item.resolve())
            with state.processed_lock:
                already = abs_path in state.processed_videos
            if already:
                continue

            if not self.wait_for_file_stable(item):
                continue

            if self.stop_event.is_set():
                break

            streams = fp.list_subtitle_streams(abs_path)
            if streams is None:
                continue

            to_extract = fp.filter_best_streams(streams, extract_langs)

            if not to_extract:
                # Probe succeeded but there is nothing to extract (no subtitle
                # streams, or none match the selected languages). Record it now
                # so we don't re-probe — spawning ffprobe + a blocking stability
                # wait — on every 5s tick forever.
                self.mark_video_processed(abs_path)
                continue

            # Don't hand new work to the executor once a stop/shutdown is in
            # progress — that would resurrect a torn-down pool and extract during
            # teardown.
            if self.stop_event.is_set() or state._shutting_down:
                break
            # Mark BEFORE submitting so the next 5s tick's processed_videos guard
            # skips this in-flight video — extraction can take up to
            # FFMPEG_TIMEOUT, far longer than the poll interval, and would
            # otherwise be re-submitted every tick. run_extraction_batch un-marks
            # it again if every stream hit a transient (retryable) failure, so a
            # one-off ffmpeg error is still retried later.
            self.mark_video_processed(abs_path)
            out_folder = str(item.parent)
            try:
                future = state.executor.submit(
                    self.run_extraction_batch,
                    abs_path,
                    to_extract,
                    out_folder,
                    item.name
                )
                # run_extraction_batch swallows its own per-stream errors, but
                # attach a callback so an unexpected crash in the worker (which
                # would otherwise vanish into the discarded Future) is logged.
                future.add_done_callback(self._log_future_error)
            except RuntimeError:
                # Executor was torn down by shutdown() mid-loop.
                break

    def mark_video_processed(self, abs_path: str) -> None:
        """Record a video as processed (in-memory set + DB), thread-safely."""
        with self.state.processed_lock:
            self.state.processed_videos.add(abs_path)
        self.db.add_processed_video(abs_path)

    def unmark_video_processed(self, abs_path: str) -> None:
        """Undo mark_video_processed so a later tick retries the video."""
        with self.state.processed_lock:
            self.state.processed_videos.discard(abs_path)
        self.db.remove_processed_video(abs_path)

    def run_extraction_batch(
        self,
        abs_path: str,
        streams_to_extract: List[SubtitleStream],
        out_folder: str,
        filename: str
    ) -> None:
        """Run subtitle extraction in a background thread."""
        try:
            Path(out_folder).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            record_error("extract", filename, f"could not create output folder: {e}")
            return
        logger.info(
            f"Extracting subtitles from {filename}: "
            f"{len(streams_to_extract)} stream(s)"
        )

        # Languages with more than one kept stream get the stream index in the
        # output filename so same-language tracks don't overwrite each other.
        lang_counts = Counter(s.language.lower() for s in streams_to_extract)

        succeeded = 0
        transient_failure = False
        for s in streams_to_extract:
            try:
                out_file = self.fp.extract_subtitle(
                    abs_path, s.index, s.language, s.codec, out_folder,
                    disambiguate=lang_counts[s.language.lower()] > 1,
                )
                succeeded += 1
                logger.info(f"Extracted -> {out_file}")
            except subprocess.TimeoutExpired:
                # Retryable: a one-off timeout shouldn't permanently mark the
                # video processed (which would skip it forever).
                transient_failure = True
                record_error(
                    "extract", filename,
                    f"stream #{s.index}: ffmpeg timed out after {FFMPEG_TIMEOUT}s"
                )
            except Exception as e:  # noqa: BLE001
                record_error(
                    "extract", filename,
                    f"stream #{s.index}: {e}"
                )

        # The video was marked processed before this batch was submitted (to
        # dedup the in-flight extraction across poll ticks). If every attempt
        # was a transient (retryable) failure, un-mark it so a later tick
        # retries; a success or a permanent error (e.g. image-based codec)
        # stays marked so it isn't retried forever.
        if not succeeded and transient_failure:
            self.unmark_video_processed(abs_path)
        if succeeded:
            self.state.note_event("Extracted", filename)

    def wait_for_file_stable(
        self,
        path: Path,
        timeout: int = FILE_STABLE_TIMEOUT,
        check_interval: int = FILE_STABLE_CHECK_INTERVAL
    ) -> bool:
        """
        Wait for a file to stop growing (finished downloading/copying).

        Returns True if the file is stable, False on timeout/error/stop.
        """
        elapsed = 0
        last_size = -1

        while elapsed < timeout:
            try:
                current_size = path.stat().st_size
            except OSError:
                return False

            if current_size == last_size and current_size > 0:
                return True

            last_size = current_size

            if self.stop_event.wait(check_interval):
                return False

            elapsed += check_interval

        return False

    @staticmethod
    def _log_future_error(future: Any) -> None:
        """done-callback: surface an exception that escaped an extraction worker
        (the Future is otherwise discarded, so the traceback would be lost)."""
        try:
            exc = future.exception()
        except Exception:  # noqa: BLE001 — cancelled future, etc.
            return
        if exc is not None:
            logger.error(f"Extraction worker crashed: {exc}", exc_info=exc)
