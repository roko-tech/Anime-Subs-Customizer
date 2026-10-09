# Security Policy

## Supported versions

Only the latest release receives fixes. The app can tell you about new versions (⋯ → **Check for updates once a day**).

## Reporting a vulnerability

Please report security problems **privately** through GitHub's
[private vulnerability reporting](https://github.com/roko-tech/Anime-Subs-Customizer/security/advisories/new)
(the **Security** tab → **Report a vulnerability**). Don't open a public issue for them.

Include what you found, how to reproduce it, and which version you ran (⋯ → **About**). You can expect an acknowledgement within a week. Fixes ship as a new release; published releases are immutable and never changed in place.

## Verifying a download

Every release has `SHA256SUMS.txt` and a GitHub build-provenance attestation. See [Check your download](docs/install-help.md#check-your-download).

## What's in scope

Anime Subs Customizer runs locally. Its only network request is the optional, off-by-default update check to `api.github.com`, which sends nothing but the app version in its `User-Agent` and only ever opens release pages under this repository. The areas that matter most:

- **File handling.** The app rewrites subtitle files inside the folder you choose and writes backups next to them. Anything that makes it read, write or delete a file outside that folder, lose an original, or corrupt a file it touches is in scope.
- **External programs.** For subtitle extraction and preview frames it runs `ffmpeg`/`ffprobe` from the app's `bin\` folder or from `PATH` (a match in the current directory is refused), always with an argument list and a timeout. Anything that gets it to run a different program, or to pass crafted arguments, is in scope.
- **Untrusted input.** Subtitle and video files come from third parties. A crafted `.ass` or media file that causes code execution or escapes the watched folder is in scope.
- **The installer and the update check.** Anything that makes the installer write outside the user's install and data folders, or makes the update check open a page outside this repository's releases, is in scope.
