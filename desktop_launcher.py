"""首席执行官 —— 桌面启动器。

模式照资金助手/财报助手（pystray 托盘 + 心跳看门狗 + Edge --app 窗口）：
- 服务常驻：关窗后服务存活，周报定时任务照常跑（技术说明 §8）
- 托盘常驻可再次唤起；托盘菜单"退出程序"才真正结束
- 启动前清理上次残留的本程序服务（只认 /api/health 的 service 标识，不误伤其他程序）
- 端口默认 8878，与发票(8876)/财报(8765)/资金(8865)/合同(8870)/CFO(8860) 六开无冲突
- CEO_APP_HOME 未设置时这里 setdefault（backend 裸跑才会明确报错，v1.2 验收项）
- v1.0.2：显示名去掉「小河狸」；首次启动自动迁移旧数据目录（小河狸首席执行官 → 首席执行官）
- v1.0.4：打包为窗口化程序（无控制台黑窗）；windowed 下 stdout/stderr 重定向到
  %LOCALAPPDATA%\\首席执行官\\logs\\launcher.log，子进程一律 CREATE_NO_WINDOW
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from werkzeug.serving import make_server


APP_DISPLAY_NAME = "首席执行官"
LEGACY_APP_DISPLAY_NAME = "小河狸首席执行官"
# 与 backend/config.py 的 SERVICE_NAME 保持一致
SERVICE_NAME = "ceo-assistant"
SERVER_START_TIMEOUT_SECONDS = 15.0
DEFAULT_PORT = 8878
PORT_SCAN_ATTEMPTS = 20


def get_bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def migrate_legacy_home_if_needed(new_home: Path) -> Path:
    """v1.0.2 改名迁移：旧目录（小河狸首席执行官）→ 新目录（首席执行官）。失败不阻断启动。"""
    if (new_home / "ceo_assistant.db").exists():
        return new_home
    legacy = new_home.parent / LEGACY_APP_DISPLAY_NAME
    if not legacy.is_dir() or not (legacy / "ceo_assistant.db").exists():
        return new_home
    import shutil
    try:
        new_home.mkdir(parents=True, exist_ok=True)
        for item in legacy.iterdir():
            target = new_home / item.name
            if target.exists():
                continue
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
        legacy.rename(new_home.parent / (LEGACY_APP_DISPLAY_NAME + "-已迁移备份"))
        # rename 失败（旧窗口占着 browser-profile 等文件锁）无碍：数据已复制到新目录，
        # 旧目录保留原样即可；下次启动发现新目录已有库，不会重复迁移。
    except OSError:
        pass
    return new_home


def get_app_home() -> Path:
    """数据目录：CEO_APP_HOME > %LOCALAPPDATA%\\首席执行官（v1.0.2 起去掉「小河狸」前缀）。

    刻意不复用 INVOICE/FUND_APP_HOME——CEO 独立于其他助手（§0 脱节铁律）。
    """
    env_home = os.environ.get("CEO_APP_HOME")
    if env_home:
        app_home = Path(env_home)
    else:
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        app_home = migrate_legacy_home_if_needed(local_app_data / APP_DISPLAY_NAME)
    app_home.mkdir(parents=True, exist_ok=True)
    return app_home


def bootstrap_app_data() -> None:
    app_home = get_app_home()
    for sub in ("data", "logs"):
        (app_home / sub).mkdir(parents=True, exist_ok=True)


def find_free_port(start_port: int = DEFAULT_PORT, max_attempts: int = PORT_SCAN_ATTEMPTS) -> int:
    for port in range(start_port, start_port + max_attempts):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    return start_port


def _probe_service(base: str) -> dict | None:
    try:
        with urlopen(f"{base}/api/health", timeout=0.35) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (URLError, TimeoutError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def find_running_service_url(start_port: int = DEFAULT_PORT, max_attempts: int = PORT_SCAN_ATTEMPTS) -> str | None:
    """本程序已在运行（常驻托盘）时返回其地址，避免重复启动。"""
    for port in range(start_port, start_port + max_attempts):
        base = f"http://127.0.0.1:{port}"
        payload = _probe_service(base)
        if payload and payload.get("service") == SERVICE_NAME:
            return f"{base}/"
    return None


def shutdown_existing_service(start_port: int = DEFAULT_PORT, max_attempts: int = PORT_SCAN_ATTEMPTS) -> bool:
    """清理上次残留的本程序服务。只关 service 标识匹配的实例，不误伤同端口其他程序。"""
    stopped = False
    for port in range(start_port, start_port + max_attempts):
        base = f"http://127.0.0.1:{port}"
        payload = _probe_service(base)
        if not payload or payload.get("service") != SERVICE_NAME:
            continue
        try:
            req = Request(f"{base}/api/shutdown", method="POST")
            urlopen(req, timeout=0.8).close()
            stopped = True
        except (URLError, TimeoutError, OSError):
            continue
    if stopped:
        time.sleep(0.6)
    return stopped


def find_app_browser() -> Path | None:
    """查找支持 --app 独立窗口的浏览器，优先 Edge（Windows 自带）。"""
    roots = [
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    candidates: list[Path] = []
    for root in roots:
        if root:
            candidates.append(Path(root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for root in roots:
        if root:
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for path in candidates:
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def open_app_window(url: str) -> None:
    if os.environ.get("CEO_NO_BROWSER") == "1":
        # 排障/自动化测试用：只起服务不开窗
        return
    browser = find_app_browser()
    if browser is None:
        webbrowser.open(url)
        return
    profile_dir = get_app_home() / "browser-profile"
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        webbrowser.open(url)
        return
    args = [
        str(browser),
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--start-maximized",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    try:
        subprocess.Popen(args, close_fds=True, **_no_console_kwargs())
    except OSError:
        webbrowser.open(url)


# v1.0.4：窗口化程序拉起子进程时不应闪出控制台窗口
def _no_console_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


def ensure_std_streams() -> None:
    """窗口化（--noconsole）打包后 sys.stdout / sys.stderr 是 None。

    任何库一旦 print 或写 stderr（werkzeug 请求日志、third-party warning）就会抛
    AttributeError 把服务带崩。这里统一接到日志文件，既避免崩溃，也留下排障线索。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    stream = None
    try:
        log_dir = get_app_home() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "launcher.log", "a", encoding="utf-8", errors="replace")
    except OSError:
        stream = None
    if stream is None:
        try:
            stream = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def get_tray_icon_image():
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    for name in ("ico.ico", "app.ico"):
        icon_path = get_bundle_dir() / name
        try:
            if icon_path.is_file():
                return Image.open(icon_path)
        except Exception:
            pass
    try:
        # 兜底：画新 logo（金色皇冠）的简化版，避免托盘空白
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 60, 60], radius=13, fill=(10, 23, 20, 255),
                            outline=(53, 199, 165, 255), width=2)
        gold, gold_d, gem = (240, 184, 79, 255), (223, 170, 63, 255), (53, 199, 165, 255)
        d.polygon([(20, 42), (20, 22), (27, 35), (32, 17), (37, 35), (44, 22), (44, 42)], fill=gold)
        for cx, cy, r in [(20, 20, 3), (32, 15, 3), (44, 20, 3)]:
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=gold)
        d.rounded_rectangle([18, 44, 46, 50], radius=2, fill=gold_d)
        d.polygon([(32, 42), (35, 45), (32, 48), (29, 45)], fill=gem)
        return img
    except Exception:
        return None


