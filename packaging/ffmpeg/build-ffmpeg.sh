#!/usr/bin/env bash
# Build a minimal, LGPL-2.1-or-later FFmpeg for Windows x64 for Anime Subs Customizer:
# ffmpeg.exe + ffprobe.exe + shared DLLs, cross-compiled on Linux x86_64 with llvm-mingw.
# The only third-party library is zlib (linked statically into the DLLs that need it).
#
# Usage:  ./build-ffmpeg.sh [OUT_DIR]
#   OUT_DIR   where the redistributable files are staged (default: $WORK_DIR/dist/bin)
#   WORK_DIR  (env) scratch directory, wiped (except downloads/) on every run
#             (default: $HOME/asc-ffmpeg)
#   JOBS      (env) parallel make jobs (default: nproc)
#
# Host requirements: bash, curl, tar, xz, make, sha256sum, coreutils, sed/awk.
# No root, no apt packages, no nasm, no pkg-config. Every download is pinned by sha256.
set -euo pipefail

# ---- pinned inputs ---------------------------------------------------------
LLVM_MINGW_VERSION=20261006
LLVM_MINGW_URL="https://github.com/mstorsjo/llvm-mingw/releases/download/${LLVM_MINGW_VERSION}/llvm-mingw-${LLVM_MINGW_VERSION}-ucrt-ubuntu-22.04-x86_64.tar.xz"
LLVM_MINGW_SHA256=5f9c6ed95b2d4bdb2869a488c5fd5857fbdabcf288a0aa3eb1da43f6a08d8ab4

ZLIB_VERSION=1.3.1
ZLIB_URL="https://github.com/madler/zlib/releases/download/v${ZLIB_VERSION}/zlib-${ZLIB_VERSION}.tar.gz"
ZLIB_SHA256=9a93b2b7dfdac77ceba5a558a580e74667dd6fede4585b91eefb60f03b72df23

FFMPEG_TAG=n9.0.2
FFMPEG_TAG_OBJECT=ce8f11b9fac4cd7ccfb41ca7c01bc17f5cda8cc3   # git tag object (informational)
FFMPEG_URL="https://github.com/FFmpeg/FFmpeg/archive/refs/tags/${FFMPEG_TAG}.tar.gz"
FFMPEG_SHA256=6e374ed621e48faa40639307dff48ba6fe574a509977956d2cce9669b7cc27e9

# ---- layout ----------------------------------------------------------------
WORK_DIR=${WORK_DIR:-$HOME/asc-ffmpeg}
OUT_DIR=${1:-${OUT_DIR:-$WORK_DIR/dist/bin}}
JOBS=${JOBS:-$(nproc)}
TRIPLE=x86_64-w64-mingw32

DL="$WORK_DIR/downloads"
mkdir -p "$DL"
cd "$WORK_DIR"
rm -rf toolchain zlib-src zlib ffmpeg stage
mkdir -p toolchain zlib-src zlib/include zlib/lib ffmpeg stage

fetch() {  # url sha256 file
    local url=$1 sha=$2 file="$DL/$3"
    if [ -f "$file" ] && echo "$sha  $file" | sha256sum -c --status; then
        echo "cached: $3"; return
    fi
    curl -fL --retry 3 --retry-delay 5 -o "$file.part" "$url"
    if ! echo "$sha  $file.part" | sha256sum -c --status; then
        echo "ERROR: sha256 mismatch for $url" >&2
        sha256sum "$file.part" >&2
        exit 1
    fi
    mv "$file.part" "$file"
}

fetch "$LLVM_MINGW_URL" "$LLVM_MINGW_SHA256" "llvm-mingw.tar.xz"
fetch "$ZLIB_URL"       "$ZLIB_SHA256"       "zlib-${ZLIB_VERSION}.tar.gz"
fetch "$FFMPEG_URL"     "$FFMPEG_SHA256"     "ffmpeg-${FFMPEG_TAG}.tar.gz"

