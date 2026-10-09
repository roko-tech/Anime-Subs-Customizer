"""Tiny i18n layer: English (LTR) + Arabic (RTL) UI strings.

Tk/customtkinter renders Arabic labels correctly on its own (Windows' text
engine applies Unicode bidi + shaping), so strings are stored as plain logical
Arabic — NO reshaping/reordering (that would double-process and mangle them,
unlike the PIL preview which does need it). Callers just mirror the LAYOUT
when is_rtl().
"""
import unicodedata
from typing import Any, Callable, Dict, List, Optional

try:
    from bidi.algorithm import get_display as _get_display
except ImportError:  # pragma: no cover - python-bidi is a listed dependency
    _get_display = None

__all__ = ["t", "t_raw", "t_logical", "set_language", "get_language",
           "is_rtl", "LANGUAGES", "visual_rtl", "resolve_ui_language",
           "windows_ui_language_id", "installer_language", "friendly_reason",
           "operation_label", "rtl_base", "wrap_rtl"]


def _is_rtl_char(ch: str) -> bool:
    o = ord(ch)
    return (0x0590 <= o <= 0x05FF     # Hebrew
            or 0x0600 <= o <= 0x06FF  # Arabic
            or 0x0750 <= o <= 0x077F  # Arabic Supplement
            or 0x08A0 <= o <= 0x08FF  # Arabic Extended-A
            or 0xFB50 <= o <= 0xFDFF  # Presentation Forms-A
            or 0xFE70 <= o <= 0xFEFF)  # Presentation Forms-B


def _is_rtlish(ch: str) -> bool:
    # An RTL letter OR a combining mark (harakat), which the bidi pass keeps
    # attached to its base and reverses along with it.
    return _is_rtl_char(ch) or unicodedata.combining(ch) != 0


def visual_rtl(text: str) -> str:
    """Reorder `text` into VISUAL order for Tk.

    Tk shapes each Arabic word correctly but applies NO bidi — it lays runs
    out left-to-right in logical order, so multi-word Arabic (and mixed
    Arabic+LTR) renders with the words reversed. We run the real Unicode bidi
    algorithm (python-bidi) to get correct visual order — which also handles
    neutral punctuation (…, :, ·), numbers and embedded LTR filenames — then
    reverse each RTL run's characters BACK to logical order so the OS shaper
    still joins them (bidi reverses within-run for non-shaping renderers,
    which would otherwise mangle the joining).

    Pure-LTR text (no RTL char) is returned unchanged, so it's safe to route
    every UI string through this.
    """
    if not text or not any(_is_rtl_char(c) for c in text):
        return text
    if _get_display is None:
        return text  # no bidi lib — render unshaped-order rather than crash
    # base_dir='R': the whole UI is an RTL paragraph in Arabic mode.
    vis = _get_display(text, base_dir="R")
    # Restore logical char order within each maximal RTL(+combining) run.
    out: List[str] = []
    run: List[str] = []
    for ch in vis:
        if _is_rtlish(ch):
            run.append(ch)
        else:
            if run:
                out.append("".join(reversed(run)))
                run = []
            out.append(ch)
    if run:
        out.append("".join(reversed(run)))
    return "".join(out)

# (code, native display name) — for the language toggle.
LANGUAGES = [("en", "English"), ("ar", "العربية")]

_lang = "en"


def set_language(lang: Any) -> None:
    global _lang
    _lang = lang if isinstance(lang, str) and lang in _STRINGS else "en"


def resolve_ui_language(stored: Any, installer: Any = None,
                        windows_lang_id: int = 0) -> str:
    """The UI language: the user's stored choice; on first run the language
    picked in the installer; otherwise Arabic when Windows' display language is
    Arabic (primary language id 0x01), else English."""
    for choice in (stored, installer):
        if isinstance(choice, str) and choice in _STRINGS:
            return choice
    return "ar" if (int(windows_lang_id or 0) & 0x3FF) == 0x01 else "en"


def windows_ui_language_id() -> int:
    """Windows' display-language id for this user (0 when unavailable)."""
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
    except Exception:  # noqa: BLE001 — not Windows, or no kernel32
        return 0


def installer_language() -> Any:
    """The language chosen in the installer ("en"/"ar"), written to the user's
    registry by Setup; None when the app wasn't installed with it."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Anime Subs Customizer") as key:
            value, _ = winreg.QueryValueEx(key, "InstallerLanguage")
        return value
    except Exception:  # noqa: BLE001 — not installed, not Windows
        return None


def get_language() -> str:
    return _lang


def is_rtl() -> bool:
    return _lang == "ar"


# Engine reasons are English technical text (they also go to the log). The
# Problems panel shows a plain, translated explanation for the common causes;
# the technical text stays available underneath. Order matters: the app's own
# messages first, then Windows errors, then generic wrappers.
_REASON_PATTERNS = (
    (r"originals for this folder were deleted", "reason_originals_deleted"),
    (r"changed by another program", "reason_foreign"),
    (r"Original Subs folder is a link", "reason_backup_link"),
    (r"file index was damaged", "reason_db_rebuilt"),
    (r"the file is empty", "reason_empty"),
    (r"SubStation Alpha v4", "reason_ssa_v4"),
    (r"No supported encoding", "reason_undecodable"),
    (r"too large to be a subtitle", "reason_too_large"),
    (r"missing required field", "reason_bad_format"),
    (r"backup is empty|not a decodable subtitle", "reason_bad_backup"),
    (r"WinError 112\b|No space left|disk is full", "reason_disk_full"),
    (r"WinError (32|33)\b|being used by another process", "reason_locked"),
    (r"WinError (53|64|67|1231)\b|network (name|path)", "reason_unreachable"),
    (r"WinError 5\b|Access is denied|Errno 13|Permission denied", "reason_access_denied"),
    (r"WinError (2|3)\b|No such file|cannot find the (file|path)", "reason_missing"),
    (r"WinError 1155\b|No application is associated", "reason_no_app"),
    (r"timed out", "reason_timeout"),
    (r"could not back up the original", "reason_backup_failed"),
)


def friendly_reason(reason: str) -> Any:
    """A translated, plain explanation of an engine reason, or None when
    there's no better wording than the technical text."""
    import re
    for pattern, key in _REASON_PATTERNS:
        if re.search(pattern, reason or "", re.IGNORECASE):
            return t_raw(key)
    return None


