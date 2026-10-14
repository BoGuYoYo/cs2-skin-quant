"""采集层：SteamDT 开放平台 → 本地 SQLite 原始仓。

一个必须说清楚的事实：
  SteamDT 的 /price/* 接口给的是「当前快照」，不是历史序列。
  所以日线历史只能靠自己每天存一份累积出来。
  第一天启动时数据库是空的，要等 21 天后标签才成熟、模型才可训。
  想跳过这个冷启动，用 SteamTradingSiteTracker-Data 的历史 dump 回填（见 backfill_from_dump）。
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests

from .config import BEIJING, CFG, DOMAIN_EXCLUDE_SQL

log = logging.getLogger("collector")

#: 全系统唯一的价格口径：只用 BUFF 的底价做主序列。
#: 任何一条通道（K 线 / 快照 / 读历史）都必须认这个常量，
#: 否则同一列 sell 里混进别的平台，会凭空跳出一根假 K 线。
PRICE_PLATFORM = "BUFF"

STEAMDT_BASE = "https://open.steamdt.com"
BATCH_MAX = 100  # /price/batch 一次最多 100 个 marketHashName

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  item_id TEXT PRIMARY KEY, name TEXT, market_hash_name TEXT, wear TEXT,
  case_id TEXT, case_name TEXT, image TEXT, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS case_meta (
  case_id TEXT PRIMARY KEY, case_name TEXT, rarity_score REAL, case_price REAL,
  case_volume_30d REAL, supply_growth_30d REAL, drop_pool TEXT,
  rationale TEXT, blocking_risk TEXT, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS snap (
  item_id TEXT, date TEXT, sell REAL, bid REAL,
  listings INTEGER, volume INTEGER, supply INTEGER, ts INTEGER,
  src TEXT, count_ts INTEGER,
  PRIMARY KEY (item_id, date)
);
CREATE INDEX IF NOT EXISTS ix_snap ON snap(item_id, date);
-- kline 必须在这里建：write_kline_rows 直接写它，而 to_local.ensure_schema
-- 只在 PUSH_MODE 含 local 时才会跑。曾经漏建导致「新库首次采集」
-- 抛 no such table: kline，且因为先写 snap 后写 kline，留下半写入状态。
CREATE TABLE IF NOT EXISTS kline (
  item_id TEXT, date TEXT, o REAL, h REAL, l REAL, c REAL, v INTEGER,
  ts INTEGER,
  PRIMARY KEY (item_id, date)
);
CREATE INDEX IF NOT EXISTS ix_kline ON kline(item_id, date);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, event_type TEXT,
  affected_cases TEXT, direction TEXT, magnitude INTEGER,
  horizon_days INTEGER, confidence REAL, evidence TEXT, reasoning TEXT
);
-- meta：少量键值状态（目前只有挂单量采集的断点游标）。
--
-- 必须在 collector 自己的 SCHEMA 里建，不能指望 to_local.ensure_schema：
-- 后者只在「发生一次推送」时才会跑，而「items 已落地、还没发生过推送」的
-- 半初始化库是真实存在的（首次 run_daily 在 base 同步写入 items 之后、
-- 首次 push 之前被杀掉就会留下这种库；机器上 4G 内存，OOM 杀过不止一次）。
--
-- 踩过的坑：漏建 meta 时，collect_listings 每写完一批就抛
-- OperationalError: no such table: meta —— 游标永远存不下去，采集器
-- 写完第一批 100 个品种就死、systemd 拉起来又死，永远卡在第一批；
-- 而流动性闸门按「不知道就放行」的设计照常工作，从看板上完全看不出
-- 采集器在崩。崩溃与静默失效同时发生，这是最难查的一类。
CREATE TABLE IF NOT EXISTS meta (
  k TEXT PRIMARY KEY, v TEXT, updated_at INTEGER NOT NULL
);
"""


def _upsert_sql(table: str, cols: list[str], pk: list[str]) -> str:
    """生成 UPSERT：冲突时只更新本次提供的列，未提供的保持原值。

    踩过的坑：原先统一用 INSERT OR REPLACE，而它的语义是「删掉整行再插入」，
    于是只写价格时会把 bid/listings/volume/supply 全部清成 NULL。
    kline 采集和 batch 采集交替跑时等于互相清空。
    """
    ph = ",".join(["?"] * len(cols))
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in pk)
    return (
        f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) "
        f"ON CONFLICT({','.join(pk)}) DO UPDATE SET {updates}"
    )


#: snap 专用（K 线通道）：用 COALESCE 让 NULL 不覆盖已有值。
#: K 线只提供价格，batch 采集会补 listings —— 两者必须能共存而不是互相清空。
#: 这一条**不加新旧比较**：K 线是「某个日期的权威日线」，它来收尾就该允许覆盖
#: （例如次日 07:00 用它把昨天那根未封口的 bar 定稿）。
SNAP_UPSERT = """
INSERT INTO snap (item_id, date, sell, bid, listings, volume, supply, ts, src, count_ts)
VALUES (?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(item_id, date) DO UPDATE SET
  sell     = COALESCE(excluded.sell,     snap.sell),
  bid      = COALESCE(excluded.bid,      snap.bid),
  listings = COALESCE(excluded.listings, snap.listings),
  volume   = COALESCE(excluded.volume,   snap.volume),
  supply   = COALESCE(excluded.supply,   snap.supply),
  ts       = COALESCE(excluded.ts,       snap.ts),
  src      = COALESCE(excluded.src,      snap.src),
  count_ts = COALESCE(excluded.count_ts, snap.count_ts)
"""

#: 价格那一组的写入条件（sell / bid / ts / src 同生共死）：
#:   NEW_PRICE  这次带了有效价格（excluded.ts 非空）
#:   NEWER      不比库里已有的价格旧
#:   SRC_OK     不是「回退平台顶掉主平台」
#:
#: ⚠️ src 必须跟着价格一起判 —— 审查发现的坑：库里本来是一条 C5 ¥50，
#: 随后来了个 BUFF 的**零报价**（价格无效）。原先只有价格被 COALESCE 保住，
#: src 却照改不误，于是那条 C5 价被贴上了 BUFF 标签，顺利通过 src 过滤进了模型。
#: 来源必须描述"当前这条价格到底是谁的"。
_PRICE_WRITE_COND = (
    "excluded.ts IS NOT NULL"
    " AND (snap.ts IS NULL OR excluded.ts >= snap.ts)"
    f" AND (excluded.src = '{PRICE_PLATFORM}' OR snap.src IS NULL OR snap.src <> '{PRICE_PLATFORM}')"
)

