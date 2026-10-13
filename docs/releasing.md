# Releasing

## Versioning

- [SemVer](https://semver.org/). `APP_VERSION` in `anime_subs/runtime.py` is the single source: the exe's version resource, the installer's `AppVersion`, the About text and the update check all read it. `release.yml` fails if the tag (`vX.Y.Z`) doesn't match.
- Patch for fixes, minor for features, major for anything that changes what the app does to existing files.
- [Keep a Changelog](https://keepachangelog.com/): every user-visible change goes under `## [Unreleased]` in `CHANGELOG.md`, written for users.

## Flow: tag → CI → draft → verify → publish

1. **Prepare on `master`.**
   - Bump `APP_VERSION`.
   - Move `[Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`.
   - Write the bilingual release notes in `docs/release-notes/vX.Y.Z.md`: English first, then Arabic inside `<div dir="rtl" lang="ar">`.
   - Commit and push. `tests.yml` must be green on that commit.
2. **Rehearse** (no tag):
   ```
   gh workflow run release.yml -f ref=master
   ```
   It runs the whole pipeline and uploads the assets as workflow artifacts, but creates no release. On a public repository it also attests them; on a private one the attest job is skipped. Check the installer smoke screenshot.
3. **Tag:**
   ```
   git tag -a vX.Y.Z -m "Anime Subs Customizer X.Y.Z"
   git push origin vX.Y.Z
   ```
   `release.yml` then:
   - verifies tag = `APP_VERSION`;
   - runs the tests;
   - builds the exe and runs its `--self-test`;
   - builds the installer, then installs it silently, runs `--self-test` on the installed copy, takes a screenshot and uninstalls silently;
   - builds the portable ZIP;
   - writes `SHA256SUMS.txt` and the CycloneDX SBOM;
   - **attests** every asset (public repo only);
   - creates a **draft** release with all assets and the notes file as the body.
4. **Verify the draft** (signed in):
   ```
   gh release download vX.Y.Z -D verify
   cd verify
   sha256sum -c SHA256SUMS.txt
   gh attestation verify AnimeSubsCustomizer-Setup.exe -R roko-tech/Anime-Subs-Customizer
   ```
   Then install the downloaded, Mark-of-the-Web-stamped installer in Windows Sandbox (first run in English and Arabic). Launched from Explorer or `Start-Process`, it first shows Windows' *Open File - Security Warning* (Unknown Publisher): click **Run**, as users do.
5. **Publish** the draft:
   ```
   gh release edit vX.Y.Z --draft=false --latest
   ```
   "Latest" is locked once an immutable release is published, so set it here.
6. **After publishing**, check while signed out (private browser window, `curl`):
   - both READMEs render;
   - `https://github.com/roko-tech/Anime-Subs-Customizer/releases/latest/download/AnimeSubsCustomizer-Setup.exe` redirects to the new asset;
   - `gh release verify-asset vX.Y.Z AnimeSubsCustomizer-Setup.exe` passes.
7. **VirusTotal**, without an account: on [virustotal.com](https://www.virustotal.com/)'s **URL** tab, submit the versioned links `…/releases/download/vX.Y.Z/AnimeSubsCustomizer-Setup.exe` and `…-portable.zip`. VirusTotal downloads each file and opens its file report at `virustotal.com/gui/file/<sha256>`; if a report doesn't appear, upload that file on the **File** tab. Add the report links to the notes (`gh release edit vX.Y.Z --notes-file …`; notes stay editable on an immutable release) and to `docs/release-notes/vX.Y.Z.md`. Draft assets aren't public, so this runs after publishing.

If the tag run fails before the draft is created **because of infrastructure** (a runner hiccup, a network timeout), re-run it from the tag: `gh workflow run release.yml -f ref=vX.Y.Z`. The workflow builds from the tag and updates the draft if one already exists; it never touches a published release. If the failure needs a **code change**, that version is burned: fix it on `master`, mark X.Y.Z as "not released" in the CHANGELOG, and release X.Y.Z+1. **Never move or delete a pushed tag.**

Before the first tag on a repository, rehearse the full pipeline on that public repository, including `actions/attest`, which a private-repo rehearsal can't run. That way the tag run has no step that was never exercised.

## Immutable releases

Immutable releases are enabled on the repository, so a published release's assets and tag can never change, and a deleted tag can never be reused. Always start from a draft. Release notes stay editable.

## Rollback

There is no "unpublish". A bad release is fixed **forward**:

1. Mark the bad release as not latest:
   ```
   gh release edit vBAD --latest=false
   ```
   Re-mark the previous good release as latest so the stable download link points at it again:
   ```
   gh release edit vGOOD --latest
   ```
2. Add a warning at the top of the bad release's notes, in both languages, saying what's wrong and which version to use.
3. Ship `vX.Y.Z+1` with the fix through the normal flow.
4. If the bad build could damage files, also open a pinned issue explaining how to recover: **Restore originals…** brings back every original from `Original Subs/`.

Users on the bad version with the update check enabled are told about the fixed version within a day.

## Release assets (version-less names)

| Asset | What |
|---|---|
| `AnimeSubsCustomizer-Setup.exe` | per-user installer (English/Arabic), includes FFmpeg (optional component) |
| `AnimeSubsCustomizer-portable.zip` | one-folder app + `portable.marker` + FFmpeg |
| `SHA256SUMS.txt` | checksums of every other asset |
| `sbom.cdx.json` | CycloneDX SBOM of the Python build environment (a superset of the bundled packages; the native components are in the notices) |
| `THIRD-PARTY-NOTICES.txt` | licences of everything bundled |
| `FFMPEG-SOURCE.md` + `ffmpeg-n<version>-source.tar.gz` + `zlib-<version>-source.tar.gz` | the LGPL source offer for the bundled FFmpeg: the exact archives the build used, the configure line and the build script link |

## FFmpeg

`packaging/ffmpeg/build-ffmpeg.sh` builds the bundled FFmpeg from source on the `ffmpeg` job's Linux runner (llvm-mingw cross-compile, LGPL 2.1 or later, zlib only; ADR-004). It fails if a requested component is dropped, if any GPL/nonfree/version3 flag is on, or if a DLL imports anything that is neither shipped nor part of Windows. To update FFmpeg, change the pinned tag, URL and sha256 at the top of the script; the build is reproducible, so two runs give identical files.

## Signing (later)

`release.yml` has no signing step yet (ADR-005). Once SignPath Foundation approves the project, add their signing action between `build` and `attest`, so the attested and published files are the signed ones, and approve each signing request by hand.

## winget (later)

Submit with [Komac](https://github.com/russellbanks/Komac):

```
komac new
```

Use `InstallerType: inno`, `Scope: user`, and the **versioned** installer URL (`…/releases/download/vX.Y.Z/AnimeSubsCustomizer-Setup.exe`). See the public issue.
