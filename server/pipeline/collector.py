"""采集层：SteamDT 开放平台 → 本地 SQLite 原始仓。

一个必须说清楚的事实：
  SteamDT 的 /price/* 接口给的是「当前快照」，不是历史序列。
  所以日线历史只能靠自己每天存一份累积出来。
  第一天启动时数据库是空的，要等 21 天后标签才成熟、模型才可训。
  想跳过这个冷启动，用 SteamTradingSiteTracker-Data 的历史 dump 回填（见 backfill_from_dump）。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests

log = logging.getLogger("collector")

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
  PRIMARY KEY (item_id, date)
);
CREATE INDEX IF NOT EXISTS ix_snap ON snap(item_id, date);
-- kline 必须在这里建：write_kline_rows 直接写它，而 to_local.ensure_schema
-- 只在 PUSH_MODE 含 local 时才会跑。曾经漏建导致「新库首次采集」
-- 抛 no such table: kline，且因为先写 snap 后写 kline，留下半写入状态。
CREATE TABLE IF NOT EXISTS kline (
  item_id TEXT, date TEXT, o REAL, h REAL, l REAL, c REAL, v INTEGER,
  PRIMARY KEY (item_id, date)
);
CREATE INDEX IF NOT EXISTS ix_kline ON kline(item_id, date);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, event_type TEXT,
  affected_cases TEXT, direction TEXT, magnitude INTEGER,
  horizon_days INTEGER, confidence REAL, evidence TEXT, reasoning TEXT
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


#: snap 专用：用 COALESCE 让 NULL 不覆盖已有值。
#: kline 采集只提供价格，batch 采集会补 listings —— 两者必须能共存而不是互相清空。
SNAP_UPSERT = """
INSERT INTO snap (item_id, date, sell, bid, listings, volume, supply, ts)
VALUES (?,?,?,?,?,?,?,?)
ON CONFLICT(item_id, date) DO UPDATE SET
  sell     = COALESCE(excluded.sell,     snap.sell),
  bid      = COALESCE(excluded.bid,      snap.bid),
  listings = COALESCE(excluded.listings, snap.listings),
  volume   = COALESCE(excluded.volume,   snap.volume),
  supply   = COALESCE(excluded.supply,   snap.supply),
  ts       = COALESCE(excluded.ts,       snap.ts)
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=60)
    # 回填/采集会长时间写库，WAL + busy_timeout 让并发写入排队而不是直接报
    # "database is locked"（踩过：cases_build 撞上回填的写事务直接失败）
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.executescript(SCHEMA)
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


def pick_platform(block: dict[str, Any], prefer: str = "BUFF") -> dict[str, Any] | None:
    """统一口径：全系统只用同一个平台的底价做主序列，避免混口径造成人工暴跌。"""
    rows = block.get("dataList") or []
    if not rows:
        return None
    for r in rows:
        if r.get("platform") == prefer:
            return r
    # 退而求其次：取最低价平台
    return min(rows, key=lambda x: x.get("sellPrice") or 9e9)


def write_daily_snapshot(con: sqlite3.Connection, blocks: Iterable[dict[str, Any]], date: str) -> int:
    """写入当日快照。同一天重复跑会覆盖（幂等）。"""
    n = 0
    for blk in blocks:
        name = blk.get("marketHashName")
        row = pick_platform(blk)
        if not name or not row:
            continue
        con.execute(
            SNAP_UPSERT,   # 不是 INSERT OR REPLACE：只更新提供的列，不清空已有供需字段
            (
                name,
                date,
                row.get("sellPrice"),
                row.get("biddingPrice"),
                row.get("sellCount"),
                None,       # 成交量由 Steam 侧补充
                None,       # 存世量需另接数据源
                int(time.time()),
            ),
        )
        n += 1
    con.commit()
    return n


