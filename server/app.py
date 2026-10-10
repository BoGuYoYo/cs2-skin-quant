"""服务器侧只读 API（可选组件）。

什么时候需要它：
  - 你想在网页上看到「算完模型但还没推送」的实时结果
  - 你想用 Cloudflare Cron 反向触发服务器跑管线
不需要的话，整个文件可以删掉 —— 主链路（管线 → /api/ingest → D1）完全不依赖它。

部署：
  uvicorn app:app --host 127.0.0.1 --port 8788
  cloudflared tunnel --url http://127.0.0.1:8788     # 出站连接，不开任何入站端口
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("api")

app = FastAPI(title="skin-alpha server api", version="1.0.0")

TOKEN = os.environ.get("INGEST_TOKEN", "")
HERE = Path(__file__).resolve().parent


def _auth(authorization: str | None) -> None:
    """Tunnel 暴露到公网，必须自己带鉴权 —— Cloudflare Access 是更省事的替代方案"""
    if not TOKEN:
        raise HTTPException(500, "INGEST_TOKEN 未在服务器上配置")
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(401, "unauthorized")


@app.get("/health")
def health():
    db = HERE / os.environ.get("DB_PATH", "./state/skins.db")
    return {
        "ok": True,
        "db_exists": db.exists(),
        "db_size_mb": round(db.stat().st_size / 1e6, 2) if db.exists() else 0,
        "model_exists": (HERE / os.environ.get("MODEL_PATH", "./state/model.pkl")).exists(),
        "llm_enabled": bool(os.environ.get("LLM_API_KEY")),
        "now": int(time.time()),
    }


@app.get("/candidates/live")
def candidates_live(
    limit: int = Query(50, ge=1, le=500),
    authorization: str | None = Header(default=None),
):
    """直接从服务器本地库读最新一天的预测（绕过 D1，用于实时对照）"""
    _auth(authorization)
    import sqlite3

    db = HERE / os.environ.get("DB_PATH", "./state/skins.db")
    if not db.exists():
        raise HTTPException(404, "本地库不存在，管线还没跑过")
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT s.item_id, s.sell AS price, s.date
             FROM snap s
            WHERE s.date = (SELECT MAX(date) FROM snap)
            ORDER BY s.sell DESC
            LIMIT ?""",
        (limit,),
    ).fetchall()
    con.close()
    return {"ok": True, "rows": [dict(r) for r in rows]}


@app.post("/admin/refresh")
def admin_refresh(
    payload: dict | None = None,
    authorization: str | None = Header(default=None),
):
    """由 Cloudflare Cron 触发：后台跑一次完整管线"""
    _auth(authorization)
    log.info("收到刷新触发：%s", payload)
    # 用 Popen 不阻塞响应；真实项目里换成队列或 systemd unit
    proc = subprocess.Popen(
        [sys.executable, "-m", "pipeline.run_daily"],
        cwd=str(HERE),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "pid": proc.pid, "trigger": (payload or {}).get("trigger")}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("API_PORT", "8788")))
