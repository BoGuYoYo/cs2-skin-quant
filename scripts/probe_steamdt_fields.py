#!/usr/bin/env python3
"""探明 SteamDT 各接口到底提供哪些字段 —— 决定供给面特征还能不能救活。

要回答的问题：
    1) kline 每行到底是几个字段？除了 [ts,o,c,h,l] 还有没有成交量？
    2) /price/single 的 dataList 里有哪些字段（在售量 sellCount 有没有）？
    3) 有没有任何接口能给「存世量 / 在售挂单量 / 日成交量」这三样原料？

用法（服务器）：
    .venv/bin/python ../scripts/probe_steamdt_fields.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

SERVER = Path("/opt/skin-alpha/server")
if not SERVER.exists():
    SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

BASE = "https://open.steamdt.com"


def load_key() -> str:
    env = SERVER / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("STEAMDT_KEY="):
                return line.strip().split("=", 1)[1].strip()
    return os.environ.get("STEAMDT_KEY", "").strip()


def main() -> int:
    key = load_key()
    if not key:
        print("✗ 没有 STEAMDT_KEY")
        return 2
    s = requests.Session()
    hdr = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    name = "AK-47 | Redline (Field-Tested)"

    print("=== [1] kline：一行到底几个字段 ===")
    r = s.post(f"{BASE}/open/cs2/item/v1/kline",
               headers=hdr,
               json={"marketHashName": name, "type": 2, "platform": "BUFF"},
               timeout=45)
    print(f"  HTTP {r.status_code}")
    j = r.json()
    print(f"  success={j.get('success')} errorMsg={j.get('errorMsg')!r}")
    data = j.get("data") or []
    print(f"  行数 {len(data)}")
    if data:
        row = data[-1]
        print(f"  最后一行: {row}  （{len(row)} 个字段）")
        print(f"  第一行:   {data[0]}")
    print(f"  顶层 keys: {list(j.keys())}")

    print("\n=== [2] price/single：dataList 字段全貌 ===")
    r2 = s.get(f"{BASE}/open/cs2/v1/price/single",
               headers={"Authorization": f"Bearer {key}"},
               params={"marketHashName": name},
               timeout=30)
    print(f"  HTTP {r2.status_code}")
    j2 = r2.json()
    print(f"  顶层 keys: {list(j2.keys())}")
    for p in (j2.get("data") or [])[:6]:
        print(f"    platform={p.get('platform')}")
        for k, v in p.items():
            print(f"       {k:<18} = {v!r}")
        break
    if j2.get("data"):
        print("  所有平台共用字段: " + ", ".join(sorted(j2["data"][0].keys())))
        print("  各平台顺序: " + ", ".join(str(p.get("platform")) for p in j2["data"]))

    print("\n=== [3] 试探是否存在「存世量/在售量」类接口 ===")
    candidates = [
        ("GET", "/open/cs2/item/v1/detail", {"marketHashName": name}),
        ("GET", "/open/cs2/item/v1/info", {"marketHashName": name}),
        ("GET", "/open/cs2/v1/item/detail", {"marketHashName": name}),
        ("GET", "/open/cs2/v1/stat/supply", {"marketHashName": name}),
        ("POST", "/open/cs2/v1/price/batch", None),
    ]
    for method, path, params in candidates:
        try:
            if method == "GET":
                rr = s.get(BASE + path, headers={"Authorization": f"Bearer {key}"},
                           params=params, timeout=15)
            else:
                rr = s.post(BASE + path, headers=hdr,
                            json={"marketHashNames": [name]}, timeout=30)
            body = rr.text[:200].replace("\n", " ")
            print(f"  {method} {path:<34} HTTP {rr.status_code}  {body}")
        except Exception as e:
            print(f"  {method} {path:<34} ✗ {type(e).__name__}: {str(e)[:60]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
