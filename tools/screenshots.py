"""Reproducible documentation screenshots of every Anime Subs Customizer screen.

Runs the real app in-process as a private test instance and walks one fixed
script through every window, dialog and message box, saving each window on
its own with PrintWindow(PW_RENDERFULLCONTENT) (the app's own self-test
method), so nothing above or behind a window can bleed into the image.

    python tools/screenshots.py --lang ar|en --out DIR
        [--folder-label TEXT] [--only NAME,...] [--library DEMO_LIB]
        [--manifest PATH] [--token TOKEN] [--timeout SECONDS] [--docs]

Writes DIR/<name>.png and merges one entry per screen (lang, name, title,
file, keys, captured, ...) into the manifest (default DIR/../manifest.json).
A screen that cannot be captured is listed with "captured": false and its
strings as text. --only limits which images are written; the whole script
still runs, so every screen is reached in the same state every time.

--docs instead writes the two README images: DIR/main.png (watching, with the
preview) and DIR/demo.gif (pick a font, a colour, turn on Watch).

Isolation (checked at start, before any window opens):
- ASC_INSTANCE_TOKEN is set before the app is imported, so its settings,
  database, caches, mutex and "Startup folder" all live in
  %TEMP%\\asc-instance-<token>. The run stops if that instance is running.
- The app works on a fresh COPY of a synthetic demo library
  (tools/make_demo_library.py, seed 7) inside that folder; the source library
  is only read. Autostart writes are stubbed out and the folder monitor's
  loop is replaced by an idle wait, so watching never processes a file.
  Nothing opens a browser, Explorer or the log; the update check's network
  call is replaced by a fixed answer for its one screen.
- Every path to the library is displayed as --folder-label; any other path on
  this machine is rewritten before it reaches a widget or message box.
- The font list is limited to fonts that ship with Windows (some come with
  its Arabic supplemental fonts), so the font picker is machine-independent.
"""
from __future__ import annotations

import argparse
import ctypes
import faulthandler
import importlib.util
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "anime_subs_customizer.pyw"
DEMO_TOOL = ROOT / "tools" / "make_demo_library.py"
DEMO_SEED = "7"
log = logging.getLogger("screenshots")

# Scratch config: a font every Windows has, BGR yellow, medium dialogue size.
UI_FONT = "Segoe UI"
UI_COLOR = "00FFFF"
FAVORITES = ["Arial", "Sakkal Majalla", "Segoe UI", "Tahoma", "Traditional Arabic"]
STOCK_FONTS = frozenset((
    "Aldhabi", "Andalus", "Arabic Typesetting", "Arial", "Arial Black",
    "Bahnschrift", "Calibri", "Calibri Light", "Cambria", "Candara",
    "Comic Sans MS", "Consolas", "Constantia", "Corbel", "Courier New",
    "Ebrima", "Franklin Gothic Medium", "Gabriola", "Gadugi", "Georgia",
    "Impact", "Ink Free", "Javanese Text", "Leelawadee UI", "Lucida Console",
    "Lucida Sans Unicode", "Malgun Gothic", "Microsoft Himalaya",
    "Microsoft JhengHei", "Microsoft Sans Serif", "Microsoft Uighur",
    "Microsoft YaHei", "Mongolian Baiti", "MS Gothic", "MV Boli", "Myanmar Text",
    "Nirmala UI", "Palatino Linotype", "Sakkal Majalla", "Segoe MDL2 Assets",
    "Segoe Print", "Segoe Script", "Segoe UI", "Segoe UI Black", "Segoe UI Emoji",
    "Segoe UI Light", "Segoe UI Semibold", "Segoe UI Semilight",
    "Segoe UI Symbol", "SimSun", "Simplified Arabic", "Simplified Arabic Fixed",
    "Sitka Text", "Sylfaen", "Tahoma", "Times New Roman", "Traditional Arabic",
    "Trebuchet MS", "Urdu Typesetting", "Verdana", "Yu Gothic",
))
LATIN_ONLY_FONT = "Georgia"            # -> "font doesn't support Arabic" note
NO_TEXT_FONT = "Segoe MDL2 Assets"     # icon font -> "supports neither" note

# Issues panel: a fixed "now" so its relative timestamps never drift.
FROZEN_NOW = datetime(2026, 10, 9, 22, 30)

# ----------------------------------------------------------------------------
# Screen catalog: name -> (English description, visible _STRINGS keys).
# keys=None means "computed from the live main-window state at capture time".
# ----------------------------------------------------------------------------
MAIN_BASE = [
    "watch_folder", "change", "live_preview", "real_bg", "bg_mode_mytext",
    "bg_mode_subtitle", "shuffle_bg", "preview_text_btn", "font", "color",
    "size", "apply_to_library", "extract_subs", "restore_originals",
]
FONT_PICKER = ["choose_font_title", "favorites", "use_this_font", "cancel"]
LANGS = ["langs_title", "langs_intro", "language_chip", "done"] + [
    f"language_{c}" for c in ("eng", "ara", "spa", "fra", "deu", "ita", "jpn",
                              "kor", "zho", "rus")]
LANGS_TAIL = [f"language_{c}" for c in ("tur", "por", "hin", "urd", "tha", "vie")]
RESTORE = ["restore_title", "restore_intro", "restore_files", "cancel"]
RESET = ["reset_title", "reset_intro", "reset_config", "reset_config_desc",
         "reset_subs", "reset_subs_desc", "reset_video", "reset_video_desc",
         "reset_selected_btn", "cancel"]

OK, YES_NO = ["ok"], ["yes", "no"]

