"""决策层 + 异动检测。

这里有两件独立的事，别混：
  A) 异动检测（detect_anomaly）：用于「提醒你现在有东西在崩」，日线级、多因子打分
  B) 建仓决策（decide）：用于「现在该不该买、买多少」，吃的是分位数回归的输出

分位数回归的全部意义在 B 里的第一条：P10 折算回来还亏 → 直接排除。
点预测做不到这件事，它只会告诉你「预期 +18%」，然后你在尾部亏掉本金。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import Cfg


# ------------------------------------------------------------------ A. 异动检测
def detect_anomaly(row: dict[str, Any], cfg: Cfg) -> tuple[str | None, float, str]:
    """返回 (等级 或 None, 打分, 理由串)"""
    score = 0.0
    why: list[str] = []

    r21 = row.get("ret_21d")
    if r21 is not None and not _nan(r21) and r21 < 0:
        drop = abs(math.expm1(r21)) * 100  # 对数收益 → 百分比跌幅
        score += min(drop / 25.0, 1.0) * 40
        why.append(f"21日 {r21 * 100:.1f}%")

    r7 = row.get("ret_7d")
    if r7 is not None and not _nan(r7) and r7 < 0:
        score += min(abs(r7) / 0.06, 1.0) * 15
        why.append(f"7日 {r7 * 100:.1f}%")

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
        why.append(f"弱于大盘 {rel * 100:.1f}%")

    # ---- 流动性闸门 ----
    listings = row.get("listings")
    volume = row.get("volume")
    if listings is not None and not _nan(listings) and listings < cfg.gate_listings:
        return None, score, "; ".join(why + ["流动性不足(挂单少)"])
    if volume is not None and not _nan(volume) and volume < cfg.gate_volume:
        return None, score, "; ".join(why + ["流动性不足(无成交)"])

    level = None
    for lv, drop_th, z_th, _cd in cfg.levels:
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
    size = min(cfg.unit, cfg.unit * kelly * 0.5)   # 半 Kelly 再打对折

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


def tranche(days_in_drop: float | None, size: float, lo: int = 7, hi: int = 28) -> float:
    """分批建仓节奏：越靠近你经验窗口的上沿（28 天），越接近满仓"""
    if days_in_drop is None or _nan(days_in_drop):
        return size * 0.5
    f = float(np.clip((float(days_in_drop) - lo) / max(hi - lo, 1), 0.0, 1.0))
    return size * f


def in_cooldown(last_ts: int | None, now_ts: int, level: str, cfg: Cfg) -> bool:
    cd_hours = next((cd for lv, _d, _z, cd in cfg.levels if lv == level), 6)
    return last_ts is not None and (now_ts - last_ts) < cd_hours * 3600


def confirm(df_tail: np.ndarray, level: str, cfg: Cfg) -> bool:
    """确认点：要求连续 confirm_n 个采样点都达到该等级，过滤瞬时闪崩"""
    return len(df_tail) >= cfg.confirm_n and bool(np.all(df_tail[-cfg.confirm_n :] >= 1))
