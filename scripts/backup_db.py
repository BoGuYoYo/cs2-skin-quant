#!/usr/bin/env python3
"""备份本地 SQLite —— 回填 540 万行花了几小时，修 bug 前先留一份。

用 VACUUM INTO 而不是 cp：
  1) 一致性快照（不会拷到写了一半的页）
  2) 自动紧凑化，通常比原库小
  3) 不需要先停掉写入进程

用法：
    python scripts/backup_db.py                    # 备份到 state/skins_backup.db
    python scripts/backup_db.py --out /path/x.db   # 指定路径
    python scripts/backup_db.py --verify-only      # 只校验已有备份
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "server"
DEFAULT_SRC = SERVER / "state" / "skins.db"
DEFAULT_OUT = SERVER / "state" / "skins_backup.db"

TABLES = ["items", "snap", "kline", "case_meta", "forecast", "factor_latest", "alerts", "task_progress"]


def human(n: float) -> str:
    return f"{n / 1024 ** 3:.2f} GB"


def verify(path: Path) -> bool:
    if not path.exists():
        print(f"  ✗ 备份不存在: {path}")
        return False
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        print(f"  integrity_check: {ok}")
        for t in TABLES:
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"  {t:<16} {n:>12,}")
            except sqlite3.OperationalError:
                print(f"  {t:<16} （表不存在）")
        try:
            r = con.execute("SELECT MIN(date), MAX(date) FROM kline").fetchone()
            print(f"  kline 日期范围   {r[0]} → {r[1]}")
            n_items = con.execute("SELECT COUNT(DISTINCT item_id) FROM kline").fetchone()[0]
            print(f"  kline 覆盖品种   {n_items:,}")
        except sqlite3.OperationalError:
            print("  kline            （表不存在）")
        return ok == "ok"
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)

    if args.verify_only:
        print(f"=== 校验 {out} ===")
        return 0 if verify(out) else 1

    src = Path(args.src)
    if not src.exists():
        print(f"源库不存在: {src}")
        return 2

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.tmp-{os.getpid()}")
    print(f"=== 备份 ===")
    print(f"  源  : {src}  ({human(src.stat().st_size)})")
    print(f"  目标: {out}")

    if out.exists():
        print(f"  已有备份会保留到新备份校验通过（{human(out.stat().st_size)}）")

    t0 = time.time()
    con = sqlite3.connect(str(src), timeout=180)
    con.execute("PRAGMA busy_timeout=180000")
    try:
        if tmp.exists():
            tmp.unlink()
        # VACUUM INTO 的路径参数不能是绑定变量，只能拼字符串；路径来自命令行，风险可控
        safe_tmp = tmp.as_posix().replace("'", "''")
        con.execute(f"VACUUM INTO '{safe_tmp}'")
    except Exception as e:      # noqa: BLE001
        print(f"  备份失败，旧备份保持不动：{e}")
        if tmp.exists():
            tmp.unlink()
        return 1
    finally:
        con.close()

    print(f"  完成，耗时 {time.time() - t0:.0f}s，大小 {human(tmp.stat().st_size)}")
    print()
    print("=== 校验 ===")
    ok = verify(tmp)
    if not ok:
        print(f"  新备份校验失败，保留原备份不动：{tmp}")
        return 1
    os.replace(tmp, out)
    print(f"  已原子替换：{out}")
    print()
    print("备份可用 ✓")

    # 顺手报告源库状态，便于对比
    print()
    print("=== 源库当前状态 ===")
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        for t in ("snap", "kline"):
            try:
                n = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"  {t:<16} {n:>12,}")
            except sqlite3.OperationalError:
                print(f"  {t:<16} （表不存在）")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
