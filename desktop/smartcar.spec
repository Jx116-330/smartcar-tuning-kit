# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for SmartCar Tuning Tool.

Build:  cd desktop && pyinstaller --clean smartcar.spec
Output: desktop/dist/SmartCarTuningTool.exe
"""
import sys
from PyInstaller.utils.hooks import collect_data_files

# ttkbootstrap ships theme assets that PyInstaller can miss
ttkb_datas = collect_data_files('ttkbootstrap')

a = Analysis(
    ['tuning_tool.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('dashboard.html', '.'),   # bundled inside exe, accessed via resource_path()
        *ttkb_datas,
    ],
    hiddenimports=['ttkbootstrap', 'config_loader'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'scipy', 'pandas'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    a.zipfiles,
    name='SmartCarTuningTool',
    debug=False,
    strip=False,
    upx=True,
    console=False,          # windowed app, no console
    # icon='icon.ico',      # uncomment and add icon file if desired
)
