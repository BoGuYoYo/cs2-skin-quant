"""域筛选落地核查：新规则到底排掉了多少品种。

用户规则（2026-10-11）：纪念品不看、StatTrak 不看。
这个脚本告诉你排掉了谁、还剩多少，避免「规则写了但没生效」这类静默失败。

用法（服务器）：cd /opt/skin-alpha/server && .venv/bin/python ../scripts/diag_domain.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from pipeline import collector  # noqa: E402
from pipeline.config import CFG, DOMAIN_EXCLUDE_SQL  # noqa: E402

con = collector.connect(CFG.db_path)

total = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
print(f"items 全目录            : {total:,}")

# 同一个 WHERE 去掉域筛选 = 旧口径
old_sql = collector.WATCHLIST_SQL.replace(DOMAIN_EXCLUDE_SQL, "")
old = [r[0] for r in con.execute(old_sql).fetchall()]
new = collector.load_watchlist(con)

from pipeline.config import DOMAIN_EXCLUDE_WEAPONS  # noqa: E402

print(f"旧监控池（无域筛选）      : {len(old):,}")
print(f"新监控池（纪念品/ST/点名枪械排除）: {len(new):,}   "
      f"排掉 {len(old) - len(new):,} 个（{(len(old) - len(new)) / max(len(old), 1):.1%}）")
print(f"  其中点名排除的枪械: {'、'.join(DOMAIN_EXCLUDE_WEAPONS)}")

print("\n按类别看排掉了什么：")
for label, pat in (("纪念品 Souvenir", "%Souvenir%"), ("StatTrak™", "%StatTrak%")):
    n = con.execute(
        "SELECT COUNT(*) FROM items WHERE market_hash_name LIKE ?", (pat,)
    ).fetchone()[0]
    in_old = sum(1 for x in old if pat.strip("%").lower() in x.lower())
    print(f"  {label:<18} 全目录 {n:>7,}   旧监控池里 {in_old:>7,}")

print("\n点名枪械在旧池里的数量（= 本轮又排掉的）：")
for w in DOMAIN_EXCLUDE_WEAPONS:
    n = sum(1 for x in old if x.startswith(f"{w} | ") or f" {w} | " in x)
    print(f"  {w:<16}{n:>6}")

print("\n样本（旧池有、新池没有的前 8 个）：")
removed = [x for x in old if x not in set(new)]
for x in removed[:8]:
    print(f"  - {x}")
if len(removed) > 8:
    print(f"  ... 共 {len(removed):,} 个")
