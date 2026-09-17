# -*- mode: python ; coding: utf-8 -*-
# 控制台构建(2026-09-14 设计文档 §3):服务/headless 场景推荐形态。
# 与 smartcar.spec 同 Analysis;唯一差异 console=True + 独立产物名。
from PyInstaller.utils.hooks import collect_data_files

ttkb_datas = collect_data_files('ttkbootstrap')

a = Analysis(
    ['tuning_tool.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('dashboard.html', '.'),
        ('web/dist', 'web_dist'),
        *ttkb_datas,
    ],
    hiddenimports=['ttkbootstrap', 'config_loader', 'asr_bridge', 'asr_vocab',
                   'camera_capture',
                   'guardrails', 'optimizer', 'score_engine'],
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
    name='YawTuningToolConsole',
    debug=False,
    strip=False,
    upx=True,
    console=True,
)
