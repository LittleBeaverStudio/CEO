"""首席执行官 —— Flask API 服务（默认端口 8878）。

端点分组：
- 页面/静态：/ （单文件 index.html）+ /assets/*
- 基础：/api/health、/api/heartbeat、/api/shutdown
- 导入：/api/import/bank、/api/import/invoice、/api/import/status、/api/import/clear
- 看板：/api/dashboard、/api/runway/what-if
- 配置：/api/config/fixed-expenses、/api/config/expense-overrides、
        /api/config/engine（回收率+安全线）、/api/config/ai、/api/config/wecom
- AI：/api/ai/chat、/api/ai/briefing、/api/ai/attribute、/api/ai/scenario
- 周报：/api/briefing/generate、/api/briefing/push
- skill 只读（守卫照发票助手 1.6.0）：/api/skill/health|info|companies|ceo-brief
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from flask import Flask, jsonify, make_response, request, send_from_directory

from backend import ai_layer, briefing, engine, importer
from backend.config import (
    APP_VERSION,
    SERVICE_NAME,
    bootstrap_app_data,
    get_app_home,
    get_db_path,
    get_log_path,
)

# 启动日志
logger = logging.getLogger("ceo_app")
if not logger.handlers:
    try:
        get_log_path().parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            handlers=[logging.FileHandler(get_log_path(), encoding="utf-8")],
        )
    except OSError:
        logging.basicConfig(level=logging.INFO)

SERVER_PORT = int(__import__("os").environ.get("CEO_API_PORT", "8878"))

bootstrap_app_data()
DB_PATH = str(get_db_path())

app = Flask(__name__, static_folder=None)
BUNDLE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 数据库初始化
# ---------------------------------------------------------------------------

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                tax_no TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS bank_txns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER DEFAULT 1,
                account_no TEXT, account_name TEXT,
                txn_date TEXT, txn_time TEXT,
                direction TEXT, amount REAL, balance_after REAL,
                counterparty TEXT, summary TEXT, txn_ref TEXT,
                unique_key TEXT,
                dup_flag TEXT DEFAULT '',
                imported_at TEXT,
                batch_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS invoice_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER DEFAULT 1,
                kind TEXT, inv_date TEXT, counterparty TEXT,
                amount REAL, tax REAL, project TEXT DEFAULT '',
                inv_no TEXT DEFAULT '',
                unique_key TEXT,
                imported_at TEXT,
                batch_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS import_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER DEFAULT 1,
                file_name TEXT, kind TEXT,
                raw_rows INTEGER DEFAULT 0,
                inserted INTEGER DEFAULT 0,
                duplicated INTEGER DEFAULT 0,
                updated INTEGER DEFAULT 0,
                suspicious INTEGER DEFAULT 0,
                warnings_json TEXT DEFAULT '[]',
                imported_at TEXT
            );
            CREATE TABLE IF NOT EXISTS fixed_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER DEFAULT 1,
                name TEXT, monthly_amount REAL, note TEXT DEFAULT '',
                day_of_month INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS expense_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER DEFAULT 1,
                month TEXT, amount REAL, note TEXT DEFAULT '',
                expense_date TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            );
            CREATE TABLE IF NOT EXISTS counterparty_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER DEFAULT 1,
                alias TEXT NOT NULL,
                canonical TEXT NOT NULL,
                created_at TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS ai_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS briefings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER DEFAULT 1,
                week_key TEXT, content TEXT, mode TEXT, created_at TEXT
            );
            """
        )
        # 公司管理（照发票助手）：至少保留一个公司；默认公司 id=1
        if not conn.execute("SELECT 1 FROM companies LIMIT 1").fetchone():
            conn.execute(
                "INSERT INTO companies (name, note, created_at) VALUES (?, '', ?)",
                ("默认公司", datetime.now().isoformat(timespec="seconds")),
            )
        # 旧库迁移：累积存储/多公司/税号新增列（已存在则跳过）
        for table, col, ddl in [
            ("companies", "tax_no", "ALTER TABLE companies ADD COLUMN tax_no TEXT DEFAULT ''"),
            ("bank_txns", "dup_flag", "ALTER TABLE bank_txns ADD COLUMN dup_flag TEXT DEFAULT ''"),
            ("bank_txns", "imported_at", "ALTER TABLE bank_txns ADD COLUMN imported_at TEXT"),
            ("bank_txns", "batch_id", "ALTER TABLE bank_txns ADD COLUMN batch_id INTEGER"),
            ("invoice_rows", "imported_at", "ALTER TABLE invoice_rows ADD COLUMN imported_at TEXT"),
            ("invoice_rows", "batch_id", "ALTER TABLE invoice_rows ADD COLUMN batch_id INTEGER"),
            ("invoice_rows", "inv_no", "ALTER TABLE invoice_rows ADD COLUMN inv_no TEXT DEFAULT ''"),
            ("bank_txns", "company_id", "ALTER TABLE bank_txns ADD COLUMN company_id INTEGER DEFAULT 1"),
            ("invoice_rows", "company_id", "ALTER TABLE invoice_rows ADD COLUMN company_id INTEGER DEFAULT 1"),
            ("import_batches", "company_id", "ALTER TABLE import_batches ADD COLUMN company_id INTEGER DEFAULT 1"),
            ("fixed_expenses", "company_id", "ALTER TABLE fixed_expenses ADD COLUMN company_id INTEGER DEFAULT 1"),
            ("fixed_expenses", "day_of_month", "ALTER TABLE fixed_expenses ADD COLUMN day_of_month INTEGER DEFAULT 0"),
            ("expense_overrides", "company_id", "ALTER TABLE expense_overrides ADD COLUMN company_id INTEGER DEFAULT 1"),
            ("expense_overrides", "expense_date", "ALTER TABLE expense_overrides ADD COLUMN expense_date TEXT DEFAULT ''"),
            ("briefings", "company_id", "ALTER TABLE briefings ADD COLUMN company_id INTEGER DEFAULT 1"),
            ("counterparty_aliases", "company_id", "ALTER TABLE counterparty_aliases ADD COLUMN company_id INTEGER DEFAULT 1"),
        ]:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if cols and col not in cols:
                conn.execute(ddl)
        # 唯一索引（旧库若有重复数据则不建，去重仍由应用层保证）
        for ddl in [
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_bank_txns_unique ON bank_txns(company_id, unique_key)",
            "CREATE INDEX IF NOT EXISTS idx_bank_txns_date ON bank_txns(txn_date)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_invoice_rows_unique ON invoice_rows(company_id, unique_key)",
            "CREATE INDEX IF NOT EXISTS idx_invoice_rows_date ON invoice_rows(inv_date)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_cp_alias_unique ON counterparty_aliases(company_id, alias)",
        ]:
            try:
                conn.execute(ddl)
            except sqlite3.IntegrityError:
                logger.warning("建唯一索引失败（历史数据存在重复 unique_key），去重仍由应用层保证")
        conn.commit()
    finally:
        conn.close()


