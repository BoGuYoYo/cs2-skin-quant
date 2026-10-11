#!/usr/bin/env python3
"""探明「日交易量」能不能拿到 —— 用户规则「交易量 <20/天 不看」的前提。

已知：kline 的日线每行 5 个字段 [ts,o,c,h,l]，没有成交量。
要确认的是：
    1) 小时线 / 周线 是不是也 5 个字段？
    2) 换个平台（STEAM / BUFF）会不会多出成交量？
    3) kline 响应的顶层除了 data 还有没有别的数组（比如独立的量序列）？
    4) /price/* 的字段里有没有任何"成交量"字段？

只发少量请求。

用法（服务器）：.venv/bin/python ../scripts/probe_volume_availability.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

SERVER = Path("/opt/skin-alpha/server")
sys.path.insert(0, str(SERVER))
BASE = "https://open.steamdt.com"


def load_key() -> str:
    env = SERVER / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("STEAMDT_KEY="):
            return line.strip().split("=", 1)[1].strip()
    return os.environ.get("STEAMDT_KEY", "").strip()


def main() -> int:
    key = load_key()
    s = requests.Session()
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    name = "AK-47 | Redline (Field-Tested)"

    print("=== kline：三种周期 × 两个平台的字段数 ===")
    for ktype, label in ((1, "小时线"), (2, "日线"), (3, "周线")):
        for plat in ("BUFF", "STEAM"):
            try:
                r = s.post(f"{BASE}/open/cs2/item/v1/kline", headers=hdr,
                           json={"marketHashName": name, "type": ktype, "platform": plat},
                           timeout=45)
                j = r.json()
                data = j.get("data") or []
                n = len(data[0]) if data else 0
                tail = data[-1] if data else None
                print(f"  {label} {plat:<7} 行数 {len(data):>4}  字段数 {n}  最后一行 {tail}")
                print(f"      顶层 keys={list(j.keys())}")
            except Exception as e:
                print(f"  {label} {plat:<7} ✗ {type(e).__name__}: {str(e)[:60]}")
            time.sleep(0.6)

    print("\n=== 响应里有没有别的数组（独立量序列）===")
    r = s.post(f"{BASE}/open/cs2/item/v1/kline", headers=hdr,
               json={"marketHashName": name, "type": 2, "platform": "BUFF"}, timeout=45)
    j = r.json()
    for k, v in j.items():
        kind = type(v).__name__
        n = len(v) if isinstance(v, (list, dict)) else ""
        print(f"  {k:<15} {kind:<8} {n}")

    print("\n=== /price/* 的字段清单 ===")
    r2 = s.get(f"{BASE}/open/cs2/v1/price/single",
               headers={"Authorization": f"Bearer {key}"},
               params={"marketHashName": name}, timeout=30)
    rows = (r2.json().get("data") or [])
    if rows:
        print("  price/single 字段: " + ", ".join(sorted(rows[0].keys())))
    print(f"  平台数 {len(rows)}")
    print("\n  注：sellCount = 在售挂单量，biddingCount = 求购量，"
          "两者都不是成交量。")

    print("\n=== 结论 ===")
    print(json.dumps({
        "kline_有成交量": False,
        "可用字段": ["sellPrice", "sellCount(在售量)", "biddingPrice", "biddingCount(求购量)"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
