"""Write THIRD-PARTY-NOTICES.txt for the Windows build.

Run with the BUILD environment's Python (the one PyInstaller used), so the
licences describe exactly what was bundled:

    python scripts/notices.py --out build/notices/THIRD-PARTY-NOTICES.txt [--ffmpeg-dir build/ffmpeg]

Python packages come from pip-licenses (with their licence texts); components
bundled without their own Python metadata (CPython and the libraries built into
it such as OpenSSL, Tcl/Tk, HarfBuzz inside uharfbuzz, FreeType inside
freetype-py, the Roboto font inside customtkinter, FFmpeg, zlib) come from
packaging/licenses/ and the FFmpeg folder.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LICENSES = ROOT / "packaging" / "licenses"

# Packages that end up inside the app (requirements.txt + their runtime deps).
BUNDLED = [
    "Pillow", "customtkinter", "darkdetect", "packaging", "pystray", "six",
    "uharfbuzz", "freetype-py", "arabic-reshaper", "python-bidi", "chardet",
    "watchdog", "pywin32", "winshell",
    "pyinstaller",  # only its bootloader (the launcher inside the exe) is bundled
]
# Known metadata errors (the licence text in the package is authoritative).
LICENSE_FIXES = {"customtkinter": "MIT License (PyPI metadata wrongly says CC0)"}
LGPL_NOTE = {
    "pyinstaller": "Only the PyInstaller bootloader (the small launcher inside AnimeSubsCustomizer.exe) is "
                   "included, under the bootloader exception in the licence below.",
    "chardet": "LGPL-2.1-or-later. Shipped as plain .py files in _internal\\chardet, "
               "so you can replace them with your own build of the library.",
    "pystray": "LGPL-3.0. Shipped as plain .py files in _internal\\pystray, "
               "so you can replace them with your own build of the library.",
    "python-bidi": "LGPL-3.0. Its compiled module is a separate file in _internal\\bidi, "
                   "so you can replace it with your own build of the library.",
}
NATIVE = [
    ("CPython (the Python runtime)", "PSF License Agreement", "CPython-LICENSE.txt"),
    ("Libraries built into CPython: OpenSSL 3, libffi, Expat, libmpdec, zlib, mimalloc",
     "Apache License 2.0 (OpenSSL), MIT (libffi, Expat), BSD (libmpdec) and others; "
     "SQLite is public domain", "CPython-incorporated-software.txt"),
    ("Roboto font (inside customtkinter)", "Apache License 2.0", "Roboto-LICENSE.txt"),
    ("Tcl/Tk (the GUI toolkit used by tkinter)", "Tcl/Tk License (BSD-style)", "TclTk-license.terms.txt"),
    ("HarfBuzz (inside uharfbuzz)", "Old MIT License", "HarfBuzz-COPYING.txt"),
    ("FreeType (inside freetype-py)", "FreeType License (FTL)", "FreeType-FTL.txt"),
]
RULE = "=" * 78


def pip_licenses() -> list:
    out = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--from=mixed", "--format=json",
         "--with-license-file", "--with-notice-file", "--no-license-path",
         "--packages", *BUNDLED],
        check=True, capture_output=True, encoding="utf-8",
    ).stdout
    return sorted(json.loads(out), key=lambda p: p["Name"].lower())


def section(title: str, licence: str, text: str, note: str = "") -> str:
    parts = [RULE, title, f"License: {licence}"]
    if note:
        parts.append(note)
    parts += ["", text.strip(), ""]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--ffmpeg-dir", help="folder with the bundled FFmpeg and its licence files")
    args = ap.parse_args()

    blocks = [
        "Anime Subs Customizer - third-party notices\n"
        "\n"
        "Anime Subs Customizer is MIT licensed (see LICENSE). It includes the\n"
        "components below, each under its own licence.\n"
    ]
    missing = []
    for pkg in pip_licenses():
        name = pkg["Name"]
        licence = LICENSE_FIXES.get(name, pkg.get("License") or "see text")
        text = pkg.get("LicenseText") or ""
        if pkg.get("NoticeText") and pkg["NoticeText"] != "UNKNOWN":
            text += "\n\nNOTICE:\n" + pkg["NoticeText"]
        if not text or text == "UNKNOWN":
            # Some wheels ship no licence file; the text is then kept here.
            fallback = LICENSES / f"{name}-LICENSE.txt"
            if not fallback.exists():
                missing.append(name)
                continue
            text = fallback.read_text(encoding="utf-8")
        blocks.append(section(f"{name} {pkg['Version']}", licence, text, LGPL_NOTE.get(name, "")))

    if missing:
        raise SystemExit(f"no licence text for {', '.join(missing)}; add packaging/licenses/<name>-LICENSE.txt")

    for title, licence, filename in NATIVE:
        blocks.append(section(title, licence, (LICENSES / filename).read_text(encoding="utf-8")))

    if args.ffmpeg_dir:
        ff = Path(args.ffmpeg_dir)
        info = ff / "BUILD-INFO.txt"
        texts = sorted(p for p in ff.glob("*") if p.is_file() and p.suffix.lower() in (".txt", ".md")
                       and p.name != "BUILD-INFO.txt")
        body = "\n\n".join(p.read_text(encoding="utf-8", errors="replace") for p in texts)
        note = ("Bundled in bin\\ as unmodified, separately replaceable DLLs. How it was built "
                "and where its exact source code is: FFMPEG-SOURCE.md (release asset) and "
                + ("BUILD-INFO.txt below." if info.exists() else "the release notes."))
        if info.exists():
            body += "\n\nBUILD-INFO.txt:\n" + info.read_text(encoding="utf-8")
        blocks.append(section("FFmpeg (subtitle extraction and preview frames)",
                              "LGPL (see the texts below)", body, note))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size // 1024} KiB, {len(blocks) - 1} components)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