init_db()  # 导入即建表/迁移：无论经启动器还是直接运行本模块，旧库都能补齐新增列


# ---------------------------------------------------------------------------
# 本地访问守卫（skill 端点用，照发票助手 1.6.0）
# ---------------------------------------------------------------------------

def is_local_request() -> bool:
    host = request.host.split(":")[0] if request.host else ""
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    remote = (request.remote_addr or "")[:9]
    return remote in {"127.", "localhost"}


# ---------------------------------------------------------------------------
# 基础端点
# ---------------------------------------------------------------------------

def _no_cache(resp):
    """v1.0.6：页面与图标一律不缓存。

    Edge 的 --app 窗口复用同一个 user-data-dir，旧 index.html / 旧 logo 会被 disk cache
    顶着，升级后仍然显示老界面（用户就会觉得"logo 没改成功"）。
    """
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/")
def index():
    return _no_cache(make_response(send_from_directory(BUNDLE_DIR, "index.html")))


@app.route("/assets/<path:filename>")
def assets(filename: str):
    return _no_cache(make_response(send_from_directory(BUNDLE_DIR / "assets", filename)))


@app.route("/app.ico")
def app_icon():
    return _no_cache(make_response(send_from_directory(BUNDLE_DIR, "app.ico")))


@app.route("/api/health")
def health():
    return jsonify({
        "service": SERVICE_NAME,
        "display_name": "首席执行官",
        "version": APP_VERSION,
        "port": SERVER_PORT,
        "time": datetime.now().isoformat(timespec="seconds"),
    })


@app.route("/api/shutdown", methods=["POST"])
def shutdown():
    if not is_local_request():
        return jsonify({"success": False, "error": "local access only"}), 403

    def stopper():
        time.sleep(0.3)
        import os
        os._exit(0)

    threading.Thread(target=stopper, daemon=True).start()
    return jsonify({"success": True})


# 心跳：窗口关闭后服务保持存活（周报定时任务照常跑，§8），仅保留兜底
HEARTBEAT_IDLE_EXIT_SECONDS = 0.0
_HEARTBEAT_LOCK = threading.Lock()
_HEARTBEAT_STATE = {"last_seen": 0.0}


@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    if not is_local_request():
        return jsonify({"success": False, "error": "local access only"}), 403
    with _HEARTBEAT_LOCK:
        _HEARTBEAT_STATE["last_seen"] = time.time()
    return jsonify({"success": True})


def heartbeat_watchdog() -> None:
    while True:
        time.sleep(5)
        if HEARTBEAT_IDLE_EXIT_SECONDS <= 0:
            continue
        with _HEARTBEAT_LOCK:
            last_seen = _HEARTBEAT_STATE["last_seen"]
        if last_seen > 0 and time.time() - last_seen > HEARTBEAT_IDLE_EXIT_SECONDS:
            logger.info("长时间未收到界面心跳，本地服务自动退出。")
            import os
            os._exit(0)


def start_heartbeat_watchdog() -> None:
    threading.Thread(target=heartbeat_watchdog, daemon=True).start()


# ---------------------------------------------------------------------------
# 公司管理（照发票助手：公司下拉 + 新增/修改/删除，数据按公司隔离）
# ---------------------------------------------------------------------------

def company_id_from() -> int:
    """从 JSON、multipart 表单或 query 读取 company_id；缺失或非法时回退默认公司 1。"""
    raw = None
    if request.method in {"POST", "PUT", "DELETE"}:
        payload = request.get_json(silent=True) or {}
        raw = payload.get("company_id")
        if raw is None:
            raw = request.form.get("company_id")
    if raw is None:
        raw = request.args.get("company_id")
    try:
        return int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        return 1


def company_exists(conn: sqlite3.Connection, company_id: int) -> bool:
    return conn.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone() is not None


