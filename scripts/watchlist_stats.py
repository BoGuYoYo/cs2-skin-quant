#!/usr/bin/env python3
"""统计监控池规模与构成（规则改动后用来核对）。"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from pipeline.collector import load_watchlist  # noqa: E402

con = sqlite3.connect(str(SERVER / "state" / "skins.db"))
total = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
wl = load_watchlist(con)

excluded = [
    ("印花/贴纸", "name LIKE '%印花%'"),
    ("涂鸦", "name LIKE '%涂鸦%'"),
    ("挂件", "name LIKE '%挂件%'"),
    ("音乐盒", "name LIKE '%音乐盒%'"),
    ("硬币", "name LIKE '%硬币%'"),
    ("勋章", "name LIKE '%勋章%'"),
    ("大行动之星", "name LIKE '%大行动之星%'"),
    ("胶囊", "name LIKE '%胶囊%'"),
    ("通行证", "name LIKE '%通行证%'"),
    ("# 开头", "market_hash_name LIKE '#%'"),
]

print(f"items 总数      : {total}")
print()
print("被排除的类别：")
for label, cond in excluded:
    n = con.execute(f"SELECT COUNT(*) FROM items WHERE {cond}").fetchone()[0]
    print(f"  {label:<12} {n:>6}")
print()
print(f"监控池（可交易） : {len(wl)}")
print(f"预计回填耗时     : {len(wl) * 0.6 / 60:.0f} 分钟（每品种 0.6s）")
print()
print("监控池样例：")
for n in wl[:10]:
    row = con.execute("SELECT name FROM items WHERE market_hash_name = ?", (n,)).fetchone()
    print(f"  {(row[0] if row else '?')[:36]:<38} {n[:52]}")
