"""推送分发：按 PUSH_MODE 选择走哪条通道。

    PUSH_MODE=worker   走自己的 Worker /api/ingest（快，一次 500 行；需要 workers.dev 可达）
    PUSH_MODE=d1       直连 Cloudflare D1 REST API（需要 api.cloudflare.com 可达）
    PUSH_MODE=both     两条都推（幂等，用于过渡期双保险）
    PUSH_MODE=none     只算不推（调试）

判断依据（在服务器上跑）：
    curl -sS -m 15 -o /dev/null -w "%{http_code}\\n" https://skin-alpha.<子域>.workers.dev/api/health
    curl -sS -m 15 -o /dev/null -w "%{http_code}\\n" https://api.cloudflare.com/client/v4/
    前者 000 而后者 400/301 → 用 d1
"""
from __future__ import annotations

import logging
import os
from typing import Any, Sequence

log = logging.getLogger("push")

MODE = os.environ.get("PUSH_MODE", "worker").strip().lower() or "worker"

_VALID = {"worker", "d1", "both", "none"}


def _worker():
    from . import to_cloudflare
    return to_cloudflare


def _d1():
    from . import to_d1
    return to_d1


def describe() -> str:
    if MODE not in _VALID:
        return f"PUSH_MODE='{MODE}' 无效（可选 {', '.join(sorted(_VALID))}），将退化为 none"
    return {
        "worker": "Worker /api/ingest 通道",
        "d1": "Cloudflare D1 REST API 通道",
        "both": "双通道（Worker + D1 REST）",
        "none": "不推送（仅本地计算）",
    }[MODE]


def push(
    stage: str,
    rows: Sequence[dict[str, Any]] | Any,
    *,
    replace_date: str | None = None,
    replace_series: str | None = None,
) -> int:
    if MODE == "none":
        log.info("stage=%s 跳过（PUSH_MODE=none），%d 行", stage, len(list(rows)) if rows is not None else 0)
        return 0

    rows = list(rows) if rows is not None else []
    total = 0
    errors: list[str] = []

    if MODE in ("worker", "both"):
        try:
            total += _worker().push(stage, rows, replace_date=replace_date, replace_series=replace_series)
        except Exception as e:
            errors.append(f"worker: {e}")
            log.warning("Worker 通道失败（stage=%s）：%s", stage, e)

    if MODE in ("d1", "both"):
        try:
            total += _d1().push(stage, rows, replace_date=replace_date, replace_series=replace_series)
        except Exception as e:
            errors.append(f"d1: {e}")
            log.warning("D1 通道失败（stage=%s）：%s", stage, e)

    # both 模式下只要有一条成功就算成功
    if errors and total == 0:
        raise RuntimeError("; ".join(errors))
    return total


def check() -> dict[str, Any]:
    """连通性自检，供 run_daily --check-push 调用"""
    out: dict[str, Any] = {"mode": MODE, "describe": describe()}
    if MODE in ("worker", "both"):
        try:
            import requests
            url = os.environ.get("CF_PUSH_URL", "").rstrip("/")
            r = requests.get(f"{url}/api/health", timeout=20)
            out["worker"] = {"ok": r.status_code == 200, "http": r.status_code, "url": url}
        except Exception as e:
            out["worker"] = {"ok": False, "error": str(e), "url": os.environ.get("CF_PUSH_URL", "")}
    if MODE in ("d1", "both"):
        try:
            out["d1"] = _d1().check()
        except Exception as e:
            out["d1"] = {"ok": False, "error": str(e)}
    return out