def get_company(conn: sqlite3.Connection, company_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, name, tax_no, note FROM companies WHERE id=?", (company_id,)).fetchone()
    return dict(row) if row else None


DEFAULT_COMPANY_PLACEHOLDERS = {"默认公司", ""}


def company_ready(company: dict[str, Any] | None) -> tuple[bool, str]:
    """v1.0.3 导入前置校验：本公司必须先设置全称与税号，
    否则发票/流水的"对方"无法与本公司区分，容易把本公司当成客户或供应商。"""
    if company is None:
        return False, "公司不存在，请先到「导入」页选择或新增公司"
    if (company.get("name") or "").strip() in DEFAULT_COMPANY_PLACEHOLDERS:
        return False, "请先在「导入」页完善本公司信息：公司全称还没设置（不能叫「默认公司」）"
    tax = (company.get("tax_no") or "").strip()
    if not tax:
        return False, "请先在「导入」页填写本公司税号（统一社会信用代码），用于区分自己与客户/供应商"
    return True, ""


def require_company_ready(company_id: int) -> tuple[bool, str]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ok, msg = company_ready(get_company(conn, company_id))
        return ok, msg
    finally:
        conn.close()


@app.route("/api/companies", methods=["GET", "POST"])
def api_companies():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            name = (payload.get("name") or "").strip()
            tax_no = (payload.get("tax_no") or "").strip()
            note = (payload.get("note") or "").strip()
            if not name:
                return jsonify({"success": False, "error": "请填写公司名称"}), 400
            now_text = datetime.now().isoformat(timespec="seconds")
            if payload.get("id"):
                if not company_exists(conn, int(payload["id"])):
                    return jsonify({"success": False, "error": "公司不存在"}), 404
                conn.execute("UPDATE companies SET name=?, tax_no=?, note=? WHERE id=?",
                             (name, tax_no, note, int(payload["id"])))
                target_id = int(payload["id"])
            else:
                cur = conn.execute(
                    "INSERT INTO companies (name, tax_no, note, created_at) VALUES (?,?,?,?)",
                    (name, tax_no, note, now_text),
                )
                target_id = int(cur.lastrowid)
            conn.commit()
            row = conn.execute(
                "SELECT id, name, tax_no, note FROM companies WHERE id=?", (target_id,)).fetchone()
            return jsonify({"success": True, "company": dict(row)})
        rows = conn.execute(
            "SELECT id, name, tax_no, note, created_at FROM companies ORDER BY id").fetchall()
        return jsonify({"success": True, "companies": [dict(r) for r in rows]})
    finally:
        conn.close()


@app.route("/api/companies/<int:company_id>", methods=["DELETE"])
def api_company_delete(company_id: int):
    conn = sqlite3.connect(DB_PATH)
    try:
        if not company_exists(conn, company_id):
            return jsonify({"success": False, "error": "公司不存在"}), 404
        total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        if total <= 1:
            return jsonify({"success": False, "error": "至少保留一个公司"}), 400
        for table in ("bank_txns", "invoice_rows", "import_batches",
                      "fixed_expenses", "expense_overrides", "briefings"):
            conn.execute(f"DELETE FROM {table} WHERE company_id=?", (company_id,))
        conn.execute("DELETE FROM companies WHERE id=?", (company_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# 导入端点（导入即解析，§3.5；数据按公司隔离）
# ---------------------------------------------------------------------------

@app.route("/api/import/bank", methods=["POST"])
def api_import_bank():
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"success": False, "error": "缺少文件"}), 400
    company_id = company_id_from()
    ok, msg = require_company_ready(company_id)  # v1.0.3：先建公司（全称+税号）再导入
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    save_path = get_app_home() / "data" / f"bank_{int(time.time())}_{file.filename}"
    file.save(str(save_path))
    try:
        summary = importer.import_bank_file(Path(DB_PATH), save_path, company_id)
    except Exception as exc:
        logger.exception("银行流水导入失败")
        return jsonify({"success": False, "error": str(exc)}), 400
    summary["source"] = file.filename
    summary["company_id"] = company_id
    return jsonify({"success": True, "summary": summary})


@app.route("/api/import/invoice", methods=["POST"])
def api_import_invoice():
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"success": False, "error": "缺少文件"}), 400
    kind = (request.form.get("kind") or "").strip().lower()
    if kind not in {"issue", "receive", ""}:
        return jsonify({"success": False, "error": f"未知发票方向：{kind}"}), 400
    company_id = company_id_from()
    ok, msg = require_company_ready(company_id)  # v1.0.3：先建公司（全称+税号）再导入
    if not ok:
        return jsonify({"success": False, "error": msg}), 400
    save_path = get_app_home() / "data" / f"invoice_{int(time.time())}_{file.filename}"
    file.save(str(save_path))
    try:
        summary = importer.import_invoice_file(Path(DB_PATH), save_path, default_kind=kind or None,
                                               company_id=company_id)
    except Exception as exc:
        logger.exception("发票明细导入失败")
        return jsonify({"success": False, "error": str(exc)}), 400
    summary["source"] = file.filename
    summary["company_id"] = company_id
    return jsonify({"success": True, "summary": summary})


