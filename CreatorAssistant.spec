# -*- mode: python ; coding: utf-8 -*-
"""One-folder CreatorAssistant build.

The bootloader exe is CreatorAssistant-core.exe. _internal must stay beside it.
Users start packaging/launcher.c, published as CreatorAssistant.exe.
"""
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

datas = [('config.example.yaml', '.')]
binaries = []
hiddenimports = ['soundfile']

for package in ('cv2', 'librosa'):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

try:
    binaries += collect_dynamic_libs('soundfile')
except Exception:
    pass

a = Analysis(
    ['run_main_gui.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['torch', 'tensorflow', 'transformers'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CreatorAssistant-core',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='CreatorAssistant-core',
)
