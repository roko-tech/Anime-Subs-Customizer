# ADR-003: Data location and migration

- Status: accepted (2026-10-09)

## Context

`_resolve_appdata_dir()` used to prefer the app's own folder whenever it was writable. A per-user install under `%LOCALAPPDATA%\Programs\…` *is* writable, so an installed exe would have kept config, the file index (`files_monitor.db`), logs and caches inside the install folder, which upgrades rewrite and uninstall deletes. Importing the engine also opened `monitor.log` and moved legacy `%APPDATA%` data as a side effect.

The maintainer's own setup is a source checkout whose data sits beside the `.pyw`. That data includes favourites, per-font size nudges, and an index of a real library styled with size presets. Moving it to the exe must not trigger a "restyle wave", where every file looks unstyled and is rewritten.

## Decision

- **Where the data lives.** The order is decided by the pure, tested function `runtime._data_dir_candidates`:
  1. **`ASC_INSTANCE_TOKEN` set** (tests only): `%TEMP%\asc-instance-<token>`, and nothing else.
  2. **Packaged build**: `%LOCALAPPDATA%\Anime Subs Customizer`, unless a `portable.marker` file sits beside the exe. The portable ZIP ships that marker, which keeps data beside the exe.
  3. **Source run**: beside the app (the developer layout), then `%LOCALAPPDATA%`.
  4. The temp folder as the last resort, so the app always starts.
- **No import-time side effects.** `configure_logging()` and `migrate_legacy_appdata()` run from `main()`. The legacy migration is skipped for token runs and when `APPDATA` is unset. Previously a relative `Anime Subs Customizer` folder in the working directory could have been "migrated".
- **Icon.** The shipped `icon.ico` (in the bundle's `_internal`, or beside the `.pyw`) is used. The generated placeholder is only a fallback when no icon was shipped.
- **Importing a source-layout setup** (one time, explicit): `AnimeSubsCustomizer.exe --import-from "<old folder>"` copies `config.json`, the font caches and the database into the current data folder. The database is copied with SQLite's online backup API (`sqlite3.Connection.backup`, the same mechanism as `.backup`). It never copies a live `-wal` file. Anything already in the target is first moved to `<data>\import-backup-<timestamp>\`, and the command refuses to run while the app is running. This is the path the maintainer's own setup takes (G7). A user who never ran from source never needs it.
- **Proving there's no restyle wave.** `--check-styled <library> [--config <config.json>] --json <out>` runs the monitor's own decision for every file without writing anything: already styled, left alone because the font can't render it, would restyle, or error. It is used as a differential gate (see Revisions).
- **First-run language seed.** The installer writes `HKCU\Software\Anime Subs Customizer\InstallerLanguage`. On first run, when `config.json` has no language yet, the app takes that value, then falls back to the Windows display language (ADR-006).

## Consequences

- Upgrades and reinstalls keep settings: the installer never writes to `%LOCALAPPDATA%\Anime Subs Customizer`. The uninstaller removes it only when the user asks.
- Tests and test builds can never read or write a real user's data folder, mutex or Startup shortcut.
- The source checkout keeps working unchanged for developers (CI runs from source).

## Verification

- `tests/test_runtime_paths.py` covers the candidate order for source, frozen, portable and token runs, the shipped icon, import-without-log, and a missing stderr.
- `--self-test` reports `data_dir`. In the Sandbox it was `%TEMP%\asc-instance-selftest-<pid>`, as designed for a self-test.
- G7: the maintainer's data is imported into the installed exe. The differential `--check-styled` gate (see Revisions) must pass, and then the log must show 0 restyle events in 10 minutes.

## Revisions after the checkpoint-2 critique

- **The migration gate is differential, not absolute.** A real library legitimately contains files the old version never styled (episodes that arrived while it was stopped, earlier failures, SSA v4 files). So the old engine (the previous release's code, run from a scratch source clone) and the new frozen exe each classify the same read-only library with the same config. The gate passes only when the new "would restyle" set is a subset of the old one and the new errors are a subset of the old errors. The live check after the move stays the same: 0 restyle events in the log for 10 minutes.
- **Schema versions.** The file index carries `PRAGMA user_version` (1 = this release, which adds the `restored` and `foreign` statuses) and `config.json` carries `schema_version`. Migrations are additive. An older build reading a newer index treats unknown statuses as "indexed", so it may restyle restored files. That is acceptable for a downgrade and documented in the release notes.
- **`--import-from`** refuses while the app's static mutex is held (an installed or source-run copy). It seeds `consented_folders` with the imported folder, so no confirmation appears for an already-watched library.
- If the data folder ever falls back to `%TEMP%`, the app warns once, because temp files can be cleaned up by Windows.
- `--self-test` deletes its own throwaway data folder when it finishes.
