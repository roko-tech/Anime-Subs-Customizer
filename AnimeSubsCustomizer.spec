# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Windows build: one folder, windowed, no UPX.

    python -m PyInstaller --noconfirm --clean AnimeSubsCustomizer.spec

Output: dist/AnimeSubsCustomizer/AnimeSubsCustomizer.exe (+ _internal/). The
version resource comes from APP_VERSION in anime_subs/runtime.py, the single
source of the version number. See docs/adr/ADR-001-packaging.md.
"""
import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
    VarStruct, VSVersionInfo,
)

ROOT = Path(SPECPATH)
VERSION = re.search(
    r'^APP_VERSION = "(\d+\.\d+\.\d+)"',
    (ROOT / "anime_subs" / "runtime.py").read_text(encoding="utf-8"),
    re.MULTILINE,
).group(1)
NUMS = tuple(int(n) for n in VERSION.split(".")) + (0,)

version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=NUMS, prodvers=NUMS, mask=0x3F, flags=0x0,
                      OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "roko-tech"),
            StringStruct("FileDescription", "Anime Subs Customizer"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "AnimeSubsCustomizer"),
            StringStruct("LegalCopyright", "MIT License"),
            StringStruct("OriginalFilename", "AnimeSubsCustomizer.exe"),
            StringStruct("ProductName", "Anime Subs Customizer"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

a = Analysis(
    ["anime_subs_customizer.pyw"],
    pathex=[str(ROOT)],
    datas=[("icon.ico", ".")],
    # Loaded by name at run time, so static analysis can't see them.
    hiddenimports=["pystray._win32", "watchdog.observers.read_directory_changes"],
    excludes=["pytest", "hypothesis", "_pytest"],
    # LGPL libraries stay as plain .py files so a user can swap in their own
    # build of them (LGPL relinking); python-bidi's .pyd is already a separate file.
    module_collection_mode={"pystray": "py", "chardet": "py"},
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AnimeSubsCustomizer",
    console=False,
    upx=False,
    icon=str(ROOT / "icon.ico"),
    version=version_info,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AnimeSubsCustomizer",
)