def start_tray_icon(on_open, on_quit):
    global _tray_ok
    try:
        import pystray
    except ImportError:
        return None
    image = get_tray_icon_image()
    if image is None:
        return None
    try:
        icon = pystray.Icon(
            "littlebeaver_ceo",
            image,
            APP_DISPLAY_NAME,
            pystray.Menu(
                pystray.MenuItem("打开主界面", lambda *_: on_open(), default=True),
                pystray.MenuItem("退出程序", lambda *_: on_quit()),
            ),
        )
        threading.Thread(target=icon.run, daemon=True).start()
        _tray_ok = True
        return icon
    except Exception:
        return None


bootstrap_app_data()
os.environ.setdefault("CEO_APP_HOME", str(get_app_home()))
ensure_std_streams()  # v1.0.4：窗口化运行先把 stdout/stderr 接上，避免 None 崩溃


# ---------------------------------------------------------------------------
# v1.0.6 原生窗口（WebView2）：任务栏图标用本程序 exe 的 app.ico
# Edge --app 的窗口进程是 msedge.exe，任务栏永远显示 Edge 图标，改不了；
# 换成 WebView2 原生窗口后，窗口类图标 = 本 exe 图标，四处才真正统一。
# 任何一步失败都回退 Edge --app，绝不因为换壳打不开软件。
# ---------------------------------------------------------------------------

