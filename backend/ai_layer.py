"""AI 层（技术说明 §5）——可选云 API 薄层，AI 只做"翻译/叙述"，不负责算数。

四大场景：经营问答 / 周简报 / 异常归因 / 情景问答。
降级（无 API）：本地规则模式——决策卡用写死模板话术，周简报退化为要点列表，
自由问答与归因禁用并提示"配置 API 后开启"。

安全（§5/§11.3）：
- API Key 只走环境变量（AGNES_API_KEY / CEO_LLM_API_KEY）或 sources.json（本地、不进项目目录）。
- 发给 LLM 的上下文只含聚合数字与脱敏标签（客户 A/B 代称），不含客户全名/税号/发票原文。
- v1.0.4：内置国内外主流厂商预设（DeepSeek / Kimi / 智谱 GLM / MiniMax / 通义 / 豆包 /
  硅基流动 / OpenAI / Ollama），新增"测试连接"与"拉取模型列表"，并把 HTTP 错误翻译成人话。
  同时修正 Agnes 的 Base URL（api.agnes-ai.com → apihub.agnes-ai.com，旧地址会 404）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Any

from backend import engine

# 每个厂商都是 OpenAI 兼容协议：base_url + "/chat/completions"
# group 仅用于前端下拉分组；models 是"建议列表"，实际可用型号以 /models 拉取为准
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "agnes": {
        "label": "Agnes AI",
        "group": "国内",
        "base_url": "https://apihub.agnes-ai.com/v1",
        "default_model": "agnes-3.0-flash",
        "models": ["agnes-3.0-flash", "agnes-2.5-flash", "agnes-2.5-pro-alpha"],
        "hint": "Base URL 必须是 apihub.agnes-ai.com/v1（旧文档的 api.agnes-ai.com 会返回 404）",
    },
    "deepseek": {
        "label": "DeepSeek 深度求索",
        "group": "国内",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "hint": "Key 在 platform.deepseek.com 创建；deepseek-chat 便宜够用",
    },
    "kimi": {
        "label": "Kimi 月之暗面",
        "group": "国内",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "kimi-latest", "kimi-k2-0905-preview"],
        "hint": "Key 在 platform.moonshot.cn 创建",
    },
    "zhipu": {
        "label": "智谱 GLM",
        "group": "国内",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-air", "glm-4-plus", "glm-4.5", "glm-4.5-air"],
        "hint": "注意 Base URL 是 /api/paas/v4，不是 /v1",
    },
    "minimax": {
        "label": "MiniMax",
        "group": "国内",
        "base_url": "https://api.minimaxi.com/v1",
        "default_model": "MiniMax-Text-01",
        "models": ["MiniMax-Text-01", "MiniMax-M1", "abab6.5s-chat"],
        "hint": "若提示路径不存在，把 Base URL 换成 https://api.minimax.chat/v1 再试",
    },
    "qwen": {
        "label": "通义千问（阿里云百炼）",
        "group": "国内",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max", "qwen-long"],
        "hint": "用阿里云百炼的 API Key（sk- 开头），走 compatible-mode 兼容接口",
    },
    "doubao": {
        "label": "豆包（火山方舟）",
        "group": "国内",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-seed-1-6-250615",
        "models": ["doubao-seed-1-6-250615", "doubao-1-5-pro-32k-250115"],
        "hint": "也可把模型填成控制台里的接入点 ID（ep-xxxx）",
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "group": "国内",
        "base_url": "https://api.siliconflow.cn/v1",
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
        "models": ["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"],
        "hint": "聚合平台，一个 Key 可调多家开源模型",
    },
    "openai": {
        "label": "OpenAI",
        "group": "海外",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o", "gpt-4.1-mini"],
        "hint": "国内网络通常需要代理",
    },
    "ollama": {
        "label": "Ollama（本机离线）",
        "group": "本地",
        "base_url": "http://127.0.0.1:11434/v1",
        "default_model": "qwen2.5:7b",
        "models": ["qwen2.5:7b", "llama3.1:8b"],
        "hint": "API Key 随便填（如 ollama），模型名用 ollama list 里的名字",
    },
    "openai_compatible": {
        "label": "自定义（OpenAI 兼容）",
        "group": "自定义",
        "base_url": "",
        "default_model": "",
        "models": [],
        "hint": "填任何 OpenAI 兼容网关的 Base URL（一般以 /v1 结尾）",
    },
}

# 历史错误地址自动纠正：命中即改写为正确域名，老配置无需手工改
_LEGACY_BASE_URL_FIXES = (
    ("api.agnes-ai.com", "apihub.agnes-ai.com"),
    ("agnes-ai.cn", "apihub.agnes-ai.com"),
)


def normalize_base_url(provider: str, url: str | None) -> str:
    """规范化 Base URL：补默认值、去掉末尾斜杠、纠正已知错误域名。"""
    value = (url or "").strip()
    if value:
        for wrong, right in _LEGACY_BASE_URL_FIXES:
            if wrong in value:
                value = value.replace(wrong, right)
        value = value.replace("apihub.agnes-ai.com/v1/v1", "apihub.agnes-ai.com/v1")
        value = value.rstrip("/")
    if not value:
        value = str(PROVIDER_PRESETS.get(provider, {}).get("base_url") or "").rstrip("/")
    return value


def _normalize_model(provider: str, model: str | None) -> str:
    value = (model or "").strip()
    # 厂商下线的旧模型名自动升级（v1.0.4：agnes-chat-latest 已不存在，会报 model not found）
    legacy = {("agnes", "agnes-chat-latest"): "agnes-3.0-flash",
              ("agnes", "agnes-2.5"): "agnes-2.5-flash"}
    if (provider, value) in legacy:
        value = legacy[(provider, value)]
    return value or str(PROVIDER_PRESETS.get(provider, {}).get("default_model") or "")


# ---------------------------------------------------------------------------
# 配置读取：sources.json 优先级低于环境变量 Key
# ---------------------------------------------------------------------------

def read_sources_config(db_path: str) -> dict[str, Any]:
    from backend.config import get_sources_json_path
    cfg: dict[str, Any] = {}
    path = get_sources_json_path()
    if path.is_file():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key='ai'").fetchone()
    finally:
        conn.close()
    if row:
        try:
            stored = json.loads(row[0])
            for k, v in stored.items():
                cfg.setdefault(k, v)
        except ValueError:
            pass
    cfg.setdefault("provider", "agnes")
    if cfg["provider"] not in PROVIDER_PRESETS:
        cfg["provider"] = "openai_compatible"
    cfg["base_url"] = normalize_base_url(cfg["provider"], cfg.get("base_url"))
    cfg["model"] = _normalize_model(cfg["provider"], cfg.get("model"))
    # v1.0.5：界面里保存的 Key 优先于环境变量。
    # 之前环境变量优先，用户在新界面填了 Key 却仍用系统里那把旧的（失效时极难排查）。
    saved_key = (cfg.get("api_key") or "").strip()
    env_key = os.environ.get("AGNES_API_KEY") or os.environ.get("CEO_LLM_API_KEY") or ""
    cfg["api_key"] = saved_key or env_key
    cfg["api_key_source"] = "saved" if saved_key else ("env" if env_key else "")
    cfg["anonymize"] = bool(cfg.get("anonymize", False))
    cfg["enabled"] = bool(cfg.get("api_key") and cfg.get("base_url") and cfg.get("model"))
    return cfg


def save_ai_config(db_path: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """保存 AI 配置到 settings（api_key 若为空/掩码则保留原值）。"""
    old = read_sources_config(db_path)
    provider = cfg.get("provider") or old.get("provider") or "agnes"
    payload = {
        "provider": provider,
        "base_url": normalize_base_url(provider, cfg.get("base_url")),
        "model": _normalize_model(provider, cfg.get("model")),
        "anonymize": bool(cfg.get("anonymize", old.get("anonymize", False))),
    }
    conn = sqlite3.connect(db_path)
    try:
        # v1.0.4 修复：前端 Key 框每次都是空的，之前"留空保存"会把已存 Key 清掉。
        # 这里显式读出库里原值，留空/掩码时写回，避免用户以为保存了其实被抹掉。
        stored_key = ""
        row = conn.execute("SELECT value FROM settings WHERE key='ai'").fetchone()
        if row:
            try:
                stored_key = (json.loads(row[0]) or {}).get("api_key") or ""
            except ValueError:
                stored_key = ""
        new_key = (cfg.get("api_key") or "").strip()
        if new_key and "*" not in new_key:
            payload["api_key"] = new_key
        elif stored_key:
            payload["api_key"] = stored_key
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('ai', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(payload, ensure_ascii=False),),
        )
        conn.commit()
    finally:
        conn.close()
    result = read_sources_config(db_path)
    result["api_key"] = (result["api_key"][:4] + "****") if result["api_key"] else ""
    return result


# ---------------------------------------------------------------------------
# HTTP：请求 + 错误翻译（把 "HTTP Error 404: Not Found" 变成能照着改的话）
# ---------------------------------------------------------------------------

def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw[:300]
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("code") or "")[:300]
        if isinstance(err, str):
            return err[:300]
        if data.get("message"):
            return str(data["message"])[:300]
    return raw[:300]


def describe_http_error(exc: Exception, base_url: str) -> str:
    """把底层 HTTP 异常翻译成用户可以照着排查的中文提示。"""
    if isinstance(exc, NetworkAttemptError):
        return (f"连不上服务器（{exc}）。已尝试直连与系统代理两条路——"
                f"检查网络/防火墙/代理是否放行 {base_url}，或换个厂商试试")
    if isinstance(exc, urllib.error.HTTPError):
        detail = _read_error_body(exc)
        code = exc.code
        if code in (401, 403):
            msg = "API Key 无效或无权限（检查是否复制完整、是否已过期、是否属于该厂商）"
        elif code == 404:
            msg = (f"接口地址不对（{base_url}/chat/completions 不存在）。"
                   "请核对 Base URL：OpenAI 兼容地址一般以 /v1 结尾；智谱是 /api/paas/v4；"
                   "Agnes 是 https://apihub.agnes-ai.com/v1")
        elif code == 429:
            msg = "调用太频繁或额度用尽（等一会儿再试，或检查账户余额）"
        elif code in (400, 422):
            msg = "请求参数被拒（多半是模型名写错，点「拉取模型列表」挑一个）"
        elif code >= 500:
            msg = "厂商服务端异常，稍后重试"
        else:
            msg = f"调用失败（HTTP {code}）"
        return f"{msg}｜HTTP {code}｜{detail}" if detail else f"{msg}｜HTTP {code}"
    if isinstance(exc, urllib.error.URLError):
        return f"连不上服务器（{exc.reason}）。检查网络/代理，或 Base URL 是否写错"
    if isinstance(exc, TimeoutError):
        return "请求超时：网络太慢或厂商响应慢，稍后重试"
    if isinstance(exc, (KeyError, IndexError)):
        return "返回格式不是 OpenAI 兼容结构（choices[0].message）——确认 Base URL 指向 /chat/completions"
    if isinstance(exc, ValueError):
        return f"返回内容不是合法 JSON：{exc}"
    return f"调用失败：{exc}"


def _http_json(url: str, api_key: str, body: bytes | None = None,
               timeout: int = 45) -> dict[str, Any]:
    """发请求并解析 JSON。v1.0.6：先直连、再走系统代理。

    国内厂商（DeepSeek/Agnes/智谱…）直连最快；但公司网络里系统代理可能对某些域名
    不转发，表现为 WinError 10060 超时。两条路都试一遍，比"配了代理就必死"稳。
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    errors: list[str] = []

    def _do(use_proxy: bool):
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
        if use_proxy:
            return urllib.request.urlopen(req, timeout=timeout)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)

    modes = [False]
    if urllib.request.getproxies():
        modes.append(True)  # 配了系统代理才补一次走代理的尝试
    for idx, use_proxy in enumerate(modes):
        try:
            with _do(use_proxy) as resp:
                return json.loads(resp.read().decode("utf-8", errors="ignore"))
        except urllib.error.HTTPError:
            # 服务器有响应（401/404/429…）：地址和 Key 的问题，换条网络路也一样，直接抛
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层失败才换路重试
            label = "系统代理" if use_proxy else "直连"
            errors.append(f"{label}失败（{_short_reason(exc)}）")
            if idx == len(modes) - 1:
                raise NetworkAttemptError("；".join(errors), exc) from exc
    raise NetworkAttemptError("；".join(errors) or "未知网络错误", None)


