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
    """用 /base 的返回填充 items 表（item_id 用平台 id，market_hash_name 做全局键）"""
    n = 0
    for it in base:
        mhn = it.get("marketHashName")
        if not mhn:
            continue
        plats = it.get("platformList") or []
        pid = next((p.get("itemId") for p in plats if p.get("name") == "BUFF"), None) or mhn
        con.execute(
            """INSERT OR REPLACE INTO items
               (item_id, name, market_hash_name, wear, case_id, case_name, image, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (str(pid), it.get("name"), mhn, _wear(mhn), None, None, None, now),
        )
        n += 1
    con.commit()
    return n


def _wear(market_hash_name: str) -> str | None:
    for w in ("Factory New", "Minimal Wear", "Field-Tested", "Well-Worn", "Battle-Scarred"):
        if f"({w})" in market_hash_name:
            return w
    return None


def load_history(con: sqlite3.Connection, days: int = 1200) -> pd.DataFrame:
    """读取本地快照仓 → 宽表 DataFrame"""
    df = pd.read_sql_query(
        "SELECT item_id, date, sell AS price, bid, listings, volume, supply FROM snap",
        con,
    )
    if df.empty:
        return df
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
