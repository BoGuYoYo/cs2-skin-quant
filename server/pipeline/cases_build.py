"""构建 case_meta —— 箱子稀有度与供给面指标。

数据来源：
    state/crates.json  来自 ByMykel/CSGO-API（箱子 → 物品映射 + 首发日期）
    本地 snap 表       箱子自身及其物品的历史价格

为什么稀有度这么算：
    「箱子稀有 → 该箱物品新增供给趋近于零 → 下跌是存量博弈而非价值毁灭」
    可观测的代理量有三个，按可靠性排序：
      1) 箱子价格 —— 最可靠。越老越稀有的箱子自己就贵（CS:GO Weapon Case ¥700 vs
         Operation Phoenix ¥21），本质是市场对「这箱还开得出东西吗」的定价
      2) 箱子年龄 —— 首发越久，越可能已从掉落池轮换出去
      3) 箱内物品价格水平 —— 反映开箱期望值；期望值低的箱子没人开，新供给也少
    拿不到 Steam 市场的实时成交量（服务器访问不了 steamcommunity.com），
    所以用「箱子价格 30 日趋势」代替开箱热度衰减。

用法：
    python -m pipeline.cases_build              # 构建并写入本地库
    python -m pipeline.cases_build --status     # 只看当前状态
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from . import collector
from .config import CFG

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("cases")

CRATES = Path(__file__).resolve().parent.parent / "state" / "crates.json"

# 去掉磨损后缀，用于把 CSGO-API 的基名匹配到本地库的完整名
_WEAR = re.compile(
    r"\s*\((Factory New|Minimal Wear|Field-Tested|Well-Worn|Battle-Scarred|Not Painted)\)\s*$"
)


def base_name(n: str | None) -> str:
    return _WEAR.sub("", (n or "").strip()).strip()


# ---------------------------------------------------------------- 数据读取
def load_cases(path: Path = CRATES) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"缺少 {path}（从 ByMykel/CSGO-API 的 public/api/en/crates.json 取）")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [c for c in data if c.get("type") == "Case"]


def latest_prices(con: sqlite3.Connection) -> dict[str, float]:
    """每个品种的最新价格"""
    rows = con.execute("""
        SELECT s.item_id, s.sell
          FROM snap s
          JOIN (SELECT item_id, MAX(date) AS d FROM snap GROUP BY item_id) m
            ON m.item_id = s.item_id AND m.d = s.date
         WHERE s.sell IS NOT NULL
    """).fetchall()
    return {r[0]: float(r[1]) for r in rows}


def price_trend(con: sqlite3.Connection, item_id: str, days: int = 30) -> float | None:
    """近 N 日价格变化率（拿不到成交量时用它代理开箱热度衰减）"""
    rows = con.execute(
        "SELECT sell FROM snap WHERE item_id = ? AND sell IS NOT NULL ORDER BY date DESC LIMIT ?",
        (item_id, days),
    ).fetchall()
    if len(rows) < 5:
        return None
    now, then = float(rows[0][0]), float(rows[-1][0])
    if then <= 0:
        return None
    return now / then - 1.0


# ---------------------------------------------------------------- 稀有度
def rarity_score(
    case_price: float | None,
    age_years: float,
    item_median: float | None,
    trend_30d: float | None,
) -> tuple[float, list[str]]:
    """0-100 分。四个分量各自封顶，避免单一指标主导。"""
    score = 0.0
    why: list[str] = []

    # 1) 箱子价格（0-45）：¥1 → 0 分，¥700 → 45 分（对数刻度）
    if case_price and case_price > 0:
        s = 45.0 * math.log10(max(case_price, 1.0)) / math.log10(700.0)
        s = max(0.0, min(45.0, s))
        score += s
        why.append(f"箱子价 ¥{case_price:.0f}")

    # 2) 年龄（0-30）：10 年封顶
    s = max(0.0, min(30.0, 30.0 * age_years / 10.0))
    score += s
    why.append(f"{age_years:.1f} 年")

    # 3) 30 日价格趋势（0-15）：涨说明稀缺加剧
    if trend_30d is not None:
        s = max(0.0, min(15.0, 15.0 * trend_30d / 0.30))
        score += s
        if abs(trend_30d) > 0.03:
            why.append(f"30日 {trend_30d * 100:+.0f}%")

    # 4) 箱内物品价格中位数（0-10）
    if item_median and item_median > 0:
        s = 10.0 * math.log10(max(item_median, 1.0)) / math.log10(100.0)
        score += max(0.0, min(10.0, s))
        why.append(f"物品中位 ¥{item_median:.1f}")

    return min(100.0, round(score, 1)), why


def infer_drop_pool(case_price: float | None, age_years: float) -> str:
    """推断掉落池状态。

    CS2 的箱子掉落是轮换的，官方不公开当前池子。
    经验规律：贵的、老的箱子基本都已经不在掉落池里了。
    """
    if case_price is None:
        return "unknown"
    if case_price >= 50 or age_years >= 6:
        return "removed"
    if case_price >= 12:
        return "unknown"
    return "active"


# ---------------------------------------------------------------- 主流程
def build(con: sqlite3.Connection) -> tuple[list[dict], dict[str, str], dict[str, str]]:
    cases = load_cases()
    prices = latest_prices(con)
    today = date.today()

    # 箱子 → 物品（本地 item_id 列表）
    items_by_case: dict[str, list[str]] = defaultdict(list)
    item_rows = con.execute("SELECT item_id, market_hash_name FROM items").fetchall()
    by_base: dict[str, list[str]] = defaultdict(list)
    for item_id, mhn in item_rows:
        by_base[base_name(mhn or item_id)].append(item_id)

    case_to_items: dict[str, list[str]] = {}
    for c in cases:
        mhn = c["market_hash_name"]
        ids: list[str] = []
        for it in c.get("contains") or []:
            b = base_name(it.get("name"))
            if b:
                ids.extend(by_base.get(b, []))
        case_to_items[mhn] = sorted(set(ids))

    out: list[dict] = []
    now_ts = int(datetime.now().timestamp())

    for c in cases:
        mhn = c["market_hash_name"]
        ids = case_to_items.get(mhn, [])

        case_price = prices.get(mhn)
        trend = price_trend(con, mhn)

        # 箱内物品价格中位数
        vals = [prices[i] for i in ids if i in prices]
        vals = [v for v in vals if v and v > 0]
        item_median = None
        if vals:
            vals.sort()
            item_median = vals[len(vals) // 2]

        # 年龄
        age_years = 0.0
        fsd = c.get("first_sale_date")
        if fsd:
            for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
                try:
                    age_years = (today - datetime.strptime(fsd, fmt).date()).days / 365.25
                    break
                except ValueError:
                    continue

        score, why = rarity_score(case_price, age_years, item_median, trend)
        pool = infer_drop_pool(case_price, age_years)

        # 价格趋势写进 rationale（它是稀缺度的辅助信号，已计入 rarity_score），
        # 但**不要**塞进 supply_growth_30d。
        # 踩过的坑：这里原先把 trend*100 写进 supply_growth_30d，
        # 而下游 features.supply_rigidity 把它当「存世量增速%」代入 tanh(growth/5)，
        # 前端也把它显示成「存世量增速」—— 箱子涨价 30% 被解释成存世量增加 30%，
        # 语义完全错误，且会随 features 修复直接污染模型。
        trend_note = f"｜箱价30日 {trend * 100:+.1f}%" if trend is not None else ""

        out.append({
            "case_id": mhn,
            "case_name": c.get("name") or mhn,
            "rarity_score": score,
            "case_price": round(case_price, 2) if case_price else None,
            # 以下两个需要外部数据源，拿不到就留空 —— 不用近似值冒充
            "case_volume_30d": None,      # Steam 市场成交量（开箱量代理），服务器访问不了 steamcommunity.com
            "supply_growth_30d": None,    # 真实存世量增速，SteamDT 所有接口都不提供
            "drop_pool": pool,
            "rationale": "；".join(why) + trend_note + f"｜覆盖 {len(ids)} 个在库物品",
            "blocking_risk": (
                "仍在掉落池，新增供给会持续压制价格" if pool == "active"
                else "掉落池状态未知，需人工确认" if pool == "unknown"
                else None
            ),
            "updated_at": now_ts,
            "_items": ids,
        })

    # items.case_id / case_name 回填
    item_to_case: dict[str, str] = {}
    item_to_cname: dict[str, str] = {}
    for c in out:
        for iid in c["_items"]:
            item_to_case.setdefault(iid, c["case_id"])
            item_to_cname.setdefault(iid, c["case_name"])

    return out, item_to_case, item_to_cname


CASE_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS case_history (
  case_id TEXT, date TEXT, rarity_score REAL, case_price REAL, drop_pool TEXT,
  PRIMARY KEY (case_id, date)
);
CREATE INDEX IF NOT EXISTS ix_case_history ON case_history(case_id, date);
"""