@app.route("/api/import/status")
def api_import_status():
    company_id = company_id_from()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        bank_total = conn.execute(
            "SELECT COUNT(*) FROM bank_txns WHERE company_id=?", (company_id,)).fetchone()[0]
        bank_last = conn.execute(
            "SELECT MAX(imported_at), MAX(txn_date) FROM bank_txns WHERE company_id=?",
            (company_id,)).fetchone()
        accounts = conn.execute(
            "SELECT account_no, MAX(account_name) AS account_name, COUNT(*) AS txn_count,"
            " MIN(txn_date) AS first_date, MAX(txn_date) AS last_date"
            " FROM bank_txns WHERE company_id=? GROUP BY account_no ORDER BY account_no",
            (company_id,),
        ).fetchall()
        issue_count = conn.execute(
            "SELECT COUNT(*) FROM invoice_rows WHERE company_id=? AND kind='issue'",
            (company_id,)).fetchone()[0]
        receive_count = conn.execute(
            "SELECT COUNT(*) FROM invoice_rows WHERE company_id=? AND kind='receive'",
            (company_id,)).fetchone()[0]
        inv_last = conn.execute(
            "SELECT MAX(imported_at), MAX(inv_date) FROM invoice_rows WHERE company_id=?",
            (company_id,)).fetchone()
        suspicious = conn.execute(
            "SELECT COUNT(*) FROM bank_txns WHERE company_id=? AND dup_flag='疑似重复'",
            (company_id,)).fetchone()[0]
    finally:
        conn.close()
    return jsonify({
        "success": True, "company_id": company_id,
        "bank": {
            "txn_count": bank_total,
            "last_imported_at": bank_last[0] or "",
            "last_txn_date": bank_last[1] or "",
            "suspicious": suspicious,
            "accounts": [dict(a) for a in accounts],
        },
        "invoice": {
            "issue": {"count": issue_count},
            "receive": {"count": receive_count},
            "last_imported_at": inv_last[0] or "",
            "last_inv_date": inv_last[1] or "",
        },
    })


@app.route("/api/import/batches")
def api_import_batches():
    """导入批次历史（数据导入页"最近导入"）。按公司隔离。"""
    company_id = company_id_from()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, file_name, kind, raw_rows, inserted, duplicated, updated, suspicious,"
            " warnings_json, imported_at FROM import_batches WHERE company_id=?"
            " ORDER BY id DESC LIMIT 50",
            (company_id,),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        item = dict(r)
        try:
            item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
        except ValueError:
            item["warnings"] = []
        result.append(item)
    return jsonify({"success": True, "batches": result})