#: 流动性那一组的写入条件（listings / volume / supply / count_ts）：
#: 价格与数量是**两次独立的观测**，各带各的时间戳。
#: 踩过的坑：价格无效时把 ts 置空，而 UPSERT 又允许 NULL 时间戳无条件更新 ——
#: 一条更旧的「零挂单量」就把刚刚写进去的 80 改成了 0，而价格时间戳还显示是新的。
#:
#: ⚠️ 还要求**这次真的带回了数量**（excluded.listings IS NOT NULL）：
#: 否则一次「响应里没有 sellCount」的空观测也会把 count_ts 推到最新，
#: 随后那条时间戳更旧的有效数量就永远进不来了（数量被 80 + 新时间戳锁死）。
#:
#: 这里**不**按平台过滤数量（试过，不行）：没有在售价时 src 本来就是空的，
#: 一卡平台条件就等于「今天没人挂售」这条最有用的流动性信息也写不进来。
#: 口径纪律对**价格**最重要（混平台会凭空跳出一根假 K 线），
#: 而挂单量只用来做「<20 就不看」这种粗粒度流动性闸门，平台差异是次要的。
_COUNT_WRITE_COND = (
    "excluded.count_ts IS NOT NULL"
    " AND excluded.listings IS NOT NULL"
    " AND (snap.count_ts IS NULL OR excluded.count_ts >= snap.count_ts)"
)

SNAP_UPSERT_QUOTE = f"""
INSERT INTO snap (item_id, date, sell, bid, listings, volume, supply, ts, src, count_ts)
VALUES (?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(item_id, date) DO UPDATE SET
  sell     = CASE WHEN {_PRICE_WRITE_COND} THEN COALESCE(excluded.sell, snap.sell) ELSE snap.sell END,
  bid      = CASE WHEN {_PRICE_WRITE_COND} THEN COALESCE(excluded.bid,  snap.bid)  ELSE snap.bid  END,
  ts       = CASE WHEN {_PRICE_WRITE_COND} THEN COALESCE(excluded.ts,   snap.ts)   ELSE snap.ts   END,
  src      = CASE WHEN {_PRICE_WRITE_COND} THEN COALESCE(excluded.src,  snap.src)  ELSE snap.src  END,
  listings = CASE WHEN {_COUNT_WRITE_COND} THEN COALESCE(excluded.listings, snap.listings) ELSE snap.listings END,
  volume   = CASE WHEN {_COUNT_WRITE_COND} THEN COALESCE(excluded.volume,   snap.volume)   ELSE snap.volume   END,
  supply   = CASE WHEN {_COUNT_WRITE_COND} THEN COALESCE(excluded.supply,   snap.supply)   ELSE snap.supply   END,
  count_ts = CASE WHEN {_COUNT_WRITE_COND} THEN COALESCE(excluded.count_ts, snap.count_ts) ELSE snap.count_ts END
"""

#: kline 表跟 snap 走**同一套**新旧规则 —— 否则图表和页面价格会分裂。
#: 踩过的坑：snap 那边拒掉了旧的当日 K 线（价格保持 ¥175），
#: 但 kline 表的 OHLC 仍被无条件覆盖成 ¥173 —— 页面显示 175、图上画 173。
KLINE_UPSERT = """
INSERT INTO kline (item_id, date, o, h, l, c, v, ts) VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT(item_id, date) DO UPDATE SET
  o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c,
  v=COALESCE(excluded.v, kline.v), ts=COALESCE(excluded.ts, kline.ts)
"""

#: 当日那根还没封口 —— 同样不许被更旧的 bar 覆盖。
#:
#: ⚠️ 光在 DO UPDATE 上加 WHERE 是**不够的**：冲突子句只管更新，
#: 当 kline 里还没有这一行时，INSERT 路径会直接插进去（审查就是靠这个抓到的：
#: snap 保住了 ¥175，kline 却被插进了 ¥173，页面价格与图表分裂）。
#: 所以这里用 snap 的 ts 当**唯一的新鲜度参照**再挡一道 INSERT：
#: 只有当这条 bar 不比 snap 里已有的报价更旧时才写。
#: 两张表共用同一个参照物，才不会出现"各自判断、各写各的"。
KLINE_UPSERT_TODAY = """
INSERT INTO kline (item_id, date, o, h, l, c, v, ts)
SELECT ?,?,?,?,?,?,?,?
WHERE NOT EXISTS (
  SELECT 1 FROM snap
   WHERE snap.item_id = ? AND snap.date = ? AND COALESCE(snap.ts, 0) > ?
)
ON CONFLICT(item_id, date) DO UPDATE SET
  o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c,
  v=COALESCE(excluded.v, kline.v), ts=COALESCE(excluded.ts, kline.ts)
WHERE excluded.ts IS NULL OR kline.ts IS NULL OR excluded.ts >= kline.ts
"""


def _migrate(con: sqlite3.Connection) -> None:
    """给**已存在的库**补上后来新增的列。

    ⚠️ 只改 SCHEMA 里的 CREATE TABLE 对老库完全无效
    （CREATE TABLE IF NOT EXISTS 见表就什么都不做），新代码写新列会直接
    抛 no such column。所以新增列必须同时写在这里。
    """
    have = {r[1] for r in con.execute("PRAGMA table_info(snap)")}
    if "src" not in have:
        con.execute("ALTER TABLE snap ADD COLUMN src TEXT")
        con.commit()
        log.info("snap 补列：src（记录这条行情来自哪个平台 / kline）")
    if "count_ts" not in have:
        con.execute("ALTER TABLE snap ADD COLUMN count_ts INTEGER")
        con.commit()
        log.info("snap 补列：count_ts（价格与挂单量是两次独立观测，各有各的时间戳）")

    have_k = {r[1] for r in con.execute("PRAGMA table_info(kline)")}
    if "ts" not in have_k:
        con.execute("ALTER TABLE kline ADD COLUMN ts INTEGER")
        con.commit()
        log.info("kline 补列：ts（K 线也要能判断新旧，否则图表会被旧 bar 覆盖）")


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=60)
    # 回填/采集会长时间写库，WAL + busy_timeout 让并发写入排队而不是直接报
    # "database is locked"（踩过：cases_build 撞上回填的写事务直接失败）
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.executescript(SCHEMA)
    _migrate(con)
    con.commit()
    return con


# --------------------------------------------------------------- SteamDT
#: /price/batch 的限流错误码。实测：被限流时 **不返回 HTTP 429**，
#: 而是 HTTP 200 + {"success": false, "errorCode": 4005,
#: "errorMsg": "当前接口请求已达到上限。详细规则请参阅接口文档。"}
RATE_LIMIT_CODE = 4005

#: 实测间隔：1 次/分钟，留 1 秒余量
BATCH_INTERVAL = 61.0


