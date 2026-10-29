"""双源导入解析（技术说明 §3，2026-10-16 修订：长期累积存储）：银行流水 xlsx + 发票明细。

设计要点：
- 银行流水：复用移植的 bank_parsers（工/中/民生/招行 + 通用表头指纹），
  多账户合并，去重键 日期+金额+对方+摘要（有流水号优先流水号）。
- 发票明细：来源任选（发票平台导出 / Excel / 小河狸发票助手导出）。
  开具=应收销售，取得=应付成本；可选项目字段。
- 落库（按用户要求修订）：银行流水/发票明细是**连续业务数据，长期累积存储**，
  像"日记账 + 台账"一样可查可审；重复导入自动去重：
  * 银行（照资金助手 bank_api.py）：批内去重 → 库内 unique_key 查重跳过 →
    无流水号记录做"疑似重复"标记（同日同向同金额但对方/摘要不同）→ 记导入批次。
  * 发票（照发票助手 api_server.py upsert）：unique_key 已存在 → 更新字段（updated），
    否则插入（inserted）→ 记导入批次。
- 每次导入落一条 import_batches 批次记录，台账页可查导入历史。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import bank_parsers as bp

# ---------------------------------------------------------------------------
# 发票明细表头规则
# v1.0.3（参考小河狸发票助手识别模式）：支持 购买方/销售方 双列布局——
# 谁是本公司由 公司全称/税号 判定，对方取另一侧；方向自动推导（自己是销方=开具，是购方=取得）。
# ---------------------------------------------------------------------------
INVOICE_HEADER_RULES: list[tuple[str, list[str]]] = [
    ("inv_date", ["开票日期", "开票日", "发票日期", "填开日期", "日期"]),
    ("direction", ["进销方向", "方向", "类型", "类别", "开票类型"]),
    ("buyer_name", ["购买方名称", "购方名称", "购买方", "购方单位", "购方"]),
    ("buyer_tax", ["购买方税号", "购方税号", "购买方纳税人识别号", "购买方识别号"]),
    ("seller_name", ["销售方名称", "销方名称", "销售方", "销方单位", "销方", "开票方名称"]),
    ("seller_tax", ["销售方税号", "销方税号", "销售方纳税人识别号", "销售方识别号"]),
    ("counterparty", [
        "对方单位", "对方名称", "往来单位", "客户名称", "客户",
        "供应商名称", "供应商", "单位名称",
    ]),
    ("amount", ["价税合计", "含税金额", "金额(含税)", "金额（含税）", "合计金额", "金额"]),
    ("tax", ["税额", "税金"]),
    ("project", ["项目名称", "项目", "项目编号", "工程项目"]),
    ("inv_no", ["发票号码", "发票号", "数电票号码"]),
]

# direction 字段取值 -> kind（开具=应收销售 / 取得=应付成本）
ISSUE_WORDS = {"开具", "销项", "销售", "开票", "正数", "销项发票", "开出"}
RECEIVE_WORDS = {"取得", "进项", "采购", "收票", "进项发票", "取得发票"}

INVOICE_MIN_CATEGORIES = 3  # 日期+对方+金额 三类即认定表头


def map_invoice_header(header: list[str]) -> dict[str, int]:
    """发票表头映射：与银行同思路，相等 > 开头 > 包含。"""
    mapping: dict[str, int] = {}
    used: set[int] = set()
    normalized = [bp.normalize_header_label(bp.cell_to_text(cell)) for cell in header]
    for field, candidates in INVOICE_HEADER_RULES:
        for mode in ("eq", "startswith", "contains"):
            for cand in candidates:
                cand_norm = bp.normalize_header_label(cand)
                for idx, head in enumerate(normalized):
                    if idx in used or not head:
                        continue
                    hit = head == cand_norm if mode == "eq" else (
                        head.startswith(cand_norm) if mode == "startswith" else cand_norm in head
                    )
                    if hit:
                        mapping[field] = idx
                        used.add(idx)
                        break
                if field in mapping:
                    break
            if field in mapping:
                break
    return mapping


def invoice_category_hits(col_map: dict[str, int]) -> int:
    return len(set(col_map) - {"inv_no"})


def find_invoice_header(rows: list[list[Any]], scan_limit: int = 20) -> tuple[int, dict[str, int]] | None:
    best: tuple[int, dict[str, int]] | None = None
    best_hits = 0
    for idx in range(min(len(rows), scan_limit)):
        col_map = map_invoice_header(rows[idx])
        hits = invoice_category_hits(col_map)
        if hits >= INVOICE_MIN_CATEGORIES and hits > best_hits:
            best = (idx, col_map)
            best_hits = hits
    return best


def normalize_counterparty(name: str) -> str:
    """对方单位归一化：去空白与括号差异，便于银行收款轻匹配。"""
    text = re.sub(r"\s+", "", str(name or ""))
    text = text.replace("（", "(").replace("）", ")")
    return text


def _clean_tax(t: str) -> str:
    return re.sub(r"[\s\-—_]", "", str(t or "")).upper()


def is_self_party(name: str, tax: str, self_name: str, self_tax: str) -> bool:
    """判定某一方（名称+税号）是否是本公司：税号相等优先，其次名称归一化后相等/互含（足够长）。"""
    st, pt = _clean_tax(self_tax), _clean_tax(tax)
    if st and pt and st == pt:
        return True
    n1, n2 = normalize_counterparty(name), normalize_counterparty(self_name)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    return (len(n1) >= 4 and n1 in n2) or (len(n2) >= 4 and n2 in n1)


def classify_direction(text: str) -> str | None:
    t = bp.normalize_header_label(text)
    if any(w in t for w in ISSUE_WORDS):
        return "issue"
    if any(w in t for w in RECEIVE_WORDS):
        return "receive"
    return None


def parse_invoice_workbook(path: Path, default_kind: str | None = None,
                           self_name: str = "", self_tax: str = "") -> dict[str, Any]:
    """解析发票明细文件（v1.0.3 参考发票助手识别模式）。

    - self_name / self_tax：本公司全称与税号（导入前已强制配置），用于识别"谁是本公司"。
    - 双列布局（购买方 + 销售方）：本公司在哪一侧，对方就是另一侧；
      方向自动推导——自己是销售方 = 开具（应收），自己是购买方 = 取得（应付）；
      两侧都不是本公司时按行内方向列，否则按 default_kind，对方按方向取另一侧。
    - 单列布局（只有"对方单位"类列）：对方若匹配本公司 → 跳过该行并警告（防止把自己记成客户/供应商）。

    返回 {file_kind, rows, warnings, source}
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".xls":
        sheets = bp.read_sheets_xls(path)
    elif suffix in {".xlsx", ".xlsm"}:
        sheets = bp.read_sheets_xlsx(path)
    else:
        raise ValueError(f"不支持的文件类型：{suffix}（仅支持 .xls / .xlsx）")

    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    kinds_seen: set[str] = set()
    skipped_self = 0

    for sheet_name, sheet_rows in sheets:
        found = find_invoice_header(sheet_rows)
        if not found:
            if sheet_rows and len(sheets) == 1:
                warnings.append(f"工作表「{sheet_name}」未识别到发票明细表头，已跳过")
            continue
        header_idx, col_map = found
        dual = "buyer_name" in col_map and "seller_name" in col_map

        for row_idx in range(header_idx + 1, len(sheet_rows)):
            row = sheet_rows[row_idx]
            if not any(bp.cell_to_text(c) for c in row):
                continue

            def cell(field: str) -> Any:
                i = col_map.get(field)
                return row[i] if i is not None and i < len(row) else None

            inv_date, _ = bp.parse_date_text(cell("inv_date"))
            if not inv_date:
                continue
            amount = bp.parse_money(cell("amount"))
            if amount is None or amount == 0:
                continue

            kind = None
            counterparty = ""
            if dual:
                buyer = bp.cell_to_text(cell("buyer_name"))
                seller = bp.cell_to_text(cell("seller_name"))
                buyer_tax = bp.cell_to_text(cell("buyer_tax")) if "buyer_tax" in col_map else ""
                seller_tax = bp.cell_to_text(cell("seller_tax")) if "seller_tax" in col_map else ""
                if is_self_party(seller, seller_tax, self_name, self_tax):
                    # 本公司开出的票 → 对方是购买方（客户）
                    kind, counterparty = "issue", buyer
                elif is_self_party(buyer, buyer_tax, self_name, self_tax):
                    # 对方开给本公司的票 → 对方是销售方（供应商）
                    kind, counterparty = "receive", seller
                else:
                    kind = classify_direction(bp.cell_to_text(cell("direction"))) if "direction" in col_map else None
                    if kind is None:
                        kind = default_kind or "issue"
                    counterparty = buyer if kind == "issue" else seller
            else:
                counterparty = bp.cell_to_text(cell("counterparty"))
                if is_self_party(counterparty, "", self_name, self_tax):
                    skipped_self += 1
                    continue
                kind = classify_direction(bp.cell_to_text(cell("direction"))) if "direction" in col_map else None
                if kind is None:
                    kind = default_kind or "issue"

            if not counterparty:
                continue
            tax = bp.parse_money(cell("tax")) if "tax" in col_map else None
            project = bp.cell_to_text(cell("project")) if "project" in col_map else ""
            inv_no = bp.cell_to_text(cell("inv_no")) if "inv_no" in col_map else ""
            amount = abs(amount)
            unique_key = f"inv:{kind}:{inv_no}" if inv_no else (
                f"inv:{kind}:{inv_date}:{amount:.2f}:{normalize_counterparty(counterparty)}"
            )
            rows.append({
                "kind": kind,
                "inv_date": inv_date,
                "counterparty": counterparty.strip(),
                "amount": round(amount, 2),
                "tax": round(tax, 2) if tax is not None else None,
                "project": project.strip(),
                "inv_no": inv_no.strip(),
                "unique_key": unique_key,
            })
            kinds_seen.add(kind)

    if skipped_self:
        warnings.append(f"有 {skipped_self} 行的对方是本公司自己，已自动跳过（避免把自己记成客户/供应商）")
    if not rows and not warnings:
        warnings.append("未解析到任何有效发票行：需包含 日期/对方单位/金额 三类列。"
                        "文件开头内容为：" + bp.summarize_rows_for_diagnosis(
                            sheets[0][1] if sheets else []))
    if len(kinds_seen) > 1 and default_kind:
        warnings.append("文件内同时含开具与取得行，已按 购买方/销售方/方向列 自动拆分")

    if len(kinds_seen) == 1:
        file_kind = next(iter(kinds_seen))
    elif "issue" in kinds_seen:
        file_kind = "mixed"
    elif default_kind:
        file_kind = default_kind
    else:
        file_kind = "issue"

    return {
        "file_kind": file_kind,
        "rows": rows,
        "warnings": warnings,
        "source": Path(path).name,
    }


