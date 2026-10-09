# ADR-004: FFmpeg

- Status: accepted (2026-10-09); outcome recorded 2026-10-10: the self-built minimal LGPL FFmpeg is used (see the end)

## Context

FFmpeg and ffprobe power two features: extracting embedded subtitle tracks from videos, and the "Real frame" / "Subtitle line" preview. Without them the app still styles `.ass` files, but those features are greyed out. The maintainer's own `bin\` holds gyan.dev builds configured with `--enable-gpl --enable-version3`. Those are GPLv3 and must never be redistributed with an MIT app.

## Options

1. **Bundle BtbN's `win64-lgpl-shared` build** in the installer, as a component that is checked by default.
2. Guided install: the app links to a download page or suggests `winget install`. A non-technical user would have to find, unzip and place two files, or open a terminal.
3. An opt-in download from inside the app. That adds network code, an update and trust surface, and a privacy statement.

## Decision

- **Option 1, bundle**, with an exact, verifiable pin: BtbN autobuild tag `autobuild-2026-10-09-14-16`, asset `ffmpeg-n9.0.2-24-gfd5d616c29-win64-lgpl-shared-9.0.zip`, SHA-256 `1B5EBBF0A06DEF4602C8AC0E98F9987689A1BCFB56FC77DBEB33E38185EC97C6`.
- **Its configure line has `--enable-version3` (LGPLv3) and no `--enable-gpl` or `--enable-nonfree`.** The build script asserts this from `ffmpeg -version` before packaging, so it fails rather than ship GPL code.
- **Shipped files:** `ffmpeg.exe`, `ffprobe.exe` and the DLLs they load (`avcodec`, `avformat`, `avfilter`, `avdevice`, `avutil`, `swresample`, `swscale`), unrenamed, in `{app}\bin\`. FFmpeg's `LICENSE.txt` goes beside them. `ffplay.exe` is not shipped. The app already searches `app_root()\bin`, which is the exe's folder in a frozen build.
- **Source offer (LGPL):** each release publishes `FFMPEG-SOURCE.md`. It names the exact FFmpeg commit (`fd5d616c29`), the BtbN build-script commit and the configure line, and links to the corresponding source archive. The release also attaches `ffmpeg-source-fd5d616c29.tar.gz`, downloaded from the FFmpeg git commit, so the source sits on the same server as the binaries.
- **Credit:** README (both languages), `THIRD-PARTY-NOTICES.txt` and the in-app About text say "This software uses libraries from the FFmpeg project under the LGPLv3".
- **The installer component can be unticked.** Users who already have FFmpeg on PATH still work, because the locator also searches PATH. It rejects a hit in the current directory.

## Consequences

- The installer grows by roughly 45–55 MB compressed. In return, extraction and the real-frame preview work out of the box.
- Updating FFmpeg means changing the pin (tag, asset and SHA-256), and the build re-verifies the configure line.
- The maintainer's GPL `bin\` is excluded by construction. Release builds run on GitHub from a clean checkout, and `bin\` is gitignored. The pre-flight greps the release ZIP and installer file list for `--enable-gpl` and for `gyan`.

## Verification

- `scripts/build.ps1` and `release.yml` download by pinned tag and asset, check the SHA-256, check the configure line for no GPL or nonfree, and bundle.
- Release pre-flight: extract the portable ZIP and run `bin\ffmpeg.exe -version` to assert the configure line.
- Sandbox matrix row 1: extraction works on a clean machine with the bundled FFmpeg.

## Revision after the checkpoint-2 critique (licensing completeness)

The BtbN LGPL build statically links about 15 other libraries: LGPL ones such as libmp3lame, libfribidi, libssh, libgme, libbluray, libsoxr, libzvbi and librsvg with glib/cairo/pango, and others under MPL or BSD licences. It also builds openh264 from source. A source offer for the FFmpeg commit alone does not cover those. Revised plan, time-boxed to 2 hours:

1. **Preferred: a minimal LGPL FFmpeg of our own.** Cross-compiled with mingw-w64 on a GitHub Linux runner, from the FFmpeg release tag, with **no external libraries**. It includes only what the app uses: the matroska/mov/avi/mpegts/flv/asf demuxers; the ass/ssa/subrip/mov_text/webvtt/dvd/pgs subtitle decoders and encoders; the ass/srt/sup/image2pipe muxers; native video decoders (h264, hevc, mpeg2/4, vp8/9) for preview frames; the png encoder; and swscale. The source is the FFmpeg tag plus our committed configure script, and its only notice is FFmpeg's LGPL. If it builds, the release uses it.
2. **Fallback: BtbN.** Ship BtbN with `THIRD-PARTY-NOTICES.txt` listing every `--enable-lib*` and its licence text. The source offer names the BtbN/FFmpeg-Builds commit of the pinned autobuild, whose `scripts.d/*.sh` pin each library's source commit, as the corresponding source, and the build log is archived with the release evidence.

Either way the DLLs keep their names and sit as separate files, so a user can replace them (LGPL relinking).

## Outcome (2026-10-10)

The minimal build worked inside the time box, so **the release ships it and the BtbN pin above is no longer used.**

- `packaging/ffmpeg/build-ffmpeg.sh` cross-compiles FFmpeg `n9.0.2` with llvm-mingw `20261006` (UCRT) on the release workflow's Linux runner. Every input is pinned by sha256. The PE timestamp is zeroed, so two runs give bit-identical files.
- **Licence: LGPL 2.1 or later.** No `--enable-gpl`, `--enable-nonfree` or `--enable-version3`. The script fails if configure reports anything else, if a requested component is dropped, or if a DLL imports anything that is neither shipped nor part of Windows. The only other code is zlib 1.3.1, linked statically.
- **Shipped in `{app}\bin\`:** `ffmpeg.exe`, `ffprobe.exe`, `avcodec`, `avformat`, `avfilter`, `avutil`, `swresample` and `swscale` DLLs (no `avdevice`, no `ffplay`), plus `COPYING.LGPLv2.1.txt`, `LICENSE.zlib.txt` and `BUILD-INFO.txt`. About 8.4 MB, 3.5 MB zipped (the BtbN build: about 155 MB).
- **Source offer:** each release attaches `FFMPEG-SOURCE.md`, `ffmpeg-n9.0.2-source.tar.gz` and `zlib-1.3.1-source.tar.gz`, the exact archives the build used. `FFMPEG-SOURCE.md` has the configure line, the toolchain pin and a link to the build script at the release tag.
- **Credit:** README, About and `THIRD-PARTY-NOTICES.txt` say FFmpeg is included under the LGPL (no version claim needed there; the exact licence is in `FFMPEG-SOURCE.md`).
- **Checked on Windows:** ffprobe stream listing, `.ass`/`.srt`/PGS extraction byte-identical to a full FFmpeg build (including Arabic and 384-character paths), and the PNG preview frame.
- **Trade-off:** no hand-written x86 assembly (`--disable-x86asm`), so decoding is about 20–30% slower. A single preview frame from a 1080p file takes about 120 ms instead of 100 ms. Extraction copies subtitle packets and doesn't decode video, so it is unaffected.
