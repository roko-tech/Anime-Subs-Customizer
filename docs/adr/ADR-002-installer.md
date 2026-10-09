# ADR-002: Installer and distribution

- Status: accepted (2026-10-09)

## Context

A non-technical user needs one download that installs without admin rights and without a terminal, in English or Arabic. The app is a tray app that runs all the time (it often starts at login), so an upgrade usually finds it running. Its data lives in `%LOCALAPPDATA%\Anime Subs Customizer` (ADR-003). Its backups live beside the user's subtitles in `Original Subs\` folders, which no installer may ever touch.

## Options

- **Inno Setup 7.x**: per-user installs, an official Arabic translation with full right-to-left layout, a language dialog, mature silent switches, scriptable in Pascal.
- MSIX: it virtualises AppData and Startup writes and must be signed. That breaks autostart and the data folder, and needs a certificate. Out of scope.
- WiX/MSI: per-user MSIs are awkward, and the Arabic UI and upgrade-while-running story is weaker.
- ZIP only: no Start menu entry or uninstall entry, and SmartScreen prompts on every exe extracted with Mark-of-the-Web.

## Decision

Use **Inno Setup 7.1** (`installer/AnimeSubsCustomizer.iss`). Also ship a **portable ZIP** for power users.

- **`PrivilegesRequired=lowest`**: a per-user install into `%LOCALAPPDATA%\Programs\Anime Subs Customizer`, with no UAC prompt. The uninstall entry goes under HKCU.
- **Permanent `AppId`** GUID: `{8B6E2F44-6C1A-4E0B-9D3F-2A7C5E9B1D20}`. It never changes, so upgrades find the old install.
- **Languages:** English plus the official `Arabic.isl` (`RightToLeft=yes`). `ShowLanguageDialog=yes`, because Setup otherwise follows the Windows display language and Arabic speakers on English Windows would never see Arabic. The chosen language is written to `HKCU\Software\Anime Subs Customizer\InstallerLanguage` (`en`/`ar`) and seeds the app's first-run language (ADR-006).
- **Upgrading while the app runs:** `AppMutex=ASS-Subs-Changer-Singleton`, plus a `--quit` call. Probed on 2026-10-09: `InitializeSetup` runs *before* the AppMutex check. A silent install with the mutex held logs "Defaulting to Cancel for suppressed message box" and exits 1. So `InitializeSetup` and `InitializeUninstall` run `<installed exe> --quit`, which signals the running copy through a named event and waits until the process has exited (exit code 0). Only then does the AppMutex check run. If the app is still running after 15 s, the AppMutex prompt asks the user to close it; in silent mode Setup aborts with exit 1.
- **`[InstallDelete] {app}\_internal`**, so files removed from the bundle don't linger. `{app}` itself holds no user data, but the uninstaller never deletes anything outside its own files.
- **Shortcuts:** a Start menu shortcut, an optional desktop shortcut (unchecked by default), and an optional "Start automatically when I sign in" task. That task creates the same `Anime Subs Customizer.lnk` in the Startup folder that the app's own Autostart toggle manages (target = the exe, `--minimized`), so the two stay consistent. Each shortcut carries the AppUserModelID `RokoTech.AnimeSubsCustomizer`.
- **"Launch Anime Subs Customizer" on the Finish page** (`postinstall nowait skipifsilent`).
- **FFmpeg** is an installer component, checked by default and installed to `{app}\bin` (ADR-004).
- **Uninstall** asks (in the UI language) whether to also delete settings: config, the file index, logs and caches. The default is **keep**. Silent uninstall keeps settings unless `/REMOVESETTINGS=1` is passed. The uninstaller removes the Startup shortcut, and it **never touches `Original Subs\` folders or subtitles**: those are the user's files.
- **Portable ZIP** (`AnimeSubsCustomizer-portable.zip`): the same one-folder app plus `portable.marker`, so data stays beside the exe. FFmpeg is included under `bin\`.
- **Stable download link:** asset names carry no version (`AnimeSubsCustomizer-Setup.exe`), so `…/releases/latest/download/AnimeSubsCustomizer-Setup.exe` keeps working.
- **winget:** P2, a later issue. It needs versioned URLs (`/download/vX/…`) and a manifest submitted with Komac.

## Consequences

- One SmartScreen prompt, on the downloaded installer. Files Setup writes carry no Mark-of-the-Web, so the installed exe starts without a prompt. To be verified in the Sandbox (matrix row 1).
- Every release must test the silent paths: install → `--self-test` → uninstall, and an upgrade over a running copy.

## Verification

- The `release.yml` installer smoke runs `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`, then the installed `--self-test`, a screenshot, then a silent uninstall, and asserts `{app}` is gone and the data folder is kept.
- Sandbox matrix rows 1 and 11 cover an interactive Arabic install, an upgrade over a running tray app (interactive and silent), and uninstall keeping and then removing settings.
- `.claude/launch/evidence/P05-inno/*.log` covers the AppMutex ordering.

## Revisions after the checkpoint-2 critique

- **On quit** (including `--quit`), the app terminates the ffmpeg/ffprobe processes it started. They run under a tracked process handle, so they can't keep `{app}\bin\*` locked during an upgrade. The installer also sets `CloseApplications=force`, so Restart Manager closes anything still holding a file in silent mode.
- **Relaunch after an upgrade.** `InitializeSetup` records whether the app was running. A `[Run]` entry starts it again with `--minimized` when it was (`Check: WasRunning`), in silent mode too, so watching doesn't stop until the next sign-in.
