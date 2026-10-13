"""skin-alpha 服务器端只读 API —— 与 Cloudflare Worker 的接口逐条对齐。

为什么要有它：
    反代到 Cloudflare 的 API 要 6~24 秒（跨境），体验不可接受。
    服务器本地已有完整数据（SQLite），直读可以降到毫秒级，且完全不依赖 Cloudflare。

设计：
    响应格式与 Worker 完全一致（{"ok":true,"data":...}），
    所以前端只需把 VITE_API_BASE 指过来，web/src/lib/api.ts 一行都不用改。

启动：
    uvicorn app:app --host 127.0.0.1 --port 8788
"""
from __future__ import annotations

import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")

app = FastAPI(title="skin-alpha server api", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

TOKEN = os.environ.get("INGEST_TOKEN", "")
HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DB_PATH", "./state/skins.db"))
if not DB_PATH.is_absolute():
    DB_PATH = HERE / DB_PATH


_DOMAIN_SQL: str | None = None


def domain_exclude_sql() -> str:
    """域外品种（纪念品 / StatTrak）的 SQL 排除条件。

    规则唯一来源在 pipeline/config.py 的 DOMAIN_EXCLUDE_SQL —— 这里只做转发，
    绝不要在本文件里再抄一份（抄了就会漂移：采集池过滤了、接口没过滤，
    于是「监控池里看不见、候选页里又有」）。

    为什么是惰性导入：API 进程不需要 pandas / requests，而 pipeline 包会带进来。
    只在真正查询时导一次，导不动就退化成不过滤（宁可多显示，不可因为
    一个可选过滤把整个列表接口搞挂）。
    """
    global _DOMAIN_SQL
    if _DOMAIN_SQL is None:
        try:
            from pipeline.config import DOMAIN_EXCLUDE_SQL

            _DOMAIN_SQL = DOMAIN_EXCLUDE_SQL
        except Exception as e:      # noqa: BLE001
            log.warning("域过滤条件加载失败（%s），本次查询不过滤", e)
            _DOMAIN_SQL = ""
    return _DOMAIN_SQL


# ---------------------------------------------------------------- 工具
def db() -> sqlite3.Connection:
    """只读连接 —— 这是自托管 API 快的关键。

    用 mode=ro 而不是普通连接：WAL 模式下只读连接不需要写锁，
    所以即使回填/采集正在写库，API 也不会被阻塞。
    （踩过：用普通连接时，回填的长写事务会让 health 等待 15 秒后报 db down）
    """
    if not DB_PATH.exists():
        raise HTTPException(503, f"本地库不存在：{DB_PATH}（管线还没跑过？）")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=3000")
    return con


def rows_to_list(rows: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def ok(data: Any) -> dict[str, Any]:
    return {"ok": True, "data": data}


def _auth(authorization: str | None) -> None:
    if not TOKEN:
        raise HTTPException(500, "INGEST_TOKEN 未在服务器上配置")
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "unauthorized")


def _latest_date(con: sqlite3.Connection, table: str) -> str | None:
    if table not in {"forecast", "snap_daily", "kline", "market_index", "backtest", "factor_latest"}:
        raise HTTPException(400, "bad table")
    r = con.execute(f"SELECT date FROM {table} ORDER BY date DESC LIMIT 1").fetchone()
    return r["date"] if r else None


SORT_COLS = {
    "score": "f.score",
    "q10": "f.q10_adj",
    "prob": "f.prob_up",
    "price": "fl.price",
    "duration": "fl.drop_duration_days",
    "rarity": "fl.case_rarity_score",
    "turnover": "fl.turnover",
}


# ---------------------------------------------------------------- 端点
@app.get("/api/health")
def health() -> dict[str, Any]:
    started = time.time()
    db_state = "down"
    counts: dict[str, int] = {}
    try:
        con = db()
        n = con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]
        db_state = "up" if n >= 0 else "down"
        counts = {
            "items": n,
            "forecast": con.execute("SELECT COUNT(*) AS n FROM forecast").fetchone()["n"],
            "kline": con.execute("SELECT COUNT(*) AS n FROM kline").fetchone()["n"],
        }
        con.close()
    except Exception as e:
        log.warning("health: %s", e)

    return {
        "ok": True,
        "service": "skin-alpha",
        "mode": "self-hosted",
        "db": db_state,
        "dbPath": str(DB_PATH),
        "counts": counts,
        "dbLatencyMs": int((time.time() - started) * 1000),
        "now": int(time.time()),
    }


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    con = db()
    try:
        # meta 表可能还没建（只有采集器碰过的新库）。这不是错误状态 ——
        # 前端要的是 latestForecastDate 和 counts，不该因为一张键值表就 500。
        # （踩过：裸查 meta 时，同类库上这个接口直接 500，Dashboard 拿不到基准日）
        try:
            meta_rows = con.execute("SELECT k, v FROM meta").fetchall()
        except sqlite3.Error as e:
            log.warning("meta 表不可读（%s），本次按空处理", e)
            meta_rows = []
        day = _latest_date(con, "forecast")
        counts = {
            "items": con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"],
            "cases": con.execute("SELECT COUNT(*) AS n FROM case_meta").fetchone()["n"],
            "forecast_today": con.execute(
                "SELECT COUNT(*) AS n FROM forecast WHERE date = (SELECT MAX(date) FROM forecast)"
            ).fetchone()["n"],
            "alerts_total": con.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"],
        }
        return ok({"latestForecastDate": day, "meta": {r["k"]: r["v"] for r in meta_rows}, "counts": counts})
    finally:
        con.close()


