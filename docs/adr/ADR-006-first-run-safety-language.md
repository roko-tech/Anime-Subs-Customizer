# ADR-006: First run, safety UX and language

- Status: accepted (2026-10-09)

## Context

The pre-launch review (87 verified findings, `.claude/launch/evidence/P1-review`) found the riskiest gaps were in the first minutes of use and in backup edge cases, not in the styling engine itself:

- **P0 ux-1 / P1 data-safety-1.** Backups are write-once per path. When a downloader (Bazarr, Sonarr) replaces a styled sidecar with a newer release, the newer original is never backed up. **Restore** then overwrites it with the older release, and the newer one is gone everywhere.
- **data-safety-2.** If the user deletes `Original Subs\` (the README even suggested it), either every later restyle is refused for good with an English error, or, once the file index is gone, *styled* content gets saved as the "original".
- **concurrency-3 / data-safety-3.** A 0-byte or half-written file can become the write-once backup.
- **ux-2 / ux-7 / arabic-rtl-3.** The first Watch silently rewrites the whole existing library. The first launch is always English, and the Arabic UI needs a menu trip and a restart.
- **ux-3.** Restore is undone by the next Watch, including the automatic one at the next login, and the UI says the opposite.
- **arabic-rtl-1 / ux-6.** Arabic message boxes scramble every paragraph after the first, their buttons follow the Windows language, and they are left-aligned.
- **ux-5 / windows-5 / data-safety-4.** A failed backup (Controlled Folder Access, read-only, locked file) is only logged. Watch shows "Watching" and nothing happens.

## Decision

### 1. Whose file is it? Writer parity

`FileProcessor.looks_styled_by_us(lines)` reuses the **same tag pass** that `compute_changes` uses to colour dialogue lines. A file counts as ours when it has at least one *taggable* dialogue line (a Dialogue row in a style we restyle, with a Text field), and running that pass with the colour found on the first such line changes nothing. One function serves both the writer and the check, so the two can't drift apart. The check doesn't depend on font, colour or size, so it recognises the app's output from any earlier session or settings. No marker line is added to files: that would change every file's bytes and mark existing libraries as unstyled.

A file with **no taggable dialogue line** carries no signal either way. Examples are a signs-and-songs-only track, an OP/ED file, or a file whose `[Events] Format` puts Text first. For such a file `compute_changes` returns "nothing to do": no backup, no write, and the file is recorded as processed. The user couldn't see a change anyway.

### 2. Backup rules (`apply_changes`)

The file's bytes are read once. The backup is written from **those exact bytes**, never re-read from disk, so a downloader replacing the file mid-operation can't swap what gets saved.

| Live file | Backup in `Original Subs\` | Action |
|---|---|---|
| no taggable dialogue | any | nothing to do (see above) |
| not ours | missing | back up |
| not ours | exists, same bytes | keep |
| not ours, with **positive evidence**: at least one taggable line lacks our colour block | exists, **different bytes** | **new original:** write the new backup to a temp file in `Original Subs\`, move the old backup to `Original Subs\.older\<name>.<YYYYmmdd-HHMMSS>-<n><ext>` (Restore and the scan ignore it), then rename the temp into place |
| anything ambiguous | exists | keep the existing backup |
| ours | exists | keep (never overwrite an original with styled content) |
| ours | **missing**, and the file index says the app styled this file before | restyle **without** a backup (there is no original left to protect) and record one localised note per folder: "Originals for this folder were deleted, so Restore can't bring them back." This replaces the DB-based permanent refusal. |
| ours | missing, no recorded history | treat it as an original and back it up. A release that happens to colour every line must never lose its original to a look-alike. |
| empty (0 bytes) | any | skip and record it. Never back up an empty file. |

**Before replacing the live file**, re-check its size and mtime. If either changed since it was read (a download still landing, another program saving), abort and retry on the next pass. If `Original Subs` is a junction or symbolic link (a reparse point), skip the file with a localised reason, so backups and `.older\` moves can't land outside the library.

`copy_original_file` refuses 0-byte sources. Extraction writes to a `.tmp_` file and `os.replace`s it when ffmpeg succeeds with non-empty output, so a partial target is never visible. A failed backup is recorded as a localised issue with the reason mapped below.

### 3. Restore that sticks

Restored files are recorded with a new status, `restored`. Watch leaves them alone until their **content** changes, and **Apply to library** restyles them on request. The Restore confirmation says so in plain words, including that saving the file in a subtitle editor counts as a change.

When a restored file's content changes, the order is:
1. It is already styled with the current settings → just record it.
2. It looks styled by us with other settings → `foreign` (ADR-007).
3. Otherwise it is a new original (the backup rule above), and it is restyled. The picker shows each series' path relative to the library, so `Show\Season 1` and `Show\Season 2` can be told apart.

### 4. First run and the confirmation

- **Language.** `ui_language` is empty until set. On first run it resolves in this order: the installer's choice (`HKCU\Software\Anime Subs Customizer\InstallerLanguage`), then the Windows display language (Arabic → `ar`), then `en`. Existing configs keep their stored value.
- **Welcome dialog** (first run with no folder). It says in three short points what the app does, that every original is kept in `Original Subs` beside the file, and that nothing changes until the user starts watching. It has a "Choose my anime folder…" button and the opt-in update checkbox (ADR-008).
- **Confirmation before the first restyle of a folder.** Turning Watch on for a folder that isn't in `consented_folders` cheaply counts the subtitle files the file index doesn't yet know as styled (a folder walk, no parsing, off the UI thread), then asks: "This will restyle the dialogue in **N** subtitle files in **<folder>**, now and whenever new ones arrive. Each original is kept in an "Original Subs" folder next to it, and Restore originals puts them back. Continue?" A drive root, the user profile or a Windows folder gets an extra warning. Consent is stored per folder, so autostart never asks again. Configs from earlier versions that already have a folder, font and colour count as consented, so no nag appears after an upgrade.
- **Apply to library** skips files that are already styled with the current settings and reports "N restyled · M already up to date · K left alone (font can't show them) · E problems".

### 5. Dialogs and right-to-left

- **One bilingual dialog** (`GUI.ask` / `GUI.inform`) replaces all ~30 `tkinter.messagebox` calls. It's a Studio-styled CTk dialog with buttons in the UI language, text aligned to the reading direction and the layout mirrored in Arabic. With the window hidden in the tray, it opens as a standalone window rather than a hidden transient. Native `MessageBoxW` with `MB_RTLREADING | MB_RIGHT` is used only by the crash handler when Tk itself is unusable.
- **Per-paragraph RTL base.** `_rtl_base` wraps each paragraph (each `\n`-separated line) separately, because a bidi embedding ends at a paragraph break. That break was the cause of the scrambled second paragraphs.
- **Bidi-safe values.** Paths, file names, counts and resolutions inside Arabic sentences are wrapped as left-to-right runs, so `D:\Videos\Anime` and `1920×1080` keep their order.
- **Digits:** Western digits (123) by default. The native-review gallery asks the owner to confirm.
- The native folder picker and the colour dialog follow the Windows language. This can't be changed and is documented.
- The Problems panel uses localised operation labels and friendly, localised reasons for common causes, with the technical detail in the log.

### 6. Friendly errors for common Windows failures

| Cause (Windows error) | What the user sees |
|---|---|
| Access denied on a file that isn't read-only (Controlled Folder Access, permissions) | "Windows blocked the change. If Ransomware protection (Controlled folder access) is on, allow Anime Subs Customizer in Windows Security, or move your library out of Documents/Videos." |
| Read-only attribute | "This file is read-only." |
| Sharing violation (32/33) | "Another program (often the video player) has this file open. It will be retried." |
| Network path gone (53/64/67) | "The library folder isn't reachable (unplugged drive or offline network share)." |
| Disk full (112) | "The disk is full." |

Watching a folder that doesn't exist shows "Folder not found" in the footer instead of "Watching".

### 7. Tray, crash handler, help

- **Tray hint.** After the first close-to-tray, the app shows one balloon: "Still running in the tray. Click the icon to open it (Windows may hide it under ^)."
- **Crash handler.** `sys.excepthook`, `threading.excepthook` and Tk's `report_callback_exception` write `crash-<timestamp>.txt` to the data folder and the traceback to the log. Then a bilingual dialog offers "Copy details" and "Open log folder". There is no telemetry.
- **⋯ menu → Help:** "User guide" opens `docs/guide.md` or `docs/guide.ar.md` on GitHub in the UI language. "Report a problem" opens the bug form with the app version and Windows version filled in. Logs are never attached automatically, and the form warns that logs contain paths. "About" shows the version and the FFmpeg credit.
- **A corrupt database** at startup is moved aside (`files_monitor.db.corrupt-<ts>`) and a fresh one is created, with one notice. The signature rule (1) means a lost index can't cause styled content to be backed up as an original.

## Consequences

- The data-integrity rules depend on the signature, not on the file index, so they survive uninstall, reinstall and Reset.
- Hand-edited styled files keep their colour blocks, so they count as ours and their original backup is kept.
- More UI strings: every new string is added in English and Arabic, and parity is enforced by test and by `--self-test`.

## Verification

- Regression tests for each row of the backup table: Bazarr replacement, deleted backups with the index present and cleared, empty files, read-only files, overlapping extraction. Also Restore → Watch keeping restored files, the language resolution order (mocked), the per-paragraph RTL wrap, and Apply's skip counts.
- Live matrix rows 1 (first run in both languages, with the confirmation), 6 (robustness), 7 (Controlled Folder Access message in the Sandbox) and 8 (data-integrity lifecycle).
