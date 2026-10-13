"""每日主流程：采集 → 特征 → 标签 → 训练 → 推理 → 校准 → 决策 → 推送 → 通知

用法：
    python -m pipeline.run_daily                 # 完整跑一遍并推送
    python -m pipeline.run_daily --dry-run       # 只算不推
    python -m pipeline.run_daily --backtest      # 只做 walk-forward 评估，打印验收结论
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import math
import os
import sys
import time
from datetime import datetime
from typing import Any, Iterable

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
from .config import BEIJING, CFG, DOMAIN_EXCLUDE_SQL, FEATURE_COLS, Cfg
from .model import QuantileModel, acceptance, walk_forward

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from push import dispatch, notify  # noqa: E402
from push import progress as task_progress  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("daily")


# ------------------------------------------------------------------ 工具
#: 全系统统一用北京时间归日。常量唯一来源在 pipeline.config.BEIJING。
#:
#: 踩过的坑：这里原先用 UTC 日期（datetime.now(timezone.utc)），而
#:   1) K 线入库是按北京时间归日的（ts + 8h），
#:   2) 挂单量采集用的是本机时间（也是北京），
#:   3) 定时器设在**北京 07:00**（= UTC 前一日 23:00）——
#: 三件事凑在一起就错位：07:00 那次任务算出来的 day 是**昨天**。
#: 实测：08:42 完成的 07:00 任务把 day 写成 2026-10-11，于是
#:   看板的「基准日」整整一天都慢一天，直到 19:00 那次才出现 10-12；
#:   而且 forecast 按 day 做 replace_date，等于把前一天的快照覆盖掉了。
#: batch 采集模式更糟：报价会直接写进前一天的价格行。


def today_str() -> str:
    return datetime.now(BEIJING).strftime("%Y-%m-%d")


def day_start_ts(day: str) -> int:
    """某天（北京）零点的 epoch —— 用于「清理今日旧告警」这类按天划界的操作"""
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=BEIJING).timestamp())


def prune_factor_latest(con, keep_ids: Iterable[str]) -> int:
    """把不在本轮截面里的 factor_latest 行删掉，返回删除条数。

    为什么不能只按日期删（`date <> day`）：
      同一天会有第二次刷新（19:00 那次），早上入选、晚上被流动性闸门剔掉的品种，
      旧行的 date **仍然是今天** —— 日期条件删不掉它，筛选页会继续拿旧价格
      当现价展示。（审查指出的漏网场景，实测确实会残留。）

    为什么用临时表而不是 NOT IN (...)：截面有 5000+ 个 id，
    SQLite 对超长 IN 列表有变量个数上限（默认 32766，但拼字符串也很蠢）。
    """
    try:
        con.execute("CREATE TEMP TABLE IF NOT EXISTS _keep_factor (item_id TEXT PRIMARY KEY)")
        con.execute("DELETE FROM _keep_factor")
        ids = [(str(i),) for i in keep_ids]
        if ids:
            con.executemany("INSERT OR IGNORE INTO _keep_factor (item_id) VALUES (?)", ids)
        n = con.execute(
            "DELETE FROM factor_latest "
            "WHERE item_id NOT IN (SELECT item_id FROM _keep_factor)"
        ).rowcount
        con.commit()
        return n or 0
    except Exception as e:      # noqa: BLE001
        log.warning("清理 factor_latest 陈旧行失败：%s", e)
        return 0


def load_watchlist(con) -> list[str]:
    """监控池 —— 定义已统一到 collector.WATCHLIST_SQL，避免两处规则漂移"""
    return collector.load_watchlist(con)


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
        # 特征列一变，缓存模型必须作废。
        # 踩过的坑（同类问题的隐患）：原先只看模型文件的新鲜度，而
        # QuantileModel._X() 会 reindex(columns=self.cols, fill_value=0.0) ——
        # 于是「改了 FEATURE_COLS 但模型被复用」既不会报错、也不会崩，
        # 只会拿着一套错位的列静默预测出垃圾。这种错最难发现，必须显式挡住。
        cached_cols: list[str] = []
        try:
            cached = QuantileModel.load(cfg.model_path)
            cached_cols = list(cached.cols)
        except Exception as e:                      # 模型文件损坏/格式变了
            log.warning("缓存模型无法加载（%s），重训", e)
        same_cols = cached_cols == list(FEATURE_COLS)
        if not same_cols:
            log.info("特征列与缓存模型不一致（缓存 %d 列 / 现在 %d 列），强制重训",
                     len(cached_cols), len(FEATURE_COLS))
        need_train = age_days >= cfg.retrain_days or not same_cols
        log.info("现有模型 %.2f 天，%s", age_days, "需要重训" if need_train else "复用")

    if not need_train:
        mdl = QuantileModel.load(cfg.model_path)
    else:
        train_part, calib_part = _split_tail(mature, 0.8, cfg.horizon)
        w = labels.sample_weights(train_part, cfg)
        log.info("训练：%d 行 × %d 特征", len(train_part), len(FEATURE_COLS))
        mdl = model_train(train_part, w, cfg)
        mdl.save(cfg.model_path)

    # ---- CQR 校准：永远用「训练段之后的独立窗口」----
    train_part, calib_part = _split_tail(mature, 0.8, cfg.horizon)
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


def _snap_items(con) -> list[str]:
    """快照仓里有数据的品种 —— 已剔除域外的纪念品 / StatTrak。

    口径必须和采集监控池一致（都用 config.DOMAIN_EXCLUDE_SQL）：
    否则会出现「采集时不看、预测时又冒出来」的不一致。
    """
    return [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT s.item_id FROM snap s"
            " JOIN items i ON i.item_id = s.item_id"
            " WHERE 1=1" + DOMAIN_EXCLUDE_SQL
        ).fetchall()
    ]


def _apply_liquidity_gate(latest: pd.DataFrame, cfg: Cfg) -> pd.DataFrame:
    """用户规则：「日交易量低的不看（小于 20 一天）」。

    比的是在售挂单量（唯一可得的流动性代理，理由见 collector.collect_listings）。

    这个函数存在的唯一理由是**怎么处理缺失数据**：
        挂单量采集器还没积累出数据时 listings 会是全表 NULL。
        如果这时写成 `COALESCE(listings, 0) >= 20`，就等于把所有品种判成「不合格」，
        页面直接空掉 —— 这正是 /api/screener 栽过的跟头（同一个坑，第二次出现）。
        所以纪律是：**不知道就放行**，并每天在日志里喊一声闸门当前没生效。
    """
    if latest.empty:
        return latest
    if "listings" not in latest.columns:
        log.warning("流动性闸门未生效：截面里没有 listings 列")
        return latest

    known = latest["listings"].notna()
    if not bool(known.any()):
        log.warning("流动性闸门未生效：listings 全为空（挂单量采集器还没积累出数据）"
                    " —— 不把「不知道」当「不合格」，全部放行")
        return latest

    keep = (~known) | (latest["listings"] >= cfg.gate_listings)
    dropped = int((~keep).sum())
    log.info("流动性闸门：挂单量已知 %d 行；剔除挂单量 < %d 的 %d 个品种，保留 %d 行",
             int(known.sum()), cfg.gate_listings, dropped, int(keep.sum()))
    return latest[keep].reset_index(drop=True)


def _load_feats_batched(
    con,
    items: list[str],
    days: int,
    case_df: pd.DataFrame,
    index_df: pd.DataFrame | None,
    events_df: pd.DataFrame | None,
    cfg: Cfg,
    only_latest: bool = False,
) -> pd.DataFrame:
    """按品种分块算特征，把峰值内存压到 1/N。

    为什么必须分块：
        回填完成后 snap 有 542 万行（原来 216 万行）。一次性
        load_history → features.build 会让 python 吃到 3.1GB RSS + 2.1GB swap，
        被内核 OOM 直接杀掉 —— 而且死得毫无痕迹：日志停在上一行、没有 traceback，
        看上去就像「跑到一半自己没了」（2026-10-11 21:19 实际发生过）。

    only_latest=True 时每块只保留每个品种的最后一行：
        推理只需要截面，不需要序列，于是内存只跟品种数成正比，
        和「历史有多长」彻底解耦。
    """
    import gc

    parts: list[pd.DataFrame] = []
    batch = max(int(cfg.feat_batch_items), 1)
    done = 0
    rows_acc = 0
    for i in range(0, len(items), batch):
        chunk = items[i : i + batch]
        h = collector.load_history(con, days=days, items=chunk)
        if h.empty:
            continue
        f = features.build(h, case_df, index_df, events_df)
        del h
        out = features.latest_cross_section(f) if only_latest else f
        del f
        parts.append(out)
        rows_acc += len(out)
        done += len(chunk)
        if (i // batch) % 5 == 0:
            log.info("  特征分块 %d/%d 品种，累计 %d 行", done, len(items), rows_acc)
        gc.collect()

    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def model_train(train_part: pd.DataFrame, w: np.ndarray, cfg: Cfg) -> QuantileModel:
    from .model import train_all

    return train_all(train_part[FEATURE_COLS], train_part["y"], w, cfg)


def _split_tail(df: pd.DataFrame, frac: float, horizon: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按**日期**切分训练段与校准段，并 purge 掉标签跨越切分点的样本。

    为什么不能按行数切（原先的 d.iloc[:cut] / d.iloc[cut:]）：
      1) 同一天的所有品种会被切到两边，而横截面样本彼此高度相关
      2) 更严重的是标签泄漏：训练段最后一天的 21 日前向收益会用到校准期的价格，
         于是校准集不再是独立窗口，CQR 的覆盖率保证形同虚设

    做法：训练集只保留「切分日 - horizon」之前的数据，确保其标签完全落在校准期开始之前。
    """
    d = df.sort_values("date").reset_index(drop=True)
    if d.empty:
        return d, d
    cut_idx = min(max(int(len(d) * frac), 1), len(d) - 1)
    cut_date = d["date"].iloc[cut_idx]
    train = d[d["date"] <= cut_date - pd.Timedelta(days=horizon)]
    calib = d[d["date"] >= cut_date]
    return train, calib