def _post_batch(s: requests.Session, api_key: str, chunk: list[str]) -> dict[str, Any]:
    r = s.post(
        f"{STEAMDT_BASE}/open/cs2/v1/price/batch",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"marketHashNames": chunk},
        timeout=30,
    )
    if r.status_code == 429:
        return {"success": False, "errorCode": RATE_LIMIT_CODE, "errorMsg": "HTTP 429"}
    r.raise_for_status()
    try:
        return r.json()
    except ValueError:
        return {"success": False, "errorCode": -1, "errorMsg": f"响应不是 JSON：{r.text[:120]!r}"}


def fetch_prices(
    market_hash_names: list[str],
    api_key: str,
    session: requests.Session | None = None,
    interval: float = BATCH_INTERVAL,
    max_retry: int = 3,
) -> list[dict[str, Any]]:
    """批量拉取多平台价格。返回 [{marketHashName, dataList:[{platform,sellPrice,...}]}, ...]

    ⚠️ 限流识别：**不能只看 HTTP 状态码**。
        踩过的坑：原先只判 `r.status_code == 429` 才退避，其余失败走
        `if not body.get("success"): log.warning(...); continue` ——
        而真实的限流正是 HTTP 200 + success=false + errorCode=4005，
        于是被限流时既不退避也不重试，**直接丢掉这 100 个品种**。
        日志里只留一行 warning，数据静默缺失，看上去却"跑完了"。
        现在的纪律：限流码 → 退避重试同一批；其他失败 → 抛错让调用方看见。

    interval: 每次批量请求之间的间隔秒数（1 次/分钟 → 61s）。
              单批调用时可以传 0，由外层循环控制节奏。
    """
    s = session or requests.Session()
    out: list[dict[str, Any]] = []
    chunks = [market_hash_names[i : i + BATCH_MAX] for i in range(0, len(market_hash_names), BATCH_MAX)]

    for idx, chunk in enumerate(chunks):
        for attempt in range(1, max_retry + 1):
            body = _post_batch(s, api_key, chunk)
            if body.get("success"):
                out.extend(body.get("data") or [])
                break
            code = body.get("errorCode")
            if code == RATE_LIMIT_CODE or str(code) == str(RATE_LIMIT_CODE):
                log.warning("限流（errorCode %s），第 %d 次退避 %.0fs 后重试同一批 %d 个",
                            code, attempt, interval, len(chunk))
                time.sleep(interval)
                continue
            # 非限流错误：参数/鉴权类问题重试也不会好，直接抛
            raise RuntimeError(f"/price/batch 失败：errorCode={code} errorMsg={body.get('errorMsg')!r}")
        else:
            raise RuntimeError(
                f"/price/batch 连续 {max_retry} 次被限流，放弃第 {idx + 1}/{len(chunks)} 批"
                f"（{len(chunk)} 个品种）—— 这不是「跑完了」，是没拿到数据"
            )
        if idx + 1 < len(chunks) and interval > 0:
            time.sleep(interval)
    return out


def fetch_base(api_key: str) -> list[dict[str, Any]]:
    """饰品基础信息 + 各平台 itemId 映射。每天只能调 1 次，务必缓存。"""
    r = requests.get(
        f"{STEAMDT_BASE}/open/cs2/v1/base",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"base 接口失败：{body.get('errorMsg')}")
    return body.get("data") or []


def pick_platform(block: dict[str, Any], prefer: str = PRICE_PLATFORM) -> dict[str, Any] | None:
    """统一口径：全系统只用同一个平台的底价做主序列，避免混口径造成人工暴跌。

    ⚠️ 回退是**例外**，不是常态：BUFF 缺失时取最低价平台，但调用方必须
    把真实平台记进 snap.src 并告警（见 write_daily_snapshot）——
    否则同一列 sell 里悄悄混进别的平台，价格序列会凭空跳一下，
    而这种跳变在特征里看不出任何异常，只会被当成「行情异动」。
    """
    rows = block.get("dataList") or []
    if not rows:
        return None
    for r in rows:
        if r.get("platform") == prefer:
            return r
    return min(rows, key=lambda x: x.get("sellPrice") or 9e9)


def _valid_price(v: Any) -> bool:
    """报价必须是有限的**正数**。

    踩过的坑：原先直接写 sellPrice，于是 0 / 负数 / NaN 都能进库。
    而 load_history() 只 dropna（去掉 NULL），**不去掉 0** ——
    一笔「0 元报价」会在特征里表现为 −100% 的暴跌，直接污染标签和训练。
    实测：库里曾经有 1,552 行 sell <= 0（见 scripts/fix_invalid_prices.py）。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


def _valid_count(v: Any) -> int | None:
    """挂单量/求购量：非负整数，否则当没数据（None）。

    注意 0 是**合法**值（真的没人挂单），它跟「没拿到数据」含义不同：
    前者会被流动性闸门剔除，后者按「不知道就放行」处理。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0:
        return None
    return int(f)


