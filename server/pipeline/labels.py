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

    这里必须**两遍**：先把所有样本的覆盖区间都累加进 ind，再用最终的重叠度算唯一性。
    踩过的坑：原先边累加边取 local[j]，排序在后的样本还没计入，
    于是靠前的样本唯一性被高估（第一个天然拿到 1.0）、靠后的被低估。
    实测 [2,3,4] 返回 [1.0, 0.75, 0.75]，正确是 [0.75, 0.5, 0.75]。
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

        spans = [(int(min(max(t1, j + 1), n)), j) for j, t1 in enumerate(ends)]

        # 第一遍：累加全部重叠
        for t1, j in spans:
            ind[j:t1] += 1.0
        # 第二遍：用最终重叠度计算
        for t1, j in spans:
            local[j] = (1.0 / ind[j:t1]).mean() if t1 > j else 0.0

        uniq[mask] = local
    uniq[~np.isfinite(uniq)] = 0.0
    return uniq


def sample_weights(df: pd.DataFrame, cfg: Cfg) -> np.ndarray:
    """时间衰减 × 唯一性 × 品种平衡，最后归一化到均值 1。

    label_end_idx 在这里**重算**，不沿用 make_labels 阶段算好的值。
    踩过的坑：make_labels 是在全量帧上算的，而调用方传进来的通常是
    is_warm / dropna 过滤后的子集，行位置已经变了 —— 旧的 end_idx 与
    局部序号完全错位，唯一性权重等于随机数。
    """
    d = df.reset_index(drop=True)
    n = len(d)
    if n == 0:
        return np.zeros(0, dtype=float)

    items = d["item_id"].to_numpy()
    dates = d["date"].to_numpy()

    # 按 (item_id, date) 排序后重算覆盖区间，最后再映射回原顺序
    order = np.lexsort((dates, items))
    items_sorted = items[order]
    grp = pd.Series(items_sorted)
    idx_in_item = grp.groupby(items_sorted).cumcount().to_numpy()
    item_len = grp.groupby(items_sorted).transform("size").to_numpy()
    end_sorted = np.minimum(idx_in_item + cfg.horizon, item_len - 1)

    w_time = 0.5 ** ((d["date"].max() - d["date"]).dt.days.to_numpy(dtype=float)
                     / max(cfg.half_life_days, 1))
    w_uniq_sorted = average_uniqueness(end_sorted, items_sorted)
    counts_sorted = grp.groupby(items_sorted).transform("size").to_numpy(dtype=float)

    w_sorted = w_time[order] * w_uniq_sorted * (1.0 / np.sqrt(np.maximum(counts_sorted, 1.0)))

    w = np.empty(n, dtype=float)
    w[order] = w_sorted
    w[~np.isfinite(w) | (w <= 0)] = 1e-6
    return w / w.mean()
