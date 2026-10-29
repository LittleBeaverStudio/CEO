"""HTTP 冒烟测试：真实启动 Flask 服务（werkzeug make_server，随机高位端口），
用 urllib 走完整 API 链路 + 验证 CEO_APP_HOME 未设置报错。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(r"D:\python\首席执行官")
sys.path.insert(0, str(ROOT))

# AI 测试强制走规则模式：避免用真实 Key 打公网（不确定、慢、可能超时）
os.environ.pop("AGNES_API_KEY", None)
os.environ.pop("CEO_LLM_API_KEY", None)

# ---- 0) CEO_APP_HOME 未设置时必须明确报错（v1.2 验收项）----
if "CEO_APP_HOME" in os.environ:
    del os.environ["CEO_APP_HOME"]
os.environ.pop("CEO_API_PORT", None)
for mod_name in list(sys.modules):
    if mod_name.startswith("backend"):
        del sys.modules[mod_name]
try:
    import backend.config
    backend.config.get_app_home()
    raise SystemExit("✘ 未设置 CEO_APP_HOME 时 backend.config 未报错！")
except backend.config.AppHomeNotSetError as exc:
    print("0) CEO_APP_HOME 未设置 → 明确报错 ✔ :", str(exc)[:60], "…")

# ---- 1) 起服务（带样例数据）----
TMP = Path(tempfile.mkdtemp(prefix="ceo_smoke_"))
os.environ["CEO_APP_HOME"] = str(TMP)
os.environ["CEO_API_PORT"] = "8878"
for mod_name in list(sys.modules):
    if mod_name.startswith("backend"):
        del sys.modules[mod_name]

from openpyxl import Workbook

from backend import config
from backend.api_server import app, init_db

DB = str(config.get_db_path())
init_db()
today_d = __import__("datetime").date.today()


def make_bank(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["账户名称: 冒烟测试公司"])
    ws.append(["查询账号: 6222000099998888"])
    ws.append(["期初余额: 200,000.00"])
    ws.append(["交易日期", "借贷标志", "借方金额", "贷方金额", "对方单位", "摘要", "交易后余额"])
    ws.append([today_d, "贷", None, 300000, "甲公司", "货款", 500000])
    ws.append([today_d, "借", 100000, None, "乙供应商", "付款", 400000])
    wb.save(path)


def make_invoice(path: Path, rows: list[tuple]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["开票日期", "对方单位", "金额", "税额", "项目名称", "发票号码"])
    for d, cp, amt, proj, no in rows:
        ws.append([d, cp, amt, 0, proj, no])
    wb.save(path)


make_bank(TMP / "银行流水.xlsx")
make_invoice(TMP / "开具.xlsx", [
    ((today_d - __import__("datetime").timedelta(days=10)).isoformat(), "甲公司", 300000, "主项目", "25317000000111111111"),
    ((today_d - __import__("datetime").timedelta(days=50)).isoformat(), "客户X公司", 120000, "副项目", "25317000000222222222"),
])
make_invoice(TMP / "取得.xlsx", [
    ((today_d - __import__("datetime").timedelta(days=8)).isoformat(), "丙供应商", 50000, "主项目", "25317000000333333333"),
])

from backend import importer
importer.import_bank_file(Path(DB), TMP / "银行流水.xlsx")
importer.import_invoice_file(Path(DB), TMP / "开具.xlsx", "issue")
importer.import_invoice_file(Path(DB), TMP / "取得.xlsx", "receive")

from werkzeug.serving import make_server

server = make_server("127.0.0.1", 0, app, threaded=True)
PORT = server.server_port
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(0.5)
BASE = f"http://127.0.0.1:{PORT}"


def call(method: str, path: str, payload: dict | None = None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("✔" if cond else "✘"), name, detail)
    if not cond:
        failures.append(name)


# ---- 2) 基础端点 ----
status, health = call("GET", "/api/health")
check("health", health.get("service") == "ceo-assistant" and health.get("version") == "1.0.9", str(health)[:80])

# ---- v1.0.3 前置：给默认公司配置 全称+税号（导入前置校验要求）----
SELF_NAME, SELF_TAX = "冒烟测试有限公司", "91110108MA01TEST0X"
status, comp_saved = call("POST", "/api/companies", {"id": 1, "name": SELF_NAME, "tax_no": SELF_TAX})
check("公司配置全称+税号", comp_saved["success"] and comp_saved["company"]["tax_no"] == SELF_TAX)

req = urllib.request.urlopen(BASE + "/", timeout=10)
html = req.read().decode("utf-8")
check("index.html", "首席执行官" in html and "runwayChart" in html and "小河狸" not in html.split("<body>")[1][:2000])
req_ico = urllib.request.urlopen(BASE + "/app.ico", timeout=10)
check("app.ico 路由", req_ico.status == 200 and len(req_ico.read()) > 1000)

# ---- 3) 看板 ----
status, data = call("GET", "/api/dashboard")
dash = data["dashboard"]
check("dashboard", data["success"] and abs(dash["runway"]["start_cash"] - 400000) < 0.01,
      f"现金={dash['runway']['start_cash']}")
check("dashboard 匹配应收", abs(dash["aging"]["total_receivable"] - 120000) < 0.01
      and abs(dash["aging"]["matched_total"] - 300000) < 0.01, "甲公司收款应匹配，客户X 进未匹配桶")
check("dashboard 项目卡", dash["has_project_field"] and dash["projects"][0]["project"] == "主项目")

# ---- 3b) v1.0.1 经营者视角字段 ----
check("dashboard 撑月数", "months_left" in (dash["runway"] or {})
      and all(v is None for v in dash["runway"]["months_left"].values()),
      str(dash.get("runway", {}).get("months_left")))
check("dashboard 应付侧", abs(dash["payable"]["total"] - 50000) < 0.01
      and dash["payable"]["suppliers"][0]["name"] == "丙供应商",
      f"应付={dash['payable']['total']}（取得 5 万未付）")
check("dashboard 支出结构", dash["spend_breakdown"]["total"] > 0
      and any(c["name"] == "其他经营支出" for c in dash["spend_breakdown"]["categories"]),
      str([(c["name"], c["amount"]) for c in dash["spend_breakdown"]["categories"]]))
check("dashboard 大额支出", dash["big_spends"]["items"]
      and dash["big_spends"]["items"][0]["amount"] >= 100000,
      str(dash["big_spends"]["threshold"]))
check("dashboard 速览/行动", len(dash["headline"]) >= 3 and dash["action_items"],
      " / ".join(dash["headline"])[:60])

# ---- 4) 固定支出 CRUD + 跑道变化 ----
status, fe = call("POST", "/api/config/fixed-expenses",
                  {"name": "工资", "monthly_amount": 80000})
check("固定支出添加", fe["success"] and abs(fe["monthly_total"] - 80000) < 0.01)
status, eng = call("POST", "/api/config/engine", {"safety_months": 1.5})
check("安全线配置", eng["success"] and abs(eng["safety_months"] - 1.5) < 1e-9)
status, data = call("GET", "/api/dashboard")
runway = data["dashboard"]["runway"]
check("跑道含固定支出", abs(runway["monthly_fixed"] - 80000) < 0.01
      and abs(runway["safety_line"] - 120000) < 0.01, f"安全线={runway['safety_line']}")
check("跑道三场景", set(runway["scenarios"]) == {"optimistic", "neutral", "pessimistic"})

# ---- 5) AI 规则模式（未在文件里配 Key；环境变量有 AGNES_API_KEY 时视为已启用）----
env_key = bool(os.environ.get("AGNES_API_KEY") or os.environ.get("CEO_LLM_API_KEY"))
status, ai_cfg = call("GET", "/api/config/ai")
check("AI 配置读取", ai_cfg["success"] and ai_cfg["enabled"] is env_key,
      f"enabled={ai_cfg['enabled']} (env_key={env_key})")
status, chat = call("POST", "/api/ai/chat", {"question": "下月会缺钱吗？"})
check("AI 问答通路", chat["success"] and chat["mode"] in {"cloud", "rule"},
      f"mode={chat['mode']}")
status, brief = call("POST", "/api/briefing/generate", {})
check("周简报(规则模式)", brief["success"] and brief["mode"] == "rule" and "本周经营要点" in brief["content"])
status, attr = call("POST", "/api/ai/attribute", {"alert": "测试预警"})
check("异常归因(规则模式)", attr["success"] and attr["mode"] == "rule" and len(attr["answer"]) > 10)
status, scen = call("POST", "/api/ai/scenario", {"client": "客户X公司"})
check("情景问答", scen["success"] and scen.get("found") is True and "客户X公司" in scen["answer"])

# ---- 6) skill 只读端点 ----
status, sk = call("GET", "/api/skill/health")
check("skill/health", sk.get("service") == "ceo-assistant")
status, sk_info = call("GET", "/api/skill/info")
check("skill/info", sk_info["success"] and any("ceo-brief" in e for e in sk_info["endpoints"])
      and "/api/skill/companies" in sk_info["endpoints"])
status, sk_companies = call("GET", "/api/skill/companies")
check("skill/companies", sk_companies["success"] and len(sk_companies["companies"]) == 1)
status, sk_brief = call("GET", "/api/skill/ceo-brief")
check("skill/ceo-brief", sk_brief["success"] and "本周经营要点" in sk_brief["content"]
      and sk_brief["company_id"] == 1)

# ---- 7) 导入状态 / 台账 / 批次 / 清空 ----
status, st = call("GET", "/api/import/status")
check("import/status", st["bank"]["txn_count"] == 2 and st["invoice"]["issue"]["count"] == 2)

# 台账端点
status, lb = call("GET", "/api/ledger/bank?page=1&page_size=10")
check("ledger/bank", lb["success"] and lb["total"] == 2 and len(lb["rows"]) == 2,
      f"total={lb['total']}")
status, lb2 = call("GET", "/api/ledger/bank?direction=out")
check("ledger/bank 方向筛选", lb2["success"] and lb2["total"] == 1
      and lb2["rows"][0]["counterparty"] == "乙供应商")
status, li = call("GET", "/api/ledger/invoice?kind=issue")
check("ledger/invoice", li["success"] and li["total"] == 2
      and abs(li["summary"]["issue"]["amount"] - 420000) < 0.01)
check("ledger/invoice 发票号码列", li["rows"][0].get("inv_no", "").startswith("25317"),
      f"inv_no={li['rows'][0].get('inv_no')}")
status, li2 = call("GET", "/api/ledger/invoice?keyword=X")
check("ledger/invoice 关键词", li2["success"] and li2["total"] == 1
      and li2["rows"][0]["counterparty"] == "客户X公司")
status, li3 = call("GET", "/api/ledger/invoice?inv_no=11111")
check("ledger/invoice 票号筛选", li3["success"] and li3["total"] == 1
      and li3["rows"][0]["counterparty"] == "甲公司")
status, li4 = call("GET", "/api/ledger/invoice?amount_min=200000")
check("ledger/invoice 金额区间", li4["success"] and li4["total"] == 1)
status, li5 = call("GET", "/api/ledger/invoice?export=1")
check("ledger/invoice 导出全量", li5["success"] and len(li5["rows"]) == li5["total"])
status, bt = call("GET", "/api/import/batches")
check("import/batches", bt["success"] and len(bt["batches"]) == 3
      and all("warnings" in b for b in bt["batches"]))

# 重复导入同一文件 → 全部去重跳过
status, re_imp = call("GET", "/api/import/status")  # 占位保持结构清晰
from backend import importer as _imp
_re_summary = _imp.import_bank_file(Path(DB), TMP / "银行流水.xlsx")
check("重复导入去重", _re_summary["inserted"] == 0 and _re_summary["duplicated"] == 2,
      f"inserted={_re_summary['inserted']} duplicated={_re_summary['duplicated']}")
# 去重后台账总数不变
status, lb3 = call("GET", "/api/ledger/bank")
check("去重后台账总数不变", lb3["total"] == 2)

status, cleared = call("POST", "/api/import/clear", {"kind": "invoice"})
check("import/clear", cleared["success"])

# ---- 8) 公司管理 + 数据隔离（照发票助手逻辑移植）----
status, comp_list = call("GET", "/api/companies")
check("companies 列表(含默认)", comp_list["success"] and len(comp_list["companies"]) >= 1)
default_cid = comp_list["companies"][0]["id"]
status, comp_new = call("POST", "/api/companies", {"name": "测试二公司", "note": "隔离验证"})
check("companies 新增", comp_new["success"] and comp_new["company"]["name"] == "测试二公司")
cid2 = comp_new["company"]["id"]
status, sk_companies2 = call("GET", "/api/skill/companies")
check("skill/companies 多公司", any(c["id"] == cid2 for c in sk_companies2["companies"]))
status, sk_brief2 = call("GET", f"/api/skill/ceo-brief?company_id={cid2}")
check("skill 周报按公司隔离", status == 404 and "尚无周报" in sk_brief2.get("error", ""))
status, comp_edit = call("POST", "/api/companies", {"id": cid2, "name": "测试二公司改名"})
check("companies 修改", comp_edit["success"] and comp_edit["company"]["name"] == "测试二公司改名")

# 公司2 导入一套独立数据
from openpyxl import load_workbook as _lwb
_wb = _lwb(str(TMP / "银行流水.xlsx"))
_ws = _wb.active
_ws.delete_rows(6)  # 删掉支出行（第6行），构成不同 unique_key 的数据：公司2 = 期初200000 + 收300000 = 500000
_wb.save(str(TMP / "银行流水_公司2.xlsx"))
from backend import importer as _imp2
_s2 = _imp2.import_bank_file(Path(DB), TMP / "银行流水_公司2.xlsx", company_id=cid2)
check("公司2 独立导入", _s2["inserted"] == 1, f"inserted={_s2['inserted']}")

# 公司1 数据不受公司2 影响；公司2 看板独立
status, d1 = call("GET", f"/api/dashboard?company_id={default_cid}")
status, d2 = call("GET", f"/api/dashboard?company_id={cid2}")
check("公司隔离-看板", d1["dashboard"]["has_bank"] and d2["dashboard"]["has_bank"]
      and abs(d1["dashboard"]["runway"]["start_cash"] - 400000) < 0.01
      and abs(d2["dashboard"]["runway"]["start_cash"] - 500000) < 0.01,
      f"公司1现金={d1['dashboard']['runway']['start_cash']} 公司2现金={d2['dashboard']['runway']['start_cash']}")
# 公司2 重复导入同一文件 → 公司2 内去重，公司1 不受影响
_s2b = _imp2.import_bank_file(Path(DB), TMP / "银行流水_公司2.xlsx", company_id=cid2)
check("公司2 重复导入去重", _s2b["inserted"] == 0 and _s2b["duplicated"] == 1)
status, lb4 = call("GET", f"/api/ledger/bank?company_id={cid2}")
status, lb5 = call("GET", f"/api/ledger/bank?company_id={default_cid}")
check("公司隔离-台账", lb4["total"] == 1 and lb5["total"] == 2,
      f"公司2={lb4['total']} 公司1={lb5['total']}")
# 固定支出按公司隔离
status, fe2 = call("POST", "/api/config/fixed-expenses",
                   {"name": "公司2房租", "monthly_amount": 9999, "company_id": cid2})
status, fe1 = call("GET", f"/api/config/fixed-expenses?company_id={default_cid}")
check("公司隔离-固定支出", fe1["monthly_total"] == 80000,
      f"公司1固定支出={fe1['monthly_total']}（不应含公司2的9999）")

# 公司2 配置全称+税号（v1.0.3 导入前置校验要求），随后走 HTTP 上传 CSV
status, _ = call("POST", "/api/companies", {"id": cid2, "name": "测试二公司改名", "tax_no": "91110108MA01TEST0Y"})
# ---- 8b) v1.0.2：CSV（GBK 编码、收支标志列）流水走 HTTP 上传（导入到公司2，随级联删除清场）----
csv_text = (
    "账户名称: CSV测试公司\n"
    "查询账号: 6222000055556666\n"
    "交易日期,收支标志,交易金额,对方账户名,摘要,账户余额\n"
    "2026-10-20,收,120000,甲公司,货款,\n"
    "2026-10-21,支,30000,丁供应商,采购,90000\n"
)
boundary = "----ceoTestBoundary"
body = (
    f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"liushui.csv\"\r\n"
    f"Content-Type: text/csv\r\n\r\n"
).encode() + csv_text.encode("gbk") + f"\r\n--{boundary}--\r\n".encode()
req_csv = urllib.request.Request(
    BASE + "/api/import/bank",
    data=(f"--{boundary}\r\nContent-Disposition: form-data; name=\"company_id\"\r\n\r\n{cid2}\r\n".encode()
          + body),
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
with urllib.request.urlopen(req_csv, timeout=10) as r:
    status_csv = json.loads(r.read().decode())
check("CSV 流水导入", status_csv["success"] and status_csv["summary"]["inserted"] == 2,
      str(status_csv.get("summary", status_csv))[:120])
if status_csv.get("success"):
    status, lb_csv = call("GET", f"/api/ledger/bank?company_id={cid2}&keyword=%E4%B8%81%E4%BE%9B%E5%BA%94%E5%95%86")
    check("CSV 流水入库(公司2)", lb_csv["total"] == 1 and abs(lb_csv["rows"][0]["amount"] - 30000) < 0.01
          and lb_csv["rows"][0]["direction"] == "out")

# ---- 8c) v1.0.3：发票买卖双向识别（购买方/销售方双列 + 税号匹配本公司）----
from openpyxl import Workbook as _WB

_wb_dual = _WB()
_ws_dual = _wb_dual.active
_ws_dual.title = "明细"
_ws_dual.append(["开票日期", "购买方名称", "购买方税号", "销售方名称", "销售方税号", "价税合计"])
_td = (today_d - __import__("datetime").timedelta(days=3)).isoformat()
_ws_dual.append([_td, "客户甲公司", "91110108CUST00001A", SELF_NAME, SELF_TAX, 100000])   # 本公司是销方 → 开具
_ws_dual.append([_td, SELF_NAME, SELF_TAX, "供应商丙公司", "91110108SUPP00002B", 40000])  # 本公司是购方 → 取得
_ws_dual.append([_td, "客户乙公司", "91110108CUST00003C", "客户丙公司", "91110108CUST00004D", 60000])  # 都不是自己 → 按默认方向
_dual_path = TMP / "双列发票.xlsx"
_wb_dual.save(_dual_path)
from backend.importer import parse_invoice_workbook as _piv
_dual = _piv(_dual_path, default_kind="issue", self_name=SELF_NAME, self_tax=SELF_TAX)
_kinds = {r["counterparty"]: r["kind"] for r in _dual["rows"]}
check("双列-本公司销方→开具", _kinds.get("客户甲公司") == "issue", str(_kinds))
check("双列-本公司购方→取得", _kinds.get("供应商丙公司") == "receive", str(_kinds))
check("双列-无本公司→默认方向", _kinds.get("客户乙公司") == "issue", str(_kinds))

# 单列布局（只有对方单位列）：对方是本公司自己 → 跳过并警告
_wb_self = _WB()
_ws_self = _wb_self.active
_ws_self.append(["开票日期", "对方单位", "金额"])
_ws_self.append([_td, SELF_NAME, 12345])
_ws_self.append([_td, "客户丁公司", 6789])
_self_path = TMP / "含自己.xlsx"
_wb_self.save(_self_path)
_self = _piv(_self_path, default_kind="issue", self_name=SELF_NAME, self_tax=SELF_TAX)
check("单列-跳过自己", len(_self["rows"]) == 1 and _self["rows"][0]["counterparty"] == "客户丁公司"
      and any("自己" in w for w in _self["warnings"]),
      str(_self["warnings"])[:80])

# 缺税号的公司走 HTTP 导入 → 400 引导先建公司
_wb_n2 = _WB()
_ws_n2 = _wb_n2.active
_ws_n2.append(["交易日期", "收支标志", "交易金额", "对方账户名", "摘要"])
_ws_n2.append([_td, "收", 500, "戊公司", "测试"])
_n2_path = TMP / "未配置公司.csv"
_wb_n2.save(_n2_path.with_suffix(".csv"))
boundary2 = "----ceoB2"
body2 = (
    f"--{boundary2}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.csv\"\r\n"
    f"Content-Type: text/csv\r\n\r\n"
).encode() + "交易日期,收支标志,交易金额,对方账户名,摘要\n2026-10-01,收,500,戊公司,测试\r\n".encode("gbk") + f"\r\n--{boundary2}--\r\n".encode()
status, comp_b = call("POST", "/api/companies", {"name": "缺税号公司", "note": ""})
cid_b = comp_b["company"]["id"]
req_b = urllib.request.Request(
    BASE + f"/api/import/bank?company_id={cid_b}",
    data=body2, headers={"Content-Type": f"multipart/form-data; boundary={boundary2}"}, method="POST")
try:
    with urllib.request.urlopen(req_b, timeout=10) as r:
        resp_b = json.loads(r.read().decode())
        code_b = r.status
except urllib.error.HTTPError as e:
    resp_b = json.loads(e.read().decode())
    code_b = e.code
check("导入前置-缺税号拦截", code_b == 400 and "税号" in resp_b.get("error", ""),
      f"code={code_b} err={resp_b.get('error', '')[:60]}")
status, _ = call("DELETE", f"/api/companies/{cid_b}")

# 删除公司2 → 数据级联删除；公司1 完好
status, deleted = call("DELETE", f"/api/companies/{cid2}")
check("companies 删除", deleted["success"])
status, lb6 = call("GET", f"/api/ledger/bank?company_id={cid2}")
check("删除公司级联", lb6["total"] == 0)
status, lb7 = call("GET", f"/api/ledger/bank?company_id={default_cid}")
check("删除后公司1完好", lb7["total"] == 2)

# ---- 8b) AI 配置保存（假 Key，不联网调用）----
status, saved = call("POST", "/api/config/ai", {"provider": "agnes", "model": "agnes-chat-latest"})
check("AI 配置保存", saved["success"] and saved["config"]["provider"] == "agnes")
check("AI Key 掩码", "****" in (saved["config"]["api_key"] or "") or saved["config"]["api_key"] == "")

print()
if failures:
    raise SystemExit("✘ 冒烟测试失败：" + "、".join(failures))
print(f"冒烟测试全部通过 ✔ （BASE={BASE}，数据目录={TMP}）")