@app.get("/api/progress")
def progress() -> dict[str, Any]:
    con = db()
    try:
        return ok(rows_to_list(con.execute(
            "SELECT * FROM task_progress ORDER BY updated_at DESC LIMIT 20"
        ).fetchall()))
    finally:
        con.close()


@app.get("/api/market/index")
def market_index(days: int = Query(365, ge=7, le=3000)) -> dict[str, Any]:
    con = db()
    try:
        rows = con.execute(
            "SELECT date, index_value, online_players, vol_turnover FROM market_index ORDER BY date DESC LIMIT ?",
            (days,),
        ).fetchall()
        return ok(rows_to_list(reversed(rows)))
    finally:
        con.close()


@app.get("/api/candidates")
def candidates(
    date: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    side: str | None = None,
    sort: str = "score",
    dir: str = "desc",
    rarityMin: float | None = None,
) -> dict[str, Any]:
    con = db()
    try:
        if date is None:
            # 最近一次运行如果就是「无截面」，不能退回到 `MAX(date)` ——
            # 那会把**昨天的买入建议**当成今天的结果返回（审查发现的组合问题：
            # run_daily 那边清了今天的预测，接口这边自动往前找了昨天的）。
            # 历史记录仍然保留，只是默认视图必须承认「今天没有候选」。
            try:
                meta = {r["k"]: r["v"] for r in con.execute("SELECT k, v FROM meta")}
            except sqlite3.Error:
                meta = {}
            marker = meta.get("no_cross_section_on") or ""
            if marker and marker == (meta.get("last_run") or ""):
                log.info("最近一次运行（%s）无有效截面，返回空候选", marker)
                return ok({"date": marker, "rows": [],
                           "reason": "最近一次运行没有产生有效截面"})

        d = date or _latest_date(con, "forecast")
        if not d:
            return ok({"date": None, "rows": []})

        where = ["f.date = ?"]
        binds: list[Any] = [d]
        if side:
            where.append("f.side = ?")
            binds.append(side)
        if rarityMin is not None:
            where.append("COALESCE(fl.case_rarity_score, 0) >= ?")
            binds.append(rarityMin)
        # 用户规则：纪念品 / StatTrak 不看。推理截面已经滤过一遍，
        # 这里再兜一层是因为历史行（以及换口径之前写进去的行）还在库里。
        # DOMAIN_EXCLUDE_SQL 以 " AND " 开头，正好接在条件串后面。
        binds.append(limit)

        order_col = SORT_COLS.get(sort, SORT_COLS["score"])
        direction = "ASC" if dir.lower() == "asc" else "DESC"

        sql = f"""
            SELECT f.item_id, f.q10_adj, f.q50, f.q90_adj, f.prob_up, f.score, f.side, f.size, f.why,
                   i.name, i.market_hash_name, i.image, i.case_name, i.wear,
                   fl.price, fl.dd_from_high, fl.drop_duration_days, fl.turnover, fl.list_ratio,
                   fl.case_rarity_score, fl.rel_ret_21d
              FROM forecast f
              LEFT JOIN items i ON i.item_id = f.item_id
              LEFT JOIN factor_latest fl ON fl.item_id = f.item_id
             WHERE {' AND '.join(where)}""" + domain_exclude_sql() + f"""
             ORDER BY {order_col} {direction}
             LIMIT ?
        """
        return ok({"date": d, "rows": rows_to_list(con.execute(sql, binds).fetchall())})
    finally:
        con.close()


