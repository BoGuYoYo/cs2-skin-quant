#!/usr/bin/env python3
"""测试 SteamDT 的 K 线接口 —— 能直接拉历史的话，21 天冷启动就不用等了。

接口：POST /open/cs2/item/v1/kline   权限：每分钟 120 次
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

BASE = "https://open.steamdt.com"
NAME = "AK-47 | Redline (Field-Tested)"


def load_key() -> str:
    root = Path(__file__).resolve().parent.parent
    env = root / "server" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("STEAMDT_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("STEAMDT_KEY", "").strip()


def probe(key: str, payload: dict, label: str) -> dict | None:
    try:
        r = requests.post(
            f"{BASE}/open/cs2/item/v1/kline",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=45,
        )
    except Exception as e:
        print(f"  [{label}] 请求异常: {e}")
        return None

    print(f"  [{label}] HTTP {r.status_code}  payload={json.dumps(payload, ensure_ascii=False)}")
    if r.status_code != 200:
        print(f"    响应: {r.text[:200]}")
        return None
    try:
        j = r.json()
    except Exception:
        print(f"    非 JSON: {r.text[:200]}")
        return None
    if not j.get("success"):
        print(f"    ✗ errorCode={j.get('errorCode')} errorMsg={j.get('errorMsg')!r}")
        return None
    data = j.get("data")
    if isinstance(data, list):
        print(f"    ✓ 返回 {len(data)} 条")
        for row in data[:3]:
            print(f"      {json.dumps(row, ensure_ascii=False)[:180]}")
        if len(data) > 3:
            print(f"      … 最后一条: {json.dumps(data[-1], ensure_ascii=False)[:180]}")
    else:
        print(f"    ✓ 返回: {json.dumps(data, ensure_ascii=False)[:400]}")
    return j


def main() -> int:
    key = load_key()
    if not key:
        print("缺少 STEAMDT_KEY")
        return 2
    print(f"key: {key[:8]}…（{len(key)} 字符）\n")

    print("=== 探测参数组合（type 的含义未知，逐个试）===")
    ok_payload = None
    for typ in (1, 2, 3, 4, 5, 6, 7):
        j = probe(key, {"marketHashName": NAME, "type": typ, "platform": "BUFF"}, f"type={typ}")
        if j:
            d = j.get("data")
            if isinstance(d, list) and len(d) > 5:
                print(f"    ↑ type={typ} 返回了 {len(d)} 条，看起来是 K 线")
                ok_payload = {"marketHashName": NAME, "type": typ, "platform": "BUFF"}
                break
        import time
        time.sleep(0.5)   # 每分钟 120 次，不用太慢

    if not ok_payload:
        print("\n所有 type 都没返回有效 K 线，试 platform 变体…")
        for plat in ("BUFF", "STEAM", "YOUPIN", "C5"):
            j = probe(key, {"marketHashName": NAME, "type": 1, "platform": plat}, f"platform={plat}")
            if j and isinstance(j.get("data"), list) and len(j["data"]) > 5:
                ok_payload = {"marketHashName": NAME, "type": 1, "platform": plat}
                break

    print("\n" + "=" * 50)
    if ok_payload:
        print(f"✓ 找到可用参数组合: {json.dumps(ok_payload, ensure_ascii=False)}")
        print("  → 可以用它拉历史 K 线，跳过 21 天冷启动")
    else:
        print("✗ 未找到可用组合，需要再看文档参数说明")
    return 0


if __name__ == "__main__":
    sys.exit(main())
