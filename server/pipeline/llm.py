"""LLM 层：只做三件事，绝不做价格预测。

  1) 事件抽取：把 CS2 更新日志/公告转成结构化供给事件（最高价值）
  2) 箱子稀有度辅助判断（在 cases.py 里调用）
  3) 投研简报：把模型输出整理成一页人话，强制同时列看多与看空证据

硬约束：所有 prompt 都禁止模型输出价格预测数字。它整理证据，你下判断。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import requests

log = logging.getLogger("llm")

BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
API_KEY = os.environ.get("LLM_API_KEY", "")
MODEL = os.environ.get("LLM_MODEL", "")

EVENT_SYSTEM = """你是 CS2 饰品市场的供给事件抽取器。
输入：CS2 更新日志 / 官方公告 / 补丁说明全文。
输出：严格 JSON，不要任何解释文字，不要 markdown 代码块。

规则：
1. 只抽取会改变【饰品供给】或【需求】的事实；皮肤视觉调整、UI 修改、性能优化一律忽略。
2. affected_cases 填受影响的武器箱英文名（如 "Prisma 2 Case"），填不出就填空数组。
3. direction 是该箱物品价格的影响方向，不是你对该事件的评价。
4. 绝对不要给出任何价格预测数字。magnitude 用 1-5 档位，不是金额。
5. evidence_quote 必须逐字引用原文中的一句话，不允许改写或翻译。"""

EVENT_USER = """更新内容：
---
{text}
---

输出格式：
{{"events": [{{"event_type": "drop_pool_change|craft_change|case_removed|case_added|price_rule_change|other", "affected_cases": ["..."], "affected_items": ["..."], "direction": "bullish|bearish|neutral", "magnitude": 1, "horizon_days": 30, "confidence": 0.0, "evidence_quote": "逐字引用", "reasoning": "一句话说明它如何改变供给"}}]}}"""

BRIEF_SYSTEM = """你是一名 CS2 饰品投研助理。把原始数据整理成一页结构化简报。

【硬约束】
1. 绝对不要预测任何价格数字，不要给"目标价""预期收益"。
2. 只陈述输入里的事实，不要引入外部信息。
3. 必须同时写出"看多证据"和"看空证据"，不允许只写一侧。
4. 结尾必须回答"这个判断最可能错在哪里"。"""

BRIEF_USER = """{item_name} 当前价 ¥{price}
30日回撤 {dd}% ｜ 下跌持续 {days} 天
存世量同箱分位 {peer_pct}% ｜ 存世量 30 日增速 {supply_growth}%
在售占比 {list_ratio}% ｜ 换手率 {turnover}%
所属箱子 {case_name}，稀有度 {rarity}/100，箱子日成交 {case_volume}
模型输出：21日收益 P10={p10} P50={p50} P90={p90}，反弹概率 {prob}
近期相关事件：{events}

按以下结构输出 Markdown：
## 结论档位：观察 / 候选 / 排除
## 供给面
## 需求面
## 时间窗匹配度（对照"下跌趋势持续 2~4 周"的经验先验）
## 看多证据（逐条附数据）
## 看空证据（逐条附数据）
## 最可能错在哪"""


def enabled() -> bool:
    return bool(BASE_URL and API_KEY and MODEL)


def chat_json(prompt: str, system: str | None = None, timeout: int = 90, retries: int = 2) -> dict[str, Any]:
    """调用 OpenAI 兼容接口并解析 JSON（自动剥掉 ```json 围栏）"""
    if not enabled():
        raise RuntimeError("LLM 未配置（LLM_BASE_URL / LLM_API_KEY / LLM_MODEL）")

    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                f"{BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
                json={"model": MODEL, "messages": messages, "temperature": 0.1},
                timeout=timeout,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            return _parse_json(content)
        except Exception as e:
            last = e
            log.warning("LLM 调用失败（第 %d 次）：%s", attempt + 1, e)
    raise RuntimeError(f"LLM 调用失败：{last}")


def chat_text(prompt: str, system: str | None = None, timeout: int = 120) -> str:
    if not enabled():
        raise RuntimeError("LLM 未配置")
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    r = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages, "temperature": 0.3},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _parse_json(text: str) -> dict[str, Any]:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def extract_supply_events(text: str) -> list[dict[str, Any]]:
    """更新日志 → 结构化供给事件。每个事件必须带逐字引用，方便人工抽查。"""
    if not enabled():
        log.info("LLM 未配置，跳过事件抽取")
        return []
    # 长文分块，避免超上下文
    chunks = _chunk(text, 6000)
    events: list[dict[str, Any]] = []
    for ch in chunks:
        try:
            parsed = chat_json(EVENT_USER.format(text=ch), system=EVENT_SYSTEM)
        except Exception as e:
            log.warning("事件抽取失败：%s", e)
            continue
        for ev in parsed.get("events") or []:
            if not isinstance(ev, dict):
                continue
            if not ev.get("evidence_quote"):
                continue  # 没有逐字引用的一律丢弃，压幻觉
            events.append(ev)
    return events


def brief(data: dict[str, Any]) -> str:
    """生成投研简报；LLM 不可用时回退到纯数据模板"""
    if enabled():
        try:
            return chat_text(BRIEF_USER.format(**data), system=BRIEF_SYSTEM)
        except Exception as e:
            log.warning("简报生成失败，回退模板：%s", e)
    return _fallback_brief(data)


def _fallback_brief(d: dict[str, Any]) -> str:
    return "\n".join([
        f"## 结论档位：{d.get('side', '—')}",
        f"## 供给面\n- 箱子 {d.get('case_name')}，稀有度 {d.get('rarity')}/100，日成交 {d.get('case_volume')}",
        f"- 存世量 30 日增速 {d.get('supply_growth')}%，同箱分位 {d.get('peer_pct')}%",
        f"## 需求面\n- 换手率 {d.get('turnover')}%，在售占比 {d.get('list_ratio')}%",
        f"## 时间窗\n- 已下跌 {d.get('days')} 天（经验甜蜜区 14~30 天）",
        f"## 模型输出\n- P10 {d.get('p10')} / P50 {d.get('p50')} / P90 {d.get('p90')}，反弹概率 {d.get('prob')}",
        "## 看多证据\n- （LLM 未启用，请人工研判）",
        "## 看空证据\n- （LLM 未启用，请人工研判）",
        "## 最可能错在哪\n- （LLM 未启用，请人工研判）",
    ])


def _chunk(text: str, size: int) -> list[str]:
    paras = text.split("\n\n")
    out: list[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 > size and buf:
            out.append(buf)
            buf = ""
        buf += p + "\n\n"
    if buf.strip():
        out.append(buf)
    return out