@app.route("/api/import/clear", methods=["POST"])
def api_import_clear():
    """手动清空（数据源管理用）。仅清本公司数据。"""
    payload = request.get_json(silent=True) or {}
    kind = payload.get("kind", "")
    company_id = company_id_from()
    conn = sqlite3.connect(DB_PATH)
    try:
        if kind in {"bank", "all"}:
            conn.execute("DELETE FROM bank_txns WHERE company_id=?", (company_id,))
        if kind in {"invoice", "all"}:
            conn.execute("DELETE FROM invoice_rows WHERE company_id=?", (company_id,))
        if kind == "all":
            conn.execute("DELETE FROM import_batches WHERE company_id=?", (company_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# 台账（长期累积业务数据的展示页，参考资金助手日记账/发票助手台账）
# ---------------------------------------------------------------------------

@app.route("/api/bank/accounts", methods=["GET", "POST"])
def api_bank_accounts():
    """v1.0.8 银行账户：列出已导入的账户并支持改名。

    网银文件里有时找不到"本方账号"（或工作表名被当成账号），
    导入时只能用工作表名占位。这里让用户把名字改成真实账户名。
    """
    company_id = company_id_from()
    conn = sqlite3.connect(DB_PATH)
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            account_no = (payload.get("account_no") or "").strip()
            account_name = (payload.get("account_name") or "").strip()
            if not account_no:
                return jsonify({"success": False, "error": "缺少账户标识"}), 400
            if not account_name:
                return jsonify({"success": False, "error": "账户名不能为空"}), 400
            cur = conn.execute(
                "UPDATE bank_txns SET account_name=? WHERE company_id=? AND account_no=?",
                (account_name, company_id, account_no),
            )
            conn.commit()
            if cur.rowcount == 0:
                return jsonify({"success": False, "error": "该账户下没有流水"}), 404
        rows = conn.execute(
            "SELECT account_no, MAX(account_name) AS account_name, COUNT(*) AS cnt, "
            "MIN(txn_date) AS first_date, MAX(txn_date) AS last_date "
            "FROM bank_txns WHERE company_id=? GROUP BY account_no ORDER BY MIN(txn_date)",
            (company_id,),
        ).fetchall()
        accounts = [{
            "account_no": r[0] or "",
            # 旧数据没存账户名时，用"未识别-XX"里的 XX 兜底，列表不至于空着
            "account_name": (r[1] or "").strip() or (
                (r[0] or "").split("未识别-", 1)[1] if (r[0] or "").startswith("未识别-") else ""),
            "count": int(r[2] or 0),
            "first_date": r[3] or "",
            "last_date": r[4] or "",
            "unrecognized": (r[0] or "").startswith("未识别"),
        } for r in rows]
        return jsonify({"success": True, "accounts": accounts})
    finally:
        conn.close()


@app.route("/api/ledger/bank")
def api_ledger_bank():
    """银行流水日记账：分页 + 筛选（账户/方向/日期/关键词/疑似重复）。"""
    args = request.args
    page = max(1, int(args.get("page", 1) or 1))
    page_size = min(200, max(10, int(args.get("page_size", 50) or 50)))
    export = args.get("export") in {"1", "true"}
    company_id = company_id_from()
    where, params = ["company_id=?"], [company_id]

    def add(cond: str, *vals: Any) -> None:
        where.append(cond)
        params.extend(vals)

    if args.get("account"):
        add("account_no=?", args.get("account"))
    if args.get("direction") in {"in", "out"}:
        add("direction=?", args.get("direction"))
    if args.get("date_from"):
        add("txn_date>=?", args.get("date_from"))
    if args.get("date_to"):
        add("txn_date<=?", args.get("date_to"))
    if args.get("keyword"):
        kw = f"%{args.get('keyword').strip()}%"
        add("(counterparty LIKE ? OR summary LIKE ? OR txn_ref LIKE ?)", kw, kw, kw)
    if args.get("dup_only") in {"1", "true"}:
        add("dup_flag='疑似重复'")
    where_sql = " AND ".join(where)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM bank_txns WHERE {where_sql}", params).fetchone()[0]
        limit_clause = "" if export else " LIMIT ? OFFSET ?"
        extra = [] if export else [page_size, (page - 1) * page_size]
        rows = conn.execute(
            f"""
            SELECT id, account_no, account_name, txn_date, txn_time, direction, amount,
                   balance_after, counterparty, summary, txn_ref, dup_flag, imported_at
            FROM bank_txns WHERE {where_sql}
            ORDER BY txn_date DESC, txn_time DESC, id DESC{limit_clause}
            """,
            [*params, *extra],
        ).fetchall()
        accounts = conn.execute(
            "SELECT DISTINCT account_no FROM bank_txns WHERE company_id=? ORDER BY account_no",
            (company_id,),
        ).fetchall()
        sums = conn.execute(
            f"""
            SELECT direction, COUNT(*) AS n, IFNULL(SUM(amount),0) AS total_amount
            FROM bank_txns WHERE {where_sql} GROUP BY direction
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return jsonify({
        "success": True, "total": total, "page": page, "page_size": page_size,
        "rows": [dict(r) for r in rows],
        "accounts": [a["account_no"] for a in accounts],
        "summary": {s["direction"]: {"count": s["n"], "amount": s["total_amount"]} for s in sums},
    })


@app.route("/api/ledger/invoice")
def api_ledger_invoice():
    """发票台账（复刻发票助手台账口径）：分页 + 筛选 + export=1 全量导出。"""
    args = request.args
    page = max(1, int(args.get("page", 1) or 1))
    page_size = min(200, max(10, int(args.get("page_size", 50) or 50)))
    export = args.get("export") in {"1", "true"}
    company_id = company_id_from()
    where, params = ["company_id=?"], [company_id]

    def add(cond: str, *vals: Any) -> None:
        where.append(cond)
        params.extend(vals)

    if args.get("kind") in {"issue", "receive"}:
        add("kind=?", args.get("kind"))
    if args.get("date_from"):
        add("inv_date>=?", args.get("date_from"))
    if args.get("date_to"):
        add("inv_date<=?", args.get("date_to"))
    if args.get("inv_no"):
        add("inv_no LIKE ?", f"%{args.get('inv_no').strip()}%")
    for param, column in [("amount_min", "amount"), ("amount_max", "amount"),
                          ("tax_min", "tax"), ("tax_max", "tax")]:
        value = args.get(param)
        if value not in (None, ""):
            try:
                add(f"{column}>=?" if param.endswith("_min") else f"{column}<=?", float(value))
            except ValueError:
                pass
    if args.get("keyword"):
        kw = f"%{args.get('keyword').strip()}%"
        add("(counterparty LIKE ? OR project LIKE ?)", kw, kw)
    if args.get("unit"):
        add("counterparty LIKE ?", f"%{args.get('unit').strip()}%")
    where_sql = " AND ".join(where)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM invoice_rows WHERE {where_sql}", params).fetchone()[0]
        limit_clause = "" if export else " LIMIT ? OFFSET ?"
        extra = [] if export else [page_size, (page - 1) * page_size]
        rows = conn.execute(
            f"""
            SELECT id, kind, inv_date, counterparty, amount, tax, project, inv_no, imported_at
            FROM invoice_rows WHERE {where_sql}
            ORDER BY inv_date DESC, id DESC{limit_clause}
            """,
            [*params, *extra],
        ).fetchall()
        sums = conn.execute(
            f"""
            SELECT kind, COUNT(*) AS n, IFNULL(SUM(amount),0) AS total_amount,
                   IFNULL(SUM(tax),0) AS total_tax
            FROM invoice_rows WHERE {where_sql} GROUP BY kind
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return jsonify({
        "success": True, "total": total, "page": page, "page_size": page_size,
        "rows": [dict(r) for r in rows],
        "summary": {s["kind"]: {"count": s["n"], "amount": s["total_amount"],
                                "tax": s["total_tax"]} for s in sums},
    })


# ---------------------------------------------------------------------------
# 看板
# ---------------------------------------------------------------------------

@app.route("/api/dashboard")
def api_dashboard():
    company_id = company_id_from()
    try:
        data = engine.build_dashboard(DB_PATH, company_id)
        data["company_id"] = company_id
        return jsonify({"success": True, "dashboard": data})
    except Exception as exc:
        logger.exception("看板计算失败")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/runway/what-if", methods=["POST"])
def api_what_if():
    payload = request.get_json(silent=True) or {}
    client = (payload.get("client") or "").strip()
    if not client:
        return jsonify({"success": False, "error": "缺少客户名"}), 400
    try:
        result = ai_layer.ai_scenario_question(DB_PATH, client,
                                               company_id=company_id_from())
        return jsonify({"success": True, "result": result})
    except Exception as exc:
        logger.exception("情景问答失败")
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# 配置：固定支出 / 大额支出 / 回收率与安全线 / AI / 企微
# ---------------------------------------------------------------------------

@app.route("/api/config/fixed-expenses", methods=["GET", "POST", "DELETE"])
def api_fixed_expenses():
    company_id = company_id_from()
    conn = sqlite3.connect(DB_PATH)
    try:
        if request.method == "GET":
            data = engine.load_fixed_expenses(conn, company_id)
            return jsonify({"success": True, "company_id": company_id, **data})
        payload = request.get_json(silent=True) or {}
        if request.method == "POST":
            name = (payload.get("name") or "").strip()
            amount = payload.get("monthly_amount")
            if not name or amount is None or amount == "":
                return jsonify({"success": False, "error": "名称与月金额必填"}), 400
            try:
                amount_value = float(amount)
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "月金额必须是数字"}), 400
            raw_day = payload.get("day_of_month")
            if raw_day in (None, ""):
                day_value = 0
            else:
                try:
                    day_value = int(float(raw_day))
                except (TypeError, ValueError):
                    return jsonify({"success": False, "error": "每月几号必须是 1~31 的整数"}), 400
                if day_value and not 1 <= day_value <= 31:
                    return jsonify({"success": False, "error": "每月几号必须是 1~31 的整数"}), 400
            note = (payload.get("note") or "").strip()
            if payload.get("id") is not None:
                # v1.0.4 支持修改
                cur = conn.execute(
                    "UPDATE fixed_expenses SET name=?, monthly_amount=?, day_of_month=?, note=? "
                    "WHERE id=? AND company_id=?",
                    (name, amount_value, day_value, note, int(payload["id"]), company_id),
                )
                if cur.rowcount == 0:
                    return jsonify({"success": False, "error": "该记录不存在（可能已被删除）"}), 404
            else:
                conn.execute(
                    "INSERT INTO fixed_expenses (company_id, name, monthly_amount, day_of_month, note) "
                    "VALUES (?,?,?,?,?)",
                    (company_id, name, amount_value, day_value, note),
                )
        else:  # DELETE
            item_id = payload.get("id")
            if item_id is None:
                return jsonify({"success": False, "error": "缺少 id"}), 400
            conn.execute("DELETE FROM fixed_expenses WHERE id=? AND company_id=?",
                         (int(item_id), company_id))
        conn.commit()
        data = engine.load_fixed_expenses(conn, company_id)
        return jsonify({"success": True, **data})
    finally:
        conn.close()


@app.route("/api/config/expense-overrides", methods=["GET", "POST", "DELETE"])
def api_expense_overrides():
    company_id = company_id_from()
    conn = sqlite3.connect(DB_PATH)
    try:
        payload = request.get_json(silent=True) or {}
        if request.method == "POST":
            date_raw = (payload.get("date") or payload.get("month") or "").strip()
            amount = payload.get("amount")
            if not date_raw:
                return jsonify({"success": False, "error": "日期必填"}), 400
            if amount is None or amount == "":
                return jsonify({"success": False, "error": "金额必填"}), 400
            try:
                amount_value = float(amount)
            except (TypeError, ValueError):
                return jsonify({"success": False, "error": "金额必须是数字"}), 400
            # 日期允许 YYYY-MM-DD（到日）或 YYYY-MM（旧式到月）
            month = date_raw[:7]
            note = (payload.get("note") or "").strip()
            if payload.get("id") is not None:
                cur = conn.execute(
                    "UPDATE expense_overrides SET month=?, expense_date=?, amount=?, note=? "
                    "WHERE id=? AND company_id=?",
                    (month, date_raw, amount_value, note, int(payload["id"]), company_id),
                )
                if cur.rowcount == 0:
                    return jsonify({"success": False, "error": "该记录不存在（可能已被删除）"}), 404
            else:
                # 同一天重复录入视为覆盖；不同天各自保留（同月可多笔）
                conn.execute(
                    "DELETE FROM expense_overrides WHERE company_id=? AND "
                    "(expense_date=? OR (COALESCE(expense_date,'')='' AND month=?))",
                    (company_id, date_raw, month),
                )
                conn.execute(
                    "INSERT INTO expense_overrides (company_id, month, expense_date, amount, note) "
                    "VALUES (?,?,?,?,?)",
                    (company_id, month, date_raw, amount_value, note),
                )
        elif request.method == "DELETE":
            if payload.get("id") is not None:
                conn.execute("DELETE FROM expense_overrides WHERE id=? AND company_id=?",
                             (int(payload["id"]), company_id))
            elif payload.get("date"):
                conn.execute("DELETE FROM expense_overrides WHERE company_id=? AND expense_date=?",
                             (company_id, payload["date"]))
            elif payload.get("month"):
                conn.execute("DELETE FROM expense_overrides WHERE company_id=? AND month=?",
                             (company_id, payload["month"]))
        conn.commit()
        data = engine.load_fixed_expenses(conn, company_id)
        return jsonify({"success": True, **data})
    finally:
        conn.close()


@app.route("/api/config/engine", methods=["GET", "POST"])
def api_config_engine():
    conn = sqlite3.connect(DB_PATH)
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            settings = engine.load_settings(conn)
            if isinstance(payload.get("recovery_rates"), dict):
                for scenario, buckets in payload["recovery_rates"].items():
                    if scenario in settings["recovery_rates"] and isinstance(buckets, dict):
                        for b, v in buckets.items():
                            if b in engine.BUCKETS:
                                try:
                                    settings["recovery_rates"][scenario][b] = max(0.0, min(float(v), 1.0))
                                except (TypeError, ValueError):
                                    pass
            if payload.get("safety_months") is not None:
                try:
                    settings["safety_months"] = max(0.0, float(payload["safety_months"]))
                except (TypeError, ValueError):
                    pass
            if payload.get("fixed_in_cashflow") is not None:
                settings["fixed_in_cashflow"] = bool(payload["fixed_in_cashflow"])
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('engine', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({"recovery_rates": settings["recovery_rates"],
                             "safety_months": settings["safety_months"],
                             "fixed_in_cashflow": settings["fixed_in_cashflow"]},
                            ensure_ascii=False),),
            )
            conn.commit()
        settings = engine.load_settings(conn)
        return jsonify({"success": True, "recovery_rates": settings["recovery_rates"],
                        "safety_months": settings["safety_months"],
                        "fixed_in_cashflow": settings["fixed_in_cashflow"]})
    finally:
        conn.close()


