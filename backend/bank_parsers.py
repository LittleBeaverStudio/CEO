#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""银行流水解析器：识别多家银行流水格式并归一化为标准交易记录。

设计要点：
1. 先在工作表前 20 行内定位表头行（按字段类别命中数判定）。
2. 通过表头指纹识别银行格式（工行/中行/民生/招行），未命中走通用列映射。
3. 从表头上方区域提取账户元数据（账号/户名/开户行/期初余额）。
4. 逐行归一化为标准记录，并计算去重键 unique_key。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 表头列映射规则：标准字段 -> 候选表头关键词（按优先级，先命中先得）
# ---------------------------------------------------------------------------
HEADER_RULES: list[tuple[str, list[str]]] = [
    ("txn_date", ["交易日期", "交易日", "记账日期", "交易日期/时间", "日期", "转账时间", "入账日期"]),
    ("txn_datetime", ["交易时间", "交易日期时间", "发生时间"]),
    ("direction_flag", ["借贷标志", "借贷标识", "借贷方向", "收支标志", "收/支", "收付标志", "收/付款标志", "借贷", "方向", "标志"]),
    ("debit_amount", ["借方金额", "借方发生额", "转出金额", "支出金额", "支出", "借方", "付款金额"]),
    ("credit_amount", ["贷方金额", "贷方发生额", "转入金额", "收入金额", "收入", "贷方", "收款金额"]),
    ("signed_amount", ["交易金额", "发生金额", "发生额", "金额", "交易金额(元)"]),
    ("balance", ["交易后余额", "账户余额", "余额"]),
    ("counterparty_name", ["对方单位", "对方账号名称", "对方户名", "对方名称", "对方账户名", "对方", "收款方户名", "付款方户名"]),
    ("counterparty_account", ["对方账号", "对方账户"]),
    ("counterparty_bank", ["对方开户行", "对方行名", "对方行号"]),
    ("payer_name", ["付款人名称", "付款人户名", "付款人"]),
    ("payer_account", ["付款人账号", "付款人账户"]),
    ("payee_name", ["收款人名称", "收款人户名", "收款人"]),
    ("payee_account", ["收款人账号", "收款人账户"]),
    ("summary", ["摘要", "客户附言", "附言", "备注", "用途摘要"]),
    ("purpose", ["用途"]),
    ("biz_type", ["业务类型", "交易类型", "业务种类"]),
    ("txn_ref", ["交易流水号", "主机交易流水号", "流水号", "凭证号码", "凭证号", "参考号"]),
    ("receipt_info", ["回单个性化信息", "回单信息"]),
]

# 表头行判定：至少命中 3 个不同字段类别
HEADER_MIN_CATEGORIES = 3

# 元数据标签：出现在表头上方区域，取同行或右侧相邻非空单元格作为值
# 注意：account_name 必须在 account_no 之前，避免"账户名称"被"账号"类标签误抓
META_LABELS: list[tuple[str, list[str]]] = [
    ("account_name", ["账户名称", "账号名称", "户名", "客户名称"]),
    ("account_no", ["查询账号", "银行账号", "账户号码", "账号"]),
    ("branch", ["开户机构", "开户网点"]),
    ("opening_balance", ["期初余额", "对账单期初余额", "起始余额"]),
    ("period_start", ["起始日期", "查询开始日期", "开始日期"]),
    ("period_end", ["截至日期", "查询结束日期", "结束日期"]),
]


def cell_to_text(value: Any) -> str:
    """把单元格值转成干净的文本；整数不带小数点。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value).strip()


def clean_amount_text(value: Any) -> str:
    """金额文本清洗：去千分位、货币符号、空格。"""
    text = cell_to_text(value).replace(",", "").replace("，", "")
    text = re.sub(r"[￥¥元\s]", "", text)
    return text


def parse_money(value: Any) -> float | None:
    text = clean_amount_text(value)
    if not text or text in {"-", "--", "—"}:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    if text.startswith("-"):
        negative = not negative
        text = text[1:]
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


def parse_date_text(value: Any) -> tuple[str, str]:
    """解析日期（可含时间），返回 (YYYY-MM-DD, HH:MM:SS)。失败返回 ("", "")。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d"), value.strftime("%H:%M:%S")
    text = cell_to_text(value).strip()
    if not text:
        return "", ""
    time_part = ""
    # 日期时间一起
    match = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", text)
    if match:
        year, month, day, hour, minute = match.groups()[:5]
        second = match.group(6) or "00"
        return f"{year}-{int(month):02d}-{int(day):02d}", f"{int(hour):02d}:{int(minute):02d}:{int(second):02d}"
    # 纯日期
    match = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?$", text)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}", ""
    # 8 位数字 YYYYMMDD
    match = re.match(r"^(\d{4})(\d{2})(\d{2})$", text)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}", ""
    # Excel 序列日期
    try:
        number = float(text)
    except ValueError:
        return "", ""
    if 20000 < number < 80000:  # 合理的 Excel 日期序列范围
        base = datetime(1899, 12, 30)
        moment = base + timedelta(days=number)
        return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M:%S")
    return "", ""


