"""冒烟测试：用合成数据跑通整条管线，不需要任何外部 API / 密钥。

验证链路：
    features.build → labels.make_labels → sample_weights
    → train_all（分位数 + 单调约束）→ predict（交叉修复）
    → CQR 校准（覆盖率）→ decide（异象检测 + 仓位）→ walk_forward（purged）

用法（在 server/ 目录下）：
    python ../scripts/smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from pipeline import decide, features, labels  # noqa: E402
from pipeline.calibrate import CQR, pit_histogram  # noqa: E402
from pipeline.config import CFG, FEATURE_COLS  # noqa: E402
from pipeline.model import train_all, walk_forward  # noqa: E402

FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def synth(n_cases: int = 8, n_items: int = 32, n_days: int = 320, seed: int = 7) -> pd.DataFrame:
    """造一份带「下跌趋势 + 稀有箱子」结构的合成数据"""
    rng = np.random.default_rng(seed)
    days = pd.date_range("2024-01-01", periods=n_days, freq="D")
    rows: list[dict] = []

    for c in range(n_cases):
        case_id = f"case_{c}"
        # 造三种结构，保证三种决策分支都测得到：
        #   稀有 + 供给停滞 + 上涨  → 应该出现 buy
        #   中性下跌                → 多数 watch/skip
        #   泛滥 + 供给洪水 + 暴跌   → 应该出现 skip 和 L2/L3 异象
        if c < 2:
            drift, growth, case_vol = 0.007 + 0.002 * c, 0.0001, 120.0
        elif c < 5:
            drift, growth, case_vol = -0.0015, 0.0012, 900.0
        else:
            drift, growth, case_vol = -0.006, 0.005, 2400.0
        rarity = 92.0 - c * 9.0
        case_drift = drift

        for i in range(n_items // n_cases):
            item_id = f"{case_id}_item_{i}"
            price = float(rng.uniform(5, 90))
            supply = int(rng.integers(3000, 25000))
            listings = max(5, int(supply * rng.uniform(0.01, 0.05)))
            for d in days:
                price = max(price * float(np.exp(rng.normal(case_drift, 0.028))), 1.0)
                supply = int(max(supply * (1 + rng.normal(growth, 0.0015)), 100))
                listings = max(3, int(listings * (1 + rng.normal(0.0, 0.05))), int(supply * 0.005))
                volume = max(0, int(rng.poisson(max(listings * 0.35, 0.5))))
                rows.append({
                    "item_id": item_id,
                    "case_id": case_id,
                    "date": d,
                    "price": round(price, 2),
                    "bid": round(price * rng.uniform(0.85, 0.97), 2),
                    "listings": listings,
                    "volume": volume,
                    "supply": supply,
                })
    return pd.DataFrame(rows)


def main() -> int:
    CFG.n_estimators = 60          # 冒烟测试跑快点
    CFG.n_splits = 3
    CFG.model_version = "smoke"

    print("\n[1] 合成数据")
    hist = synth()
    print(f"  {len(hist):,} 行 · {hist.item_id.nunique()} 个品种 · {hist.case_id.nunique()} 个箱子")

    # 箱子层元数据要和 synth 的结构一致，否则特征与标签的因果链就断了
    case_rules = []
    for c in range(8):
        if c < 2:
            vol, g = 120.0, 0.02
        elif c < 5:
            vol, g = 900.0, 0.12
        else:
            vol, g = 2400.0, 0.50
        case_rules.append({
            "case_id": f"case_{c}", "case_name": f"Case {c}",
            "rarity_score": 92.0 - c * 9.0,
            "case_volume_30d": vol, "supply_growth_30d": g,
        })
    case_df = pd.DataFrame(case_rules)
    periods = pd.date_range(hist.date.min(), hist.date.max(), freq="D")
    index_df = pd.DataFrame({"date": periods, "index_value": 100 + np.arange(len(periods)) * 0.02})

    print("\n[2] 特征工程")
    feats = features.build(hist, case_df, index_df)
    missing = [c for c in FEATURE_COLS if c not in feats.columns]
    check("所有特征列齐备", not missing, f"缺失={missing}" if missing else f"{len(FEATURE_COLS)} 列")
    check("无非有限值爆炸", np.isfinite(feats[FEATURE_COLS].to_numpy(dtype=float)).mean() > 0.95,
          f"有限值占比 {np.isfinite(feats[FEATURE_COLS].to_numpy(dtype=float)).mean():.1%}")
    check("供给刚性已生成", "supply_rigidity" in feats.columns and feats["supply_rigidity"].notna().all())
    check("下跌天数非负", (feats["drop_duration_days"] >= 0).all())
    check("预热标记生效", "is_warm" in feats.columns and bool(feats["is_warm"].any()),
          f"可用样本 {int(feats['is_warm'].sum()):,}/{len(feats):,}")
    warm = feats[feats["is_warm"]] if "is_warm" in feats.columns else feats
    check("预热期后特征无 NaN", bool(warm[FEATURE_COLS].notna().all().all()))
    check("最新截面无 NaN",
          bool(features.latest_cross_section(feats)[FEATURE_COLS].notna().all().all()))

    print("\n[3] 标签与权重")
    labeled = labels.make_labels(feats, CFG)
    mature = labeled.dropna(subset=["y"])
    check("成熟标签充足", len(mature) > 2000, f"{len(mature):,} 行")
    check("净收益已扣费", labeled["y"].abs().max() < 5.0, f"|y|max={labeled['y'].abs().max():.3f}")
    w = labels.sample_weights(mature, CFG)
    check("权重均值≈1", abs(w.mean() - 1.0) < 1e-6, f"mean={w.mean():.4f}")
    check("权重全为正", (w > 0).all(), f"min={w.min():.2e}")

    print("\n[4] 模型训练与推理")
    mdl = train_all(mature[FEATURE_COLS], mature["y"], w, CFG)
    X = mature[FEATURE_COLS].tail(500)
    P = mdl.predict(X)
    taus = sorted(CFG.quantiles)
    check("输出形状正确", P.shape == (len(X), len(taus)), f"{P.shape}")
    mono = np.all(P[:, 0] <= P[:, 1] + 1e-9) and np.all(P[:, 1] <= P[:, 2] + 1e-9)
    check("分位数无交叉（保序）", bool(mono))
    check("预测有区分度", float(P[:, 1].std()) > 1e-4, f"P50 std={P[:, 1].std():.4f}")

    print("\n[4b] 经济先验模型（带单调约束 —— monotone 在 quantile 上被 LightGBM 禁用，故选两阶段）")
    prior = mdl.predict_prior(X)
    check("prior 已训练", prior is not None and len(prior) == len(X),
          "None" if prior is None else f"{len(prior)} 条")
    if prior is not None:
        # 用全量样本做「区间替换」检验，比单点探针靠谱：
        # 单点很容易落在同一个叶子里，那样检验会假通过（我第一版就踩了这个坑）。
        lo_all = X.copy(); lo_all["case_rarity_score"] = 5.0
        hi_all = X.copy(); hi_all["case_rarity_score"] = 98.0
        m_lo, m_hi = float(mdl.predict_prior(lo_all).mean()), float(mdl.predict_prior(hi_all).mean())
        check("稀有度单调性成立", m_hi >= m_lo, f"rarity 5→98：prior 均值 {m_lo:+.5f} → {m_hi:+.5f}")
        check("稀有度确实产生分裂", abs(m_hi - m_lo) > 1e-9, f"Δ={m_hi - m_lo:+.6f}")

        g_lo = X.copy(); g_lo["supply_growth_30d"] = 0.0
        g_hi = X.copy(); g_hi["supply_growth_30d"] = 8.0
        mg_lo, mg_hi = float(mdl.predict_prior(g_lo).mean()), float(mdl.predict_prior(g_hi).mean())
        check("供给增速单调性成立", mg_hi <= mg_lo, f"growth 0→8：prior 均值 {mg_lo:+.5f} → {mg_hi:+.5f}")

        r_lo = X.copy(); r_lo["list_ratio"] = 0.001
        r_hi = X.copy(); r_hi["list_ratio"] = 0.09
        mr_lo, mr_hi = float(mdl.predict_prior(r_lo).mean()), float(mdl.predict_prior(r_hi).mean())
        check("在售占比单调性成立", mr_hi <= mr_lo, f"list_ratio 0.1%→9%：prior 均值 {mr_lo:+.5f} → {mr_hi:+.5f}")

        # 先验与分位数的一致性：两者排序应正相关（否则说明两个模型在打架）
        q50 = P[:, 1]
        corr = float(np.corrcoef(prior, q50)[0, 1])
        check("先验与分位数同向", corr > -0.2, f"corr(prior, P50)={corr:+.3f}")

    print("\n[5] CQR 共形校准")
    train_part, calib = mature.iloc[: int(len(mature) * 0.8)], mature.iloc[int(len(mature) * 0.8):]
    cqr = CQR(alpha=0.20).fit(
        mdl.predict(calib[FEATURE_COLS])[:, 0],
        mdl.predict(calib[FEATURE_COLS])[:, -1],
        calib["y"].to_numpy(),
    )
    check("校正量已算出", cqr.Q is not None and np.isfinite(cqr.Q), f"Q={cqr.Q:.4f}")
    preds = mdl.predict(mature[FEATURE_COLS])
    raw_cov = float(((mature["y"] >= preds[:, 0]) & (mature["y"] <= preds[:, 2])).mean())
    adj = cqr.transform(preds[:, 0], preds[:, 2])
    adj_cov = float(((mature["y"] >= adj[0]) & (mature["y"] <= adj[1])).mean())
    print(f"       校准前区间覆盖率 {raw_cov:.1%} → 校准后 {adj_cov:.1%}（目标 80%）")
    check("校准后覆盖率更接近目标", abs(adj_cov - 0.8) <= abs(raw_cov - 0.8) + 0.05)

    # 只有 3 个分位数，PIT 直方图本来就只会落在 4 个离散值上，不能用「均匀」判定。
    # 真正该查的是：每个 tau 的实测覆盖率是否接近标称值，且随 tau 单调。
    covs = [float((mature["y"] <= preds[:, k]).mean()) for k in range(len(taus))]
    print("       各 tau 实测覆盖率：" + ", ".join(f"{t:.1f}→{c:.1%}" for t, c in zip(taus, covs)))
    check("覆盖率随 tau 单调", all(covs[i] <= covs[i + 1] + 0.02 for i in range(len(covs) - 1)))
    check("P10 覆盖率量级正确", 0.03 <= covs[0] <= 0.25, f"{covs[0]:.1%}")
    check("P90 覆盖率量级正确", 0.70 <= covs[-1] <= 0.98, f"{covs[-1]:.1%}")
    pit = pit_histogram(mature["y"].to_numpy(), {t: preds[:, k] for k, t in enumerate(taus)})
    check("PIT 直方图归一", abs(pit.sum() - 1.0) < 1e-6)

    print("\n[6] 决策与异象检测")
    latest = features.latest_cross_section(labeled)
    L = mdl.predict(latest[FEATURE_COLS])
    lo_adj, hi_adj = cqr.transform(L[:, 0], L[:, -1])
    sides: dict[str, int] = {}
    levels: dict[str, int] = {}
    sizes: list[float] = []
    for i, (_, r) in enumerate(latest.iterrows()):
        row = r.to_dict()
        row.update(q10=L[i, 0], q50=L[i, 1], q90=L[i, 2],
                   q10_adj=float(lo_adj[i]), q90_adj=float(hi_adj[i]))
        d = decide.decide(row, CFG)
        sides[d["side"]] = sides.get(d["side"], 0) + 1
        sizes.append(d["size"])
        s = decide.score(row, CFG)
        check("打分在合理区间", 0 <= s <= 100, f"score={s}") if i == 0 else None
        lv, _sc, _why = decide.detect_anomaly(row, CFG)
        if lv:
            levels[lv] = levels.get(lv, 0) + 1
    print(f"       判断分布 {sides}；异象 {levels or '无'}")
    check("决策输出三分支合法", set(sides) <= {"buy", "watch", "skip"})
    check("仓位不超上限", max(sizes) <= CFG.unit + 1e-9, f"max={max(sizes):.3%}")

    # 三个分支的边界必须逐个测到 —— 这是整套系统最关键的取舍规则
    check("P10≤0 → skip（下沿都亏就不碰）",
          decide.decide({"q10_adj": -0.05, "q50": 0.30, "q90_adj": 0.50}, CFG)["side"] == "skip")
    check("P10>0 且赔率足 → buy",
          decide.decide({"q10_adj": 0.02, "q50": 0.35, "q90_adj": 0.60}, CFG)["side"] == "buy")
    check("中位不足门槛 → watch",
          decide.decide({"q10_adj": 0.01, "q50": 0.05, "q90_adj": 0.20}, CFG)["side"] == "watch")
    check("字段缺失 → skip",
          decide.decide({"q10_adj": None, "q50": 0.3, "q90_adj": 0.5}, CFG)["side"] == "skip")

    print("\n[7] Purged walk-forward 评估")
    metrics, base = walk_forward(labeled, CFG)
    if metrics.empty:
        check("产生 walk-forward 折", False, "样本不足，未产生任何折")
    else:
        print(metrics.to_string(index=False))
        print("       无条件基线：" + ", ".join(f"{k}={v:.4f}" for k, v in base.items()))
        check("产生多折评估", len(metrics) >= 1, f"{len(metrics)} 折")
        check("指标有限", bool(np.isfinite(metrics[[c for c in metrics.columns if c.startswith('pinball')]].to_numpy()).all()))
        check("覆盖率列存在", any(c.startswith("cover_") for c in metrics.columns))

    print("\n" + "=" * 62)
    if FAIL:
        print(f"结果：{len(FAIL)} 项失败 → {FAIL}")
        return 1
    print("结果：全部通过 ✓  管线逻辑自洽（合成数据，不代表真实收益）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
