#!/usr/bin/env python3
"""把条件式 className 从旧命名映射到 ark 命名。

为什么单独写脚本：PowerShell 的反引号是转义符，会毁掉模板字符串；
单引号里嵌套引号又要写 ''，极易出错。Python 一次搞定。
"""
from __future__ import annotations

import sys
from pathlib import Path

PAGES = Path(__file__).resolve().parent.parent / "web" / "src" / "pages"

REPLACEMENTS = [
    (
        "className={level === lv ? 'primary' : 'ghost'}",
        "className={level === lv ? 'ark-btn is-primary' : 'ark-btn'}",
    ),
    (
        "className={mode === 'pool' ? 'primary' : 'ghost'}",
        "className={mode === 'pool' ? 'ark-btn is-primary' : 'ark-btn'}",
    ),
    (
        "className={mode === 'screener' ? 'primary' : 'ghost'}",
        "className={mode === 'screener' ? 'ark-btn is-primary' : 'ark-btn'}",
    ),
    (
        "className={sort === 'rarity' ? 'primary' : 'ghost'}",
        "className={sort === 'rarity' ? 'ark-btn is-primary' : 'ark-btn'}",
    ),
    (
        "className={sort === 'volume' ? 'primary' : 'ghost'}",
        "className={sort === 'volume' ? 'ark-btn is-primary' : 'ark-btn'}",
    ),
    (
        "className={days === d ? 'primary' : 'ghost'}",
        "className={days === d ? 'ark-btn is-primary' : 'ark-btn'}",
    ),
    # 其余可能残留的简写
    ("className=\"card stat\"", "className=\"ark-panel ark-stat\""),
    ("className=\"card link\"", "className=\"ark-panel\""),
    ("className=\"chart\"", "className=\"chart\""),
]

total = 0
for f in sorted(PAGES.glob("*.tsx")):
    src = f.read_text(encoding="utf-8")
    before = src
    for old, new in REPLACEMENTS:
        if old in src:
            src = src.replace(old, new)
    if src != before:
        f.write_text(src, encoding="utf-8")
        n = sum(1 for o, _ in REPLACEMENTS if o in before)
        print(f"  {f.name:<18} 替换 {n} 处")
        total += n

print(f"\n共 {total} 处")

# 打印仍然存在的旧命名，便于人工确认
import re
leftovers = []
for f in sorted(PAGES.glob("*.tsx")):
    for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(r"""['"](primary|ghost)['"]""", line) or "table-wrap" in line or 'className="card' in line:
            leftovers.append(f"  {f.name}:{i}  {line.strip()[:90]}")
if leftovers:
    print("\n仍需人工处理的残留：")
    print("\n".join(leftovers))
else:
    print("无残留 ✓")

sys.exit(0)
