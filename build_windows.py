"""首席执行官 —— Windows 打包脚本（照小河狸系列既有形态）。

用法：
    python build_windows.py            # 生成 dist/首席执行官/单目录版
    python build_windows.py --onedir   # 同上（显式）
    python build_windows.py --installer# 追加调用 Inno Setup（installer.iss）生成安装包

打包内容：desktop_launcher.py（入口）+ backend/* + index.html + assets/ + app.ico
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_NAME = "首席执行官"
ENTRY = "desktop_launcher.py"


def build(onedir: bool = True) -> int:
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", APP_NAME,
        "--icon", str(ROOT / "app.ico"),
        "--add-data", f"{ROOT / 'index.html'};.",
        "--add-data", f"{ROOT / 'assets'};assets",
        "--add-data", f"{ROOT / 'app.ico'};.",
        "--hidden-import", "backend",
        "--hidden-import", "backend.api_server",
        "--collect-submodules", "backend",
        # v1.0.4：openpyxl / xlrd 在 bank_parsers 里是"函数内延迟导入"，PyInstaller
        # 静态分析看不到，打包版会缺包导致导入 xlsx/xls 报 ImportError，必须显式声明。
        "--hidden-import", "openpyxl",
        "--hidden-import", "xlrd",
        "--collect-submodules", "pystray",
        # v1.0.6：原生窗口（WebView2）。pywebview 靠 pythonnet(clr) 调 WinForms，
        # 缺 DLL 会静默回退 Edge，所以这里必须收全。
        "--collect-all", "webview",
        "--collect-all", "pythonnet",
        "--collect-all", "clr_loader",
        # v1.0.4：窗口化打包，双击不再弹控制台黑窗
        "--noconsole",
    ]
    if onedir:
        cmd.append("--onedir")
    else:
        cmd.append("--onefile")
    cmd.append(str(ROOT / ENTRY))
    print(" ".join(cmd))
    return subprocess.call(cmd, cwd=str(ROOT))


def build_installer() -> int:
    iscc_candidates = [Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
                       Path(r"C:\Program Files\Inno Setup 6\ISCC.exe")]
    iscc = next((p for p in iscc_candidates if p.is_file()), None)
    if iscc is None:
        print("未找到 Inno Setup 6（ISCC.exe），跳过安装包生成")
        return 1
    return subprocess.call([str(iscc), str(ROOT / "installer.iss")], cwd=str(ROOT))


if __name__ == "__main__":
    args = sys.argv[1:]
    code = build(onedir="--onefile" not in args)
    if code == 0 and "--installer" in args:
        code = build_installer()
    sys.exit(code)
