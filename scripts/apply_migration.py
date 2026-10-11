#!/usr/bin/env python3
"""用 D1 REST API 应用 SQL 迁移 —— 备用方案。

什么时候需要：
    wrangler 的 OAuth 凭据失效时会报：
        The given account is not valid or is not authorized to access this service [code: 7403]
    但只要有 API Token（Account→D1→Edit），就能绕过 wrangler 直接跑 SQL。

用法：
    python scripts/apply_migration.py migrations/0003_progress.sql
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

from push import to_d1  # noqa: E402


def split_statements(sql: str) -> list[str]:
    """按分号拆语句，去掉注释行。够用就好 —— 我们的迁移里没有函数体/触发器。"""
    body = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    parts = [s.strip() for s in body.split(";")]
    return [p for p in parts if p]


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python scripts/apply_migration.py <sql文件>")
        return 2
    path = Path(sys.argv[1])
    if not path.exists():
        print(f"文件不存在: {path}")
        return 2

    stmts = split_statements(path.read_text(encoding="utf-8"))
    print(f"应用 {path.name}：{len(stmts)} 条语句")
    print(f"目标 D1: {to_d1.DATABASE_ID or '(未配置)'}")
    print()

    ok = 0
    for i, s in enumerate(stmts, 1):
        preview = re.sub(r"\s+", " ", s)[:70]
        try:
            to_d1._query(s)
            print(f"  [{i}] ✓ {preview}…")
            ok += 1
        except Exception as e:
            msg = str(e)
            # IF NOT EXISTS 让重复执行是安全的，但索引重名之类还是可能报错
            if "already exists" in msg.lower():
                print(f"  [{i}] ○ 已存在，跳过：{preview}…")
                ok += 1
            else:
                print(f"  [{i}] ✗ {msg[:160]}")
    print(f"\n完成 {ok}/{len(stmts)}")
    return 0 if ok == len(stmts) else 1


if __name__ == "__main__":
    sys.exit(main())
