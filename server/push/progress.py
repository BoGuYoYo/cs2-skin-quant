"""任务进度上报 —— 让前端能实时看到回填 / 采集 / 训练的进展。

设计取舍：
    不做本地状态累积，每次直接把「当前完整快照」upsert 到 D1 的 task_progress。
    这样断线重连、进程重启都不会丢进度，前端也只需要读最新一行。
    上报失败**绝不**影响主流程（回填跑 3 小时，不能因为一次 HTTP 失败就中断）。
"""
from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger("progress")

#: 同一任务的进度最多这么频繁地上报一次，避免把 D1 写入额度耗光
_min_interval = 20.0
_last: dict[str, float] = {}


def report(task: str, status: str, force: bool = False, **fields: Any) -> None:
    """上报任务进度。字段为 None 的会被丢弃（不覆盖已有值）。"""
    now = time.time()
    if not force and now - _last.get(task, 0.0) < _min_interval:
        return
    _last[task] = now

    try:
        from . import dispatch  # 延迟导入，避免启动时的循环依赖

        row: dict[str, Any] = {"task": task, "status": status, "updated_at": int(now)}
        for k, v in fields.items():
            if v is not None:
                row[k] = round(v, 4) if isinstance(v, float) else v
        dispatch.push("task_progress", [row])
    except Exception as e:
        log.debug("进度上报失败（不影响主流程）：%s", e)


def finish(task: str, message: str = "", **fields: Any) -> None:
    report(task, "done", force=True, message=message, **fields)


def fail(task: str, message: str, **fields: Any) -> None:
    report(task, "failed", force=True, message=message[:300], **fields)


def progress_fields(
    i: int, total: int, t0: float, ok: int, empty: int, fail: int, rows: int, started_at: int
) -> dict[str, Any]:
    """由循环变量算出速率的 ETA —— 让前端能直接显示「剩余 X 分钟」"""
    elapsed = max(time.time() - t0, 1e-6)
    rate = i / elapsed * 60.0
    eta = (total - i) / rate if rate > 0 and i < total else None
    return {
        "total": total,
        "done": i,
        "ok": ok,
        "empty": empty,
        "fail": fail,
        "rows": rows,
        "rate_per_min": round(rate, 1),
        "eta_minutes": round(eta, 1) if eta is not None else None,
        "started_at": started_at,
    }
