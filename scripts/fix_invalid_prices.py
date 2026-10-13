"""一次性修复历史脏价格：把 snap 里非正的 sell / bid 置空。

为什么必须有这一步：
    写入端的校验是这一轮才加的（collector._valid_price）。而在那之前
    sellPrice=0 / 负数 / NaN 都能直接入库，实测库里已经有 **1,552 行 sell <= 0**。
    而 load_history() 原先只 dropna（去掉 NULL），**不去掉 0** ——
    一行「0 元报价」在特征里就是一根 −100% 的暴跌，会污染标签和训练集。

为什么置空而不是删除整行：
    这些行可能同时带着有效的挂单量（listings）—— 那是流动性闸门的输入。
    置空价格既能让它们从价格序列里消失（load_history 会 dropna），
    又不丢掉挂单量信息。真要删也要先确认 listings 为空。

用法（服务器）：
    .venv/bin/python ../scripts/fix_invalid_prices.py            # 只统计，不改
    .venv/bin/python ../scripts/fix_invalid_prices.py --apply    # 执行修复
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from pipeline import collector  # noqa: E402
from pipeline.config import CFG  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的执行修复（默认只统计）")
    args = ap.parse_args()

    con = collector.connect(CFG.db_path)

    total = con.execute("SELECT COUNT(*) FROM snap").fetchone()[0]
    print(f"snap 总行数 {total:,}")
    print()
    checks = [
        ("sell IS NOT NULL AND sell <= 0", "非正价格（应置空）"),
        ("bid IS NOT NULL AND bid <= 0", "非正求购价（应置空）"),
        ("listings IS NOT NULL AND listings < 0", "负数挂单量（应置空）"),
    ]
    print(f"{'问题':<24}{'行数':>12}")
    for cond, label in checks:
        n = con.execute(f"SELECT COUNT(*) FROM snap WHERE {cond}").fetchone()[0]  # noqa: S608
        print(f"{label:<24}{n:>12,}")

    zero_with_listings = con.execute(
        "SELECT COUNT(*) FROM snap WHERE sell IS NOT NULL AND sell <= 0 "
        "AND listings IS NOT NULL"
    ).fetchone()[0]
    print(f"\n其中「非正价格但同时有挂单量」的行：{zero_with_listings:,}"
          "（这些置空价格后可保留挂单量数据）")

    if not args.apply:
        print("\n（这是预演。加 --apply 才会真的改库）")
        return 0

    n1 = con.execute("UPDATE snap SET sell = NULL WHERE sell IS NOT NULL AND sell <= 0").rowcount
    n2 = con.execute("UPDATE snap SET bid = NULL WHERE bid IS NOT NULL AND bid <= 0").rowcount
    n3 = con.execute("UPDATE snap SET listings = NULL "
                     "WHERE listings IS NOT NULL AND listings < 0").rowcount
    con.commit()
    print(f"\n已修复：sell 置空 {n1:,} 行，bid 置空 {n2:,} 行，listings 置空 {n3:,} 行")
    left = con.execute("SELECT COUNT(*) FROM snap WHERE sell IS NOT NULL AND sell <= 0").fetchone()[0]
    print(f"复查：剩余非正价格 {left} 行")
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