@app.get("/api/screener")
def screener(
    priceMax: float = 100.0,
    priceMin: float = 3.0,
    turnoverMin: float = 0.002,
    listRatioMax: float = 0.035,
    rarityMin: float = 60.0,
    limit: int = Query(100, ge=1, le=500),
) -> dict[str, Any]:
    con = db()
    try:
        # ⚠️ 这里对「当前没有数据源的列」必须放宽成 IS NULL 放行，不能 COALESCE 成 0。
        #    踩过的坑：turnover（换手率 = volume/supply）的条件原先写成
        #        COALESCE(fl.turnover, 0) >= 0.002
        #    而 snap 的 volume/supply 全表 NULL → turnover 恒为 0 → 0 >= 0.002 永假
        #    → 这个筛选器**永远返回空**，用户看到的是「候选页没有东西」，
        #    却完全看不出是被一个没有数据源的筛选条件卡死的。
        #    语义修正：字段缺失 = 「不知道」，不该当成「不合格」排除掉。
        rows = con.execute(
            """SELECT fl.item_id, fl.price, fl.dd_from_high, fl.drop_duration_days, fl.ret_21d,
                      fl.turnover, fl.list_ratio, fl.supply_peer_pct, fl.case_rarity_score,
                      fl.supply_rigidity, i.name, i.case_name, i.image
                 FROM factor_latest fl
                 LEFT JOIN items i ON i.item_id = fl.item_id
                WHERE fl.price BETWEEN ? AND ?
                  AND (fl.turnover   IS NULL OR fl.turnover   >= ?)
                  AND (fl.list_ratio IS NULL OR fl.list_ratio <= ?)
                  AND COALESCE(fl.case_rarity_score, 0) >= ?""" + domain_exclude_sql() + """
                ORDER BY fl.case_rarity_score DESC, fl.drop_duration_days DESC
                LIMIT ?""",
            (priceMin, priceMax, turnoverMin, listRatioMax, rarityMin, limit),
        ).fetchall()
        return ok(rows_to_list(rows))
    finally:
        con.close()


@app.get("/api/items/{item_id}")
def item_detail(item_id: str) -> dict[str, Any]:
    if not item_id:
        raise HTTPException(400, "id required")
    con = db()
    try:
        item = con.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
        if not item:
            raise HTTPException(404, "item not found")
        factor = con.execute("SELECT * FROM factor_latest WHERE item_id = ?", (item_id,)).fetchone()
        forecast = con.execute(
            "SELECT * FROM forecast WHERE item_id = ? ORDER BY date DESC LIMIT 1", (item_id,)
        ).fetchone()
        # 「最近一次运行没有产生有效截面」时，这条历史预测属于**旧建议**，
        # 不能当成当前结论展示（审查发现：候选列表已经返回空，
        # 但单品详情页仍然照旧返回昨天的 side=buy）。
        # 历史记录保留，只是明确标注为历史。
        try:
            meta = {r["k"]: r["v"] for r in con.execute("SELECT k, v FROM meta")}
        except sqlite3.Error:
            meta = {}
        marker = meta.get("no_cross_section_on") or ""
        fc_dict = dict(forecast) if forecast else None
        if fc_dict is not None:
            last_run = meta.get("last_run") or ""
            fc_dict["stale"] = bool(marker and marker == last_run
                                    and str(fc_dict.get("date")) < last_run)
            fc_dict["staleReason"] = ("最近一次运行没有产生有效截面，这条是历史预测"
                                      if fc_dict["stale"] else None)
        kase = con.execute(
            """SELECT cm.* FROM case_meta cm JOIN items i ON i.case_id = cm.case_id
                WHERE i.item_id = ?""",
            (item_id,),
        ).fetchone()
        alerts = con.execute(
            "SELECT level, score, ts, detail FROM alerts WHERE item_id = ? ORDER BY ts DESC LIMIT 20",
            (item_id,),
        ).fetchall()
        return ok({
            "item": dict(item),
            "factor": dict(factor) if factor else None,
            "forecast": fc_dict,
            "caseMeta": dict(kase) if kase else None,
            "alerts": rows_to_list(alerts),
        })
    finally:
        con.close()