def parse_time_text(value: Any) -> str:
    text = cell_to_text(value).strip()
    match = re.match(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", text)
    if match:
        hour, minute, second = match.groups()
        return f"{int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}"
    return ""


def is_valid_ref(ref: str) -> bool:
    """流水号/回单号是否有效（排除全 0 占位符等）。"""
    if not ref or len(ref) < 6:
        return False
    if set(ref) <= {"0"}:
        return False
    if ref.upper() in {"N/A", "NA", "NULL", "NONE"}:
        return False
    return True


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def normalize_header_label(value: str) -> str:
    """表头/标签归一化：去空格、去掉 [英文说明] 与尾部冒号。"""
    text = re.sub(r"\s+", "", str(value or ""))
    text = re.split(r"[\[【（(]", text, maxsplit=1)[0]
    return text.rstrip("：:").strip()


def map_header_columns(header: list[str]) -> dict[str, int]:
    """把表头行映射为 标准字段 -> 列下标。三轮匹配：相等 > 开头 > 包含。"""
    mapping: dict[str, int] = {}
    used: set[int] = set()
    normalized = [normalize_header_label(cell_to_text(cell)) for cell in header]
    for field, candidates in HEADER_RULES:
        for mode in ("eq", "startswith", "contains"):
            for cand in candidates:
                cand_norm = normalize_header_label(cand)
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


def header_category_hits(col_map: dict[str, int]) -> int:
    """表头命中的字段类别数（金额类合并计 1）。"""
    categories = set(col_map)
    amount_fields = {"debit_amount", "credit_amount", "signed_amount"}
    if categories & amount_fields:
        categories -= amount_fields
        categories.add("amount")
    return len(categories)


def detect_format(col_map: dict[str, int], header: list[str]) -> str:
    """通过表头指纹识别银行格式。"""
    fields = set(col_map)
    header_text = "|".join(normalize_name(cell_to_text(cell)) for cell in header)
    if "凭证号" in header_text and "direction_flag" in fields and "credit_amount" in fields:
        return "工商银行"
    if "payer_name" in fields and "payee_name" in fields:
        return "中国银行"
    if "主机交易流水号" in header_text:
        return "民生银行"
    if "起息日" in header_text:
        return "招商银行"
    return "通用格式"


def find_header_row(rows: list[list[Any]], scan_limit: int = 20) -> tuple[int, dict[str, int]] | None:
    """在前 scan_limit 行内寻找表头行，返回 (行下标, 列映射)。"""
    best: tuple[int, dict[str, int]] | None = None
    best_hits = 0
    for idx in range(min(len(rows), scan_limit)):
        col_map = map_header_columns(rows[idx])
        hits = header_category_hits(col_map)
        if hits >= HEADER_MIN_CATEGORIES and hits > best_hits:
            best = (idx, col_map)
            best_hits = hits
    return best


def extract_metadata(rows: list[list[Any]], header_row_idx: int) -> dict[str, str]:
    """从表头上方区域提取账户元数据（标签 + 相邻值）。

    支持两种布局：标签独立成格、值在右侧相邻格（主流银行导出）；
    以及「标签: 值」写在同一格（部分网银/手工表格）。CEO 移植增强。
    """
    meta: dict[str, str] = {}
    region = rows[:header_row_idx]
    for row in region:
        texts = [cell_to_text(cell) for cell in row]
        for col_idx, text in enumerate(texts):
            if not text:
                continue
            clean = normalize_header_label(text)
            if not clean:
                continue
            for key, labels in META_LABELS:
                if key in meta:
                    continue
                label_variants = {normalize_header_label(label) for label in labels}
                exact = clean in label_variants
                inline_only = any(
                    clean.startswith(variant) and clean[len(variant):][:1] in {":", "："}
                    for variant in label_variants
                )
                if not exact and not inline_only:
                    continue
                # 值优先取标签单元格自身冒号后的内容，其次右侧相邻非空单元格
                inline = re.split(r"[:：]", text, maxsplit=1)
                value = inline[1].strip() if len(inline) > 1 else ""
                if not value:
                    for next_idx in range(col_idx + 1, len(texts)):
                        if texts[next_idx]:
                            value = texts[next_idx]
                            break
                if value:
                    meta[key] = value
                break
    return meta


def extract_receipt_ref(text: str) -> str:
    """从回单个性化信息中提取支付交易序号。"""
    match = re.search(r"支付交易序号[:：]\s*([A-Za-z0-9]+)", text or "")
    if match:
        return match.group(1)
    match = re.search(r"指令编号[:：]\s*([A-Za-z0-9]+)", text or "")
    return match.group(1) if match else ""


def build_record(row: list[Any], col_map: dict[str, int], fmt: str) -> dict[str, Any] | None:
    """把一行数据归一化为标准交易记录；无效行返回 None。"""

    def cell(field: str) -> Any:
        idx = col_map.get(field)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    def text(field: str) -> str:
        return cell_to_text(cell(field))

    # 日期/时间
    txn_date, txn_time = "", ""
    if "txn_date" in col_map:
        txn_date, txn_time = parse_date_text(cell("txn_date"))
    if "txn_datetime" in col_map:
        date2, time2 = parse_date_text(cell("txn_datetime"))
        if date2:
            txn_date, txn_time = date2, time2 or txn_time
        elif time2:
            txn_time = time2
        else:
            # 纯时间值（如招行的 16:39:15）
            only_time = parse_time_text(cell("txn_datetime"))
            if only_time:
                txn_time = txn_time or only_time
    if not txn_date and "txn_datetime" in col_map:
        txn_time = parse_time_text(cell("txn_datetime")) or txn_time
    if not txn_date:
        return None

    # 方向与金额
    direction = ""
    amount: float | None = None
    direction_source = ""
    if "direction_flag" in col_map:
        flag = text("direction_flag")
        if flag.startswith("借") or flag in {"支", "支出", "付", "出", "付款", "DEBIT", "D", "借方", "借记"}:
            direction = "out"
            direction_source = "flag"
        elif flag.startswith("贷") or flag in {"收", "收入", "贷记", "入", "收款", "CREDIT", "C", "贷方"}:
            direction = "in"
            direction_source = "flag"
    if "debit_amount" in col_map:
        debit = parse_money(cell("debit_amount"))
        if debit:
            direction, amount = "out", abs(debit)
            direction_source = "debit"
    if "credit_amount" in col_map:
        credit = parse_money(cell("credit_amount"))
        if credit:
            direction, amount = "in", abs(credit)
            direction_source = "credit"
    if amount is None and "signed_amount" in col_map:
        signed = parse_money(cell("signed_amount"))
        if signed is not None and signed != 0:
            amount = abs(signed)
            # 已有借贷/收支标志列时方向以标志为准（金额列的正负号在很多银行导出里恒为正），
            # 只有完全没有标志列时才用交易金额正负号推断方向。
            if not direction:
                direction = "in" if signed > 0 else "out"
                direction_source = "signed"
        elif signed == 0:
            return None
    if not direction or amount is None or amount <= 0:
        return None

    # 对方信息
    counterparty_name = text("counterparty_name")
    counterparty_account = text("counterparty_account")
    counterparty_bank = text("counterparty_bank")
    if fmt == "中国银行":
        # 往账（付）看收款人，来账（收）看付款人
        if direction == "out":
            counterparty_name = counterparty_name or text("payee_name")
            counterparty_account = counterparty_account or text("payee_account")
        else:
            counterparty_name = counterparty_name or text("payer_name")
            counterparty_account = counterparty_account or text("payer_account")

    # 摘要与用途
    summary = text("summary")
    purpose = text("purpose")
    if not summary:
        summary = text("biz_type")

    # 参考号
    txn_ref = text("txn_ref") if is_valid_ref(text("txn_ref")) else ""
    receipt_ref = extract_receipt_ref(text("receipt_info"))

    balance_after = parse_money(cell("balance")) if "balance" in col_map else None

    return {
        "txn_date": txn_date,
        "txn_time": txn_time,
        "direction": direction,
        "direction_source": direction_source,
        "amount": round(amount, 2),
        "balance_after": balance_after,
        "counterparty_name": counterparty_name,
        "counterparty_account": counterparty_account,
        "counterparty_bank": counterparty_bank,
        "summary": summary,
        "purpose": purpose,
        "txn_ref": txn_ref,
        "receipt_ref": receipt_ref,
    }


def unique_key_for(account_no: str, rec: dict[str, Any]) -> str:
    """生成流水唯一键：优先银行流水号，否则多字段组合。"""
    if rec.get("txn_ref"):
        return f"ref:{account_no}:{rec['txn_ref']}"
    return (
        f"mix:{account_no}:{rec['txn_date']}:{rec['txn_time']}:{rec['direction']}:"
        f"{rec['amount']:.2f}:{normalize_name(rec['counterparty_name'])}:{rec['summary']}"
    )


def guess_account_no(rows: list[list[Any]], col_map: dict[str, int], header_row_idx: int) -> str:
    """表头上方找不到账号时，尝试从数据行固定列（如招行）提取。"""
    for key in ("payer_account", "payee_account"):
        pass
    # 招行：每行第 0 列为本方账号，但没有独立标准字段；这里不做深猜，返回空
    return ""


def parse_sheet(sheet_name: str, rows: list[list[Any]]) -> dict[str, Any]:
    """解析单个工作表。"""
    warnings: list[str] = []
    found = find_header_row(rows)
    if not found:
        return {
            "sheet_name": sheet_name,
            "format_name": "",
            "account": {},
            "records": [],
            "warnings": [f"工作表「{sheet_name}」未识别到银行流水表头，已跳过"],
        }
    header_idx, col_map = found
    fmt = detect_format(col_map, rows[header_idx])
    meta = extract_metadata(rows, header_idx)

    account_no = normalize_name(meta.get("account_no", ""))
    account_name = meta.get("account_name", "")
    if not account_no:
        account_no = f"未识别-{sheet_name}"
        # v1.0.8：账号识别不到时，账户名也用工作表名兜底（否则账户列表里是空白一条）
        if not account_name:
            account_name = sheet_name
        warnings.append(f"工作表「{sheet_name}」未找到本方账号，暂以工作表名标记，导入后可在「银行账户」里改名")

    records: list[dict[str, Any]] = []
    skipped = 0
    for row_idx in range(header_idx + 1, len(rows)):
        row = rows[row_idx]
        if not any(cell_to_text(cell) for cell in row):
            continue
        rec = build_record(row, col_map, fmt)
        if rec is None:
            # 只有含日期文本的行才算“试图成为数据行”，否则静默跳过（页脚汇总等）
            skipped += 1
            continue
        rec["row_index"] = row_idx
        rec["raw"] = {i: cell_to_text(cell) for i, cell in enumerate(row) if cell_to_text(cell)}
        records.append(rec)

    return {
        "sheet_name": sheet_name,
        "format_name": fmt,
        "account": {
            "account_no": account_no,
            "account_name": account_name,
            "branch": meta.get("branch", ""),
            "bank_name": fmt if fmt != "通用格式" else "",
            "opening_balance": parse_money(meta.get("opening_balance")),
            "period_start": meta.get("period_start", ""),
            "period_end": meta.get("period_end", ""),
        },
        "records": records,
        "skipped_rows": skipped,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# 文件读取：.xls 用 xlrd，.xlsx 用 openpyxl
# ---------------------------------------------------------------------------

def read_sheets_xls(path: Path) -> list[tuple[str, list[list[Any]]]]:
    import xlrd

    workbook = xlrd.open_workbook(str(path))
    sheets = []
    for sheet in workbook.sheets():
        rows: list[list[Any]] = []
        for r in range(sheet.nrows):
            row_values = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == 3:  # XL_CELL_DATE
                    try:
                        moment = xlrd.xldate_as_datetime(cell.value, workbook.datemode)
                        row_values.append(moment)
                        continue
                    except Exception:
                        pass
                row_values.append(cell.value)
            rows.append(row_values)
        sheets.append((sheet.name, rows))
    return sheets


def read_sheets_xlsx(path: Path) -> list[tuple[str, list[list[Any]]]]:
    import openpyxl

    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    sheets = []
    for sheet in workbook.worksheets:
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
        sheets.append((sheet.title, rows))
    workbook.close()
    return sheets


def read_sheets_csv(path: Path) -> list[tuple[str, list[list[Any]]]]:
    """CSV 流水（多家网银默认导出格式）：自动探测编码（utf-8-sig / gbk / utf-8）。"""
    import csv
    import io

    raw = Path(path).read_bytes()
    text = None
    for encoding in ("utf-8-sig", "gbk", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        raise ValueError("无法识别 CSV 文件编码（尝试 utf-8 / gbk / gb18030 均失败）")
    # 去掉 BOM 后按首个统一分隔符切：逗号优先，制表符次之（部分银行导出用 tab）
    rows: list[list[Any]] = []
    dialect_sniff = csv.Sniffer()
    sample = text[:4096]
    try:
        dialect = dialect_sniff.sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    for row in csv.reader(io.StringIO(text), dialect):
        rows.append([cell for cell in row])
    return [(Path(path).stem, rows)]


def summarize_rows_for_diagnosis(rows: list[list[Any]], limit: int = 3) -> str:
    """把前几行非空内容拼成一句摘要，用于"未识别表头"时帮用户/支持定位问题。"""
    picked: list[str] = []
    for row in rows:
        cells = [cell_to_text(c) for c in row if cell_to_text(c)]
        if cells:
            picked.append(" | ".join(cells)[:120])
        if len(picked) >= limit:
            break
    return " ⏎ ".join(picked) if picked else "（文件为空或全是空行）"


def parse_bank_workbook(path: Path) -> list[dict[str, Any]]:
    """解析整个流水文件，返回每个工作表的解析结果。支持 .xls / .xlsx / .xlsm / .csv。"""
    suffix = Path(path).suffix.lower()
    if suffix == ".xls":
        sheets = read_sheets_xls(path)
    elif suffix in {".xlsx", ".xlsm"}:
        sheets = read_sheets_xlsx(path)
    elif suffix == ".csv":
        sheets = read_sheets_csv(path)
    else:
        raise ValueError(f"不支持的文件类型：{suffix or '(无后缀)'}（支持 .xlsx / .xls / .csv）")
    results = []
    for sheet_name, rows in sheets:
        result = parse_sheet(sheet_name, rows)
        if result["records"] or result["format_name"]:
            results.append(result)
        elif result["warnings"]:
            results.append(result)
    if results and not any(r["records"] for r in results):
        # 所有工作表都没解析出流水：附上首个工作表的开头内容，帮用户定位是"格式不对"还是"导错了文件"
        first = results[0]
        first["warnings"].append(
            "未从文件中解析出任何流水行。文件开头内容为：" + summarize_rows_for_diagnosis(sheets[0][1])
            if sheets else "未从文件中解析出任何流水行。"
        )
    return results


def detect_direction_inversion(records: list[dict[str, Any]]) -> bool:
    """根据余额连续性判断借贷方向语义是否与常规相反（贷方为支出、借方为收入）。

    不同银行对"借方/贷方"与"流入/流出"的对应关系可能不一致：
    - 常规语义：借方发生额 = 支出(out)，贷方发生额 = 收入(in)。
    - 相反语义：借方发生额 = 收入(in)，贷方发生额 = 支出(out)。
    仅凭表头无法区分，但同一张对账单中"交易后余额"的增减方向是客观的：
    余额增加 -> 实际流入(in)；余额减少 -> 实际流出(out)。
    对相邻两行（都有余额）做对比，统计"当前解析方向"与"余额变化方向"的吻合数；
    若翻转后吻合数更多且至少 1 对成立，判定该银行借贷语义相反，需要翻转方向。

    返回 True 表示需要把由 借贷标志/借贷金额 推导的方向整体翻转。
    """
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    prev: dict[str, Any] | None = None
    for rec in records:
        bal = rec.get("balance_after")
        if bal is None:
            prev = None  # 余额缺失处断开，避免跨缺失对比
            continue
        if prev is not None and prev.get("balance_after") is not None:
            pairs.append((prev, rec))
        prev = rec
    if not pairs:
        return False
    matches_normal = 0
    matches_flip = 0
    for prev_rec, cur_rec in pairs:
        delta = (cur_rec["balance_after"] or 0.0) - (prev_rec["balance_after"] or 0.0)
        if abs(delta) < 0.005:
            continue  # 余额未变化（如结转、调账），无法判断方向
        actual = "in" if delta > 0 else "out"
        if cur_rec["direction"] == actual:
            matches_normal += 1
        else:
            matches_flip += 1
    return matches_flip > matches_normal and matches_flip >= 1


def check_balance_continuity(records: list[dict[str, Any]]) -> list[str]:
    """按文件顺序校验余额连续性，返回异常描述列表。"""
    issues: list[str] = []
    previous: dict[str, Any] | None = None
    for rec in records:
        if rec.get("balance_after") is None:
            previous = rec
            continue
        if previous is not None and previous.get("balance_after") is not None:
            signed = rec["amount"] if rec["direction"] == "in" else -rec["amount"]
            expected = previous["balance_after"] + signed
            if abs(expected - rec["balance_after"]) > 0.011:
                issues.append(
                    f"{rec['txn_date']} {rec['txn_time']} 余额不连续：上笔余额 "
                    f"{previous['balance_after']:.2f}，本笔 {rec['direction'] == 'in' and '+' or '-'}{rec['amount']:.2f}，"
                    f"应为 {expected:.2f}，实际 {rec['balance_after']:.2f}"
                )
        previous = rec
    return issues
