"""决策引擎（技术说明 §4）——本地确定性计算，零 AI 依赖。

铁律：
- 现金跑道三场景（乐观/中性/悲观）并列输出，绝不给"某月必缺钱"的单点。
- 应收余额 = 开具发票 − 银行已收，轻匹配（对方+金额+日期窗口±90天），
  未匹配开具额进"未匹配应收"桶——这是计算口径，不是核销工作流。
- 回收率是假设不是事实，所有输出必须可标注"基于回款假设"。
"""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime
from typing import Any

# ---------------------------------------------------------------------------
# 默认配置（可被 settings 表覆盖）
# ---------------------------------------------------------------------------
DEFAULT_RECOVERY_RATES: dict[str, dict[str, float]] = {
    "optimistic": {"0-30": 0.98, "31-60": 0.90, "61-90": 0.75, "90+": 0.50},
    "neutral":    {"0-30": 0.95, "31-60": 0.70, "61-90": 0.45, "90+": 0.20},
    "pessimistic": {"0-30": 0.90, "31-60": 0.50, "61-90": 0.25, "90+": 0.05},
}
SCENARIO_LABELS = {"optimistic": "乐观", "neutral": "中性", "pessimistic": "悲观"}
BUCKETS = ["0-30", "31-60", "61-90", "90+"]
MATCH_WINDOW_DAYS = 90       # 轻匹配日期窗口（§3.3）
FORECAST_MONTHS = 6          # 现金跑道展望月数
TREND_MONTHS = 6             # 收支/回款趋势展示月数
DEFAULT_SAFETY_MONTHS = 1.0  # 安全线默认 = 1 个月固定支出

AMOUNT_TOLERANCE = 0.01      # 轻匹配金额容差（角分级）

# ---------------------------------------------------------------------------
# v1.0.1 经营者视角：支出结构分类词典（保守归类，认不出的进"其他经营支出"）
# ---------------------------------------------------------------------------
SPEND_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("人力（工资/社保）", ("工资", "薪酬", "社保", "公积金", "劳务", "代发", "薪资", "年终")),
    ("场地（房租/水电）", ("房租", "租金", "物业", "水电", "电费", "水费", "燃气", "装修", "取暖")),
    ("税费", ("税", "国库", "税务", "缴税", "报税")),
    ("财务（利息/手续费）", ("利息", "贷款", "还款", "手续费", "年费", "汇兑", "贴现")),
    ("平台/推广", ("推广", "广告", "平台", "流量", "服务费", "会员", "订阅", "软件", "云")),
]
SPEND_OTHER_LABEL = "其他经营支出"
BIG_SPEND_MIN = 10000.0      # 大额支出最小关注额（元）
BIG_SPEND_LOOKBACK_DAYS = 30
ACTION_TOP_N = 3             # 行动清单每类最多条数


def _month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _ord_of(date_text: str) -> int | None:
    """'YYYY-MM-DD' → date.toordinal()；非法输入返回 None。"""
    try:
        return datetime.strptime(date_text, "%Y-%m-%d").date().toordinal()
    except (ValueError, TypeError):
        return None


