#!/usr/bin/env python3
"""诊断 K 线回填失败的原因：逐品种打印结果，并统计失败模式。

用法：python scripts/diag_kline.py --n 40
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

import requests

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from pipeline.backfill import fetch_kline, reload_names  # noqa: E402


def load_key() -> str:
    env = SERVER / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("STEAMDT_KEY="):
                return line.strip().split("=", 1)[1]
    return os.environ.get("STEAMDT_KEY", "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--platform", default="BUFF")
    args = ap.parse_args()

    key = load_key()
    con = sqlite3.connect(str(SERVER / "state" / "skins.db"))
    names = reload_names(con)
    sample = names[: args.n]

    print(f"监控池 {len(names)}，抽样 {len(sample)} 个，platform={args.platform}\n")
    sess = requests.Session()
    ok = 0
    reasons: Counter = Counter()
    name_of: dict[str, str] = {}

    for i, name in enumerate(sample, 1):
        meta = con.execute("SELECT name FROM items WHERE market_hash_name = ?", (name,)).fetchone()
        cn = meta[0] if meta else "?"
        name_of[name] = cn
        try:
            rows = fetch_kline(sess, key, name, 2, args.platform)
            if rows:
                ok += 1
                first = rows[0][0] if rows else None
                print(f"  [{i:>3}] ✓ {len(rows):>4} 条   {cn}")
            else:
                reasons["空数据"] += 1
                print(f"  [{i:>3}] ○ 空数据      {cn}   ({name[:50]})")
        except RuntimeError as e:
            msg = str(e)
            reasons[msg[:50]] += 1
            print(f"  [{i:>3}] ✗ {msg[:44]:<44} {cn}")
        except Exception as e:
            reasons[type(e).__name__] += 1
            print(f"  [{i:>3}] ✗ {type(e).__name__}: {str(e)[:40]}")
        time.sleep(0.55)

    print(f"\n成功 {ok}/{len(sample)} = {ok / len(sample) * 100:.0f}%")
    print("失败原因统计：")
    for r, c in reasons.most_common():
        print(f"  {c:>4}  {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
