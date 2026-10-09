# ADR-007: Multiple instances and shared libraries

- Status: accepted (2026-10-09)

## Context

The single-instance mutex is per Windows session, and the data folder is per user. Two copies of the app can therefore watch one library: two Windows accounts with fast user switching, a desktop and a laptop on one NAS share, or a synced folder. If they use **different** settings, each one's output looks to the other like a changed file that needs restyling.

**Reproduced twice in the review** (evidence: concurrency-1 and its verifier). Two headless monitors (Arial/80FFFF and Segoe UI/00FFFF) on one 3-file folder rewrote every file every ~2.3 s, alternating, for as long as both ran: 56 rewrites in 42 s. The write-once backup stayed pristine, so there was no data loss. The cost is endless rewrites, disks and NAS that never sleep, media-server rescans and a meaningless "styled" counter.

## Options

1. **A global mutex** (`Global\`). It would stop a second Windows user from running the app at all, and it does nothing for two PCs.
2. **A folder lease file** in the library (`.anime-subs-customizer.lock` with user, PC and heartbeat). It works across PCs, but it writes into the user's library, can go stale after a crash, and fails on read-only shares.
3. **A marker line naming the writer in `[Script Info]`.** It changes every file's bytes and would make every existing styled library look unstyled (a restyle wave).
4. **Don't fight foreign output.** A file the app had recorded as its own output that changes into *someone else's* styled output (ADR-006 signature: it looks styled by an Anime Subs Customizer, but not with our settings) is marked `foreign` and left alone until it changes into something that isn't styled output, such as a new release.

## Decision

- **Option 4**, plus documentation: "Use one copy of the app per library. If two PCs share a folder, let only one of them watch it."
- When the monitor finds a known, previously styled file whose new content looks styled by us but isn't already styled with this copy's settings, it marks the file `foreign` with its current mtime and hash, and records one localised Problems entry per folder: "Another copy of Anime Subs Customizer (or another program) restyled files here with different settings. Leaving them as they are. Use one copy per library." The ping-pong stops at the first foreign write.
- **Apply to library** still restyles `foreign` files on request: an explicit user action wins.
- **Unknown files**, the first contact after a reinstall or a cleared index, that already look styled are restyled to the current settings without re-backing them up when a backup exists. That's the settings-loss path, and it isn't a fight.
- A hand edit in Aegisub that changes style rows but keeps the colour blocks is also treated as `foreign` and respected, which is the desired behaviour for manual fixes.

## Consequences

- With two copies using different settings on one library, the first copy to restyle a file wins, and the other leaves that file alone after its first look. Neither loops.
- No new files are written into the library. No locks can go stale.

## Verification

- A unit test: two `FolderMonitor`s with different settings and separate databases on one temp folder, ticked alternately. The file stabilises after at most one write each, and the foreign Problems entry is recorded once.
- Live matrix row 9: two token instances on one demo library with different fonts, watched for 2 minutes, count the "Applied changes" log lines. Expect no ping-pong.

## Revisions after the checkpoint-2 critique

- **Decision order for a known file whose content changed:**
  1. Already styled with this copy's settings → just record it. Same settings are never foreign.
  2. Looks styled by us (writer parity, ADR-006 §1) with other settings, and the content hash differs from the recorded one → `foreign`. An mtime-only touch never marks a file foreign.
  3. Otherwise → a new original.
- **Neutral wording.** The most common trigger is a hand edit in a subtitle editor or a sync tool, not a second copy. The Problems entry reads: "Changed by another program after it was styled, so it was left as it is. Apply to library restyles it." The count also appears in Apply's summary.
- **Two loops inside one copy** (Watch off → on while a scan is still in a slow step) are prevented separately. Each watch session owns its own stop event, so a stopped loop can't be revived, and a thread that is still stopping keeps its reference so a quick re-Start waits for it (`tests/test_monitor_restart.py`).