def write_daily_snapshot(
    con: sqlite3.Connection,
    blocks: Iterable[dict[str, Any]],
    date: str,
    prefer: str = PRICE_PLATFORM,
) -> int:
    """写入当日快照。

    ⚠️ date 必须由调用方**每批重新算**。踩过的坑：常驻采集在循环外算了一次
    `day` 就一路用到底，跨过午夜后把新报价继续写进前一天的记录里
    （实测：1,994 行 10-11 的数据是 10-12 凌晨 01:29 写进去的，
    等于用第二天的盘中价覆盖了第一天的收盘价）。

    ⚠️ ts 用响应里的 updateTime（这条报价自己的时间），不是本机写入时刻。
    否则「旧报价不许覆盖新报价」这条约束根本无从判断（见 SNAP_UPSERT_QUOTE）。

    ⚠️ 报价必须过 _valid_price：0 / 负数 / NaN 一律不写进价格。
    但不能因此**丢掉整条响应**（踩过的坑）：没有在售价时 sellCount 往往正好是 0，
    而「挂单量已知为 0」是要喂给流动性闸门的有效信息 —— 一起丢掉的话，
    闸门会把它当成「不知道」而放行，等于放行了一个没人挂售的品种。
    所以无效价格写成 NULL（load_history 会 dropna 掉，不进模型），
    但挂单量/求购量照常记录；ts 也传 NULL —— 不拿一条没有价格的报价去宣称"更新"。
    """
    n = 0
    bad_price = 0
    stale_quote = 0
    no_time = 0
    fallback: dict[str, int] = {}
    row_day = datetime.strptime(date, "%Y-%m-%d").date()
    for blk in blocks:
        name = blk.get("marketHashName")
        row = pick_platform(blk, prefer)
        if not name or not row:
            continue
        price_ok = _valid_price(row.get("sellPrice"))
        if not price_ok:
            bad_price += 1
        bid = row.get("biddingPrice")
        plat = str(row.get("platform") or "?")
        if plat != prefer:
            fallback[plat] = fallback.get(plat, 0) + 1

        # ⚠️ 来源时间必须如实使用，**不能**用本机时间顶替（审查发现）：
        # updateTime 缺失时写本机当前时间，等于凭空给这条报价盖一个"刚刚观测"的章，
        # 新鲜度闸门（按观测时间判断）就会放行一条观测时间未知的数据。
        # 所以缺失就留空 —— 宁可承认"不知道"，也不制造虚假的新鲜度。
        raw_ts = row.get("updateTime")
        try:
            quote_ts = int(raw_ts) if raw_ts else None
        except (TypeError, ValueError):
            quote_ts = None
        if quote_ts is None:
            no_time += 1

        # ⚠️ 陈旧报价**不许写进历史**（审查发现）：观测时间比入库日期早太多的报价，
        # 如果照样按"今天"的行写进去，就会：
        #   1) 让训练集里出现一堆"日期是新的、价格是旧的"样本（污染特征与标签）；
        #   2) 让 is_warm 被这些假样本喂饱，截面看起来是预热过的。
        # 只拦最后一行是不够的 —— 必须在**入历史之前**就挡掉。
        # 价格序列的权威来源仍是 K 线通道（每个交易日一根 bar，戳就是那天），
        # 所以挡掉陈旧快照不会让冷门品种丢价格。
        if quote_ts is not None:
            obs_day = datetime.fromtimestamp(quote_ts, tz=BEIJING).date()
            if (row_day - obs_day).days > CFG.quote_max_age_days:
                stale_quote += 1
                continue

        cnt = _valid_count(row.get("sellCount"))
        con.execute(
            SNAP_UPSERT_QUOTE,   # 不是 INSERT OR REPLACE，也不是无比较的 UPSERT
            (
                name,
                date,
                float(row["sellPrice"]) if price_ok else None,
                float(bid) if _valid_price(bid) else None,
                cnt,
                None,       # 成交量由 Steam 侧补充
                None,       # 存世量需另接数据源
                # 价格与数量是两次独立的观测：
                #   ts       —— 价格的观测时间，价格无效就置空（不许它顶掉已有价格的时间戳，
                #                也不许它把 src 改成 BUFF 而价格其实是别的平台的）
                #   count_ts —— 挂单量自己的观测时间；**响应里没有挂单量就不给时间戳**，
                #                否则空观测会把后续更旧（但有效）的数量锁在门外
                quote_ts if price_ok else None,
                plat if price_ok else None,
                quote_ts if cnt is not None else None,
            ),
        )
        n += 1
    con.commit()
    if stale_quote:
        log.warning("跳过 %d 条陈旧报价（观测时间比入库日期早 %d 天以上）—— "
                    "这些行情如果按今天写进去，会污染训练集（日期新、价格旧）"
                    "并让 is_warm 看着像是预热过的；价格序列仍由 K 线通道保证",
                    stale_quote, CFG.quote_max_age_days)
    if no_time:
        log.warning("有 %d 条响应没有 updateTime：观测时间按「未知」处理（不伪造本机时间）",
                    no_time)
    if bad_price:
        log.warning("丢弃 %d 条无效报价的**价格**（sellPrice 非正 / 非有限，已置空），"
                    "但保留其挂单量 —— 静默写入会让它在特征里变成 -100%% 暴跌", bad_price)
    if fallback:
        log.warning("平台回退 %d 个品种（%s 缺失）：%s —— "
                    "这些行的 src 不是 %s，混口径会在价格序列里制造人工跳价；"
                    "load_history 已按 src 过滤，它们不会进模型",
                    sum(fallback.values()), prefer, fallback, prefer)
    return n


# ------------------------------------------------- 流动性数据（在售挂单量 / 求购量）
#
# 用户的规则：「日交易量 <20/天 不看」。
# 但实测 SteamDT **没有成交量接口** ——
#   kline：小时/日/周 × BUFF/STEAM，一律 5 个字段（ts,o,c,h,l），纯价格
#   /price/*：只有 sellPrice(在售价) sellCount(在售量) biddingPrice(求购价) biddingCount(求购量)
# 所以只能拿「在售挂单量 sellCount」当流动性代理：挂单量 < 20 的品种，
# 要么真的没人交易，要么盘口薄到没有意义，与「交易量低的不看」的意图一致。
#
# ⚠️ 它只有当前快照，**历史补不回来**（公开数据集只有 2022-02-14 单日）。
#    想用就得从今天开始前向积累，这也是下面这个循环存在的唯一理由。

LISTINGS_CURSOR_KEY = "listings_cursor"


def _meta_get(con: sqlite3.Connection, k: str, default: str | None = None) -> str | None:
    try:
        r = con.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    except sqlite3.OperationalError:      # meta 表还没建（纯采集脚本首次跑）
        return default
    return r[0] if r else default


def _meta_set(con: sqlite3.Connection, k: str, v: str) -> None:
    """存游标。

    ⚠️ 失败不能让采集停摆：游标只是「省点重复劳动」的优化，丢了顶多下次
    从旧位置重跑一遍（SNAP_UPSERT 是幂等的，重复写不会出错）。
    所以这里兜住异常、吼一声继续跑，而不是让整轮采集崩掉。

    踩过的坑（两层）：meta 表曾经不在 collector.SCHEMA 里 → 这里每批都抛
    OperationalError → 采集器写完第一批就死、systemd 拉起又死；而流动性闸门
    因为「没数据就放行」照常显示正常，看板上完全看不出采集器在崩。
    Schema 已补上（根因）；这层兜底是第二道防线。
    """
    try:
        con.execute(
            "INSERT INTO meta (k, v, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
            (k, v, int(time.time())),
        )
        con.commit()
    except sqlite3.Error as e:
        log.warning("游标保存失败（%s=%s）：%s —— 本轮继续，下次会从旧位置重跑", k, v, e)


def _load_progress():
    """惰性加载 push.progress —— 前端进度是锦上添花，缺了也要能跑。"""
    try:
        import os as _os
        import sys as _sys
        _srv = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _srv not in _sys.path:
            _sys.path.insert(0, _srv)
        from push import progress as _prog  # type: ignore
        return _prog
    except Exception:
        return None