def _push_cold_start(con, cfg: Cfg, day: str, now_ts: int, labeled, reason: str) -> None:
    """冷启动期（标签未成熟）只推送基础数据。

    没有 forecast / factor_latest —— 前端会显示「今日打分标的 0」，
    但 K 线、饰品列表、运行日志都是真实的。总比线上空空如也强，
    也让你能提前验证采集链路是否稳定。
    """
    try:
        # 注意：这里**不再**生成/推送 kline。
        # 采集阶段（collector.collect_daily_kline）已经把 K 线原始 OHLC 写进 kline 表了；
        # 而曾经那个 make_kline（已删）只能从 snap 的单点价格反推 o=h=l=c 的假 OHLC，
        # 推上去会把真数据覆盖成假的（踩过，所以那个函数连同它的用法一起删掉了）。
        #
        # ⚠️ 关联条件用 item_id，不是 market_hash_name。
        # 原先写的是 `WHERE market_hash_name IN (SELECT DISTINCT item_id FROM snap)` ——
        # 拿「哈希名列」去比「id 列」。它当时能跑通，纯粹因为 write_items 把
        # item_id 设成了 market_hash_name（当前的不变量），不是结构上成立。
        # 属于紧挨着的注释警告的那类「靠巧合」的写法，已改成同列比较。
        item_rows = [
            {"item_id": r[0], "name": r[1], "market_hash_name": r[2], "wear": r[3],
             "case_id": r[4], "case_name": r[5], "image": r[6], "updated_at": now_ts}
            for r in con.execute(
                "SELECT item_id, name, market_hash_name, wear, case_id, case_name, image FROM items "
                "WHERE item_id IN (SELECT DISTINCT item_id FROM snap)"
            ).fetchall()
        ]

        plan = [
            ("items", item_rows, {}),
            ("meta", [
                {"k": "model_version", "v": f"{cfg.model_version}-coldstart", "updated_at": now_ts},
                {"k": "last_run", "v": day, "updated_at": now_ts},
                {"k": "status", "v": "cold_start", "updated_at": now_ts},
                {"k": "cold_start_reason", "v": reason[:200], "updated_at": now_ts},
                {"k": "feature_rows", "v": str(len(labeled)), "updated_at": now_ts},
            ], {}),
            ("runs", [
                {"ts": now_ts, "stage": "daily-coldstart", "ok": 1, "rows": len(item_rows),
                 "message": f"冷启动：特征 {len(labeled)} 行；预测已跳过"},
            ], {}),
        ]
        for stage, rows, kw in plan:
            dispatch.push(stage, rows, **kw)
        log.info("冷启动基础数据已推送：items %d（kline 由采集阶段写入）", len(item_rows))
    except Exception as e:
        log.error("冷启动推送失败：%s", e)


