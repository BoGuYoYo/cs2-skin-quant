"""性能基准：量出管线各阶段的真实耗时与内存峰值。

用途：回答「2C4T / 4GB 的 Linux 服务器够不够跑这套东西」，
     以及「哪些阶段适合放到本机跑、哪些必须留在服务器」。

用法：
    python scripts/bench.py                          # 500 品种 × 1000 天
    python scripts/bench.py --items 2000 --days 1000 # 压力测试（200 万行）
    python scripts/bench.py --threads 2              # 模拟 2 核服务器
"""
from __future__ import annotations

import argparse
import gc
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pandas as pd
import psutil

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from pipeline import features, labels  # noqa: E402
from pipeline.config import CFG, FEATURE_COLS  # noqa: E402
from pipeline.model import train_all  # noqa: E402

PROC = psutil.Process()


def rss_mb() -> float:
    return PROC.memory_info().rss / 1e6


class Meter:
    """后台线程采样 RSS，捕捉瞬时峰值 —— 只看结束时的值会漏掉 pandas 的中间拷贝。"""

    def __init__(self, interval: float = 0.03):
        self.interval = interval
        self._stop = False
        self.base = 0.0
        self.peak = 0.0

    def _run(self) -> None:
        while not self._stop:
            cur = rss_mb()
            if cur > self.peak:
                self.peak = cur
            time.sleep(self.interval)

    def __enter__(self) -> "Meter":
        gc.collect()
        self.base = rss_mb()
        self.peak = self.base
        self._stop = False
        self.t0 = time.perf_counter()
        threading.Thread(target=self._run, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.seconds = time.perf_counter() - self.t0
        self._stop = True
        time.sleep(self.interval * 2)

    @property
    def peak_delta(self) -> float:
        return (self.peak - self.base) / 1024.0   # 峰值增量 GB

    @property
    def end_delta(self) -> float:
        return (rss_mb() - self.base) / 1024.0    # 净驻留 GB


ROWS: list[tuple[str, float, float, float]] = []  # (阶段, 秒, 峰值增量GB, 净驻留GB)


def step(name: str, fn, *a, **kw):
    with Meter() as m:
        out = fn(*a, **kw)
    ROWS.append((name, m.seconds, m.peak_delta, m.end_delta))
    print(f"  {name:<28} {m.seconds:>7.1f}s   峰值 +{m.peak_delta:>5.2f}GB   净增 {m.end_delta:>5.2f}GB")
    sys.stdout.flush()
    return out


# ------------------------------------------------------------------ 数据
def synth(n_items: int, n_days: int, n_cases: int = 120, seed: int = 1) -> pd.DataFrame:
    """向量化生成 —— 逐行 append 会让数据生成本身比训练还慢。"""
    rng = np.random.default_rng(seed)
    case_of_item = rng.integers(0, n_cases, n_items)

    drift = rng.normal(-0.0006, 0.0018, n_items)
    vol = rng.uniform(0.015, 0.045, n_items)
    p0 = rng.uniform(5, 90, n_items)

    steps = rng.normal(0.0, 1.0, (n_items, n_days)) * vol[:, None] + drift[:, None]
    price = np.exp(np.log(p0)[:, None] + np.cumsum(steps, axis=1))

    supply = (
        rng.integers(3000, 25000, n_items)[:, None]
        * np.exp(np.cumsum(rng.normal(0.0004, 0.001, (n_items, n_days)), axis=1))
    ).astype(np.int64)
    listings = np.maximum(3, (supply * rng.uniform(0.01, 0.05, n_items)[:, None]).astype(np.int64))
    volume = rng.poisson(np.maximum(listings * 0.3, 0.5)).astype(np.int64)

    dates = pd.date_range("2022-01-01", periods=n_days, freq="D")
    return pd.DataFrame({
        "item_id": pd.Categorical(np.repeat([f"i{i:05d}" for i in range(n_items)], n_days)),
        "case_id": pd.Categorical(np.repeat([f"c{c:03d}" for c in case_of_item], n_days)),
        "date": np.tile(dates.values, n_items),
        "price": price.ravel().astype(np.float32),
        "bid": (price.ravel() * 0.93).astype(np.float32),
        "listings": listings.ravel(),
        "volume": volume.ravel(),
        "supply": supply.ravel(),
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", type=int, default=500)
    ap.add_argument("--days", type=int, default=1000)
    ap.add_argument("--cases", type=int, default=120)
    ap.add_argument("--threads", type=int, default=0, help="0 = 用满所有核")
    ap.add_argument("--trees", type=int, default=0, help="0 = 用 config 默认值（400）")
    ap.add_argument("--no-train", action="store_true", help="跳过训练，只测数据阶段")
    ap.add_argument(
        "--cpu-ratio",
        type=float,
        default=2.6,
        help="目标机单核性能 / 本机单核性能。i3-4160 vs i5-12600KF ≈ 2.6；同款机器填 1.0",
    )
    args = ap.parse_args()

    if args.threads:
        CFG.num_threads = args.threads
    if args.trees:
        CFG.n_estimators = args.trees

    n_rows = args.items * args.days
    print(f"\n{'=' * 74}")
    print(f"基准：{args.items} 品种 × {args.days} 天 = {n_rows:,} 行")
    print(f"线程 num_threads={'auto' if not CFG.num_threads else CFG.num_threads}   "
          f"树数={CFG.n_estimators}   分位数={len(CFG.quantiles)}+prior")
    print(f"本机：{psutil.cpu_count(logical=False)} 物理核 / {psutil.cpu_count()} 逻辑核 / "
          f"{psutil.virtual_memory().total / 1e9:.1f}GB 内存")
    print(f"{'=' * 74}")

    hist = step("合成数据", synth, args.items, args.days, args.cases)
    print(f"  DataFrame 常驻 {hist.memory_usage(deep=True).sum() / 1e6:.0f} MB")

    case_df = pd.DataFrame({
        "case_id": pd.Categorical([f"c{c:03d}" for c in range(args.cases)]),
        "case_name": [f"Case {c}" for c in range(args.cases)],
        "rarity_score": np.linspace(20, 95, args.cases),
        "case_volume_30d": np.linspace(100, 3000, args.cases),
        "supply_growth_30d": np.linspace(0.05, 2.5, args.cases),
    })
    periods = pd.date_range(hist.date.min(), hist.date.max(), freq="D")
    index_df = pd.DataFrame({"date": periods, "index_value": 100 + np.arange(len(periods)) * 0.02})

    feats = step("特征工程", features.build, hist, case_df, index_df)
    del hist
    gc.collect()

    labeled = step("标签构造", labels.make_labels, feats, CFG)

    warm = labeled.dropna(subset=["y"])
    if "is_warm" in warm.columns:
        warm = warm[warm["is_warm"]]
    print(f"  可用训练样本 {len(warm):,} 行（预热期与未成熟标签已剔除）")

    w = step("样本权重（唯一性）", labels.sample_weights, warm, CFG)

    if args.no_train:
        _summary(n_rows, args.cpu_ratio)
        return 0

    mdl = step(f"训练（{len(CFG.quantiles)}+1 个模型）", train_all, warm[FEATURE_COLS], warm["y"], w, CFG)

    latest = features.latest_cross_section(labeled)
    step("推理（全截面）", mdl.predict, latest[FEATURE_COLS])

    _summary(n_rows, args.cpu_ratio)
    return 0


def _summary(n_rows: int, cpu_ratio: float) -> None:
    total = sum(r[1] for r in ROWS)
    peak = max(r[2] for r in ROWS)
    print(f"\n{'-' * 74}")
    print(f"{'阶段':<28}{'耗时':>10}{'峰值增量':>12}")
    print(f"{'-' * 74}")
    for name, sec, pk, _end in ROWS:
        print(f"{name:<28}{sec:>9.1f}s{pk:>11.2f}GB")
    print(f"{'-' * 74}")
    print(f"{'合计':<28}{total:>9.1f}s")
    print(f"{'内存峰值增量':<28}{peak:>10.2f}GB   → 4GB 机器可运行性判断见下")
    print(f"{'-' * 74}")

    # 轻量阶段（纯 I/O + 少量计算）在 2 核上几乎不受影响；重 CPU 阶段按单核性能折算
    io_steps = {"合成数据", "推理（全截面）"}
    est = sum(s if n in io_steps else s * cpu_ratio for n, s, _p, _e in ROWS)
    print(f"\n推算到目标机（单核折算 ×{cpu_ratio}，内存不足会 swap 未计入）：")
    print(f"  预计耗时 ≈ {est / 60:.1f} 分钟（本机 {total / 60:.1f} 分钟）")
    if peak > 2.0:
        print(f"  ⚠️ 峰值内存增量 {peak:.2f}GB —— 4GB 机器需减规模或加 swap")
    elif peak > 1.0:
        print(f"  ⚠️ 峰值内存增量 {peak:.2f}GB —— 4GB 可以跑，但要留出系统余量")
    else:
        print(f"  ✓ 峰值内存增量 {peak:.2f}GB —— 4GB 机器余量充足")
    print(f"  提示：回测（--backtest）是 6 折 × {len(CFG.quantiles)}+1 个模型 ≈ 训练耗时的 {6}x，"
          f"建议放本机跑。")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
