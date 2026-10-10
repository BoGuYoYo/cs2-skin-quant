"""标签与样本权重。

分位数回归真正的难点在这，不在模型：
  h=21 天的前向标签意味着相邻样本共享 20 天未来 —— 重叠标签会让有效样本量
  虚高 21 倍，正则化全部失效，回测漂亮而实盘崩。必须用「唯一性权重」修正。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import Cfg


def make_labels(df: pd.DataFrame, cfg: Cfg) -> pd.DataFrame:
    """在 df 上新增 y（对数净收益）、y_h{aux}、fwd_max_dd、label_end_idx"""
    df = df.sort_values(["item_id", "date"]).copy()
    g = df.groupby("item_id", group_keys=False)

    def net_logret(shift_n: int) -> pd.Series:
        p = df["price"]
        # 卖出被扣 sell_fee + withdraw_fee；买入实际成本抬高 buy_fee
        ratio = (p.groupby(df["item_id"]).shift(-shift_n) * (1 - cfg.sell_fee - cfg.withdraw_fee)) / (
            p * (1 + cfg.buy_fee)
        )
        return np.log(ratio.clip(lower=1e-3, upper=1e3))

    df["y"] = net_logret(cfg.horizon)
    for h in cfg.horizons_aux:
        df[f"y_h{h}"] = net_logret(h)

    fwd_min = g["price"].transform(
        lambda s: s[::-1].rolling(cfg.horizon, min_periods=2).min()[::-1].shift(-1)
    )
    df["fwd_max_dd"] = fwd_min / df["price"] - 1.0

    # 标签结束位置（全局行号），供唯一性权重使用
    idx_in_item = g.cumcount().to_numpy()
    item_len = g["item_id"].transform("size").to_numpy()
    df["label_end_idx"] = np.minimum(idx_in_item + cfg.horizon, item_len - 1)
    return df


def average_uniqueness(end_idx: np.ndarray, item_ids: np.ndarray) -> np.ndarray:
    """López de Prado 的平均唯一性：每个样本按它覆盖区间的平均重叠度降权。

    注意必须按 item 分别计算 —— 跨品种的「重叠」没有意义。
    """
    uniq = np.zeros(len(end_idx), dtype=float)
    for item in pd.unique(item_ids):
        mask = np.flatnonzero(item_ids == item)
        if mask.size == 0:
            continue
        ends = end_idx[mask]
        n = int(ends.max()) + 1
        ind = np.zeros(n, dtype=float)
        local = np.zeros(mask.size, dtype=float)
        for j, t1 in enumerate(ends):
            t1 = int(min(max(t1, j + 1), n))
            ind[j:t1] += 1.0
            local[j] = (1.0 / ind[j:t1]).mean() if t1 > j else 0.0
        uniq[mask] = local
    uniq[~np.isfinite(uniq)] = 0.0
    return uniq


def sample_weights(df: pd.DataFrame, cfg: Cfg) -> np.ndarray:
    """时间衰减 × 唯一性 × 品种平衡，最后归一化到均值 1"""
    age_days = (df["date"].max() - df["date"]).dt.days.to_numpy(dtype=float)
    w_time = 0.5 ** (age_days / max(cfg.half_life_days, 1))

    w_uniq = average_uniqueness(df["label_end_idx"].to_numpy(), df["item_id"].to_numpy())

    counts = df.groupby("item_id")["item_id"].transform("size").to_numpy(dtype=float)
    w_item = 1.0 / np.sqrt(np.maximum(counts, 1.0))

    w = w_time * w_uniq * w_item
    w[~np.isfinite(w) | (w <= 0)] = 1e-6
    return w / w.mean()
