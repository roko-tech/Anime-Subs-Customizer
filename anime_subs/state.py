"""Application state singleton + config persistence (GUI-aware)."""
import dataclasses
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, List, Any, Callable, Set, Tuple

import tkinter as tk
import customtkinter as ctk
from pystray import Icon

from .runtime import *
from . import db as _db

__all__ = ["AppState", "app_state", "ConfigManager"]


# ----------------------------------------
# Application State
# ----------------------------------------
class AppState:
    """Manage application state and GUI variables."""
    
    def __init__(self):
        self.monitoring_flag: bool = False
        self.stop_event: threading.Event = threading.Event()
        # Set to wake the monitor loop early — by the watchdog observer on a
        # filesystem change, or by stop/shutdown to break the wait promptly.
        self.wake_event: threading.Event = threading.Event()
        self.monitored_folder: Optional[str] = None
        self.processed_videos: Set[str] = set()
        # Guards processed_videos against the monitor thread, extraction
        # workers, and the GUI thread (reset) mutating/rebinding it at once.
        self.processed_lock: threading.Lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._executor_lock: threading.Lock = threading.Lock()
        self._shutting_down: bool = False
        # Set on the GUI thread while a long batch op (Apply to Existing /
        # Extract All / Restore) runs, so a second op — including one launched
        # from the tray menu, which bypasses the progress dialog's modal grab —
        # can't start concurrently on the same files. GUI-thread only.
        self.operation_in_progress: bool = False
        self.root: Optional[ctk.CTk] = None
        self.all_fonts: List[str] = []
        self.font_combobox: Optional[ctk.CTkEntry] = None
        self.folder_label: Optional[ctk.CTkLabel] = None
        self.toggle_button: Optional[ctk.CTkButton] = None
        self.status_label: Optional[ctk.CTkLabel] = None
        self.tray_icon: Optional[Icon] = None
        self.monitoring_thread: Optional[threading.Thread] = None
        self.led_indicator: Optional[ctk.CTkLabel] = None
        self.font_bold: Optional[tk.BooleanVar] = None
        self.font_italic: Optional[tk.BooleanVar] = None
        self.font_underline: Optional[tk.BooleanVar] = None
        self.selected_color_code: Optional[str] = None
        self.color_indicator: Optional[ctk.CTkButton] = None
        # Dialogue-size preset key ("none"|"small"|"medium"|"large"); the GUI
        # segmented button writes it, snapshot_config reads it. Plain str so
        # config-building stays headless-safe.
        self.font_size: str = "none"
        self.extract_enabled: Optional[tk.BooleanVar] = None
        self.extract_langs: Optional[tk.StringVar] = None
        self.preview_entry: Optional[ctk.CTkLabel] = None
        self.extraction_widgets: Dict[str, Any] = {}
        self.ffmpeg_available: bool = False
        self.extract_all_btn: Optional[ctk.CTkButton] = None
        self.issues_label: Optional[ctk.CTkLabel] = None
        self.config: AppConfig = AppConfig()
        # Preview text now lives on the state (the redesigned preview is a
        # rendered image, not a text widget holding the string).
        self.preview_text: str = DEFAULT_PREVIEW_TEXT
        # Most recent user-visible file event, feeding the footer status line:
        # (unix_ts, verb, name) e.g. (…, "Styled", "Show – 12.ass").
        self.last_event: Optional[Tuple[float, str, str]] = None
        # Files styled by the monitor since watching started (header count).
        self.styled_count: int = 0
        # Set by the GUI; called (from any thread) after note_event records.
        self.event_listener: Optional[Callable[[], None]] = None
        self._event_lock: threading.Lock = threading.Lock()

    def note_event(self, verb: str, name: str, styled: bool = False) -> None:
        """Record the most recent file event (thread-safe; any thread).

        The registered listener is responsible for marshaling onto the Tk
        main loop — this method never touches widgets.
        """
        with self._event_lock:
            self.last_event = (time.time(), verb, name)
            if styled:
                self.styled_count += 1
            listener = self.event_listener
        if listener:
            try:
                listener()
            except Exception as e:  # noqa: BLE001 — a UI hiccup must not kill workers
                logger.debug(f"Event listener failed: {e}")

    def get_style_flags(self) -> Tuple[bool, bool, bool]:
        """Return (bold, italic, underline) from the GUI variables.
        Main-thread only."""
        return (
            self.font_bold.get() if self.font_bold else False,
            self.font_italic.get() if self.font_italic else False,
            self.font_underline.get() if self.font_underline else False,
        )

    def snapshot_config(self, folder: Optional[str] = None) -> "AppConfig":
        """Build an AppConfig from the live widget/var state (main-thread only).

        Reading the widgets lives here (on the state object) rather than in the
        persistence layer, so config-building is decoupled from saving and can
        be exercised without the persistence path. Widgets that aren't built yet
        fall back to safe defaults, so this is callable headlessly.
        """
        preview_text = self.preview_text or DEFAULT_PREVIEW_TEXT

        bold, italic, underline = self.get_style_flags()
        # Start from the current config so every field the UI doesn't own
        # (favourites, nudges, consented folders, update-check state, …) is
        # carried over instead of silently reset on the next save.
        return dataclasses.replace(
            self.config,
            font=self.font_combobox.get() if self.font_combobox else "",
            color=self.selected_color_code,
            folder=folder or self.monitored_folder,
            font_bold=bold,
            font_italic=italic,
            font_underline=underline,
            font_size=self.font_size,
            extract_enabled=self.extract_enabled.get() if self.extract_enabled else False,
            extract_langs=self.extract_langs.get() if self.extract_langs else "",
            preview_text=preview_text,
        )

    @property
    def executor(self) -> ThreadPoolExecutor:
        """Get or create the thread pool executor (double-checked lock).

        Refuses to lazily resurrect a pool once shutdown has begun, so a
        late monitor-thread submit can't spawn a fresh, never-joined pool
        (and stray ffmpeg work) during/after teardown.
        """
        if self._shutting_down:
            raise RuntimeError("Executor unavailable during shutdown")
        if self._executor is None:
            with self._executor_lock:
                if self._shutting_down:
                    raise RuntimeError("Executor unavailable during shutdown")
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=2)
        return self._executor

    def initialize_variables(self, root: ctk.CTk) -> None:
        """Initialize Tkinter variables."""
        self.font_bold = tk.BooleanVar(root, value=False)
        self.font_italic = tk.BooleanVar(root, value=False)
        self.font_underline = tk.BooleanVar(root, value=False)
        self.extract_enabled = tk.BooleanVar(root, value=False)
        self.extract_langs = tk.StringVar(root, value="")

    def shutdown(self) -> None:
        """Clean shutdown of resources."""
        self.monitoring_flag = False
        # Set the shutdown flag before tearing anything down so the executor
        # property and get_db_pool() refuse to resurrect resources from here on.
        self._shutting_down = True
        self.stop_event.set()
        # Wake the monitor loop so it notices the stop immediately instead of
        # sitting in its (possibly long) wait for the next filesystem change.
        self.wake_event.set()

        if self.monitoring_thread and self.monitoring_thread.is_alive():
            self.monitoring_thread.join(timeout=2.0)

        # Read+null the executor under its own lock so this can't race the
        # property's lazy-init.
        with self._executor_lock:
            executor = self._executor
            self._executor = None
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)

        # Stop any ffmpeg/ffprobe still running for us (extraction, preview
        # frames): they'd outlive the app and keep the install folder locked.
        terminate_tracked_processes()

        # Close database pool and block any late resurrection (the pool globals
        # live in the db module now).
        _db.shutdown_db_pool()

        logger.info("Application shutdown complete")


app_state = AppState()


# ----------------------------------------
# Configuration Management
# ----------------------------------------
class ConfigManager:
    """Manage application configuration."""
    
    @staticmethod
    def save_config(folder: Optional[str] = None, state: Optional[AppState] = None) -> bool:
        """Save current configuration to file.

        `state` defaults to the global app_state but can be injected (e.g. in
        tests) — config-building now lives on the state object (snapshot_config),
        so this layer only snapshots-and-persists.
        """
        st = state if state is not None else app_state
        config = st.snapshot_config(folder)
        st.config = config
        return config.save()
    
    @staticmethod
    def load_config() -> AppConfig:
        """Load configuration from file."""
        config = AppConfig.load()
        app_state.config = config
        return config