tar -xJf "$DL/llvm-mingw.tar.xz" -C toolchain --strip-components=1
tar -xzf "$DL/zlib-${ZLIB_VERSION}.tar.gz" -C zlib-src --strip-components=1
tar -xzf "$DL/ffmpeg-${FFMPEG_TAG}.tar.gz" -C ffmpeg --strip-components=1
export PATH="$WORK_DIR/toolchain/bin:$PATH"

# ---- zlib (static) ---------------------------------------------------------
make -C zlib-src -f win32/Makefile.gcc -j"$JOBS" libz.a \
    CC="$TRIPLE-clang" AR=llvm-ar RC="$TRIPLE-windres" CFLAGS="-O2 -Wall"
cp zlib-src/zlib.h zlib-src/zconf.h zlib/include/
cp zlib-src/libz.a zlib/lib/

# ---- FFmpeg ----------------------------------------------------------------
cd "$WORK_DIR/ffmpeg"
# GitHub tag archives have no VERSION file; without it version.sh falls back to
# `git describe`, which would pick up whatever repo WORK_DIR happens to live in.
echo "${FFMPEG_TAG#n}" > VERSION

# Relative zlib paths keep the build host's home directory out of the binaries
# (the configure line is embedded in every library). Configure and make both
# run from this directory, so ../zlib resolves the same way for both.
CONFIGURE_FLAGS=(
    --enable-cross-compile --target-os=mingw32 --arch=x86_64
    --cross-prefix="$TRIPLE-"
    --cc="$TRIPLE-clang" --cxx="$TRIPLE-clang++"
    --ar=llvm-ar --ranlib=llvm-ranlib --nm=llvm-nm --strip=llvm-strip
    --windres="$TRIPLE-windres" --pkg-config=false
    --enable-shared --disable-static
    --disable-autodetect --disable-everything
    --disable-doc --disable-network --disable-ffplay --disable-avdevice
    --disable-x86asm
    --enable-w32threads
    --enable-zlib --extra-cflags=-I../zlib/include
    # --no-insert-timestamp: PE header time = 0, so rebuilds are bit-identical
    --extra-ldflags="-L../zlib/lib -Wl,--no-insert-timestamp"
    --enable-protocol=file,pipe
    --enable-demuxer=matroska,mov,avi,mpegts,flv,asf,ass,srt,webvtt,sup
    --enable-muxer=ass,srt,sup,image2pipe,webvtt,null
    --enable-decoder=ass,ssa,subrip,srt,movtext,webvtt,pgssub,dvdsub,h264,hevc,mpeg2video,mpeg4,vp8,vp9,png,mjpeg,aac,mp3
    # wrapped_avframe + pcm_s16le are the null muxer's default video/audio
    # encoders; without them `-f null -` fails with "Encoder not found".
    --enable-encoder=ass,ssa,subrip,srt,movtext,webvtt,png,wrapped_avframe,pcm_s16le
    --enable-parser=h264,hevc,mpegvideo,mpeg4video,vp8,vp9,aac
    --enable-bsf=h264_mp4toannexb,hevc_mp4toannexb
    --enable-filter=scale,format,null,anull,copy,aresample,aformat,trim,atrim,setpts
)
./configure "${CONFIGURE_FLAGS[@]}" 2>&1 | tee ../configure.log
# configure only warns about misspelt or dependency-disabled components; fail instead.
if grep -E "did not match anything|because not all dependencies are satisfied|unknown option" ../configure.log; then
    echo "ERROR: configure dropped a requested component (see above)" >&2
    exit 1
fi
if grep -Eq "^CONFIG_(GPL|NONFREE|VERSION3)=yes" ffbuild/config.mak; then
    echo "ERROR: non-LGPL licence flag enabled" >&2; exit 1
fi
grep -q "^License: LGPL version 2.1 or later" ../configure.log || {
    echo "ERROR: configure did not report LGPL 2.1 or later" >&2; exit 1; }

make -j"$JOBS"
make install DESTDIR="$WORK_DIR/stage"   # installs stripped exes + DLLs into usr/local/bin

