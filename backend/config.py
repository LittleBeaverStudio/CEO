"""首席执行官 —— 路径与环境配置。

铁律（技术说明 §2）：
- 数据目录由环境变量 CEO_APP_HOME 决定；未设置时明确报错，绝不静默写错目录。
  （desktop_launcher.py 启动前会用 默认 %LOCALAPPDATA%\\首席执行官 setdefault，
   因此用户双击启动不受影响；直接裸跑 backend 时若忘记设置会得到清晰报错。）
- v1.0.2 起显示名去掉「小河狸」字样；老版本数据目录（%LOCALAPPDATA%\\小河狸首席执行官）
  首次启动时整体迁移到新目录，数据不丢。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_DISPLAY_NAME = "首席执行官"
APP_INTERNAL_NAME = "littlebeaver_ceo"
SERVICE_NAME = "ceo-assistant"
APP_VERSION = "1.0.9"

# v1.0.2 改名前的旧数据目录名（迁移源）
LEGACY_APP_DISPLAY_NAME = "小河狸首席执行官"

DEFAULT_PORT = 8878
PORT_SCAN_ATTEMPTS = 20

ENV_HOME = "CEO_APP_HOME"
ENV_PORT = "CEO_API_PORT"


class AppHomeNotSetError(RuntimeError):
    """CEO_APP_HOME 未设置且无法安全推断时抛出（技术说明 v1.2 验收项）。"""


def default_app_home() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return local_app_data / APP_DISPLAY_NAME


def migrate_legacy_home_if_needed(new_home: Path) -> Path:
    """v1.0.2 改名迁移：新目录还没有库而旧目录（小河狸首席执行官）有时，整体搬过来。

    只在新目录不含 ceo_assistant.db 时执行；任何异常都放弃迁移、继续用新目录，
    绝不让迁移失败阻断启动。
    """
    if (new_home / "ceo_assistant.db").exists():
        return new_home
    legacy = new_home.parent / LEGACY_APP_DISPLAY_NAME
    if not legacy.is_dir() or not (legacy / "ceo_assistant.db").exists():
        return new_home
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
        # 迁移成功后旧目录改名保留，不直接删（用户可自行清理）
        legacy.rename(new_home.parent / (LEGACY_APP_DISPLAY_NAME + "-已迁移备份"))
    except OSError:
        pass
    return new_home


def get_app_home() -> Path:
    """数据目录：CEO_APP_HOME 优先；未设置时用默认目录但在裸跑 backend 时报错。

    判定方式：desktop_launcher / 打包入口都会先把 CEO_APP_HOME setdefault 进环境，
    因此"未设置"只发生在绕过启动器直接 import backend 的场景——此时按验收要求
    明确报错，而不是静默写到错误的当前目录。
    """
    env_home = os.environ.get(ENV_HOME)
    if env_home:
        home = Path(env_home)
    elif os.environ.get("CEO_ALLOW_DEFAULT_HOME") == "1" or os.environ.get(ENV_PORT):
        # 由启动器/测试显式放行（已设置端口说明经启动器启动）
        home = migrate_legacy_home_if_needed(default_app_home())
    else:
        raise AppHomeNotSetError(
            f"环境变量 {ENV_HOME} 未设置。"
            f"请通过 desktop_launcher.py 启动，或显式设置 {ENV_HOME}="
            f"（默认建议 {default_app_home()}）后再运行 backend。"
        )
    home.mkdir(parents=True, exist_ok=True)
    return home


def get_db_path() -> Path:
    return get_app_home() / "ceo_assistant.db"


def get_sources_json_path() -> Path:
    """AI 接入配置（provider/base_url/model/api_key）。本地文件、不进项目目录。"""
    return get_app_home() / "sources.json"


def bootstrap_app_data() -> None:
    home = get_app_home()
    for sub in ("data", "logs"):
        (home / sub).mkdir(parents=True, exist_ok=True)


def get_log_path() -> Path:
    return get_app_home() / "logs" / "ceo_app.log"
