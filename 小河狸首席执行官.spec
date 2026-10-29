# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = ['backend', 'backend.api_server']
hiddenimports += collect_submodules('backend')
# v1.0.4：延迟导入的第三方包（PyInstaller 静态分析抓不到）
hiddenimports += ['openpyxl', 'xlrd']
hiddenimports += collect_submodules('pystray')
hiddenimports += collect_submodules('webview')
hiddenimports += collect_submodules('pythonnet')


a = Analysis(
    ['D:\\python\\首席执行官\\desktop_launcher.py'],
    pathex=[],
    binaries=[],
    datas=[('D:\\python\\首席执行官\\index.html', '.'), ('D:\\python\\首席执行官\\assets', 'assets'), ('D:\\python\\首席执行官\\app.ico', '.')],
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
    [],
    exclude_binaries=True,
    name='小河狸首席执行官',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['D:\\python\\首席执行官\\app.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='小河狸首席执行官',
)