@app.route("/api/config/counterparty-aliases", methods=["GET", "POST", "DELETE"])
def api_counterparty_aliases():
    """v1.0.5 往来单位关联：发票名与流水名不一致但属同一家 → 标记后合并计算。

    GET 同时返回候选名单（发票/流水里出现过的对方名称，按笔数排序），前端做 datalist。
    """
    company_id = company_id_from()
    conn = sqlite3.connect(DB_PATH)
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            alias = (payload.get("alias") or "").strip()
            canonical = (payload.get("canonical") or "").strip()
            if not alias or not canonical:
                return jsonify({"success": False, "error": "两个名称都要填"}), 400
            if alias == canonical:
                return jsonify({"success": False, "error": "两个名称相同，无需关联"}), 400
            # 反手关联会形成环（A→B 已有，又来 B→A），先拦掉
            row = conn.execute(
                "SELECT canonical FROM counterparty_aliases WHERE company_id=? AND alias=?",
                (company_id, canonical),
            ).fetchone()
            if row and (row[0] or "").strip() == alias:
                return jsonify({"success": False, "error": "这两家已互为关联，方向反了"}), 400
            conn.execute("DELETE FROM counterparty_aliases WHERE company_id=? AND alias=?",
                         (company_id, alias))
            conn.execute(
                "INSERT INTO counterparty_aliases (company_id, alias, canonical, created_at) "
                "VALUES (?,?,?,?)",
                (company_id, alias, canonical, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        elif request.method == "DELETE":
            payload = request.get_json(silent=True) or {}
            if payload.get("id") is not None:
                conn.execute("DELETE FROM counterparty_aliases WHERE id=? AND company_id=?",
                             (int(payload["id"]), company_id))
            elif payload.get("alias"):
                conn.execute("DELETE FROM counterparty_aliases WHERE alias=? AND company_id=?",
                             ((payload["alias"] or "").strip(), company_id))
            conn.commit()

        alias_map = engine.load_counterparty_aliases(conn, company_id)
        self_names = engine.load_self_names(conn, company_id)
        rows = conn.execute(
            "SELECT id, alias, canonical FROM counterparty_aliases WHERE company_id=? ORDER BY id",
            (company_id,),
        ).fetchall()
        aliases = [{"id": r[0], "alias": r[1], "canonical": alias_map.get(r[1], r[2]) or r[2]}
                   for r in rows]

        name_counter: dict[str, int] = {}
        for table in ("invoice_rows", "bank_txns"):
            for row in conn.execute(
                f"SELECT counterparty, COUNT(*) FROM {table} WHERE company_id=? GROUP BY counterparty",
                (company_id,),
            ).fetchall():
                name = (row[0] or "").strip()
                if not name or engine.is_self_name(name, self_names):
                    continue
                name_counter[name] = name_counter.get(name, 0) + int(row[1])
        names = [n for n, _ in sorted(name_counter.items(), key=lambda kv: (-kv[1], kv[0]))]
        return jsonify({"success": True, "aliases": aliases, "names": names[:400],
                        "self_names": self_names})
    finally:
        conn.close()


@app.route("/api/config/ai", methods=["GET", "POST"])
def api_config_ai():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        saved = ai_layer.save_ai_config(DB_PATH, payload)
        return jsonify({"success": True, "config": saved,
                        "enabled": ai_layer.read_sources_config(DB_PATH)["enabled"]})
    cfg = ai_layer.read_sources_config(DB_PATH)
    masked = dict(cfg)
    masked["api_key"] = (cfg["api_key"][:4] + "****") if cfg["api_key"] else ""
    return jsonify({"success": True, "config": masked, "enabled": cfg["enabled"],
                    "providers": {k: {"label": v["label"], "base_url": v["base_url"],
                                      "default_model": v["default_model"],
                                      "group": v.get("group", ""),
                                      "models": v.get("models", []),
                                      "hint": v.get("hint", "")}
                                  for k, v in ai_layer.PROVIDER_PRESETS.items()}})


@app.route("/api/config/ai/test", methods=["POST"])
def api_config_ai_test():
    """v1.0.4 测试连接：未保存的表单值也能直接测，返回人话结论。"""
    payload = request.get_json(silent=True) or {}
    try:
        result = ai_layer.test_ai_config(DB_PATH, payload)
    except Exception as exc:  # noqa: BLE001 - 兜底不让按钮把页面搞崩
        return jsonify({"success": True, "ok": False, "message": f"测试失败：{exc}"})
    return jsonify({"success": True, **result})


@app.route("/api/config/ai/models", methods=["GET", "POST"])
def api_config_ai_models():
    """拉取厂商模型列表：填了 Key 就能选，不用背模型名。"""
    payload = request.get_json(silent=True) or {}
    saved = ai_layer.read_sources_config(DB_PATH)
    provider = payload.get("provider") or saved.get("provider") or "agnes"
    api_key = (payload.get("api_key") or "").strip()
    if not api_key or "*" in api_key:
        api_key = saved.get("api_key") or ""
    base_url = ai_layer.normalize_base_url(
        provider, payload.get("base_url") or (saved.get("base_url") if provider == saved.get("provider") else "")
    )
    result = ai_layer.list_models(base_url, api_key)
    return jsonify({"success": True, "base_url": base_url, **result})


@app.route("/api/config/wecom", methods=["GET", "POST"])
def api_config_wecom():
    conn = sqlite3.connect(DB_PATH)
    try:
        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('wecom', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps({"webhook_url": (payload.get("webhook_url") or "").strip()}),),
            )
            conn.commit()
        row = conn.execute("SELECT value FROM settings WHERE key='wecom'").fetchone()
        webhook = ""
        if row:
            try:
                webhook = (json.loads(row[0]) or {}).get("webhook_url", "")
            except ValueError:
                webhook = ""
        masked = (webhook[:38] + "…") if len(webhook) > 42 else webhook
        return jsonify({"success": True, "webhook_url": masked, "configured": bool(webhook)})
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AI 场景端点
# ---------------------------------------------------------------------------

@app.route("/api/ai/chat", methods=["POST"])
def api_ai_chat():
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"success": False, "error": "问题不能为空"}), 400
    result = ai_layer.ai_chat(DB_PATH, question, company_id=company_id_from())
    conn = sqlite3.connect(DB_PATH)
    try:
        now_text = datetime.now().isoformat(timespec="seconds")
        conn.execute("INSERT INTO ai_history (role, content, created_at) VALUES (?,?,?)",
                     ("user", question, now_text))
        conn.execute("INSERT INTO ai_history (role, content, created_at) VALUES (?,?,?)",
                     ("assistant", result.get("answer", ""), now_text))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"success": True, **result})


