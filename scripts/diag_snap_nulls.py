"""诊断 snap 表各列的可用性 —— 供给面/流动性特征到底有没有原料。

背景：verify_supply_features 发现 supply_peer_pct 恒为 0。
根因假设：K 线回填只写 price，volume/supply/listings 全 NULL，
于是 turnover / list_ratio / list_slope_7d / supply_peer_pct 这些
「需求与抛压」特征全都退化成常数 0 —— 和 RARITY≡50 是同一类静默失效。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path("state/skins.db")
if len(sys.argv) > 1:
    DB = Path(sys.argv[1])

con = sqlite3.connect(str(DB))
COLS = ("sell", "bid", "listings", "volume", "supply")

n = con.execute("SELECT COUNT(*) FROM snap").fetchone()[0]
print(f"snap 总行数 {n:,}")
print(f"{'列':<10} {'非空行数':>12} {'占比':>8}")
for c in COLS:
    k = con.execute(f"SELECT COUNT({c}) FROM snap").fetchone()[0]
    print(f"{c:<10} {k:>12,} {k / n:>7.2%}")

print("\n按月份看 supply 非空率：")
rows = con.execute(
    "SELECT substr(date,1,7) AS m, COUNT(*) AS t, COUNT(supply) AS s,"
    "       COUNT(listings) AS l, COUNT(volume) AS v"
    " FROM snap GROUP BY m ORDER BY m"
).fetchall()
print(f"{'月份':<10} {'行数':>10} {'supply':>9} {'listings':>9} {'volume':>9}")
for m, t, s, l, v in rows:
    print(f"{m:<10} {t:>10,} {s / t:>8.2%} {l / t:>8.2%} {v / t:>8.2%}")

print("\nsupply 非空的品种数 / 总品种数：")
tot_items = con.execute("SELECT COUNT(DISTINCT item_id) FROM snap").fetchone()[0]
sup_items = con.execute("SELECT COUNT(DISTINCT item_id) FROM snap WHERE supply IS NOT NULL").fetchone()[0]
print(f"  {sup_items:,} / {tot_items:,} = {sup_items / tot_items:.2%}")

print("\n有 supply 的样本里，同箱同品质内的分位是否分散（抽样 5 个箱子某一天）：")
row = con.execute(
    "SELECT date FROM snap WHERE supply IS NOT NULL ORDER BY date DESC LIMIT 1"
).fetchone()
if row:
    d = row[0]
    q = con.execute(
        "SELECT i.case_name, COUNT(*), COUNT(DISTINCT s.supply) FROM snap s"
        " JOIN items i ON i.item_id = s.item_id"
        " WHERE s.date = ? AND s.supply IS NOT NULL AND i.case_id IS NOT NULL"
        " GROUP BY i.case_name ORDER BY 2 DESC LIMIT 5",
        (d,),
    ).fetchall()
    print(f"  最新有 supply 的日期 {d}")
    for name, cnt, ds in q:
        print(f"    {(name or '')[:40]:<42} 样本 {cnt:>5}  不同 supply 值 {ds}")
else:
    print("  ✗ 全表没有任何 supply 数据")
