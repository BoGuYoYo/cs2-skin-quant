#!/usr/bin/env python3
"""核对线上 D1 的数据：各表行数、候选视图抽样、管线健康度。

用法：
    # 用环境变量（推荐在服务器/CI 上）
    export CF_ACCOUNT_ID=xxx
    export CF_D1_DATABASE_ID=xxx
    export CF_API_TOKEN=xxx
    python scripts/verify_d1.py

    # 或直接内联
    python scripts/verify_d1.py --account xxx --database xxx --token xxx
"""
from __future__ import annotations

import argparse
import os
import sys

import requests

TABLES = [
    "items", "case_meta", "snap_daily", "kline", "forecast",
    "factor_latest", "alerts", "market_index", "backtest", "meta", "runs",
    "task_progress",
]

API = "https://api.cloudflare.com/client/v4/accounts/{acct}/d1/database/{db}/query"


def run(base: str, headers: dict, sql: str):
    r = requests.post(base, headers=headers, json={"sql": sql}, timeout=45)
    try:
        data = r.json()
    except Exception:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    if not data.get("success"):
        errs = data.get("errors") or []
        return None, "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errs) or f"HTTP {r.status_code}"
    res = data.get("result") or []
    if res and not res[0].get("success", True):
        return None, str(res[0].get("error"))
    return (res[0].get("results") if res else []) or [], None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default=os.environ.get("CF_ACCOUNT_ID", "").strip())
    ap.add_argument("--database", default=os.environ.get("CF_D1_DATABASE_ID", "").strip())
    ap.add_argument("--token", default=os.environ.get("CF_API_TOKEN", "").strip())
    args = ap.parse_args()

    missing = [n for n, v in (("CF_ACCOUNT_ID", args.account),
                              ("CF_D1_DATABASE_ID", args.database),
                              ("CF_API_TOKEN", args.token)) if not v]
    if missing:
        print(f"缺少: {', '.join(missing)}")
        return 2

    base = API.format(acct=args.account, db=args.database)
    headers = {"Authorization": f"Bearer {args.token}"}

    print(f"\n目标 D1: {args.database}")
    print("=" * 46)

    print("\n[各表行数]")
    total = 0
    empty: list[str] = []
    for t in TABLES:
        rows, err = run(base, headers, f"SELECT COUNT(*) AS n FROM {t}")
        if err:
            print(f"  {t:<15} ERR  {err}")
            continue
        n = rows[0]["n"] if rows else 0
        total += n
        flag = "" if n else "   ← 空"
        if not n:
            empty.append(t)
        print(f"  {t:<15} {n:>7,}{flag}")
    print(f"  {'合计':<13} {total:>9,}")

    print("\n[管线健康度]")
    rows, err = run(base, headers, "SELECT MAX(date) AS d, COUNT(*) AS n FROM forecast")
    if err:
        print(f"  最新预测日期: ERR {err}")
    else:
        d = rows[0]["d"] if rows else None
        print(f"  最新预测日期: {d}  （{rows[0]['n'] if rows else 0} 条）")
        if d:
            from datetime import date
            gap = (date.today() - date.fromisoformat(d)).days
            tag = "✓ 新鲜" if gap <= 1 else ("⚠ 滞后" if gap < 7 else "✗ 停摆")
            print(f"  距今         : {gap} 天  {tag}")

    rows, err = run(base, headers, "SELECT ts, stage, ok, rows, message FROM runs ORDER BY ts DESC LIMIT 5")
    if not err and rows:
        print("\n[最近运行记录]")
        from datetime import datetime
        for r in rows:
            ts = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S")
            print(f"  {ts}  {r['stage']:<18} ok={r['ok']} rows={r['rows']}  {r['message'] or ''}")
    elif not err:
        print("\n[最近运行记录] （空）")

    print("\n[候选池抽样]")
    rows, err = run(base, headers,
                    "SELECT name, case_name, price, drop_duration_days, q10_adj, score, side "
                    "FROM candidate_view ORDER BY score DESC LIMIT 5")
    if err:
        print(f"  ERR {err}")
    elif not rows:
        print("  （无候选）")
    else:
        for r in rows:
            price = r["price"] if r["price"] is not None else 0
            q10 = (r["q10_adj"] or 0) * 100
            print(f"  {(r['name'] or '?')[:32]:<34} ¥{price:>8.2f}  "
                  f"{r['drop_duration_days'] or 0:>3}天  P10 {q10:>+6.1f}%  {r['side']}")

    print("\n[告警抽样]")
    rows, err = run(base, headers,
                    "SELECT level, name, detail FROM alerts ORDER BY ts DESC LIMIT 5")
    if err:
        print(f"  ERR {err}")
    elif not rows:
        print("  （无告警）")
    else:
        for r in rows:
            print(f"  [{r['level']}] {(r['name'] or '?')[:30]:<32} {r['detail'] or ''}")

    # 结论
    print("\n" + "=" * 46)
    if empty:
        print(f"空表: {', '.join(empty)}")
    if total == 0:
        print("结论：线上库是空的 —— 管线还没推送过数据。")
        return 1
    print(f"结论：共 {total:,} 行，数据结构正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
