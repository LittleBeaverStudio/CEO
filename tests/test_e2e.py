"""端到端验证：构造样例银行流水 + 发票明细 → 导入 → 决策引擎 → 手算核对。

用临时 CEO_APP_HOME，不污染真实数据目录。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(r"D:\python\首席执行官")
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="ceo_test_"))
os.environ["CEO_APP_HOME"] = str(TMP)
os.environ["CEO_API_PORT"] = "8878"  # 允许 backend 默认目录判定放行（模拟启动器）

from openpyxl import Workbook

from backend import config, engine, importer
from backend.api_server import init_db

DB = str(config.get_db_path())
init_db()

today = date.today()

# ---------------------------------------------------------------------------
# 样例 1：银行流水（通用格式：日期/借贷标志/借方金额/贷方金额/对方单位/摘要/余额）
# ---------------------------------------------------------------------------

def makedirs_bank(path: Path, rows: list[list]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "流水"
    ws.append(["账户名称: 测试科技有限公司"])
    ws.append(["查询账号: 6222000011112222"])
    ws.append(["期初余额: 500,000.00"])
    ws.append(["交易日期", "借贷标志", "借方金额", "贷方金额", "对方单位", "摘要", "交易后余额"])
    for row in rows:
        ws.append(row)
    wb.save(path)


def inv(kind_dir: str, name: str, counterparty: str, amount: float, days_ago: int,
        project: str = "") -> tuple[str, float, str]:
    return kind_dir, counterparty, amount, (today - timedelta(days=days_ago)).isoformat(), project


rows = []
# 历史月度收支（近 4 个月，月均净流入约 +3 万，供跑道 net_op 基线）
for months_back in range(4, 0, -1):
    month_index = today.year * 12 + today.month - 1 - months_back
    m_day = date(month_index // 12, month_index % 12 + 1, 15)
    rows.append([m_day, "贷", None, 380000, "甲公司", "货款", None])
    rows.append([m_day, "借", 350000, None, "乙供应商", "采购付款", None])

# 开具发票对应的收款（能轻匹配上的）
# 发票1：客户A 200,000，开票 20 天前 → 未匹配（0-30 桶）
# 发票2：客户B 150,000，开票 45 天前 → 银行已收（31-60 桶被收掉，不进应收）
# 发票3：客户C 300,000，开票 100 天前 → 未匹配（90+ 桶）
# 发票4：客户D 80,000，开票 70 天前 → 未匹配（61-90 桶）
inv_issue = [
    ("issue", "客户A公司", 200000, (today - timedelta(days=20)).isoformat(), "项目一"),
    ("issue", "客户B公司", 150000, (today - timedelta(days=45)).isoformat(), "项目一"),
    ("issue", "客户C公司", 300000, (today - timedelta(days=100)).isoformat(), "项目二"),
    ("issue", "客户D公司", 80000, (today - timedelta(days=70)).isoformat(), ""),
]
inv_receive = [
    ("receive", "丙材料商", 120000, (today - timedelta(days=15)).isoformat(), "项目二"),
    ("receive", "丁服务商", 30000, (today - timedelta(days=10)).isoformat(), ""),
]

# 客户B 的收款（当月入账，金额一致；不污染历史完整月的净流基线）
rows.append([today - timedelta(days=5), "贷", None, 150000, "客户B公司", "货款", None])
# 最后一笔带余额：500000(期初) + (380000-350000)*4 + 150000 + 66000 = 836000
rows.append([today - timedelta(days=1), "贷", None, 66000, "戊公司", "服务费", 836000])

bank_file = TMP / "样例银行流水.xlsx"
makedirs_bank(bank_file, rows)

# ---------------------------------------------------------------------------
# 样例 2：发票明细（开具 / 取得两个文件，带项目字段）
# ---------------------------------------------------------------------------

def make_invoice_file(path: Path, kind: str, data: list[tuple]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "明细"
    ws.append(["开票日期", "对方单位", "金额", "税额", "项目名称"])
    for _, cp, amt, d, proj in data:
        ws.append([d, cp, amt, round(amt / 1.13 * 0.13, 2), proj])
    wb.save(path)


issue_file = TMP / "样例开具发票.xlsx"
receive_file = TMP / "样例取得发票.xlsx"
make_invoice_file(issue_file, "issue", inv_issue)
make_invoice_file(receive_file, "receive", inv_receive)

# ---------------------------------------------------------------------------
# 导入 + 引擎
# ---------------------------------------------------------------------------
print("== 1. 银行导入 ==")
bank_summary = importer.import_bank_file(Path(DB), bank_file)
print(json.dumps(bank_summary, ensure_ascii=False, indent=1, default=str))

print("\n== 2. 发票导入（开具） ==")
print(importer.import_invoice_file(Path(DB), issue_file, "issue"))
print("== 3. 发票导入（取得） ==")
print(importer.import_invoice_file(Path(DB), receive_file, "receive"))

print("\n== 4. 决策引擎 ==")
dash = engine.build_dashboard(DB)

# 手算核对
expected_cash = 836000  # 500000期初 + (380000-350000)*4 + 150000 + 66000
expected_receivable = 200000 + 300000 + 80000  # 580000（客户B 已收掉）
expected_issued = 730000
expected_matched = 150000

cash_ok = abs(dash["runway"]["start_cash"] - expected_cash) < 0.01
ar_ok = abs(dash["aging"]["total_receivable"] - expected_receivable) < 0.01
issued_ok = abs(dash["aging"]["total_issued"] - expected_issued) < 0.01
matched_ok = abs(dash["aging"]["matched_total"] - expected_matched) < 0.01

print(f"当前现金   = {dash['runway']['start_cash']:,.2f}  (期望 {expected_cash:,.2f})  {'✔' if cash_ok else '✘'}")
print(f"未匹配应收 = {dash['aging']['total_receivable']:,.2f}  (期望 {expected_receivable:,.2f})  {'✔' if ar_ok else '✘'}")
print(f"开具总额   = {dash['aging']['total_issued']:,.2f}  (期望 {expected_issued:,.2f})  {'✔' if issued_ok else '✘'}")
print(f"已匹配收款 = {dash['aging']['matched_total']:,.2f}  (期望 {expected_matched:,.2f})  {'✔' if matched_ok else '✘'}")
print(f"回款率     = {dash['collection']['rate_overall']}%  (期望 {expected_matched/expected_issued*100:.1f}%)")
print("账龄桶     =", {b['bucket']: b['amount'] for b in dash['aging']['buckets']})
print("  期望: 0-30=200000, 61-90=80000, 90+=300000, 31-60=0")
print("应收TOP    =", [(c['name'], c['amount'], c['high_risk']) for c in dash['ar_top']])
print("跑道预警   =", dash["runway"]["alert"]["text"])
print("净流基线   =", dash["runway"]["net_op_avg"], "(期望 30000)")
print("项目卡     =", dash["projects"])
print("has_project_field =", dash["has_project_field"])
print("警告       =", dash["warnings"])

# 跑道中性场景首月手算：836000 + 回款(0-30:200000*0.95 + 61-90:80000*0.45/3 + 90+:300000*0.2/6)
#                            + 净流30000 - 固定支出(未配置=0)
first = dash["runway"]["scenarios"]["neutral"]["curve"][0]
expected_first = 836000 + 200000 * 0.95 + 80000 * 0.45 / 3 + 300000 * 0.2 / 6 + 30000
print(f"中性第1月  = {first['cash']:,.2f}  (期望 {expected_first:,.2f})  {'✔' if abs(first['cash']-expected_first)<0.01 else '✘'}")

assert cash_ok and ar_ok and issued_ok and matched_ok, "核心数值与手算不一致！"
assert abs(first["cash"] - expected_first) < 0.01, "跑道首月与手算不一致！"
assert dash["has_project_field"] is True and dash["projects"], "项目卡应显示"
print("\n全部断言通过 ✔")

# ---- v1.0.1 经营者视角新指标（手算核对）----
print("\n== 4.5 v1.0.1 经营者视角指标 ==")
# 撑月数：中性首月现金远大于 0，展望期内不会见负
ml = dash["runway"]["months_left"]
assert ml["neutral"] is None and ml["pessimistic"] is None and ml["optimistic"] is None, \
    f"样例数据撑月数应为 6 个月以上（None）：{ml}"
print(f"撑月数 = {ml}（展望 6 个月内现金均不见负）✔")

# 应付：取得发票 丙材料商 120000（15 天前）+ 丁服务商 30000（10 天前）
# 银行支出只有历史月 350000×4（乙供应商 采购付款），与取得发票金额/对方均不匹配 → 全部未付
ap = dash["payable"]
assert ap["has_data"] is True
assert abs(ap["total_received_inv"] - 150000) < 0.01
assert abs(ap["total"] - 150000) < 0.01, f"应付未付应为 150000：{ap['total']}"
assert abs(ap["matched_total"] - 0) < 0.01
assert ap["suppliers"][0]["name"] == "丙材料商" and ap["suppliers"][0]["days_max"] == 15
print(f"应付 = {ap['total']:,.2f}（丙材料商 {ap['suppliers'][0]['amount']:,.0f} 拿票 {ap['suppliers'][0]['days_max']} 天）✔")

# 支出结构：历史月乙供应商 采购付款 350000×4 + 无其他支出 → 全部进"其他经营支出"（无关键词命中）
sb = dash["spend_breakdown"]
assert sb["total"] > 0 and sb["categories"], f"支出结构不应为空：{sb['categories']}"
cat_names = {c["name"] for c in sb["categories"]}
assert "其他经营支出" in cat_names, f"乙供应商采购款应进其他经营支出：{cat_names}"
other = next(c for c in sb["categories"] if c["name"] == "其他经营支出")
assert abs(other["amount"] - 1400000) < 0.01, f"其他经营支出应为 1400000：{other['amount']}"
print(f"支出结构 = {[(c['name'], c['amount']) for c in sb['categories']]} ✔")

# 大额支出：样例支出都在最近 30 天之前，因此近 30 天列表应为空
bs = dash["big_spends"]
assert bs["threshold"] >= 10000, f"大额阈值不应低于 1 万：{bs['threshold']}"
assert not bs["items"], f"近 30 天没有支出，列表应为空：{bs['items']}"
print(f"大额支出阈值 {bs['threshold']:,.0f}，近 30 天大额 {len(bs['items'])} 笔 ✔")

# 老板三句话：现金句 + 应收句 + 生意句 + 应付句
hl = dash["headline"]
assert len(hl) == 4, f"四句速览：{hl}"
assert "账上还有" in hl[0] and ("还能撑" in hl[0] or "现金充足" in hl[0]), f"现金句措辞异常：{hl[0]}"
assert "客户C公司" in hl[1] and "58.0 万" in hl[1], f"应收句应含总额 58 万与 TOP 客户C：{hl[1]}"
assert "丙材料商" in hl[3], f"应付句应含最大供应商：{hl[3]}"
print("一句话速览：")
for line in hl:
    print("  ·", line)

# 本周要办：样例现金充足且应收 90+ 有 30 万 → 应含客户C催收项
actions = dash["action_items"]
assert any("催收" in a["text"] and "客户C公司" in a["text"] for a in actions), \
    f"应含客户C催收项：{[a['text'] for a in actions]}"
print("本周要办：")
for a in actions:
    print(f"  [{a['level']}]", a["text"])
print("v1.0.1 经营者指标全部通过 ✔")

# 情景问答：客户C 拖到 90 天
wif = engine.what_if_client(DB, "客户C公司")
print("\n== 5. 情景问答（客户C 拖到 90 天） ==")
print("found:", wif["found"], "| moved:", wif["moved_amount"])
print("原预警:", wif["original_alert"]["text"])
print("新预警:", wif["new_alert"]["text"])

# 重复导入去重（长期累积存储：同一文件再导一次应全部跳过）
print("\n== 5.5 重复导入去重 ==")
bank_again = importer.import_bank_file(Path(DB), bank_file)
assert bank_again["inserted"] == 0 and bank_again["duplicated"] == 10, \
    f"重复导入银行应 0 新增 10 重复：{bank_again}"
issue_again = importer.import_invoice_file(Path(DB), issue_file, "issue")
assert issue_again["inserted"] == 0 and issue_again["updated"] == 4, \
    f"重复导入发票应 0 新增 4 更新：{issue_again}"
print(f"银行再导：新增 {bank_again['inserted']}，重复跳过 {bank_again['duplicated']} ✔")
print(f"发票再导：新增 {issue_again['inserted']}，更新 {issue_again['updated']} ✔")
# 引擎数值不受重复导入影响
dash_rerun = engine.build_dashboard(DB)
assert abs(dash_rerun["aging"]["total_receivable"] - expected_receivable) < 0.01
print("重复导入后引擎数值不变 ✔")

# 无项目字段时项目卡应隐藏
print("\n== 6. 无项目字段回退 ==")
import sqlite3
conn = sqlite3.connect(DB)
conn.execute("UPDATE invoice_rows SET project=''")
conn.commit()
conn.close()
dash2 = engine.build_dashboard(DB)
print("has_project_field =", dash2["has_project_field"], " projects =", dash2["projects"])
assert dash2["has_project_field"] is False and dash2["projects"] is None, "无项目字段时项目卡应隐藏！"
print("OK ✔")
print("\n测试数据目录:", TMP)
