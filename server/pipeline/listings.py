"""常驻挂单量采集：给「流动性闸门」喂数。

规则来源（用户 2026-10-11 定）：
    「日交易量低的不看（小于 20 一天）」

为什么这里采的是**在售挂单量**而不是成交量：
    实测 SteamDT 根本没有成交量接口 ——
      kline 三个周期 × 两个平台，一律 5 个字段 [ts,o,c,h,l]，纯价格
      /price/* 只有在售价/在售量/求购价/求购量
    所以拿 sellCount（在售挂单量）当流动性代理。挂单量 < 20 的品种，
    要么真的没人交易，要么盘口薄到没有意义 —— 与规则的意图一致。

为什么必须常驻：
    它只有当前快照，历史补不回来（公开数据集只有 2022-02-14 单日）。
    配额 1 次/分钟 × 100 个 → 全量一轮 189 分钟，所以只能挂着循环跑，
    一轮接一轮，游标存在 meta.listings_cursor，重启不从头来。

用法：
    python -m pipeline.listings              # 常驻循环（systemd 服务的 ExecStart）
    python -m pipeline.listings --batches 3  # 只跑 3 批（约 3 分钟，验证用）
    python -m pipeline.listings --status     # 只看游标与已积累的数据
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

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
log = logging.getLogger("listings")


def status(con) -> int:
    total = con.execute("SELECT COUNT(*) FROM snap").fetchone()[0]
    with_list = con.execute("SELECT COUNT(listings) FROM snap").fetchone()[0]
    print(f"  snap 总行数        : {total:,}")
    print(f"  有挂单量的行数      : {with_list:,}（{with_list / max(total, 1):.2%}）")
    cur = collector._meta_get(con, collector.LISTINGS_CURSOR_KEY, "0")
    pool = len(collector.load_watchlist(con))
    try:
        pct = int(cur) / max(pool, 1) * 100
    except (TypeError, ValueError):
        pct = 0.0
    print(f"  监控池              : {pool:,}")
    print(f"  游标                : {cur}（{pct:.1f}%）")
    rows = con.execute(
        "SELECT date, COUNT(listings) FROM snap WHERE listings IS NOT NULL "
        "GROUP BY date ORDER BY date DESC LIMIT 7"
    ).fetchall()
    if rows:
        print("  最近有挂单量的日期：")
        for d, n in rows:
            print(f"    {d}  {n:,} 个品种")
    else:
        print("  最近有挂单量的日期：无（还没开始积累）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="常驻挂单量采集")
    ap.add_argument("--batches", type=int, default=None,
                    help="只跑 N 批就退出（验证用；不填 = 常驻循环）")
    ap.add_argument("--interval", type=float, default=collector.BATCH_INTERVAL,
                    help=f"批间隔秒数，默认 {collector.BATCH_INTERVAL:.0f}（接口配额 1 次/分钟）")
    ap.add_argument("--status", action="store_true", help="只看状态")
    args = ap.parse_args()

    con = collector.connect(CFG.db_path)

    if args.status:
        return status(con)

    key = os.environ.get("STEAMDT_KEY", "")
    if not key:
        log.error("STEAMDT_KEY 未配置，退出")
        return 2

    names = collector.load_watchlist(con)
    log.info("挂单量采集启动：监控池 %d 个（纪念品 / StatTrak 已排除），"
             "间隔 %.0fs → 一轮约 %.0f 分钟",
             len(names), args.interval, len(names) / collector.BATCH_MAX * args.interval / 60)
    if not names:
        log.error("监控池为空，先跑 base 同步")
        return 3

    # ⚠️ 进度上报交给 collect_listings（审查 9 发现）。
    # 原先这里只写一次 running/done=0 就再无下文：常驻循环**永远不返回**，
    # 所以下面那句 finish() 在线上一次都没执行过 —— 看板上「挂单量采集」
    # 的进度条永久停在 0%、速率/剩余显示「—」，既看不出它活着，
    # 也看不出它死了。现在由采集循环每批上报真实游标。
    if not task_progress:
        log.warning("进度模块不可用：看板上的挂单量进度不会更新（采集不受影响）")

    try:
        stats = collector.collect_listings(
            con, requests.Session(), key, names,
            interval=args.interval, max_batches=args.batches,
        )
    except KeyboardInterrupt:
        log.info("手动中断")
        return 0

    log.info("本轮结束：%d 批 / %d 个品种 / 写入 %d 行 / 失败 %d",
             stats["batches"], stats["items"], stats["rows"], stats["fail"])
    if task_progress:
        try:
            task_progress.finish("listings", message=f"挂单量本轮完成：{stats['items']} 个品种")
        except Exception:
            pass
    return 0 if not stats["fail"] else 1


if __name__ == "__main__":
    sys.exit(main())
