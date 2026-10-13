"""核查 GPT6 审查里几条断言的**真实影响面**（不改数据，只读 + 一次接口调用）。

回答三个问题：
  1) factor_latest 里到底积了多少「本轮不再入选」的旧行？（[P1] 旧因子没清理）
  2) 今天的告警里 L1/L2/L3 各多少条？「先取前 5 条再筛 L3」会漏掉多少？（[P2]）
  3) /price/batch 里 BUFF 缺失的比例有多高？缺了就会回退到别的平台 ——
     也就是 write_daily_snapshot 会因为「混口径」而制造人工跳价。（[P2]）

用法（服务器）：cd /opt/skin-alpha/server && .venv/bin/python ../scripts/diag_review_claims.py
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from pipeline import collector  # noqa: E402
from pipeline.config import CFG  # noqa: E402

con = collector.connect(CFG.db_path)

print("=== [P1] factor_latest 的陈旧行 ===")
total = con.execute("SELECT COUNT(*) FROM factor_latest").fetchone()[0]
rows = con.execute(
    "SELECT date, COUNT(*) FROM factor_latest GROUP BY date ORDER BY date DESC LIMIT 5"
).fetchall()
print(f"  factor_latest 总行数 {total:,}")
for d, n in rows:
    print(f"    date={d}  {n:,} 行")
if rows:
    newest = rows[0][0]
    stale = con.execute(
        "SELECT COUNT(*) FROM factor_latest WHERE date < ?", (newest,)
    ).fetchone()[0]
    print(f"  最新日期 {newest}，比它旧的行（= 本轮没入选、但页面还看得见）: {stale:,}")
    print(f"  按 /api/screener 的查询（只按 price/rarity 过滤，不看日期），"
          f"这些旧行**会**出现在页面上。")

print("\n=== [P2] 告警等级分布（决定「前 5 条」会漏多少）===")
last_ts = con.execute("SELECT MAX(ts) FROM alerts").fetchone()[0]
if last_ts:
    import datetime as _dt
    day0 = int(_dt.datetime.fromtimestamp(last_ts, _dt.timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    cnt = Counter()
    for lv, n in con.execute(
        "SELECT level, COUNT(*) FROM alerts WHERE ts >= ? GROUP BY level", (day0,)
    ).fetchall():
        cnt[lv] = n
    print(f"  最近一批告警: {dict(cnt)}  合计 {sum(cnt.values())}")
    first5 = [r[0] for r in con.execute(
        "SELECT level FROM alerts WHERE ts >= ? ORDER BY id LIMIT 5", (day0,)
    ).fetchall()]
    print(f"  前 5 条（按插入顺序）的等级: {first5}")
    print(f"  => 当前写法只会对其中等级为 L3 的发通知，"
          f"本次实际会发 {sum(1 for x in first5 if x == 'L3')} 条，"
          f"而库里 L3 共 {cnt.get('L3', 0)} 条")

print("\n=== [P2] /price/batch 里 BUFF 的缺失率（回退平台 = 混口径）===")
key = ""
for line in (Path("/opt/skin-alpha/server/.env")).read_text(encoding="utf-8").splitlines():
    if line.strip().startswith("STEAMDT_KEY="):
        key = line.strip().split("=", 1)[1].strip()
if not key:
    key = os.environ.get("STEAMDT_KEY", "")
names = collector.load_watchlist(con)[:100]
if not key or not names:
    print("  跳过（没有 key 或监控池为空）")
else:
    import requests
    try:
        blocks = collector.fetch_prices(names, key, requests.Session(), interval=0, max_retry=2)
    except RuntimeError as e:
        print(f"  调用失败（可能正在被挂单量采集器占用配额）：{e}")
        blocks = []
    if blocks:
        missing = 0
        picked = Counter()
        for blk in blocks:
            data = blk.get("dataList") or []
            has_buff = any(p.get("platform") == "BUFF" for p in data)
            if not has_buff:
                missing += 1
                picked[str(collector.pick_platform(blk).get("platform") if collector.pick_platform(blk) else "None")] += 1
        print(f"  样本 {len(blocks)} 个品种，其中 BUFF 缺失 {missing} 个（{missing / len(blocks):.1%}）")
        if missing:
            print(f"  这些会回退到：{dict(picked)}")
            print("  → 同一列 sell 里混入其他平台的价，正是 pick_platform 注释警告的「人工跳价」")
        else:
            print("  → 本样本里没有触发回退；风险存在但当前未实际发生")