# ---- stage redistributable --------------------------------------------------
STAGE_BIN="$WORK_DIR/stage/usr/local/bin"
mkdir -p "$OUT_DIR"
rm -f "$OUT_DIR"/*.exe "$OUT_DIR"/*.dll
cp "$STAGE_BIN"/ffmpeg.exe "$STAGE_BIN"/ffprobe.exe "$STAGE_BIN"/*.dll "$OUT_DIR"/
cp COPYING.LGPLv2.1 "$OUT_DIR"/COPYING.LGPLv2.1.txt
cp "$WORK_DIR/zlib-src/LICENSE" "$OUT_DIR"/LICENSE.zlib.txt

# Every import must be a DLL we ship or a Windows system DLL.
SYSTEM_DLLS='^(kernel32|user32|advapi32|bcrypt|ole32|shell32|shlwapi|psapi|secur32|ws2_32|api-ms-win-[a-z0-9-]+)\.dll$'
missing=0
for f in "$OUT_DIR"/*.exe "$OUT_DIR"/*.dll; do
    for dll in $(llvm-objdump -p "$f" | awk '/DLL Name:/ {print $3}'); do
        if [ -f "$OUT_DIR/$dll" ]; then continue; fi
        if echo "$dll" | tr 'A-Z' 'a-z' | grep -Eq "$SYSTEM_DLLS"; then continue; fi
        echo "ERROR: $(basename "$f") imports $dll, which is neither shipped nor a system DLL" >&2
        missing=1
    done
done
[ "$missing" = 0 ] || exit 1

# ---- BUILD-INFO.txt ----------------------------------------------------------
{
    echo "Minimal LGPL FFmpeg build for Anime Subs Customizer (Windows x64)"
    echo
    echo "FFmpeg:     $FFMPEG_TAG (tag object $FFMPEG_TAG_OBJECT)"
    echo "            $FFMPEG_URL"
    echo "            sha256 $FFMPEG_SHA256"
    echo "zlib:       $ZLIB_VERSION (static)"
    echo "            $ZLIB_URL"
    echo "            sha256 $ZLIB_SHA256"
    echo "Toolchain:  llvm-mingw $LLVM_MINGW_VERSION (UCRT)"
    echo "            $LLVM_MINGW_URL"
    echo "            sha256 $LLVM_MINGW_SHA256"
    echo "            $("$TRIPLE-clang" --version | head -n1)"
    echo
    echo "zlib build: make -f win32/Makefile.gcc libz.a CC=$TRIPLE-clang AR=llvm-ar RC=$TRIPLE-windres CFLAGS=\"-O2 -Wall\""
    echo
    echo "FFmpeg configure line (run in the source dir, zlib installed in ../zlib):"
    printf './configure'; printf ' %q' "${CONFIGURE_FLAGS[@]}"; echo
    echo
    sed -n '/^License:/p' ../configure.log
    echo
    echo "Files (sha256):"
    (cd "$OUT_DIR" && sha256sum ./*.exe ./*.dll)
    echo
    echo "Imports (llvm-objdump -p):"
    for f in "$OUT_DIR"/*.exe "$OUT_DIR"/*.dll; do
        echo "  $(basename "$f"): $(llvm-objdump -p "$f" | awk '/DLL Name:/ {printf "%s ", $3}')"
    done
    echo
    echo "ffmpeg -version:"
    if "$OUT_DIR/ffmpeg.exe" -hide_banner -version 2>/dev/null | tr -d '\r'; then :; else
        echo "  (not run: build host cannot execute Windows binaries)"
    fi
} > "$OUT_DIR/BUILD-INFO.txt"

# ---- LGPL source offer (release assets) ---------------------------------------
# SOURCE_OUT (env, optional): where to put the exact source archives this build
# used and FFMPEG-SOURCE.md. ASC_REF (env, optional): the git ref of this script,
# for the link in FFMPEG-SOURCE.md.
if [ -n "${SOURCE_OUT:-}" ]; then
    mkdir -p "$SOURCE_OUT"
    FF_SRC="ffmpeg-${FFMPEG_TAG}-source.tar.gz"
    ZLIB_SRC="zlib-${ZLIB_VERSION}-source.tar.gz"
    cp "$DL/ffmpeg-${FFMPEG_TAG}.tar.gz" "$SOURCE_OUT/$FF_SRC"
    cp "$DL/zlib-${ZLIB_VERSION}.tar.gz" "$SOURCE_OUT/$ZLIB_SRC"
    REF=${ASC_REF:-master}
    REF=${REF#refs/tags/}
    REF=${REF#refs/heads/}
    {
        echo "# FFmpeg in Anime Subs Customizer: source and build"
        echo
        echo "The app's \`bin\` folder contains FFmpeg ${FFMPEG_TAG#n}: \`ffmpeg.exe\`, \`ffprobe.exe\` and their DLLs."
        echo "It is licensed under the **GNU Lesser General Public License, version 2.1 or later**"
        echo "(\`COPYING.LGPLv2.1.txt\` next to it). FFmpeg is a trademark of Fabrice Bellard."
        echo "This build contains no GPL, nonfree or third-party codec libraries. Its only"
        echo "other component is zlib ${ZLIB_VERSION} (zlib licence, \`LICENSE.zlib.txt\`), linked statically."
        echo
        echo "## Corresponding source"
        echo
        echo "Attached to this release, byte for byte the archives the build used:"
        echo
        echo "| File | sha256 | Upstream |"
        echo "|---|---|---|"
        echo "| \`$FF_SRC\` | \`$FFMPEG_SHA256\` | $FFMPEG_URL (tag \`$FFMPEG_TAG\`, tag object \`$FFMPEG_TAG_OBJECT\`) |"
        echo "| \`$ZLIB_SRC\` | \`$ZLIB_SHA256\` | $ZLIB_URL |"
        echo
        echo "FFmpeg's source is not modified. The build script only adds a \`VERSION\` file"
        echo "containing \`${FFMPEG_TAG#n}\`, because tag archives don't include one."
        echo
        echo "## How it was built"
        echo
        echo "[\`packaging/ffmpeg/build-ffmpeg.sh\`](https://github.com/roko-tech/Anime-Subs-Customizer/blob/$REF/packaging/ffmpeg/build-ffmpeg.sh)"
        echo "cross-compiles it on Linux x86_64 with llvm-mingw $LLVM_MINGW_VERSION (UCRT,"
        echo "sha256 \`$LLVM_MINGW_SHA256\`). Every download is pinned by sha256. To rebuild:"
        echo
        echo '```sh'
        echo 'bash packaging/ffmpeg/build-ffmpeg.sh /path/to/out'
        echo '```'
        echo
        echo "Configure line:"
        echo
        echo '```'
        printf './configure'; printf ' %q' "${CONFIGURE_FLAGS[@]}"; echo
        echo '```'
        echo
        echo "## Replacing it"
        echo
        echo "The DLLs are unmodified FFmpeg libraries, installed as separate files in the"
        echo "\`bin\` folder of the app (\`%LOCALAPPDATA%\\Programs\\Anime Subs Customizer\\bin\`, or"
        echo "\`bin\` inside the portable copy). You may replace them, or the whole folder, with"
        echo "your own build of the same FFmpeg major versions. Without a \`bin\` folder the app"
        echo "uses \`ffmpeg\` and \`ffprobe\` from \`PATH\`. Installing a newer version of the app"
        echo "replaces \`bin\`, so put your own build back after upgrading."
        echo
        echo "## Files"
        echo
        echo '```'
        (cd "$OUT_DIR" && sha256sum ./*.exe ./*.dll)
        echo '```'
    } > "$SOURCE_OUT/FFMPEG-SOURCE.md"
    echo "Source offer: $SOURCE_OUT"
fi

echo
echo "Done: $OUT_DIR"
ls -l "$OUT_DIR"
