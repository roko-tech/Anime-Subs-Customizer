# ADR-008: Update notifications

- Status: accepted (2026-10-09)

## Context

An unsigned app without an update channel leaves users on old versions with known bugs. The bundled parsers and FFmpeg would also never get security fixes. The app currently makes **no network connections**, and the README says so. SignPath's terms require a privacy statement and an opt-out for any connection the user didn't choose. Unauthenticated GitHub API calls are limited to 60 per hour per IP and need a `User-Agent`.

## Options

1. No update check. Users find releases themselves.
2. **An opt-in daily check** that shows a notice with a link. Nothing is downloaded automatically.
3. Automatic download and install. That needs signing to be trustworthy, and it's a much bigger trust surface.

## Decision

- **Option 2.** Off by default. The user turns it on in the welcome dialog (a checkbox, unticked) or later in ⋯ → "Check for updates once a day".
- **Request:** `GET https://api.github.com/repos/roko-tech/Anime-Subs-Customizer/releases/latest`, `User-Agent: AnimeSubsCustomizer/<version>`, `Accept: application/vnd.github+json`, 10 s timeout, at most once per 24 h (`last_update_check` stored in the config). It sends no identifiers, cookies or file information.
- **Comparison:** the `tag_name` (`vX.Y.Z`) is compared as SemVer with `APP_VERSION`. Drafts and prereleases are ignored, because the endpoint never returns them.
- **Failures** (no network, 403/429 rate limit, unexpected JSON) are silent and logged at INFO. The next attempt waits a day.
- **Notice:** a quiet footer pill, "Update available: vX.Y.Z", and a single tray balloon. Clicking either opens the release page in the browser. The app never downloads or runs anything itself.
- **Privacy statement** (README, both languages): "No telemetry. The app connects to the internet only if you turn on the daily update check. It then asks GitHub once a day for the latest version number; GitHub sees your IP address and the app version, nothing else."

## Consequences

- When the check is enabled, the only outbound connection is to `api.github.com:443`. Matrix row 12 checks it with `Get-NetTCPConnection`.
- Users who never opt in get no notifications. The README and release notes say where updates appear.

## Verification

- Unit tests: version comparison, the 24 h throttle, silent handling of 403/429/garbage (mocked HTTP), and that nothing is requested while the check is off.
- Matrix row 12: with the check off, no TCP connections over 10 minutes. With it on, exactly one to `api.github.com`.

## Revisions after the checkpoint-2 critique

- **Notify whenever the latest published version differs from the running one**, as "Recommended version: vX.Y.Z". A rollback that re-points "latest" to an older good release then also reaches users of the bad one. Development builds newer than the latest release are not nagged.
- The request runs on a daemon thread, never on the UI thread. The only URL the app opens is built from a hard-coded `https://github.com/roko-tech/Anime-Subs-Customizer/releases/tag/` prefix plus a validated `vX.Y.Z` tag. The API's `html_url` is ignored.
