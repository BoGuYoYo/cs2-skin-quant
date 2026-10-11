"""推送分发：按 PUSH_MODE 选择走哪条通道。

    PUSH_MODE=local    写服务器本地 SQLite（自托管 API 用，毫秒级）
    PUSH_MODE=d1       直连 Cloudflare D1 REST API（需要 api.cloudflare.com 可达）
    PUSH_MODE=worker   走自己的 Worker /api/ingest（需要 workers.dev 可达）
    PUSH_MODE=both     本地 + D1 双写（推荐：本地服务前端，D1 作云端备份）
    PUSH_MODE=none     只算不推（调试）

判断依据（在服务器上跑）：
    curl -sS -m 15 -o /dev/null -w "%{http_code}\\n" https://skin-alpha.<子域>.workers.dev/api/health
    curl -sS -m 15 -o /dev/null -w "%{http_code}\\n" https://api.cloudflare.com/client/v4/
    前者 000 而后者 400/301 → 用 d1（或 both）
"""
from __future__ import annotations

import logging
import os
from typing import Any, Sequence

log = logging.getLogger("push")

MODE = os.environ.get("PUSH_MODE", "worker").strip().lower() or "worker"

_VALID = {"local", "worker", "d1", "both", "none"}

#: 不进 D1 的表。
#: D1 免费额度只有 10 万行写入/天，而 kline 一次全量就是 150 万行 ——
#: 直接推会把额度一次打满并触发 7500 错误（踩过）。
#: 这些大表只写本地 SQLite，D1 只保留预测 / 告警 / 元数据这类「小结果」。
D1_SKIP_STAGES = {
    s.strip() for s in os.environ.get("D1_SKIP_STAGES", "kline,snap_daily").split(",") if s.strip()
}


def _worker():
    from . import to_cloudflare
    return to_cloudflare


def _d1():
    from . import to_d1
    return to_d1


def _local():
    from . import to_local
    return to_local


def describe() -> str:
    if MODE not in _VALID:
        return f"PUSH_MODE='{MODE}' 无效（可选 {', '.join(sorted(_VALID))}），将退化为 none"
    return {
        "local": "本地 SQLite 通道（自托管 API 用）",
        "worker": "Worker /api/ingest 通道",
        "d1": "Cloudflare D1 REST API 通道",
        "both": "双写：本地 SQLite + Cloudflare D1",
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
        log.info("stage=%s 跳过（PUSH_MODE=none）", stage)
        return 0

    rows = list(rows) if rows is not None else []
    total = 0
    errors: list[str] = []

    # 本地优先：它是前端的第一数据源，失败必须暴露出来
    if MODE in ("local", "both"):
        try:
            total += _local().push(stage, rows, replace_date=replace_date, replace_series=replace_series)
        except Exception as e:
            errors.append(f"local: {e}")
            log.warning("本地通道失败（stage=%s）：%s", stage, e)

    # both = 本地 + D1。故意不含 worker：workers.dev 在多数网络不可达，
    # 且它和 d1 写的是同一份数据，双写没有意义（踩过：both 触发三条通道，
    # worker 每次超时 60 秒拖慢整条管线）
    if MODE == "worker":
        try:
            total += _worker().push(stage, rows, replace_date=replace_date, replace_series=replace_series)
        except Exception as e:
            errors.append(f"worker: {e}")
            log.warning("Worker 通道失败（stage=%s）：%s", stage, e)

    if MODE in ("d1", "both"):
        if stage in D1_SKIP_STAGES:
            log.info("stage=%s 跳过 D1（大表，免费额度 10 万行/天，见 D1_SKIP_STAGES）", stage)
        else:
            try:
                total += _d1().push(stage, rows, replace_date=replace_date, replace_series=replace_series)
            except Exception as e:
                errors.append(f"d1: {e}")
                log.warning("D1 通道失败（stage=%s）：%s", stage, e)

    # 只要有一条通道成功就算成功（both 模式下 Cloudflare 挂了不该让管线失败）
    if errors and total == 0:
        raise RuntimeError("; ".join(errors))
    return total


def check() -> dict[str, Any]:
    """连通性自检，供 run_daily --check-push 调用"""
    out: dict[str, Any] = {"mode": MODE, "describe": describe()}
    if MODE in ("local", "both"):
        try:
            out["local"] = _local().check()
        except Exception as e:
            out["local"] = {"ok": False, "error": str(e)}
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
