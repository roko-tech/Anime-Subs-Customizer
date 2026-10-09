# Development

Users install the app with the Windows installer (see the [README](../README.md)). This page is for running it from source, testing and building.

## Run from source

Requirements: Windows 10 or 11, Python 3.10 to 3.14 from python.org (it includes tkinter). FFmpeg is optional.

```powershell
git clone https://github.com/roko-tech/Anime-Subs-Customizer.git
cd Anime-Subs-Customizer
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt -r requirements-dev.txt
.venv\Scripts\pythonw anime_subs_customizer.pyw
```

A source run keeps its settings beside the code (`config.json`, `files_monitor.db`, `monitor.log`). FFmpeg is found in `bin\` next to the `.pyw` or on `PATH`.

### Command-line options

| Option | What it does |
|---|---|
| `--minimized` | start in the notification area |
| `--quit` | ask a running copy to exit and wait until it has (used by the installer); exit code 0 = nothing running any more |
| `--self-test [--self-test-out=PATH]` | start in a throwaway data folder, check the build end to end, write a JSON report, exit 0/1 |
| `--check-styled FOLDER [--config PATH] [--json PATH]` | read-only: what the settings would do to every `.ass` file in FOLDER |
| `--import-from FOLDER [--json PATH]` | copy settings and the file list from an older layout into the current data folder |

### Testing next to a running copy

Set `ASC_INSTANCE_TOKEN` to any short name before starting a test copy. It then keeps all its data in `%TEMP%\asc-instance-<token>`, uses its own single-instance mutex and quit event, keeps its "Startup folder" inside that private folder, and shows `[TEST <token>]` in the title. It can't touch the real copy's settings or login shortcut.

```powershell
$env:ASC_INSTANCE_TOKEN = "dev1"; .venv\Scripts\pythonw anime_subs_customizer.pyw
```

## Tests

```powershell
.venv\Scripts\python -m pytest tests -q
```

CI runs the suite on Windows with Python 3.10 to 3.14 (`.github/workflows/tests.yml`). It covers the styling engine (decoding, golden legacy-encoding fixtures, typesetting detection, sizing, byte-faithful round trips), the backup rules, the monitor's decisions (including two copies on one library), the data-folder rules, single instance and `--quit`, autostart, the update check, the friendly error messages, and the English/Arabic string tables. A few font tests skip themselves when a font they measure isn't installed.

`tools/make_demo_library.py OUTDIR --seed 7` generates a copyright-free test library (videos with embedded tracks in several languages, sidecars in every supported encoding). `tools/screenshots.py` makes the documentation screenshots from it.

## Building the Windows app

```powershell
pwsh scripts/build.ps1 -Python <path to python 3.13.x> -FFmpegDir <folder with ffmpeg.exe>
```

It creates a hash-locked build environment (`.venv-build`, from `requirements-lock.txt`), runs the tests, freezes the app with PyInstaller (`AnimeSubsCustomizer.spec`), runs the frozen exe's `--self-test`, writes `THIRD-PARTY-NOTICES.txt`, and builds the Inno Setup 7 installer (`installer/AnimeSubsCustomizer.iss`), the portable ZIP and `SHA256SUMS.txt` in `build\out`. `scripts/installer-smoke.ps1` then tests the installer on a clean machine (silent install, self-test, upgrade over a running copy, uninstall).

Releases are built by `.github/workflows/release.yml`; see [releasing.md](releasing.md).

The build's exact dependencies (with hashes) are in `requirements-lock.txt`, generated from `requirements-build.in`. After changing either `requirements.txt` or `requirements-build.in`, regenerate it:

```powershell
uv pip compile requirements-build.in --generate-hashes --python-version 3.13 --python-platform x86_64-pc-windows-msvc -o requirements-lock.txt
```

Dependabot alerts report vulnerable dependencies; updates (the pinned action SHAs in `.github/workflows`, `requirements*.txt` and the lock above) are done by hand.

## How the styling works

For each `.ass` file the app:

1. **Decodes it strictly**: BOM, then UTF-8, then the legacy-encoding rules in `FileProcessor._legacy_candidates` (chardet 5 for CJK/Cyrillic, a direct Arabic check, a Japanese-vs-Arabic plausibility check). A file that can't be decoded cleanly (including UTF-16 without a BOM) is skipped and listed under Issues, never rewritten.
2. **Separates dialogue from typesetting.** A style counts as typesetting when most of its lines use positioning, drawing, rotation, clipping, transforms or karaoke, or when its name marks it as a sign, song, title and so on (including Arabic names). Those styles and their lines aren't touched.
3. **Checks the font.** If the font can't render the file's dialogue script, the file is left alone (logged).
4. **Backs it up** following [ADR-006](adr/ADR-006-first-run-safety-language.md): the original bytes go to `Original Subs/<name>`; a newer release that replaced the file becomes the original (the old one moves to `Original Subs/.older/`); styled or empty content is never saved as an original.
5. **Rewrites the dialogue styles** by the file's own `Format:` order: `Fontname`, `PrimaryColour`, `Bold`, `Italic`, `Underline`, black `OutlineColour`/`BackColour`, `Outline`/`Shadow` scaled to the script resolution, and `Fontsize` for a size preset.
6. **Colours the dialogue lines**: each line in those styles starts with `{\c&H…&}` in your colour (replacing a leading colour block, never stacking).
7. **Sets `ScaledBorderAndShadow: yes`** when the header is missing (an explicit `no` is respected).

Everything else stays byte-for-byte the same: encoding, line endings, embedded `[Fonts]`, extradata and comments. The file is written atomically (temp file, fsync, rename), and not at all if it changed while being processed.

## Architecture

See [architecture.md](architecture.md) and the decision records in [adr/](adr/).

| Module | Responsibility |
|---|---|
| `anime_subs_customizer.pyw` | GUI (customtkinter), tray, dialogs, command-line entry points |
| `anime_subs/runtime.py` | paths and data folder, config, logging, atomic IO, tracked child processes, issues |
| `anime_subs/i18n.py` | English/Arabic strings, right-to-left helpers, language detection, friendly error texts |
| `anime_subs/instance.py` | single-instance mutex and the `--quit` event |
| `anime_subs/db.py` | SQLite (WAL) file list |
| `anime_subs/fonts.py` | font discovery, coverage, per-font size measurement |
| `anime_subs/media.py` | FFmpeg location, extraction languages |
| `anime_subs/ass_editor.py` | the ASS engine: decode, typesetting detection, styling, backups, restore, extraction |
| `anime_subs/state.py` | app state and config snapshots |
| `anime_subs/monitor.py` | the folder monitor (watchdog-woken, reconciled against recorded state) |
| `anime_subs/updates.py` | the opt-in update check |
| `anime_subs/migrate.py` | `--import-from` |
| `anime_subs/check_styled.py` | `--check-styled` |