def operation_label(operation: str) -> str:
    """Translated label for a Problems-panel operation ("apply" -> "Restyle")."""
    key = f"op_{operation}"
    table = _STRINGS.get(_lang, _STRINGS["en"])
    if key in table or key in _STRINGS["en"]:
        return t_raw(key)
    return operation.upper()


_RLE, _LRE, _PDF = "‫", "‪", "‬"  # RTL / LTR embedding … pop


def _rtl_base(s: str) -> str:
    """Wrap each Arabic paragraph in an RTL embedding so Tk and Windows render
    it with an RTL BASE direction. Without this they default to an LTR base and
    a trailing neutral (the "…" on "…text" buttons) resolves to the wrong end.
    An embedding ends at a paragraph break, so every "\n"-separated paragraph
    gets its own (one wrap around the whole text scrambled every paragraph
    after the first). Only paragraphs with RTL letters are wrapped, so pure-LTR
    values (paths, font names, filenames) pass through untouched."""
    if not is_rtl():
        return s
    return "\n".join(_RLE + p + _PDF if any(_is_rtl_char(c) for c in p) else p
                     for p in s.split("\n"))


def _ltr_value(value: Any) -> Any:
    """In the Arabic UI, embed a left-to-right value (a path, file name, font
    name, error text) as an LTR run, so "D:\\Videos\\Show (2024)" keeps its order
    inside an Arabic sentence. Uses LRE…PDF, not the newer isolate marks, which
    GDI on Windows 10 draws as visible boxes."""
    if (is_rtl() and isinstance(value, str) and value
            and not any(_is_rtl_char(c) for c in value)):
        return _LRE + value + _PDF
    return value


def rtl_base(s: str) -> str:
    """_rtl_base for text the GUI composes from several pieces (a chip label
    with a count, the footer's "· 5m ago"), so its neutrals land at the right
    end in the Arabic UI. A no-op in English."""
    return _rtl_base(s)


_EMBED_OPENERS = (_LRE, _RLE, "\u202d", "\u202e")  # LRE, RLE, LRO, RLO
_NO_MARKS = dict.fromkeys(map(ord, "\u200e\u200f\u202a\u202b\u202c\u202d\u202e"))


def wrap_rtl(text: str, measure: Callable[[str], float], width: float) -> str:
    """Break Arabic label text into lines that each keep an RTL base.

    Tk wraps a long label itself (wraplength) but draws every line after the
    first without the paragraph's RTL embedding, so wrapped Arabic lines came
    out left to right: the closing "." jumped to the line's start and mixed
    Arabic/Latin phrases swapped places. This breaks each paragraph greedily
    at spaces, with `measure(text)` (no bidi marks) <= `width`, and wraps every
    line that has Arabic in its own RLE…PDF (the paragraph's outer one from
    _rtl_base is dropped first). An LRE…PDF value from _ltr_value that crosses
    a break is closed at the line end and reopened on the next line. Give it a
    little less than the label's wraplength so Tk never breaks a line again.
    The English UI gets `text` back unchanged."""
    if not is_rtl() or not text:
        return text
    out: List[str] = []
    for para in text.split("\n"):
        if para.startswith(_RLE) and para.endswith(_PDF):
            para = para[1:-1]
        lines: List[str] = []
        current: Optional[str] = None
        for word in para.split(" "):
            candidate = word if current is None else current + " " + word
            if current is not None and measure(candidate.translate(_NO_MARKS)) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current or "")
        carried: List[str] = []
        for line in lines:
            still_open = list(carried)
            for ch in line:
                if ch in _EMBED_OPENERS:
                    still_open.append(ch)
                elif ch == _PDF and still_open:
                    still_open.pop()
            body = "".join(carried) + line + _PDF * len(still_open)
            carried = still_open
            out.append(_RLE + body + _PDF if any(_is_rtl_char(c) for c in line) else body)
    return "\n".join(out)


def t_logical(key: str, **fmt: Any) -> str:
    """Raw logical translation, NO RTL-base wrap — for composing pieces into a
    larger string the caller displays directly (e.g. the footer, which orders
    its parts by widget packing). Wrapping each piece would nest the RTL marks."""
    table = _STRINGS.get(_lang, _STRINGS["en"])
    s = table.get(key)
    if s is None:
        s = _STRINGS["en"].get(key, key)
    if not fmt:
        return s
    return s.format(**{k: _ltr_value(v) for k, v in fmt.items()})


def t_raw(key: str, **fmt: Any) -> str:
    """Translate `key` (logical order) and, in RTL, add an RTL base direction
    so trailing "…"/neutrals land at the visual end. Word ORDER is left to
    Tk/Windows' own bidi (works once an Arabic-capable font is pinned — see
    GUI.UI_FAMILY); no manual reordering."""
    return _rtl_base(t_logical(key, **fmt))


def t(key: str, **fmt: Any) -> str:
    """Translate `key`. Returns PLAIN logical text — no manual reordering.

    The UI pins Segoe UI on every widget (GUI.UI_FAMILY), and Tk/Windows then
    applies the Unicode bidi algorithm itself, so Arabic renders correctly
    from logical order. (customtkinter's DEFAULT font is Roboto, which lacks
    Arabic; its fallback path didn't bidi, which is what made plain text look
    reversed and led to the visual_rtl detour — no longer needed.) Retained as
    a distinct name from t_raw only for call-site intent; both are plain now.
    """
    return t_raw(key, **fmt)


