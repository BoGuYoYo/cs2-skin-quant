"""CQR（Conformalized Quantile Regression）：让区间覆盖率有有限样本保证。

为什么必须做：
  LightGBM 的分位数回归在样本量偏小时，区间宽度会系统性偏窄。
  你号称 10% 分位，实测可能只有 4% 的样本真的跌破了它 —— 于是你以为的
  「下沿」是假的，仓位管理全部建立在错误的风险认知上。
  CQR 用一个独立校准集把宽度校正回来，且不依赖任何分布假设。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger("calibrate")


@dataclass
class CQR:
    alpha: float = 0.20          # alpha=0.2 → 目标 80% 区间
    q_lo_tau: float = 0.10       # 下沿分位数
    q_hi_tau: float = 0.90       # 上沿分位数
    Q: float | None = None
    n_calib: int = 0

    def fit(self, q_lo: np.ndarray, q_hi: np.ndarray, y: np.ndarray) -> "CQR":
        """一致性得分：真实值落在区间外多远（区间内记 0）；取 (1-alpha) 分位做校正量"""
        q_lo = np.asarray(q_lo, dtype=float)
        q_hi = np.asarray(q_hi, dtype=float)
        y = np.asarray(y, dtype=float)
        if q_lo.size != q_hi.size or q_lo.size != y.size:
            raise ValueError("q_lo / q_hi / y 长度必须一致")
        E = np.maximum(q_lo - y, y - q_hi)
        n = E.size
        self.n_calib = n
        k = int(np.ceil((1.0 - self.alpha) * (n + 1)))
        self.Q = float(np.sort(E)[min(k, n) - 1])
        log.info("CQR 校准完成：n=%d，校正量 Q=%.4f（相当于收紧/放宽 %.2f%%）", n, self.Q, self.Q * 100)
        return self

    def transform(self, q_lo: np.ndarray, q_hi: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if self.Q is None:
            raise RuntimeError("CQR 未校准，先调用 fit()")
        return np.asarray(q_lo) - self.Q, np.asarray(q_hi) + self.Q

    def coverage(self, q_lo: np.ndarray, q_hi: np.ndarray, y: np.ndarray) -> float:
        lo, hi = self.transform(q_lo, q_hi)
        return float(((y >= lo) & (y <= hi)).mean())


def pit_histogram(y: np.ndarray, preds: dict[float, np.ndarray], bins: int = 10) -> np.ndarray:
    """PIT 直方图：把每个 tau 的覆盖率平均起来，应该接近均匀分布。

    U 型 = 区间太窄（模型过度自信）；倒 U 型 = 区间太宽（模型没学到东西）。
    """
    taus = sorted(preds)
    u = np.mean([(y <= preds[t]).astype(float) for t in taus], axis=0)
    hist, _ = np.histogram(u, bins=bins, range=(0.0, 1.0))
    return hist / max(len(u), 1)
