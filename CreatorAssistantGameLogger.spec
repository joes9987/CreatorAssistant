# -*- mode: python ; coding: utf-8 -*-
"""One-file game logger so it can sit beside the main app without its own _internal."""

a = Analysis(
    ['run_logger_gui.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['obsws_python'],
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
    a.binaries,
    a.datas,
    [],
    name='CreatorAssistantGameLogger',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
