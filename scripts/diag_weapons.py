"""枪械清单：库里到底有哪些武器、各多少款、多少在监控池里。

目的：加「去除某几把枪」这类规则前，先拿到确切命名 ——
market_hash_name 里的武器名形如 "[★ ][StatTrak™ ]WEAPON | SKIN (WEAR)"，
不先看清楚就写 NOT LIKE，很容易误伤（比如 'M4' 会连 M4A1/M4A4 一起打掉）。

用法（服务器）：cd /opt/skin-alpha/server && .venv/bin/python ../scripts/diag_weapons.py
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from pipeline import collector  # noqa: E402
from pipeline.config import CFG  # noqa: E402

con = collector.connect(CFG.db_path)


def weapon_of(mhn: str) -> str:
    s = (mhn or "").strip()
    if s.startswith("★ "):
        s = s[2:]
    head, sep, _ = s.partition(" | ")
    return head if sep else "(非皮肤/探员类)"


rows = con.execute(
    "SELECT market_hash_name, name FROM items WHERE market_hash_name IS NOT NULL"
).fetchall()

cnt: Counter[str] = Counter()
cn: dict[str, Counter[str]] = {}
for mhn, name in rows:
    w = weapon_of(mhn)
    cnt[w] += 1
    cn.setdefault(w, Counter())[(name or "").partition(" | ")[0]] += 1

pool = set(collector.load_watchlist(con))
pool_cnt: Counter[str] = Counter()
for mhn in pool:
    pool_cnt[weapon_of(mhn)] += 1

print(f"items 全目录 {len(rows):,}   监控池 {len(pool):,}")
print(f"\n{'武器（market_hash_name）':<32}{'目录':>7}{'监控池':>8}  中文名")
print("-" * 78)
for w, n in sorted(cnt.items(), key=lambda x: -x[1]):
    if w == "(非皮肤/探员类)":
        continue
    zh = "、".join(x for x, _ in cn[w].most_common(2))
    print(f"{w:<32}{n:>7}{pool_cnt.get(w, 0):>8}  {zh[:32]}")

print(f"\n（另有 {cnt.get('(非皮肤/探员类)', 0):,} 条没有『 | 』分隔，"
      f"多为探员/印花/其他，监控池里 {pool_cnt.get('(非皮肤/探员类)', 0):,} 条）")

print("\n候选剔除项（用户点名 + 同类低流动性枪械）在这库里的确切命名：")
for w in ("Negev", "M249", "Dual Berettas", "CZ75-Auto", "SG 553", "SSG 08",
          "R8 Revolver", "XM1014", "MAG-7", "Sawed-Off", "Nova", "PP-Bizon",
          "MAC-10", "P2000", "Five-SeveN", "Tec-9", "P250", "Galil AR", "FAMAS"):
    print(f"  {w:<18} 目录 {cnt.get(w, 0):>5}   监控池 {pool_cnt.get(w, 0):>5}")

print("\n=== 当前监控池里还剩哪些武器（按款数降序）===")
live = [(w, n) for w, n in pool_cnt.items() if n > 0 and w != "(非皮肤/探员类)"]
for w, n in sorted(live, key=lambda x: -x[1]):
    print(f"  {w:<24}{n:>5}")
print(f"\n  合计 {sum(n for _, n in live):,} 款（另有非皮肤类 {pool_cnt.get('(非皮肤/探员类)', 0):,} 款）")
