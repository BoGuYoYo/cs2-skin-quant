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
    # 0 = 用满所有核。2 核小机器上建议显式设成 2，避免 OpenMP 线程争抢反而更慢。
    num_threads: int = field(default_factory=lambda: _i("NUM_THREADS", 0))

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
    #: 流动性闸门 —— 用户 2026-10-11 的规则：「日交易量低的不看（小于 20 一天）」。
    #:
    #: 注意这里比的是**在售挂单量 sellCount**，不是成交量：
    #: 实测 SteamDT 没有成交量接口（kline 三个周期 × 两个平台都只有 5 个价格字段），
    #: 只能用挂单量当流动性代理（见 collector.collect_listings 与 pipeline.listings）。
    #: 数据缺失时闸门自动失效，不把「不知道」当「不合格」—— 见 run_daily._apply_liquidity_gate。
    gate_listings: int = field(default_factory=lambda: _i("GATE_MIN_LISTINGS", 20))
    gate_volume: int = 3

    # ---------------- 路径 ----------------
    state_dir: Path = field(default_factory=lambda: Path(_env("DB_PATH", "./state/skins.db")).parent)
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", "./state/skins.db")))
    model_path: Path = field(default_factory=lambda: Path(_env("MODEL_PATH", "./state/model.pkl")))

    retrain_days: int = field(default_factory=lambda: _i("RETRAIN_DAYS", 3))
    model_version: str = field(default_factory=lambda: _env("MODEL_VERSION", "lgb-q-v1"))

    # ---------------- 内存（4G 小机器上的硬约束）----------------
    # 踩过的坑：回填把 snap 从 216 万行涨到 542 万行后，整条管线一次吃掉
    # 3.1GB RSS + 2.1GB swap，被内核 OOM 直接杀掉 —— 日志里连 traceback 都没有，
    # 看上去就像「跑到一半自己没了」。所以特征必须分块算，训练集必须抽样。
    infer_days: int = field(default_factory=lambda: _i("INFER_DAYS", 120))
    train_item_limit: int = field(default_factory=lambda: _i("TRAIN_ITEM_LIMIT", 4000))
    feat_batch_items: int = field(default_factory=lambda: _i("FEAT_BATCH_ITEMS", 1500))

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 特征列
#
# 这里必须分成两份，而且是**按数据可得性**分，不是按重要性分。
#
# 为什么要这么较真：MONOTONE 会给列加单调约束，而常数列的约束是「假的」——
# 它看起来在表达「换手率越高越好」，实际上那一列恒为 0，模型从没学到任何东西，
# 只是我们误以为约束生效了。实测 23 列里有 10 列恒为常数。
#
# 纪律：没有真实原料的列，就**不要**放进 FEATURE_COLS。
# 宁可少几列，不可假装有。
FEATURE_COLS: list[str] = [
    # 价格形态（9 列：全部有真实原料）
    "ret_1d", "ret_7d", "ret_14d", "ret_21d", "ret_30d",
    "dd_from_high", "drop_duration_days",
    "vol_21", "vol_z",
    # 供给面：只有箱子稀有度这一条链路是活的（case_meta 驱动）
    "case_rarity_score", "supply_rigidity",
    # 相对强度：market_index 为空时 rel_ret_* 会退化成 ret_* 的副本（见下）
    "rel_ret_7d", "rel_ret_21d",
]

#: 已声明、但**当前没有真实原料**的特征。
#: 保留在这里是为了留下「缺什么、为什么不能拿近似值冒充」的记录，
#: 免得下次有人看到 23 列变 13 列又顺手加回来。
FEATURE_COLS_PENDING: dict[str, str] = {
    "supply_peer_pct": "需 snap.supply（存世量同箱分位）—— Steam 不公开存世量，SteamDT 无接口",
    "case_volume_30d": "需箱子成交量 —— 服务器访问不了 steamcommunity.com",
    "supply_growth_30d": "需存世量增速 —— 相对量也拿不到；绝不能拿箱价涨幅冒充（踩过一次）",
    "turnover": "需 volume ÷ supply —— 两个字段全表 NULL",
    "turnover_ma7": "同上",
    "list_ratio": "需 listings ÷ supply —— supply 永远拿不到",
    "list_slope_7d": "需 listings 历史 —— 只能靠 /price/batch 前向积累，现在全表 NULL",
    "event_bearish_30d": "需 events 表有数据 —— llm.extract_supply_events 写好了，但没有落库环节",
    "event_bullish_30d": "同上",
    "event_conf_max": "同上",
}

