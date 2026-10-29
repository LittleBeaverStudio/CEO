"""周报生成与企业微信推送（技术说明 §5 场景二）。

- 缓存：briefings 表按 ISO 周缓存（同周重复生成直接返回缓存，手动"重新生成"可强刷）。
- 推送：企业微信群机器人 webhook（settings.wecom_webhook_url），urllib 直发，
  不依赖用户机器上任何其他脚本——CEO 是独立 App。
- skill 端点 /api/skill/ceo-brief 从这里取最新周报文本。
"""
from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from backend import ai_layer


def current_week_key(now: datetime | None = None) -> str:
    moment = now or datetime.now()
    iso = moment.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def generate_briefing(db_path: str, force: bool = False, company_id: int = 1) -> dict[str, Any]:
    week_key = current_week_key()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not force:
            row = conn.execute(
                "SELECT content, mode, created_at FROM briefings WHERE company_id=? AND week_key=?"
                " ORDER BY id DESC LIMIT 1",
                (company_id, week_key),
            ).fetchone()
            if row:
                return {"week_key": week_key, "content": row["content"], "mode": row["mode"],
                        "created_at": row["created_at"], "cached": True}
    finally:
        conn.close()

    result = ai_layer.ai_briefing(db_path, company_id)
    content = result.get("content") or result.get("answer") or ""
    mode = result.get("mode", "rule")
    now_text = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO briefings (company_id, week_key, content, mode, created_at) VALUES (?,?,?,?,?)",
            (company_id, week_key, content, mode, now_text),
        )
        conn.commit()
    finally:
        conn.close()
    return {"week_key": week_key, "content": content, "mode": mode,
            "created_at": now_text, "cached": False,
            "note": result.get("note", "")}


def latest_briefing(db_path: str, company_id: int = 1) -> dict[str, Any] | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT week_key, content, mode, created_at FROM briefings WHERE company_id=?"
            " ORDER BY id DESC LIMIT 1", (company_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def push_briefing_to_wecom(db_path: str, content: str | None = None) -> dict[str, Any]:
    """推送到企业微信群机器人 webhook（settings 表配置）。"""
    text = content
    if not text:
        latest = latest_briefing(db_path)
        if not latest:
            return {"success": False, "error": "还没有周报，请先生成"}
        text = latest["content"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='wecom'").fetchone()
    finally:
        conn.close()
    webhook = ""
    if row:
        try:
            webhook = (json.loads(row[0]) or {}).get("webhook_url", "").strip()
        except ValueError:
            webhook = ""
    if not webhook or not webhook.startswith("http"):
        return {"success": False, "error": "未配置企业微信群机器人 Webhook，请到「设置」填写"}
    body = json.dumps({"msgtype": "text", "text": {"content": text}}).encode("utf-8")
    req = urllib.request.Request(webhook, data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        if data.get("errcode") == 0:
            return {"success": True}
        return {"success": False, "error": f"企业微信返回：{data}"}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return {"success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# 周报定时任务（关窗后服务存活时照常跑，§8）
# ---------------------------------------------------------------------------

_weekly_state = {"last_run_week": ""}


def weekly_briefing_loop(db_path: str) -> None:
    """每小时自检一次：进入新的一周且为周一上午 8-11 点窗口时自动生成周报。"""
    import threading
    import time

    def runner() -> None:
        while True:
            try:
                now = datetime.now()
                week_key = current_week_key(now)
                if (now.weekday() == 0 and 8 <= now.hour < 11
                        and _weekly_state["last_run_week"] != week_key):
                    result = generate_briefing(db_path)
                    if not result.get("cached"):
                        _weekly_state["last_run_week"] = week_key
            except Exception:
                pass
            time.sleep(1800)

    threading.Thread(target=runner, daemon=True).start()