# ---------------------------------------------------------------------------
# 落库（长期累积 + 去重，参考资金助手/发票助手）
# ---------------------------------------------------------------------------

def _new_batch(conn: sqlite3.Connection, company_id: int, kind: str, file_name: str) -> int:
    cur = conn.execute(
        "INSERT INTO import_batches (company_id, file_name, kind, raw_rows, inserted, duplicated,"
        " updated, suspicious, warnings_json, imported_at) VALUES (?,?,?,?,?,0,0,0,?,?)",
        (company_id, file_name, kind, 0, 0, "[]", datetime.now().isoformat(timespec="seconds")),
    )
    return int(cur.lastrowid)


def _finish_batch(conn: sqlite3.Connection, batch_id: int, *, raw_rows: int, inserted: int,
                  duplicated: int, updated: int, suspicious: int, warnings: list[str]) -> None:
    conn.execute(
        "UPDATE import_batches SET raw_rows=?, inserted=?, duplicated=?, updated=?, suspicious=?,"
        " warnings_json=? WHERE id=?",
        (raw_rows, inserted, duplicated, updated, suspicious,
         json.dumps(warnings, ensure_ascii=False), batch_id),
    )


def save_bank_incremental(conn: sqlite3.Connection, parse_results: list[dict[str, Any]],
                          source_name: str, company_id: int = 1) -> dict[str, Any]:
    """银行流水增量入库：批内去重 + 库内查重 + 疑似重复标记（照资金助手逻辑）。

    数据按公司隔离：去重/疑似重复检查均限定 company_id（同一笔流水可分别属于不同公司）。
    """
    batch_id = _new_batch(conn, company_id, "bank", source_name)
    now_text = datetime.now().isoformat(timespec="seconds")
    total_inserted = total_dup = total_suspicious = 0
    raw_rows = 0
    accounts: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    opening_sum = 0.0
    has_opening = False

    for sheet_result in parse_results:
        acct = sheet_result["account"]
        account_no = acct.get("account_no") or f"未识别-{sheet_result['sheet_name']}"
        accounts[account_no] = {
            "account_no": account_no,
            "account_name": acct.get("account_name", ""),
            "bank_name": acct.get("bank_name", ""),
            "txn_count": len(sheet_result["records"]),
        }
        if acct.get("opening_balance") is not None:
            opening_sum += acct["opening_balance"]
            has_opening = True
        warnings.extend(sheet_result.get("warnings", []))
        raw_rows += len(sheet_result["records"])

        seen_keys: set[str] = set()
        for rec in sheet_result["records"]:
            key = bp.unique_key_for(account_no, rec)
            # 1) 批内去重（同一文件可能含重复行）
            if key in seen_keys:
                total_dup += 1
                continue
            seen_keys.add(key)
            # 2) 库内查重（跨批次/跨文件重复导入；限定本公司）
            exists = conn.execute(
                "SELECT 1 FROM bank_txns WHERE company_id=? AND unique_key=?",
                (company_id, key),
            ).fetchone()
            if exists:
                total_dup += 1
                continue
            # 3) 疑似重复：无银行流水号的记录，同账户+同日+同向+同金额但对方/摘要不同
            dup_flag = ""
            if not rec.get("txn_ref"):
                similar = conn.execute(
                    """
                    SELECT 1 FROM bank_txns
                    WHERE company_id=? AND account_no=? AND txn_date=? AND direction=? AND amount=?
                      AND unique_key<>? AND (IFNULL(counterparty,'')<>? OR IFNULL(summary,'')<>?)
                    LIMIT 1
                    """,
                    (company_id, account_no, rec["txn_date"], rec["direction"], rec["amount"], key,
                     rec.get("counterparty_name", ""), rec.get("summary", "")),
                ).fetchone()
                if similar:
                    dup_flag = "疑似重复"
                    total_suspicious += 1
            conn.execute(
                """
                INSERT INTO bank_txns (company_id, account_no, account_name, txn_date, txn_time,
                                       direction, amount, balance_after, counterparty, summary,
                                       txn_ref, unique_key, dup_flag, imported_at, batch_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    company_id, account_no, acct.get("account_name", ""), rec["txn_date"],
                    rec["txn_time"], rec["direction"], rec["amount"], rec.get("balance_after"),
                    rec.get("counterparty_name", ""), rec.get("summary", ""),
                    rec.get("txn_ref", ""), key, dup_flag, now_text, batch_id,
                ),
            )
            total_inserted += 1

    _finish_batch(conn, batch_id, raw_rows=raw_rows, inserted=total_inserted,
                  duplicated=total_dup, updated=0, suspicious=total_suspicious,
                  warnings=warnings)
    # 期初余额合计（供引擎在流水缺余额列时推算当前现金；按公司覆盖式存储）
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"bank_opening_sum:{company_id}", repr(opening_sum) if has_opening else ""),
    )
    return {
        "raw_rows": raw_rows,
        "inserted": total_inserted,
        "duplicated": total_dup,
        "suspicious": total_suspicious,
        "accounts": list(accounts.values()),
        "warnings": warnings,
    }


def save_invoice_incremental(conn: sqlite3.Connection, parse_result: dict[str, Any],
                             default_kind: str, company_id: int = 1) -> dict[str, Any]:
    """发票增量入库：unique_key upsert（照发票助手逻辑，存在即更新）。按公司隔离。"""
    batch_id = _new_batch(conn, company_id, "invoice", parse_result.get("source", ""))
    now_text = datetime.now().isoformat(timespec="seconds")
    rows = parse_result["rows"]
    inserted = updated = 0
    for r in rows:
        exists = conn.execute(
            "SELECT id FROM invoice_rows WHERE company_id=? AND unique_key=?",
            (company_id, r["unique_key"]),
        ).fetchone()
        if exists:
            conn.execute(
                """
                UPDATE invoice_rows SET kind=?, inv_date=?, counterparty=?, amount=?, tax=?,
                                         project=?, inv_no=?, imported_at=?, batch_id=?
                WHERE company_id=? AND unique_key=?
                """,
                (r["kind"], r["inv_date"], r["counterparty"], r["amount"], r["tax"],
                 r["project"], r.get("inv_no", ""), now_text, batch_id,
                 company_id, r["unique_key"]),
            )
            updated += 1
        else:
            conn.execute(
                """
                INSERT INTO invoice_rows (company_id, kind, inv_date, counterparty, amount, tax,
                                          project, inv_no, unique_key, imported_at, batch_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (company_id, r["kind"], r["inv_date"], r["counterparty"], r["amount"], r["tax"],
                 r["project"], r.get("inv_no", ""), r["unique_key"], now_text, batch_id),
            )
            inserted += 1
    warnings = parse_result.get("warnings", [])
    _finish_batch(conn, batch_id, raw_rows=len(rows), inserted=inserted,
                  duplicated=0, updated=updated, suspicious=0, warnings=warnings)
    issue_n = sum(1 for r in rows if r["kind"] == "issue")
    return {
        "raw_rows": len(rows),
        "inserted": inserted,
        "updated": updated,
        "duplicated": 0,
        "issue_count": issue_n,
        "receive_count": len(rows) - issue_n,
        "has_project_field": any(r["project"] for r in rows),
        "warnings": warnings,
    }


def audit_bank_dates(conn: sqlite3.Connection, parse_results: list[dict[str, Any]],
                     company_id: int = 1) -> list[str]:
    """v1.0.8 日期体检（参考资金助手）：导入前先看日期有没有明显录错。

    三类问题：
    1) 未来日期 —— 比今天还晚，多半是年份/月份录错；
    2) 与已有数据错位 —— 本次有流水早于该账户已导入的最新日期，可能是补录，也可能日期填错；
    3) 整批比库里旧 —— 本次文件最新日期比库里最新还早一年以上，多半是导错文件或年份写错。
    """
    from datetime import date as _date
    today_text = _date.today().isoformat()
    notes: list[str] = []
    for sheet in parse_results:
        records = [r for r in (sheet.get("records") or []) if r.get("txn_date")]
        if not records:
            continue
        dates = [r["txn_date"] for r in records]
        future = [d for d in dates if d > today_text]
        if future:
            notes.append(
                f"工作表「{sheet['sheet_name']}」有 {len(future)} 笔日期晚于今天"
                f"（最晚 {max(future)}），请核对是否年份/月份录错"
            )
        account_no = (sheet.get("account") or {}).get("account_no") or ""
        if not account_no:
            continue
        row = conn.execute(
            "SELECT MIN(txn_date), MAX(txn_date) FROM bank_txns WHERE company_id=? AND account_no=?",
            (company_id, account_no),
        ).fetchone()
        existing_max = row[1] if row else None
        if not existing_max:
            continue
        batch_max, batch_min = max(dates), min(dates)
        earlier = [d for d in dates if d < existing_max]
        if earlier:
            notes.append(
                f"本次有 {len(earlier)} 笔早于该账户已有最新流水（{existing_max}，最早 {min(earlier)}）："
                "补录历史属正常，若是填错日期请先改文件再导"
            )
        if batch_max < existing_max and month_gap(batch_max, existing_max) >= 10:
            notes.append(
                f"整批日期比库里旧很多（本次最晚 {batch_max}，库里已到 {existing_max}），"
                "确认不是导错文件或年份写错"
            )
    return notes


def month_gap(early: str, late: str) -> int:
    try:
        return (int(late[:4]) - int(early[:4])) * 12 + (int(late[5:7]) - int(early[5:7]))
    except (ValueError, IndexError):
        return 0


def import_bank_file(db_path: Path, file_path: Path, company_id: int = 1) -> dict[str, Any]:
    parse_results = bp.parse_bank_workbook(file_path)
    if not parse_results:
        raise ValueError("未能从该文件解析出任何银行流水工作表。"
                         "请确认导出的是网银「明细/流水」而不是汇总页；支持 .xlsx / .xls / .csv。")
    # 所有表都没有流水行时也明确报错（parse_bank_workbook 已在 warnings 附上文件开头内容）
    if not any(r["records"] for r in parse_results):
        detail = "；".join(w for r in parse_results for w in r.get("warnings", [])[:2])
        raise ValueError(f"文件识别成功但没解析出流水行。{detail[:300]}")
    # 余额方向自检（移植自资金助手：不同银行借贷语义可能相反）
    for sheet_result in parse_results:
        if bp.detect_direction_inversion(sheet_result["records"]):
            for rec in sheet_result["records"]:
                if rec["direction_source"] in {"flag", "debit", "credit"}:
                    rec["direction"] = "in" if rec["direction"] == "out" else "out"
            sheet_result.setdefault("warnings", []).append(
                f"工作表「{sheet_result['sheet_name']}」借贷语义与余额变动相反，已自动翻转方向"
            )
    # v1.0.3：对方是本公司的流水给个提醒（钱是真的，但对方字段记录的是自己，多见于柜台/内部划转）
    self_name, self_tax = _get_self_info(db_path, company_id)
    if self_name:
        self_rows = sum(
            1 for r in parse_results for rec in r["records"]
            if is_self_party(rec.get("counterparty_name", ""), "", self_name, self_tax)
        )
        if self_rows:
            parse_results[0].setdefault("warnings", []).append(
                f"有 {self_rows} 笔流水的对方写的是本公司自己（常见于内部划转），"
                "已计入现金流，不参与客户/供应商识别"
            )
    conn = sqlite3.connect(db_path)
    try:
        # v1.0.8：入库前先体检日期（未来日期 / 比库里旧 / 与已有数据错位）
        date_notes = audit_bank_dates(conn, parse_results, company_id)
        if date_notes:
            parse_results[0].setdefault("warnings", []).extend(date_notes)
        summary = save_bank_incremental(conn, parse_results, file_path.name, company_id)
        conn.commit()
    finally:
        conn.close()
    return summary


def _get_self_info(db_path: Path, company_id: int) -> tuple[str, str]:
    """读本公司全称与税号（导入前已由 API 层校验过非空）。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT name, tax_no FROM companies WHERE id=?",
                           (company_id,)).fetchone()
        return (row[0] or "", row[1] or "") if row else ("", "")
    finally:
        conn.close()


def import_invoice_file(db_path: Path, file_path: Path, default_kind: str | None,
                        company_id: int = 1) -> dict[str, Any]:
    self_name, self_tax = _get_self_info(db_path, company_id)
    parse_result = parse_invoice_workbook(file_path, default_kind=default_kind,
                                          self_name=self_name, self_tax=self_tax)
    conn = sqlite3.connect(db_path)
    try:
        summary = save_invoice_incremental(conn, parse_result, default_kind or "issue", company_id)
        conn.commit()
    finally:
        conn.close()
    return summary
