"""特征工程：把你的经验法则翻译成可计算因子。

三个必须记住的纪律：
  1) 所有特征只能用 t 时刻及之前的数据（滞后特征不要 shift 反了）
  2) 「下跌持续天数」用的是 30 日最高价，不是全历史最高价
  3) 相对强度（相对大盘）必须做，否则大盘下行期会刷出一堆假机会
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .config import FEATURE_COLS

log = logging.getLogger("features")


def drop_duration(price: pd.Series, win: int = 30) -> pd.Series:
    """当前价低于过去 win 日最高价的连续天数 —— 你的「2~4 周」先验的直接度量"""
    high = price.rolling(win, min_periods=5).max()
    under = (price < high).astype(int)
    return under.groupby((under == 0).cumsum()).cumsum()


def densify_calendar(
    df: pd.DataFrame,
    item_col: str = "item_id",
    date_col: str = "date",
    max_fill: int = 1,
) -> pd.DataFrame:
    """把每个品种补齐到连续日历，让后续 shift(n) / diff(n) 真正等于 n 个日历天。

    为什么必须做：
        原先 ret_21d = logp.diff(21)、y = shift(-21) 都是**行位移**。
        只要中间漏采一天，「21 日收益」实际跨了 22 个日历天；
        回测里按日期做的训练/测试隔离也随之不可靠。
        实测数据里确实存在缺口（例如 10-09 → 10-11 缺 10-10）。

    为什么只填 max_fill 天：
        连续缺失超过 max_fill 说明该品种长期没采到，ffill 出一长串假价格
        比留缺口更有害。长缺口保持 NaN，交给下游 dropna / is_warm 过滤。
    """
    if df.empty:
        return df
    parts: list[pd.DataFrame] = []
    for item, g in df.groupby(item_col, sort=False):
        g = g.sort_values(date_col)
        if len(g) < 2:
            parts.append(g)
            continue
        full_idx = pd.date_range(g[date_col].iloc[0], g[date_col].iloc[-1], freq="D")
        if len(full_idx) == len(g):
            parts.append(g)          # 无缺口，快路径直接返回
            continue
        gg = g.set_index(date_col).reindex(full_idx).ffill(limit=max_fill)
        gg[item_col] = item
        gg.index.name = date_col
        parts.append(gg.reset_index())
    out = pd.concat(parts, ignore_index=True)
    return out.sort_values([item_col, date_col]).reset_index(drop=True)


def build(
    hist: pd.DataFrame,
    case_df: pd.DataFrame | None = None,
    index_df: pd.DataFrame | None = None,
    events_df: pd.DataFrame | None = None,
    case_hist: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    hist:     item_id, date, price, bid, listings, volume, supply
    case_df:  case_id, case_name, rarity_score, case_volume_30d, supply_growth_30d, drop_pool
              （**当前**的箱子快照）
    case_hist: case_id, date, rarity_score（**逐日**复算的稀有度，见
              cases_build.build_history）—— 回测防前视泄漏靠它，缺省时退回 case_df
    index_df: date, index_value
    events_df: date, direction, affected_cases, confidence（LLM 事件表）
    返回：原始列 + FEATURE_COLS
    """
    df = hist.sort_values(["item_id", "date"]).copy()
    if df.empty:
        return df

    # 先稠密化日历：补掉单日缺口，让后续所有 diff / rolling / shift
    # 真正等于「N 个日历天」，而不是「N 条记录」
    df = densify_calendar(df)
    g = df.groupby("item_id", group_keys=False)

    # ---------------------------------------------------------- 价格形态
    df["logp"] = np.log(df["price"].clip(lower=0.5))
    df["ret_1d"] = g["logp"].diff()
    for w in (7, 14, 21, 30):
        df[f"ret_{w}d"] = g["logp"].diff(w)

    roll_high = g["price"].transform(lambda s: s.rolling(30, min_periods=5).max())
    df["dd_from_high"] = df["price"] / roll_high - 1.0
    df["drop_duration_days"] = g["price"].transform(drop_duration)

    df["vol_21"] = g["ret_1d"].transform(lambda s: s.rolling(21, min_periods=10).std())
    mu60 = g["ret_1d"].transform(lambda s: s.rolling(60, min_periods=20).mean())
    sd60 = g["ret_1d"].transform(lambda s: s.rolling(60, min_periods=20).std())
    df["vol_z"] = (df["ret_1d"] - mu60) / sd60.replace(0, np.nan)

    # ------------------------------------------------ 供给面（你的核心洞察）
    df["turnover"] = df["volume"] / df["supply"].replace(0, np.nan)
    df["turnover_ma7"] = g["turnover"].transform(lambda s: s.rolling(7, min_periods=3).mean())
    df["list_ratio"] = df["listings"] / df["supply"].replace(0, np.nan)
    df["list_slope_7d"] = g["listings"].transform(lambda s: s.pct_change(7)) * 100

    # 存世量在同箱同品质内的分位（你说的「低于平均值」的精确形式）
    if "case_id" in df.columns:
        df["supply_peer_pct"] = df.groupby(["case_id", "date"])["supply"].rank(pct=True)
    else:
        df["supply_peer_pct"] = df.groupby("date")["supply"].rank(pct=True)

    # 合并箱子层数据
    if case_df is not None and not case_df.empty and "case_id" in df.columns:
        cols = [c for c in ("case_id", "rarity_score", "case_volume_30d", "supply_growth_30d",
                            "drop_pool", "rationale", "blocking_risk") if c in case_df.columns]
        df = df.merge(case_df[cols].drop_duplicates("case_id"), on="case_id", how="left")
    for col, default in (("rarity_score", 50.0), ("case_volume_30d", np.nan),
                         ("supply_growth_30d", np.nan), ("drop_pool", "unknown")):
        if col not in df.columns:
            df[col] = default
    df["case_rarity_score"] = df["rarity_score"].fillna(50.0)

    # --------------------------------------------- 逐日稀有度（防前视泄漏）
    # case_meta 里的 rarity_score 是用**今天**的箱价/趋势/中位价算的；
    # 把它 merge 到全部历史日期上，等于让历史样本提前知道"这个箱子后来变稀有了"
    # —— 而箱价本身编码了这一年的涨跌。walk-forward 的 purge/embargo 拦不住
    # "静态特征里带未来"，所以按日复算的历史值必须优先（审查发现）。
    # 历史表没覆盖到的日期（新箱上线前、或还没构建历史）退回静态值。
    if case_hist is not None and not case_hist.empty and "case_id" in df.columns:
        ch = case_hist[["case_id", "date", "rarity_score"]].copy()
        ch["date"] = pd.to_datetime(ch["date"])
        ch = ch.rename(columns={"rarity_score": "rarity_score_byday"})
        df = df.merge(ch, on=["case_id", "date"], how="left")
        n_hit = int(df["rarity_score_byday"].notna().sum())
        df["case_rarity_score"] = df["rarity_score_byday"].fillna(df["case_rarity_score"])
        df = df.drop(columns=["rarity_score_byday"])
        log.debug("逐日稀有度命中 %d 行", n_hit)

    # 供给刚性合成：箱子稀有（高）+ 存世量增速低 → 供给接近饱和
    #
    # 理想公式需要「存世量增速」，但 SteamDT 所有接口都不提供该字段，
    # cases_build 现在诚实地置 None（早先塞的是箱子价格涨幅，语义错误）。
    # 所以这里做条件退化：有真实增速时用完整公式，没有时只保留稀有度分量。
    # 退化成 rarity/100 后与 case_rarity_score 共线，但语义正确、值域仍是 0-1，
    # 模型自己会处理共线性 —— 比用一个假数据算出的"刚性"要诚实得多。
    if df["supply_growth_30d"].notna().any():
        growth = df["supply_growth_30d"].fillna(3.0).clip(lower=0)
        df["supply_rigidity"] = (df["case_rarity_score"] / 100.0) * (1.0 - np.tanh(growth / 5.0))
    else:
        df["supply_rigidity"] = df["case_rarity_score"] / 100.0

    # ---------------------------------------------------------- 相对强度
    if index_df is not None and not index_df.empty:
        idx = index_df.copy()
        idx["date"] = pd.to_datetime(idx["date"])
        idx = idx.sort_values("date").set_index("date")["index_value"]
        # 指数收益按日对齐后再映射，避免日期错位
        idx_logret = np.log(idx).diff()
        df = df.merge(
            pd.DataFrame({
                "date": idx.index,
                "mkt_7": idx_logret.rolling(7, min_periods=3).sum().values,
                "mkt_21": idx_logret.rolling(21, min_periods=10).sum().values,
            }),
            on="date",
            how="left",
        )
        df["rel_ret_7d"] = df["ret_7d"] - df["mkt_7"].fillna(0)
        df["rel_ret_21d"] = df["ret_21d"] - df["mkt_21"].fillna(0)
    else:
        # 没有大盘序列时，「相对大盘」是未知，不是「相对自己」。
        # 用自身收益顶替会让前端显示同一个数，还会让 decide 把跌幅重复计两次。
        df["rel_ret_7d"] = np.nan
        df["rel_ret_21d"] = np.nan

    # ------------------------------------------------------- LLM 事件特征
    # events 表没有生产者时，事件数是未知而不是零。只有真正读到事件源后，
    # 才把滚动窗口内没有命中的日期填成 0。
    df["event_bearish_30d"] = np.nan
    df["event_bullish_30d"] = np.nan
    df["event_conf_max"] = np.nan
    if events_df is not None and not events_df.empty:
        df["event_bearish_30d"] = 0
        df["event_bullish_30d"] = 0
        df["event_conf_max"] = 0.0
        ev = events_df.copy()
        ev["date"] = pd.to_datetime(ev["date"])
        ev = ev[ev["direction"].isin(("bullish", "bearish"))]
        if not ev.empty:
            agg = (
                ev.assign(bear=(ev["direction"] == "bearish").astype(int),
                          bull=(ev["direction"] == "bullish").astype(int))
                .groupby(pd.Grouper(key="date", freq="D"))
                .agg(event_bearish_30d=("bear", "sum"),
                     event_bullish_30d=("bull", "sum"),
                     event_conf_max=("confidence", "max"))
                .reset_index()
            )

            # 先把事件表铺满「事件起始日 → 行情最后一天」的连续日历，再做 30 日滚动。
            # 踩过的坑：groupby 后 agg 只含**有事件的日期**，滚动也只是在这些日子上滑，
            # 于是最后一次事件之后的所有日期都拿不到计数
            # —— 实测 1/02 计 1，1/03 立刻变 0，而不是保留 30 天。
            span_end = max(agg["date"].max(), df["date"].max())
            full_range = pd.date_range(agg["date"].min(), span_end, freq="D")
            agg = (
                agg.set_index("date")
                .reindex(full_range)
                .fillna({"event_bearish_30d": 0.0, "event_bullish_30d": 0.0, "event_conf_max": 0.0})
                .reset_index()
                .rename(columns={"index": "date"})
            )
            agg.columns = ["date", "event_bearish_30d", "event_bullish_30d", "event_conf_max"]

            agg["event_bearish_30d"] = agg["event_bearish_30d"].rolling(30, min_periods=1).sum()
            agg["event_bullish_30d"] = agg["event_bullish_30d"].rolling(30, min_periods=1).sum()
            agg["event_conf_max"] = agg["event_conf_max"].rolling(30, min_periods=1).max()
            df = df.merge(agg, on="date", how="left", suffixes=("", "_ev"))
            for c in ("event_bearish_30d", "event_bullish_30d", "event_conf_max"):
                if f"{c}_ev" in df.columns:
                    df[c] = df[f"{c}_ev"].fillna(df[c])
                    df.drop(columns=[f"{c}_ev"], inplace=True)
                df[c] = df[c].fillna(0)

    for c in FEATURE_COLS:
        if c not in df.columns:
            df[c] = 0.0

    # 窗口预热标记：长窗口特征齐全才算「可用样本」。
    # 前 30 天的样本必须排除，否则等于拿噪声训练模型。
    df["is_warm"] = (
        df["ret_30d"].notna() & df["dd_from_high"].notna() & df["vol_21"].notna()
    )

    # 按品种前向填充，残余 NaN 归零。相对大盘例外：
    # 没有指数时它必须保持 NaN，既让 LightGBM 按缺失值处理，
    # 也让前端显示「—」而不是伪造一个「与大盘持平」的 0。
    # 注意 1：fillna 发生在 is_warm 计算之后，所以预热期样本仍会被标记为不可用。
    # 注意 2：只填 FEATURE_COLS。FEATURE_COLS_PENDING 里的列**故意留 NaN**——
    #   它们没有真实原料（存世量/成交量/挂单量/事件），填 0 会让前端把「没数据」
    #   显示成「换手率 0.00%」，那正是我们要消灭的那种"看着像模像样"的假数据。
    #   NaN 一路推到前端会渲染成 "—"，这才是诚实的。
    for c in FEATURE_COLS:
        if c in {"rel_ret_7d", "rel_ret_21d"}:
            continue
        df[c] = df.groupby("item_id")[c].ffill().fillna(0.0)

    # float64 → float32：542 万行 × 38 列，光 dtype 就从 1.6GB 降到 0.8GB。
    # 4G 内存的机器上这一步是能不能跑完的分界线（曾经被 OOM 杀掉）。
    for c in df.columns:
        if df[c].dtype == "float64":
            df[c] = df[c].astype("float32")
    return df


def latest_cross_section(df: pd.DataFrame) -> pd.DataFrame:
    """取每个品种的**最后一行**（整行取，不是逐列取最后一个非空值）。

    踩过的坑：原先写的是 df.sort_values("date").groupby("item_id").last() ——
    DataFrameGroupBy.last() 是**逐列**各取最后一个非空值，于是会把
    「前几天有、今天没有」的挂单量拼到今天的新价格上，连 date 列都可能
    来自另一行。表面上是「最新截面」，实际是多个日期的字段缝合怪。
    新加上来的流动性闸门正好读 listings，会被这个截面直接坑到
    （拿前几天的挂单量去判断今天的流动性）。

    整行取之后：今天没更新到的品种，那一行就是 NaN —— 闸门按「不知道就放行」
    处理（见 run_daily._apply_liquidity_gate），这才是诚实的。
    """
    if df.empty:
        return df
    last = df.sort_values("date").groupby("item_id", as_index=False).tail(1)
    return last.reset_index(drop=True)
