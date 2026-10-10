"""全局配置。所有可调参数集中在这里，方便回测时网格搜索覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _f(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


@dataclass
class Cfg:
    # ---------------- 预测尺度 ----------------
    # 你的经验先验：下跌趋势持续 2~4 周 → h=21 正好覆盖一个完整周期
    horizon: int = field(default_factory=lambda: _i("HORIZON", 21))
    horizons_aux: tuple[int, ...] = (42,)

    # ---------------- 成本（BUFF 口径）----------------
    buy_fee: float = field(default_factory=lambda: _f("BUY_FEE", 0.025))
    sell_fee: float = field(default_factory=lambda: _f("SELL_FEE", 0.025))
    withdraw_fee: float = field(default_factory=lambda: _f("WITHDRAW_FEE", 0.01))

    # ---------------- 域筛选（经验法则）----------------
    price_min: float = 3.0
    price_max: float = 100.0
    turnover_min: float = 0.002      # 日成交/存世 ≥0.2%，排除死水
    list_ratio_max: float = 0.035    # 在售/存世 ≤3.5%，抛压不重
    case_rarity_min: float = 60.0

    # ---------------- 标签权重 ----------------
    half_life_days: int = 365

    # ---------------- 模型 ----------------
    quantiles: tuple[float, ...] = (0.1, 0.5, 0.9)
    n_estimators: int = 400
    learning_rate: float = 0.03
    num_leaves: int = 15
    min_child_samples: int = 40
    reg_lambda: float = 5.0
    subsample: float = 0.8
    colsample: float = 0.7

    # ---------------- 验证 ----------------
    n_splits: int = 6
    embargo: int = 21

    # ---------------- 决策 ----------------
    edge_min: float = 0.10           # 中位净收益门槛
    unit: float = 0.05               # 单一标的最大仓位
    cqr_alpha: float = 0.20          # 目标 80% 区间

    # ---------------- 告警分级 ----------------
    levels: tuple[tuple[str, float, float, int], ...] = (
        # (等级, 24h跌幅阈值%, z-score下限, 冷却小时)
        ("L1", -8.0, 2.0, 12),
        ("L2", -15.0, 3.0, 6),
        ("L3", -25.0, 4.5, 2),
    )
    confirm_n: int = 2
    gate_listings: int = 10
    gate_volume: int = 3

    # ---------------- 路径 ----------------
    state_dir: Path = field(default_factory=lambda: Path(_env("DB_PATH", "./state/skins.db")).parent)
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", "./state/skins.db")))
    model_path: Path = field(default_factory=lambda: Path(_env("MODEL_PATH", "./state/model.pkl")))

    retrain_days: int = field(default_factory=lambda: _i("RETRAIN_DAYS", 3))
    model_version: str = field(default_factory=lambda: _env("MODEL_VERSION", "lgb-q-v1"))

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)


# 特征列：顺序固定，训练与推理必须一致（模型按顺序对齐）
FEATURE_COLS: list[str] = [
    # 价格形态
    "ret_1d", "ret_7d", "ret_14d", "ret_21d", "ret_30d",
    "dd_from_high", "drop_duration_days",
    "vol_21", "vol_z",
    # 供给面（核心：箱子稀有 + 存世饱和）
    "supply_peer_pct", "case_rarity_score", "case_volume_30d",
    "supply_growth_30d", "supply_rigidity",
    # 需求与抛压
    "turnover", "turnover_ma7", "list_ratio", "list_slope_7d",
    # 相对强度
    "rel_ret_7d", "rel_ret_21d",
    # LLM 事件
    "event_bearish_30d", "event_bullish_30d", "event_conf_max",
]

# 单调约束：把经验法则硬编码进模型，小样本下最有效的防过拟合手段
MONOTONE: dict[str, int] = {
    "case_rarity_score": 1,     # 箱子越稀有 → 未来收益越高
    "case_volume_30d": -1,      # 开箱量越大 → 供给洪水 → 收益越低
    "supply_growth_30d": -1,    # 存世增速越快 → 越差
    "supply_rigidity": 1,       # 供给刚性 → 越好
    "supply_peer_pct": -1,      # 存世量同箱分位越高（越不稀有）→ 越差
    "turnover": 1,              # 换手越高 → 有需求 → 越好
    "list_ratio": -1,           # 在售占比越高 → 抛压越重 → 越差
    "list_slope_7d": -1,        # 挂单量增长 → 抛压 → 越差
    "rel_ret_21d": 1,           # 强于大盘 → 越好
}

CFG = Cfg()