CATALOG: List[Tuple[str, str, Optional[List[str]]]] = [
    ("main_idle", "Main window - idle, not watching, nothing selected for extraction", None),
    ("main_font_no_arabic", "Main window - preview with a Latin-only font (Georgia)", None),
    ("main_font_no_neither", "Main window - preview with an icon font that has no text glyphs", None),
    ("menu_overflow", "The ... overflow menu", [
        "extract_all_subs", "open_log", "reset", "autostart", "start_minimized",
        "language", "menu_updates", "menu_guide", "menu_report", "menu_about"]),
    ("font_picker", "Font picker - opened, no filters", FONT_PICKER + [
        "capable_chip", "script_arabic"]),
    ("font_picker_scan_start", "Font picker - Arabic-capable filter just switched on (scan starting)",
     FONT_PICKER + ["capable_scanning", "script_arabic"]),
    ("font_picker_indexing", "Font picker - coverage scan indexing fonts", FONT_PICKER + ["scan_indexing"]),
    ("font_picker_scanning", "Font picker - coverage scan in progress", FONT_PICKER + ["scan_scanning"]),
    ("font_picker_capable", "Font picker - Arabic-capable filter on (with count)", FONT_PICKER + [
        "capable_chip", "script_arabic"]),
    ("font_picker_favorites", "Font picker - Arabic-capable filter and Favorites filter on",
     FONT_PICKER + ["capable_chip", "script_arabic"]),
    ("font_picker_no_match", "Font picker - search with no matching font", FONT_PICKER + [
        "capable_chip", "script_arabic", "no_matching_fonts"]),
    ("color_picker", "Subtitle colour picker (native Windows dialog; buttons follow the Windows language)",
     ["choose_color_title"]),
    ("preview_text_editor", "Preview-text editor (native RichEdit field)", [
        "preview_text_title", "preview_text_helper", "save", "cancel"]),
    ("langs_no_ffmpeg", "Extract-languages dialog - ffmpeg missing", LANGS + [
        "langs_more", "ffmpeg_disabled"]),
    ("langs_dialog", "Extract-languages dialog - Arabic and English selected", LANGS + ["langs_more"]),
    ("langs_dialog_expanded", "Extract-languages dialog - all 16 languages shown", LANGS + LANGS_TAIL),
    ("first_watch_none", "First Watch of a folder - confirmation when it has no subtitles yet", [
        "first_watch_title", "first_watch_none", "first_watch_start", "cancel"]),
    ("first_watch_risky", "First Watch of a folder - confirmation with the drive/system-folder warning", [
        "first_watch_title", "first_watch_body", "first_watch_risky", "first_watch_start", "cancel"]),
    ("first_watch", "First Watch of a folder - confirmation with the file count", [
        "first_watch_title", "first_watch_body", "first_watch_start", "cancel"]),
    ("main_watching_empty", "Main window - watching, nothing styled yet, extraction on", None),
    ("mb_switch_folder", "Message - change folder while watching", [
        "switch_folder_title", "switch_folder_body"] + YES_NO),
    ("mb_stop_before_apply", "Message - Apply to library while watching", [
        "warn_title", "stop_before_apply"] + OK),
    ("mb_stop_before_extract_all", "Message - Extract all subs while watching", [
        "warn_title", "stop_before_extract_all"] + OK),
    ("mb_stop_before_restore", "Message - Restore originals while watching", [
        "warn_title", "stop_before_restore"] + OK),
    ("mb_stop_before_reset", "Message - Reset while watching", [
        "warn_title", "stop_before_reset"] + OK),
    ("main_watching", "Main window - watching, 3 files styled, 5 issues", None),
    ("mb_still_stopping", "Message - Watch turned on while the last scan is still stopping", [
        "busy_title", "still_stopping"] + OK),
    ("issues_panel", "Issues panel - 5 issues (plain-language reason + technical details)", [
        "issues_title", "issues_intro", "op_restore", "op_foreign", "op_read", "op_extract",
        "op_decode", "reason_bad_backup", "reason_foreign", "reason_locked",
        "reason_timeout", "reason_undecodable", "issue_details", "open_log", "clear", "close"]),
    ("issues_panel_empty", "Issues panel - empty", ["issues_title", "issues_none", "open_log", "close"]),
    ("main_idle_last", "Main window - stopped, footer shows the last event 2 h ago, 1 issue", None),
    ("mb_exit_prompt", "Message - closing the window (keep in tray / quit / cancel)", [
        "exit_title", "minimize_to_tray_q", "keep_in_tray", "quit_app", "cancel"]),
    ("mb_restart_failed", "Message - language switch could not restart the app", [
        "restart_to_apply_title", "restart_to_apply"] + OK),
    ("mb_busy", "Message - another operation is running", ["busy_title", "busy_body"] + OK),
    ("mb_select_folder_first", "Message - no watch folder chosen", [
        "warn_title", "select_folder_first"] + OK),
    ("mb_nothing_to_watch", "Message - Watch with no font/colour and extraction off", [
        "warn_title", "nothing_to_watch_for"] + OK),
    ("mb_select_font_color", "Message - Apply with no colour chosen", [
        "warn_title", "select_font_color"] + OK),
    ("mb_ffmpeg_not_found", "Message - ffmpeg missing (Extract all / Extract subs)", [
        "missing_tools_title", "ffmpeg_not_found"] + OK),
    ("mb_log_not_found", "Message - log file missing", ["error_title", "log_not_found"] + OK),
    ("mb_cant_open_log", "Message - log file cannot be opened (raw Windows error text)", [
        "error_title", "cant_open_log"] + OK),
    ("about", "About", ["menu_about", "about_body", "about_website", "close"]),
    ("about_update", "About - with a newer version noted", [
        "menu_about", "about_body", "update_available_body", "about_website", "close"]),
    ("update_available", "New-version notice (opt-in daily check; network stubbed)", [
        "update_available_title", "update_available_body", "update_open", "welcome_later"]),
    ("crash_dialog", "Unexpected-error dialog", [
        "crash_title", "crash_body", "crash_copy", "crash_open_folder", "close"]),
    ("mb_native_crash_start", "Native Windows message box - the app could not start "
     "(right-to-left reading order; the button follows the Windows language)", [
        "crash_title", "crash_start_body"]),
    ("mb_no_backups", "Message - Restore with no Original Subs backups", [
        "no_backups_title", "no_backups_body"] + OK),
    ("apply_scope", "Apply-to-library scope chooser", [
        "apply_to_library", "apply_scope_prompt", "apply_whole", "apply_subfolder",
        "apply_subfolder_hint", "cancel"]),
    ("mb_apply_summary", "Message - Apply to library finished (summary + problems line)", [
        "done_title", "apply_summary", "apply_summary_problems"] + OK),
    ("mb_reextract_confirm", "Message - confirm Extract all subs", [
        "confirm_title", "reextract_confirm"] + YES_NO),
    ("mb_extracted", "Message - Extract all subs finished", ["done_title", "extracted_n"] + OK),
    ("main_after_extract", "Main window - stopped, footer shows the extraction event", None),
    ("mb_confirm_restore", "Message - restore a single series", [
        "confirm_restore_title", "overwrite_styled_q"] + YES_NO),
    ("restore_picker_idle", "Restore picker - nothing selected", RESTORE + [
        "restore_btn_idle", "select_all"]),
    ("restore_picker", "Restore picker - two series selected", RESTORE + ["restore_btn", "select_all"]),
    ("restore_picker_all", "Restore picker - all series selected", RESTORE + ["restore_btn", "clear_all"]),
    ("mb_restored", "Message - restore finished with one failure", [
        "done_title", "restored_n", "restored_failed"] + OK),
    ("main_after_restore", "Main window - stopped, footer shows the restore event", None),
    ("main_real_bg", "Main window - real video frame behind a real subtitle line", None),
    ("main_no_sub_found", "Main window - subtitle-line mode found no subtitle", None),
    ("progress_apply", "Progress dialog - applying (5 of 16)", ["prog_apply", "prog_count"]),
    ("progress_extract", "Progress dialog - extracting, scanning the folder", [
        "prog_extract", "prog_scanning"]),
    ("progress_restore", "Progress dialog - restore complete", ["prog_restore", "prog_count", "done"]),
    ("reset_dialog", "Reset dialog - nothing selected", RESET),
    ("reset_dialog_selected", "Reset dialog - all three items selected", RESET),
    ("mb_confirm_reset", "Message - confirm reset of all three items", [
        "confirm_reset_title", "reset_recap_head", "reset_item_config", "reset_item_subs",
        "reset_item_videos", "reset_recap_tail"] + YES_NO),
    ("mb_reset_errors", "Message - reset failed (the error lines are hard-coded English)", [
        "reset_errors_title"] + OK),
    ("mb_reset_done", "Message - reset finished", ["reset_title", "reset_done"] + OK),
    ("main_no_folder", "Main window - after resetting the configuration (no folder, no font)", None),
    ("welcome", "First-run welcome (shown once, when no folder is set)", [
        "welcome_title", "welcome_body", "update_opt_in", "welcome_choose", "welcome_later"]),
]

# Surfaces this tool cannot (or must not) capture; their strings go in the
# manifest as text.
UNCAPTURED: List[Tuple[str, str, List[str], str]] = [
    ("tray_menu", "System-tray icon menu (pystray Win32 popup menu)", [
        "tray_show", "tray_start_watching", "tray_stop_watching", "tray_change_folder",
        "apply_to_library", "extract_all_subs", "restore_originals", "open_log",
        "reset", "autostart", "start_minimized", "tray_quit"],
     "The Win32 popup only opens on a real right-click of the notification-area "
     "icon; scripting it would mean driving the user's taskbar."),
    ("tray_balloons", "Notification-area balloons (first minimise to tray; update found while in the tray)", [
        "tray_hint", "update_available_tray"],
     "Windows shell notifications, drawn by Explorer outside the app's windows."),
    ("folder_picker", "Windows folder picker (Change... / A specific series... / welcome)", [
        "apply_pick_title"],
     "Native Explorer dialog: it lists this machine's real drives and folders, "
     "so it is not captured. Only its title comes from the app."),
    ("preview_font_no_latin", "Preview-well note for an Arabic-only font", ["font_no_latin"],
     "No font that ships with Windows lacks Latin glyphs, so the state cannot be "
     "reached with stock fonts."),
    ("other_forms", "Forms and reasons not shown in the captured screens", [
        "rel_min", "rel_day", "event_files_one", "event_videos_one", "files_one",
        "op_apply", "op_backup", "op_database", "op_font", "reason_ssa_v4",
        "reason_originals_deleted",
        "reason_backup_link", "reason_db_rebuilt", "reason_empty", "reason_too_large",
        "reason_bad_format", "reason_disk_full", "reason_unreachable",
        "reason_access_denied", "reason_missing", "reason_backup_failed"],
     "Same layouts as the captured screens; listed so every string gets reviewed."),
]

