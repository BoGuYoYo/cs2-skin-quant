"""推送通道：服务器 → Cloudflare Worker → D1。

为什么不让 Python 直连 D1 REST API：
  - D1 REST 一次只能执行一条语句，几万行要几万次 HTTP，慢且容易超时；
  - Worker 的 /api/ingest 内部用 db.batch()，900 条语句一个往返，快一个数量级。
所以走 Worker 中转，Python 侧只负责分块。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable, Sequence

import requests

log = logging.getLogger("push")

PUSH_URL = os.environ.get("CF_PUSH_URL", "").rstrip("/")
TOKEN = os.environ.get("INGEST_TOKEN", "")

# 单次请求携带的行数。太大 → Worker CPU 超限；太小 → 请求数爆炸。500 是实测的平衡点。
CHUNK = int(os.environ.get("PUSH_CHUNK", "500"))
RETRIES = 3


class PushError(RuntimeError):
    pass


def _post(payload: dict[str, Any]) -> dict[str, Any]:
    if not PUSH_URL or not TOKEN:
        raise PushError("CF_PUSH_URL / INGEST_TOKEN 未配置，先在 .env 里填好")

    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(
                f"{PUSH_URL}/api/ingest",
                json=payload,
                headers={
                    "authorization": f"Bearer {TOKEN}",
                    "content-type": "application/json",
                },
                timeout=60,
            )
            if r.status_code == 401:
                raise PushError("推送被拒（401）：INGEST_TOKEN 与 Worker secret 不一致")
            data = r.json()
            if not r.ok or not data.get("ok"):
                raise PushError(f"HTTP {r.status_code}: {data.get('error') or r.text[:300]}")
            return data["data"]
        except PushError:
            raise
        except Exception as e:  # 网络抖动重试
            last = e
            sleep = 2 ** attempt
            log.warning("push 第 %d 次失败（%s），%ds 后重试", attempt, e, sleep)
            time.sleep(sleep)
    raise PushError(f"推送失败，已重试 {RETRIES} 次：{last}")


def push(
    stage: str,
    rows: Sequence[dict[str, Any]] | Iterable[dict[str, Any]],
    *,
    replace_date: str | None = None,
    replace_series: str | None = None,
) -> int:
    """把一个 stage 的数据推送到 D1，返回写入行数。

    Args:
        stage: ingest.ts 中 STAGES 的键（snap_daily / forecast / items / ...）
        replace_date: 先删掉该日期的旧数据（幂等重跑）
        replace_series: 先删掉该 item_id 的旧数据（items / case_meta / factor_latest 用）
    """
    rows = list(rows)
    if not rows:
        log.info("stage=%s 无数据，跳过", stage)
        return 0

    inserted = 0
    total_chunks = (len(rows) + CHUNK - 1) // CHUNK
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i : i + CHUNK]
        # replace_* 只在第一块传，避免重复删除
        first = i == 0
        payload: dict[str, Any] = {"stage": stage, "rows": chunk}
        if first and replace_date:
            payload["replaceDate"] = replace_date
        if first and replace_series:
            payload["replaceSeries"] = replace_series
        res = _post(payload)
        inserted += int(res.get("inserted", 0))
        log.info("stage=%s 分块 %d/%d 写入 %d 行", stage, i // CHUNK + 1, total_chunks, res.get("inserted", 0))

    log.info("stage=%s 完成，共写入 %d 行", stage, inserted)
    return inserted


def push_many(items: dict[str, Sequence[dict[str, Any]]]) -> dict[str, int]:
    """按顺序推送多个 stage（顺序有讲究：先 master data 再明细）"""
    out: dict[str, int] = {}
    for stage, rows in items.items():
        out[stage] = push(stage, rows)
    return out


if __name__ == "__main__":  # 手动冒烟测试
    logging.basicConfig(level=logging.INFO)
    print(push("runs", [{"ts": int(time.time()), "stage": "smoke", "ok": 1, "rows": 1, "message": "push channel ok"}]))
