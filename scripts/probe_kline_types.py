#!/usr/bin/env python3
"""探测 SteamDT K 线接口的全部周期（type 参数）。

接口：POST /open/cs2/item/v1/kline   权限：每分钟 120 次，一次请求返回整段历史
用途：找到日线周期后，可以一次性拉完所有品种的历史，跳过 21 天冷启动。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://open.steamdt.com"
NAME = "AK-47 | Redline (Field-Tested)"


def load_key() -> str:
    root = Path(__file__).resolve().parent.parent
    env = root / "server" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("STEAMDT_KEY="):
                return line.strip().split("=", 1)[1]
    return os.environ.get("STEAMDT_KEY", "").strip()


def interval_label(sec: int) -> str:
    if sec % 86400 == 0 and sec >= 86400:
        return f"{sec // 86400} 天"
    if sec % 3600 == 0:
        return f"{sec // 3600} 小时"
    if sec % 60 == 0:
        return f"{sec // 60} 分钟"
    return f"{sec} 秒"


def main() -> int:
    key = load_key()
    if not key:
        print("缺少 STEAMDT_KEY")
        return 2

    print(f"探测 K 线 type 参数（样本：{NAME}）\n")
    print(f"{'type':<6}{'条数':>7}{'间隔':>10}{'覆盖时长':>12}  首/末时间")
    print("-" * 74)

    found: dict[int, dict] = {}
    for typ in range(1, 11):
        try:
            r = requests.post(
                f"{BASE}/open/cs2/item/v1/kline",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"marketHashName": NAME, "type": typ, "platform": "BUFF"},
                timeout=45,
            )
            j = r.json()
        except Exception as e:
            print(f"{typ:<6}{'ERR':>7}  {e}")
            time.sleep(0.4)
            continue

        if not j.get("success"):
            print(f"{typ:<6}{'失败':>7}  errorCode={j.get('errorCode')} {j.get('errorMsg')!r}")
            time.sleep(0.4)
            continue

        data = j.get("data") or []
        if not isinstance(data, list) or len(data) < 2:
            print(f"{typ:<6}{len(data) if isinstance(data, list) else '?':>7}  （数据不足）")
            time.sleep(0.4)
            continue

        try:
            t0, t1 = int(data[0][0]), int(data[1][0])
            iv = t1 - t0
            span = int(data[-1][0]) - int(data[0][0])
            f = datetime.fromtimestamp(int(data[0][0]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            l = datetime.fromtimestamp(int(data[-1][0]), tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"{typ:<6}{len(data):>7}{interval_label(iv):>10}{interval_label(span):>12}  {f} → {l}")
            found[typ] = {"n": len(data), "interval": iv, "span": span}
        except Exception as e:
            print(f"{typ:<6}{len(data):>7}  解析失败: {e}  样例={data[0]}")
        time.sleep(0.4)

    print("-" * 74)
    daily = [t for t, v in found.items() if v["interval"] >= 86400]
    hourly = [t for t, v in found.items() if 3600 <= v["interval"] < 86400]

    print("\n结论：")
    if daily:
        t = daily[0]
        print(f"  ✓ 日线周期 = type {t}（{found[t]['n']} 条，覆盖 {interval_label(found[t]['span'])}）")
        print(f"    → 19137 个品种 × 1 次请求 ÷ 120 每分钟 ≈ {19137 / 120:.0f} 分钟拉完全部历史")
    if hourly:
        t = hourly[0]
        print(f"  ✓ 小时线周期 = type {t}（{found[t]['n']} 条，覆盖 {interval_label(found[t]['span'])}）")
        print(f"    → 小时线可以聚合成日线，同样能跳过冷启动")
    if not daily and not hourly:
        print("  ✗ 未识别出日线/小时线，请人工看上面的表格")

    out = Path(__file__).resolve().parent.parent / "server" / "state" / "kline_types.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n结果已存 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