# ----------------------------------------------------------------------------
# Win32 (own WinDLL instances: the app sets no argtypes on ctypes.windll and
# must keep its own conversions)
# ----------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
dwmapi = ctypes.WinDLL("dwmapi")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
for _fn, _args, _res in (
    (user32.EnumWindows, [WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL),
    (user32.EnumChildWindows, [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL),
    (user32.GetWindowThreadProcessId, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
    (user32.GetClassNameW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
    (user32.GetWindowTextW, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
    (user32.GetWindowTextLengthW, [wintypes.HWND], ctypes.c_int),
    (user32.IsWindowVisible, [wintypes.HWND], wintypes.BOOL),
    (user32.IsWindow, [wintypes.HWND], wintypes.BOOL),
    (user32.GetDlgCtrlID, [wintypes.HWND], ctypes.c_int),
    (user32.PostMessageW, [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
    (user32.GetWindowRect, [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
    (user32.GetWindowDC, [wintypes.HWND], wintypes.HDC),
    (user32.ReleaseDC, [wintypes.HWND, wintypes.HDC], ctypes.c_int),
    (user32.PrintWindow, [wintypes.HWND, wintypes.HDC, wintypes.UINT], wintypes.BOOL),
    (user32.SetFocus, [wintypes.HWND], wintypes.HWND),
    (user32.GetWindowLongW, [wintypes.HWND, ctypes.c_int], ctypes.c_long),
    (gdi32.CreateCompatibleDC, [wintypes.HDC], wintypes.HDC),
    (gdi32.CreateCompatibleBitmap, [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP),
    (gdi32.SelectObject, [wintypes.HDC, wintypes.HGDIOBJ], wintypes.HGDIOBJ),
    (gdi32.GetDIBits, [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT], ctypes.c_int),
    (gdi32.DeleteObject, [wintypes.HGDIOBJ], wintypes.BOOL),
    (gdi32.DeleteDC, [wintypes.HDC], wintypes.BOOL),
    (dwmapi.DwmGetWindowAttribute, [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD], ctypes.c_long),
    (kernel32.GetLongPathNameW, [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD], wintypes.DWORD),
    (kernel32.GetShortPathNameW, [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD], wintypes.DWORD),
):
    _fn.argtypes, _fn.restype = _args, _res

PW_RENDERFULLCONTENT = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9
GWL_EXSTYLE, WS_EX_LAYOUTRTL = -20, 0x00400000
WM_COMMAND = 0x0111
BUTTON_IDS = {"ok": 1, "cancel": 2, "yes": 6, "no": 7}
_BIDI_MARKS = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
                                     "\u2066\u2067\u2068\u2069"))


class _BMIHeader(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def plain(text: Any) -> str:
    """Text without bidi control marks (for matching and the manifest)."""
    return str(text or "").translate(_BIDI_MARKS)


def _win_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd) + 1
    buf = ctypes.create_unicode_buffer(n)
    user32.GetWindowTextW(hwnd, buf, n)
    return buf.value


def _win_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def _own_dialogs() -> List[int]:
    """Visible standard dialogs (#32770: message boxes, colour picker) of this process."""
    pid, found = os.getpid(), []

    def cb(hwnd: int, _l: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) \
                and _win_class(hwnd) == "#32770":
            found.append(hwnd)
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return found


def _children(hwnd: int) -> List[Tuple[str, int, int, str]]:
    """(class, control id, hwnd, text) of every child window."""
    out: List[Tuple[str, int, int, str]] = []

    def cb(child: int, _l: int) -> bool:
        out.append((_win_class(child), user32.GetDlgCtrlID(child), child, _win_text(child)))
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return out


def capture_hwnd(hwnd: int, path: Path) -> Tuple[int, int]:
    """PNG of one top-level window; see capture_image."""
    img = capture_image(hwnd)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return img.size


def capture_image(hwnd: int) -> Any:
    """One top-level window (PrintWindow full content) as a PIL image, cropped
    to the visible DWM frame so the invisible resize borders are not included."""
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError(f"GetWindowRect failed for {hwnd:#x}")
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT):
            raise OSError("PrintWindow failed")
        gdi32.SelectObject(mdc, old)  # GetDIBits needs the bitmap deselected
        bmi = _BMIHeader(ctypes.sizeof(_BMIHeader), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        if gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0) != h:
            raise OSError("GetDIBits failed")
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
    from PIL import Image
    img = Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
    ext = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                    ctypes.byref(ext), ctypes.sizeof(ext)) == 0:
        box = (ext.left - rect.left, ext.top - rect.top,
               ext.right - rect.left, ext.bottom - rect.top)
        if 0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h:
            img = img.crop(box)
    if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_LAYOUTRTL:
        # PrintWindow hands back a mirror-layout window (e.g. a MB_RTLREADING
        # message box) flipped left-right; flip it back to what is on screen.
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    if img.getextrema() == ((0, 0), (0, 0), (0, 0)):
        raise OSError("capture came back blank")
    return img


class NativeBox(threading.Thread):
    """Waits for this process's standard dialog with `title`, saves it, then
    presses `click`. Runs off the Tk thread, which is blocked in the dialog's
    modal loop meanwhile; touches only Win32."""

    def __init__(self, name: str, title: str, click: str, shot: Optional[Path],
                 timeout: float = 60.0) -> None:
        super().__init__(name=f"box-{name}", daemon=True)
        self.box_name, self.title, self.click = name, plain(title), click
        self.shot, self.timeout = shot, timeout
        self.done = threading.Event()
        self.info: Dict[str, Any] = {}
        self.size: Optional[Tuple[int, int]] = None
        self.error: Optional[str] = None

    def run(self) -> None:
        hwnd = 0
        try:
            deadline = time.monotonic() + self.timeout
            while not hwnd:
                hwnd = next((h for h in _own_dialogs()
                             if plain(_win_text(h)) == self.title), 0)
                if not hwnd:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"no dialog titled {self.title!r}")
                    time.sleep(0.05)
            time.sleep(0.6)  # finish painting / the open animation
            kids = _children(hwnd)
            buttons = [(cid, plain(txt).replace("&", "")) for cls, cid, _h, txt in kids
                       if cls == "Button" and txt]
            statics = [plain(txt) for cls, _cid, _h, txt in kids
                       if cls == "Static" and txt.strip()]
            self.info = {"title": plain(_win_text(hwnd)), "message": "\n".join(statics),
                         "buttons": [b for _c, b in buttons]}
            if self.shot is not None:
                self.size = capture_hwnd(hwnd, self.shot)
        except Exception as e:  # noqa: BLE001 - reported to the script
            self.error = f"{type(e).__name__}: {e}"
        finally:
            if hwnd:
                self._press(hwnd)
            self.done.set()

    def _press(self, hwnd: int) -> None:
        want = BUTTON_IDS[self.click]
        ids = {cid for cls, cid, _h, _t in _children(hwnd) if cls == "Button"}
        if want not in ids:
            want = next(iter(ids)) if len(ids) == 1 and self.click == "ok" else 2
        user32.PostMessageW(hwnd, WM_COMMAND, want, 0)
        deadline = time.monotonic() + 10
        while user32.IsWindow(hwnd) and time.monotonic() < deadline:
            time.sleep(0.05)


# ----------------------------------------------------------------------------
# Paths and privacy
# ----------------------------------------------------------------------------
def _path_forms(p: Path) -> List[str]:
    """The ways a Windows path can be spelled on screen (long/8.3, \\ and /)."""
    forms = {str(p)}
    for fn in (kernel32.GetLongPathNameW, kernel32.GetShortPathNameW):
        buf = ctypes.create_unicode_buffer(1024)
        if fn(str(p), buf, 1024):
            forms.add(buf.value)
    return sorted(forms | {f.replace("\\", "/") for f in forms}, key=len, reverse=True)


class Scrubber:
    """Rewrites any path on this machine to a generic one before display."""

    def __init__(self, label: str, library: Path, data_dir: Path) -> None:
        profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
        targets = [(library, label),
                   (data_dir, r"C:\Users\Demo\AppData\Local\Anime Subs Customizer"),
                   (profile, r"C:\Users\Demo")]
        self.rules: List[Tuple[re.Pattern, str]] = []
        for real, shown in targets:
            for form in _path_forms(real):
                repl = shown.replace("\\", "/") if "/" in form else shown
                self.rules.append((re.compile(re.escape(form), re.IGNORECASE), repl))
        self.rules.sort(key=lambda r: len(r[0].pattern), reverse=True)
        user = os.environ.get("USERNAME", "")
        if len(user) > 2:
            self.rules.append((re.compile(re.escape(user), re.IGNORECASE), "Demo"))

    def __call__(self, text: Any) -> Any:
        if not isinstance(text, str) or not text:
            return text
        for pattern, repl in self.rules:
            text = pattern.sub(lambda _m, r=repl: r, text)
        return text


def prepare_library(source: Optional[Path], data_dir: Path) -> Path:
    """Fresh working copy of the synthetic demo library inside the instance's
    data folder. The source is only read."""
    if source is None:
        source = data_dir / f"demo-lib-seed{DEMO_SEED}"
        if not (source / "manifest.json").exists():
            log.info("generating the demo library (seed %s)", DEMO_SEED)
            subprocess.run([sys.executable, str(DEMO_TOOL), str(source), "--seed", DEMO_SEED],
                           check=True)
    try:
        meta = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SystemExit(f"{source} is not a generated demo library: {e}")
    if "make_demo_library" not in str(meta.get("generator", "")):
        raise SystemExit(f"{source} was not made by tools/make_demo_library.py")
    work = data_dir / "library"
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(source, work)
    (work / "manifest.json").unlink()
    return work


def reset_instance_state(data_dir: Path) -> None:
    """Remove the previous run's settings/database/preview frames (caches stay)."""
    if not data_dir.name.startswith("asc-instance-"):
        raise SystemExit(f"refusing to clean {data_dir}: not a test-instance folder")
    for pattern in ("config.json", "files_monitor.db*", "preview_frame_*",
                    "show_request.flag", "monitor.log*", "font_support_cache.json",
                    "docs-shots-*.log"):
        for p in data_dir.glob(pattern):
            p.unlink()
    shutil.rmtree(data_dir / "Startup", ignore_errors=True)


def load_app_module() -> Any:
    loader = SourceFileLoader("anime_subs_customizer_docs", str(APP_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------
# Script runner: a generator yields a delay in ms, or (predicate, timeout_s,
# description); the Tk loop resumes it. Steps never overlap.
# ----------------------------------------------------------------------------
class Runner:
    def __init__(self, root: Any, gen: Iterator[Any], on_done: Callable[[Optional[BaseException]], None]):
        self.root, self.gen, self.on_done = root, gen, on_done

    def start(self) -> None:
        self._step(lambda: next(self.gen))

    def _step(self, resume: Callable[[], Any]) -> None:
        try:
            cmd = resume()
        except StopIteration:
            self.on_done(None)
            return
        except BaseException as e:  # noqa: BLE001
            self.on_done(e)
            return
        if isinstance(cmd, (int, float)):
            self.root.after(int(cmd), self._step, lambda: next(self.gen))
        else:
            pred, timeout, desc = cmd
            self._poll(pred, time.monotonic() + timeout, desc)

    def _poll(self, pred: Callable[[], bool], deadline: float, desc: str) -> None:
        try:
            ok = bool(pred())
        except Exception:  # noqa: BLE001 - e.g. a widget mid-destroy
            ok = False
        if ok:
            self.root.after(1, self._step, lambda: next(self.gen))
        elif time.monotonic() > deadline:
            err = TimeoutError(f"timed out waiting for {desc}")
            self._step(lambda: self.gen.throw(err))
        else:
            self.root.after(50, self._poll, pred, deadline, desc)


def until(pred: Callable[[], bool], timeout: float = 15.0, desc: str = "") -> Iterator[Any]:
    yield (pred, timeout, desc or getattr(pred, "__name__", "condition"))


class Shooter:
    """The scripted walk through every screen."""

    def __init__(self, app: Any, root: Any, lang: str, out: Path,
                 only: Optional[set], scrub: Scrubber, library: Path, data_dir: Path) -> None:
        self.app, self.root, self.lang, self.out = app, root, lang, out
        self.only, self.scrub, self.library, self.data_dir = only, scrub, library, data_dir
        self.GUI, self.st, self.i18n = app.GUI, app.app_state, app.i18n
        self.ctk, self.tk = app.ctk, app.tk
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.catalog = {n: (title, keys) for n, title, keys in CATALOG}

    # -- helpers ----------------------------------------------------------
    def wanted(self, name: str) -> bool:
        return self.only is None or name in self.only

    def t(self, key: str, **kw: Any) -> str:
        return self.i18n.t(key, **kw)

    def walk(self, widget: Any) -> Iterator[Any]:
        stack = [widget]
        while stack:
            w = stack.pop(0)
            yield w
            try:
                stack.extend(w.winfo_children())
            except self.tk.TclError:
                pass

    def find(self, parent: Any, cls: Any, text: Optional[str] = None,
             pred: Optional[Callable[[Any], bool]] = None) -> Any:
        for w in self.walk(parent):
            if not isinstance(w, cls):
                continue
            try:
                if text is not None and plain(w.cget("text")) != plain(text):
                    continue
                if pred is not None and not pred(w):
                    continue
            except (self.tk.TclError, ValueError, AttributeError):
                continue
            return w
        raise LookupError(f"no {cls.__name__} {text or ''!r} under {parent}")

    def toplevels(self) -> List[Any]:
        return [w for w in self.root.winfo_children() if isinstance(w, self.tk.Toplevel)]

    def names(self) -> set:
        return {str(w) for w in self.toplevels()}

    def new_toplevel(self, before: set, settle_ms: int = 1300, title: Optional[str] = None,
                     timeout: float = 30.0) -> Iterator[Any]:
        """Wait for a window that is not in `before` (optionally with this
        title, so a progress dialog in between is skipped)."""
        found: List[Any] = []
        want = plain(title) if title is not None else None

        def appeared() -> bool:
            for w in self.toplevels():
                if str(w) in before or not (w.winfo_exists() and w.winfo_viewable()):
                    continue
                if want is not None and plain(w.title()) != want:
                    continue
                found.append(w)
                return True
            return False

        yield from until(appeared, timeout, f"a new dialog {want or ''}")
        # _studio_dialog re-centres at 400/1000 ms and re-sets its icon at 300 ms.
        yield settle_ms
        return found[0]

    def close_window(self, win: Any) -> Iterator[Any]:
        """Close like the title-bar X (the window's own WM_DELETE_WINDOW handler)."""
        try:
            cmd = win.tk.call("wm", "protocol", win._w, "WM_DELETE_WINDOW")
            if cmd:
                win.tk.call(cmd)
            else:
                win.destroy()
        except self.tk.TclError:
            pass
        yield from until(lambda: not win.winfo_exists(), 10, "the dialog to close")
        yield 250

    def press(self, dlg: Any, label_key: Optional[str]) -> Iterator[Any]:
        """Click the dialog button labelled t(label_key); None = the close box."""
        if label_key is None:
            yield from self.close_window(dlg)
            return
        self.find(dlg, self.ctk.CTkButton, self.t(label_key)).invoke()
        yield from until(lambda: not dlg.winfo_exists(), 10, "the dialog to close")
        yield 250

    def chain(self, action: Optional[Callable[[], Any]],
              steps: List[Tuple[str, str, Optional[str], bool]],
              timeout: float = 30.0) -> Iterator[Any]:
        """Run `action` from the Tk loop (an in-app message blocks in a modal
        wait), then for each (name, title key, button key, save?) wait for that
        dialog, save it and press the button."""
        before = self.names()
        if action is not None:
            self.root.after(1, action)
        for name, title_key, button, save in steps:
            dlg = yield from self.new_toplevel(before, title=self.i18n.t_raw(title_key),
                                               timeout=timeout)
            before = self.names()
            if save:
                yield from self.shoot(name, dlg)
            yield from self.press(dlg, button)

    def message(self, name: str, title_key: str, button: Optional[str],
                action: Callable[[], Any]) -> Iterator[Any]:
        yield from self.chain(action, [(name, title_key, button, True)])

    def scrub_widgets(self, win: Any) -> None:
        for w in self.walk(win):
            try:
                text = w.cget("text")
            except (self.tk.TclError, ValueError, AttributeError):
                continue
            if isinstance(text, str) and text:
                clean = self.scrub(text)
                if clean != text:
                    w.configure(text=clean)

    def record(self, name: str, captured: bool, keys: Optional[List[str]] = None,
               **extra: Any) -> None:
        title, static_keys = self.catalog[name]
        entry = {"lang": self.lang, "name": name, "title": title,
                 "file": str(self.out / f"{name}.png") if captured else None,
                 "keys": keys if keys is not None else static_keys,
                 "captured": captured}
        entry.update(extra)
        self.entries[name] = entry
        log.info("%-24s %s", name, "captured" if captured else f"NOT captured {extra}")

    def shoot(self, name: str, win: Any, keys: Optional[List[str]] = None) -> Iterator[Any]:
        if not self.wanted(name):
            return
        self.scrub_widgets(win)
        win.update_idletasks()
        yield 150
        hwnd = int(win.wm_frame(), 16)
        try:
            size = capture_hwnd(hwnd, self.out / f"{name}.png")
        except OSError:
            yield 500  # one retry: a window that has not composed yet
            size = capture_hwnd(hwnd, self.out / f"{name}.png")
        self.record(name, True, keys, size=list(size))

    def shoot_main(self, name: str, extra_keys: Tuple[str, ...] = ()) -> Iterator[Any]:
        self.root.focus_set()
        yield 400
        yield from self.shoot(name, self.root, self.main_keys(extra_keys))

    def main_keys(self, extra: Tuple[str, ...]) -> List[str]:
        GUI, st = self.GUI, self.st
        keys = list(MAIN_BASE)
        if not st.monitored_folder:
            keys.append("not_selected")
        if st.monitoring_flag:
            if st.styled_count:
                keys += ["watching_count", "files_one" if st.styled_count == 1 else "files_many"]
            else:
                keys.append("watching")
        else:
            keys.append("not_watching")
        if not (GUI._preview_bg_on and GUI._preview_bg is not None):
            keys.append("video_frame")
        if not GUI._selected_lang_codes():
            keys.append("no_langs_selected")
        if st.last_event:
            ts, verb, name = st.last_event
            keys.append(self.app._EVENT_VERB_KEY.get(verb, "ev_styled"))
            count = re.search(r"\d+", name)
            for noun in ("event_files_one", "event_files_many",
                         "event_videos_one", "event_videos_many"):
                if count and self.i18n.t_logical(noun, n=int(count.group())) == name:
                    keys.append(noun)
            if not st.monitoring_flag:
                keys.append("ev_idle_last")
            age = time.time() - ts
            keys.append("rel_just_now" if age < 60 else "rel_min" if age < 3600
                        else "rel_hour" if age < 86400 else "rel_day")
        else:
            keys.append("watching_for_files" if st.monitoring_flag else "idle")
        n = self.app.IssueTracker.count()
        keys.append("no_issues" if n == 0 else "issues_one" if n == 1 else "issues_many")
        return keys + [k for k in extra if k not in keys]

    def native(self, name: str, title_key: str, click: str,
               action: Callable[[], Any]) -> Iterator[Any]:
        """A NATIVE Windows dialog (colour picker, startup-crash box): `action`
        blocks in it, so a helper thread saves it and presses `click`."""
        shot = self.out / f"{name}.png" if self.wanted(name) else None
        box = NativeBox(name, self.i18n.t_raw(title_key), click, shot)
        box.start()
        action()
        yield from until(box.done.is_set, box.timeout + 15, f"native dialog {name}")
        if box.error:
            raise RuntimeError(f"{name}: {box.error}")
        if self.wanted(name):
            self.record(name, True, native=box.info, size=list(box.size or ()))
        yield 300

    def idle_op(self) -> Iterator[Any]:
        yield from until(lambda: not self.st.operation_in_progress, 120,
                         "the running operation to finish")
        yield 300

    def recover(self) -> Iterator[Any]:
        """After a failed block: dismiss stray dialogs, clear flags."""
        for hwnd in _own_dialogs():
            user32.PostMessageW(hwnd, WM_COMMAND, 2, 0)
        yield 500
        for w in self.toplevels():
            try:
                cmd = w.tk.call("wm", "protocol", w._w, "WM_DELETE_WINDOW")
                if cmd:
                    w.tk.call(cmd)
                if w.winfo_exists():
                    w.grab_release()
                    w.destroy()
            except self.tk.TclError:
                pass
        self.st.operation_in_progress = False
        yield 800

    # -- the script --------------------------------------------------------
    def script(self) -> Iterator[Any]:
        yield from self.startup()
        blocks = [self.b_main, self.b_overflow, self.b_font_picker, self.b_color,
                  self.b_preview_text, self.b_langs, self.b_first_watch, self.b_watching,
                  self.b_issues, self.b_simple_messages, self.b_about_update_crash,
                  self.b_no_backups, self.b_apply, self.b_extract, self.b_restore,
                  self.b_real_bg, self.b_progress, self.b_reset, self.b_welcome]
        for block in blocks:
            try:
                yield from block()
            except Exception as e:  # noqa: BLE001 - keep going, report it
                log.error("block %s failed: %s\n%s", block.__name__, e, traceback.format_exc())
                self.failures.append(f"{block.__name__}: {type(e).__name__}: {e}")
                yield from self.recover()

    failures: List[str] = []

    def startup(self) -> Iterator[Any]:
        GUI, st = self.GUI, self.st
        self.failures = []
        yield from until(lambda: self.app.PreviewRenderer.path_map_ready and st.all_fonts,
                         180, "the font path map")
        # What main() does on its background thread: locate ffmpeg.
        st.ffmpeg_available = bool(self.app.FFmpegLocator.get_binary("ffmpeg")
                                   and self.app.FFmpegLocator.get_binary("ffprobe"))
        GUI.update_extraction_ui_state()
        if st.monitoring_flag:  # a consented folder auto-starts at launch
            GUI.toggle_monitoring()
        self.app.IssueTracker.clear()
        st.last_event, st.styled_count = None, 0
        GUI._refresh_status_ui()
        GUI._refresh_issues_label()
        GUI.update_preview()
        yield 1500

    def set_font(self, family: str) -> None:
        entry = self.st.font_combobox
        entry.delete(0, "end")
        entry.insert(0, family)
        self.GUI._refresh_nudge_control()
        self.GUI.update_preview()

    def b_main(self) -> Iterator[Any]:
        yield from self.shoot_main("main_idle")
        self.set_font(LATIN_ONLY_FONT)
        yield from self.shoot_main("main_font_no_arabic", ("font_no_arabic",))
        self.set_font(NO_TEXT_FONT)
        yield from self.shoot_main("main_font_no_neither", ("font_no_neither",))
        self.set_font(UI_FONT)
        yield 300

    def b_overflow(self) -> Iterator[Any]:
        more = self.find(self.root, self.ctk.CTkButton, "⋯")
        before = self.names()
        more.invoke()
        pop = next(w for w in self.toplevels() if str(w) not in before)
        pop.unbind("<FocusOut>")  # our capture must not depend on window focus
        pop = yield from self.new_toplevel(before, settle_ms=500)
        yield from self.shoot("menu_overflow", pop)
        more.invoke()  # a second click closes it
        yield 300

    def b_font_picker(self) -> Iterator[Any]:
        app, ctk = self.app, self.ctk
        hint = self.find(self.root, ctk.CTkLabel, self.t("font"))
        before = self.names()
        hint._label.event_generate("<Button-1>")
        dlg = yield from self.new_toplevel(before)
        self.find(dlg, self.tk.Text).focus_set()
        yield 500
        yield from self.shoot("font_picker", dlg)

        script_name = self.i18n.t_logical("script_arabic")
        chip = self.find(dlg, ctk.CTkButton,
                         self.i18n.t_logical("capable_chip", script=script_name))
        chip_text = lambda: plain(chip.cget("text"))  # noqa: E731
        fs = app.FontSupport
        lines = [ln for ln in (self.st.preview_text or "").split("\n") if ln.strip()]
        source = next((ln for ln in lines if fs.test_key(ln)), lines[0] if lines else "")
        fs._cache.pop(fs.test_key(source), None)  # force the slow (scanning) path
        gates = [threading.Event() for _ in range(3)]
        real_filter = fs.filter_supporting

        def held_filter(families, preview_text, on_progress=None, cancel=None,
                        on_scan_progress=None):
            gates[0].wait(60)
            if on_scan_progress:
                on_scan_progress(1, 4)
            gates[1].wait(60)
            if on_progress:
                on_progress(1, 2)
            gates[2].wait(60)
            return real_filter(families, preview_text, cancel=cancel)

        with mock.patch.object(fs, "filter_supporting", held_filter):
            chip.invoke()
            want = plain(self.i18n.t_logical("capable_scanning", script=script_name))
            yield from until(lambda: chip_text() == want, 10, "the scanning chip")
            yield 300
            yield from self.shoot("font_picker_scan_start", dlg)
            gates[0].set()
            want = plain(self.i18n.t_logical("scan_indexing", pct=25))
            yield from until(lambda: chip_text() == want, 10, "the indexing chip")
            yield 300
            yield from self.shoot("font_picker_indexing", dlg)
            gates[1].set()
            want = plain(self.i18n.t_logical("scan_scanning", pct=50))
            yield from until(lambda: chip_text() == want, 10, "the scanning-percent chip")
            yield 300
            yield from self.shoot("font_picker_scanning", dlg)
            gates[2].set()
            yield from until(lambda: "✓" in chip_text(), 60, "the filtered list")
        yield 800
        yield from self.shoot("font_picker_capable", dlg)

        favorites = self.i18n.t_logical("favorites")
        fav = self.find(dlg, ctk.CTkButton,
                        pred=lambda w: favorites in plain(w.cget("text")))
        fav.invoke()
        yield 900
        yield from self.shoot("font_picker_favorites", dlg)
        fav.invoke()
        yield 600

        search = self.find(dlg, ctk.CTkEntry)
        search.insert(0, "zzqx")
        search._entry.event_generate("<KeyRelease>")  # as typing would
        yield 900  # 150 ms search debounce + specimen render
        self.find(dlg, self.tk.Text).focus_set()
        yield 300
        yield from self.shoot("font_picker_no_match", dlg)
        search.delete(0, "end")
        search._entry.event_generate("<KeyRelease>")
        yield 400
        yield from self.close_window(dlg)

    def b_color(self) -> Iterator[Any]:
        yield from self.native("color_picker", "choose_color_title", "cancel",
                               self.GUI.choose_color)

    def b_preview_text(self) -> Iterator[Any]:
        btn = self.find(self.root, self.ctk.CTkButton, self.t("preview_text_btn"))
        before = self.names()
        btn.invoke()
        dlg = yield from self.new_toplevel(before)
        # Take Win32 focus off the native RichEdit (same thread) so its
        # blinking caret is never in the image.
        user32.SetFocus(dlg.winfo_id())
        yield 400
        yield from self.shoot("preview_text_editor", dlg)
        yield from self.close_window(dlg)

    def open_langs(self) -> Iterator[Any]:
        before = self.names()
        self.st.extraction_widgets["chip"]._label.event_generate("<Button-1>")
        dlg = yield from self.new_toplevel(before)
        return dlg

    def lang_chip(self, dlg: Any, code: str) -> Any:
        return self.find(dlg, self.ctk.CTkButton,
                         pred=lambda w: f"{code} ·" in plain(w.cget("text")))

    def b_langs(self) -> Iterator[Any]:
        st, GUI = self.st, self.GUI
        st.ffmpeg_available = False
        GUI.update_extraction_ui_state()
        dlg = yield from self.open_langs()
        yield from self.shoot("langs_no_ffmpeg", dlg)
        yield from self.close_window(dlg)
        st.ffmpeg_available = True
        GUI.update_extraction_ui_state()

        dlg = yield from self.open_langs()
        for code in ("ara", "eng"):
            self.lang_chip(dlg, code).invoke()  # each toggle rebuilds the chips
            yield 300
        yield 400
        yield from self.shoot("langs_dialog", dlg)
        self.find(dlg, self.ctk.CTkButton, self.t("langs_more", n=len(LANGS_TAIL))).invoke()
        yield 800
        yield from self.shoot("langs_dialog_expanded", dlg)
        yield from self.close_window(dlg)

    def b_first_watch(self) -> Iterator[Any]:
        """Watch asks once per folder before its first restyle."""
        st, GUI = self.st, self.GUI
        library = st.monitored_folder
        empty = self.library / "Upcoming (2032)"
        empty.mkdir(exist_ok=True)
        st.monitored_folder = empty.as_posix()
        try:
            yield from self.message("first_watch_none", "first_watch_title", "cancel",
                                    GUI.toggle_monitoring)
        finally:
            st.monitored_folder = library
        with mock.patch.object(GUI, "_risky_folder", lambda folder: True):
            yield from self.message("first_watch_risky", "first_watch_title", "cancel",
                                    GUI.toggle_monitoring)
        yield from self.message("first_watch", "first_watch_title", "first_watch_start",
                                GUI.toggle_monitoring)
        yield from until(lambda: st.monitoring_flag, 10, "watching to start")
        GUI.toggle_monitoring()  # b_watching starts it again with extraction on
        yield from until(lambda: not st.monitoring_flag, 5, "watching to stop")

    def b_watching(self) -> Iterator[Any]:
        st, GUI = self.st, self.GUI
        st.extraction_widgets["label"]._label.event_generate("<Button-1>")  # extraction on
        GUI.toggle_monitoring()
        yield from until(lambda: st.monitoring_flag, 5, "watching to start")
        yield 300
        yield from self.shoot_main("main_watching_empty")
        yield from self.message("mb_switch_folder", "switch_folder_title", "no", GUI.select_folder)
        yield from self.message("mb_stop_before_apply", "warn_title", "ok",
                                GUI.process_existing_ass)
        yield from self.message("mb_stop_before_extract_all", "warn_title", "ok",
                                GUI.process_all_extraction)
        yield from self.message("mb_stop_before_restore", "warn_title", "ok",
                                GUI.restore_originals)
        yield from self.message("mb_stop_before_reset", "warn_title", "ok", GUI.reset_app)
        for name in ("Lantern Harbor Diaries - 01.en.ass",
                     "[DemoSubs] Sky Garden - 02 [720p].ar.ass",
                     "[DemoSubs] Sky Garden - 01 [1080p].ar.ass"):
            st.note_event("Styled", name, styled=True)
        self.seed_issues()
        yield 600
        yield from self.shoot_main("main_watching")
        GUI.toggle_monitoring()
        yield from until(lambda: not st.monitoring_flag, 5, "watching to stop")
        # The last scan is still finishing (a slow disk): Watch says so.
        release = threading.Event()
        lingering = threading.Thread(target=release.wait, args=(30,), daemon=True)
        lingering.start()
        st.monitoring_thread = lingering
        try:
            yield from self.message("mb_still_stopping", "busy_title", "ok",
                                    GUI.toggle_monitoring)
        finally:
            release.set()
            st.monitoring_thread = None
        yield 300

    def seed_issues(self) -> None:
        tracker = self.app.IssueTracker
        tracker.clear()
        label = self.scrub(str(self.library))
        timeout = getattr(self.app, "FFMPEG_TIMEOUT", 120)
        seeds = [  # (op, file, reason as the engine words it, minutes before FROZEN_NOW)
            ("decode", "Lantern Harbor Diaries - 01.ja.ass",
             "No supported encoding matched this file (tried utf-8, cp1256, cp1252, "
             "shift_jis, …)", 3 * 24 * 60 + 12),
            ("extract", "حديقة السماء - الحلقة 01.mkv",
             f"stream #2: ffmpeg timed out after {timeout}s", 2 * 24 * 60 + 47),
            ("read", "[DemoSubs] Sky Garden - 02 [720p].en.ass",
             "[WinError 32] The process cannot access the file because it is being used "
             f"by another process: '{label}\\Sky Garden (2031)\\[DemoSubs] Sky Garden - 02 [720p].en.ass'",
             75),
            ("foreign", "[DemoSubs] Sky Garden - 01 [1080p].ar.ass",
             "changed by another program after it was styled, so it was left as it is; "
             "Apply to library restyles it", 16),
            ("restore", "حديقة السماء - الحلقة 02.ar.ass",
             "backup is empty (0 bytes); not restored", 3),
        ]
        for op, name, reason, _m in seeds:
            tracker.record(op, name, reason)
        with tracker._lock:
            for issue, (*_x, minutes) in zip(tracker._issues, seeds):
                issue.timestamp = (FROZEN_NOW - timedelta(minutes=minutes)).timestamp()

    def b_issues(self) -> Iterator[Any]:
        GUI, tracker = self.GUI, self.app.IssueTracker

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):  # noqa: ANN001
                return FROZEN_NOW

        for name in ("issues_panel", "issues_panel_empty"):
            before = self.names()
            with mock.patch.object(self.app, "datetime", FrozenDateTime):
                GUI.show_issues_panel()  # builds its text synchronously
            dlg = yield from self.new_toplevel(before)
            yield from self.shoot(name, dlg)
            yield from self.close_window(dlg)
            tracker.clear()
        tracker.record("restore", "حديقة السماء - الحلقة 02.ar.ass",
                       "backup is empty (0 bytes); not restored")
        self.st.last_event = (time.time() - 2 * 3600 - 120, "Styled",
                              "[DemoSubs] Sky Garden - 01 [1080p].ar.ass")
        GUI._refresh_status_ui()
        yield 400
        yield from self.shoot_main("main_idle_last")

    def b_simple_messages(self) -> Iterator[Any]:
        st, GUI, app = self.st, self.GUI, self.app
        yield from self.message("mb_exit_prompt", "exit_title", "cancel", GUI.on_close_request)
        with mock.patch.object(app.subprocess, "Popen", side_effect=OSError("stubbed")):
            yield from self.message("mb_restart_failed", "restart_to_apply_title", "ok",
                                    GUI.restart_app)
        st.operation_in_progress = True
        try:
            yield from self.message("mb_busy", "busy_title", "ok", GUI.select_folder)
        finally:
            st.operation_in_progress = False
        folder = st.monitored_folder
        st.monitored_folder = None
        try:
            yield from self.message("mb_select_folder_first", "warn_title", "ok",
                                    GUI.toggle_monitoring)
        finally:
            st.monitored_folder = folder
        color, extract = st.selected_color_code, st.extract_enabled.get()
        st.selected_color_code = None
        st.extract_enabled.set(False)
        try:
            yield from self.message("mb_nothing_to_watch", "warn_title", "ok",
                                    GUI.toggle_monitoring)
            yield from self.message("mb_select_font_color", "warn_title", "ok",
                                    GUI.process_existing_ass)
        finally:
            st.selected_color_code = color
            st.extract_enabled.set(extract)
        st.ffmpeg_available = False
        try:
            yield from self.message("mb_ffmpeg_not_found", "missing_tools_title", "ok",
                                    GUI.process_all_extraction)
        finally:
            st.ffmpeg_available = True
        missing = self.data_dir / "docs-shots-missing.log"
        with mock.patch.object(app, "LOG_PATH", missing):
            yield from self.message("mb_log_not_found", "error_title", "ok", GUI.open_log_file)
        present = self.data_dir / "monitor.log"
        present.write_text("", encoding="utf-8")
        err = OSError(22, "No application is associated with the specified file for this "
                          "operation", r"C:\Users\Demo\AppData\Local\Anime Subs Customizer"
                          r"\monitor.log", 1155)
        with mock.patch.object(app, "LOG_PATH", present), \
                mock.patch.object(app.os, "startfile", side_effect=err, create=True):
            yield from self.message("mb_cant_open_log", "error_title", "ok", GUI.open_log_file)
        GUI._refresh_status_ui()

    def b_about_update_crash(self) -> Iterator[Any]:
        app, GUI, cfg = self.app, self.GUI, self.st.config
        yield from self.message("about", "menu_about", "close", GUI.show_about)
        newer = "v9.9.0"  # any version that differs from the running one
        cfg.notified_update = newer
        try:
            yield from self.message("about_update", "menu_about", "close", GUI.show_about)
        finally:
            cfg.notified_update = ""
        # The daily check (packaged builds, opt-in) with GitHub stubbed out.
        cfg.update_check, cfg.last_update_check = True, 0.0
        try:
            with mock.patch.object(app, "is_frozen", lambda: True), \
                    mock.patch.object(app.updates, "fetch_latest_tag", lambda *a, **k: newer):
                yield from self.message("update_available", "update_available_title",
                                        "welcome_later", GUI._maybe_check_updates)
        finally:
            cfg.update_check, cfg.last_update_check, cfg.notified_update = False, 0.0, ""
        yield from self.message("crash_dialog", "crash_title", "close",
                                lambda: app._show_crash_dialog("(demo details)"))
        yield from self.native(
            "mb_native_crash_start", "crash_title", "ok",
            lambda: GUI._native_message(self.i18n.t_raw("crash_title"),
                                        self.i18n.t_raw("crash_start_body", path=(
                                            "%LOCALAPPDATA%\\Anime Subs Customizer")),
                                        "error"))

    def b_no_backups(self) -> Iterator[Any]:
        yield from self.message("mb_no_backups", "no_backups_title", "ok",
                                self.GUI.restore_originals)
        yield from self.idle_op()

    def b_apply(self) -> Iterator[Any]:
        before = self.names()
        self.GUI.process_existing_ass()
        dlg = yield from self.new_toplevel(before)
        yield from self.shoot("apply_scope", dlg)
        yield from self.chain(
            self.find(dlg, self.ctk.CTkButton, self.t("apply_whole")).invoke,
            [("mb_apply_summary", "done_title", "ok", True)], timeout=180)
        yield from self.idle_op()

    def b_extract(self) -> Iterator[Any]:
        yield from self.chain(self.GUI.process_all_extraction, [
            ("mb_reextract_confirm", "confirm_title", "yes", True),
            ("mb_extracted", "done_title", "ok", True)], timeout=180)
        yield from self.idle_op()
        yield from self.shoot_main("main_after_extract")

    def b_restore(self) -> Iterator[Any]:
        st, GUI = self.st, self.GUI
        library = st.monitored_folder
        st.monitored_folder = str(self.library / "Sky Garden (2031)")
        try:
            yield from self.message("mb_confirm_restore", "confirm_restore_title", "no",
                                    GUI.restore_originals)
            yield from self.idle_op()
        finally:
            st.monitored_folder = library
        # One empty backup -> a real "could not restore" failure.
        backups = sorted((self.library / "[DemoSubs] Velvet Comet Patrol [BD 1080p]"
                          / "Original Subs").glob("*.ass"))
        backups[0].write_bytes(b"")

        before = self.names()
        self.root.after(1, GUI.restore_originals)  # the picker waits in a modal loop
        dlg = yield from self.new_toplevel(before, title=self.i18n.t_raw("restore_title"))
        yield from self.shoot("restore_picker_idle", dlg)
        boxes = [w for w in self.walk(dlg) if isinstance(w, self.ctk.CTkCheckBox)]
        for cb in boxes[:2]:
            cb.toggle()
        yield 400
        yield from self.shoot("restore_picker", dlg)
        self.find(dlg, self.ctk.CTkLabel, self.t("select_all"))._label.event_generate("<Button-1>")
        yield 400
        yield from self.shoot("restore_picker_all", dlg)
        primary = self.find(dlg, self.ctk.CTkButton,
                            pred=lambda w: plain(w.cget("text")) != plain(self.t("cancel")))
        yield from self.chain(primary.invoke, [("mb_restored", "done_title", "ok", True)],
                              timeout=180)
        yield from self.idle_op()
        yield from self.shoot_main("main_after_restore")

    def b_real_bg(self) -> Iterator[Any]:
        GUI, app = self.GUI, self.app
        mytext, sub = self.t("bg_mode_mytext"), GUI._bg_sub_label
        GUI._bg_mode_seg.set(sub)
        GUI._on_bg_mode(sub)
        random.seed(7)
        GUI._bg_switch.select()
        GUI.toggle_preview_bg()  # no saved frame -> grabs one off-thread
        ready = lambda: (GUI._preview_bg is not None  # noqa: E731
                         and GUI._shuffle_btn.cget("state") == "normal")
        yield from until(ready, 90, "a real video frame")
        yield 800
        yield from self.shoot_main("main_real_bg")
        previous = GUI._preview_bg
        with mock.patch.object(app.PreviewBackground, "grab_line_frame", return_value=None):
            random.seed(7)
            GUI.shuffle_preview_background()
            yield from until(lambda: GUI._preview_bg is not previous and ready(), 90,
                             "a frame with no subtitle line")
        yield 800
        yield from self.shoot_main("main_no_sub_found", ("no_sub_found",))
        GUI._bg_switch.deselect()
        GUI.toggle_preview_bg()
        GUI._bg_mode_seg.set(mytext)
        GUI._on_bg_mode(mytext)
        yield 500

    def b_progress(self) -> Iterator[Any]:
        steps = (
            ("progress_apply", "prog_apply", (5, 16, "[DemoSubs] Sky Garden - 01 [1080p].ar.ass")),
            ("progress_extract", "prog_extract", None),
            ("progress_restore", "prog_restore", (12, 12, self.t("done"))),
        )
        for name, title_key, update_args in steps:
            before = self.names()
            win, update, close = self.GUI._build_progress_dialog(
                self.t(title_key), self.t("prog_scanning"))
            if update_args:
                update(*update_args)
            dlg = yield from self.new_toplevel(before)
            yield from self.shoot(name, dlg)
            close()
            yield from until(lambda w=win: not w.winfo_exists(), 10, "the progress dialog to close")
            yield 300

    def open_reset(self) -> Iterator[Any]:
        before = self.names()
        self.GUI.reset_app()
        dlg = yield from self.new_toplevel(before)
        rows = [w for w in self.walk(dlg) if isinstance(w, self.ctk.CTkCheckBox)]
        button = self.find(dlg, self.ctk.CTkButton, self.t("reset_selected_btn"))
        return dlg, rows, button

    def b_reset(self) -> Iterator[Any]:
        app = self.app
        dlg, rows, button = yield from self.open_reset()
        yield from self.shoot("reset_dialog", dlg)
        for cb in rows:
            cb.toggle()
        yield 400
        yield from self.shoot("reset_dialog_selected", dlg)
        yield from self.message("mb_confirm_reset", "confirm_reset_title", "no", button.invoke)
        yield from self.close_window(dlg)

        dlg, rows, button = yield from self.open_reset()
        rows[1].toggle()  # subtitle index only
        yield 300
        with mock.patch.object(app.DatabaseManager, "clear_monitored_files",
                               side_effect=sqlite3.OperationalError("database is locked")):
            yield from self.chain(button.invoke, [
                ("mb_confirm_reset", "confirm_reset_title", "yes", False),
                ("mb_reset_errors", "reset_errors_title", "ok", True)])

        dlg, rows, button = yield from self.open_reset()
        rows[0].toggle()  # configuration only
        yield 300
        yield from self.chain(button.invoke, [
            ("mb_confirm_reset", "confirm_reset_title", "yes", False),
            ("mb_reset_done", "reset_title", "ok", True)])
        yield 600
        yield from self.shoot_main("main_no_folder")

    # -- README images ---------------------------------------------------
    DOCS_STEPS = (  # (font, BGR colour, watching?, styled events?, ms on screen)
        ("Arial", "FFFFFF", False, False, 1300),
        ("Times New Roman", "FFFFFF", False, False, 1000),
        ("Segoe UI Semibold", "FFFFFF", False, False, 1000),
        ("Segoe UI Semibold", "FFFF00", False, False, 1000),
        ("Segoe UI Semibold", "00FFFF", False, False, 1200),
        ("Segoe UI Semibold", "00FFFF", True, False, 1200),
        ("Segoe UI Semibold", "00FFFF", True, True, 2800),
    )

    def set_color(self, bgr: str) -> None:
        """What choosing a colour in the picker does to the window."""
        self.st.selected_color_code = bgr
        rgb = self.app.bgr_to_rgb_hex(bgr)
        self.st.color_indicator.configure(fg_color=rgb, hover_color=rgb)
        self.GUI.update_preview()

    def docs_script(self) -> Iterator[Any]:
        """main.png (watching, with the preview) and demo.gif (a font, a colour,
        then Watch) in this language, from the synthetic library only."""
        yield from self.startup()
        st, GUI = self.st, self.GUI
        frames: List[Tuple[Any, int]] = []
        for font, color, watching, styled, ms in self.DOCS_STEPS:
            self.set_font(font)
            self.set_color(color)
            if watching and not st.monitoring_flag:
                GUI.toggle_monitoring()  # the folder was confirmed in config
                yield from until(lambda: st.monitoring_flag, 10, "watching to start")
            if styled and not st.styled_count:
                for name in ("Lantern Harbor Diaries - 01.en.ass",
                             "[DemoSubs] Sky Garden - 02 [720p].ar.ass",
                             "[DemoSubs] Sky Garden - 01 [1080p].ar.ass"):
                    st.note_event("Styled", name, styled=True)
            self.root.focus_set()
            yield 900
            frames.append((capture_image(int(self.root.wm_frame(), 16)), ms))
        self.out.mkdir(parents=True, exist_ok=True)
        save_png(frames[-1][0], self.out / "main.png", width=1300)
        save_gif(frames, self.out / "demo.gif", width=800)
        log.info("docs images -> %s", self.out)
        GUI.toggle_monitoring()

    def b_welcome(self) -> Iterator[Any]:
        """First run: no folder yet (the reset above cleared it)."""
        yield from self.message("welcome", "welcome_title", "welcome_later",
                                self.GUI._maybe_welcome)


def _scaled(img: Any, width: int) -> Any:
    from PIL import Image
    if img.width <= width:
        return img
    return img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)


def save_png(img: Any, path: Path, width: int) -> None:
    """A README screenshot: scaled down, 256 colours (the dark UI has few)."""
    _scaled(img, width).quantize(colors=256, method=2, dither=0).save(path, optimize=True)


def save_gif(frames: List[Tuple[Any, int]], path: Path, width: int) -> None:
    """A looping GIF. Every frame shares one palette, built from all of them,
    so the unchanged parts of the window stay identical (and compress)."""
    from PIL import Image
    scaled = [(_scaled(img, width).convert("RGB"), ms) for img, ms in frames]
    w, h = scaled[0][0].size
    montage = Image.new("RGB", (w, h * len(scaled)))
    for i, (img, _ms) in enumerate(scaled):
        montage.paste(img, (0, i * h))
    palette = montage.quantize(colors=255, method=2, dither=0)
    images = [img.quantize(palette=palette, dither=0) for img, _ms in scaled]
    images[0].save(path, save_all=True, append_images=images[1:], loop=0,
                   duration=[ms for _img, ms in scaled], optimize=True, disposal=1)


# ----------------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------------
def app_revision() -> str:
    """The checkout the screens were taken from ("<sha>", "+dirty" if edited)."""
    try:
        sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--",
                                "anime_subs", "anime_subs_customizer.pyw"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return sha + ("+dirty" if dirty else "") if sha else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_manifest(path: Path, entries: List[Dict[str, Any]], meta: Dict[str, Any]) -> None:
    base = path.parent
    for e in entries:
        if e.get("file"):
            f = Path(e["file"])
            try:
                e["file"] = f.relative_to(base).as_posix()
            except ValueError:
                e["file"] = str(f)
    data: Dict[str, Any] = {"generator": "tools/screenshots.py", "entries": []}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    new = {(e["lang"], e["name"]) for e in entries}
    kept = [e for e in data.get("entries", []) if (e.get("lang"), e.get("name")) not in new]
    order = {n: i for i, (n, *_r) in enumerate(CATALOG + [(u[0],) for u in UNCAPTURED])}
    data["entries"] = sorted(kept + entries, key=lambda e: (
        e.get("lang", ""), order.get(e.get("name"), 999), e.get("name", "")))
    data["generated_at"] = datetime.now().isoformat(timespec="seconds")
    data.setdefault("runs", {})[meta["lang"]] = meta
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------
def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lang", choices=("ar", "en"), required=True, help="UI language")
    ap.add_argument("--out", type=Path, required=True, help="folder for the PNGs")
    ap.add_argument("--folder-label", default=r"D:\Videos\Anime",
                    help=r"watch folder shown in the UI (default: D:\Videos\Anime)")
    ap.add_argument("--only", default="", help="comma-separated screen names to write")
    ap.add_argument("--library", type=Path,
                    help="a library made by tools/make_demo_library.py (default: "
                         "generate one, seed 7, in the instance folder)")
    ap.add_argument("--manifest", type=Path, help="manifest path (default: OUT/../manifest.json)")
    ap.add_argument("--token", default="docs-shots", help="ASC_INSTANCE_TOKEN (default docs-shots)")
    ap.add_argument("--timeout", type=int, default=900,
                    help="abort with a stack dump after this many seconds (default 900)")
    ap.add_argument("--docs", action="store_true",
                    help="write the README images (OUT/main.png, OUT/demo.gif) instead")
    args = ap.parse_args(argv)
    names = {n for n, *_r in CATALOG} | {u[0] for u in UNCAPTURED}
    args.only_set = {s.strip() for s in args.only.split(",") if s.strip()} or None
    unknown = (args.only_set or set()) - names
    if unknown:
        ap.error(f"unknown screen name(s): {', '.join(sorted(unknown))}")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", args.token):
        ap.error("--token must be 1-40 of A-Z a-z 0-9 _ -")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    log.setLevel(logging.INFO)
    faulthandler.dump_traceback_later(args.timeout, exit=True)

    # Isolation BEFORE the app is imported: the token moves every data file,
    # the mutex and the Startup folder; argv must not carry --minimized etc.
    os.environ["ASC_INSTANCE_TOKEN"] = args.token
    sys.argv = [str(APP_PATH)]
    sys.path.insert(0, str(ROOT))
    from anime_subs import runtime
    data_dir = runtime.APPDATA_DIR
    if runtime.INSTANCE_TOKEN != args.token or data_dir.name != f"asc-instance-{args.token}":
        raise SystemExit(f"instance isolation not in effect (data folder {data_dir})")
    if runtime.CONFIG_FILE_PATH.parent != data_dir:
        raise SystemExit("config.json would not live in the instance folder")

    from anime_subs import instance
    guard = instance.SingleInstance()
    if not guard.acquire():
        raise SystemExit(f"instance '{args.token}' is already running")

    reset_instance_state(data_dir)
    library = prepare_library(args.library.resolve() if args.library else None, data_dir)
    config = {
        "font": UI_FONT, "color": UI_COLOR, "folder": library.as_posix(),
        "font_size": "medium", "ui_language": args.lang, "favorite_fonts": FAVORITES,
        "extract_enabled": False, "extract_langs": "",
        "preview_bg_enabled": False, "preview_bg_from_sub": False,
        # A current-schema config whose folder has not been confirmed for
        # Watch yet, so the first-watch question is part of the tour.
        "schema_version": 1, "consented_folders": [], "welcome_done": True,
        "tray_hint_shown": True, "update_check": False,
    }
    if args.docs:  # a watched folder that was already confirmed
        config.update(font="Arial", color="FFFFFF", extract_enabled=True,
                      extract_langs="ara,eng",
                      consented_folders=[runtime.folder_key(library.as_posix())])
    runtime.CONFIG_FILE_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")

    revision = app_revision()
    app = load_app_module()
    if app.TrayIconManager._startup_shortcut_path() is None or \
            not Path(app.TrayIconManager._startup_shortcut_path()).is_relative_to(data_dir):
        raise SystemExit("the Startup folder is not redirected into the instance folder")
    app._instance_guard = guard  # restart_app releases/re-acquires OUR mutex
    scrub = Scrubber(args.folder_label, library, data_dir)

    # -- stubs ---------------------------------------------------------------
    app.TrayIconManager.set_autostart = staticmethod(lambda enabled: None)

    class QuietMonitor(app.FolderMonitor):
        """Watching without touching a file: the loop just waits for Stop."""
        def run(self) -> None:
            (getattr(self, "stop_event", None) or self.state.stop_event).wait()

    app.FolderMonitor = QuietMonitor
    real_on_fonts = app.GUI._on_fonts_loaded
    app.GUI._on_fonts_loaded = staticmethod(
        lambda fonts: real_on_fonts([f for f in fonts if f in STOCK_FONTS]))
    real_folder_display = app.GUI._set_folder_display
    app.GUI._set_folder_display = staticmethod(
        lambda folder: real_folder_display(scrub(folder) if folder else folder))
    real_risky = getattr(app.GUI, "_risky_folder", None)
    if real_risky is not None:
        # The scratch library sits under %LOCALAPPDATA%\Temp, which the app
        # rightly calls a system folder; judge the folder the screens show.
        app.GUI._risky_folder = staticmethod(lambda folder: real_risky(scrub(folder)))
    import tkinter.messagebox as tk_messagebox
    real_show = tk_messagebox._show

    def scrubbed_show(title=None, message=None, _icon=None, _type=None, **options):  # noqa: ANN001
        return real_show(scrub(title), scrub(message), _icon, _type, **options)

    tk_messagebox._show = scrubbed_show
    real_message = getattr(app.GUI, "message", None)
    if real_message is not None:  # the app's own bilingual message dialog
        app.GUI.message = staticmethod(
            lambda title, body, **kw: real_message(scrub(title), scrub(body), **kw))

    # -- run -----------------------------------------------------------------
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    random.seed(7)
    app.DatabaseManager.init_db()
    root = app.GUI.create_gui()
    root.title(app.APP_NAME)  # not "[TEST <token>]"
    shooter = Shooter(app, root, args.lang, out, args.only_set, scrub, library, data_dir)
    outcome: Dict[str, Any] = {}

    def finished(error: Optional[BaseException]) -> None:
        if error is not None:
            outcome["error"] = "".join(traceback.format_exception(error))
        for w in shooter.toplevels():
            try:
                w.destroy()
            except Exception:  # noqa: BLE001
                pass
        app.app_state.shutdown()
        root.after(200, root.destroy)

    script = shooter.docs_script() if args.docs else shooter.script()
    root.after(800, Runner(root, script, finished).start)
    try:
        root.mainloop()
    finally:
        faulthandler.cancel_dump_traceback_later()

    # -- report --------------------------------------------------------------
    if args.docs:
        if outcome.get("error"):
            print(outcome["error"])
            return 1
        print(f"{args.lang}: README images -> {out}")
        return 0
    entries = [shooter.entries[n] for n, *_r in CATALOG if n in shooter.entries]
    missing = [n for n, *_r in CATALOG
               if n not in shooter.entries and shooter.wanted(n)]
    for name in missing:
        title, keys = shooter.catalog[name]
        entries.append({"lang": args.lang, "name": name, "title": title, "file": None,
                        "keys": keys, "captured": False,
                        "reason": "capture failed - see the run's log"})
    for name, title, keys, reason in UNCAPTURED:
        if shooter.wanted(name):
            app.i18n.set_language(args.lang)
            strings = {k: app.i18n.t_logical(k) for k in keys}
            entries.append({"lang": args.lang, "name": name, "title": title, "file": None,
                            "keys": keys, "captured": False, "reason": reason,
                            "strings": strings})
    manifest = args.manifest or out.parent / "manifest.json"
    write_manifest(manifest.resolve(), entries, {
        "lang": args.lang, "app_version": app.APP_VERSION, "app_revision": revision,
        "taken_at": datetime.now().isoformat(timespec="seconds")})

    shot = sum(1 for e in entries if e["captured"])
    print(f"{args.lang}: {shot} screens captured -> {out}; manifest {manifest}")
    for f in shooter.failures:
        print(f"FAILED block {f}")
    if missing:
        print("not captured: " + ", ".join(missing))
    if outcome.get("error"):
        print(outcome["error"])
    return 1 if (missing or shooter.failures or outcome.get("error")) else 0


if __name__ == "__main__":
    sys.exit(main())
