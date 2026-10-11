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
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, event_type TEXT,
  affected_cases TEXT, direction TEXT, magnitude INTEGER,
  horizon_days INTEGER, confidence REAL, evidence TEXT, reasoning TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    con.commit()
    return con


# --------------------------------------------------------------- SteamDT
def fetch_prices(market_hash_names: list[str], api_key: str, session: requests.Session | None = None) -> list[dict[str, Any]]:
    """批量拉取多平台价格。返回 [{marketHashName, BUFF:{sell,bid,listings}, ...}]"""
    s = session or requests.Session()
    out: list[dict[str, Any]] = []
    for i in range(0, len(market_hash_names), BATCH_MAX):
        chunk = market_hash_names[i : i + BATCH_MAX]
        r = s.post(
            f"{STEAMDT_BASE}/open/cs2/v1/price/batch",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"marketHashNames": chunk},
            timeout=30,
        )
        if r.status_code == 429:
            log.warning("限流（/price/batch 为 1 次/分钟），退避 65s")
            time.sleep(65)
            continue
        r.raise_for_status()
        body = r.json()
        if not body.get("success"):
            log.warning("接口返回失败：%s", body.get("errorMsg"))
            continue
        for blk in body.get("data") or []:
            out.append(blk)
        if i + BATCH_MAX < len(market_hash_names):
            time.sleep(61)  # 尊重 1 次/分钟限制
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
            "INSERT OR REPLACE INTO snap (item_id, date, sell, bid, listings, volume, supply, ts) VALUES (?,?,?,?,?,?,?,?)",
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


def write_kline_rows(con: sqlite3.Connection, item_id: str, rows: list) -> int:
    """K 线行 → snap 表（只填价格，bid/listings/supply 留空由价格接口补）"""
    n = 0
    for row in rows:
        try:
            ts = int(row[0])
            close = float(row[4])
        except (IndexError, TypeError, ValueError):
            continue
        day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        con.execute(
            "INSERT OR REPLACE INTO snap (item_id, date, sell, bid, listings, volume, supply, ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (item_id, day, close, None, None, None, None, ts),
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


def load_history(con: sqlite3.Connection, days: int = 1200) -> pd.DataFrame:
    """读取本地快照仓 → 宽表 DataFrame

    注意：K 线回填只写价格，volume/supply/listings 都是 NULL。
    pandas 会把整列 NULL 推断成 object，进而让 turnover = volume/supply 也是 object，
    LightGBM 会直接拒绝（"pandas dtypes must be int, float or bool"）。
    所以这里必须显式 to_numeric 兜住。
    """
    df = pd.read_sql_query(
        "SELECT item_id, date, sell AS price, bid, listings, volume, supply FROM snap",
        con,
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