def add_months(month: str, n: int) -> str:
    year, mon = int(month[:4]), int(month[5:7])
    total = year * 12 + (mon - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def month_diff(a: str, b: str) -> int:
    return (int(b[:4]) * 12 + int(b[5:7])) - (int(a[:4]) * 12 + int(a[5:7]))


def load_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT value FROM settings WHERE key='engine'").fetchone()
    settings: dict[str, Any] = {}
    if row:
        try:
            import json
            settings = json.loads(row[0])
        except Exception:
            settings = {}
    rates = DEFAULT_RECOVERY_RATES.copy()
    override_rates = settings.get("recovery_rates") or {}
    for scenario in rates:
        if isinstance(override_rates.get(scenario), dict):
            rates[scenario] = {**rates[scenario], **{
                b: float(v) for b, v in override_rates[scenario].items() if b in BUCKETS
            }}
    settings.setdefault("recovery_rates", rates)
    try:
        settings.setdefault("safety_months", float(settings.get("safety_months", DEFAULT_SAFETY_MONTHS)))
    except (TypeError, ValueError):
        settings["safety_months"] = DEFAULT_SAFETY_MONTHS
    # v1.0.8：固定支出是否已经在银行流水里体现（默认是）。
    # 是 → 跑道用流水净流（已含工资/房租），不再重复扣 monthly_fixed，只额外扣"某月大额支出"；
    # 否 → 老口径，每月再扣一遍固定支出（适合工资走个人卡、公司流水里看不到的情况）。
    settings["fixed_in_cashflow"] = bool(settings.get("fixed_in_cashflow", True))
    return settings


def load_fixed_expenses(conn: sqlite3.Connection, company_id: int = 1) -> dict[str, Any]:
    """固定支出：按月固定总额 + 某月大额支出覆盖（§3.4 命门）。按公司隔离。

    v1.0.4：
    - 每月固定支出增加「每月几号」（day_of_month，0 = 未指定）
    - 大额支出日期可精确到日（expense_date，兼容旧数据只有 month）
    - overrides 仍按「月份」聚合（跑道/行动清单口径不变），
      另给 override_items 逐笔明细供设置页编辑/删除
    """
    monthly_total = 0.0
    items: list[dict[str, Any]] = []
    fe_cols = {r[1] for r in conn.execute("PRAGMA table_info(fixed_expenses)").fetchall()}
    has_day = "day_of_month" in fe_cols
    for row in conn.execute(
        "SELECT id, name, monthly_amount, note" + (", day_of_month" if has_day else "")
        + " FROM fixed_expenses WHERE company_id=? ORDER BY id",
        (company_id,),
    ).fetchall():
        monthly_total += float(row[2] or 0)
        items.append({
            "id": row[0], "name": row[1], "monthly_amount": float(row[2] or 0),
            "note": row[3] or "",
            "day_of_month": int(row[4] or 0) if has_day else 0,
        })

    ov_cols = {r[1] for r in conn.execute("PRAGMA table_info(expense_overrides)").fetchall()}
    has_date = "expense_date" in ov_cols
    override_items: list[dict[str, Any]] = []
    for row in conn.execute(
        "SELECT id, month, amount, note" + (", expense_date" if has_date else "")
        + " FROM expense_overrides WHERE company_id=? ORDER BY id",
        (company_id,),
    ).fetchall():
        month = row[1] or ""
        raw_date = (row[4] if has_date else "") or ""
        date_value = raw_date or month
        override_items.append({
            "id": row[0],
            "month": (date_value[:7] if len(date_value) >= 7 else month),
            "date": date_value,
            "amount": float(row[2] or 0),
            "note": row[3] or "",
        })
    override_items.sort(key=lambda x: (x["month"], x["date"], x["id"]))

    overrides: dict[str, dict[str, Any]] = {}
    for it in override_items:
        month = it["month"]
        cur = overrides.get(month)
        if cur is None:
            overrides[month] = {"id": it["id"], "amount": it["amount"],
                                "note": it["note"], "date": it["date"], "count": 1}
        else:
            cur["amount"] += it["amount"]
            cur["count"] += 1
            if it["note"]:
                cur["note"] = (cur["note"] + "；" + it["note"]) if cur["note"] else it["note"]
    return {
        "monthly_total": monthly_total,
        "items": items,
        "overrides": overrides,
        "override_items": override_items,
    }


# ---------------------------------------------------------------------------
# v1.0.5 往来单位归一：别名合并（同一家的不同写法）+ 排除本公司
# ---------------------------------------------------------------------------

def load_counterparty_aliases(conn: sqlite3.Connection, company_id: int = 1) -> dict[str, str]:
    """别名 → 主名 的扁平映射（已做链式归一与防环）。

    场景：银行流水里对方写「A 公司」、发票对方写「B 公司」，实际是同一家业务
    （A 付款、B 开票）。标记后匹配与聚合都按主名走，不会拆成「一笔欠款 + 一笔欠票」。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(counterparty_aliases)").fetchall()}
    if not cols or "alias" not in cols:
        return {}
    raw: dict[str, str] = {}
    for row in conn.execute(
        "SELECT alias, canonical FROM counterparty_aliases WHERE company_id=?", (company_id,)
    ).fetchall():
        alias, canonical = (row[0] or "").strip(), (row[1] or "").strip()
        if alias and canonical and alias != canonical:
            raw[alias] = canonical
    resolved: dict[str, str] = {}
    for alias in raw:
        cur, seen = alias, {alias}
        while cur in raw:
            nxt = raw[cur]
            if nxt in seen:
                break
            seen.add(nxt)
            cur = nxt
        resolved[alias] = cur
    return resolved


def canonical_name(name: str | None, alias_map: dict[str, str]) -> str:
    value = (name or "").strip()
    if not value:
        return ""
    return alias_map.get(value, value)


def load_self_names(conn: sqlite3.Connection, company_id: int = 1) -> list[str]:
    """本公司自己的名称/税号：用来把「对方是自己」的发票排除出应收/应付。"""
    names: list[str] = []
    try:
        row = conn.execute("SELECT name, tax_no FROM companies WHERE id=?", (company_id,)).fetchone()
    except sqlite3.Error:
        row = None
    if row:
        for value in (row[0], row[1]):
            text = (value or "").strip()
            if text and text not in names:
                names.append(text)
    return names


def is_self_name(name: str | None, self_names: list[str]) -> bool:
    """判定「对方就是本公司自己」。

    规则与 _name_similar 一致（全称/简称互含），但加了长度门槛，
    避免「工资」「税」这类短名误伤；空格差异（导出常见）先压掉再比。
    """
    value = (name or "").strip()
    if not value or not self_names:
        return False
    compact = value.replace(" ", "").replace("　", "")
    for self_name in self_names:
        target = (self_name or "").replace(" ", "").replace("　", "")
        if not target:
            continue
        if compact == target:
            return True
        # 税号/全称互含：只在双方都够长时才做包含判断
        if len(target) >= 6 and len(compact) >= 6 and (target in compact or compact in target):
            return True
    return False


def _apply_counterparty_alias(rows: list[dict[str, Any]], alias_map: dict[str, str]) -> None:
    """把 counterparty 换成主名，原名留到 counterparty_raw（台账/支出分类仍可回看）。"""
    for row in rows:
        raw = (row.get("counterparty") or "").strip()
        row["counterparty_raw"] = raw
        row["counterparty"] = canonical_name(raw, alias_map)


def alias_reverse_map(alias_map: dict[str, str]) -> dict[str, list[str]]:
    reverse: dict[str, list[str]] = defaultdict(list)
    for alias, canonical in alias_map.items():
        reverse[canonical].append(alias)
    return reverse


# ---------------------------------------------------------------------------
# 轻匹配：开具发票 − 银行已收（对方+金额+日期窗口±90天）
# ---------------------------------------------------------------------------

def _name_similar(a: str, b: str) -> bool:
    na, nb = a.strip(), b.strip()
    if not na or not nb:
        return False
    if na == nb:
        return True
    return na in nb or nb in na  # 全称/简称互含


def match_receivables(issue_rows: list[dict[str, Any]], bank_rows: list[dict[str, Any]]
                      ) -> tuple[list[dict[str, Any]], float]:
    """对每张开具发票找银行收款；返回 (发票行[附matched], 已匹配金额)。"""
    receipts = [r for r in bank_rows if r["direction"] == "in"]
    used_receipt_ids: set[int] = set()
    matched_total = 0.0
    for inv in issue_rows:
        inv_date = datetime.strptime(inv["inv_date"], "%Y-%m-%d").date()
        matched = False
        for rcpt in receipts:
            if rcpt["id"] in used_receipt_ids:
                continue
            if abs(float(rcpt["amount"]) - float(inv["amount"])) > AMOUNT_TOLERANCE:
                continue
            try:
                rcpt_date = datetime.strptime(rcpt["txn_date"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if abs((rcpt_date - inv_date).days) > MATCH_WINDOW_DAYS:
                continue
            if not _name_similar(inv["counterparty"], rcpt["counterparty"] or ""):
                continue
            used_receipt_ids.add(rcpt["id"])
            matched = True
            matched_total += float(inv["amount"])
            break
        inv["matched"] = matched
    return issue_rows, matched_total


def bucket_for(days: int) -> str:
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


# ---------------------------------------------------------------------------
# v1.0.1 经营者视角计算（全部本地确定性，零 AI 依赖）
# ---------------------------------------------------------------------------

def classify_spend(counterparty: str, summary: str) -> str:
    """按 摘要+对方 关键词把支出归入经营大类（老板视角，非会计科目）。"""
    text = f"{counterparty or ''} {summary or ''}"
    for label, words in SPEND_CATEGORIES:
        if any(w in text for w in words):
            return label
    return SPEND_OTHER_LABEL


def match_payables(receive_rows: list[dict[str, Any]], bank_rows: list[dict[str, Any]]
                   ) -> tuple[list[dict[str, Any]], float]:
    """应付侧轻匹配（与应收对称）：取得发票 − 银行已付（对方+金额+±90天）。

    返回 (取得发票行[附matched], 已匹配付款额)。未匹配的取得发票 ≈ 还要付给供应商的钱。
    """
    payments = [r for r in bank_rows if r["direction"] == "out"]
    used_ids: set[int] = set()
    matched_total = 0.0
    for inv in receive_rows:
        try:
            inv_date = datetime.strptime(inv["inv_date"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            inv["matched"] = False
            continue
        matched = False
        for pay in payments:
            if pay["id"] in used_ids:
                continue
            if abs(float(pay["amount"]) - float(inv["amount"])) > AMOUNT_TOLERANCE:
                continue
            try:
                pay_date = datetime.strptime(pay["txn_date"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if abs((pay_date - inv_date).days) > MATCH_WINDOW_DAYS:
                continue
            if not _name_similar(inv["counterparty"], pay["counterparty"] or ""):
                continue
            used_ids.add(pay["id"])
            matched = True
            matched_total += float(inv["amount"])
            break
        inv["matched"] = matched
    return receive_rows, matched_total


def compute_months_left(curve: list[dict[str, Any]], start_cash: float) -> float | None:
    """从三场景曲线折算"还能撑几个月"：现金首次 < 0 的时点，月内线性插值。

    展望期内未跌破 → None（前端显示"6 个月以上"）。绝不给"某月必缺钱"伪精确单点，
    调用方必须并列展示 中性/悲观 两个数。
    """
    if start_cash < 0:
        return 0.0
    prev = start_cash
    for idx, point in enumerate(curve):
        cash = point["cash"]
        if cash < 0:
            burn = prev - cash  # 当月消耗（>0 才可能跌破）
            if burn <= 0:
                return float(idx + 1)
            return round(idx + (prev / burn), 1)
        prev = cash
    return None


def ar_suggest(max_days: int) -> str:
    """应收建议动作（规则话术，账龄越久动作越硬）。"""
    if max_days > 90:
        return "考虑法务 / 上门"
    if max_days > 60:
        return "上门催收 · 暂停赊账"
    if max_days > 30:
        return "加紧催收"
    return "正常跟进"


def build_headline(*, runway: dict[str, Any] | None, aging: dict[str, Any],
                   collection: dict[str, Any], ar_top: list[dict[str, Any]],
                   payable: dict[str, Any] | None, recent_flow: dict[str, float] | None) -> list[str]:
    """老板三句话（本地规则生成，零 AI 依赖）：现金 / 应收 / 生意（+应付）。"""

    def wan(v: float | None) -> str:
        return f"{v / 10000:,.1f} 万" if v is not None else "—"

    lines: list[str] = []
    # 1) 现金句
    if runway:
        ml = runway.get("months_left") or {}
        neutral, pessimistic = ml.get("neutral"), ml.get("pessimistic")
        cash_txt = f"账上还有 {wan(runway['start_cash'])} 元"
        if neutral is None and pessimistic is None:
            cash_txt += "，按现在节奏 6 个月内现金充足"
        elif pessimistic is not None:
            cash_txt += f"，按现在节奏大约还能撑 {neutral} 个月（最差情况 {pessimistic} 个月）"
        else:
            cash_txt += f"，按现在节奏大约还能撑 {neutral} 个月（最差情况 6 个月以上）"
        lines.append(cash_txt + "。")
    else:
        lines.append("还没有银行流水，看不到账上现金——先到「导入」页丢入银行流水。")
    # 2) 应收句
    if aging["total_receivable"] > 0 and ar_top:
        top = ar_top[0]
        overdue = aging["buckets"][3]["amount"]
        lines.append(
            f"{aging['unmatched_count']} 张发票共 {wan(aging['total_receivable'])} 元没收回来"
            f"（其中拖了 90 天以上的 {wan(overdue)} 元）；最该盯的是「{top['name']}」"
            f"{wan(top['amount'])} 元、最长拖了 {top['max_days']} 天。"
        )
    elif aging["total_issued"] > 0:
        lines.append(f"开出去的票 {wan(aging['total_issued'])} 元基本都收回来了，回款情况不错。")
    else:
        lines.append("还没有发票数据，看不到谁欠你钱——导入发票明细后这里会给出催收重点。")
    # 3) 生意句（滚动 30 天 vs 前 30 天）
    if recent_flow:
        inflow, outflow, prev_net = recent_flow["in30"], recent_flow["out30"], recent_flow["prev_net30"]
        net = inflow - outflow
        if prev_net is not None and abs(prev_net) >= 1:
            change = net - prev_net
            trend = f"，比前 30 天多收 {wan(change)} 元" if change >= 0 else f"，比前 30 天少收 {wan(-change)} 元"
        else:
            trend = ""
        lines.append(f"最近 30 天：收进 {wan(inflow)} 元、付出 {wan(outflow)} 元、净 {wan(net)} 元{trend}。")
    # 4) 应付句（有取得发票未付时）
    if payable and payable.get("has_data") and payable["total"] > 0:
        top_sup = payable["suppliers"][0] if payable["suppliers"] else None
        extra = f"，最大一笔「{top_sup['name']}」{wan(top_sup['amount'])} 元" if top_sup else ""
        lines.append(f"另外估计还有 {wan(payable['total'])} 元供应商款没付{extra}——排现金流时别漏了。")
    return lines


def build_action_items(*, runway: dict[str, Any] | None, aging: dict[str, Any],
                       ar_top: list[dict[str, Any]], payable: dict[str, Any] | None,
                       fixed: dict[str, Any], this_month: str) -> list[dict[str, str]]:
    """本周要办清单（规则生成）：现金 / 催收 / 停赊 / 备款 / 付款。"""

    def wan(v: float) -> str:
        return f"{v / 10000:,.1f} 万"

    items: list[dict[str, str]] = []
    # 1) 现金行动（最高优先级）
    if runway:
        ml = runway.get("months_left") or {}
        pessimistic, neutral = ml.get("pessimistic"), ml.get("neutral")
        if runway["start_cash"] < runway["safety_line"]:
            items.append({"level": "high",
                          "text": f"账上现金 {wan(runway['start_cash'])} 元已低于安全线（{wan(runway['safety_line'])} 元）："
                                  f"本周先安排资金——催收 TOP 客户或联系银行。"})
        elif pessimistic is not None and pessimistic < 2:
            items.append({"level": "high",
                          "text": f"最差情况只够撑 {pessimistic} 个月：启动备用授信 / 催收，别等缺钱再动。"})
        elif neutral is not None and neutral < 3:
            items.append({"level": "mid",
                          "text": f"中性估计还能撑 {neutral} 个月：把大额支出排个优先级，回款盯紧一点。"})
        # 大额支出备款（本月与下月）
        for month in (this_month, add_months(this_month, 1)):
            ov = fixed["overrides"].get(month)
            if ov and ov["amount"] > 0:
                note = f"（{ov['note']}）" if ov["note"] else ""
                items.append({"level": "mid",
                              "text": f"{month} 要额外付出 {wan(ov['amount'])} 元{note}，提前备款。"})
    # 2) 催收行动（90+ 与高风险）
    for cust in ar_top:
        if len([i for i in items if i["text"].startswith("催收")]) >= ACTION_TOP_N:
            break
        if cust["buckets"]["90+"] > 0:
            items.append({"level": "high" if cust["buckets"]["90+"] >= 50000 else "mid",
                          "text": f"催收：「{cust['name']}」{wan(cust['buckets']['90+'])} 元已拖 90 天以上"
                                  f"（共欠 {wan(cust['amount'])} 元）——先电话再函件，留好凭证。"})
        elif cust["high_risk"]:
            items.append({"level": "mid",
                          "text": f"「{cust['name']}」欠 {wan(cust['amount'])} 元、占应收 {cust['share']}%："
                                  f"先暂停赊账，收款后再供货。"})
    # 3) 供应商付款（最长未付 > 45 天，保供应）
    if payable and payable.get("has_data"):
        for sup in payable["suppliers"]:
            if len([i for i in items if i["text"].startswith("付款")]) >= ACTION_TOP_N:
                break
            if sup["days_max"] > 45:
                items.append({"level": "mid",
                              "text": f"付款：「{sup['name']}」{wan(sup['amount'])} 元拿票 {sup['days_max']} 天没付"
                                      f"——安排一下，别影响供货。"})
    if not items:
        items.append({"level": "low", "text": "本周没有必须处理的资金事项：节奏保持，记得月底前更新一次流水。"})
    return items


# ---------------------------------------------------------------------------
# 主入口：构建驾驶舱数据
# ---------------------------------------------------------------------------

def build_dashboard(db_path: str, company_id: int = 1) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _build_dashboard(conn, company_id)
    finally:
        conn.close()


def _build_dashboard(conn: sqlite3.Connection, company_id: int = 1) -> dict[str, Any]:
    today = date.today()
    this_month = _month_key(today)
    settings = load_settings(conn)
    fixed = load_fixed_expenses(conn, company_id)

    bank_rows = [dict(r) for r in conn.execute(
        "SELECT * FROM bank_txns WHERE company_id=?", (company_id,)).fetchall()]
    invoice_rows_all = [dict(r) for r in conn.execute(
        "SELECT * FROM invoice_rows WHERE company_id=?", (company_id,)).fetchall()]

    # v1.0.5：先做往来单位归一（别名合并），再把「对方是本公司自己」的发票剔出应收/应付。
    # 银行流水不剔（钱是真发生的），只做名称归一供匹配用。
    alias_map = load_counterparty_aliases(conn, company_id)
    self_names = load_self_names(conn, company_id)
    _apply_counterparty_alias(bank_rows, alias_map)
    _apply_counterparty_alias(invoice_rows_all, alias_map)

    invoice_rows: list[dict[str, Any]] = []
    self_excluded = 0
    for row in invoice_rows_all:
        if is_self_name(row.get("counterparty_raw"), self_names):
            self_excluded += 1
            continue
        invoice_rows.append(row)

    issue_rows = [r for r in invoice_rows if r["kind"] == "issue"]
    receive_rows = [r for r in invoice_rows if r["kind"] == "receive"]

    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta").fetchall()}

    # 累积存储：数据截至 = 最新业务日期；导入时间 = 最新一次导入（本公司）
    last_bank_date = conn.execute(
        "SELECT MAX(txn_date) FROM bank_txns WHERE company_id=?", (company_id,)
    ).fetchone()[0] or ""
    last_inv_date = conn.execute(
        "SELECT MAX(inv_date) FROM invoice_rows WHERE company_id=?", (company_id,)
    ).fetchone()[0] or ""
    last_import = max(
        [r[0] for r in conn.execute(
            "SELECT MAX(imported_at) FROM bank_txns WHERE company_id=?"
            " UNION ALL SELECT MAX(imported_at) FROM invoice_rows WHERE company_id=?",
            (company_id, company_id),
        ).fetchall() if r[0]],
        default="",
    )

    dashboard: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": last_bank_date or last_inv_date,
        "last_imported_at": last_import,
        "has_bank": bool(bank_rows),
        "has_invoice": bool(invoice_rows_all),
        "warnings": [],
        "self_excluded_invoices": self_excluded,
        "counterparty_aliases": [
            {"alias": a, "canonical": c} for a, c in sorted(alias_map.items())
        ],
    }
    # v1.0.6：这条不再推到驾驶舱（说明性文字不上主界面），只作为数据字段返回

    # ---- 银行侧：当前现金、月度收支 ----
    monthly_flow: dict[str, dict[str, float]] = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    cash_now: float | None = None
    if bank_rows:
        for r in bank_rows:
            if not r["txn_date"]:
                continue
            mk = r["txn_date"][:7]
            monthly_flow[mk]["in" if r["direction"] == "in" else "out"] += float(r["amount"])
        with_balance = [r for r in bank_rows if r["balance_after"] is not None]
        if with_balance:
            # 同日多笔时按文件顺序（id）取最后一笔带余额的行
            latest = max(with_balance, key=lambda r: (r["txn_date"], r["txn_time"] or "", r["id"]))
            cash_now = float(latest["balance_after"])
        else:
            try:
                opening = float(meta.get(f"bank_opening_sum:{company_id}") or 0)
                cash_now = opening + sum(f["in"] - f["out"] for f in monthly_flow.values())
                dashboard["warnings"].append("流水无余额列，当前现金=期初余额合计+累计收支推算")
            except ValueError:
                pass

    months_sorted = sorted(monthly_flow)
    trend_months = months_sorted[-TREND_MONTHS:]
    dashboard["cash_trend"] = [
        {"month": m, "in": round(monthly_flow[m]["in"], 2),
         "out": round(monthly_flow[m]["out"], 2),
         "net": round(monthly_flow[m]["in"] - monthly_flow[m]["out"], 2)}
        for m in trend_months
    ]

    # 近 3 个完整月净流均值（不含当月）
    past_months = [m for m in months_sorted if m < this_month][-3:]
    if past_months:
        net_avg = sum(monthly_flow[m]["in"] - monthly_flow[m]["out"] for m in past_months) / len(past_months)
    else:
        net_avg = 0.0
        if bank_rows:
            dashboard["warnings"].append("缺少完整历史月份，经营净流基线按 0 处理，跑道偏保守")

    # ---- 应收：轻匹配 + 账龄 ----
    issue_rows, matched_total = match_receivables(issue_rows, bank_rows)
    total_issued = sum(float(r["amount"]) for r in issue_rows)
    unmatched = [r for r in issue_rows if not r["matched"]]
    total_receivable = sum(float(r["amount"]) for r in unmatched)

    bucket_amount: dict[str, float] = {b: 0.0 for b in BUCKETS}
    bucket_count: dict[str, int] = {b: 0 for b in BUCKETS}
    by_customer: dict[str, dict[str, Any]] = {}
    for r in unmatched:
        try:
            inv_date = datetime.strptime(r["inv_date"], "%Y-%m-%d").date()
            days = (today - inv_date).days
        except ValueError:
            days = 0
        b = bucket_for(max(days, 0))
        bucket_amount[b] += float(r["amount"])
        bucket_count[b] += 1
        cust = by_customer.setdefault(r["counterparty"], {
            "name": r["counterparty"], "amount": 0.0, "count": 0,
            "buckets": {bb: 0.0 for bb in BUCKETS}, "max_days": 0,
        })
        cust["amount"] += float(r["amount"])
        cust["count"] += 1
        cust["buckets"][b] += float(r["amount"])
        cust["max_days"] = max(cust["max_days"], max(days, 0))

    dashboard["aging"] = {
        "total_receivable": round(total_receivable, 2),
        "total_issued": round(total_issued, 2),
        "matched_total": round(matched_total, 2),
        "unmatched_count": len(unmatched),
        "buckets": [
            {"bucket": b, "amount": round(bucket_amount[b], 2), "count": bucket_count[b]}
            for b in BUCKETS
        ],
    }

    # ---- 应收 TOP（高风险 = 有 61 天以上账龄的应收且占其应收一半以上）----
    reverse_aliases = alias_reverse_map(alias_map)
    customers = sorted(by_customer.values(), key=lambda c: c["amount"], reverse=True)
    top_list: list[dict[str, Any]] = []
    for idx, cust in enumerate(customers[:10]):
        old_amount = cust["buckets"]["61-90"] + cust["buckets"]["90+"]
        risky = cust["amount"] > 0 and old_amount / cust["amount"] >= 0.5
        top_list.append({
            "rank": idx + 1,
            "name": cust["name"],
            "alias_names": reverse_aliases.get(cust["name"], []),
            "amount": round(cust["amount"], 2),
            "share": round(cust["amount"] / total_receivable * 100, 1) if total_receivable else 0.0,
            "overdue_90": round(cust["buckets"]["90+"], 2),
            "high_risk": bool(risky and (total_receivable == 0 or cust["amount"] / total_receivable >= 0.1
                                         or cust["amount"] >= 50000)),
            "max_days": cust["max_days"],
            "suggest": ar_suggest(cust["max_days"]),
            "buckets": {b: round(v, 2) for b, v in cust["buckets"].items()},
        })
    dashboard["ar_top"] = top_list

    # ---- 回款健康度 ----
    dashboard["collection"] = {
        "rate_overall": round(matched_total / total_issued * 100, 1) if total_issued else None,
        "received_total": round(matched_total, 2),
        "issued_total": round(total_issued, 2),
        "monthly": [],  # 按月：开具额 vs 已匹配收款额
    }
    inv_monthly: dict[str, float] = defaultdict(float)
    for r in issue_rows:
        inv_monthly[r["inv_date"][:7]] += float(r["amount"])
    recv_monthly: dict[str, float] = defaultdict(float)
    # 重新执行一次匹配收集"已收月份"（与 match_receivables 同规则，供趋势展示）
    used_ids: set[int] = set()
    for inv in sorted(issue_rows, key=lambda r: r["inv_date"]):
        try:
            inv_date = datetime.strptime(inv["inv_date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        for rcpt in bank_rows:
            if rcpt["direction"] != "in" or id(rcpt) in used_ids:
                continue
            if abs(rcpt["amount"] - inv["amount"]) > AMOUNT_TOLERANCE:
                continue
            try:
                rcpt_date = datetime.strptime(rcpt["txn_date"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if abs((rcpt_date - inv_date).days) > MATCH_WINDOW_DAYS:
                continue
            if not _name_similar(inv["counterparty"], rcpt["counterparty"] or ""):
                continue
            used_ids.add(id(rcpt))
            recv_monthly[rcpt["txn_date"][:7]] += float(inv["amount"])
            break
    trend_all = sorted(set(inv_monthly) | set(recv_monthly))[-TREND_MONTHS:]
    dashboard["collection"]["monthly"] = [
        {"month": m, "issued": round(inv_monthly.get(m, 0.0), 2),
         "received": round(recv_monthly.get(m, 0.0), 2),
         "rate": round(recv_monthly.get(m, 0.0) / inv_monthly[m] * 100, 1)
         if inv_monthly.get(m) else None}
        for m in trend_all
    ]

    # ---- 现金跑道（三场景，展望 6 个月）----
    dashboard["runway"] = build_runway(
        cash_now=cash_now,
        net_avg=net_avg,
        fixed=fixed,
        aging=dashboard["aging"],
        rates=settings["recovery_rates"],
        safety_months=settings["safety_months"],
        start_month=this_month,
        fixed_in_cashflow=settings["fixed_in_cashflow"],
    ) if (cash_now is not None or bank_rows) else None
    if dashboard["runway"]:
        dashboard["runway"]["months_left"] = {
            key: compute_months_left(curve["curve"], dashboard["runway"]["start_cash"])
            for key, curve in dashboard["runway"]["scenarios"].items()
        }

    # ---- 项目盈利（条件卡：发票带项目字段才显）----
    has_project = any(r.get("project") for r in invoice_rows)
    dashboard["has_project_field"] = has_project
    if has_project:
        projects: dict[str, dict[str, float]] = defaultdict(lambda: {"issue": 0.0, "receive": 0.0})
        for r in invoice_rows:
            p = projects[r["project"] or "未标注项目"]
            p["issue" if r["kind"] == "issue" else "receive"] += float(r["amount"])
        dashboard["projects"] = [
            {"project": name,
             "income": round(v["issue"], 2),
             "cost": round(v["receive"], 2),
             "gross": round(v["issue"] - v["receive"], 2)}
            for name, v in sorted(projects.items(), key=lambda kv: kv[1]["issue"], reverse=True)
        ]
    else:
        dashboard["projects"] = None

    dashboard["fixed_expenses"] = fixed
    dashboard["safety_months"] = settings["safety_months"]
    dashboard["fixed_in_cashflow"] = settings["fixed_in_cashflow"]
    dashboard["recovery_rates"] = settings["recovery_rates"]

    # ---- v1.0.1 应付侧（我欠供应商）：取得发票 − 银行已付 ----
    receive_matched, pay_matched_total = match_payables(receive_rows, bank_rows)
    total_received_inv = sum(float(r["amount"]) for r in receive_rows)
    unpaid = [r for r in receive_rows if not r["matched"]]
    by_supplier: dict[str, dict[str, Any]] = {}
    for r in unpaid:
        try:
            days = (today - datetime.strptime(r["inv_date"], "%Y-%m-%d").date()).days
        except ValueError:
            days = 0
        sup = by_supplier.setdefault(r["counterparty"], {
            "name": r["counterparty"], "amount": 0.0, "count": 0, "days_max": 0,
        })
        sup["amount"] += float(r["amount"])
        sup["count"] += 1
        sup["days_max"] = max(sup["days_max"], max(days, 0))
    dashboard["payable"] = {
        "has_data": bool(receive_rows),
        "total": round(sum(float(r["amount"]) for r in unpaid), 2),
        "total_received_inv": round(total_received_inv, 2),
        "matched_total": round(pay_matched_total, 2),
        "unpaid_count": len(unpaid),
        "suppliers": [
            {"name": s["name"], "amount": round(s["amount"], 2),
             "alias_names": reverse_aliases.get(s["name"], []),
             "count": s["count"], "days_max": s["days_max"]}
            for s in sorted(by_supplier.values(), key=lambda x: x["amount"], reverse=True)[:10]
        ],
    }

    # ---- v1.0.1 支出结构（钱花哪去了）：近 6 月分类 + Top 支出对象 ----
    spend_rows = [r for r in bank_rows if r["direction"] == "out" and r["txn_date"]]
    trend_month_keys = months_sorted[-TREND_MONTHS:]
    spend_in_trend = [r for r in spend_rows if r["txn_date"][:7] in set(trend_month_keys)]
    cat_totals: dict[str, float] = defaultdict(float)
    cat_monthly: dict[str, dict[str, float]] = defaultdict(lambda: {m: 0.0 for m in trend_month_keys})
    by_target: dict[str, dict[str, Any]] = {}
    for r in (spend_in_trend or spend_rows):
        cat = classify_spend(r.get("counterparty_raw") or r["counterparty"] or "", r["summary"] or "")
        amount = float(r["amount"])
        cat_totals[cat] += amount
        if r["txn_date"][:7] in cat_monthly[cat]:
            cat_monthly[cat][r["txn_date"][:7]] += amount
        tgt = by_target.setdefault(r["counterparty"] or "（未记对方）", {"name": r["counterparty"] or "（未记对方）", "amount": 0.0})
        tgt["amount"] += amount
    spend_grand = sum(cat_totals.values())
    dashboard["spend_breakdown"] = {
        "months": trend_month_keys,
        "total": round(spend_grand, 2),
        "categories": [
            {"name": name, "amount": round(v, 2),
             "share": round(v / spend_grand * 100, 1) if spend_grand else 0.0}
            for name, v in sorted(cat_totals.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "monthly": {name: {m: round(v, 2) for m, v in months.items()} for name, months in cat_monthly.items()},
        "top_targets": [
            {"name": t["name"], "amount": round(t["amount"], 2)}
            for t in sorted(by_target.values(), key=lambda x: x["amount"], reverse=True)[:8]
        ],
    }

    # ---- v1.0.1 近 30 天大额支出（单笔 ≥ max(月均支出×30%, 1万)）----
    cutoff = today.toordinal() - BIG_SPEND_LOOKBACK_DAYS
    recent_out = [r for r in spend_rows
                  if r["txn_date"] and _ord_of(r["txn_date"]) is not None
                  and _ord_of(r["txn_date"]) >= cutoff]
    recent_out_total = sum(float(r["amount"]) for r in recent_out)
    monthly_avg_out = (sum(f["out"] for f in monthly_flow.values()) / len(monthly_flow)) if monthly_flow else 0.0
    big_threshold = max(monthly_avg_out * 0.3, BIG_SPEND_MIN)
    big_list = sorted(
        [r for r in recent_out if float(r["amount"]) >= big_threshold],
        key=lambda r: float(r["amount"]), reverse=True,
    )[:10]
    dashboard["big_spends"] = {
        "threshold": round(big_threshold, 2),
        "total_out_30d": round(recent_out_total, 2),
        "items": [
            {"date": r["txn_date"], "counterparty": r["counterparty"] or "（未记对方）",
             "amount": round(float(r["amount"]), 2), "summary": r["summary"] or ""}
            for r in big_list
        ],
    }

    # ---- v1.0.1 老板三句话 + 本周行动清单（本地规则，零 AI 依赖）----
    recent_flow = None
    if bank_rows:
        in30 = out30 = prev_net = 0.0
        has_prev = False
        for r in bank_rows:
            o = _ord_of(r.get("txn_date") or "")
            if o is None:
                continue
            amount = float(r["amount"])
            if o >= today.toordinal() - 30:
                if r["direction"] == "in":
                    in30 += amount
                else:
                    out30 += amount
            elif o >= today.toordinal() - 60:
                has_prev = True
                prev_net += amount if r["direction"] == "in" else -amount
        recent_flow = {"in30": in30, "out30": out30, "net30": in30 - out30,
                       "prev_net30": prev_net if has_prev else None}
    dashboard["recent_flow"] = recent_flow
    dashboard["headline"] = build_headline(
        runway=dashboard["runway"], aging=dashboard["aging"],
        collection=dashboard["collection"], ar_top=top_list,
        payable=dashboard["payable"], recent_flow=recent_flow,
    )
    dashboard["action_items"] = build_action_items(
        runway=dashboard["runway"], aging=dashboard["aging"], ar_top=top_list,
        payable=dashboard["payable"], fixed=fixed, this_month=this_month,
    )
    return dashboard


def build_runway(*, cash_now: float | None, net_avg: float, fixed: dict[str, Any],
                 aging: dict[str, Any], rates: dict[str, dict[str, float]],
                 safety_months: float, start_month: str,
                 fixed_in_cashflow: bool = True) -> dict[str, Any]:
    """三场景现金跑道。回收分布假设：
    0-30 天桶当月（第 1 月）回完；31-60 桶第 1-2 月各半；61-90 桶第 1-3 月各 1/3；
    90+ 桶 6 个月均摊。回收率 = 桶 × 场景（§4.1 表）。
    """
    monthly_fixed = fixed["monthly_total"]
    safety_line = monthly_fixed * safety_months
    start_cash = cash_now if cash_now is not None else 0.0
    # 已在流水里体现的固定支出不再重复扣，只有"某月大额支出"是流水外的增量
    regular_fixed = 0.0 if fixed_in_cashflow else monthly_fixed

    # 各桶期望回款总额按时间摊布（比例表）
    spread = {
        "0-30": [1.0],
        "31-60": [0.5, 0.5],
        "61-90": [1 / 3, 1 / 3, 1 / 3],
        "90+": [1 / 6] * 6,
    }
    bucket_amount = {b["bucket"]: b["amount"] for b in aging["buckets"]}

    scenarios: dict[str, list[dict[str, Any]]] = {}
    for scenario in ("optimistic", "neutral", "pessimistic"):
        cash = start_cash
        curve: list[dict[str, Any]] = []
        for t in range(FORECAST_MONTHS):
            month = add_months(start_month, t)
            collected = 0.0
            for bucket, ratios in spread.items():
                expected = bucket_amount.get(bucket, 0.0) * rates[scenario][bucket]
                if t < len(ratios):
                    collected += expected * ratios[t]
            extra_fixed = fixed["overrides"].get(month, {}).get("amount", 0.0)
            fixed_t = regular_fixed + extra_fixed
            cash = cash + collected + net_avg - fixed_t
            curve.append({
                "month": month,
                "cash": round(cash, 2),
                "collected": round(collected, 2),
                "fixed": round(fixed_t, 2),          # 本月实际扣减的固定支出（不含已含在净流里的常规部分）
                "extra_fixed": round(extra_fixed, 2),  # 本月额外大额支出
                "net_op": round(net_avg, 2),
                "below_safety": cash < safety_line,
            })
        scenarios[scenario] = curve

    # 中性场景首个跌破安全线的月份 → "预计第 N 月告急（中性）"
    alert_month, alert_index = None, None
    for idx, point in enumerate(scenarios["neutral"]):
        if point["below_safety"]:
            alert_month, alert_index = point["month"], idx + 1
            break

    return {
        "start_cash": round(start_cash, 2),
        "monthly_fixed": round(monthly_fixed, 2),
        "fixed_in_cashflow": bool(fixed_in_cashflow),
        "safety_line": round(safety_line, 2),
        "net_op_avg": round(net_avg, 2),
        "months": FORECAST_MONTHS,
        "scenarios": {
            key: {"label": SCENARIO_LABELS[key], "curve": curve}
            for key, curve in scenarios.items()
        },
        "alert": {
            "scenario": "neutral",
            "month": alert_month,
            "index": alert_index,  # 第 N 月（1-based），None=展望期内安全
            "text": (f"中性场景下，预计 {alert_month}（第 {alert_index} 月）现金跌破安全线"
                     f"（{safety_line:,.2f} 元）") if alert_month else "展望 6 个月内，中性场景现金高于安全线",
        },
        "assumption_note": "曲线基于回款假设（按账龄桶×情景回收率摊布），不是事实承诺；请三场景并列查看",
    }


def what_if_client(db_path: str, client_name: str, company_id: int = 1,
                   scenario: str = "neutral") -> dict[str, Any]:
    """情景问答：假设某客户回款全部拖到 90 天桶 → 重算三场景。

    用于 AI 情景问答（§5 场景四）：本地模型先重算，AI 只做解释。按公司隔离。
    """
    dashboard = build_dashboard(db_path, company_id)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        settings = load_settings(conn)
    finally:
        conn.close()
    rates = {k: dict(v) for k, v in settings["recovery_rates"].items()}
    # 找到该客户的应收，把其金额从原桶挪到 90+
    moved = 0.0
    for cust in dashboard["ar_top"]:
        if cust["name"] == client_name or _name_similar(cust["name"], client_name):
            moved = cust["amount"]
            break
    if moved <= 0:
        return {"found": False, "message": f"未在应收名单中找到「{client_name}」", "dashboard": dashboard}
    # 从账龄桶中扣除该客户金额并全部记入 90+
    buckets = {b["bucket"]: b["amount"] for b in dashboard["aging"]["buckets"]}
    remaining = moved
    for b in reversed(BUCKETS):  # 从最老的桶开始扣，接近真实挪移
        take = min(buckets[b], remaining)
        buckets[b] -= take
        remaining -= take
        if remaining <= 0:
            break
    buckets["90+"] = buckets.get("90+", 0.0) + moved
    new_aging = dict(dashboard["aging"])
    new_aging["buckets"] = [
        {"bucket": b, "amount": round(buckets.get(b, 0.0), 2),
         "count": next((c["count"] for c in dashboard["aging"]["buckets"] if c["bucket"] == b), 0)}
        for b in BUCKETS
    ]
    fixed = dashboard["fixed_expenses"]
    runway = build_runway(
        cash_now=dashboard["runway"]["start_cash"],
        net_avg=dashboard["runway"]["net_op_avg"],
        fixed=fixed,
        aging=new_aging,
        rates=rates,
        safety_months=dashboard["safety_months"],
        start_month=_month_key(date.today()),
        fixed_in_cashflow=dashboard["runway"].get("fixed_in_cashflow", True),
    )
    runway["months_left"] = {
        key: compute_months_left(curve["curve"], runway["start_cash"])
        for key, curve in runway["scenarios"].items()
    }
    return {
        "found": True,
        "client": client_name,
        "moved_amount": round(moved, 2),
        "original_alert": dashboard["runway"]["alert"],
        "new_alert": runway["alert"],
        "runway": runway,
        "dashboard": dashboard,
    }
