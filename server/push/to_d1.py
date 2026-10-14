"""直连 Cloudflare D1 REST API 推送数据。

为什么需要这条通道：
    *.workers.dev 在部分网络（含国内机房）被 SNI 阻断，Worker 的 /api/ingest 走不通；
    但 api.cloudflare.com 通常可达 —— 实测服务器上返回 400/301（连接成功）。
    所以当 Worker 通道不可用时，直接调 D1 REST API 写入。

两条通道的对比：
    to_cloudflare.py  → POST 到自己的 Worker，由 Worker 用 db.batch() 写 D1
                        （每次 500 行，请求少；依赖 workers.dev 域名可达）
    to_d1.py（本文件） → 直接打 Cloudflare API，每次一条多值 INSERT
                        （请求多一些；只要 api.cloudflare.com 可达就行）

所需环境变量：
    CF_ACCOUNT_ID        Cloudflare 账户 ID
    CF_D1_DATABASE_ID    D1 数据库 ID
    CF_API_TOKEN         API Token（权限：Account → D1 → Edit）

接口与 to_cloudflare.push() 完全一致，可直接替换。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Iterable, Sequence

import requests

log = logging.getLogger("push.d1")

ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "").strip()
DATABASE_ID = os.environ.get("CF_D1_DATABASE_ID", "").strip()
API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()

API_BASE = "https://api.cloudflare.com/client/v4"

# D1 的实测限制（踩过的坑，别改回去）：
#   - 参数绑定（?）单条语句最多 100 个 → 19 列的表一次只能插 5 行，200 万行要 40 万次请求
#   - 所以默认走「字面量 SQL」：值直接拼进语句、自己做转义，只受 SQL 长度限制
#     实测 90KB 的语句可以正常执行，1200 行 kline 一次搞定
MAX_PARAMS = int(os.environ.get("D1_MAX_PARAMS", "100"))
MAX_SQL_LEN = int(os.environ.get("D1_MAX_SQL_LEN", "90000"))
USE_PARAMS = os.environ.get("D1_USE_PARAMS", "0") == "1"
RETRIES = 3

# 与 worker/routes/ingest.ts 的 STAGES 保持一致：列名与顺序必须相同
STAGES: dict[str, list[str]] = {
    "items": ["item_id", "name", "market_hash_name", "wear", "case_id", "case_name", "image", "updated_at"],
    "case_meta": ["case_id", "case_name", "rarity_score", "case_price", "case_volume_30d",
                  "supply_growth_30d", "drop_pool", "rationale", "blocking_risk", "updated_at"],
    "snap_daily": ["item_id", "date", "open", "high", "low", "close", "bid", "listings", "volume", "supply"],
    "kline": ["item_id", "date", "o", "h", "l", "c", "v"],
    "forecast": ["item_id", "date", "horizon", "q10", "q50", "q90", "q10_adj", "q90_adj",
                 "prob_up", "fwd_max_dd", "score", "side", "size", "why", "model_version"],
    "factor_latest": ["item_id", "date", "price", "dd_from_high", "drop_duration_days", "ret_7d",
                      "ret_21d", "ret_30d", "vol_21", "turnover", "list_ratio", "list_slope_7d",
                      "supply_peer_pct", "supply_rigidity", "case_rarity_score", "rel_ret_21d",
                      "event_bearish_30d", "event_bullish_30d", "updated_at"],
    "alerts": ["item_id", "name", "level", "score", "ts", "detail", "pushed"],
    "market_index": ["date", "index_value", "online_players", "vol_turnover"],
    "backtest": ["date", "mean_21d", "cum", "win_rate", "n", "model_version"],
    "meta": ["k", "v", "updated_at"],
    "runs": ["ts", "stage", "ok", "rows", "message"],
    "task_progress": ["task", "status", "total", "done", "ok", "empty", "fail",
                      "rows", "rate_per_min", "eta_minutes", "message", "started_at", "updated_at"],
}


class D1Error(RuntimeError):
    pass


class PayloadTooLarge(D1Error):
    """语句过大 —— 上层据此自动减半批次重试"""


# ------------------------------------------------------------------ SQL 构造
def _coerce(v: Any) -> Any:
    """归一化成 SQLite 能安全接受的原生类型。

    ⚠️ 关键坑（踩过）：
        numpy 2.x 下 repr(np.float64(-0.2645)) == 'np.float64(-0.2645)'。
        而 isinstance(np.float64(...), float) 是 True，所以会走进 float 分支，
        repr 出来后拼进 SQL 就是 `np.float64(-0.2645)` —— 直接语法错误：
            d1: 7500: near "(": syntax error at offset 185
        所以必须先用 .item() 还原成 Python 原生标量。
        不 import numpy 是为了让推送模块保持零重依赖，用 __module__ 判断即可。
    """
    if v is None:
        return None

    tmod = type(v).__module__
    if (tmod.startswith("numpy") or tmod.startswith("pandas")) and hasattr(v, "item"):
        try:
            v = v.item()
        except (ValueError, TypeError):
            v = str(v)

    if v is None:
        return None
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        # NaN / inf 不能进 SQLite
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v
    if isinstance(v, (dict, list, tuple)):
        return json.dumps(v, ensure_ascii=False)
    if not isinstance(v, str):
        return str(v)
    return v


def build_insert(table: str, cols: list[str], rows: Sequence[dict[str, Any]]) -> tuple[str, list[Any]]:
    """生成多值 INSERT OR REPLACE 语句 + 位置参数数组"""
    placeholders = "(" + ",".join(["?"] * len(cols)) + ")"
    sql = (
        f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES "
        + ",".join([placeholders] * len(rows))
    )
    params: list[Any] = []
    for r in rows:
        for c in cols:
            params.append(_coerce(r.get(c)))
    return sql, params


def _batch_size(n_cols: int, n_rows: int) -> int:
    by_params = max(1, MAX_PARAMS // max(n_cols, 1))
    by_len = max(1, MAX_SQL_LEN // max(n_cols * 16, 1))   # 每个值粗估 16 字符
    return max(1, min(n_rows, by_params, by_len))


# ------------------------------------------------- 字面量模式（默认，快）
def _sql_literal(v: Any) -> str:
    """把 Python 值转成 SQL 字面量。

    SQLite 的字符串只需转义单引号（'' 表示一个 '），反斜杠不是转义字符，
    所以这里不能像 MySQL 那样处理反斜杠 —— 否则中文和 Windows 路径会被写坏。
    """
    v = _coerce(v)          # 防御性归一化：直接调用时也要能正确处理 numpy 标量
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)      # Python float 的 repr 保证 round-trip
    return "'" + str(v).replace("'", "''") + "'"


def build_insert_literal(
    table: str, cols: list[str], rows: Sequence[dict[str, Any]], max_len: int | None = None
) -> list[str]:
    """按 SQL 长度切分成若干条多值 INSERT，返回语句列表"""
    limit = max_len if max_len is not None else MAX_SQL_LEN
    prefix = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES "
    out: list[str] = []
    buf: list[str] = []
    cur = len(prefix)

    for r in rows:
        tup = "(" + ",".join(_sql_literal(_coerce(r.get(c))) for c in cols) + ")"
        if buf and cur + len(tup) + 1 > limit:
            out.append(prefix + ",".join(buf))
            buf, cur = [], len(prefix)
        buf.append(tup)
        cur += len(tup) + 1

    if buf:
        out.append(prefix + ",".join(buf))
    return out


# ------------------------------------------------------------------ HTTP
def _headers() -> dict[str, str]:
    if not API_TOKEN:
        raise D1Error("CF_API_TOKEN 未配置")
    return {"Authorization": f"Bearer {API_TOKEN}", "Content-Type": "application/json"}


def _endpoint() -> str:
    if not ACCOUNT_ID or not DATABASE_ID:
        raise D1Error("CF_ACCOUNT_ID / CF_D1_DATABASE_ID 未配置")
    return f"{API_BASE}/accounts/{ACCOUNT_ID}/d1/database/{DATABASE_ID}/query"


def _post(sql: str, params: list[Any] | None = None) -> tuple[list[dict[str, Any]], int]:
    """执行一条语句，返回 (结果行列表, 受影响行数)。

    注意区分这两者：对 SELECT 而言 meta.changes 恒为 0，
    所以「查行数」必须用返回值里的 results，不能用 changes
    （之前 check() 用 changes 做探针，导致永远显示 0，看起来像空库）。
    """
    body: dict[str, Any] = {"sql": sql}
    if params:
        body["params"] = params

    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.post(_endpoint(), headers=_headers(), json=body, timeout=90)
            if r.status_code == 401 or r.status_code == 403:
                raise D1Error(f"鉴权失败（HTTP {r.status_code}）：检查 CF_API_TOKEN 权限是否为 Account→D1→Edit")
            if r.status_code == 413:
                raise PayloadTooLarge("请求体过大")

            data = r.json()
            if not data.get("success"):
                errs = data.get("errors") or []
                msg = "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errs) or r.text[:300]
                # D1 对超限的报错文案不固定，用关键词识别以便降级重试
                low = msg.lower()
                if any(k in low for k in ("too large", "too many", "exceed", "too long", "limit")):
                    raise PayloadTooLarge(msg)
                raise D1Error(msg)

            results = data.get("result") or []
            if results and not results[0].get("success", True):
                raise D1Error(str(results[0].get("error") or results[0]))

            rows = (results[0].get("results") or []) if results else []
            changes = int(((results[0].get("meta") or {}).get("changes", 0)) or 0)
            return rows, changes

        except PayloadTooLarge:
            raise
        except D1Error:
            raise
        except Exception as e:            # 网络抖动重试
            last = e
            sleep = 2 ** attempt
            log.warning("D1 请求失败（第 %d 次）：%s，%ds 后重试", attempt, e, sleep)
            time.sleep(sleep)
    raise D1Error(f"D1 请求失败，已重试 {RETRIES} 次：{last}")


def _query(sql: str, params: list[Any] | None = None) -> int:
    """执行写语句，返回受影响行数"""
    return _post(sql, params)[1]


def fetch(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """执行查询语句，返回结果行。

    这是「读」的正确入口 —— 别再用 _query 读数据，它只会给你 0。
    """
    return _post(sql, params)[0]


def fetch_one(sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
    rows = fetch(sql, params)
    return rows[0] if rows else None


# ------------------------------------------------------------------ 对外接口
def push(
    stage: str,
    rows: Sequence[dict[str, Any]] | Iterable[dict[str, Any]],
    *,
    replace_date: str | None = None,
    replace_series: str | None = None,
    replace_since_ts: int | None = None,
    replace_all: bool = False,
) -> int:
    """把一个 stage 的数据推送到 D1（接口与 to_cloudflare.push 一致）"""
    cols = STAGES.get(stage)
    if cols is None:
        raise D1Error(f"未知 stage：{stage}（可用：{', '.join(STAGES)}）")

    rows = list(rows)

    table = _table_of(stage)
    changed = 0

    # 幂等重跑：先删旧的
    #
    # ⚠️ 这一段必须在 `if not rows: return 0` **之前**（审查 9 发现）。
    # 原先空列表直接 return，于是「本轮不该有行」的删除语义整批丢失：
    #   - alerts（只有 ts）：某天异动归零时 replaceSince 被跳过，
    #     本地已撤回的告警在 D1 里继续挂着；
    #   - forecast / factor_latest：abort_no_cross_section 想把
    #     「今天没有截面」推上云端，只能推空列表 —— 同样被跳过。
    # 删除与插入是两件事，不能共用「有没有行」这一个前置条件。
    if replace_all:
        changed += _query(f"DELETE FROM {table}", [])
    if replace_date and "date" in cols:
        changed += _query(f"DELETE FROM {table} WHERE date = ?", [replace_date])
    if replace_series and "item_id" in cols:
        changed += _query(f"DELETE FROM {table} WHERE item_id = ?", [replace_series])
    # alerts 这类「只有 ts、没有 date」的表按时间戳划界删（否则同日重跑会翻倍）
    if replace_since_ts is not None and "ts" in cols:
        changed += _query(f"DELETE FROM {table} WHERE ts >= ?", [replace_since_ts])
    elif replace_since_ts is not None:
        log.warning("stage=%s 不支持 replace_since_ts（没有 ts 列），本次未做按时间替换", stage)

    if not rows:
        log.info("stage=%s 无数据，跳过插入（删除 %d 行）", stage, changed)
        return changed

    if USE_PARAMS:
        # 参数绑定模式：每语句最多 100 个参数，慢但完全免注入（D1_USE_PARAMS=1 才启用）
        batch = _batch_size(len(cols), len(rows))
        for i in range(0, len(rows), batch):
            sql, params = build_insert(table, cols, rows[i : i + batch])
            changed += _query(sql, params)
        log.info("stage=%s 完成（参数绑定模式），写入 %d 行", stage, changed)
        return changed

    # 字面量模式（默认）：按 SQL 长度切分，实测 90KB 语句可执行
    limit = MAX_SQL_LEN
    while True:
        stmts = build_insert_literal(table, cols, rows, limit)
        total = 0
        try:
            for s in stmts:
                total += _query(s)
            changed += total
            log.info("stage=%s 完成，%d 行 / %d 条语句（上限 %d 字节）",
                     stage, len(rows), len(stmts), limit)
            return changed
        except PayloadTooLarge as e:
            if limit <= 4096:
                raise D1Error(f"stage={stage} 语句降到 {limit} 字节仍超限：{e}")
            limit //= 2
            log.warning("stage=%s 语句过大，SQL 长度上限降到 %d 字节后重试", stage, limit)


def _table_of(stage: str) -> str:
    """表名与 stage 名一一对应 —— 直接用 stage 当表名。

    这里曾经维护过一份 _TABLE_MAP 映射表，加新表时容易漏改
    （踩过：加了 task_progress 到 STAGES 但忘了加映射，运行时报 KeyError）。
    stage 本身已在 STAGES 里做过白名单校验，所以直接返回是安全的。
    那份映射表因为已经没有任何调用方（死代码，还会让人以为改表名要来这里）
    而被删除 —— 需要时从 git 历史取回。
    """
    return stage


def push_many(items: dict[str, Sequence[dict[str, Any]]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for stage, rows in items.items():
        out[stage] = push(stage, rows)
    return out


def check() -> dict[str, Any]:
    """连通性自检：真的读一次 D1，确认 token 与库 ID 都对"""
    row = fetch_one("SELECT COUNT(*) AS n FROM items")
    n = int((row or {}).get("n", 0) or 0)
    latest = fetch_one("SELECT MAX(date) AS d FROM forecast")
    return {
        "ok": True,
        "database": DATABASE_ID,
        "items": n,
        "latest_forecast": (latest or {}).get("d"),
    }


# ------------------------------------------------------------------ 自检
def _selftest() -> int:
    """不联网验证 SQL 生成逻辑"""
    rows = [
        {"item_id": "a", "name": "测试'引号", "market_hash_name": "X | Y (Factory New)",
         "wear": "Factory New", "case_id": None, "case_name": "O'Brien Case",
         "image": None, "updated_at": 123},
        {"item_id": "b", "name": "第二件", "market_hash_name": "P | Q (Minimal Wear)",
         "wear": "Minimal Wear", "case_id": "c1", "case_name": "Case2",
         "image": "http://x/y.png", "updated_at": 124},
    ]

    print("── 参数绑定模式 ──")
    sql, params = build_insert("items", STAGES["items"], rows)
    assert len(params) == 16, f"参数个数应为 16，实际 {len(params)}"
    assert sql.count("?") == 16
    assert params[1] == "测试'引号"
    assert params[4] is None and params[6] is None
    print("  ✓ 16 参数 / 16 占位符，None 透传正常")

    print("── 字面量模式（默认）──")
    stmts = build_insert_literal("items", STAGES["items"], rows, max_len=200)
    print("  语句:", stmts[0][:110], "…")
    assert "'测试''引号'" in stmts[0], f"单引号未正确转义：{stmts[0]}"
    assert "O''Brien Case" in stmts[0], "箱名里的单引号未转义"
    assert "NULL" in stmts[0], "None 应写成 NULL"
    assert "123" in stmts[0], "整数应为裸值"
    print("  ✓ 单引号转义 / NULL / 裸整数值")

    # 反斜杠绝不能被当转义符（SQLite 规则），否则中文路径会写坏
    bs = build_insert_literal("items", STAGES["items"],
                              [{**rows[0], "name": r"C:\path\新品"}], max_len=9999)
    assert r"C:\path\新品" in bs[0], "反斜杠被错误处理"
    print("  ✓ 反斜杠与中文原样保留")

    # 切分边界
    many = [dict(rows[0], item_id=f"i{n}") for n in range(500)]
    s90 = build_insert_literal("items", STAGES["items"], many, max_len=90_000)
    s2k = build_insert_literal("items", STAGES["items"], many, max_len=2_000)
    assert len(s90) <= len(s2k), "长度上限越小应切越多条"
    assert sum(s.count("(") for s in s2k) >= 500, "切分后不应丢行"
    print(f"  ✓ 切分：500 行 → 90KB 上限 {len(s90)} 条 / 2KB 上限 {len(s2k)} 条")

    # 单行就超限时不能死循环也不能丢数据
    one = build_insert_literal("items", STAGES["items"], rows[:1], max_len=10)
    assert len(one) == 1, "单行超限时应单独成句"
    print("  ✓ 单行超限不丢数据、不死循环")

    print("── 类型转换 ──")
    assert _coerce(float("nan")) is None
    assert _coerce(float("inf")) is None
    assert _coerce(True) == 1
    assert _coerce({"a": 1}) == '{"a": 1}'
    assert _sql_literal(None) == "NULL"
    assert _sql_literal(True) == "1"
    assert _sql_literal(1.5) == "1.5"
    assert _sql_literal("a'b") == "'a''b'"
    print("  ✓ NaN/inf→NULL，bool→1/0，dict→JSON，引号转义")

    # 回归测试：numpy 标量的 repr 坑（曾导致 forecast stage 整批语法错误）
    try:
        import numpy as np  # noqa: F401

        assert _sql_literal(np.float64(-0.2645)) == "-0.2645", \
            f"np.float64 未归一化：{_sql_literal(np.float64(-0.2645))}"
        assert _sql_literal(np.int64(21)) == "21"
        assert _sql_literal(np.bool_(True)) == "1"
        np_rows = [{"item_id": "n1", "name": "np品", "market_hash_name": "A | B (FN)",
                    "wear": "Factory New", "case_id": None, "case_name": "C",
                    "image": None, "updated_at": np.int64(7)}]
        s = build_insert_literal("items", STAGES["items"], np_rows, max_len=9999)[0]
        assert "np." not in s, f"语句里残留 numpy 表示：{s}"
        assert _coerce(np.datetime64("2026-10-11")) is not None
        print("  ✓ numpy 标量归一化（np.float64/int64/bool_/datetime64）")
    except ImportError:
        print("  - 跳过 numpy 测试（未安装 numpy）")

    print("── 各 stage 的批次估算（参数模式，D1 上限 100 参数）──")
    for stage, cols in STAGES.items():
        b = _batch_size(len(cols), 10_000)
        print(f"  {stage:<14} {len(cols):>2} 列 → 每批 {b:>3} 行")

    print("\n自检通过 ✓（未联网，仅验证 SQL 构造与类型转换）")
    return 0


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(json.dumps(check(), ensure_ascii=False, indent=2))
