"""写本地 SQLite —— 让服务器自托管 API 不再依赖 Cloudflare。

背景：
    反代到 Cloudflare 的 API 要 6~24 秒（跨境），体验不可接受。
    服务器本地已经有完整数据（18853 个品种的历史），直接读本地库可以降到毫秒级。

设计：
    表结构与 D1 完全一致（复用 to_d1.STAGES 的列定义），
    这样 app.py 的查询语句可以与 Worker 的 read.ts 逐条对应，前端无需任何改动。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger("push.local")

# 复用 D1 通道的列定义，保证两边 schema 不漂移
from .to_d1 import STAGES  # noqa: E402

_DB_PATH = Path(os.environ.get("DB_PATH", "./state/skins.db"))


# ---------------------------------------------------------------- 建表
def ensure_schema(con: sqlite3.Connection) -> None:
    """建出与 D1 相同结构的表（幂等）。

    注意 snap（采集用）与 snap_daily（D1 用）并存：
    snap 是采集器的原始落库点，snap_daily 是聚合后的日线，两者职责不同。
    """
    cols_sql = {
        "items": """
            item_id TEXT PRIMARY KEY, name TEXT, market_hash_name TEXT, wear TEXT,
            case_id TEXT, case_name TEXT, image TEXT, updated_at INTEGER NOT NULL""",
        "case_meta": """
            case_id TEXT PRIMARY KEY, case_name TEXT, rarity_score REAL, case_price REAL,
            case_volume_30d REAL, supply_growth_30d REAL, drop_pool TEXT,
            rationale TEXT, blocking_risk TEXT, updated_at INTEGER NOT NULL""",
        "snap_daily": """
            item_id TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            bid REAL, listings INTEGER, volume INTEGER, supply INTEGER,
            PRIMARY KEY (item_id, date)""",
        "kline": """
            item_id TEXT, date TEXT, o REAL, h REAL, l REAL, c REAL, v INTEGER,
            PRIMARY KEY (item_id, date)""",
        "forecast": """
            item_id TEXT, date TEXT, horizon INTEGER, q10 REAL, q50 REAL, q90 REAL,
            q10_adj REAL, q90_adj REAL, prob_up REAL, fwd_max_dd REAL, score REAL,
            side TEXT, size REAL, why TEXT, model_version TEXT,
            PRIMARY KEY (item_id, date)""",
        "factor_latest": """
            item_id TEXT PRIMARY KEY, date TEXT, price REAL, dd_from_high REAL,
            drop_duration_days INTEGER, ret_7d REAL, ret_21d REAL, ret_30d REAL,
            vol_21 REAL, turnover REAL, list_ratio REAL, list_slope_7d REAL,
            supply_peer_pct REAL, supply_rigidity REAL, case_rarity_score REAL,
            rel_ret_21d REAL, event_bearish_30d INTEGER, event_bullish_30d INTEGER,
            updated_at INTEGER NOT NULL""",
        "alerts": """
            id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, name TEXT,
            level TEXT NOT NULL, score REAL, ts INTEGER NOT NULL, detail TEXT,
            pushed INTEGER NOT NULL DEFAULT 0""",
        "market_index": """
            date TEXT PRIMARY KEY, index_value REAL, online_players INTEGER,
            vol_turnover REAL""",
        "backtest": """
            date TEXT PRIMARY KEY, mean_21d REAL, cum REAL, win_rate REAL,
            n INTEGER, model_version TEXT""",
        "meta": "k TEXT PRIMARY KEY, v TEXT, updated_at INTEGER NOT NULL",
        "task_progress": """
            task TEXT PRIMARY KEY, status TEXT NOT NULL, total INTEGER, done INTEGER,
            ok INTEGER, empty INTEGER, fail INTEGER, rows INTEGER,
            rate_per_min REAL, eta_minutes REAL, message TEXT,
            started_at INTEGER, updated_at INTEGER NOT NULL""",
        "runs": """
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, stage TEXT NOT NULL,
            ok INTEGER NOT NULL, rows INTEGER, message TEXT""",
    }
    for table, cols in cols_sql.items():
        con.execute(f"CREATE TABLE IF NOT EXISTS {table} ({cols})")

    # 索引（与 migrations 对应）
    for idx in [
        "CREATE INDEX IF NOT EXISTS ix_fc_date_score ON forecast(date, score DESC)",
        "CREATE INDEX IF NOT EXISTS ix_fc_date_q10 ON forecast(date, q10_adj DESC)",
        "CREATE INDEX IF NOT EXISTS ix_alert_ts ON alerts(ts DESC)",
        "CREATE INDEX IF NOT EXISTS ix_kline_item ON kline(item_id, date DESC)",
        "CREATE INDEX IF NOT EXISTS ix_progress_updated ON task_progress(updated_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_items_mhn ON items(market_hash_name)",
    ]:
        try:
            con.execute(idx)
        except sqlite3.OperationalError:
            pass

    # 视图：与 D1 的 candidate_view / alert_view 保持一致
    con.execute("""
        CREATE VIEW IF NOT EXISTS candidate_view AS
        SELECT f.date, f.item_id, i.name, i.market_hash_name, i.image, i.case_name, i.wear,
               fl.price, fl.dd_from_high, fl.drop_duration_days, fl.turnover, fl.list_ratio,
               fl.case_rarity_score, fl.rel_ret_21d,
               f.q10, f.q50, f.q90, f.q10_adj, f.q90_adj, f.prob_up,
               f.score, f.side, f.size, f.why, f.model_version
          FROM forecast f
          LEFT JOIN items i ON i.item_id = f.item_id
          LEFT JOIN factor_latest fl ON fl.item_id = f.item_id
    """)
    con.execute("""
        CREATE VIEW IF NOT EXISTS alert_view AS
        SELECT a.id, a.item_id, COALESCE(a.name, i.name) AS name, a.level, a.score,
               a.ts, a.detail, a.pushed, i.image, i.case_name, i.wear
          FROM alerts a
          LEFT JOIN items i ON i.item_id = a.item_id
    """)
    con.commit()


def connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_DB_PATH), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")       # 允许读写并发（读不会被写阻塞）
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")     # 回填在跑时会争抢写锁，等它 30 秒
    ensure_schema(con)
    return con


# ---------------------------------------------------------------- 写入
def _coerce(v: Any) -> Any:
    if v is None:
        return None
    tmod = type(v).__module__
    if (tmod.startswith("numpy") or tmod.startswith("pandas")) and hasattr(v, "item"):
        try:
            v = v.item()
        except (ValueError, TypeError):
            v = str(v)
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v
    if isinstance(v, (dict, list, tuple)):
        import json
        return json.dumps(v, ensure_ascii=False)
    return v


def push(
    stage: str,
    rows: Sequence[dict[str, Any]] | Iterable[dict[str, Any]],
    *,
    replace_date: str | None = None,
    replace_series: str | None = None,
) -> int:
    """写本地 SQLite（接口与 to_d1.push 完全一致）"""
    cols = STAGES.get(stage)
    if cols is None:
        raise ValueError(f"未知 stage：{stage}")

    rows = list(rows)
    if not rows:
        return 0

    con = connect()
    try:
        changed = 0
        if replace_date and "date" in cols:
            cur = con.execute(f"DELETE FROM {stage} WHERE date = ?", (replace_date,))
            changed += cur.rowcount or 0
        if replace_series and "item_id" in cols:
            cur = con.execute(f"DELETE FROM {stage} WHERE item_id = ?", (replace_series,))
            changed += cur.rowcount or 0

        ph = ",".join(["?"] * len(cols))
        sql = f"INSERT OR REPLACE INTO {stage} ({','.join(cols)}) VALUES ({ph})"
        payload = [[_coerce(r.get(c)) for c in cols] for r in rows]

        # 回填/采集进程会长时间持有写锁，这里退避重试而不是直接失败
        import time as _time
        last: Exception | None = None
        for attempt in range(1, 7):
            try:
                con.executemany(sql, payload)
                con.commit()
                return len(rows)
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower():
                    raise
                last = e
                wait = 2 ** attempt
                log.warning("本地库被占用，%d 秒后重试（第 %d 次）", wait, attempt)
                _time.sleep(wait)
        raise RuntimeError(f"本地库持续被占用：{last}")
    finally:
        con.close()


def check() -> dict[str, Any]:
    """自检：确认本地库可读"""
    con = connect()
    try:
        n = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        latest = con.execute("SELECT MAX(date) FROM forecast").fetchone()[0]
        return {"ok": True, "db": str(_DB_PATH), "items": n, "latest_forecast": latest}
    finally:
        con.close()
