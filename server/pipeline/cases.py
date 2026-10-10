"""箱子稀有度：整套判断的支点。

核心因果链：
    箱子稀有 → 开箱量低 → 该箱物品新增供给趋近于零 → 存世量饱和
    → 价格下跌变成「存量恐慌抛售」而不是「供给洪水的价值毁灭」
    → 下方有硬支撑，这才是能赚钱的那种下跌

可验证的代理变量（全部可从市场数据算出，不依赖传闻）：
    case_volume_30d    箱子在 Steam 市场的日成交量 ≈ 开箱量 ≈ 新增供给速率
    supply_growth_30d  该箱物品存世量的 30 日增速，趋近 0 = 供给饱和
    drop_pool          是否仍在掉落池（决定新增供给是否永久归零）
    case_price         箱子的市场价（稀有箱子自己就贵，开箱成本高 → 开箱意愿低）
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

log = logging.getLogger("cases")

# 掉落池状态需要维护：CS2 每次更新都可能增删箱子。
# 生产里由 llm.extract_case_changes() 从更新日志里抽，这里给出人工兜底表。
DROP_POOL_OVERRIDES: dict[str, str] = {
    # "Prisma Case": "active",
    # "Prisma 2 Case": "active",
}

RARITY_PROMPT = """你是 CS2 饰品市场的供给分析师。为下面这个武器箱打一个「稀有度」评分。

箱子名：{case_name}
箱子的市场价：¥{case_price}
箱子 30 日日成交量（≈开箱量）：{case_volume}
该箱物品存世量 30 日增速：{supply_growth}%
掉落池状态：{drop_pool}

评分维度（每项 0-25 分，总分 0-100）：
1. 掉落可用性：已从掉落池移除 = 25；仍在掉落 = 0
2. 供给饱和：存世量增速 <0.5% = 25；>5% = 0（线性）
3. 开箱热度衰减：箱子成交量越低分越高（低成交 = 新供给少）
4. 绝对稀缺度：结合箱子价格判断，箱子越贵说明市场存量越少

只输出 JSON，不要任何解释文字：
{{"score": 0-100 的数字, "rationale": "一句话说明为什么这个分数", "blocking_risk": "这个箱子最大的风险是什么"}}
"""


def score_cases_rule_based(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """纯规则评分：没有任何外部依赖，先跑通流程用这个。"""
    out: list[dict[str, Any]] = []
    vols = [c.get("case_volume_30d") or 0 for c in cases if c.get("case_volume_30d")]
    vmax = max(vols) if vols else 1.0

    for c in cases:
        s = 0.0
        reasons: list[str] = []

        # 1) 掉落可用性
        pool = c.get("drop_pool") or DROP_POOL_OVERRIDES.get(c.get("case_name", ""), "unknown")
        if pool == "removed":
            s += 25
            reasons.append("已移出掉落池，新增供给永久归零")
        elif pool == "active":
            reasons.append("仍在掉落池")
        else:
            s += 12
            reasons.append("掉落池状态未知（按中性处理）")

        # 2) 供给饱和
        growth = c.get("supply_growth_30d")
        if growth is not None:
            sat = max(0.0, min(25.0, 25.0 * (1.0 - min(growth, 5.0) / 5.0)))
            s += sat
            reasons.append(f"存世量 30 日增速 {growth:.2f}%")

        # 3) 开箱热度（成交量越低越稀有）
        vol = c.get("case_volume_30d") or 0
        if vmax > 0:
            s += 25.0 * (1.0 - min(vol / vmax, 1.0) ** 0.5)
            reasons.append(f"箱子日成交 {vol:.0f}")

        # 4) 箱子价（越贵越稀缺，用对数缩放避免极端值主导）
        cp = c.get("case_price")
        if cp is not None and cp > 0:
            s += max(0.0, min(25.0, 12.0 * (1.0 + (cp / 10.0) ** 0.5 - 1.0)))
            reasons.append(f"箱子价 ¥{cp:.2f}")

        out.append({
            **c,
            "rarity_score": round(min(s, 100.0), 1),
            "drop_pool": pool,
            "rationale": "；".join(reasons),
            "blocking_risk": "掉落池仍在活跃状态，新增供给会持续压制价格"
            if pool == "active"
            else "存世量增速仍高，供给未见饱和"
            if (growth or 0) > 2.0
            else None,
        })
    return out


def score_cases_llm(cases: list[dict[str, Any]], llm_call) -> list[dict[str, Any]]:
    """用 LLM 覆盖规则评分（llm_call 是 llm.py 里的 chat_json）"""
    import json

    out: list[dict[str, Any]] = []
    for c in cases:
        prompt = RARITY_PROMPT.format(
            case_name=c.get("case_name", ""),
            case_price=c.get("case_price") or 0,
            case_volume=c.get("case_volume_30d") or 0,
            supply_growth=c.get("supply_growth_30d") or 0,
            drop_pool=c.get("drop_pool", "unknown"),
        )
        try:
            parsed = llm_call(prompt)
            out.append({
                **c,
                "rarity_score": float(parsed.get("score", 50)),
                "rationale": str(parsed.get("rationale", ""))[:500],
                "blocking_risk": str(parsed.get("blocking_risk", ""))[:500] or None,
            })
        except Exception as e:
            log.warning("LLM 评分失败（%s），回退规则评分", e)
            out.append(score_cases_rule_based([c])[0])
    return out


def write_cases(con: sqlite3.Connection, cases: list[dict[str, Any]], now: int) -> int:
    n = 0
    for c in cases:
        if not c.get("case_id"):
            continue
        con.execute(
            """INSERT OR REPLACE INTO case_meta
               (case_id, case_name, rarity_score, case_price, case_volume_30d,
                supply_growth_30d, drop_pool, rationale, blocking_risk, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                c["case_id"], c.get("case_name"), c.get("rarity_score"),
                c.get("case_price"), c.get("case_volume_30d"), c.get("supply_growth_30d"),
                c.get("drop_pool"), c.get("rationale"), c.get("blocking_risk"), now,
            ),
        )
        n += 1
    con.commit()
    return n


def load_cases(con: sqlite3.Connection) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM case_meta").fetchall()
    return [dict(r) for r in rows]


def collect_case_metrics(session, cases: list[dict[str, str]]) -> list[dict[str, Any]]:
    """采集箱子自身的市场指标。

    cases: [{"case_id": ..., "case_name": ..., "market_hash_name": "Prisma 2 Case"}]
    箱子的成交量直接用 Steam 市场的 priceoverview.volume —— 箱子主要被买来开，
    所以箱子成交量是该箱物品新增供给速率的最佳公开代理。
    """
    import requests

    out: list[dict[str, Any]] = []
    for c in cases:
        row: dict[str, Any] = dict(c)
        try:
            r = session.get(
                "https://steamcommunity.com/market/priceoverview/",
                params={"appid": 730, "currency": 23, "market_hash_name": c["market_hash_name"]},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            j = r.json()
            if j.get("success"):
                row["case_price"] = _to_float(j.get("lowest_price"))
                row["case_volume_30d"] = _to_float(j.get("volume"))
        except Exception as e:
            log.warning("箱子 %s 采集失败：%s", c.get("case_name"), e)
        out.append(row)
    return out


def _to_float(s: Any) -> float | None:
    if not s:
        return None
    try:
        return float("".join(ch for ch in str(s) if ch.isdigit() or ch == "."))
    except ValueError:
        return None
