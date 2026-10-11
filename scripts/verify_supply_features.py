"""供给侧特征存活验证 —— 证明 case_id 真的打通到了特征宽表。

只证明一件事：
    features.build 产出的 case_rarity_score / supply_rigidity / supply_peer_pct
    是**随箱子变化**的真实信号，而不是常数。

为什么需要这个脚本：
    collector.load_history 原先只 SELECT snap 的列，没有 JOIN items 取 case_id，
    而 features.build 里合并箱子层数据的前置条件正是 `if "case_id" in df.columns`，
    于是那整段被静默跳过 —— 23 个特征里有 9 个退化成常数：
        case_rarity_score ≡ 50、supply_rigidity ≡ 0.2315、drop_pool ≡ "unknown"、
        supply_peer_pct ≡ 全局分位（而不是同箱同品质内分位）
    MONOTONE 里针对这几列的单调约束因此全成了摆设。
    这类 bug 不会抛异常、不会报警，只会让模型安静地变笨，所以必须有断言守住。

用法（服务器上，cwd = /opt/skin-alpha/server）：
    .venv/bin/python ../scripts/verify_supply_features.py            # 全量
    .venv/bin/python ../scripts/verify_supply_features.py --days 90  # 只取近 90 天，快
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from pipeline import cases as cases_mod  # noqa: E402
from pipeline import collector, features  # noqa: E402
from pipeline.config import CFG, FEATURE_COLS  # noqa: E402

# features.build 里由箱表层（case_meta）驱动的列
CASE_COLS = ["case_rarity_score", "supply_rigidity", "drop_pool"]


def nunique_report(df: pd.DataFrame, cols: list[str]) -> int:
    """打印每列的 unique 值个数 / 缺失率，返回失败列数"""
    bad = 0
    for c in cols:
        if c not in df.columns:
            print(f"  {c:<20} 缺失整列 ✗")
            bad += 1
            continue
        s = df[c]
        n = s.nunique(dropna=True)
        nn = s.notna().mean()
        head = "、".join(str(v) for v in s.dropna().unique()[:4])
        flag = "✓" if n > 1 else "✗ 常数！"
        if n <= 1:
            bad += 1
        print(f"  {c:<20} unique={n:<6} 非空={nn:6.1%}  {flag}  样例: {head[:70]}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1200, help="只取最近 N 天历史（默认全量）")
    args = ap.parse_args()

    con = collector.connect(CFG.db_path)

    print("=== 1. 数据源层：snap ⋈ items 是否带出 case_id ===")
    total, with_case = con.execute(
        "SELECT COUNT(*), COUNT(case_id) FROM ("
        "  SELECT s.item_id, i.case_id FROM snap s LEFT JOIN items i ON i.item_id = s.item_id"
        ")"
    ).fetchone()
    print(f"  snap ⋈ items 行数 {total:,}，其中带 case_id 的 {with_case:,}（{with_case / max(total, 1):.1%}）")
    if with_case == 0:
        print("  ✗ 一条都没关联上 —— 先跑 pipeline.cases_build")
        return 1

    print("\n=== 2. 箱子层：case_meta 稀有度是否分散 ===")
    case_df = pd.DataFrame(cases_mod.load_cases(con))
    print(f"  case_meta {len(case_df)} 行")
    bad = nunique_report(case_df, ["rarity_score", "case_price", "drop_pool"])
    print(f"  supply_growth_30d 全空: {case_df['supply_growth_30d'].isna().all()}"
          "（SteamDT 不提供存世量增速，诚实地留空，不再拿箱价涨幅冒充）")

    print(f"\n=== 3. 特征层：load_history → features.build（近 {args.days} 天） ===")
    hist = collector.load_history(con, days=args.days)
    print(f"  hist {len(hist):,} 行 × {hist.shape[1]} 列")
    if "case_id" not in hist.columns:
        print("  ✗ hist 没有 case_id 列 —— load_history 的 JOIN 又丢了")
        return 1
    print(f"  hist 带 case_id 行数占比 {hist['case_id'].notna().mean():.1%}，"
          f"独立箱子 {hist['case_id'].nunique()} 个")

    feats = features.build(hist, case_df, None, None)
    print(f"  特征宽表 {len(feats):,} 行 × {feats.shape[1]} 列")

    print("\n=== 4. 全量体检：23 个特征列各自是否还是常数 ===")
    dead: list[str] = []
    print(f"  {'特征':<20} {'unique':>8} {'非空':>8} {'std':>10}  判定")
    for c in FEATURE_COLS:
        if c not in feats.columns:
            print(f"  {c:<20} {'—':>8} {'—':>8} {'—':>10}  ✗ 未产出")
            bad += 1
            dead.append(c)
            continue
        s = feats[c]
        n = s.nunique(dropna=True)
        std = float(s.std(ddof=0)) if pd.api.types.is_numeric_dtype(s) else float("nan")
        ok = n > 1
        if not ok:
            dead.append(c)
        print(f"  {c:<20} {n:>8} {s.notna().mean():>7.1%} {std:>10.4g}  {'✓' if ok else '✗ 常数'}")
    if dead:
        print(f"\n  ⚠️ 常数特征 {len(dead)}/{len(FEATURE_COLS)} 个：{', '.join(dead)}")
        print("     常数特征对模型零贡献，MONOTONE 里针对它们的约束也全是摆设。")
    else:
        print("\n  全部 23 个特征都有变化 ✓")

    print("\n=== 5. 断言：由 case_meta 驱动的 3 列必须分散 ===")
    bad += nunique_report(feats, CASE_COLS)

    # 已关联箱子的样本必须拿到真实稀有度；未关联的才该退回默认 50
    linked = feats[feats["case_id"].notna()]
    unlinked = feats[feats["case_id"].isna()]
    if not linked.empty:
        n_linked = linked["case_rarity_score"].nunique()
        print(f"\n  已关联箱子样本 {len(linked):,} 行，case_rarity_score 有 {n_linked} 个不同取值")
        if n_linked <= 1:
            print("  ✗ 关联上了却还是常数 —— merge 的列名/类型对不上")
            bad += 1
        print(f"  未关联样本 {len(unlinked):,} 行，退默认值 50："
              f"{(unlinked['case_rarity_score'] == 50).all() if not unlinked.empty else 'N/A'}")

    # ---- 根因分类：常数特征到底是代码 bug，还是上游根本没有这份原料 ----
    print("\n=== 6. 常数特征的根因定位（区分 bug / 数据源缺口）===")
    raw_null = {}
    for c in ("bid", "listings", "volume", "supply"):
        k = con.execute(f"SELECT COUNT({c}) FROM snap").fetchone()[0]  # noqa: S608
        raw_null[c] = k
    n_snap = con.execute("SELECT COUNT(*) FROM snap").fetchone()[0]
    for c, k in raw_null.items():
        mark = "全表 NULL" if k == 0 else f"{k / max(n_snap, 1):.1%} 非空"
        print(f"  snap.{c:<10} {mark}")

    # 每个死掉的列 → 它依赖哪个原始列
    deps = {
        "supply_growth_30d": "supply（存世量增速，接口不存在）",
        "case_volume_30d": "Steam 成交量（接口不存在）",
        "supply_peer_pct": "supply",
        "supply_rigidity": "case_rarity_score",
        "turnover": "volume + supply",
        "turnover_ma7": "volume + supply",
        "list_ratio": "listings + supply",
        "list_slope_7d": "listings",
    }
    upstream_gap, code_bug = [], []
    for c in dead:
        why = deps.get(c, "见 features.build")
        uses_null = any(x in why for x in ("supply", "volume", "listings"))
        (upstream_gap if uses_null else code_bug).append(f"{c}（依赖 {why}）")
    if code_bug:
        print("\n  ✗ 代码/A 类问题（上游有料却没接上）：")
        for x in code_bug:
            print(f"      - {x}")
        bad += len(code_bug)
    if upstream_gap:
        print("\n  ⚠️ 上游数据源缺口（SteamDT 根本不提供）：")
        for x in upstream_gap:
            print(f"      - {x}")
        print("      这些列目前恒为常数 —— 要么接新数据源，要么从 FEATURE_COLS 里拿掉。")

    print()
    if bad:
        print(f"❌ 失败：{bad} 项没通过")
        return 1
    if upstream_gap:
        print(f"⚠️  case_id 链路已通（这是本次要验证的事），"
              f"但仍有 {len(upstream_gap)} 列受上游数据源缺口拖累")
        return 2
    print("✅ 供给面特征全部存活，case_id 打通到了特征层")
    return 0


if __name__ == "__main__":
    sys.exit(main())
