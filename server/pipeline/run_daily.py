"""每日主流程：采集 → 特征 → 标签 → 训练 → 推理 → 校准 → 决策 → 推送 → 通知

用法：
    python -m pipeline.run_daily                 # 完整跑一遍并推送
    python -m pipeline.run_daily --dry-run       # 只算不推
    python -m pipeline.run_daily --backtest      # 只做 walk-forward 评估，打印验收结论
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
import requests

try:  # 允许直接 python run_daily.py 运行
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

from . import cases as cases_mod
from . import collector, decide, features, labels, llm
from .calibrate import CQR
from .config import CFG, FEATURE_COLS, Cfg
from .model import QuantileModel, acceptance, walk_forward

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from push import dispatch, notify  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daily")


# ------------------------------------------------------------------ 工具
def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_watchlist(con) -> list[str]:
    """监控池 —— 定义已统一到 collector.WATCHLIST_SQL，避免两处规则漂移"""
    return collector.load_watchlist(con)


def make_kline(hist: pd.DataFrame) -> pd.DataFrame:
    """日线聚合：一天多个采样点时取 first/max/min/last，单点时 open=前一日 close"""
    if hist.empty:
        return hist
    g = hist.groupby(["item_id", "date"])["price"]
    agg = g.agg(o="first", h="max", l="min", c="last").reset_index()
    agg["v"] = hist.groupby(["item_id", "date"])["volume"].sum().to_numpy()
    agg = agg.sort_values(["item_id", "date"])
    prev_close = agg.groupby("item_id")["c"].shift(1)
    agg["o"] = prev_close.fillna(agg["o"])
    return agg


def load_or_train(cfg: Cfg, labeled: pd.DataFrame) -> tuple[QuantileModel, CQR, dict[str, Any]]:
    """按 retrain_days 决定是否重训；同时产出 CQR 校准器"""
    meta: dict[str, Any] = {}
    mature = labeled.dropna(subset=["y"])
    if "is_warm" in mature.columns:
        before = len(mature)
        mature = mature[mature["is_warm"]]
        log.info("剔除预热期样本 %d 行，剩 %d 行", before - len(mature), len(mature))
    if len(mature) < 300:
        raise RuntimeError(
            f"成熟标签只有 {len(mature)} 行，不足以训练。"
            f"需要至少 300 行 —— 冷启动 21 天后才会自然满足，或先用历史 dump 回填。"
        )

    need_train = True
    if cfg.model_path.exists():
        age_days = (time.time() - cfg.model_path.stat().st_mtime) / 86400
        need_train = age_days >= cfg.retrain_days
        log.info("现有模型 %.2f 天，%s", age_days, "需要重训" if need_train else "复用")

    if not need_train:
        mdl = QuantileModel.load(cfg.model_path)
    else:
        train_part, calib_part = _split_tail(mature, 0.8)
        w = labels.sample_weights(train_part, cfg)
        log.info("训练：%d 行 × %d 特征", len(train_part), len(FEATURE_COLS))
        mdl = model_train(train_part, w, cfg)
        mdl.save(cfg.model_path)

    # ---- CQR 校准：永远用「训练段之后的独立窗口」----
    train_part, calib_part = _split_tail(mature, 0.8)
    if len(calib_part) >= 50:
        P = mdl.predict(calib_part[FEATURE_COLS])
        taus = sorted(cfg.quantiles)
        cqr = CQR(alpha=cfg.cqr_alpha, q_lo_tau=taus[0], q_hi_tau=taus[-1])
        cqr.fit(P[:, 0], P[:, -1], calib_part["y"].to_numpy())
        meta["cqr_Q"] = cqr.Q
        meta["cqr_n"] = cqr.n_calib
    else:
        log.warning("校准样本不足（%d < 50），CQR 退化为恒等变换", len(calib_part))
        cqr = CQR(alpha=cfg.cqr_alpha)
        cqr.Q = 0.0
    return mdl, cqr, meta


def model_train(train_part: pd.DataFrame, w: np.ndarray, cfg: Cfg) -> QuantileModel:
    from .model import train_all

    return train_all(train_part[FEATURE_COLS], train_part["y"], w, cfg)


def _split_tail(df: pd.DataFrame, frac: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = df.sort_values("date").reset_index(drop=True)
    cut = int(len(d) * frac)
    return d.iloc[:cut], d.iloc[cut:]


def _push_cold_start(con, cfg: Cfg, day: str, now_ts: int, hist, reason: str) -> None:
    """冷启动期（标签未成熟）只推送基础数据。

    没有 forecast / factor_latest —— 前端会显示「今日打分标的 0」，
    但 K 线、饰品列表、运行日志都是真实的。总比线上空空如也强，
    也让你能提前验证采集链路是否稳定。
    """
    try:
        kline_df = make_kline(hist)
        kline_rows = [
            {"item_id": r.item_id, "date": r.date.strftime("%Y-%m-%d"),
             "o": float(r.o), "h": float(r.h), "l": float(r.l), "c": float(r.c),
             "v": int(r.v) if pd.notna(r.v) else None}
            for r in kline_df.itertuples()
        ] if not kline_df.empty else []

        item_rows = [
            {"item_id": r[0], "name": r[1], "market_hash_name": r[2], "wear": r[3],
             "case_id": r[4], "case_name": r[5], "image": r[6], "updated_at": now_ts}
            for r in con.execute(
                "SELECT item_id, name, market_hash_name, wear, case_id, case_name, image FROM items "
                "WHERE market_hash_name IN (SELECT DISTINCT item_id FROM snap)"
            ).fetchall()
        ]

        plan = [
            ("items", item_rows, {}),
            ("kline", kline_rows, {}),
            ("meta", [
                {"k": "model_version", "v": f"{cfg.model_version}-coldstart", "updated_at": now_ts},
                {"k": "last_run", "v": day, "updated_at": now_ts},
                {"k": "status", "v": "cold_start", "updated_at": now_ts},
                {"k": "cold_start_reason", "v": reason[:200], "updated_at": now_ts},
                {"k": "snap_rows", "v": str(len(hist)), "updated_at": now_ts},
            ], {}),
            ("runs", [
                {"ts": now_ts, "stage": "daily-coldstart", "ok": 1, "rows": len(kline_rows),
                 "message": f"冷启动采集 {len(hist)} 行快照；预测已跳过"},
            ], {}),
        ]
        for stage, rows, kw in plan:
            dispatch.push(stage, rows, **kw)
        log.info("冷启动基础数据已推送：items %d / kline %d", len(item_rows), len(kline_rows))
    except Exception as e:
        log.error("冷启动推送失败：%s", e)


# ------------------------------------------------------------------ 主流程
def run(dry_run: bool = False, backtest_only: bool = False) -> int:
    cfg = CFG
    cfg.ensure_dirs()
    key = os.environ.get("STEAMDT_KEY", "")
    if not key:
        log.error("STEAMDT_KEY 未配置，退出")
        return 2

    con = collector.connect(cfg.db_path)
    session = requests.Session()
    day = today_str()
    now_ts = int(time.time())      # 提前定义：训练失败走冷启动分支时也要用到
    fails: list[str] = []

    # ---------------- 1. 采集 ----------------
    watchlist = load_watchlist(con)
    if not watchlist:
        log.warning("items 表为空，先跑一次 base 同步（每天限 1 次）")
        try:
            base = collector.fetch_base(key)
            n = collector.write_items(con, base, int(time.time()))
            log.info("已写入 %d 个饰品基础信息", n)
            watchlist = load_watchlist(con)
        except Exception as e:
            log.error("base 同步失败：%s", e)
            return 3

    log.info("监控池 %d 个武器皮肤（已排除印花/涂鸦/挂件等）", len(watchlist))

    # 采集规模：SteamDT 的 /price/batch 限流 1 次/分钟 × 100 个品种。
    #   2000 个   → 20 次请求  → 约 20 分钟
    #   18826 个  → 189 次请求 → 约 3.2 小时
    #   44497 全量 → 445 次请求 → 约 7.5 小时
    # WATCH_LIMIT=0 表示不限量（全池）。
    limit = int(os.environ.get("WATCH_LIMIT", "2000"))
    pool = watchlist if limit <= 0 else watchlist[:limit]
    log.info("本轮采集 %d 个（WATCH_LIMIT=%s，预计 %.0f 分钟）",
             len(pool), "不限" if limit <= 0 else limit, len(pool) / 100 * 61 / 60)
    try:
        blocks = collector.fetch_prices(pool, key, session)
        n = collector.write_daily_snapshot(con, blocks, day)
        log.info("写入当日快照 %d 行", n)
    except Exception as e:
        fails.append(f"采集失败: {e}")
        log.error("采集失败：%s", e)

    # ---------------- 2. 特征 ----------------
    hist = collector.load_history(con)
    if hist.empty:
        log.error("本地无历史数据，无法继续")
        return 4

    case_df = pd.DataFrame(cases_mod.load_cases(con))
    index_df = _load_index(con)
    events_df = _load_events(con)

    feats = features.build(hist, case_df, index_df, events_df)
    labeled = labels.make_labels(feats, cfg)
    log.info("特征宽表 %d 行 × %d 列", len(labeled), labeled.shape[1])

    # ---------------- 3. 回测模式 ----------------
    if backtest_only:
        metrics, base = walk_forward(labeled, cfg)
        if not metrics.empty:
            log.info("逐折指标：\n%s", metrics.to_string(index=False))
        ok, notes = acceptance(metrics, base)
        for line in notes:
            log.info("  %s", line)
        log.info("验收结论：%s", "通过" if ok else "未通过")
        return 0 if ok else 1

    # ---------------- 4. 模型 + 校准 ----------------
    try:
        mdl, cqr, calib_meta = load_or_train(cfg, labeled)
    except RuntimeError as e:
        log.error("%s", e)
        fails.append(str(e))
        if not dry_run:
            log.warning("冷启动期：跳过预测，但仍推送已采集的基础数据（items / kline）")
            _push_cold_start(con, cfg, day, now_ts, hist, str(e))
        return 5

    # ---------------- 5. 推理 ----------------
    latest = features.latest_cross_section(labeled)
    P = mdl.predict(latest[FEATURE_COLS])

    # 经济先验模型（带单调约束，见 model.train_prior）：
    # 它不能给分位数加约束（LightGBM 不允许），但可以用来做一致性校验 ——
    # 分位数说能买、先验说很差，就降级观察。这是把经验法则真正接进决策的地方。
    prior = mdl.predict_prior(latest[FEATURE_COLS])
    prior_rank = pd.Series(prior).rank(pct=True).to_numpy() if prior is not None else None

    q_lo_adj, q_hi_adj = cqr.transform(P[:, 0], P[:, -1])

    forecast_rows: list[dict[str, Any]] = []
    factor_rows: list[dict[str, Any]] = []
    alert_rows: list[dict[str, Any]] = []
    now_ts = int(time.time())

    # 概率校准：用 P50 是否为正 + 分位宽度做单调映射（真实项目里应训一个分类器）
    width = np.maximum(q_hi_adj - q_lo_adj, 1e-6)
    prob_up = np.clip((P[:, 1] + 0.05) / 0.25, 0.0, 1.0)

    candidates: list[dict[str, Any]] = []
    for i, (_, r) in enumerate(latest.iterrows()):
        row = r.to_dict()
        row["q10_adj"] = float(q_lo_adj[i])
        row["q90_adj"] = float(q_hi_adj[i])
        row["q10"] = float(P[i, 0])
        row["q50"] = float(P[i, 1])
        row["q90"] = float(P[i, 2])
        row["prob_up"] = float(prob_up[i])

        d = decide.decide(row, cfg)
        sc = decide.score(row, cfg)

        # 先验一致性校验：带单调约束的模型排在末 35% 就不给买入资格
        if prior_rank is not None and d["side"] == "buy" and prior_rank[i] < 0.35:
            d = {
                "side": "watch",
                "size": 0.0,
                "why": d["why"] + f"；先验模型不认同（同批分位 {prior_rank[i] * 100:.0f}%）",
            }

        forecast_rows.append({
            "item_id": row["item_id"],
            "date": day,
            "horizon": cfg.horizon,
            "q10": row["q10"], "q50": row["q50"], "q90": row["q90"],
            "q10_adj": row["q10_adj"], "q90_adj": row["q90_adj"],
            "prob_up": row["prob_up"],
            "fwd_max_dd": None,
            "score": sc,
            "side": d["side"], "size": d["size"], "why": d["why"],
            "model_version": cfg.model_version,
        })

        factor_rows.append({
            "item_id": row["item_id"], "date": day,
            "price": _f(row.get("price")),
            "dd_from_high": _f(row.get("dd_from_high")),
            "drop_duration_days": _i(row.get("drop_duration_days")),
            "ret_7d": _f(row.get("ret_7d")), "ret_21d": _f(row.get("ret_21d")),
            "ret_30d": _f(row.get("ret_30d")), "vol_21": _f(row.get("vol_21")),
            "turnover": _f(row.get("turnover")), "list_ratio": _f(row.get("list_ratio")),
            "list_slope_7d": _f(row.get("list_slope_7d")),
            "supply_peer_pct": _f(row.get("supply_peer_pct")),
            "supply_rigidity": _f(row.get("supply_rigidity")),
            "case_rarity_score": _f(row.get("case_rarity_score")),
            "rel_ret_21d": _f(row.get("rel_ret_21d")),
            "event_bearish_30d": _i(row.get("event_bearish_30d")),
            "event_bullish_30d": _i(row.get("event_bullish_30d")),
            "updated_at": now_ts,
        })

        # ---- 异动检测 ----
        level, ascor, why = decide.detect_anomaly(row, cfg)
        if level:
            alert_rows.append({
                "item_id": row["item_id"],
                "name": _name_of(con, row["item_id"]),
                "level": level, "score": round(ascor, 1), "ts": now_ts,
                "detail": why, "pushed": 0,
            })

        if d["side"] == "buy":
            candidates.append({
                "item_id": row["item_id"],
                "name": _name_of(con, row["item_id"]),
                "price": _f(row.get("price")) or 0.0,
                "p10": row["q10_adj"], "p50": row["q50"],
                "size": d["size"], "score": sc,
            })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info("预测完成：%d 条；候选 %d 个；异动 %d 条", len(forecast_rows), len(candidates), len(alert_rows))

    if dry_run:
        log.info("--dry-run：跳过推送")
        _print_top(candidates)
        return 0

    # ---------------- 6. 推送 ----------------
    kline_df = make_kline(hist)
    kline_rows = [
        {"item_id": r.item_id, "date": r.date.strftime("%Y-%m-%d"),
         "o": float(r.o), "h": float(r.h), "l": float(r.l), "c": float(r.c),
         "v": int(r.v) if pd.notna(r.v) else None}
        for r in kline_df.itertuples()
    ] if not kline_df.empty else []

    item_rows = [
        {"item_id": r[0], "name": r[1], "market_hash_name": r[2], "wear": r[3],
         "case_id": r[4], "case_name": r[5], "image": r[6], "updated_at": now_ts}
        for r in con.execute(
            "SELECT item_id, name, market_hash_name, wear, case_id, case_name, image FROM items"
        ).fetchall()
    ]

    case_rows = [
        {"case_id": c["case_id"], "case_name": c.get("case_name"),
         "rarity_score": c.get("rarity_score"), "case_price": c.get("case_price"),
         "case_volume_30d": c.get("case_volume_30d"), "supply_growth_30d": c.get("supply_growth_30d"),
         "drop_pool": c.get("drop_pool"), "rationale": c.get("rationale"),
         "blocking_risk": c.get("blocking_risk"), "updated_at": now_ts}
        for c in cases_mod.load_cases(con)
    ]

    log.info("推送通道：%s", dispatch.describe())
    try:
        for stage, rows, kw in [
            ("items", item_rows, {}),
            ("case_meta", case_rows, {}),
            ("factor_latest", factor_rows, {}),
            ("forecast", forecast_rows, {"replace_date": day}),
            ("alerts", alert_rows, {}),
            ("kline", kline_rows, {}),
            (
                "meta",
                [
                    {"k": "model_version", "v": cfg.model_version, "updated_at": now_ts},
                    {"k": "last_run", "v": day, "updated_at": now_ts},
                    {"k": "horizon", "v": str(cfg.horizon), "updated_at": now_ts},
                    {"k": "cqr_Q", "v": str(calib_meta.get("cqr_Q")), "updated_at": now_ts},
                ],
                {},
            ),
            (
                "runs",
                [
                    {
                        "ts": now_ts, "stage": "daily", "ok": 1, "rows": len(forecast_rows),
                        "message": f"候选 {len(candidates)}，异动 {len(alert_rows)}",
                    }
                ],
                {},
            ),
        ]:
            dispatch.push(stage, rows, **kw)
    except Exception as e:
        fails.append(f"推送失败: {e}")
        log.error("推送失败：%s", e)

    # ---------------- 7. 通知 ----------------
    if candidates:
        notify.notify_digest(day, candidates, fails or None)
    for a in alert_rows[:5]:
        if a["level"] == "L3":
            notify.notify_item(a["name"] or a["item_id"], a["level"], 0.0, a["detail"] or "")

    _print_top(candidates)
    return 0 if not fails else 6


# ------------------------------------------------------------------ 小工具
def _f(v: Any) -> float | None:
    try:
        x = float(v)
        return None if np.isnan(x) else round(x, 6)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    try:
        x = float(v)
        return None if np.isnan(x) else int(x)
    except (TypeError, ValueError):
        return None


def _name_of(con, item_id: str) -> str | None:
    r = con.execute("SELECT name FROM items WHERE item_id = ?", (item_id,)).fetchone()
    return r[0] if r else None


def _load_index(con) -> pd.DataFrame | None:
    try:
        df = pd.read_sql_query("SELECT date, index_value FROM market_index", con)
        return df if not df.empty else None
    except Exception:
        return None


def _load_events(con) -> pd.DataFrame | None:
    try:
        df = pd.read_sql_query("SELECT date, direction, confidence FROM events", con)
        return df if not df.empty else None
    except Exception:
        return None


def _print_top(candidates: list[dict[str, Any]]) -> None:
    if not candidates:
        log.info("今日无候选")
        return
    log.info("Top 候选：")
    for c in candidates[:10]:
        log.info(
            "  %-42s ¥%7.2f  P10 %+6.1f%%  P50 %+6.1f%%  仓位 %.2f%%  打分 %.1f",
            (c["name"] or c["item_id"])[:42], c["price"],
            np.expm1(c["p10"]) * 100, np.expm1(c["p50"]) * 100, c["size"] * 100, c["score"],
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="skin-alpha 每日管线")
    ap.add_argument("--dry-run", action="store_true", help="只计算不推送")
    ap.add_argument("--backtest", action="store_true", help="只做 walk-forward 评估并打印验收结论")
    ap.add_argument("--check-push", action="store_true", help="只测推送通道连通性（不采集不训练）")
    args = ap.parse_args()

    if args.check_push:
        result = dispatch.check()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        ok = all(
            v.get("ok", False)
            for k, v in result.items()
            if k in ("worker", "d1") and isinstance(v, dict)
        )
        return 0 if ok else 1

    return run(dry_run=args.dry_run, backtest_only=args.backtest)


if __name__ == "__main__":
    raise SystemExit(main())
