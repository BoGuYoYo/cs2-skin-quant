#!/usr/bin/env python3
"""生成演示数据并推送到 Worker —— 让空看板立刻有内容，用于验证整条展示链路。

用法（本地 wrangler dev）：
    python scripts/seed_demo.py

指定目标（线上 Worker）：
    python scripts/seed_demo.py --url https://xxx.workers.dev --token <INGEST_TOKEN>

数据是合成的（几何布朗运动），走势模仿真实崩塌形态，用于看界面，不是真实行情。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import requests

# item_id, 名称, market_hash_name, 磨损, 箱子, 起始价, 日漂移, 日波动, 稀有度, 箱日成交量, 存世增速%, 掉落池, 在售比
SPECS = [
    ("tec9-brother", "Tec-9 | 兄弟连 (崭新出厂)", "Tec-9 | Brother (Factory New)", "Factory New",
     "case-fracture", "Fracture Case", 76.80, -0.0110, 0.035, 41.0, 2800.0, 0.85, "active", 0.053),
    ("r8-bonemask", "R8 左轮手枪 | 废物王 (崭新出厂)", "R8 Revolver | Bone Mask (Factory New)", "Factory New",
     "case-chroma2", "Chroma 2 Case", 34.99, -0.0100, 0.032, 45.0, 2400.0, 0.72, "active", 0.041),
    ("ak-redline-ft", "AK-47 | 红线 (久经沙场)", "AK-47 | Redline (Field-Tested)", "Field-Tested",
     "case-phoenix", "Operation Phoenix Weapon Case", 92.50, -0.0015, 0.018, 88.0, 180.0, 0.04, "removed", 0.021),
    ("awp-asiimov-ft", "AWP | 二西莫夫 (久经沙场)", "AWP | Asiimov (Field-Tested)", "Field-Tested",
     "case-winter", "Winter Offensive Weapon Case", 108.00, -0.0022, 0.020, 84.0, 210.0, 0.06, "removed", 0.024),
    ("m4a4-howl", "M4A4 | 咆哮 (久经沙场)", "M4A4 | Howl (Field-Tested)", "Field-Tested",
     "case-huntsman", "Huntsman Weapon Case", 96.00, 0.0008, 0.015, 95.0, 90.0, 0.01, "removed", 0.012),
    ("glock-fade", "格洛克 18 型 | 渐变之色 (崭新出厂)", "Glock-18 | Fade (Factory New)", "Factory New",
     "case-assault", "Assault Collection", 78.00, -0.0045, 0.026, 71.0, 620.0, 0.28, "removed", 0.031),
]

DAYS = 200
HORIZON = 21
SEED = 20261011


def build_payloads(today: date, rng: np.random.Generator):
    dates = [today - timedelta(days=DAYS - 1 - i) for i in range(DAYS)]
    ds = [d.isoformat() for d in dates]

    items, cases, kline, forecast, factors, alerts = [], [], [], [], [], []
    now_ts = int(time.time())

    for (iid, name, mhn, wear, cid, cname, p0, drift, vol, rarity,
         case_vol, growth, pool, list_ratio) in SPECS:
        # 价格：几何布朗运动 + 指定漂移
        steps = rng.normal(drift, vol, DAYS)
        path = p0 * np.exp(np.cumsum(steps))
        path = np.maximum(path, 1.0)

        supply = int(3000 + rarity * 60 + rng.integers(0, 4000))
        supply_path = (supply * np.exp(np.cumsum(rng.normal(growth / 100 / DAYS * 4, 0.0008, DAYS)))).astype(int)

        items.append({
            "item_id": iid, "name": name, "market_hash_name": mhn, "wear": wear,
            "case_id": cid, "case_name": cname, "image": None, "updated_at": now_ts,
        })

        for i, d in enumerate(ds):
            c = float(path[i])
            hi = c * (1 + abs(float(rng.normal(0, 0.012))))
            lo = c * (1 - abs(float(rng.normal(0, 0.012))))
            op = lo + (hi - lo) * float(rng.random())
            kline.append({
                "item_id": iid, "date": d,
                "o": round(op, 2), "h": round(max(hi, c, op), 2),
                "l": round(min(lo, c, op), 2), "c": round(c, 2),
                "v": int(max(0, rng.poisson(max(case_vol / 240, 1)))),
            })

        price = float(path[-1])
        ret_21 = float(math_log(path[-1], path[-22])) if DAYS > 22 else 0.0
        ret_7 = float(math_log(path[-1], path[-8]))
        dd = price / float(np.max(path[-30:])) - 1.0
        # 下跌持续天数：从 30 日高点算起
        hi30 = float(np.max(path[-30:]))
        dur = 0
        for v in path[::-1]:
            if v < hi30:
                dur += 1
            else:
                break

        turnover = float(case_vol) / max(float(supply_path[-1]), 1.0) * 0.02
        rigidity = (rarity / 100.0) * (1.0 - np.tanh(growth / 5.0))

        # 预测：用近期漂移外推 + 波动构造分位数（仅演示）
        mu = ret_21 / 21 * HORIZON * 0.5
        sigma = vol * np.sqrt(HORIZON)
        q10, q50, q90 = mu - 1.28 * sigma, mu, mu + 1.28 * sigma
        adj = 0.6 * sigma * 0.15
        q10a, q90a = q10 - adj, q90 + adj
        prob = float(np.clip((q50 + 0.05) / 0.25, 0.0, 1.0))

        if q10a <= 0:
            side, size = "skip", 0.0
            why = f"下沿 {np.expm1(q10a):+.1%} 仍为负"
        elif q50 < 0.10:
            side, size = "watch", 0.0
            why = f"中位仅 {np.expm1(q50):+.1%}，不足 10%"
        else:
            side, size = "buy", 0.05
            why = (f"P10 {np.expm1(q10a):+.1%} / P50 {np.expm1(q50):+.1%} / "
                   f"P90 {np.expm1(q90a):+.1%}")

        score = round(40 * float(np.clip(q10a / 0.30, 0, 1)) + 25 * prob
                      + 15 * float(np.clip((rarity - 40) / 60, 0, 1)) + 10, 2)

        forecast.append({
            "item_id": iid, "date": today.isoformat(), "horizon": HORIZON,
            "q10": round(q10, 4), "q50": round(q50, 4), "q90": round(q90, 4),
            "q10_adj": round(q10a, 4), "q90_adj": round(q90a, 4),
            "prob_up": round(prob, 4), "fwd_max_dd": round(-sigma * 0.8, 4),
            "score": score, "side": side, "size": size, "why": why,
            "model_version": "demo-seed",
        })

        factors.append({
            "item_id": iid, "date": today.isoformat(), "price": round(price, 2),
            "dd_from_high": round(dd, 4), "drop_duration_days": dur,
            "ret_7d": round(ret_7, 4), "ret_21d": round(ret_21, 4),
            "ret_30d": round(math_log(path[-1], path[-31]), 4) if DAYS > 31 else 0.0,
            "vol_21": round(float(np.std(np.diff(np.log(path[-22:])))), 4),
            "turnover": round(turnover, 5), "list_ratio": list_ratio,
            "list_slope_7d": round(float(rng.normal(18, 30)), 2),
            "supply_peer_pct": round(float(rng.uniform(0.1, 0.6)), 4),
            "supply_rigidity": round(float(rigidity), 4),
            "case_rarity_score": rarity,
            "rel_ret_21d": round(ret_21 + 0.02, 4),
            "event_bearish_30d": int(rng.integers(0, 3)),
            "event_bullish_30d": int(rng.integers(0, 2)),
            "updated_at": now_ts,
        })

        drop_pct = (1 - price / float(np.max(path[-30:]))) * 100
        if drop_pct >= 25:
            lvl = "L3"
        elif drop_pct >= 15:
            lvl = "L2"
        elif drop_pct >= 8:
            lvl = "L1"
        else:
            lvl = None
        if lvl:
            alerts.append({
                "item_id": iid, "name": name, "level": lvl,
                "score": round(min(100, drop_pct * 2.5), 1),
                "ts": now_ts - int(rng.integers(600, 60000)),
                "detail": f"30日自高点 -{drop_pct:.1f}% · 下跌 {dur} 天 · 换手 {turnover*100:.2f}%",
                "pushed": 0,
            })

        cases.append({
            "case_id": cid, "case_name": cname, "rarity_score": rarity,
            "case_price": round(2.0 + (100 - rarity) * 0.35, 2),
            "case_volume_30d": case_vol, "supply_growth_30d": growth,
            "drop_pool": pool,
            "rationale": f"{'已移出掉落池' if pool == 'removed' else '仍在掉落池'}；"
                         f"存世量 30 日增速 {growth:.2f}%；箱子日成交 {case_vol:.0f}",
            "blocking_risk": ("掉落池仍在活跃状态，新增供给会持续压制价格"
                              if pool == "active" else
                              "存世量增速仍高，供给未见饱和" if growth > 0.5 else None),
            "updated_at": now_ts,
        })

    # 大盘指数
    idx = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.008, DAYS)))
    market = [
        {"date": d, "index_value": round(float(idx[i]), 2),
         "online_players": int(900000 + rng.integers(0, 400000)),
         "vol_turnover": round(float(rng.uniform(3000, 6000)), 1)}
        for i, d in enumerate(ds)
    ]

    # 回测净值
    bt_days = 40
    bt_dates = [today - timedelta(days=bt_days - 1 - i) for i in range(bt_days)]
    cum = 0.0
    backtest = []
    for d in bt_dates:
        r = float(rng.normal(0.012, 0.035))
        cum += r
        backtest.append({
            "date": d.isoformat(), "mean_21d": round(r, 4),
            "cum": round(cum, 4), "win_rate": round(float(rng.uniform(0.45, 0.7)), 3),
            "n": 20, "model_version": "demo-seed",
        })

    meta = [
        {"k": "model_version", "v": "demo-seed", "updated_at": now_ts},
        {"k": "last_run", "v": today.isoformat(), "updated_at": now_ts},
        {"k": "horizon", "v": str(HORIZON), "updated_at": now_ts},
        {"k": "cqr_Q", "v": "0.0131", "updated_at": now_ts},
        {"k": "note", "v": "演示数据（合成），非真实行情", "updated_at": now_ts},
    ]
    return {
        "items": items, "case_meta": cases, "kline": kline,
        "forecast": forecast, "factor_latest": factors,
        "alerts": alerts, "market_index": market, "backtest": backtest, "meta": meta,
    }


def math_log(a: float, b: float) -> float:
    return float(np.log(max(a, 1e-9) / max(b, 1e-9)))


SERVER_DIR = Path(__file__).resolve().parent.parent / "server"
_dispatch = None


def _get_dispatch(mode: str, url: str, token: str):
    """延迟导入 push.dispatch —— 它是在 import 时读 PUSH_MODE 的，必须先设好环境变量"""
    global _dispatch
    if _dispatch is None:
        os.environ["PUSH_MODE"] = mode
        if url:
            os.environ.setdefault("CF_PUSH_URL", url)
        if token:
            os.environ.setdefault("INGEST_TOKEN", token)
        if str(SERVER_DIR) not in sys.path:
            sys.path.insert(0, str(SERVER_DIR))
        from push import dispatch as _d
        _dispatch = _d
        print(f"  推送通道：{_d.describe()}")
    return _dispatch


def push_stage(stage: str, rows: list, replace_date: str | None = None) -> int:
    if not rows:
        print(f"  - {stage:<14} {'(空)':>6}")
        return 0
    try:
        n = _dispatch.push(stage, rows, replace_date=replace_date)
        print(f"  ✓ {stage:<14} {n:>6} 行")
        return n
    except Exception as e:
        print(f"  ✗ {stage:<14} {e}")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="worker", choices=["worker", "d1", "both"],
                    help="worker=走自己的 Worker；d1=直连 Cloudflare D1 REST API")
    ap.add_argument("--url", default="http://127.0.0.1:8787",
                    help="Worker 地址（--mode worker 时用）")
    ap.add_argument("--token", default="dev-token-local",
                    help="Worker 的 INGEST_TOKEN（--mode worker 时用）")
    ap.add_argument("--date", default=date.today().isoformat())
    args = ap.parse_args()

    today = date.fromisoformat(args.date)
    rng = np.random.default_rng(SEED)

    print(f"\n生成演示数据（{len(SPECS)} 个饰品 × {DAYS} 天）…")
    payloads = build_payloads(today, rng)

    _get_dispatch(args.mode, args.url, args.token)
    target = "Cloudflare D1" if args.mode in ("d1", "both") else args.url
    print(f"目标：{target}")

    total = 0
    for stage, key, rd in [
        ("items", "items", None),
        ("case_meta", "case_meta", None),
        ("factor_latest", "factor_latest", None),
        ("forecast", "forecast", today.isoformat()),
        ("market_index", "market_index", None),
        ("backtest", "backtest", None),
        ("kline", "kline", None),
        ("alerts", "alerts", None),
        ("meta", "meta", None),
    ]:
        total += push_stage(stage, payloads[key], rd)

    print(f"\n完成，共写入 {total} 行。")
    if args.mode in ("d1", "both"):
        print("数据已在线上 D1 —— 等域名 zone 变成 active 就能打开网页看到。\n")
    else:
        print(f"打开 {args.url} 查看。\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