# ------------------------------------------------------------------ 主流程
def run(dry_run: bool = False, backtest_only: bool = False, skip_collect: bool = False) -> int:
    cfg = CFG
    cfg.ensure_dirs()
    key = os.environ.get("STEAMDT_KEY", "")
    if not key and not skip_collect:
        log.error("STEAMDT_KEY 未配置，退出")
        return 2

    con = collector.connect(cfg.db_path)
    session = requests.Session()
    day = today_str()
    now_ts = int(time.time())      # 提前定义：训练失败走冷启动分支时也要用到
    fails: list[str] = []

    # ---------------- 1. 采集 ----------------
    # 采集只提供「价格」这一件事的增量，但它要占掉整个管线 90% 的时间
    # （18853 个品种 × 0.6s ≈ 3.2 小时）。改代码后想快速看新模型，用
    # --skip-collect 复用已有历史，30 分钟就能跑完特征+训练+预测。
    watchlist = load_watchlist(con)
    if skip_collect:
        log.info("--skip-collect：跳过采集，直接用库里的历史（监控池 %d 个）", len(watchlist))
    else:
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

        log.info("监控池 %d 个可交易品种（武器皮肤 + 探员）", len(watchlist))

        # WATCH_LIMIT=0 表示不限量。
        limit = int(os.environ.get("WATCH_LIMIT", "0"))
        pool = watchlist if limit <= 0 else watchlist[:limit]

        # 采集通道：
        #   kline（默认）= /open/cs2/item/v1/kline，120 次/分钟，一次拿整段历史（能补漏掉的日期）
        #   batch       = /price/batch，1 次/分钟 × 100 个，只有当前快照
        mode = os.environ.get("COLLECT_MODE", "kline").strip().lower()
        try:
            if mode == "batch":
                log.info("采集[batch] %d 个，预计 %.0f 分钟", len(pool), len(pool) / 100 * 61 / 60)
                blocks = collector.fetch_prices(pool, key, session)
                n = collector.write_daily_snapshot(con, blocks, day)
                log.info("写入当日快照 %d 行", n)
            else:
                log.info("采集[kline] %d 个，预计 %.0f 分钟", len(pool), len(pool) * 0.6 / 60)
                stats = collector.collect_daily_kline(con, session, key, pool)
                log.info("K线采集结果：成功 %d / 空 %d / 失败 %d / 写入 %d 行",
                         stats["ok"], stats["empty"], stats["fail"], stats["rows"])
        except Exception as e:
            fails.append(f"采集失败: {e}")
            log.error("采集失败：%s", e)

    # ---------------- 2. 特征 ----------------
    case_df = pd.DataFrame(cases_mod.load_cases(con))
    index_df = _load_index(con)
    events_df = _load_events(con)

    all_items = _snap_items(con)
    if not all_items:
        log.error("本地无历史数据，无法继续")
        return 4

    # 训练集按品种抽样：542 万行全量训练在 4G 内存上跑不动，而横截面样本
    # 彼此高度相关，抽 4000 个品种（约 130 万行）对分位数回归的损失很小。
    train_items = all_items
    if 0 < cfg.train_item_limit < len(all_items):
        rng = np.random.default_rng(20261011)   # 固定种子：抽样可复现，便于对比两版模型
        train_items = sorted(
            rng.choice(np.array(all_items), size=cfg.train_item_limit, replace=False).tolist()
        )
        log.info("训练集抽样 %d/%d 个品种（内存约束，TRAIN_ITEM_LIMIT 可调）",
                 len(train_items), len(all_items))

    feats = _load_feats_batched(con, train_items, 1200, case_df, index_df, events_df, cfg)
    if feats.empty:
        log.error("特征宽表为空，无法继续")
        return 4
    labeled = labels.make_labels(feats, cfg)
    log.info("特征宽表 %d 行 × %d 列（训练集 %d 个品种）",
             len(labeled), labeled.shape[1], len(train_items))
    del feats
    gc.collect()

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
    task_progress.report("train", "running", force=True,
                         message=f"训练中（{len(labeled):,} 行特征）")
    try:
        mdl, cqr, calib_meta = load_or_train(cfg, labeled)
        task_progress.finish("train", message=f"训练完成 · {cfg.model_version}")
    except RuntimeError as e:
        log.error("%s", e)
        fails.append(str(e))
        task_progress.fail("train", str(e))
        if not dry_run:
            log.warning("冷启动期：跳过预测，但仍推送已采集的基础数据（items / kline）")
            _push_cold_start(con, cfg, day, now_ts, labeled, str(e))
        return 5

    # ---------------- 5. 推理 ----------------
    # 推理要覆盖全部品种（16k 个），但每个品种只需要最后一行 →
    # 分块算特征、每块只留截面，内存与历史长度解耦。
    latest = _load_feats_batched(
        con, all_items, cfg.infer_days, case_df, index_df, events_df, cfg, only_latest=True
    )
    if latest.empty:
        log.error("推理截面为空，无法继续")
        return 6
    log.info("推理截面 %d 行 × %d 列（近 %d 天）", len(latest), latest.shape[1], cfg.infer_days)

    # 用户规则：日交易量（用挂单量代理）< 20 不看
    latest = _apply_liquidity_gate(latest, cfg)
    if latest.empty:
        # 连截面都没了：不能就这么 return —— factor_latest 里还留着上一轮的旧行，
        # 页面上会继续显示它们，而它们既不是今天的、也不是本轮选出来的。
        # 宁可空着（诚实地表示「今天没有截面」），也不拿旧数据冒充现价。
        log.error("流动性闸门把截面全部剔除：清掉 factor_latest 旧行后退出")
        n = prune_factor_latest(con, [])
        if n:
            log.warning("已清理 factor_latest %d 条（本轮无截面，不保留旧行）", n)
        return 6
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
    # 「未来 21 日收益 ≥ +10%」的概率。
    #
    # 踩过的坑：原先写的是 np.clip((P50 + 0.05) / 0.25, 0, 1) —— 对中位数做线性裁剪，
    # 于是 P50=0.20 就显示成「反弹概率 100%」，即使下分位还在严重亏损。
    # 它既不是概率、也没经过校准，却在前端叫「反弹概率」并参与综合评分。
    #
    # 现在改为用分位数反推分布：正态假设下 P10/P90 对应 ±1.2816σ，
    # 于是 σ = (P90 - P10) / (2 × 1.2816)，再算 P(X ≥ log(1.10))。
    # 这仍然依赖正态假设（不是校准过的概率），但至少用上了分布宽度，
    # 而且下分位很差时结果会自然变小。
    _REBOUND_TARGET = math.log(1.10)

    def _norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    _sigma = np.maximum((P[:, 2] - P[:, 0]) / (2.0 * 1.2816), 1e-6)
    prob_up = np.array([
        1.0 - _norm_cdf((_REBOUND_TARGET - float(p50)) / float(s))
        for p50, s in zip(P[:, 1], _sigma)
    ])

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
    # 不推 kline：采集阶段已把原始 OHLC 写进 kline 表，
    # 而从 snap 单点反推的 o=h=l=c 是假 OHLC，推上去会用假数据覆盖真数据
    # （踩过；当初那个 make_kline 函数连它一起删了，免得有人再去调）。
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

    # alerts 是自增主键，同一天重跑会不断叠加（实测演示数据里同一条 L3 出现了 3 次）。
    # 日线策略一天一份快照，所以推送前先清掉当天的旧记录。
    # 「今天」按北京零点划界 —— day 现在就是北京日期，别再当 UTC 解释
    # （原先 replace(tzinfo=utc) 会把这 8 小时算错，跨零点的告警清不掉）。
    try:
        day_start = day_start_ts(day)
        n_del = con.execute("DELETE FROM alerts WHERE ts >= ?", (day_start,)).rowcount
        con.commit()
        if n_del:
            log.info("清理今日旧告警 %d 条（同日重跑不叠加）", n_del)
    except Exception as e:
        log.warning("清理旧告警失败：%s", e)

    log.info("推送通道：%s", dispatch.describe())
    try:
        for stage, rows, kw in [
            ("items", item_rows, {}),
            ("case_meta", case_rows, {}),
            ("factor_latest", factor_rows, {}),
            ("forecast", forecast_rows, {"replace_date": day}),
            ("alerts", alert_rows, {}),
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

    # factor_latest 是「当前截面」表，不是历史表：本轮没入选的品种
    # （被流动性闸门剔掉 / 被域筛选排除 / 本轮没数据）如果留着旧行，
    # 页面会拿旧价格当现价展示 —— 而 /api/screener 只按 price 和 rarity 过滤、
    # **不看日期**，所以这些旧行一定会露出来。
    # 实测：修之前积了 10,732 行 10-11 的旧行，而当时池子只有 5,435 个品种。
    #
    # 注意这里必须按 **item_id 白名单** 删，不能只按日期删：
    # 同一天有第二次刷新（19:00 那次），早上入选、晚上被闸门剔掉的品种
    # 日期仍然是今天，`date <> day` 删不掉它（审查指出的漏网场景）。
    n_stale = prune_factor_latest(con, [r["item_id"] for r in factor_rows])
    if n_stale:
        log.info("清理 factor_latest 陈旧行 %d 条（本轮未入选的品种不再展示）", n_stale)

    # ---------------- 7. 通知 ----------------
    if candidates:
        notify.notify_digest(day, candidates, fails or None)

    # 告警通知：先按等级筛 + 按分数排序，**最后**才限量。
    # 踩过的坑：原先写法是 `for a in alert_rows[:5]: if a["level"] == "L3"` ——
    # alert_rows 是按品种遍历顺序追加的（与严重程度无关），于是前 5 条里
    # 常常一条 L3 都没有。实测那一刻库里 L3 有 235 条，前 5 条是
    # [L2, L3, L3, L2, L3]，只推出去了 3 条；换个顺序就可能一条都不推。
    # 顺序必须是：筛等级 → 按分数降序 → 取前 N。其余留在看板上。
    price_by_item = {
        r["item_id"]: r["price"]
        for r in factor_rows
        if r.get("item_id") and r.get("price") is not None
    }
    l3_alerts = sorted(
        (a for a in alert_rows if a["level"] == "L3"),
        key=lambda x: -float(x.get("score") or 0.0),
    )
    if l3_alerts:
        log.info("L3 告警 %d 条，按分数取前 %d 条推送（其余留在看板）",
                 len(l3_alerts), min(len(l3_alerts), cfg.alert_push_max))
    for a in l3_alerts[: cfg.alert_push_max]:
        notify.notify_item(
            a["name"] or a["item_id"], a["level"],
            float(price_by_item.get(a["item_id"]) or 0.0),
            a["detail"] or "",
        )

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
            # 对数 → 简单收益换算统一走 notify.log_to_simple_pct，
            # 别再各自写 np.expm1（手机通知那条通道就是这么写错的）
            notify.log_to_simple_pct(c["p10"]), notify.log_to_simple_pct(c["p50"]),
            c["size"] * 100, c["score"],
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="skin-alpha 每日管线")
    ap.add_argument("--dry-run", action="store_true", help="只计算不推送")
    ap.add_argument("--backtest", action="store_true", help="只做 walk-forward 评估并打印验收结论")
    ap.add_argument("--skip-collect", action="store_true",
                    help="跳过采集（复用库里已有历史）—— 改完特征/模型后快速重跑用，"
                         "省掉 3.2 小时的 K 线采集")
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

    return run(dry_run=args.dry_run, backtest_only=args.backtest, skip_collect=args.skip_collect)


if __name__ == "__main__":
    raise SystemExit(main())
