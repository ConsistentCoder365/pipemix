# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for PipeMix on macOS — a one-dir build wrapped in
PipeMix.app. Invoked by build_mac.py, which builds the .icns first.

One executable serves both uses: double-clicking the app opens the window,
and `PipeMix.app/Contents/MacOS/PipeMix --list` (or --cli, --share) runs in
a terminal. macOS has no console/windowed subsystem split to work around,
unlike pipemix.spec's two Windows exes.

Data paths. frontend/dist ships under Contents/Resources, which is
`sys._MEIPASS` in a BUNDLE build — the first place macos/app.py's `_roots()`
looks.

No pyobjc is needed by PipeMix itself (Core Audio is reached through ctypes),
but pywebview's Cocoa backend pulls it in, and PyInstaller's pywebview hook
collects it.
"""

from pathlib import Path

project_root = Path(SPECPATH)
src_dir = project_root / "src"
frontend_dist = project_root / "frontend" / "dist"
icon_file = project_root / "build" / "pipemix.icns"

import tomllib
with open(project_root / "pyproject.toml", "rb") as f:
    version = tomllib.load(f)["project"]["version"]

a = Analysis(
    [str(src_dir / "pipemix" / "macos" / "main.py")],
    pathex=[str(src_dir)],
    binaries=[],
    datas=[(str(frontend_dist), "frontend/dist")],
    hiddenimports=["webview.platforms.cocoa"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The Windows and Linux trees are never imported on macOS; keep their
    # heavy dependencies from being chased.
    excludes=["comtypes", "pycaw", "gi", "tkinter"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PipeMix",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PipeMix")

app = BUNDLE(
    coll,
    name="PipeMix.app",
    icon=str(icon_file),
    bundle_identifier="com.pipemix.app",
    version=version,
    info_plist={
        "CFBundleName": "PipeMix",
        "CFBundleDisplayName": "PipeMix",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.music",
    },
)
