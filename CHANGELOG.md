# Changelog

All notable changes to Anime Subs Customizer are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project uses [Semantic Versioning](https://semver.org/).

## 1.2.0 - Unreleased

The first public release. Anime Subs Customizer now installs like any other
Windows app, and no Python is needed.

### Added

- **Windows installer.** A normal per-user installer, with no administrator
  prompt, in English or Arabic. It adds Start menu and optional desktop shortcuts,
  can start the app when you sign in, and can upgrade while the app is running.
  A portable ZIP is available too.
- **FFmpeg included** (optional in the installer), so subtitle extraction and
  the real-frame preview work out of the box.
- **Welcome screen** on first start: what the app does, where your originals go,
  and a button to pick your anime folder.
- **A confirmation before the first restyle of a folder.** It shows how many
  subtitle files will change and warns if you picked a whole drive or a system
  folder by mistake. It asks once per folder; starting with Windows never asks.
- **The app starts in your language.** It uses the language you chose in the
  installer, or else your Windows display language (Arabic or English).
- **Help in the ⋯ menu:** a user guide in English and Arabic, *Report a problem*
  (opens a form with the app and Windows versions filled in; logs are never
  attached automatically), and *About*.
- **Optional update check** (off by default): once a day the app can ask GitHub
  for the latest version number and show a link. Nothing else is sent.
- A one-time tray notice explaining that the app keeps running in the
  notification area.
- A crash report and an error window with *Copy details* and *Open log folder*,
  instead of failing silently. Nothing is sent anywhere.

### Changed

- **Your originals are safer.**
  - When a downloader (Bazarr, Sonarr, a re-download) replaces a subtitle with a
    newer release, that release becomes the saved original, and the older one is
    kept in `Original Subs\.older\`. **Restore** now brings back the newer
    release instead of an outdated one.
  - Styled content is never saved as an "original". If you deleted the
    `Original Subs` folders, files are restyled without a backup and you're told
    once, instead of being refused forever.
  - Empty or half-written files are never backed up or styled.
  - Subtitles are extracted to a temporary file first, so a failed or interrupted
    extraction never leaves an empty subtitle and never replaces an existing one.
  - Nothing is written if a file changes while it is being styled; it is tried
    again on the next pass.
- **Restored files stay original.** *Restore originals* now marks files so that
  Watch leaves them alone until a new version of the file arrives. *Apply to
  library* restyles them if you want.
- **No tug-of-war between two copies.** If another copy of the app (or another
  program) restyles a file with different settings, this copy leaves it alone
  instead of rewriting it back and forth.
- **Apply to library** skips files that are already styled and tells you how many
  were restyled, already up to date, or left as they are.
- **All messages are in your language**, right-to-left in Arabic, with buttons in
  Arabic too. The Issues panel explains common problems in plain words, for
  example when Windows' Ransomware protection blocks a change, a file is in use
  by your player, or a drive is unplugged.
- Settings, the file list and logs now live in
  `%LOCALAPPDATA%\Anime Subs Customizer`. The portable ZIP keeps them beside the app.
- Subtitles without a byte-order mark in Japanese, Chinese, Russian or Western
  encodings are recognised more reliably, and Arabic `cp1256` files keep being
  read correctly.

### Fixed

- Fonts installed under an Arabic (or any non-English) Windows user name, or with
  an Arabic file name, now work in the preview and the font checks.
- Subtitle extraction no longer silently skips videos on English and other
  non-Arabic Windows when a track title has non-English characters.
- Turning Watch off and on quickly can no longer leave two watchers running.
- A damaged file list no longer stops the app from starting; it is rebuilt.
- Leftover temporary files from a crash are no longer treated as subtitles.
- Read-only subtitles no longer leave undeletable temporary files behind, and
  every backup failure is shown under Issues.

## 1.1.0 - 2026-10-02 (private pre-release)

### Added

- **Arabic interface.** The whole UI ships in English and Arabic, with the layout
  fully mirrored right-to-left for Arabic. Switch from the `⋯` menu; the app
  restarts itself in the new language.
- **Dialogue size presets (— / S / M / L).** Optionally set dialogue to a fixed
  share of the frame height (5.5% / 7% / 9%), so subtitles look the same size
  across 360p, 1080p, and wide 2.35:1 releases. Off by default.
- **Size that matches across fonts.** Presets are scaled by each font's measured
  letter height (cap height for Latin, the shaped reading body for Arabic), so
  "Medium" looks the same size in every font.
- **Per-font size nudge.** A −50%…+50% stepper (5% steps), remembered per font, for
  fonts that still feel too big or too small.
- **Real-frame preview.** Show a random still from your own library behind the
  preview text, or switch to **Subtitle line** mode to preview a real line from
  a sidecar or embedded subtitle on the exact frame where it plays.
- **Favorite fonts.** Star fonts in the picker, filter to **Favorites**, and
  restrict the font field's arrow/wheel cycling to your starred fonts.
- **Accurate Arabic preview in every font.** The preview and font picker shape
  text with HarfBuzz and rasterize it with FreeType (the same pipeline the
  libass subtitle renderer uses), so OpenType-only Arabic fonts render
  correctly instead of as boxes.
- **Variable fonts.** Each named instance (e.g. *Thin*, *Black*) is resolved and
  previewed at its real weight.
- **Apply to one series.** *Apply to library* now asks whether to restyle the whole
  library or a single series folder.
- A multi-line preview-text editor with proper right-to-left caret handling, and a
  bilingual default preview text (an Arabic line and an English line).

### Changed

- **Signs and typesetting are protected.** Only dialogue styles are restyled.
  Styles used for signs, typesetting, karaoke, and song lyrics are left exactly
  as released, so on-screen typesetting still matches the video. These styles are
  detected from their tags (positioning, drawing, rotation, karaoke) or from
  common sign-style names, including Arabic ones.
- Restyled dialogue gets a solid black outline and shadow, replacing the grey
  edges some releases ship.
- `ScaledBorderAndShadow: yes` is now set so the scaled outline and shadow render
  at the intended thickness. An explicit `no` in the file is respected. Files
  with no `PlayResY` use the ASS default canvas (384×288) instead of 720p sizing.
- Files the chosen font can't render (for example, English subtitles with an
  Arabic-only font) are skipped and noted in the log instead of being restyled
  into boxes. A file's script is detected from its text, not from the filename's
  language tag.
- Font coverage is read directly from each font's character map, which fixes
  fonts such as Cairo, Tajawal, and Hacen being wrongly reported as unable to
  render Arabic or Latin text.
- Large subtitle files process much faster (line splitting is about 10× quicker),
  and the Issues panel opens instantly.
- Requires customtkinter 5.2.2 or newer (older releases don't run on Python
  3.12) and below 6, which hasn't been tested yet.

### Fixed

- Restyled lines in a CRLF file now keep CRLF. They used to come back with LF
  only, leaving the file with mixed line endings.
- A UTF-16 subtitle without a byte-order mark is now skipped and listed under
  Issues. Before, it was misread as UTF-8 and rewritten into a corrupted file
  (the original was still in `Original Subs/`).
- *Subtitle line* preview works on Python 3.13 and newer, where it never found
  a line.
- Extracting French, German, or Chinese subtitles now finds tracks tagged
  `fre`, `ger`, or `chi`, the codes MKV files usually use.
- A malformed colour value in `config.json` no longer resets your whole
  configuration (folder, favorites, nudges).
- A subtitle that is still being copied in (for example, over a network share)
  is no longer backed up and restyled while it's only partly written.
- Re-extracting subtitles no longer overwrites a sidecar that already has a
  backup, so hand edits are kept.
- Restoring originals is now atomic, so an interrupted restore can't leave a
  partly written file.
- One file that fails no longer stops the rest of a monitoring pass.
- The window no longer reopens off-screen after minimize/restore or a display
  change (such as a monitor or GPU reset).
- Fixed a hang in the font picker when scrolling while starring fonts.
- Fixed a crash on Shift+Enter in the preview-text editor.
- Fixed spurious "apply failed" warnings about font coverage.
- *Apply to library* could run twice.
- Turning on **Watch** with nothing to do (no font or colour, and extraction
  off) now explains why instead of silently doing nothing.
- Control rows no longer disappear after maximize → restore.
- Synthetic italic in the preview slanted the wrong way.
- Dialogs use the app icon and no longer jump into place when they open.

## 1.0.0 - 2026-07-03 (private pre-release)

First release.

- Watches a library folder and restyles every new or changed `.ass` subtitle to
  your font, colour, and bold/italic/underline, with outline and shadow scaled to
  each file's resolution.
- Live preview of the style over a 16:9 frame, and a font picker that renders each
  family in its own typeface, with a script-coverage filter and a live specimen.
- Byte-faithful edits: original encoding, line endings, embedded `[Fonts]`, and
  extradata are preserved.
- Automatic extraction of embedded subtitle tracks by language (FFmpeg).
- Per-series backups in `Original Subs/` with *Restore originals*, an Issues
  panel, and detection of already-styled files by content hash.
- System tray app with autostart, start-minimized, and a single-instance guard.