def write_items(con: sqlite3.Connection, base: list[dict[str, Any]], now: int) -> int:
    """用 /base 的返回填充 items 表。

    ⚠️ 主键必须用 market_hash_name，不能用平台的 goods_id。
       踩过的坑：采集 / 特征 / 预测 / 回填全都拿 market_hash_name 当 item_id，
       而这里原先写的是 BUFF goods_id（纯数字），导致 items 与
       forecast / snap / kline 全都 JOIN 不上 —— 前端候选池的名字、图片全为 null。
       平台的 itemId 属于映射信息，需要时再从 /base 取，不该混进主键。
    """
    n = 0
    for it in base:
        mhn = it.get("marketHashName")
        if not mhn:
            continue
        con.execute(
            """INSERT OR REPLACE INTO items
               (item_id, name, market_hash_name, wear, case_id, case_name, image, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (mhn, it.get("name"), mhn, _wear(mhn), None, None, None, now),
        )
        n += 1
    con.commit()
    return n


def _wear(market_hash_name: str) -> str | None:
    for w in ("Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"):
        if f"({w})" in market_hash_name:
            return w
    return None


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


def write_kline_rows(con: sqlite3.Connection, item_id: str, rows: list, write_kline: bool = True) -> int:
    """K 线行 → snap（价格）+ kline（完整 OHLC）

    ⚠️ 字段顺序是 [ts, open, close, high, low]，**不是** [ts, o, h, l, c]。
       踩过的坑：把 row[4] 当成 close，实际那是 low —— 结果 150 万行历史价格
       全被记成「当日最低价」，与实盘系统性偏低。判据：字段顺序为 [o,h,l,c] 时
       会出现 high < low 的矛盾；按 [o,c,h,l] 解释则 6/6 条都满足
       high >= max(o,c) 且 low <= min(o,c)，且 close 与实时在售价完全一致。

    ⚠️ 时间戳是 UTC 16:00（= 北京时间 00:00）。必须按北京时间归日，
       否则整条序列偏移一天（踩过：10-10 的数据被记成 10-09）。
    """
    n = 0
    for row in rows:
        try:
            ts = int(row[0])
            o, c, h, l = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        except (IndexError, TypeError, ValueError):
            continue
        day = datetime.fromtimestamp(ts + 8 * 3600, tz=timezone.utc).strftime("%Y-%m-%d")
        # 关键：K 线只提供价格，用 SNAP_UPSERT 保证它不会把 batch 采集补上的
        # bid / listings 清成 NULL（原先的 INSERT OR REPLACE 会）
        con.execute(SNAP_UPSERT, (item_id, day, c, None, None, None, None, ts))
        if write_kline:
            con.execute(
                "INSERT INTO kline (item_id, date, o, h, l, c, v) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(item_id, date) DO UPDATE SET "
                "o=excluded.o, h=excluded.h, l=excluded.l, c=excluded.c, "
                "v=COALESCE(excluded.v, kline.v)",
                (item_id, day, o, h, l, c, None),
            )
        n += 1
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
    _prog = None
    try:
        import os as _os
        import sys as _sys
        _srv = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _srv not in _sys.path:
            _sys.path.insert(0, _srv)
        from push import progress as _prog  # type: ignore
    except Exception:
        _prog = None

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
            n = write_kline_rows(con, name, rows)
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
    """
    where = ""
    params: list[Any] = []
    if items is not None:
        if not items:
            return pd.DataFrame()
        where = " WHERE s.item_id IN (%s)" % ",".join(["?"] * len(items))
        params = list(items)

    df = pd.read_sql_query(
        """SELECT s.item_id, s.date, s.sell AS price, s.bid, s.listings, s.volume, s.supply,
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

    df = df.dropna(subset=["price"])
    df["date"] = pd.to_datetime(df["date"])
    cutoff = df["date"].max() - pd.Timedelta(days=days)
    return df[df["date"] >= cutoff].sort_values(["item_id", "date"]).reset_index(drop=True)


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