@app.route("/api/ai/history")
def api_ai_history():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT role, content, created_at FROM ai_history ORDER BY id DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"success": True, "history": [dict(r) for r in reversed(rows)]})


@app.route("/api/ai/attribute", methods=["POST"])
def api_ai_attribute():
    payload = request.get_json(silent=True) or {}
    alert = (payload.get("alert") or "").strip()
    if not alert:
        return jsonify({"success": False, "error": "缺少预警内容"}), 400
    return jsonify({"success": True,
                    **ai_layer.ai_attribute_alert(DB_PATH, alert, company_id=company_id_from())})


@app.route("/api/ai/scenario", methods=["POST"])
def api_ai_scenario():
    payload = request.get_json(silent=True) or {}
    client = (payload.get("client") or "").strip()
    if not client:
        return jsonify({"success": False, "error": "缺少客户名"}), 400
    return jsonify({"success": True,
                    **ai_layer.ai_scenario_question(DB_PATH, client, company_id=company_id_from())})


# ---------------------------------------------------------------------------
# 周报
# ---------------------------------------------------------------------------

@app.route("/api/briefing/generate", methods=["POST"])
def api_briefing_generate():
    payload = request.get_json(silent=True) or {}
    try:
        result = briefing.generate_briefing(DB_PATH, force=bool(payload.get("force")),
                                            company_id=company_id_from())
        return jsonify({"success": True, **result})
    except Exception as exc:
        logger.exception("周报生成失败")
        return jsonify({"success": False, "error": str(exc)}), 500