@app.get("/api/items/{item_id}/kline")
def item_kline(item_id: str, days: int = Query(180, ge=7, le=2000)) -> dict[str, Any]:
    con = db()
    try:
        rows = con.execute(
            "SELECT date, o, h, l, c, v FROM kline WHERE item_id = ? ORDER BY date DESC LIMIT ?",
            (item_id, days),
        ).fetchall()
        return ok(rows_to_list(reversed(rows)))
    finally:
        con.close()


@app.get("/api/alerts")
def alerts(limit: int = Query(100, ge=1, le=500), level: str | None = None) -> dict[str, Any]:
    con = db()
    try:
        if level:
            rows = con.execute(
                "SELECT * FROM alert_view WHERE level = ? ORDER BY ts DESC LIMIT ?", (level, limit)
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM alert_view ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return ok(rows_to_list(rows))
    finally:
        con.close()


@app.get("/api/cases")
def cases(limit: int = Query(100, ge=1, le=500), sort: str = "rarity") -> dict[str, Any]:
    con = db()
    try:
        order = "case_volume_30d DESC" if sort == "volume" else "rarity_score DESC"
        rows = con.execute(f"SELECT * FROM case_meta ORDER BY {order} LIMIT ?", (limit,)).fetchall()
        return ok(rows_to_list(rows))
    finally:
        con.close()


@app.get("/api/backtest")
def backtest() -> dict[str, Any]:
    con = db()
    try:
        return ok(rows_to_list(con.execute("SELECT * FROM backtest ORDER BY date ASC").fetchall()))
    finally:
        con.close()


@app.get("/api/search")
def search(q: str = Query("", max_length=60)) -> dict[str, Any]:
    if len(q) < 2:
        return ok([])
    con = db()
    try:
        like = f"%{q}%"
        rows = con.execute(
            """SELECT item_id, name, market_hash_name, case_name, image FROM items
                WHERE name LIKE ? OR market_hash_name LIKE ? LIMIT 30""",
            (like, like),
        ).fetchall()
        return ok(rows_to_list(rows))
    finally:
        con.close()


# ---------------------------------------------------------------- 管理
@app.post("/api/ingest")
def ingest(payload: dict, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """本地写入入口（与 Worker 的 /api/ingest 同语义），便于其他服务推送"""
    _auth(authorization)
    stage = str(payload.get("stage", ""))
    rows = payload.get("rows") or []
    sys.path.insert(0, str(HERE))
    from push import to_local

    n = to_local.push(
        stage, rows,
        replace_date=payload.get("replaceDate"),
        replace_series=payload.get("replaceSeries"),
    )
    return ok({"stage": stage, "inserted": n})


@app.post("/admin/refresh")
def admin_refresh(payload: dict | None = None, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """触发一次完整管线（后台）"""
    _auth(authorization)
    log.info("收到刷新触发：%s", payload)
    proc = subprocess.Popen(
        [sys.executable, "-m", "pipeline.run_daily"],
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return ok({"pid": proc.pid, "trigger": (payload or {}).get("trigger")})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("API_PORT", "8788")))
