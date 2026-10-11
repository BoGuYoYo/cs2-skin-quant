#!/usr/bin/env python3
"""测 /price/batch 的真实频率上限 —— 决定「全量补挂单量」要跑多久。

代码里写着「1 次/分钟」，但如果是真的，18853 个品种要 189 分钟；
如果其实是 60 次/分钟，只要 3 分钟。差别很大，值得实测。

只发少量请求，不打爆配额。

用法（服务器）：.venv/bin/python ../scripts/probe_batch_rate.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

SERVER = Path("/opt/skin-alpha/server")
sys.path.insert(0, str(SERVER))


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
    url = "https://open.steamdt.com/open/cs2/v1/price/batch"
    names = ["AK-47 | Redline (Field-Tested)", "AWP | Asiimov (Field-Tested)"]

    print("间隔 2 秒连发 5 次，看第几次开始 429：")
    for i in range(1, 6):
        t0 = time.time()
        try:
            r = s.post(url, headers=hdr, json={"marketHashNames": names}, timeout=30)
            body = r.text[:120].replace("\n", " ")
            print(f"  #{i}  HTTP {r.status_code}  {time.time() - t0:.2f}s  {body}")
            if r.status_code == 429:
                print("  → 确实有严格限流，保持 61s 间隔")
                return 0
        except Exception as e:
            print(f"  #{i}  ✗ {type(e).__name__}: {str(e)[:60]}")
        if i < 5:
            time.sleep(2)

    print("\n→ 连发 5 次都没有 429，说明限流比注释里写的宽松；")
    print("  但仍应收敛在文档配额内：先用 61s 间隔跑一轮，观察日志里的 429 次数再提速。")

    print("\n顺带验证 sellCount 是否随平台差异明显（取 BUFF）：")
    r = s.post(url, headers=hdr, json={"marketHashNames": names}, timeout=30)
    for blk in (r.json().get("data") or []):
        rows = blk.get("dataList") or []
        print(f"  {blk.get('marketHashName')}")
        for p in rows:
            print(f"     {str(p.get('platform')):<10} 在售价 {p.get('sellPrice')!s:>9}  "
                  f"在售量 {p.get('sellCount')!s:>8}  求购价 {p.get('biddingPrice')!s:>9}  "
                  f"求购量 {p.get('biddingCount')!s:>6}")
        print(f"     平台数 {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