class NetworkAttemptError(Exception):
    """直连与代理都失败时抛出，携带每一步的原因。"""

    def __init__(self, message: str, original: Exception | None) -> None:
        super().__init__(message)
        self.original = original


def _short_reason(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    return str(exc) or exc.__class__.__name__


def _chat_completion(cfg: dict[str, Any], messages: list[dict[str, str]],
                     timeout: int = 45, max_tokens: int | None = None) -> str:
    """OpenAI 兼容 chat/completions 调用（任何厂商预设 / 自定义 base_url）。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.3,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    data = _http_json(url, cfg["api_key"], json.dumps(payload).encode("utf-8"), timeout=timeout)
    return data["choices"][0]["message"]["content"].strip()


def list_models(base_url: str, api_key: str, timeout: int = 20) -> dict[str, Any]:
    """拉取厂商模型列表（GET {base_url}/models）。失败不影响聊天，仅作辅助。"""
    base = normalize_base_url("", base_url)
    if not base:
        return {"ok": False, "models": [], "message": "请先填 Base URL"}
    url = base.rstrip("/") + "/models"
    try:
        data = _http_json(url, api_key, None, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 统一翻译
        return {"ok": False, "models": [], "message": describe_http_error(exc, base)}
    items = data.get("data") if isinstance(data, dict) else None
    models = [str(m.get("id")) for m in (items or []) if isinstance(m, dict) and m.get("id")]
    return {"ok": True, "models": models[:200], "message": f"共 {len(models)} 个模型"}


def test_ai_config(db_path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """「测试连接」：用当前表单（未保存也可）发一次最小请求，返回可读结果。"""
    payload = payload or {}
    saved = read_sources_config(db_path)
    provider = payload.get("provider") or saved.get("provider") or "agnes"
    if provider not in PROVIDER_PRESETS:
        provider = "openai_compatible"
    api_key = (payload.get("api_key") or "").strip()
    if not api_key or "*" in api_key:
        api_key = saved.get("api_key") or ""
    raw_base = payload.get("base_url")
    if not raw_base:
        # 只有同一个厂商时才沿用已保存地址，否则用该厂商的预设（免得拿 Agnes 的地址打 DeepSeek）
        raw_base = saved.get("base_url") if provider == saved.get("provider") else None
    cfg = {
        "provider": provider,
        "base_url": normalize_base_url(provider, raw_base),
        "model": _normalize_model(provider, payload.get("model") or (
            saved.get("model") if provider == saved.get("provider") else None)),
        "api_key": api_key,
    }
    if not cfg["base_url"]:
        return {"ok": False, "message": "请先填 Base URL（或选一个厂商预设）"}
    if not cfg["model"]:
        return {"ok": False, "message": "请先填模型名（可点「拉取模型列表」选一个）"}
    if not cfg["api_key"]:
        return {"ok": False, "message": "请先填 API Key"}

    started = time.time()
    try:
        reply = _chat_completion(
            cfg,
            [{"role": "user", "content": "回复两个字：连通"}],
            timeout=30,
            max_tokens=32,
        )
    except Exception as exc:  # noqa: BLE001
        elapsed = int((time.time() - started) * 1000)
        result = {"ok": False, "message": describe_http_error(exc, cfg["base_url"]),
                  "elapsed_ms": elapsed, "base_url": cfg["base_url"], "model": cfg["model"]}
        # 只有"模型名不对"这一类才顺手拉列表；401/404/网络不通再拉一次只会让用户多等
        if isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 422):
            models = list_models(cfg["base_url"], cfg["api_key"], timeout=10)
            if models.get("ok"):
                result["models"] = models["models"]
        return result
    elapsed = int((time.time() - started) * 1000)
    models = list_models(cfg["base_url"], cfg["api_key"], timeout=12)
    return {
        "ok": True,
        "message": f"连接成功（{elapsed} ms），模型回复：{reply[:60]}",
        "elapsed_ms": elapsed,
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "models": models.get("models", []),
    }


# ---------------------------------------------------------------------------
# 上下文组装：聚合数字 + 脱敏标签（客户 A/B/C）
# ---------------------------------------------------------------------------

def build_anonymized_context(db_path: str, company_id: int = 1,
                            anonymize: bool = True) -> tuple[dict[str, Any], dict[str, str], dict[str, Any]]:
    """组装发给 LLM 的上下文。

    anonymize=True 时把客户/供应商换成"客户A/B"，False 时用真实名称
    （v1.0.6：默认给真实名称，老板要看到自己认识的公司名）。
    """
    dashboard = engine.build_dashboard(db_path, company_id)
    counterparty_labels: dict[str, str] = {}
    if anonymize:
        for idx, cust in enumerate(dashboard.get("ar_top", [])):
            counterparty_labels[cust["name"]] = f"客户{chr(ord('A') + idx)}"

    runway = dashboard.get("runway")
    months_left = (runway or {}).get("months_left") or {}
    payable = dashboard.get("payable") or {}
    spend = dashboard.get("spend_breakdown") or {}
    recent_flow = dashboard.get("recent_flow")

    supplier_labels: dict[str, str] = {}
    if anonymize:
        for idx, sup in enumerate((payable.get("suppliers") or [])[:10]):
            supplier_labels[sup["name"]] = f"供应商{chr(ord('A') + idx)}"

    # 规则生成的文本（速览/行动清单）含真实单位名，发给 LLM 前统一替换为代称
    def _anonymize_text(text: str) -> str:
        for real, label in {**counterparty_labels, **supplier_labels}.items():
            text = text.replace(real, label)
        return text

    action_items = [
        {"紧急度": {"high": "高", "mid": "中", "low": "低"}.get(a.get("level"), a.get("level", "")),
         "事项": _anonymize_text(a.get("text", ""))}
        for a in dashboard.get("action_items", [])
    ]

    ctx: dict[str, Any] = {
        "数据截至": dashboard.get("as_of") or "未导入",
        "当前现金": runway["start_cash"] if runway else None,
        "月固定支出合计": runway["monthly_fixed"] if runway else None,
        "安全线": runway["safety_line"] if runway else None,
        "近3月经营净流均值": runway["net_op_avg"] if runway else None,
        "现金跑道预警": runway["alert"]["text"] if runway else "未导入流水",
        "还能撑几个月": {
            "中性": months_left.get("neutral"),
            "悲观": months_left.get("pessimistic"),
            "乐观": months_left.get("optimistic"),
        },  # None = 展望 6 个月内现金不见负
        "最近30天收支": recent_flow,  # {in30, out30, net30, prev_net30}
        "应收总额（未收回）": dashboard["aging"]["total_receivable"],
        "开具总额": dashboard["aging"]["total_issued"],
        "已匹配收款": dashboard["aging"]["matched_total"],
        "整体回款率%": dashboard["collection"]["rate_overall"],
        "应收账龄桶": {
            b["bucket"]: {"金额": b["amount"], "笔数": b["count"]}
            for b in dashboard["aging"]["buckets"]
        },
        "应收TOP": [
            {
                "代称": counterparty_labels.get(
                    c["name"],
                    f"客户{chr(ord('A') + c['rank'] - 1)}" if anonymize else c["name"],
                ),
                "应收余额": c["amount"],
                "最长拖天数": c.get("max_days", 0),
                "90天以上": c["overdue_90"],
                "高风险": c["high_risk"],
            }
            for c in dashboard["ar_top"][:10]
        ],
        "应付（我欠供应商）": {
            "未付总额": payable.get("total", 0.0),
            "取得发票总额": payable.get("total_received_inv"),
            "已支付": payable.get("matched_total"),
            "未付张数": payable.get("unpaid_count", 0),
            "供应商TOP": [
                {"代称": s_label, "未付金额": s["amount"], "拿票最长天数": s["days_max"]}
                for s_label, s in (
                    (supplier_labels.get(s["name"], f"供应商{chr(ord('A') + i)}" if anonymize else s["name"]), s)
                    for i, s in enumerate((payable.get("suppliers") or [])[:10])
                )
            ],
        },
        "支出结构（近6月）": {
            "合计": spend.get("total"),
            "分类": [
                {"类别": c["name"], "金额": c["amount"], "占比%": c["share"]}
                for c in (spend.get("categories") or [])
            ],
        },
        "本周要办": action_items,
        "收支趋势（近月）": dashboard.get("cash_trend", [])[-3:],
        "项目盈利": dashboard.get("projects"),
    }
    if not anonymize:
        # 用真名时把"代称"这个键改掉，免得 LLM 看着别扭
        for item in ctx["应收TOP"]:
            item["客户名称"] = item.pop("代称")
        for item in ctx["应付（我欠供应商）"]["供应商TOP"]:
            item["供应商名称"] = item.pop("代称")
    return ctx, counterparty_labels, dashboard


# ---------------------------------------------------------------------------
# 四大场景
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "你是老板的经营分析助理。只使用给定 JSON 上下文中的数字回答，"
    "禁止编造上下文里没有的数字；数据未覆盖的问题要如实说'当前数据未覆盖'。"
    "应收（别人欠我）与应付（我欠供应商）是经营口径估算值（开票−已收 / 取得−已付），"
    "不是会计报表数字，回答时如实说明。"
    "回答用中文口语，简洁直接，关键数字要引用，站在经营者视角而非会计视角。"
    "结尾不要加免责声明以外的废话。"
)


def ai_chat(db_path: str, question: str, company_id: int = 1) -> dict[str, Any]:
    """场景一：经营问答。"""
    cfg = read_sources_config(db_path)
    if not cfg["enabled"]:
        return {
            "mode": "rule",
            "answer": "本地规则模式：自由问答需要配置云 LLM API。请到「设置 → AI 接入」填写 Key 后开启。",
            "disabled": True,
        }
    ctx, labels, dashboard = build_anonymized_context(db_path, company_id, anonymize=cfg["anonymize"])
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"当前看板数据{ '（客户已脱敏）' if cfg['anonymize'] else ''}：\n{json.dumps(ctx, ensure_ascii=False)}"},
        {"role": "user", "content": question},
    ]
    try:
        answer = _chat_completion(cfg, messages)
    except Exception as exc:  # noqa: BLE001 - 统一翻译成可读提示
        return {"mode": "cloud",
                "answer": f"AI 调用失败：{describe_http_error(exc, cfg['base_url'])}",
                "error": True}
    return {"mode": "cloud", "answer": answer, "context_labels": labels}


def _party_label(item: dict[str, Any]) -> str:
    """上下文里的客户/供应商名字：脱敏时是"代称"，真名时是"客户名称/供应商名称"。"""
    return str(item.get("客户名称") or item.get("供应商名称") or item.get("代称") or "")


def rule_mode_briefing(db_path: str, company_id: int = 1, anonymize: bool = True) -> str:
    """降级周报：写死模板要点列表（口径与驾驶舱一致，含撑月数/应付/支出结构/行动）。"""
    ctx, _, _ = build_anonymized_context(db_path, company_id, anonymize=anonymize)
    lines = ["【本周经营要点（本地规则模式）】"]
    if ctx["当前现金"] is not None:
        ml = ctx["还能撑几个月"]
        neutral_txt = f"{ml['中性']} 个月" if ml["中性"] is not None else "6 个月以上"
        pess_txt = f"{ml['悲观']} 个月" if ml["悲观"] is not None else "6 个月以上"
        lines.append(f"当前现金 {ctx['当前现金']:,.2f} 元，月固定支出 {ctx['月固定支出合计']:,.2f} 元。")
        lines.append(f"按现在节奏大约还能撑 {neutral_txt}（最差 {pess_txt}）；{ctx['现金跑道预警']}。")
    else:
        lines.append("尚未导入银行流水，无法计算现金跑道。")
    lines.append(f"应收（别人欠我）{ctx['应收总额（未收回）']:,.2f} 元，整体回款率 {ctx['整体回款率%']}%。")
    if ctx["应收TOP"]:
        top = ctx["应收TOP"][0]
        days_txt = f"、最长拖 {top['最长拖天数']} 天" if top.get("最长拖天数") else ""
        lines.append(f"欠款最多：{_party_label(top)} {top['应收余额']:,.2f} 元{days_txt}。")
    ap = ctx["应付（我欠供应商）"]
    if ap["未付总额"]:
        lines.append(f"应付供应商约 {ap['未付总额']:,.2f} 元未付。")
    spend = ctx["支出结构（近6月）"]
    if spend["合计"]:
        top_cat = spend["分类"][0] if spend["分类"] else None
        if top_cat:
            lines.append(f"近 6 个月支出 {spend['合计']:,.2f} 元，最大头「{top_cat['类别']}」占 {top_cat['占比%']}%。")
    risky = [c for c in ctx["应收TOP"] if c["高风险"]]
    if risky:
        lines.append("高风险客户：" + "、".join(f"{_party_label(c)}({c['应收余额']:,.0f}元)" for c in risky[:3])
                     + "，建议跟进。")
    for item in ctx["本周要办"][:3]:
        lines.append(f"· {item['事项']}")
    return "\n".join(lines)


def ai_briefing(db_path: str, company_id: int = 1) -> dict[str, Any]:
    """场景二：周简报。无 API → 要点列表。"""
    cfg = read_sources_config(db_path)
    if not cfg["enabled"]:
        return {"mode": "rule", "content": rule_mode_briefing(db_path, company_id, cfg["anonymize"])}
    ctx, labels, _ = build_anonymized_context(db_path, company_id, anonymize=cfg["anonymize"])
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            "请根据以下看板数据写一段老板能直接转发到管理群的口语化周简报（150字以内，"
            "包含现金、还能撑几个月、应收与回款要点，应付或支出结构有异常也提一句，"
            "数字保留千分位）：\n"
            + json.dumps(ctx, ensure_ascii=False)},
    ]
    try:
        content = _chat_completion(cfg, messages)
    except Exception as exc:  # noqa: BLE001
        return {"mode": "rule", "content": rule_mode_briefing(db_path, company_id, cfg["anonymize"]),
                "note": f"AI 调用失败，已退回本地模板：{describe_http_error(exc, cfg['base_url'])}"}
    return {"mode": "cloud", "content": content, "context_labels": labels}


def ai_attribute_alert(db_path: str, alert_text: str, company_id: int = 1) -> dict[str, Any]:
    """场景三：异常归因。无 API → 模板话术。"""
    cfg = read_sources_config(db_path)
    if not cfg["enabled"]:
        ctx, _, _ = build_anonymized_context(db_path, company_id, anonymize=cfg["anonymize"])
        risky = [c for c in ctx["应收TOP"] if c["高风险"]]
        detail = ("；".join(
            f"{_party_label(c)}欠 {c['应收余额']:,.0f} 元（最长拖 {c['最长拖天数']} 天，90 天以上 {c['90天以上']:,.0f} 元）"
            for c in risky[:3]) or "账龄结构尚可") if ctx["应收TOP"] else "无应收数据"
        return {
            "mode": "rule",
            "answer": f"预警原因：{alert_text}。主要拖累：{detail}。建议：优先跟进 90 天以上账龄客户，"
                      "并核对下月固定支出是否可延后。",
        }
    ctx, labels, _ = build_anonymized_context(db_path, company_id, anonymize=cfg["anonymize"])
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"看板数据{ '（客户已脱敏）' if cfg['anonymize'] else ''}：\n{json.dumps(ctx, ensure_ascii=False)}"},
        {"role": "user", "content": f"用三句话说清这条预警：为什么触发、影响什么、建议先查什么。预警内容：{alert_text}"},
    ]
    try:
        answer = _chat_completion(cfg, messages)
    except Exception as exc:  # noqa: BLE001
        return {"mode": "rule", "answer": rule_mode_briefing(db_path, company_id, cfg["anonymize"]),
                "note": f"AI 调用失败，已退回本地模板：{describe_http_error(exc, cfg['base_url'])}"}
    return {"mode": "cloud", "answer": answer}


def ai_scenario_question(db_path: str, client_name: str, company_id: int = 1) -> dict[str, Any]:
    """场景四：情景问答（"如果客户 X 拖到 90 天会怎样"）。

    本地现金模型先临时覆盖该客户回款假设 → 重算三场景 → AI 解释。
    """
    result = engine.what_if_client(db_path, client_name, company_id)
    if not result.get("found"):
        return {"mode": "rule", "answer": result.get("message", "未找到该客户"), "found": False}
    original_alert = result["original_alert"]["text"]
    new_alert = result["new_alert"]["text"]
    rule_answer = (
        f"假设「{result['client']}」的 {result['moved_amount']:,.2f} 元应收全部拖到 90 天以上：\n"
        f"原跑道：{original_alert}\n"
        f"新跑道：{new_alert}"
    )
    cfg = read_sources_config(db_path)
    if not cfg["enabled"]:
        return {"mode": "rule", "answer": rule_answer, "found": True, "runway": result["runway"]}
    ctx = {
        "当前现金": result["runway"]["start_cash"],
        "月固定支出": result["runway"]["monthly_fixed"],
        "原预警": original_alert,
        "新预警": new_alert,
        "新三场景曲线（月末现金）": {
            s["label"]: [p["cash"] for p in s["curve"]] for s in result["runway"]["scenarios"].values()
        },
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            f"老板问：如果客户（代称 {client_name}）拖到 90 天才付款会怎样？"
            f"本地模型已重算（数字如下），请用口语解释变化与建议（120字内）：\n"
            + json.dumps(ctx, ensure_ascii=False)},
    ]
    try:
        answer = _chat_completion(cfg, messages)
    except Exception as exc:  # noqa: BLE001
        return {"mode": "rule", "answer": rule_answer, "found": True,
                "note": f"AI 调用失败，已退回本地模板：{describe_http_error(exc, cfg['base_url'])}",
                "runway": result["runway"]}
    return {"mode": "cloud", "answer": answer, "found": True, "runway": result["runway"]}
