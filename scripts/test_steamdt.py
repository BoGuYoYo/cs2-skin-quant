#!/usr/bin/env python3
"""SteamDT API 连通性与鉴权自检。

先用轻量接口（price/single，60 次/分钟）验证 key，
确认后再决定要不要消耗每日仅 1 次的 /base 配额。

用法：
    python scripts/test_steamdt.py            # 只测轻量接口
    python scripts/test_steamdt.py --base     # 额外调用 /base（会消耗当天配额）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

BASE = "https://open.steamdt.com"


def load_env() -> str:
    """显式指定 .env 路径 —— dotenv 的 find_dotenv() 在 stdin 执行时会断言失败"""
    root = Path(__file__).resolve().parent.parent
    for p in (root / "server" / ".env", root / ".env"):
        if not p.exists():
            continue
        try:
            from dotenv import load_dotenv
            load_dotenv(p, override=False)
        except ImportError:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        print(f"  已加载配置: {p}")
        break
    return os.environ.get("STEAMDT_KEY", "").strip()


def get(path: str, key: str, **params):
    r = requests.get(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {key}"},
        params=params or None,
        timeout=45,
    )
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"_raw": r.text[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", action="store_true", help="额外调用 /base（每天仅 1 次配额）")
    ap.add_argument("--name", default="AK-47 | Redline (Field-Tested)")
    args = ap.parse_args()

    print("=== 载入配置 ===")
    key = load_env()
    if not key:
        print("  ✗ STEAMDT_KEY 为空")
        return 2
    print(f"  key: {key[:8]}…{key[-4:]}（{len(key)} 字符）")

    ok = True

    # ---------------------------------------------------------- 轻量接口
    print("\n=== [1] /price/single（轻量，60 次/分钟）===")
    print(f"  查询: {args.name}")
    code, j = get("/open/cs2/v1/price/single", key, marketHashName=args.name)
    print(f"  HTTP {code}")
    if code != 200:
        print(f"  响应: {json.dumps(j, ensure_ascii=False)[:300]}")
        ok = False
    elif not j.get("success"):
        print(f"  ✗ 鉴权或参数失败: errorCode={j.get('errorCode')} errorMsg={j.get('errorMsg')!r}")
        if j.get("errorCode") in (401, 403):
            print("     → key 无效或未开通，去 open.steamdt.com 个人中心 → API 管理 确认")
        ok = False
    else:
        rows = j.get("data") or []
        print(f"  ✓ 成功，返回 {len(rows)} 个平台报价")
        for p in rows[:12]:
            sell = p.get("sellPrice")
            bid = p.get("biddingPrice")
            cnt = p.get("sellCount")
            print(f"    {str(p.get('platform')):<10} 在售 {sell!s:>10}  求购 {bid!s:>10}  在售量 {cnt}")
        if not rows:
            print("    （该饰品在监控平台暂无报价，换个名字试试）")

    # ---------------------------------------------------------- base（重配额）
    if args.base:
        print("\n=== [2] /base（每天仅 1 次配额，谨慎）===")
        code2, j2 = get("/open/cs2/v1/base", key)
        print(f"  HTTP {code2}")
        if code2 != 200 or not j2.get("success"):
            print(f"  ✗ errorCode={j2.get('errorCode')} errorMsg={j2.get('errorMsg')!r}")
            ok = False
        else:
            data = j2.get("data") or []
            print(f"  ✓ 饰品总数: {len(data)}")
            for it in data[:3]:
                plats = it.get("platformList") or []
                names = [p.get("name") for p in plats]
                print(f"    - {it.get('name')}  [{it.get('marketHashName')}]")
                print(f"      平台: {', '.join(str(n) for n in names)}")
            # 顺手缓存到本地，避免重复消耗配额
            cache = Path(__file__).resolve().parent.parent / "server" / "state" / "base_cache.json"
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            print(f"  已缓存到 {cache}（{cache.stat().st_size / 1024:.0f} KB）")
    else:
        print("\n（未调用 /base，跳过。需要建立 items 表时加 --base）")

    print("\n" + "=" * 40)
    print("结论：" + ("key 可用 ✓" if ok else "存在问题 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