_webview_window = None
_tray_ok = False


def _set_window_icon_native() -> bool:
    """把原生窗口的图标换成 app.ico（pywebview 默认用 python.exe 的图标）。

    不设这一步，窗口是原生了，但图标还是 Python 的，用户照样觉得"没改"。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        icon_path = str(get_bundle_dir() / "app.ico")
        if not Path(icon_path).is_file():
            return False
        user32 = ctypes.windll.user32
        LR_LOADFROMFILE, IMAGE_ICON, WM_SETICON = 0x0010, 1, 0x0080
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT]
        big = user32.LoadImageW(None, icon_path, IMAGE_ICON, 64, 64, LR_LOADFROMFILE)
        small = user32.LoadImageW(None, icon_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if not big and not small:
            return False
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

        pid = os.getpid()
        targets: list[int] = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value != pid:
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value == APP_DISPLAY_NAME:
                targets.append(hwnd)
            return True

        user32.EnumWindows(enum_proc(_cb), 0)
        for hwnd in targets:
            if big:
                user32.SendMessageW(hwnd, WM_SETICON, 1, big)   # ICON_BIG
            if small:
                user32.SendMessageW(hwnd, WM_SETICON, 0, small)  # ICON_SMALL
        return bool(targets)
    except Exception:
        return False


def _keep_window_icon(attempts: int = 40, interval: float = 0.25) -> None:
    for _ in range(attempts):
        if _set_window_icon_native():
            return
        time.sleep(interval)


def _webview_show() -> bool:
    """托盘「打开主界面」：优先唤起已经隐藏的原生窗口。"""
    win = _webview_window
    if win is None:
        return False
    try:
        win.show()
        win.restore()
        return True
    except Exception:
        return False


def open_ui(url: str) -> None:
    if _webview_show():
        return
    open_app_window(url)


def run_webview_shell(url: str) -> bool:
    """在主线程跑 WebView2 窗口（阻塞）。返回 False 表示不可用，调用方回退 Edge。"""
    global _webview_window
    try:
        import webview  # pywebview
    except Exception:
        return False
    try:
        window = webview.create_window(
            APP_DISPLAY_NAME,
            url,
            width=1440,
            height=900,
            min_size=(1024, 680),
            background_color="#0a1714",
            maximized=True,
            text_select=True,
        )

        def _on_closing():
            # 关窗 = 收起窗口，服务与周报任务继续跑（与原托盘模式一致）。
            # 但如果托盘没起来（缺包/被拦截），藏起来就再也叫不出来了 —— 这种情况直接退出。
            if not _tray_ok:
                try:
                    window.destroy()
                except Exception:
                    pass
                os._exit(0)
            try:
                window.hide()
            except Exception:
                pass
            return False

        window.events.closing += _on_closing
        _webview_window = window
        # 窗口出来后把图标换成 app.ico（后台轮询，拿到 hwnd 就设）
        threading.Thread(target=_keep_window_icon, daemon=True).start()
        profile_dir = get_app_home() / "webview-profile"
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            profile_dir = None
        webview.start(
            private_mode=False,
            storage_path=str(profile_dir) if profile_dir else None,
            debug=False,
        )
        return True
    except Exception:
        import traceback
        try:
            traceback.print_exc()
        except Exception:
            pass
        _webview_window = None
        return False


def select_port_and_prepare() -> None:
    """运行期准备（放 main 里而非模块级，import 本模块不再有弹窗/退出等副作用）：
    已在运行（常驻托盘）时，再点图标只唤起窗口，不重启服务；否则清理残留、选定端口。"""
    preferred_port = int(os.environ.get("CEO_API_PORT", str(DEFAULT_PORT)))
    running_url = find_running_service_url(preferred_port)
    if running_url:
        open_ui(running_url)
        raise SystemExit(0)
    shutdown_existing_service(preferred_port)
    os.environ["CEO_API_PORT"] = str(find_free_port(preferred_port))


def main() -> None:
    select_port_and_prepare()
    from backend.api_server import SERVER_PORT, app, init_db, start_heartbeat_watchdog  # noqa: E402

    init_db()

    class ServerThread(threading.Thread):
        def __init__(self) -> None:
            super().__init__(daemon=False)
            self._server = make_server("0.0.0.0", SERVER_PORT, app, threaded=True)

        def run(self) -> None:
            self._server.serve_forever()

        def shutdown(self) -> None:
            self._server.shutdown()

    class HeadlessLauncher:
        def __init__(self) -> None:
            self.server = ServerThread()
            self.local_url = f"http://127.0.0.1:{SERVER_PORT}/"
            self._shutdown_lock = threading.Lock()
            self._stopped = False
            self.tray_icon = None

        def start(self) -> None:
            self.server.start()
            self.wait_until_ready()
            # v1.0.6：窗口由 main 决定壳（WebView2 优先，失败回退 Edge），这里只挂托盘
            self.tray_icon = start_tray_icon(
                on_open=lambda: open_ui(self.local_url),
                on_quit=self.quit_from_tray,
            )

        def quit_from_tray(self) -> None:
            """从托盘退出：移除图标并结束进程。"""
            if self.tray_icon is not None:
                try:
                    self.tray_icon.stop()
                except Exception:
                    pass
            self.shutdown()
            os._exit(0)

        def wait_until_ready(self) -> bool:
            deadline = time.time() + SERVER_START_TIMEOUT_SECONDS
            health_url = f"{self.local_url.rstrip('/')}/api/health"
            while time.time() < deadline:
                try:
                    with urlopen(health_url, timeout=0.6):
                        return True
                except (URLError, TimeoutError, OSError):
                    time.sleep(0.25)
            return False

        def shutdown(self) -> None:
            with self._shutdown_lock:
                if self._stopped:
                    return
                self._stopped = True
                try:
                    self.server.shutdown()
                except Exception:
                    pass

        def wait_forever(self) -> None:
            try:
                self.server.join()
            finally:
                self.shutdown()

    launcher = HeadlessLauncher()
    atexit.register(launcher.shutdown)
    start_heartbeat_watchdog()

    from backend.briefing import weekly_briefing_loop  # noqa: E402
    weekly_briefing_loop(str(get_app_home() / "ceo_assistant.db"))

    def handle_exit(*_args: object) -> None:
        launcher.shutdown()
        raise SystemExit(0)

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, handle_exit)
            except (ValueError, OSError):
                pass

    launcher.start()
    if os.environ.get("CEO_NO_BROWSER") == "1":
        launcher.wait_forever()
        return
    if not run_webview_shell(launcher.local_url):
        # WebView2 不可用（缺运行时/打包缺 DLL）→ 退回原来的 Edge --app 窗口
        open_app_window(launcher.local_url)
    launcher.wait_forever()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # 窗口化后没有控制台，异常必须落到日志，否则用户只看到"点了没反应"
        import traceback
        try:
            traceback.print_exc()
            sys.stderr.flush()
        except Exception:
            pass
        raise