@app.route("/api/briefing/push", methods=["POST"])
def api_briefing_push():
    payload = request.get_json(silent=True) or {}
    return jsonify(briefing.push_briefing_to_wecom(DB_PATH, payload.get("content")))


# ---------------------------------------------------------------------------
# skill 只读端点（§7，守卫照发票助手 1.6.0）
# ---------------------------------------------------------------------------

@app.route("/api/skill/health")
def api_skill_health():
    return jsonify({"service": SERVICE_NAME, "version": APP_VERSION, "ok": True})


@app.route("/api/skill/info")
def api_skill_info():
    if not is_local_request():
        return jsonify({"success": False, "error": "local access only"}), 403
    return jsonify({
        "success": True,
        "service": SERVICE_NAME,
        "display_name": "首席执行官",
        "version": APP_VERSION,
        "endpoints": ["/api/skill/health", "/api/skill/info", "/api/skill/companies",
                      "/api/skill/ceo-brief?company_id=<id>"],
        "description": "老板经营驾驶舱（只读）：现金跑道/应收/账龄/回款/周报",
    })


@app.route("/api/skill/companies")
def api_skill_companies():
    if not is_local_request():
        return jsonify({"success": False, "error": "local access only"}), 403
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, name FROM companies ORDER BY id").fetchall()
    finally:
        conn.close()
    return jsonify({"success": True, "companies": [dict(row) for row in rows]})


@app.route("/api/skill/ceo-brief")
def api_skill_ceo_brief():
    if not is_local_request():
        return jsonify({"success": False, "error": "local access only"}), 403
    company_id = company_id_from()
    latest = briefing.latest_briefing(DB_PATH, company_id)
    if not latest:
        return jsonify({"success": False, "error": "尚无周报"}), 404
    return jsonify({"success": True, "company_id": company_id, **latest})
