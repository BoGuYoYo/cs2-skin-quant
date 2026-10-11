"""写 NOT LIKE 之前先确认命名变体 —— 防止「规则写了但没生效」。

CS2 的 market_hash_name 前缀不止一种：
    普通        "Negev | Power Loader (Factory New)"
    StatTrak™  "StatTrak™ Negev | ..."
    纪念品      "Souvenir Negev | ..."
    纯正        "Genuine P250 | ..."
纪念品/StatTrak 我们已经整体排除，但**其他前缀**（Genuine 等）会让
"NOT LIKE 'Negev | %'" 这种前缀匹配漏掉一部分 —— 那就成了静默失效。

用法（服务器）：cd /opt/skin-alpha/server && .venv/bin/python ../scripts/diag_weapon_variants.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from pipeline import collector  # noqa: E402
from pipeline.config import CFG  # noqa: E402

WEAPONS = ("Negev", "M249", "Dual Berettas", "CZ75-Auto", "SG 553", "SSG 08",
           "R8 Revolver", "XM1014", "Nova", "MAG-7", "Sawed-Off", "PP-Bizon",
           "SCAR-20", "G3SG1", "AUG", "P2000", "Five-SeveN", "Tec-9", "P250",
           "Galil AR", "FAMAS", "MP7", "MP9", "P90", "UMP-45", "MAC-10")

con = collector.connect(CFG.db_path)

print("每种枪的命名前缀分布（只想留一种就够，多了说明会漏）：")
print(f"{'武器':<16}{'总计':>6}  前缀形态")
print("-" * 76)
for w in WEAPONS:
    rows = con.execute(
        "SELECT market_hash_name FROM items WHERE market_hash_name LIKE ?", (f"%{w} | %",)
    ).fetchall()
    prefixes: dict[str, int] = {}
    for (mhn,) in rows:
        head = mhn.split(" | ")[0]
        # 前缀 = 枪名之前的那部分
        idx = head.find(w)
        pre = head[:idx] if idx >= 0 else "(?)"
        prefixes[pre] = prefixes.get(pre, 0) + 1
    disp = "、".join(f"'{p}'×{n}" for p, n in sorted(prefixes.items(), key=lambda x: -x[1]))
    print(f"{w:<16}{len(rows):>6}  {disp}")

print("\n监控池里（已排除纪念品/StatTrak）这些枪还剩多少 —— 也就是前缀匹配需要覆盖的量：")
pool = collector.load_watchlist(con)
for w in WEAPONS:
    n = sum(1 for x in pool if f"| {w} |" in x or x.startswith(f"{w} | ") or f" {w} | " in x)
    print(f"  {w:<16}{n:>5}")
