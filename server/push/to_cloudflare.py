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
    replace_since_ts: int | None = None,
    replace_all: bool = False,
) -> int:
    """把一个 stage 的数据推送到 D1，返回写入行数。

    Args:
        stage: ingest.ts 中 STAGES 的键（snap_daily / forecast / items / ...）
        replace_date: 先删掉该日期的旧数据（幂等重跑）
        replace_series: 先删掉该 item_id 的旧数据（items / case_meta / factor_latest 用）
        replace_since_ts: 先删掉 ts >= 该值的旧数据（alerts 这种只有 ts 的表用）
        replace_all: 整表替换（只允许「当前截面」型表，见 Worker 的 REPLACE_ALL_STAGES）
    """
    rows = list(rows)
    has_replace = bool(replace_all or replace_date or replace_series
                       or replace_since_ts is not None)
    if not rows and not has_replace:
        log.info("stage=%s 无数据，跳过", stage)
        return 0

    # ⚠️ rows 为空但带 replace_* 时**必须照发**（审查 9 发现）：
    # 「本轮没有行」≠「本轮不该有行」。原先这里直接 return，
    # 于是 alerts 的 replaceSince、forecast 的 replaceDate、
    # factor_latest 的 replaceAll 在空批次下全部丢失 ——
    # 本地已经撤回的告警/建议会在云端继续挂着（本地那句 DELETE 是无条件的）。
    # 空行也要发，Worker 侧同样要先删后插（见 ingest.ts 的同款注释）。
    inserted = 0
    total_chunks = max(1, (len(rows) + CHUNK - 1) // CHUNK)
    # range 在 len(rows)==0 时是空的 —— 所以空批次必须单独发一次，
    # 否则「只删不插」这个语义根本到不了 Worker。
    offsets = list(range(0, len(rows), CHUNK)) or [0]
    for i in offsets:
        chunk = rows[i : i + CHUNK]
        # replace_* 只在第一块传，避免重复删除
        first = i == 0
        payload: dict[str, Any] = {"stage": stage, "rows": chunk}
        if first and replace_date:
            payload["replaceDate"] = replace_date
        if first and replace_series:
            payload["replaceSeries"] = replace_series
        if first and replace_since_ts is not None:
            payload["replaceSince"] = replace_since_ts
        if first and replace_all:
            payload["replaceAll"] = True
        res = _post(payload)
        inserted += int(res.get("inserted", 0))
        if chunk:
            log.info("stage=%s 分块 %d/%d 写入 %d 行", stage, i // CHUNK + 1, total_chunks, res.get("inserted", 0))
        else:
            log.info("stage=%s 空批次：只执行 replace_* 删除（删 %s 行）", stage, res.get("deleted"))

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
