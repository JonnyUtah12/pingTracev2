# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for PingTrace - onedir, flat contents (ARCHITECTURE.md section 5).

Build (from any cwd):   pyinstaller packaging/pingtrace.spec
Output:                dist/pingtrace/   (packaging/build_portable.sh|.ps1 then
                         assembles the final portable folder)

Why this shape:
- onedir, never onefile: onefile unpacks to a temp dir on every launch, which
  breaks "all state stays inside the portable folder" and slows startup.
- contents_directory='.' (flat onedir): static/ and the runtime support files
  sit NEXT TO the executable, so config.app_root() (the exe's directory) finds
  static/ and writes metrics.db / .pingtrace.pid / targets.json right there.
  The frozen app needs zero code changes.
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

a = Analysis(
    [os.path.join(ROOT, 'main.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, 'static'), 'static')],
    hiddenimports=[
        # uvicorn resolves these from config strings at runtime; hook-uvicorn
        # collects them, but the ones this app actually uses are pinned here.
        'uvicorn.logging',
        'uvicorn.loops.asyncio',
        'uvicorn.protocols.http.h11_impl',
    ],
    hookconfigfiles=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest', 'matplotlib'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='pingtrace',
    debug=False,
    bootloader_ignore_signals=False,
    strip=sys.platform != 'win32',  # strip only where binutils strip exists (Linux/macOS); Windows skips it
    upx=False,
    console=True,  # run.bat starts it minimized; logs stay visible for debugging
    contents_directory='.',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=sys.platform != 'win32',  # strip only where binutils strip exists (Linux/macOS); Windows skips it
    upx=False,
    name='pingtrace',
    contents_directory='.',
)
