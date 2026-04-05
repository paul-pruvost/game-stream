# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all, collect_submodules

# Collect binary packages that PyInstaller misses automatically
datas_extra    = []
binaries_extra = []
hiddenimports  = []

for pkg in [
    "numpy", "cv2", "mss", "pygame", "av",
    "sounddevice", "aiohttp", "cryptography",
    "pynput", "zeroconf", "pyperclip",
]:
    d, b, h = collect_all(pkg)
    datas_extra    += d
    binaries_extra += b
    hiddenimports  += h

hiddenimports += collect_submodules("asyncio")
hiddenimports += collect_submodules("email")
hiddenimports += collect_submodules("encodings")
hiddenimports += collect_submodules("logging")

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=binaries_extra,
    datas=[
        ('shared',   'shared'),
        ('host',     'host'),
        ('client',   'client'),
        ('mobile',   'mobile'),
        ('relay.py', '.'),
        ('launch.py', '.'),
    ] + datas_extra,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='GameStream',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
