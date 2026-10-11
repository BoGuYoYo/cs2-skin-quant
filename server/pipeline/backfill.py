"""用 SteamDT 的 K 线接口批量回填历史价格 —— 一次性操作，用于跳过 21 天冷启动。

背景：
    /price/batch 只能拿「当前快照」，所以要一天天攒出日线序列，必须等 21 天。
    但 /open/cs2/item/v1/kline 能直接返回整段历史：
        type=1 小时线 2160 条（90 天）
        type=2 日线    365 条（1 年）
        type=3 周线    224 条（6 年）
    权限每分钟 120 次 → 19137 个品种约需 2.7 小时。

用法：
    python -m pipeline.backfill                 # 回填全部武器皮肤
    python -m pipeline.backfill --limit 500     # 只回填前 500 个（试跑）
    python -m pipeline.backfill --ktype 2       # 日线（默认）
    python -m pipeline.backfill --status        # 只看进度，不拉数据
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from . import collector
from .config import CFG

# 上报进度给前端（push 包在 server/ 下，不在 pipeline/ 里）
_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)
try:
    from push import progress as task_progress  # noqa: E402
except Exception:  # 前端进度是锦上添花，缺了也要能跑
    task_progress = None  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")

KLINE_URL = "https://open.steamdt.com/open/cs2/item/v1/kline"

# 每分钟 120 次配额，留 20% 余量 → 每 0.6 秒一次
SLEEP_PER_CALL = float(os.environ.get("KLINE_SLEEP", "0.6"))

# 单个品种超过这个天数就认为已回填过，跳过（断点续传）
SKIP_THRESHOLD_DAYS = 200

# 每多少个品种 commit 一次。
# 这里必须远小于日志间隔：回填每品种耗时约 0.6 秒，若 100 个才提交，
# 一个写事务就长达 60 秒，会把 SQLite 写锁一直占住，
# 导致同机的自托管 API 连接超时（踩过：health 等待 15 秒后报 db down）。
COMMIT_EVERY = int(os.environ.get("BACKFILL_COMMIT_EVERY", "10"))

# 每多少个品种打一次日志 / 上报一次进度
LOG_EVERY = int(os.environ.get("BACKFILL_LOG_EVERY", "100"))


def reload_names(con: sqlite3.Connection) -> list[str]:
    """监控池 —— 统一来自 collector.WATCHLIST_SQL（只保留可交易品种）"""
    return collector.load_watchlist(con)


def fetch_kline(session: requests.Session, api_key: str, name: str, ktype: int, platform: str) -> list:
    r = session.post(
        KLINE_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"marketHashName": name, "type": ktype, "platform": platform},
        timeout=45,
    )
    if r.status_code == 429:
        raise RuntimeError("RATE_LIMIT")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"API {j.get('errorCode')}: {j.get('errorMsg')}")
    data = j.get("data")
    return data if isinstance(data, list) else []


def write_kline(con: sqlite3.Connection, item_id: str, rows: list) -> int:
    """转发到 collector.write_kline_rows —— 只保留一份实现，避免两边漂移。

    这里原先有一份独立实现，字段顺序解析是错的（把 low 当成了 close），
    导致 150 万行历史价格全被记成当日最低价。修的时候得同时改两处、极易漏 ——
    所以统一到 collector 一处。
    """
    return collector.write_kline_rows(con, item_id, rows, write_kline=True)


def status(con: sqlite3.Connection) -> None:
    total = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    names = reload_names(con)
    withdata = con.execute("SELECT COUNT(DISTINCT item_id) FROM snap").fetchone()[0]
    rows = con.execute("SELECT COUNT(*) FROM snap").fetchone()[0]
    rng = con.execute("SELECT MIN(date), MAX(date) FROM snap").fetchone()
    print(f"  items 总数      : {total}")
    print(f"  监控池（武器皮肤）: {len(names)}")
    print(f"  已有数据的品种   : {withdata}")
    print(f"  snap 总行数     : {rows}")
    print(f"  日期范围        : {rng[0]} → {rng[1]}")
    remain = max(0, len(names) - withdata)
    print(f"  待回填          : {remain} 个品种，预计 {remain * SLEEP_PER_CALL / 60:.0f} 分钟")


def main() -> int:
    ap = argparse.ArgumentParser(description="K 线历史回填")
    ap.add_argument("--limit", type=int, default=0, help="只回填前 N 个（0=全部）")
    ap.add_argument("--ktype", type=int, default=2, help="1=小时 2=日线 3=周线")
    ap.add_argument("--platform", default="BUFF")
    ap.add_argument("--status", action="store_true", help="只显示进度")
    ap.add_argument("--force", action="store_true", help="不跳过已有数据的品种")
    args = ap.parse_args()

    key = os.environ.get("STEAMDT_KEY", "").strip()
    if not key:
        log.error("STEAMDT_KEY 未配置")
        return 2

    con = collector.connect(CFG.db_path)
    if args.status:
        print("\n=== 回填进度 ===")
        status(con)
        return 0

    names = reload_names(con)
    if not names:
        log.error("监控池为空 —— 先跑一次 run_daily 让 /base 写入 items 表")
        return 3

    # 断点续传：已有足够历史天数的品种跳过
    done: set[str] = set()
    if not args.force:
        for item_id, cnt in con.execute(
            "SELECT item_id, COUNT(*) FROM snap GROUP BY item_id HAVING COUNT(*) >= ?",
            (SKIP_THRESHOLD_DAYS,),
        ):
            done.add(item_id)

    todo = [n for n in names if n not in done]
    if args.limit > 0:
        todo = todo[: args.limit]

    log.info("监控池 %d，已回填 %d，本轮待处理 %d（日线）", len(names), len(done), len(todo))
    if not todo:
        log.info("全部已回填，无需处理")
        status(con)
        return 0

    est = len(todo) * SLEEP_PER_CALL / 60
    log.info("预计耗时 %.0f 分钟（每品种 %.2fs，配额 120 次/分钟）", est, SLEEP_PER_CALL)

    started_at = int(time.time())
    if task_progress:
        task_progress.report("backfill", "running", force=True,
                             total=len(todo), done=0, ok=0, empty=0, fail=0, rows=0,
                             eta_minutes=est, started_at=started_at,
                             message=f"日线回填 {len(todo)} 个品种")

    session = requests.Session()
    ok = fail = empty = total_rows = 0
    t0 = time.time()
    fail_streak = 0

    for i, name in enumerate(todo, 1):
        try:
            rows = fetch_kline(session, key, name, args.ktype, args.platform)
            n = write_kline(con, name, rows)
            if n:
                ok += 1
                total_rows += n
                fail_streak = 0
            else:
                # 空数据 ≠ 失败：该品种在所选平台没有报价（不可交易，或未上架）
                empty += 1
        except RuntimeError as e:
            msg = str(e)
            if "RATE_LIMIT" in msg:
                log.warning("触发限流，退避 65 秒")
                time.sleep(65)
                continue
            fail += 1
            fail_streak += 1
            if fail_streak <= 3:
                log.debug("  %s → %s", name[:40], msg)
            if fail_streak > 50:
                log.error("连续失败 %d 次，可能是配额耗尽或 key 失效，中止", fail_streak)
                break
        except Exception as e:
            fail += 1
            log.debug("  %s → %s", name[:40], e)

        if i % COMMIT_EVERY == 0:
            con.commit()
            if i % LOG_EVERY == 0:
                elapsed = time.time() - t0
                rate = i / elapsed * 60
                eta = (len(todo) - i) / max(rate, 1e-6)
                log.info("进度 %d/%d（%.1f%%）成功 %d 空 %d 失败 %d 行数 %d | %.0f 个/分钟 | 剩余 %.0f 分钟",
                         i, len(todo), i / len(todo) * 100, ok, empty, fail, total_rows, rate, eta)
                if task_progress:
                    task_progress.report(
                        "backfill", "running",
                        **task_progress.progress_fields(i, len(todo), t0, ok, empty, fail,
                                                        total_rows, started_at),
                    )
        time.sleep(SLEEP_PER_CALL)

    con.commit()
    if task_progress:
        task_progress.finish(
            "backfill",
            message=f"完成：成功 {ok}，空 {empty}，失败 {fail}，共 {total_rows:,} 行",
            total=len(todo), done=len(todo), ok=ok, empty=empty, fail=fail,
            rows=total_rows, eta_minutes=0, started_at=started_at,
        )
    log.info("回填完成：成功 %d，空数据 %d，失败 %d，写入 %d 行，耗时 %.1f 分钟",
             ok, empty, fail, total_rows, (time.time() - t0) / 60)
    status(con)
    return 0


if __name__ == "__main__":
    sys.exit(main())