def build_history(con: sqlite3.Connection, cases: list[dict], lookback_days: int = 420) -> int:
    """按**每个交易日**复算箱子稀有度，写进 case_history。

    为什么必须这么做（审查发现的前视泄漏）：
        rarity_score 是用**今天的**箱价、今天往前 30 天的趋势、今天的箱内物品
        中位价算出来的，而 features.build 把它 merge 到**全部历史日期**上 ——
        等于让 2025-10 的训练样本用上了 2026-10 的箱价信息，而箱价本身编码了
        「这一年谁涨谁跌」。walk-forward 的 purge/embargo 只能隔开时间，
        拦不住「静态特征里带未来」。
    现在每个日期只用那一天能看见的数据，公式与 rarity_score() 完全一致：
        那天的箱价 + 那天的年龄 + 那天往前 30 天的箱价趋势 + 那天的箱内中位价。

    ⚠️ cases 必须是 **crates.json 的原始条目**（带 first_sale_date 和 contains），
    不能传 build() 的输出 —— 那个输出里没有这两个字段，传错的话年龄恒为 0、
    箱内中位价恒为 None，分数会系统性偏低约 40 分（实测踩过：85 分算成 45 分）。

    返回写入行数。
    """
    con.executescript(CASE_HISTORY_SCHEMA)
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()

    # 箱子 → 物品（本地 item_id），沿用 build() 的口径
    item_rows = con.execute("SELECT item_id, market_hash_name FROM items").fetchall()
    by_base: dict[str, list[str]] = defaultdict(list)
    for item_id, mhn in item_rows:
        by_base[base_name(mhn or item_id)].append(item_id)

    total = 0
    con.execute("DELETE FROM case_history WHERE date >= ?", (cutoff,))
    for c in cases:
        cid = c.get("case_id") or c.get("market_hash_name")
        if not cid:
            continue
        ids: list[str] = []
        for it in c.get("contains") or []:
            b = base_name(it.get("name"))
            if b:
                ids.extend(by_base.get(b, []))
        if not ids:
            continue

        # 箱价序列
        box = pd.read_sql_query(
            "SELECT date, sell AS price FROM snap WHERE item_id = ? AND sell > 0 "
            " AND date >= ? ORDER BY date", con, params=(cid, cutoff))
        if box.empty:
            continue
        box["date"] = pd.to_datetime(box["date"])
        box = box.drop_duplicates("date").set_index("date")["price"].astype(float)
        dates = box.index

        # 箱内物品价格中位数（按日）
        med = None
        ph = ",".join(["?"] * len(ids))
        ip = pd.read_sql_query(
            f"SELECT date, sell AS price FROM snap WHERE item_id IN ({ph}) "
            f" AND sell > 0 AND date >= ?", con, params=ids + [cutoff])
        if not ip.empty:
            ip["date"] = pd.to_datetime(ip["date"])
            med = ip.groupby("date")["price"].median()

        # 年龄（按天算，随日期变化）
        fsd = c.get("first_sale_date")
        first: date | None = None
        if fsd:
            for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
                try:
                    first = datetime.strptime(fsd, fmt).date()
                    break
                except ValueError:
                    continue

        rows = []
        for d in dates:
            p = float(box.loc[d])
            # 趋势：该日往前 30 个采样点（与 cases_build.price_trend 同口径）
            prior = box.loc[:d]
            trend = None
            if len(prior) >= 2:
                back = float(prior.iloc[0] if len(prior) <= 30 else prior.iloc[-30])
                if back > 0:
                    trend = p / back - 1.0
            age_years = ((d.date() - first).days / 365.25) if first else 0.0
            item_median = float(med.loc[d]) if (med is not None and d in med.index) else None
            score, _ = rarity_score(p, age_years, item_median, trend)
            rows.append((cid, d.strftime("%Y-%m-%d"), score, round(p, 2),
                         infer_drop_pool(p, age_years)))
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO case_history "
                "(case_id, date, rarity_score, case_price, drop_pool) VALUES (?,?,?,?,?)",
                rows)
            total += len(rows)
            con.commit()
    log.info("case_history 写入 %d 行（%d 个箱子的逐日稀有度）", total, len(cases))
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="构建 case_meta")
    ap.add_argument("--status", action="store_true", help="只看状态")
    ap.add_argument("--no-push", action="store_true", help="只写本地，不推送")
    args = ap.parse_args()

    con = collector.connect(CFG.db_path)

    if args.status:
        n = con.execute("SELECT COUNT(*) FROM case_meta").fetchone()[0]
        print(f"  case_meta 行数: {n}")
        for r in con.execute(
            "SELECT case_name, rarity_score, case_price, drop_pool FROM case_meta "
            "ORDER BY rarity_score DESC LIMIT 10"
        ):
            print(f"    {r[0]:<44} {r[1]:>5}  ¥{r[2] or 0:>8.2f}  {r[3]}")
        items_with_case = con.execute("SELECT COUNT(*) FROM items WHERE case_id IS NOT NULL").fetchone()[0]
        print(f"  已关联箱子的物品: {items_with_case}")
        try:
            hn = con.execute("SELECT COUNT(*) FROM case_history").fetchone()[0]
            print(f"  case_history 逐日稀有度: {hn} 行")
        except sqlite3.OperationalError:
            print("  case_history 逐日稀有度: 还没建（跑一次不带 --status 的构建）")
        return 0

    log.info("读取 crates.json …")
    cases, item_to_case, item_to_cname = build(con)
    log.info("构建完成：%d 个箱子，%d 个物品可关联", len(cases), len(item_to_case))

    # 逐日稀有度（消掉回测里的前视泄漏，见 build_history 的说明）。
    # ⚠️ 必须传 crates.json 的**原始条目**：build() 的输出里没有
    # first_sale_date / contains，传错会让年龄恒 0、箱内中位价恒 None。
    try:
        build_history(con, load_cases())
    except Exception as e:      # noqa: BLE001
        log.warning("case_history 构建失败（不影响 case_meta）：%s", e)

    # 写 case_meta（先删掉已不存在的箱子，避免残留）
    current = {c["case_id"] for c in cases}
    existing = {r[0] for r in con.execute("SELECT case_id FROM case_meta").fetchall()}
    stale = existing - current
    for cid in stale:
        con.execute("DELETE FROM case_meta WHERE case_id = ?", (cid,))
    if stale:
        log.info("清理了 %d 个已下线的箱子", len(stale))

    rows = []
    for c in cases:
        c2 = {k: v for k, v in c.items() if not k.startswith("_")}
        rows.append(c2)
        con.execute(
            """INSERT OR REPLACE INTO case_meta
               (case_id, case_name, rarity_score, case_price, case_volume_30d,
                supply_growth_30d, drop_pool, rationale, blocking_risk, updated_at)
               VALUES (:case_id,:case_name,:rarity_score,:case_price,:case_volume_30d,
                       :supply_growth_30d,:drop_pool,:rationale,:blocking_risk,:updated_at)""",
            c2,
        )

    # 回填 items 的 case_id / case_name
    n_link = 0
    for iid, cid in item_to_case.items():
        con.execute(
            "UPDATE items SET case_id = ?, case_name = ? WHERE item_id = ?",
            (cid, item_to_cname.get(iid), iid),
        )
        n_link += 1
    con.commit()
    log.info("写库完成：case_meta %d 行，items 关联 %d 个", len(rows), n_link)

    if not args.no_push:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from push import dispatch

        log.info("推送通道：%s", dispatch.describe())
        dispatch.push("case_meta", rows)
        # items 的 case_id 变了，同步过去
        item_rows = [
            {"item_id": r[0], "name": r[1], "market_hash_name": r[2], "wear": r[3],
             "case_id": r[4], "case_name": r[5], "image": r[6], "updated_at": r[7]}
            for r in con.execute(
                "SELECT item_id, name, market_hash_name, wear, case_id, case_name, image, updated_at "
                "FROM items WHERE case_id IS NOT NULL"
            ).fetchall()
        ]
        log.info("同步 %d 个已关联箱子的物品", len(item_rows))
        dispatch.push("items", item_rows)

    # 汇总
    print()
    print("=== 箱子稀有度 Top 12 ===")
    for c in sorted(rows, key=lambda x: -x["rarity_score"])[:12]:
        price = c["case_price"]
        print(f"  {c['rarity_score']:>5.1f}  {(c['case_name'] or '')[:40]:<42} "
              f"¥{price if price else 0:>8.2f}  {c['drop_pool']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