def collect_listings(
    con: sqlite3.Connection,
    session: requests.Session,
    api_key: str,
    names: list[str],
    interval: float = BATCH_INTERVAL,
    max_batches: int | None = None,
    log_every: int = 5,
) -> dict[str, int]:
    """按 1 次/分钟的配额循环拉挂单量，写进 snap，断点续跑。

    配额现实：一批 100 个，18853 个品种一轮 = 189 批 = 189 分钟。
    进度存在 meta.listings_cursor，重启后接着上次的位置跑，不从头再来。

    max_batches: 只跑 N 批就返回（测试用；None = 跑满一轮）。
    """
    # ⚠️ day 不能只在循环外算一次：这个循环是**常驻**的（一轮约 86 分钟，
    # 一轮接一轮），算一次的话跨过午夜后，第二天的新报价会继续写进
    # 前一天的记录里 —— 等于用第二天的盘中价覆盖第一天的收盘价。
    # 实测：1,994 行 2026-10-11 的数据是 10-12 凌晨 01:29 写的。
    #
    # ⚠️ 而且必须用 BEIJING（审查发现）：这里是裸 datetime.now()，
    # 用的是服务器本地时区；同一份数据在 write_daily_snapshot 里又按 BEIJING
    # 比较观测日期 —— 服务器一旦不是 +08，两套口径就会在日界附近打架。
    # config 已明确「任何按天划界的地方都必须用 BEIJING」，这里不能例外。
    day = datetime.now(BEIJING).strftime("%Y-%m-%d")
    stats = {"batches": 0, "items": 0, "rows": 0, "fail": 0, "day": day}
    if not names:
        return stats

    try:
        cur = int(_meta_get(con, LISTINGS_CURSOR_KEY, "0") or 0)
    except ValueError:
        cur = 0
    if cur >= len(names):
        cur = 0
        log.info("挂单量采集：上一轮已走完，从头发起新一轮（%d 个品种）", len(names))

    # ---- 进度上报（审查 9 发现）----
    # 这是个**常驻**循环：listings.py 只在启动时写一次 running/done=0，
    # 而 collect_listings 跑到天荒地老也不会返回 —— 那句 finish() 永远到不了。
    # 于是看板上「挂单量采集」的进度条永远停在 0%、速率与剩余显示「—」，
    # 而采集器其实每分钟都在往前走。这是「看板说谎」：
    # 既看不出它活着，也看不出它死了（行一直显示 running）。
    # 现在每批上报真实游标，进度条才与现实对得上。
    _prog = _load_progress()
    _t0 = time.time()            # 本次进程起点：用于算真实速率
    _started_at = int(_t0)       # 展示用的「开始时间」

    def _report(done: int, message: str | None = None) -> None:
        if not _prog:
            return
        elapsed = max(time.time() - _t0, 1e-6)
        rate = stats["items"] / elapsed * 60.0     # 个/分钟：本次进程真正发出的量
        remaining = max(len(names) - done, 0)
        try:
            _prog.report(
                "listings", "running", total=len(names), done=done,
                fail=stats["fail"], rows=stats["rows"],
                rate_per_min=round(rate, 1),
                eta_minutes=round(remaining / rate, 1) if rate > 0 else None,
                started_at=_started_at,
                message=message or f"常驻循环 · 游标 {done}/{len(names)}",
            )
        except Exception as e:      # noqa: BLE001
            log.debug("挂单量进度上报失败（不影响采集）：%s", e)

    _report(cur)     # 重启后立刻把真实游标亮出来，别让看板停在 0%

    n_chunks = (len(names) + BATCH_MAX - 1) // BATCH_MAX
    while stats["batches"] < (max_batches or n_chunks * 10**6):
        if cur >= len(names):
            log.info("挂单量采集：一轮结束（%d 个品种 / %d 批），从头发起新一轮",
                     len(names), n_chunks)
            cur = 0
            _started_at = int(time.time())
        chunk = names[cur : cur + BATCH_MAX]
        if not chunk:
            break

        try:
            blocks = fetch_prices(chunk, api_key, session, interval=interval)
        except RuntimeError as e:
            # 不静默丢批：记下来、退避、下一轮再补这些品种（游标不动）
            stats["fail"] += len(chunk)
            log.error("挂单量采集失败（游标停在 %d，下轮重试）：%s", cur, e)
            time.sleep(interval * 2)
            continue

        # 每批重新取日期：常驻服务会跨午夜，不能把启动日期一路用下去
        new_day = datetime.now(BEIJING).strftime("%Y-%m-%d")
        if new_day != day:
            log.info("挂单量采集：跨日 %s → %s，后续行情写入新日期（旧日期不再改动）",
                     day, new_day)
            day = new_day
            stats["day"] = new_day

        stats["rows"] += write_daily_snapshot(con, blocks, day)
        stats["items"] += len(chunk)
        stats["batches"] += 1
        cur += len(chunk)
        _meta_set(con, LISTINGS_CURSOR_KEY, str(cur))

        if stats["batches"] % log_every == 0:
            pct = cur / len(names) * 100
            log.info("  挂单量 %d/%d（%.1f%%，第 %d 批，累计写入 %d 行）",
                     cur, len(names), pct, stats["batches"], stats["rows"])
        _report(cur)      # 每批都报：进度条、速率、剩余分钟数才有意义
        time.sleep(interval)

    return stats


def write_items(con: sqlite3.Connection, base: list[dict[str, Any]], now: int) -> int:
    """用 /base 的返回填充 items 表。

    ⚠️ 主键必须用 market_hash_name，不能用平台的 goods_id。
       踩过的坑：采集 / 特征 / 预测 / 回填全都拿 market_hash_name 当 item_id，
       而这里原先写的是 BUFF goods_id（纯数字），导致 items 与
       forecast / snap / kline 全都 JOIN 不上 —— 前端候选池的名字、图片全为 null。
       平台的 itemId 属于映射信息，需要时再从 /base 取，不该混进主键。

    ⚠️ 绝对不能用 INSERT OR REPLACE（审查发现）。
       它的语义是「删行重插」，而这里 case_id / case_name / image 恒为 NULL ——
       于是 cases_build 用 UPDATE 建立起来的箱子关联会被整片抹掉，
       核心稀有度特征（case_rarity_score）随即退化成默认值 50。
       现在改成只更新「/base 真的提供的那几列」，关联列由 cases_build 负责，
       两边各管各的，谁也覆盖不了谁。
       （同类坑在本文件其它地方也踩过：只有 snap 保留了 COALESCE 语义。）
    """
    n = 0
    for it in base:
        mhn = it.get("marketHashName")
        if not mhn:
            continue
        con.execute(
            """INSERT INTO items (item_id, name, market_hash_name, wear, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET
                 name            = COALESCE(excluded.name, items.name),
                 market_hash_name = COALESCE(excluded.market_hash_name, items.market_hash_name),
                 wear            = COALESCE(excluded.wear, items.wear),
                 updated_at      = excluded.updated_at""",
            (mhn, it.get("name"), mhn, _wear(mhn), now),
        )
        n += 1
    con.commit()
    return n


