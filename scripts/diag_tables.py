"""数据库盘点：所有表、行数、以及事件表为什么没喂进特征。

用法（服务器）：cd /opt/skin-alpha/server && .venv/bin/python ../scripts/diag_tables.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

db = Path(sys.argv[1] if len(sys.argv) > 1 else "state/skins.db")
con = sqlite3.connect(str(db))
print(f"库：{db.resolve()}  ({db.stat().st_size / 1024 ** 2:.0f} MB)")

tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
print(f"\n{'表名':<22} {'行数':>12}")
for t in tables:
    try:
        n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
    except Exception as e:
        n = f"err:{e}"
    print(f"{t:<22} {n:>12,}" if isinstance(n, int) else f"{t:<22} {n}")

for t in ("events", "market_index"):
    if t not in tables:
        print(f"\n✗ 没有 {t} 表 —— features.build 里事件/大盘特征必然全为 0")
        continue
    rows = con.execute(f"SELECT * FROM {t} LIMIT 5").fetchall()  # noqa: S608
    cols = [d[0] for d in con.execute(f"SELECT * FROM {t} LIMIT 1").description]  # noqa: S608
    print(f"\n=== {t} 列：{cols} ===")
    for r in rows:
        print(f"    {r}")