_STRINGS: Dict[str, Dict[str, str]] = {
    "en": {
        # Header
        "watch_folder": "WATCH FOLDER",
        "change": "Change…",
        "not_selected": "Not selected",
        "watching": "Watching",
        "watching_count": "Watching · {n} {files} styled",
        "not_watching": "Not watching",
        "files_one": "file",
        "files_many": "files",
        # Preview well
        "live_preview": "LIVE PREVIEW",
        "video_frame": "( video frame — 1920×1080 )",
        "shuffle_bg": "Shuffle",
        "real_bg": "Real frame",
        "bg_mode_mytext": "My text",
        "bg_mode_subtitle": "Subtitle line",
        "no_sub_found": "( no subtitle found in your library )",
        "font_no_arabic": "This font doesn't support Arabic",
        "font_no_latin": "This font doesn't support English",
        "font_no_neither": "This font supports neither Arabic nor English",
        # Style row
        "font": "Font",
        "color": "Color",
        "choose_color_title": "Choose subtitle color",
        "size": "Size",
        "preview_text_btn": "Preview text…",
        # Actions
        "apply_to_library": "Apply to library",
        "apply_scope_prompt": "Restyle every existing subtitle in…",
        "apply_whole": "The whole library",
        "apply_subfolder": "A specific series…",
        "apply_subfolder_hint": "Pick one anime's folder to restyle only its subtitles.",
        "apply_pick_title": "Choose a series folder",
        "extract_subs": "Extract subs",
        "restore_originals": "Restore originals…",
        # Footer
        "idle": "Idle",
        "watching_for_files": "Watching for new files…",
        "no_issues": "No issues",
        "issues_one": "{n} issue",
        "issues_many": "{n} issues",
        "ev_styled": "Styled",
        "ev_extracted": "Extracted",
        "ev_restored": "Restored",
        "event_files_one": "{n} file",
        "event_files_many": "{n} files",
        "event_videos_one": "{n} video",
        "event_videos_many": "{n} videos",
        "ev_idle_last": "Idle — last {verb} ",
        "rel_just_now": "just now",
        "rel_min": "{n}m ago",
        "rel_hour": "{n}h ago",
        "rel_day": "{n}d ago",
        # Overflow / tray menus
        "tray_show": "Show",
        "tray_start_watching": "Start watching",
        "tray_stop_watching": "Stop watching",
        "tray_change_folder": "Change folder…",
        "tray_quit": "Quit",
        "extract_all_subs": "Extract all subs",
        "open_log": "Open log",
        "reset": "Reset…",
        "autostart": "Autostart at login",
        "start_minimized": "Start minimized",
        "language": "Language",
        "language_item": "Language: {name}",
        "restart_to_apply_title": "Language changed",
        "restart_to_apply": "Restart the app to apply the new language.",
        # Common messageboxes
        "missing_tools_title": "Missing Tools",
        "ffmpeg_not_found": "ffmpeg/ffprobe not found.",
        # Common buttons / titles
        "cancel": "Cancel",
        "ok": "OK",
        "welcome_title": "Welcome to Anime Subs Customizer",
        "welcome_body": "Pick your anime folder, then a font and a colour. When you turn on Watch, the dialogue in your subtitles is restyled, and new subtitles are styled as they arrive.\n\n• Signs, songs and karaoke keep their own look.\n• Every original is kept in an \"Original Subs\" folder next to it, and Restore originals… puts it back.\n• Nothing changes until you turn on Watch and confirm.",
        "welcome_choose": "Choose my anime folder…",
        "welcome_later": "Later",
        "update_opt_in": "Check once a day for a newer version (asks GitHub for the version number; nothing else is sent)",
        "menu_updates": "Check for updates once a day",
        "menu_guide": "User guide",
        "menu_report": "Report a problem…",
        "menu_about": "About",
        "about_body": "Anime Subs Customizer {version}\n\nFree and open source (MIT licence). It uses FFmpeg, included under the LGPL, for subtitle extraction and preview frames.\n\nHelp, source code and new versions: github.com/roko-tech/Anime-Subs-Customizer",
        "about_website": "Open the website",
        "update_available_title": "New version",
        "update_available_body": "Version {version} is the recommended version (you have {current}).",
        "update_available_tray": "Version {version} is available. Open the app and choose ⋯ > About to get it.",
        "update_open": "Open the download page",
        "tray_hint": "Still running and watching your folder. Click the icon in the notification area to open it (Windows may hide it under ^).",
        "op_apply": "Restyle",
        "op_extract": "Extract",
        "op_decode": "Read",
        "op_read": "Read",
        "op_restore": "Restore",
        "op_backup": "Backup",
        "op_database": "File list",
        "op_foreign": "Changed elsewhere",
        "op_font": "Font",
        "issue_details": "Details: {detail}",
        "reason_originals_deleted": "The originals for this folder were deleted, so Restore can't bring them back. Files here are restyled without a backup.",
        "reason_foreign": "Another program changed this file after it was styled, so it was left as it is. Apply to library restyles it.",
        "reason_backup_link": "The Original Subs folder here is a link to another place, so nothing was changed.",
        "reason_db_rebuilt": "The app's list of files was damaged and has been rebuilt. Your subtitles and their backups are not affected.",
        "reason_empty": "The file is empty, so it was left as it is.",
        "reason_ssa_v4": "It uses the old SubStation Alpha v4 format, which the app doesn't change, so it was left untouched.",
        "reason_undecodable": "Its text encoding couldn't be recognised, so it was left untouched.",
        "reason_too_large": "It's too large to be a subtitle file, so it was skipped.",
        "reason_bad_format": "Its style table is damaged, so it was left untouched.",
        "reason_bad_backup": "The saved original is empty or damaged, so it wasn't restored.",
        "reason_disk_full": "The disk is full.",
        "reason_locked": "Another program (often the video player) has this file open. It will be tried again later.",
        "reason_unreachable": "The folder isn't reachable (an unplugged drive or an offline network share).",
        "reason_access_denied": "Windows blocked the change. The file may be read-only, or Ransomware protection (Controlled folder access) may be on: allow Anime Subs Customizer in Windows Security, or keep your library outside Documents and Videos.",
        "reason_missing": "The file disappeared before it could be processed.",
        "reason_timeout": "FFmpeg took too long and was stopped. It will be tried again later.",
        "reason_backup_failed": "The original couldn't be saved, so the file was left unstyled.",
        "reason_no_app": "No app is set to open this kind of file. Open it with Notepad instead.",
        "first_watch_title": "Start watching this folder?",
        "first_watch_body": "The dialogue in {n} subtitle file(s) in this folder will be restyled now, and in new subtitles as they arrive:\n{folder}\n\nSigns, songs and karaoke keep their own look. Each original file is kept in an \"Original Subs\" folder next to it, and Restore originals… puts them back at any time.",
        "first_watch_none": "There are no subtitle files in this folder yet. New ones will be restyled as they arrive, and each original is kept in an \"Original Subs\" folder next to it:\n{folder}",
        "first_watch_risky": "Warning: this looks like a whole drive, your user folder or a system folder, not an anime folder. Every subtitle file anywhere inside it would be changed.",
        "first_watch_start": "Start watching",
        "crash_title": "Something went wrong",
        "crash_body": "Anime Subs Customizer ran into an unexpected problem.\n\nSubtitle files are always saved in one safe step, so this error didn't damage any of them. A report was saved in the app's log folder; you can copy it into a bug report.",
        "crash_start_body": "Anime Subs Customizer couldn't start because of an unexpected problem. A report was saved in its log folder ({path}).",
        "crash_copy": "Copy details",
        "crash_open_folder": "Open log folder",
        "yes": "Yes",
        "no": "No",
        "keep_in_tray": "Keep running in the tray",
        "quit_app": "Quit",
        "still_stopping": "Still finishing the last scan. Try again in a few seconds.",
        "save": "Save",
        "close": "Close",
        "clear": "Clear",
        "done": "Done",
        "select_all": "Select all",
        "clear_all": "Clear all",
        "warn_title": "Warning",
        "busy_title": "Busy",
        "confirm_title": "Confirm",
        "done_title": "Done",
        "error_title": "Error",
        "exit_title": "Exit",
        "select_folder_first": "Select a folder first.",
        "nothing_to_watch_for": "Pick a font and colour — or turn on subtitle extraction — before watching, or nothing will be changed.",
        "busy_body": "An operation is already running. Please wait for it to finish.",
        # Close / folder
        "minimize_to_tray_q": "Minimize to tray?",
        "switch_folder_title": "Switch folder?",
        "switch_folder_body": "Selecting a new folder will stop monitoring the current one. Continue?",
        # Apply to library
        "stop_before_apply": "Stop monitoring before applying to existing files — otherwise the monitor will process the same files concurrently.",
        "select_font_color": "Select both font and color.",
        "modify_all_ass_q": "Modify all existing .ass files?",
        "processed_n": "Processed {done}/{total} files.",
        "apply_summary": "Restyled: {styled}\nAlready up to date: {current}\nLeft as they are: {left} (for example, the font can't show their language, or they have no dialogue)",
        "apply_summary_problems": "{n} file(s) couldn't be changed. See Issues at the bottom of the window for why.",
        # Extract all
        "stop_before_extract_all": "Stop monitoring before extracting all — otherwise the monitor will extract the same files concurrently.",
        "reextract_confirm": "Re-extract subtitles from every video in this folder? This processes files even if they've been extracted before.",
        "extracted_n": "Extracted subtitles from {done}/{total} files.",
        # Log
        "log_not_found": "Log file not found.",
        "cant_open_log": "Could not open the log file:\n{path}\n\n{err}",
        # Restore
        "stop_before_restore": "Turn off Watch before restoring originals.",
        "restore_title": "Restore originals",
        "no_backups_title": "Restore Originals",
        "no_backups_body": "No 'Original Subs' backups found under:\n{path}",
        "confirm_restore_title": "Confirm Restore",
        "overwrite_styled_q": "Put back the original of {n} subtitle file(s) in:\n{name}\n\nThe styled files are replaced by their saved \"Original Subs\" copies. Restored files stay original: Watch leaves them alone until a new version of the file arrives, and Apply to library restyles them.",
        "restored_n": "Restored {n} file(s).",
        "restored_failed": "\n{n} failed — see log.",
        "restore_intro": "Backups found for {n} series. Restoring rewrites each file back to its Original Subs/ copy.",
        "restore_files": "{n} files",
        "restore_files_one": "{n} file",
        "restore_btn": "Restore {series} series · {files} files",
        "restore_btn_one": "Restore {series} series · {files} file",
        "restore_btn_idle": "Restore",
        # Reset
        "stop_before_reset": "Stop monitoring before resetting.",
        "reset_title": "Reset",
        "reset_intro": "Choose what to clear. Original Subs backups are never touched.",
        "reset_config": "Configuration",
        "reset_config_desc": "Font, color, folder and options return to defaults.",
        "reset_subs": "Subtitle index",
        "reset_subs_desc": "Existing .ass files are re-scanned on next run.",
        "reset_video": "Video index",
        "reset_video_desc": "Next run re-extracts every video — slow on big libraries.",
        "reset_selected_btn": "Reset selected",
        "confirm_reset_title": "Confirm reset",
        "reset_recap_head": "This will permanently:\n\n",
        "reset_recap_tail": "\n\nYour 'Original Subs' backups are NOT affected. This cannot be undone.\n\nContinue?",
        "reset_item_config": "• Configuration (font, color, folder)",
        "reset_item_subs": "• Subtitle database (re-indexes existing .ass)",
        "reset_item_videos": "• Video index — the next run RE-EXTRACTS every video",
        "reset_errors_title": "Reset Errors",
        "reset_err_config": "Couldn't delete the settings file: {err}",
        "reset_err_subs": "Couldn't clear the subtitle index: {err}",
        "reset_err_videos": "Couldn't clear the video index: {err}",
        "reset_done": "Selected items have been reset.",
        # Progress dialog titles
        "prog_apply": "Applying style to library",
        "prog_extract": "Extracting subtitles",
        "prog_restore": "Restoring originals",
        "prog_scanning": "Scanning folder…",
        "prog_count": "{current} of {total}",
        # Preview text dialog
        "preview_text_title": "Preview text",
        "preview_text_helper": "Arabic or English — the field follows your text direction.",
        # Issues dialog
        "issues_title": "Issues",
        "issues_intro": "{n} recent — newest first. Files with issues are left untouched.",
        "issues_none": "No issues recorded.",
        # Extract languages dialog
        "langs_title": "Extract languages",
        "langs_intro": "Embedded subtitle tracks in these languages get pulled out of new videos.",
        "langs_more": "+ {n} more",
        "ffmpeg_disabled": "ffmpeg not found — extraction disabled",
        "no_langs_selected": "none",
        "language_chip": "{code} · {name}{check}",
        "language_eng": "English",
        "language_ara": "Arabic",
        "language_spa": "Spanish",
        "language_fra": "French",
        "language_deu": "German",
        "language_ita": "Italian",
        "language_jpn": "Japanese",
        "language_kor": "Korean",
        "language_zho": "Chinese",
        "language_rus": "Russian",
        "language_tur": "Turkish",
        "language_por": "Portuguese",
        "language_hin": "Hindi",
        "language_urd": "Urdu",
        "language_tha": "Thai",
        "language_vie": "Vietnamese",
        # Font picker
        "choose_font_title": "Choose font",
        "search_fonts_ph": "Search {n} fonts…",
        "use_this_font": "Use this font",
        "no_matching_fonts": "No matching fonts",
        "spec_latin": "Latin",
        "spec_glyphs": "{n:,} glyphs",
        "favorites": "Favorites",
        "capable_chip": "{script}-capable",
        "capable_scanning": "{script}-capable · scanning…",
        "scan_indexing": "Indexing fonts… {pct}%",
        "scan_scanning": "Scanning… {pct}%",
        "script_hebrew": "Hebrew",
        "script_arabic": "Arabic",
        "script_thai": "Thai",
        "script_kana": "Japanese kana",
        "script_cjk": "CJK",
        "script_hangul": "Hangul",
        "script_generic": "Script",
    },
    "ar": {
        # Header
        "watch_folder": "مجلد المراقبة",
        "change": "تغيير…",
        "not_selected": "لم يُحدَّد",
        "watching": "المراقبة جارية",
        "watching_count": "المراقبة جارية · {files}: {n}",
        "not_watching": "المراقبة متوقفة",
        "files_one": "ملف منسّق",
        "files_many": "ملفات منسّقة",
        # Preview well
        "live_preview": "معاينة حية",
        "video_frame": "( إطار فيديو — 1920×1080 )",
        "shuffle_bg": "بدّل الخلفية",
        "real_bg": "إطار من الفيديو",
        "bg_mode_mytext": "نصّي",
        "bg_mode_subtitle": "سطر الترجمة",
        "no_sub_found": "( لا توجد ترجمة في مكتبتك )",
        "font_no_arabic": "هذا الخط لا يدعم العربية",
        "font_no_latin": "هذا الخط لا يدعم الإنجليزية",
        "font_no_neither": "هذا الخط لا يدعم العربية ولا الإنجليزية",
        # Style row
        "font": "الخط",
        "color": "اللون",
        "choose_color_title": "اختيار لون الترجمة",
        "size": "الحجم",
        "preview_text_btn": "نص المعاينة…",
        # Actions
        "apply_to_library": "تطبيق على المكتبة",
        "apply_scope_prompt": "أعِد تنسيق جميع ملفات الترجمة الموجودة في…",
        "apply_whole": "المكتبة بالكامل",
        "apply_subfolder": "مسلسل محدّد…",
        "apply_subfolder_hint": "اختر مجلد أنمي واحد لإعادة تنسيق ترجماته فقط.",
        "apply_pick_title": "اختر مجلد المسلسل",
        "extract_subs": "استخراج الترجمات",
        "restore_originals": "استعادة النسخ الأصلية…",
        # Footer
        "idle": "خامل",
        "watching_for_files": "في انتظار ملفات جديدة…",
        "no_issues": "لا توجد مشاكل",
        "issues_one": "مشكلة: {n}",
        "issues_many": "مشاكل: {n}",
        "ev_styled": "تم تنسيق",
        "ev_extracted": "تم الاستخراج من",
        "ev_restored": "تمت استعادة",
        "event_files_one": "{n} ملف",
        "event_files_many": "{n} من الملفات",
        "event_videos_one": "{n} فيديو",
        "event_videos_many": "{n} من الفيديوهات",
        "ev_idle_last": "خامل — آخر إجراء: {verb} ",
        "rel_just_now": "الآن",
        "rel_min": "قبل {n} د",
        "rel_hour": "قبل {n} س",
        "rel_day": "قبل {n} ي",
        # Overflow / tray menus
        "tray_show": "إظهار النافذة",
        "tray_start_watching": "بدء المراقبة",
        "tray_stop_watching": "إيقاف المراقبة",
        "tray_change_folder": "تغيير المجلد…",
        "tray_quit": "إنهاء التطبيق",
        "extract_all_subs": "استخراج كل الترجمات",
        "open_log": "فتح السجل",
        "reset": "إعادة تعيين…",
        "autostart": "التشغيل تلقائيًا عند تسجيل الدخول",
        "start_minimized": "البدء مُصغَّرًا",
        "language": "اللغة",
        "language_item": "اللغة: {name}",
        "restart_to_apply_title": "تم تغيير اللغة",
        "restart_to_apply": "أعد تشغيل التطبيق لتطبيق اللغة الجديدة.",
        # Common messageboxes
        "missing_tools_title": "أدوات مفقودة",
        "ffmpeg_not_found": "لم يُعثر على ffmpeg/ffprobe.",
        # Common buttons / titles
        "cancel": "إلغاء",
        "ok": "حسنًا",
        "welcome_title": "مرحبًا بك في Anime Subs Customizer",
        "welcome_body": "اختر مجلد الأنمي لديك، ثم خطًّا ولونًا. عند تشغيل المراقبة يُعاد تنسيق الحوار في ملفات الترجمة، وتُنسَّق الترجمات الجديدة فور وصولها.\n\n• تحتفظ اللافتات والأغاني والكاريوكي بمظهرها الأصلي.\n• يُحفظ كل ملف أصلي في مجلد «Original Subs» بجانبه، ويمكنك إرجاعه من «استعادة النسخ الأصلية…».\n• لا يتغيّر أي شيء قبل أن تشغّل المراقبة وتؤكّد.",
        "welcome_choose": "اختر مجلد الأنمي…",
        "welcome_later": "لاحقًا",
        "update_opt_in": "تحقّق مرة يوميًا من وجود إصدار أحدث (يسأل GitHub عن رقم الإصدار فقط، ولا يُرسل أي شيء آخر)",
        "menu_updates": "التحقق من التحديثات مرة يوميًا",
        "menu_guide": "دليل الاستخدام",
        "menu_report": "الإبلاغ عن مشكلة…",
        "menu_about": "حول التطبيق",
        "about_body": "Anime Subs Customizer {version}\n\nبرنامج مجاني ومفتوح المصدر (رخصة MIT). يستخدم FFmpeg، المرفق برخصة LGPL، لاستخراج الترجمات ولقطات المعاينة.\n\nالمساعدة والشيفرة المصدرية والإصدارات الجديدة: github.com/roko-tech/Anime-Subs-Customizer",
        "about_website": "فتح الموقع",
        "update_available_title": "إصدار جديد",
        "update_available_body": "الإصدار {version} هو الإصدار الموصى به (لديك {current}).",
        "update_available_tray": "الإصدار {version} متاح. افتح التطبيق واختر ⋯ ثم «حول التطبيق» للحصول عليه.",
        "update_open": "فتح صفحة التنزيل",
        "tray_hint": "ما زال التطبيق يعمل ويراقب مجلدك. انقر أيقونته في منطقة الإعلام لفتحه (قد يخفيها Windows تحت السهم ^).",
        "op_apply": "تنسيق",
        "op_extract": "استخراج",
        "op_decode": "قراءة",
        "op_read": "قراءة",
        "op_restore": "استعادة",
        "op_backup": "نسخ احتياطي",
        "op_database": "قائمة الملفات",
        "op_foreign": "تغيّر خارجيًا",
        "op_font": "الخط",
        "issue_details": "التفاصيل: {detail}",
        "reason_originals_deleted": "حُذفت النسخ الأصلية لهذا المجلد، لذلك لا يمكن استعادتها. يُعاد تنسيق الملفات هنا دون نسخة احتياطية.",
        "reason_foreign": "غيّر برنامج آخر هذا الملف بعد تنسيقه، فتُرك كما هو. يمكنك إعادة تنسيقه من «تطبيق على المكتبة».",
        "reason_backup_link": "مجلد Original Subs هنا اختصار إلى مكان آخر، لذلك لم يُغيَّر شيء.",
        "reason_db_rebuilt": "تلفت قائمة الملفات الخاصة بالتطبيق وأُعيد بناؤها. لم تتأثر ترجماتك ولا نسخها الاحتياطية.",
        "reason_empty": "الملف فارغ، فتُرك كما هو.",
        "reason_ssa_v4": "يستخدم تنسيق SubStation Alpha v4 القديم الذي لا يغيّره التطبيق، فتُرك دون تغيير.",
        "reason_undecodable": "تعذّر التعرّف على ترميز نصه، فتُرك دون تغيير.",
        "reason_too_large": "حجمه أكبر من أن يكون ملف ترجمة، فتم تخطيه.",
        "reason_bad_format": "جدول الأنماط فيه تالف، فتُرك دون تغيير.",
        "reason_bad_backup": "النسخة الأصلية المحفوظة فارغة أو تالفة، فلم تُستعد.",
        "reason_disk_full": "القرص ممتلئ.",
        "reason_locked": "يستخدم برنامج آخر هذا الملف حاليًا (غالبًا مشغّل الفيديو). ستُعاد المحاولة لاحقًا.",
        "reason_unreachable": "تعذّر الوصول إلى المجلد (قرص مفصول أو مشاركة شبكة غير متصلة).",
        "reason_access_denied": "منع Windows هذا التغيير. ربما الملف للقراءة فقط، أو أن «الحماية من برامج الفدية» (الوصول المتحكَّم فيه إلى المجلدات) مفعّلة: اسمح للتطبيق من «أمن Windows»، أو احفظ مكتبتك خارج مجلدَي المستندات والفيديو.",
        "reason_missing": "اختفى الملف قبل أن تتم معالجته.",
        "reason_timeout": "استغرق FFmpeg وقتًا طويلًا فأُوقف. ستُعاد المحاولة لاحقًا.",
        "reason_backup_failed": "تعذّر حفظ النسخة الأصلية، فتُرك الملف دون تنسيق.",
        "reason_no_app": "لا يوجد تطبيق مُعيَّن لفتح هذا النوع من الملفات. افتحه بالمفكرة (Notepad) بدلًا من ذلك.",
        "first_watch_title": "هل تبدأ مراقبة هذا المجلد؟",
        "first_watch_body": "سيُعاد تنسيق الحوار في ملفات الترجمة الموجودة في هذا المجلد الآن (عددها {n})، وفي الترجمات الجديدة عند وصولها:\n{folder}\n\nتحتفظ اللافتات والأغاني والكاريوكي بمظهرها الأصلي. ويُحفظ كل ملف أصلي في مجلد «Original Subs» بجانبه، ويمكنك إرجاعه في أي وقت من «استعادة النسخ الأصلية…».",
        "first_watch_none": "لا توجد ملفات ترجمة في هذا المجلد بعد. سيُعاد تنسيق الملفات الجديدة عند وصولها، ويُحفظ كل ملف أصلي في مجلد «Original Subs» بجانبه:\n{folder}",
        "first_watch_risky": "تنبيه: يبدو أن هذا قرص كامل أو مجلد المستخدم أو مجلد للنظام، وليس مجلد أنمي. سيتغيّر كل ملف ترجمة في أي مكان داخله.",
        "first_watch_start": "ابدأ المراقبة",
        "crash_title": "حدث خطأ غير متوقع",
        "crash_body": "واجه التطبيق مشكلة غير متوقعة.\n\nيحفظ التطبيق ملفات الترجمة دائمًا في خطوة واحدة آمنة، لذلك لم يُتلف هذا الخطأ أيًّا منها. حُفظ تقرير في مجلد سجل التطبيق، ويمكنك نسخه وإرفاقه ببلاغ عن المشكلة.",
        "crash_start_body": "تعذّر تشغيل التطبيق بسبب مشكلة غير متوقعة. حُفظ تقرير في مجلد السجل ({path}).",
        "crash_copy": "نسخ التفاصيل",
        "crash_open_folder": "فتح مجلد السجل",
        "yes": "نعم",
        "no": "لا",
        "keep_in_tray": "واصل العمل في منطقة الإعلام",
        "quit_app": "إنهاء",
        "still_stopping": "ما زال التطبيق ينهي الفحص السابق. حاول مرة أخرى بعد بضع ثوانٍ.",
        "save": "حفظ",
        "close": "إغلاق",
        "clear": "مسح",
        "done": "تم",
        "select_all": "تحديد الكل",
        "clear_all": "إلغاء تحديد الكل",
        "warn_title": "تحذير",
        "busy_title": "مشغول",
        "confirm_title": "تأكيد",
        "done_title": "تم",
        "error_title": "خطأ",
        "exit_title": "خروج",
        "select_folder_first": "اختر مجلدًا أولًا.",
        "nothing_to_watch_for": "اختر خطًا ولونًا — أو فعّل استخراج الترجمات — قبل بدء المراقبة، وإلا فلن يتغيّر أي ملف.",
        "busy_body": "توجد عملية قيد التشغيل بالفعل. يُرجى الانتظار حتى تنتهي.",
        # Close / folder
        "minimize_to_tray_q": "هل تريد التصغير إلى منطقة الإعلام؟",
        "switch_folder_title": "تغيير المجلد؟",
        "switch_folder_body": "اختيار مجلد جديد سيوقف مراقبة المجلد الحالي. هل تريد المتابعة؟",
        # Apply to library
        "stop_before_apply": "أوقف المراقبة قبل تطبيق التنسيق على الملفات الحالية، وإلا فسيعالج التطبيق الملفات نفسها مرتين بالتزامن.",
        "select_font_color": "اختر الخط واللون معًا.",
        "modify_all_ass_q": "هل تريد تعديل جميع ملفات ‎.ass الحالية؟",
        "processed_n": "الملفات التي تمت معالجتها: {done}/{total}.",
        "apply_summary": "أُعيد تنسيقها: {styled}\nمنسّقة مسبقًا بالإعدادات الحالية: {current}\nتُركت كما هي: {left} (مثلًا لأن الخط لا يدعم لغتها، أو لأنها بلا حوار)",
        "apply_summary_problems": "تعذّر تغيير بعض الملفات (عددها {n}). ستجد السبب في «المشاكل» أسفل النافذة.",
        # Extract all
        "stop_before_extract_all": "أوقف المراقبة قبل استخراج جميع الترجمات، وإلا فسيستخرج التطبيق الترجمات من الملفات نفسها مرتين بالتزامن.",
        "reextract_confirm": "هل تريد إعادة استخراج الترجمات من كل فيديو في هذا المجلد؟ ستشمل العملية جميع الفيديوهات، حتى التي سبق استخراج ترجماتها.",
        "extracted_n": "الفيديوهات التي استُخرجت ترجماتها: {done}/{total}.",
        # Log
        "log_not_found": "لم يُعثر على ملف السجل.",
        "cant_open_log": "تعذّر فتح ملف السجل:\n{path}\n\n{err}",
        # Restore
        "stop_before_restore": "أوقف المراقبة قبل استعادة النسخ الأصلية.",
        "restore_title": "استعادة النسخ الأصلية",
        "no_backups_title": "استعادة النسخ الأصلية",
        "no_backups_body": "لم يُعثر على نسخ احتياطية في «Original Subs» ضمن:\n{path}",
        "confirm_restore_title": "تأكيد الاستعادة",
        "overwrite_styled_q": "هل تريد إرجاع النسخة الأصلية لملفات الترجمة في هذا المجلد (عددها {n})؟\n{name}\n\nستُستبدل الملفات المنسّقة بنسخها المحفوظة في «Original Subs». تبقى الملفات المستعادة على حالها الأصلية: لا تغيّرها المراقبة حتى تصل نسخة جديدة من الملف، ويمكنك إعادة تنسيقها من «تطبيق على المكتبة».",
        "restored_n": "تمت استعادة {n} من الملفات.",
        "restored_failed": "\nتعذّرت استعادة {n} من الملفات — راجع السجل.",
        "restore_intro": "عُثر على نسخ احتياطية لـ {n} من المسلسلات. عند الاستعادة، يُستبدل كل ملف بنسخته المحفوظة في «Original Subs».",
        "restore_files": "الملفات: {n}",
        "restore_files_one": "الملفات: {n}",
        "restore_btn": "استعادة · المسلسلات: {series} · الملفات: {files}",
        "restore_btn_one": "استعادة · المسلسلات: {series} · الملفات: {files}",
        "restore_btn_idle": "استعادة",
        # Reset
        "stop_before_reset": "أوقف المراقبة قبل إعادة التعيين.",
        "reset_title": "إعادة تعيين",
        "reset_intro": "اختر ما تريد مسحه. لا يتم المساس بنسخ Original Subs الاحتياطية أبدًا.",
        "reset_config": "الإعدادات",
        "reset_config_desc": "تعود إعدادات الخط واللون والمجلد والخيارات إلى قيمها الافتراضية.",
        "reset_subs": "فهرس الترجمات",
        "reset_subs_desc": "تُفحَص ملفات ‎.ass الحالية من جديد عند التشغيل التالي.",
        "reset_video": "فهرس الفيديو",
        "reset_video_desc": "عند التشغيل التالي، يُعاد استخراج الترجمات من جميع الفيديوهات — وقد يستغرق ذلك وقتًا طويلًا في المكتبات الكبيرة.",
        "reset_selected_btn": "إعادة تعيين العناصر المحددة",
        "confirm_reset_title": "تأكيد إعادة التعيين",
        "reset_recap_head": "سيؤدي هذا بشكل دائم إلى:\n\n",
        "reset_recap_tail": "\n\nلن تتأثر نسخ «Original Subs» الاحتياطية. لا يمكن التراجع عن هذا.\n\nهل تريد المتابعة؟",
        "reset_item_config": "• الإعدادات (الخط، اللون، المجلد)",
        "reset_item_subs": "• قاعدة بيانات الترجمات (إعادة فهرسة ملفات ‎.ass الحالية)",
        "reset_item_videos": "• فهرس الفيديو — يُعاد استخراج الترجمات من كل فيديو عند التشغيل التالي",
        "reset_errors_title": "أخطاء إعادة التعيين",
        "reset_err_config": "تعذّر حذف ملف الإعدادات: {err}",
        "reset_err_subs": "تعذّر مسح فهرس الترجمات: {err}",
        "reset_err_videos": "تعذّر مسح فهرس الفيديو: {err}",
        "reset_done": "تمت إعادة تعيين العناصر المحددة.",
        # Progress dialog titles
        "prog_apply": "تطبيق التنسيق على المكتبة",
        "prog_extract": "استخراج الترجمات",
        "prog_restore": "استعادة النسخ الأصلية",
        "prog_scanning": "جارٍ فحص المجلد…",
        "prog_count": "{current} من {total}",
        # Preview text dialog
        "preview_text_title": "نص المعاينة",
        "preview_text_helper": "بالعربية أو الإنجليزية — يتبع الحقل اتجاه النص الذي تكتبه.",
        # Issues dialog
        "issues_title": "المشاكل",
        "issues_intro": "المشاكل الأخيرة ({n}) — الأحدث أولًا. تُترك الملفات التي تحتوي على مشاكل دون تعديل.",
        "issues_none": "لا توجد مشاكل مُسجَّلة.",
        # Extract languages dialog
        "langs_title": "لغات الاستخراج",
        "langs_intro": "تُستخرَج مسارات الترجمة المضمّنة بهذه اللغات من الفيديوهات الجديدة.",
        "langs_more": "+ {n} أخرى",
        "ffmpeg_disabled": "لم يُعثر على ffmpeg — الاستخراج معطّل",
        "no_langs_selected": "لا شيء",
        "language_chip": "{code} · {name}{check}",
        "language_eng": "الإنجليزية",
        "language_ara": "العربية",
        "language_spa": "الإسبانية",
        "language_fra": "الفرنسية",
        "language_deu": "الألمانية",
        "language_ita": "الإيطالية",
        "language_jpn": "اليابانية",
        "language_kor": "الكورية",
        "language_zho": "الصينية",
        "language_rus": "الروسية",
        "language_tur": "التركية",
        "language_por": "البرتغالية",
        "language_hin": "الهندية",
        "language_urd": "الأردية",
        "language_tha": "التايلاندية",
        "language_vie": "الفيتنامية",
        # Font picker
        "choose_font_title": "اختر الخط",
        "search_fonts_ph": "ابحث في الخطوط ({n})…",
        "use_this_font": "استخدم هذا الخط",
        "no_matching_fonts": "لا خطوط مطابقة",
        "spec_latin": "اللاتينية",
        "spec_glyphs": "المحارف: {n:,}",
        "favorites": "المفضّلة",
        "capable_chip": "يدعم {script}",
        "capable_scanning": "يدعم {script} · جارٍ الفحص…",
        "scan_indexing": "فهرسة الخطوط… {pct}%",
        "scan_scanning": "جارٍ الفحص… {pct}%",
        "script_hebrew": "العبرية",
        "script_arabic": "العربية",
        "script_thai": "التايلاندية",
        "script_kana": "كانا اليابانية",
        "script_cjk": "الصينية واليابانية والكورية",
        "script_hangul": "الهانغول",
        "script_generic": "نظام الكتابة",
    },
}