def _wear(market_hash_name: str) -> str | None:
    for w in ("Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"):
        if f"({w})" in market_hash_name:
            return w
    return None


#: 域外品种（纪念品 / StatTrak）的排除条件来自 config.DOMAIN_EXCLUDE_SQL ——
#: 唯一来源在那边，本文件不要另写一份，否则前端列表 / 采集池 / 推理口径必然打架。
# 监控池定义：只保留可交易的武器皮肤与探员。


# 监控池定义：只保留可交易的武器皮肤与探员。
#
# 踩过的坑：/base 返回 44497 个品种，但其中大量是**不可交易**的道具 ——
# 服役勋章、大行动之星、生日硬币、音乐盒胶囊、涂鸦、挂件、印花。
# 它们拿 K 线只会返回空数据（曾经让回填成功率看起来只有 20%，其实是规则没滤干净）。
# 另外千万**不要**排除「探员」—— Agent 是可交易饰品，价格数据完整。
WATCHLIST_SQL = """
  SELECT market_hash_name FROM items
   WHERE market_hash_name IS NOT NULL
     AND market_hash_name NOT LIKE '#%'
     AND name NOT LIKE '%印花%'
     AND name NOT LIKE '%涂鸦%'
     AND name NOT LIKE '%挂件%'
     AND name NOT LIKE '%音乐盒%'
     AND name NOT LIKE '%硬币%'
     AND name NOT LIKE '%勋章%'
     AND name NOT LIKE '%大行动之星%'
     AND name NOT LIKE '%胶囊%'
     AND name NOT LIKE '%通行证%'
     AND market_hash_name NOT LIKE '%Coin%'
     AND market_hash_name NOT LIKE '%Medal%'
     AND market_hash_name NOT LIKE '%Star for Operation%'
     AND market_hash_name NOT LIKE '%Music Kit%'
     AND market_hash_name NOT LIKE '%Capsule%'
     AND market_hash_name NOT LIKE '%Graffiti%'
     AND market_hash_name NOT LIKE '%Patch%'
""" + DOMAIN_EXCLUDE_SQL + """
   ORDER BY market_hash_name
"""


def load_watchlist(con: sqlite3.Connection) -> list[str]:
    """监控池 = 可交易的武器皮肤 + 探员（排除不可交易道具）"""
    return [r[0] for r in con.execute(WATCHLIST_SQL).fetchall()]


# --------------------------------------------------------------- K 线接口
KLINE_URL = f"{STEAMDT_BASE}/open/cs2/item/v1/kline"

#: type 参数：1=小时线(2160条/90天)  2=日线(365条/1年)  3=周线(224条/6年)
KLINE_TYPE_HOUR = 1
KLINE_TYPE_DAY = 2
KLINE_TYPE_WEEK = 3


def fetch_kline(
    session: requests.Session,
    api_key: str,
    market_hash_name: str,
    ktype: int = KLINE_TYPE_DAY,
    platform: str = "BUFF",
) -> list[list]:
    """拉一段 K 线。返回 [[ts, open, high, low, close], ...]，权限 120 次/分钟。"""
    r = session.post(
        KLINE_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"marketHashName": market_hash_name, "type": ktype, "platform": platform},
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


def _ohlc_ok(o: float, h: float, l: float, c: float) -> bool:
    """K 线自洽性 + 有限性校验。

    踩过的坑：这里原先只做 float() 转换就落库 —— 而 float("nan") / float("inf")
    都不抛异常，矛盾的 bar（high < max(o,c)）也照写。字段顺序那个坑
    （[ts,o,c,h,l] 被当成 [ts,o,h,l,c]）正是靠这条不变量才认得出来：
    回归测试里一直有同款检查，但**生产代码里从来没接上**。
    """
    if not all(math.isfinite(v) and v > 0 for v in (o, h, l, c)):
        return False
    return h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9


def write_kline_rows(
    con: sqlite3.Connection,
    item_id: str,
    rows: list,
    write_kline: bool = True,
    src: str = PRICE_PLATFORM,
) -> int:
    """K 线行 → snap（价格）+ kline（完整 OHLC）

    ⚠️ 字段顺序是 [ts, open, close, high, low]，**不是** [ts, o, h, l, c]。
       踩过的坑：把 row[4] 当成 close，实际那是 low —— 结果 150 万行历史价格
       全被记成「当日最低价」，与实盘系统性偏低。判据：字段顺序为 [o,h,l,c] 时
       会出现 high < low 的矛盾；按 [o,c,h,l] 解释则 6/6 条都满足
       high >= max(o,c) 且 low <= min(o,c)，且 close 与实时在售价完全一致。
       现在这条判据已经进 _ohlc_ok()，不合规的 bar 直接丢弃。

    ⚠️ 时间戳是 UTC 16:00（= 北京时间 00:00）。必须按北京时间归日，
       否则整条序列偏移一天（踩过：10-10 的数据被记成 10-09）。

    ⚠️ 新旧保护要分开对待（踩过的坑：只给 batch 加了保护、K 线仍无比较）：
       - **历史日线是「某天的权威定稿」**：次日补写/修正就该覆盖，
         所以走无比较的 SNAP_UPSERT。
       - **今天那根还没封口**：它是盘中值，必须和报价一样受新旧保护 ——
         否则一根 5 分钟前的 K 线会把刚抓到的更新报价顶回去，价格序列倒退。
    """
    n = 0
    bad = 0
    today = datetime.now(BEIJING).strftime("%Y-%m-%d")
    for row in rows:
        try:
            ts = int(row[0])
            o, c, h, l = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        except (IndexError, TypeError, ValueError):
            bad += 1
            continue
        if not _ohlc_ok(o, h, l, c):
            bad += 1
            continue
        day = datetime.fromtimestamp(ts + 8 * 3600, tz=timezone.utc).strftime("%Y-%m-%d")
        # 当日（含未来）那一根还没封口：snap 与 kline 都必须受新旧保护，
        # 两张表一起拒或一起收，否则页面价格和图表会对不上。
        today_bar = day >= today
        con.execute(
            SNAP_UPSERT_QUOTE if today_bar else SNAP_UPSERT,
            (item_id, day, c, None, None, None, None, ts, src, None),
        )
        if write_kline:
            if today_bar:
                # 参数：SELECT 的 8 个 + 守卫子查询的 3 个（item_id / date / ts）
                con.execute(KLINE_UPSERT_TODAY,
                            (item_id, day, o, h, l, c, None, ts, item_id, day, ts))
            else:
                con.execute(KLINE_UPSERT, (item_id, day, o, h, l, c, None, ts))
        n += 1
    if bad:
        log.warning("%s：丢弃 %d 条不合格 K 线（非有限值 / 非正数 / OHLC 自相矛盾）",
                    item_id[:44], bad)
    return n


