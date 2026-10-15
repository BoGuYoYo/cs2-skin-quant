"""决策层 + 异动检测。

这里有两件独立的事，别混：
  A) 异动检测（detect_anomaly）：用于「提醒你现在有东西在崩」，日线级、多因子打分
  B) 建仓决策（decide）：用于「现在该不该买、买多少」，吃的是分位数回归的输出

分位数回归的全部意义在 B 里的第一条：P10 折算回来还亏 → 直接排除。
点预测做不到这件事，它只会告诉你「预期 +18%」，然后你在尾部亏掉本金。

⚠️ 曾经在这里设计、但未接入本模块的三个函数已删除（需要时从 git 历史取回）：
    tranche()      分批建仓节奏（越接近 28 天跌势越接近满仓）
    in_cooldown()  告警冷却（现在由 run_daily 基于 alerts.pushed + cfg.levels 持久化实现）
    confirm()      闪崩二次确认（要求连续 confirm_n 个采样点都达标）
截断而不是留着：无人调用的函数会随依赖（cfg 字段、numpy API）一起烂掉，
而且让人以为「分批建仓/冷却/二次确认」已经生效了 —— 实际上线上一次都没跑过。
分批与二次确认仍未接入；冷却已经落到 alerts.pushed，前端「已推送」显示真实状态。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import Cfg


# ------------------------------------------------------------------ A. 异动检测
def _simple_pct(v: Any) -> float:
    """对数净收益 → 简单净收益百分比。

    ⚠️ 理由串里的收益**必须**过这一层（审查 9 发现）。
    踩过的坑：打分那一行老老实实写了 expm1，紧接着打印那一行却写
    `f"21日 {r21 * 100:.1f}%"` —— 同一行代码里两套口径，于是
    ret_21d = -0.501（真实跌 39.4%）在看板上显示成「21日 -50.1%」，
    手机推送、alerts.detail、详情页三处一起夸大。而前端 format.ts 的 logPct()
    和 push/notify.log_to_simple_pct 都是对的，只有这一路错 —— 属于
    「同一个数在三个地方显示成两个值」这类最难被发现的 bug。

    公式不在这里重写：唯一实现在 push/notify.log_to_simple_pct（带完整踩坑记录），
    惰性导入避免 pipeline 在 import 期就依赖 push（push 会读环境变量、拉 requests）。
    """
    from push.notify import log_to_simple_pct   # 运行时才导入，见 docstring
    return log_to_simple_pct(v)


def detect_anomaly(row: dict[str, Any], cfg: Cfg) -> tuple[str | None, float, str]:
    """返回 (等级 或 None, 打分, 理由串)"""
    score = 0.0
    why: list[str] = []

    r21 = row.get("ret_21d")
    if r21 is not None and not _nan(r21) and r21 < 0:
        drop = abs(math.expm1(r21)) * 100  # 对数收益 → 百分比跌幅
        score += min(drop / 25.0, 1.0) * 40
        why.append(f"21日 {_simple_pct(r21):.1f}%")

    r7 = row.get("ret_7d")
    if r7 is not None and not _nan(r7) and r7 < 0:
        # 与 r21 一样先还原简单跌幅，再按同一阈值打分。
        drop7 = abs(math.expm1(r7))
        score += min(drop7 / 0.06, 1.0) * 15
        why.append(f"7日 {_simple_pct(r7):.1f}%")

    z = row.get("vol_z")
    if z is not None and not _nan(z) and z < 0:
        score += min(abs(z) / 5.0, 1.0) * 25
        why.append(f"vol_z={z:.2f}")

    slope = row.get("list_slope_7d")
    if slope is not None and not _nan(slope) and slope > 30:
        score += 10
        why.append(f"挂单+{slope:.0f}%")

    # 相对强度：弱于大盘才有意义（否则只是大盘在跌）
    rel = row.get("rel_ret_21d")
    if rel is not None and not _nan(rel) and rel < -0.10:
        score += 10
        why.append(f"弱于大盘 {_simple_pct(rel):.1f}%")

    # ---- 流动性闸门 ----
    listings = row.get("listings")
    volume = row.get("volume")
    if listings is not None and not _nan(listings) and listings < cfg.gate_listings:
        return None, score, "; ".join(why + ["流动性不足(挂单少)"])
    if volume is not None and not _nan(volume) and volume < cfg.gate_volume:
        return None, score, "; ".join(why + ["流动性不足(无成交)"])

    # 必须从最高等级往下匹配。
    # 踩过的坑：原先是按 cfg.levels 的正序（L1 → L2 → L3）遍历并 break，
    # 于是任何超过 L1 阈值的暴跌都先命中 L1 就退出了 —— 跌 62.8% 也只报 L1。
    # 而 run_daily 只对 L3 发单品通知，结果线上 1253 条告警没有一条能触发推送。
    level = None
    for lv, drop_th, z_th, _cd in reversed(cfg.levels):
        ok_drop = r21 is not None and not _nan(r21) and r21 <= math.log1p(drop_th / 100.0)
        ok_z = z is not None and not _nan(z) and z <= -z_th
        if ok_drop and (ok_z or score >= 55):
            level = lv
            break
    return level, score, "; ".join(why)


def _nan(v: Any) -> bool:
    try:
        return bool(np.isnan(v))
    except (TypeError, ValueError):
        return True


# ------------------------------------------------------------------ B. 建仓决策
def score(row: dict[str, Any], cfg: Cfg) -> float:
    """综合打分，只用于排序展示；真正的取舍在 decide() 里"""
    p10 = row.get("q10_adj")
    prob = row.get("prob_up") or 0.0
    rarity = row.get("case_rarity_score") or 50.0
    days = row.get("drop_duration_days")
    list_ratio = row.get("list_ratio")

    s = 0.0
    if p10 is not None and not _nan(p10):
        s += 40.0 * float(np.clip(p10 / 0.30, 0.0, 1.0))       # 下沿越接近盈亏平衡越差
    s += 25.0 * float(prob)
    s += 15.0 * float(np.clip((rarity - 40.0) / 60.0, 0.0, 1.0))

    # 时间窗匹配度：你的 2~4 周先验
    if days is not None and not _nan(days):
        d = float(days)
        if 14 <= d <= 30:
            s += 15.0
        elif 7 <= d < 14:
            s += 7.0
        elif d > 30:
            s += 3.0

    if list_ratio is not None and not _nan(list_ratio):
        s += 5.0 * float(np.clip(1.0 - list_ratio / max(cfg.list_ratio_max, 1e-6), 0.0, 1.0))
    return round(s, 2)


def decide(row: dict[str, Any], cfg: Cfg) -> dict[str, Any]:
    """分位数 → {side, size, why}"""
    p10, p50, p90 = row.get("q10_adj"), row.get("q50"), row.get("q90_adj")
    if any(v is None or _nan(v) for v in (p10, p50, p90)):
        return {"side": "skip", "size": 0.0, "why": "预测缺失"}

    # ① 下沿都亏 → 不碰。这是分位数回归相对点预测的全部价值所在。
    if p10 <= 0.0:
        return {"side": "skip", "size": 0.0, "why": f"下沿 {math.expm1(p10):+.1%} 仍为负"}

    # ② 赔率不足（已扣双边费 + 提现）→ 只观察
    if p50 < cfg.edge_min:
        return {"side": "watch", "size": 0.0, "why": f"中位仅 {math.expm1(p50):+.1%}，不足 {cfg.edge_min:.0%}"}

    # ③ 保守 Kelly：把 P10 当亏损情形，赔率 = p50 / (p50 - p10)
    risk = max(p50 - p10, 1e-6)
    ratio = p50 / risk
    kelly = max(0.0, min(1.0, (ratio - 1.0) / ratio)) if ratio > 1 else 0.0
    # 半 Kelly 再打对折。注意 kelly ∈ [0,1]，所以 unit*kelly*0.5 ≤ unit/2 < unit ——
    # 原先外面套了个 min(cfg.unit, ...)，那个 min 永远取不到 cfg.unit（审查发现
    # 是死代码），去掉以免让人以为"满仓上限"真的会生效。
    size = cfg.unit * kelly * 0.5

    days = row.get("drop_duration_days")
    fit_note = ""
    if days is not None and not _nan(days) and float(days) > 45:
        size *= 0.5
        fit_note = "；下跌超 45 天，仓位减半"

    return {
        "side": "buy",
        "size": round(size, 4),
        "why": (f"P10 {math.expm1(p10):+.1%} / P50 {math.expm1(p50):+.1%} / P90 {math.expm1(p90):+.1%}"
                f"，赔率 {ratio:.2f}，仓位 {size:.2%}{fit_note}"),
    }