# 单调约束：把经验法则硬编码进模型，小样本下最有效的防过拟合手段。
# 只保留原料真实存在的列 —— 对常数列加约束等于给自己假的安全感。
MONOTONE: dict[str, int] = {
    "case_rarity_score": 1,     # 箱子越稀有 → 新增供给越少 → 未来收益越高
    "supply_rigidity": 1,       # 供给刚性 → 越好
}
# 已被移除的约束与原因（留痕，别顺手加回来）：
#   case_volume_30d / supply_growth_30d / supply_peer_pct / turnover /
#   list_ratio / list_slope_7d —— 对应列当前恒为常数，约束无意义。
#   rel_ret_21d: +1 —— market_index 表是空的，rel_ret_21d 实际就是 ret_21d，
#     这个约束会变成「21 日动量恒为正向贡献」，与「跌 2~4 周后买入」的先验直接冲突。
#     等大盘指数真的接上数据源再加回来。

CFG = Cfg()

# ---------------------------------------------------------------- 域筛选（用户规则）
#: 用户 2026-10-11 追加的规则：
#:   1) 纪念品不看
#:   2) StatTrak 不看
#:   3) 内格夫、M249、贝瑞塔、CZ、SG553「等枪械」不看
DOMAIN_EXCLUDE_WEAPONS: tuple[str, ...] = (
    "Negev",          # 内格夫（机枪，盘口最薄）
    "M249",           # M249（机枪）
    "Dual Berettas",  # 双持贝瑞塔
    "CZ75-Auto",      # CZ75 自动型（简称 CZ）
    "SG 553",         # SG553
)
# 想再砍几把枪就往上面这个元组里加名字即可 —— 采集池 / 推理截面 /
# 前端列表接口三处都会自动跟着变（见文件末尾 DOMAIN_EXCLUDE_SQL 的拼装）。
#
# 命名变体（实测，别凭印象）：
#   普通       "Negev | Power Loader (Factory New)"   ← 前缀 ''
#   StatTrak™  "StatTrak™ Negev | ..."                ← 前缀 'StatTrak™ '
#   纪念品      "Souvenir Negev | ..."                 ← 前缀 'Souvenir '
#   纯正        "Genuine P250 | ..."                   ← 前缀 'Genuine '（库里只有 1 条，是 P250）
# 所以只匹配裸名是不够的，两种形态都得覆盖。


def _weapon_exclude_sql(weapons: tuple[str, ...]) -> str:
    """把枪名列表拼成排除条件。

    ⚠️ 必须带 " | " 一起匹配，绝不能用裸子串：
        'M4' 会顺手打掉 M4A4 / M4A1-S，'SSG'、'P90' 之类也有同样的坑。
        带上 " | "（武器名与皮肤名之间的分隔符）就只剩精确语义了。
    """
    parts: list[str] = []
    for w in weapons:
        parts.append(f" AND market_hash_name NOT LIKE '{w} | %'")       # 裸名
        parts.append(f" AND market_hash_name NOT LIKE '% {w} | %'")     # 带前缀（StatTrak™/Souvenir/Genuine）
    return "".join(parts)


DOMAIN_EXCLUDE_SQL = (
    # 纪念品 / StatTrak：整类排除
    " AND market_hash_name NOT LIKE '%Souvenir%'"
    " AND market_hash_name NOT LIKE '%StatTrak%'"
) + _weapon_exclude_sql(DOMAIN_EXCLUDE_WEAPONS)