def collect_daily_kline(
    con: sqlite3.Connection,
    session: requests.Session,
    api_key: str,
    names: list[str],
    platform: str = "BUFF",
    sleep: float = 0.6,
    log_every: int = 200,
) -> dict[str, int]:
    """日常采集走 K 线接口。

    相比 /price/batch 的两个优势：
      1) 配额 120 次/分钟（batch 只有 1 次/分钟）→ 18853 个品种约 3 小时，batch 要 3.2 小时且更容易撞限额
      2) 一次返回整段历史，能自动补齐前几天漏掉的日期（batch 只有当前快照）
    """
    import time as _time

    # 进度上报（前端实时查看）；导入失败不影响采集
    _prog = _load_progress()

    stats = {"ok": 0, "empty": 0, "fail": 0, "rows": 0}
    t0 = _time.time()
    started_at = int(t0)
    if _prog:
        _prog.report("collect", "running", force=True, total=len(names), done=0,
                     ok=0, empty=0, fail=0, rows=0,
                     eta_minutes=len(names) * sleep / 60, started_at=started_at,
                     message=f"K线采集 {len(names)} 个品种")

    for i, name in enumerate(names, 1):
        try:
            rows = fetch_kline(session, api_key, name, KLINE_TYPE_DAY, platform)
            # src 必须传真实平台：不传会默认成 BUFF，于是「用 STEAM 回填」
            # 会被记成 BUFF 数据，来源记录反而变成误导（审查发现的问题）。
            n = write_kline_rows(con, name, rows, src=platform)
            if n:
                stats["ok"] += 1
                stats["rows"] += n
            else:
                stats["empty"] += 1
        except RuntimeError as e:
            if "RATE_LIMIT" in str(e):
                log.warning("触发限流，退避 65 秒")
                _time.sleep(65)
                continue
            stats["fail"] += 1
        except Exception:
            stats["fail"] += 1

        if i % log_every == 0:
            con.commit()
            el = _time.time() - t0
            log.info("K线采集 %d/%d（%.1f%%）成功 %d 空 %d 失败 %d 行 %d | %.0f 个/分钟 | 剩余 %.0f 分钟",
                     i, len(names), i / len(names) * 100, stats["ok"], stats["empty"],
                     stats["fail"], stats["rows"], i / el * 60,
                     (len(names) - i) / max(i / el, 1e-9) / 60)
            if _prog:
                _prog.report("collect", "running",
                             **_prog.progress_fields(i, len(names), t0, stats["ok"],
                                                     stats["empty"], stats["fail"],
                                                     stats["rows"], started_at))
        _time.sleep(sleep)

    con.commit()
    if _prog:
        _prog.finish("collect",
                     message=f"完成：成功 {stats['ok']}，空 {stats['empty']}，失败 {stats['fail']}",
                     total=len(names), done=len(names), ok=stats["ok"], empty=stats["empty"],
                     fail=stats["fail"], rows=stats["rows"], eta_minutes=0, started_at=started_at)
    log.info("K线采集完成：成功 %d 空 %d 失败 %d，写入 %d 行，耗时 %.1f 分钟",
             stats["ok"], stats["empty"], stats["fail"], stats["rows"], (_time.time() - t0) / 60)
    return stats


def load_history(
    con: sqlite3.Connection,
    days: int = 1200,
    items: list[str] | None = None,
) -> pd.DataFrame:
    """读取本地快照仓 → 宽表 DataFrame

    必须 JOIN items 取 case_id —— 这是供给面特征能否生效的关键。
    踩过的坑：这里原先只 SELECT snap 的列，没有 case_id，而 features.build 的
    箱子合并以 `if "case_id" in df.columns` 为前置条件，于是整段被静默跳过，
    case_rarity_score 退回默认值 50、supply_rigidity 恒 0.2315 —— 23 个特征里
    9 个是常数，MONOTONE 里针对这些列的约束等于没有约束。

    另外：K 线回填只写价格，volume/supply/listings 都是 NULL。
    pandas 会把整列 NULL 推断成 object，进而让 turnover = volume/supply 也是 object，
    LightGBM 会直接拒绝（"pandas dtypes must be int, float or bool"）。
    所以这里必须显式 to_numeric 兜住。

    items: 只取这些品种（None = 全部）。
        4G 内存的机器上一次读 542 万行再算特征会被内核 OOM 杀掉，
        所以 run_daily 改成按品种分块调用本函数（见 _load_feats_batched）。

    平台口径：**只读 BUFF 与未标注行的价格**。
        src 是本项目自己记的来源（C5/YOUPIN/STEAM = 当时 BUFF 缺失、回退抓的）。
        混口径会在同一列里制造人工跳价：悠悠有品的 ¥50 和 BUFF 的 ¥52 之间
        本来就有天然价差，混进来就会被当成行情跳变。
        src IS NULL 的是加 src 列之前的历史行（那时只有 K 线 BUFF 与少量回退），
        保留它们；其余非 BUFF 的一律不进模型（它们仍然留在库里可供诊断）。
    """
    # 条件一律走 WHERE（别拼进 JOIN 的 ON —— 那样被过滤掉的行会变成一堆
    # item 列为 NULL 的幽灵行，而不是干净地消失）
    conds = ["(s.src IS NULL OR s.src = ?)"]
    params: list[Any] = [PRICE_PLATFORM]
    if items is not None:
        if not items:
            return pd.DataFrame()
        conds.insert(0, "s.item_id IN (%s)" % ",".join(["?"] * len(items)))
        params = list(items) + [PRICE_PLATFORM]
    where = " WHERE " + " AND ".join(conds)

    df = pd.read_sql_query(
        """SELECT s.item_id, s.date, s.sell AS price, s.bid, s.listings, s.volume, s.supply,
                  s.ts AS obs_ts,
                  i.case_id AS case_id, i.case_name AS case_name
             FROM snap s
             LEFT JOIN items i ON i.item_id = s.item_id""" + where,
        con,
        params=params,
    )
    if df.empty:
        return df

    for c in ("price", "bid", "listings", "volume", "supply"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # ---------------------------------------------------------- 观测日期
    # 「入库日期（date）」≠「这条行情真正发生的时间」。
    # 踩过的坑（审查）：date 是**采集那天**写的，于是 2026-10-13 抓到一条
    # 来源 updateTime = 2026-09-29 的缓存报价时，新鲜度闸门只看 date，
    # 就把一个月前的行情当成今天的放行了。
    #
    # ts 是这条行情自己的时间戳：
    #   batch 报价 → 平台返回的 updateTime
    #   K 线       → 那根 bar 的戳（历史 bar 是 UTC 16:00 = 北京次日 00:00，
    #                当前 bar 是最后一次更新时间）
    # 两者都按同一套约定（+8h）换成北京日期，正好等于 K 线入库时用的口径。
    # ts 为空（没有价格、或早期数据）时退回 date。
    ts_num = pd.to_numeric(df.pop("obs_ts"), errors="coerce")
    obs = pd.to_datetime(ts_num, unit="s", errors="coerce", utc=True) + pd.Timedelta(hours=8)
    df["obs_date"] = obs.dt.tz_localize(None).dt.normalize()
    df["obs_date"] = df["obs_date"].fillna(pd.to_datetime(df["date"]))

    df = df.dropna(subset=["price"])
    # 读到 0 / 负价时直接当无效（写入端已校验，这里是防历史脏数据的安全网 ——
    # 库里曾经有 1,552 行 sell <= 0，见 scripts/fix_invalid_prices.py）
    n_bad = int((df["price"] <= 0).sum())
    if n_bad:
        log.warning("load_history：跳过 %d 行非正价格（历史脏数据）", n_bad)
        df = df[df["price"] > 0]
    df["date"] = pd.to_datetime(df["date"])
    cutoff = df["date"].max() - pd.Timedelta(days=days)
    return df[df["date"] >= cutoff].sort_values(["item_id", "date"]).reset_index(drop=True)


def latest_listings(con: sqlite3.Connection, items: list[str] | None = None) -> pd.DataFrame:
    """每个品种**最新一次有挂单量的观测**（不管那天有没有有效价格）。

    为什么要单独读一遍（审查发现）：
        「今天没有在售价（sellPrice=0）→ 价格写 NULL，挂单量记成 0」
        这条信息在 load_history 里会被 dropna 丢掉（它只保留有价格的行），
        于是截面拿到的其实是**昨天的价格 + 昨天的挂单量**；
        而昨天的挂单量可能是 100（合格），闸门就把一个"今天已经没人挂售"的品种
        当成合格放行了 —— 也正是这条规则想剔除的对象。

    所以：价格取「最后一根有效价」（load_history），流动性取「最新一次快照」（本函数），
    两者按品种关联。返回 item_id / listings / listings_date / count_ts。
    """
    conds = ["s.listings IS NOT NULL"]
    inner_conds = ["listings IS NOT NULL"]
    params: list[Any] = []
    if items is not None:
        if not items:
            return pd.DataFrame()
        ph = ",".join(["?"] * len(items))
        conds.insert(0, f"s.item_id IN ({ph})")
        inner_conds.insert(0, f"item_id IN ({ph})")
        # 参数顺序跟着 SQL 文本：内层子查询在前，外层 WHERE 在后
        params = list(items) + list(items)

    df = pd.read_sql_query(
        # ⚠️ 选「最新**观测**」，不是「最新**行**」（审查发现）：
        # 原先用 MAX(date) 挑行，于是「昨天观测到挂单量 0、今天收到一条观测时间更旧的
        # 缓存数量 100」时，会选中今天那行旧缓存，再因为过期被判成"不知道"而放行 ——
        # 昨天那条**仍然有效**的 0 反而被丢掉了。
        # 所以排序键用 count_ts（没有 count_ts 的老行退回入库日期）。
        #
        # ⚠️ 内层子查询也要带上品种过滤（审查发现）：否则每次都在 540 万行的
        # snap 上做全表 GROUP BY，而调用方其实只关心当前这批品种。
        "SELECT s.item_id, s.listings, s.date AS listings_date, s.count_ts "
        "  FROM snap s "
        "  JOIN (SELECT item_id,"
        "               MAX(COALESCE(count_ts, CAST(strftime('%s', date) AS INTEGER))) AS k"
        "          FROM snap WHERE " + " AND ".join(inner_conds) + " GROUP BY item_id) m "
        "    ON m.item_id = s.item_id "
        "   AND COALESCE(s.count_ts, CAST(strftime('%s', s.date) AS INTEGER)) = m.k "
        " WHERE " + " AND ".join(conds),
        con,
        params=params,
    )
    if df.empty:
        return df
    for c in ("listings", "count_ts"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # 观测日期同样按 count_ts 还原（同 load_history 的理由：
    # 入库日期是采集那天，不能代表这条挂单量是什么时候看到的）
    obs = pd.to_datetime(df["count_ts"], unit="s", errors="coerce", utc=True) + pd.Timedelta(hours=8)
    df["listings_obs_date"] = obs.dt.tz_localize(None).dt.normalize()
    df["listings_obs_date"] = df["listings_obs_date"].fillna(pd.to_datetime(df["listings_date"]))
    return df


def backfill_from_dump(con: sqlite3.Connection, dump_dir: str | Path) -> int:
    """用 SteamTradingSiteTracker-Data 的历史 dump 回填，跳过 21 天冷启动。

    该数据集里的结构（DataDumps / SteamBuffSnapshot）字段随版本变动，
    这里按「item_name, date, price 三列」的通用假设做映射，实际使用前先看一眼表头。
    """
    dump_dir = Path(dump_dir)
    n = 0
    for csv in sorted(dump_dir.rglob("*.csv")):
        try:
            d = pd.read_csv(csv)
        except Exception as e:
            log.warning("跳过 %s：%s", csv.name, e)
            continue
        cols = {c.lower(): c for c in d.columns}
        name_col = next((cols[c] for c in ("item_name", "market_hash_name", "name") if c in cols), None)
        date_col = next((cols[c] for c in ("date", "ts", "timestamp") if c in cols), None)
        price_col = next((cols[c] for c in ("price", "sell_price", "buff_price") if c in cols), None)
        if not (name_col and date_col and price_col):
            log.warning("列名无法识别，跳过：%s（列=%s）", csv.name, list(d.columns))
            continue
        for _, r in d.iterrows():
            try:
                day = pd.to_datetime(r[date_col]).strftime("%Y-%m-%d")
            except Exception:
                continue
            con.execute(
                "INSERT OR IGNORE INTO snap (item_id, date, sell, bid, listings, volume, supply, ts) VALUES (?,?,?,?,?,?,?,?)",
                (str(r[name_col]), day, float(r[price_col]), None, None, None, None, int(time.time())),
            )
            n += 1
        con.commit()
        log.info("回填 %s：累计 %d 行", csv.name, n)
    return n
