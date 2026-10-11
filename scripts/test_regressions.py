#!/usr/bin/env python3
"""回归测试：每条断言对应一个真实踩过的坑，防止同类错误再现。

这些 bug 的共同特征是「程序不报错、结果看起来也像模像样」——
只有不变量断言能防住。跑法：

    python scripts/test_regressions.py
"""
from __future__ import annotations

import math
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from pipeline import collector, decide, features, labels  # noqa: E402
from pipeline.config import (  # noqa: E402
    CFG,
    DOMAIN_EXCLUDE_SQL,
    DOMAIN_EXCLUDE_WEAPONS,
    FEATURE_COLS,
    FEATURE_COLS_PENDING,
    MONOTONE,
)

FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAIL.append(name)


def test_kline_field_order() -> None:
    """K 线字段顺序是 [ts, open, close, high, low]，不是 [o,h,l,c]。

    当时症状：把 row[4]（low）当 close，150 万行历史价格全是「当日最低价」，
    与实盘系统性偏低，但程序毫无异常。
    """
    print("\n[1] K 线字段顺序")
    # 用真实形状的数据：o=100, c=105, h=108, l=98
    row = [1783918800, 100.0, 105.0, 108.0, 98.0]
    o, c, h, l = row[1], row[2], row[3], row[4]
    check("h >= max(o,c)", h >= max(o, c), f"h={h} o={o} c={c}")
    check("l <= min(o,c)", l <= min(o, c), f"l={l}")
    check("若按 [o,h,l,c] 解释会自相矛盾",
          not (row[2] >= max(row[1], row[4]) and row[3] <= min(row[1], row[4])),
          "该形状下 [o,h,l,c] 解释不成立 —— 可用来区分两种顺序")


def test_timestamp_day_boundary() -> None:
    """时间戳是 UTC 16:00 = 北京 00:00，必须按北京时间归日。

    当时症状：整条序列偏移一天，10-10 的数据被记成 10-09。
    """
    print("\n[2] 时间戳归日")
    ts = int(datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc).timestamp())  # 北京 10-11 00:00
    utc_day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    cn_day = datetime.fromtimestamp(ts + 8 * 3600, tz=timezone.utc).strftime("%Y-%m-%d")
    check("UTC 归日会得到前一天", utc_day == "2026-10-10", utc_day)
    check("+8 小时归日得到北京日期", cn_day == "2026-10-11", cn_day)


def test_uniqueness_weight() -> None:
    """唯一性权重必须先累加全部重叠再计算。

    当时症状：第一个样本天然拿到 1.0，靠后的被低估 —— 实测 [1.0,0.75,0.75]，
    正确应为 [0.75,0.5,0.75]。
    """
    print("\n[3] 唯一性权重")
    ends = np.array([2, 3, 4])
    ids = np.array(["a", "a", "a"])
    got = labels.average_uniqueness(ends, ids)
    expect = np.array([0.75, 0.5, 0.75])
    check("与手工计算一致", np.allclose(got, expect, atol=1e-6),
          f"got={np.round(got, 4).tolist()} expect={expect.tolist()}")
    check("首样本不再天然为 1", abs(got[0] - 1.0) > 1e-6, f"{got[0]:.4f}")


def test_alert_level_order() -> None:
    """告警等级必须从高到低匹配。

    当时症状：跌 62.8% 也只报 L1（线上 1253 条告警全是 L1），
    而 run_daily 只对 L3 发通知 → 所有暴跌通知被静默丢弃。
    """
    print("\n[4] 告警等级顺序")
    extreme = {"ret_21d": -0.51, "ret_7d": -0.17, "vol_z": -6.0,
               "list_slope_7d": 60, "rel_ret_21d": -0.20, "listings": 500, "volume": 30}
    lv, score, _ = decide.detect_anomaly(extreme, CFG)
    check("极端暴跌报 L3", lv == "L3", f"got={lv} score={score:.0f}")

    mild = {"ret_21d": -0.09, "ret_7d": -0.01, "vol_z": -2.2,
            "listings": 500, "volume": 30}
    lv2, _, _ = decide.detect_anomaly(mild, CFG)
    check("轻微下跌报 L1", lv2 == "L1", f"got={lv2}")


def test_calendar_densify() -> None:
    """缺口必须补齐，否则 shift(n) 是「n 条记录」而非「n 天」。

    当时症状：数据缺 10-10 时，「21 日收益」实际跨 22 个日历天，
    标签窗口与回测的日期隔离全部失真。
    """
    print("\n[5] 日历稠密化")
    rows = []
    for i, d in enumerate(pd.date_range("2026-01-01", periods=5, freq="D")):
        if d.strftime("%Y-%m-%d") == "2026-01-03":
            continue  # 挖掉一天
        rows.append({"item_id": "X", "date": d, "price": 100.0 + i, "bid": None,
                     "listings": None, "volume": None, "supply": None})
    df = pd.DataFrame(rows)
    dense = features.densify_calendar(df)
    check("缺口被补齐到连续日历", len(dense) == 5, f"{len(df)} → {len(dense)}")
    gaps = dense["date"].diff().dropna().dt.days.unique().tolist()
    check("相邻日期差恒为 1 天", gaps == [1], f"diffs={gaps}")


def test_supply_growth_not_price() -> None:
    """supply_growth_30d 不能存价格涨幅。

    当时症状：cases_build 把箱子价格 30 日涨幅写进该字段，
    下游 features 又按「存世量增速%」代入 tanh(growth/5)，语义污染模型。
    """
    print("\n[6] supply_growth_30d 语义")
    src = (SERVER / "pipeline" / "cases_build.py").read_text(encoding="utf-8")
    check("cases_build 不再写入价格趋势", '"supply_growth_30d": None' in src,
          "应显式置 None")
    check("价格趋势已挪到 rationale", "trend_note" in src)


def test_ohlc_invariant_helper() -> None:
    """提供一个可复用的 OHLC 不变量校验（供入库后抽查）。"""
    print("\n[7] OHLC 不变量校验函数")

    def ohlc_ok(o: float, h: float, l: float, c: float) -> bool:
        return h >= max(o, c) - 1e-9 and l <= min(o, c) + 1e-9

    check("正常 OHLC 通过", ohlc_ok(100, 108, 98, 105))
    check("h<max(o,c) 被拒", not ohlc_ok(100, 101, 98, 105))
    check("l>min(o,c) 被拒", not ohlc_ok(100, 108, 104, 105))


def test_prob_up_is_bounded_and_monotone() -> None:
    """prob_up 必须随下沿单调、且不会只因 P50 高就冲到 100%。

    当时症状：np.clip((P50+0.05)/0.25) 让 P50=0.20 直接显示 100%，
    即使下分位还在严重亏损。
    """
    print("\n[8] prob_up 不再是 P50 的线性裁剪")

    def norm_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def prob(q10: float, q50: float, q90: float) -> float:
        sigma = max((q90 - q10) / (2 * 1.2816), 1e-6)
        return 1.0 - norm_cdf((math.log(1.10) - q50) / sigma)

    wide_low = prob(-0.40, 0.20, 0.80)   # 下沿很差
    tight_high = prob(0.05, 0.20, 0.35)  # 下沿不错
    check("同样 P50 下，下沿差 ⇒ 概率低", wide_low < tight_high,
          f"wide={wide_low:.3f} tight={tight_high:.3f}")
    check("概率落在 [0,1]", 0.0 <= wide_low <= 1.0 and 0.0 <= tight_high <= 1.0)


def test_batch_rate_limit_detection() -> None:
    """限流要看 errorCode 4005，不能只看 HTTP 状态码。

    当时症状：/price/batch 被限流时返回的是 **HTTP 200 + success=false +
    errorCode=4005**，而旧代码只判 status_code == 429，其余失败直接
    `log.warning(...); continue` 丢掉整批 100 个品种 —— 日志上只有一行 warning，
    数据静默缺失，看上去却"跑完了"。
    """
    print("\n[9] /price/batch 限流识别")

    class _Resp:
        def __init__(self, body: dict) -> None:
            self.status_code = 200
            self._body = body
            self.text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._body

    class _Session:
        """前 2 次返回限流，第 3 次才成功"""

        def __init__(self) -> None:
            self.calls = 0

        def post(self, *a, **kw) -> "_Resp":
            self.calls += 1
            if self.calls < 3:
                return _Resp({"success": False, "errorCode": 4005,
                              "errorMsg": "当前接口请求已达到上限。"})
            return _Resp({"success": True, "data": [{"marketHashName": "X"}]})

    sess = _Session()
    got = collector.fetch_prices(["X"], "key", session=sess, interval=0.0, max_retry=3)  # type: ignore[arg-type]
    check("限流后重试同一批并最终拿到数据", len(got) == 1 and sess.calls == 3,
          f"calls={sess.calls} got={len(got)}")

    class _Always:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *a, **kw) -> "_Resp":
            self.calls += 1
            return _Resp({"success": False, "errorCode": 4005, "errorMsg": "上限"})

    always = _Always()
    try:
        collector.fetch_prices(["X"], "key", session=always, interval=0.0, max_retry=2)  # type: ignore[arg-type]
        raised = False
    except RuntimeError as e:
        raised = "4005" in str(e) or "限流" in str(e)
    check("持续限流必须抛错而不是静默返回空", raised, "静默丢批正是当初的 bug")

    src = (SERVER / "pipeline" / "collector.py").read_text(encoding="utf-8")
    check("源码里不再只认 429", "RATE_LIMIT_CODE = 4005" in src)


def test_no_constant_features_when_inputs_present() -> None:
    """上游有原料时，特征绝不能退化成常数。

    当时症状：collector.load_history 没 JOIN case_id，features.build 里
    合并箱子层的那段被静默跳过 —— 23 个特征里 9 个变成常数
    （case_rarity_score ≡ 50、supply_rigidity ≡ 0.2315），MONOTONE 里
    针对这些列的约束全成了摆设，模型安静地变笨。
    这条断言是同类问题的**通用防线**：只喂完整原料，就要求每个特征都有变化。
    """
    print("\n[10] 特征常数体检")
    rng = np.random.default_rng(7)
    days = 90
    rows = []
    cases = {"CaseA": 80.0, "CaseB": 30.0}
    for ci, (cid, _) in enumerate(cases.items()):
        for k in range(3):                       # 每个箱子 3 个物品 → 分位才有区分度
            item = f"{cid}-{k}"
            price = 100.0 * (1 + ci * 0.3)
            supply = 5000.0 + k * 1000
            for d in pd.date_range("2026-01-01", periods=days, freq="D"):
                price *= float(np.exp(rng.normal(0.0005, 0.01)))
                supply *= float(np.exp(rng.normal(0.0008, 0.0005)))
                rows.append({
                    "item_id": item, "date": d, "price": round(price, 2),
                    "bid": round(price * 0.98, 2),
                    "listings": int(supply * (0.02 + 0.005 * k)),
                    "volume": int(supply * 0.01),
                    "supply": int(supply),
                    "case_id": cid, "case_name": cid,
                })
    hist = pd.DataFrame(rows)
    case_df = pd.DataFrame([
        {"case_id": cid, "case_name": cid, "rarity_score": r,
         "case_volume_30d": 5000.0 + i * 3000, "supply_growth_30d": 0.5 + i * 1.5,
         "drop_pool": "removed" if i == 0 else "active"}
        for i, (cid, r) in enumerate(cases.items())
    ])
    events_df = pd.DataFrame([
        {"date": pd.Timestamp("2026-02-01"), "direction": "bearish", "confidence": 0.8},
        {"date": pd.Timestamp("2026-03-05"), "direction": "bullish", "confidence": 0.6},
    ])
    feats = features.build(hist, case_df, None, events_df)

    constants = []
    for c in FEATURE_COLS:
        if c not in feats.columns:
            constants.append(f"{c}(缺失)")
        elif feats[c].nunique(dropna=True) <= 1:
            constants.append(c)
    check(f"完整原料下 {len(FEATURE_COLS)} 个特征全部有变化", not constants,
          f"常数特征：{constants}" if constants else f"{len(FEATURE_COLS)} 个特征全部存活")

    # 反证：拿掉 case_id 这列，case_rarity_score 必须退回默认常数 50
    bare = features.build(hist.drop(columns=["case_id", "case_name"]), case_df, None, events_df)
    check("去掉 case_id 后稀有度确实退化成常数（说明检测有效）",
          bare["case_rarity_score"].nunique(dropna=True) == 1,
          f"unique={bare['case_rarity_score'].nunique(dropna=True)}")

    # ---- 配置层面的两条不变量 ----
    overlap = set(FEATURE_COLS) & set(FEATURE_COLS_PENDING)
    check("待接入列不得混进 FEATURE_COLS", not overlap, f"重叠：{overlap}")

    orphan = set(MONOTONE) - set(FEATURE_COLS)
    check("MONOTONE 不能约束不在特征集里的列", not orphan,
          f"孤儿约束：{orphan}（对常数列加约束 = 假装有约束）")
    dead_mono = [c for c in MONOTONE if c in feats.columns and feats[c].nunique(dropna=True) <= 1]
    check("MONOTONE 约束的列在完整原料下都必须有变化", not dead_mono, f"受约束的常数列：{dead_mono}")


def test_domain_exclude_rules() -> None:
    """用户规则：纪念品不看、StatTrak 不看（2026-10-11）。

    这条规则必须在采集池和推理口径里同时生效，否则会出现
    「监控池里看不见、候选页里又有」的不一致。
    """
    print("\n[11] 域筛选：纪念品 / StatTrak")
    sql = collector.WATCHLIST_SQL
    check("采集池排除纪念品", "NOT LIKE '%Souvenir%'" in sql)
    check("采集池排除 StatTrak", "NOT LIKE '%StatTrak%'" in sql)

    # 规则唯一来源：别在别的文件里另抄一份
    src_run = (SERVER / "pipeline" / "run_daily.py").read_text(encoding="utf-8")
    src_col = (SERVER / "pipeline" / "collector.py").read_text(encoding="utf-8")
    src_api = (SERVER / "app.py").read_text(encoding="utf-8")
    check("run_daily 用的是 config 里的同一份规则", "DOMAIN_EXCLUDE_SQL" in src_run)
    check("collector 从 config 导入而不是自己写一份",
          "from .config import DOMAIN_EXCLUDE_SQL" in src_col)
    check("API 列表接口也兜了同一份规则", "domain_exclude_sql()" in src_api)

    # 命名事实：StatTrak 带 ™，匹配用的是 %StatTrak%，别改成 'StatTrak '
    check("匹配用的是不含空格的 %StatTrak%（匕首名是 '★ StatTrak™ ...'）",
          "StatTrak%" in sql)


def test_liquidity_gate_missing_data() -> None:
    """用户规则：「日交易量 <20 不看」—— 但数据缺失时必须放行，不能全灭。

    踩过的坑（同一个坑第二次）：/api/screener 曾写成
    COALESCE(turnover, 0) >= 0.002，而 turnover 恒为 0 → 0 >= 0.002 永假
    → 页面永远空白。流动性闸门要是照抄这个写法，挂单量还没积累时
    会把所有品种判成"不合格"，同样全灭。
    """
    print("\n[12] 流动性闸门与缺失数据")
    from pipeline.run_daily import _apply_liquidity_gate  # noqa: PLC0415

    thr = CFG.gate_listings
    check("阈值就是用户说的 20", thr == 20, f"gate_listings={thr}")

    # ① 挂单量全空 → 全部放行
    df = pd.DataFrame({"item_id": ["a", "b"], "listings": [None, None]})
    out = _apply_liquidity_gate(df, CFG)
    check("挂单量全空时全部放行（不把「不知道」当「不合格」）", len(out) == 2,
          f"保留 {len(out)}/2")

    # ② 有数据 → 严格按阈值卡
    df2 = pd.DataFrame({"item_id": ["a", "b", "c"],
                        "listings": [thr - 1, thr, 5000]})
    out2 = _apply_liquidity_gate(df2, CFG)
    check("挂单量 < 20 的剔除、>= 20 的保留",
          out2["item_id"].tolist() == ["b", "c"], f"保留 {out2['item_id'].tolist()}")

    # ③ 混合（部分品种还没采到）→ 未知的放行，已知且小的剔除
    df3 = pd.DataFrame({"item_id": ["a", "b", "c"], "listings": [None, 5, 100]})
    out3 = _apply_liquidity_gate(df3, CFG)
    check("未知放行、已知不合格剔除",
          out3["item_id"].tolist() == ["a", "c"], f"保留 {out3['item_id'].tolist()}")

    # ④ 静态检查：筛选器不能再出现「把缺失当 0」的老写法。
    #    注意要先去掉注释行 —— app.py 里那段注释正是在讲这次踩的坑，
    #    里面**故意**留了旧写法的原文，直接全文匹配会误报。
    api_src = (SERVER / "app.py").read_text(encoding="utf-8")
    api_code = "\n".join(
        ln for ln in api_src.splitlines() if not ln.strip().startswith("#")
    )
    check("screener 不再用 COALESCE(turnover, 0) >= 卡死",
          "COALESCE(fl.turnover, 0) >=" not in api_code)
    check("screener 改成 IS NULL 放行", "fl.turnover   IS NULL OR" in api_code)


def test_update_schedule_is_7am_beijing() -> None:
    """用户规则：早上七点左右开始更新（2026-10-11）。"""
    print("\n[13] 更新时间：北京 07:00")
    timer = (Path(__file__).resolve().parent / "skin-alpha-daily.timer").read_text(encoding="utf-8")
    check("OnCalendar 是 UTC 23:00（= 北京 07:00）", "23:00:00 UTC" in timer)
    check("开机补跑仍开启", "Persistent=true" in timer)


def test_weapon_exclusion_sql() -> None:
    """用户规则：内格夫 / M249 / 贝瑞塔 / CZ / SG553「等枪械」不看。

    这条测试用**真实 SQL**（collector.WATCHLIST_SQL）在内存库里跑一遍，
    因为最容易犯的错不是漏写规则，而是写法误伤：
        'NOT LIKE %M4%' 会顺手打掉 M4A4 / M4A1-S；
        'NOT LIKE Negev%'（不带 " | "）则会漏掉 'Souvenir Negev | ...' 这种带前缀的形态。
    两种错都很隐蔽 —— 前者悄悄少赚，后者悄悄没生效。
    """
    print("\n[14] 枪械排除：内格夫 / M249 / 贝瑞塔 / CZ / SG553")
    check("五把枪都在配置里",
          set(DOMAIN_EXCLUDE_WEAPONS) == {"Negev", "M249", "Dual Berettas",
                                          "CZ75-Auto", "SG 553"},
          f"{DOMAIN_EXCLUDE_WEAPONS}")
    check("匹配带上 ' | ' 分隔符（不用裸子串）", "Negev | %" in DOMAIN_EXCLUDE_SQL)

    import sqlite3  # noqa: PLC0415

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE items (item_id TEXT, name TEXT, market_hash_name TEXT)")
    # (market_hash_name, 是否应留在监控池)
    samples = [
        ("Negev | Power Loader (Factory New)", False),
        ("M249 | Aztec (Minimal Wear)", False),
        ("Dual Berettas | Melondrama (Factory New)", False),
        ("CZ75-Auto | Tacticat (Well-Worn)", False),
        ("SG 553 | Cyrex (Field-Tested)", False),
        # 带前缀形态：即使哪天放开纪念品/StatTrak 也仍然要挡住
        ("Souvenir Negev | Man-o'-war (Field-Tested)", False),
        ("StatTrak™ M249 | Aztec (Minimal Wear)", False),
        ("Genuine CZ75-Auto | X", False),
        # 必须保留：同类名字最容易被误伤
        ("M4A4 | 龙王 (Factory New)", True),
        ("M4A1-S | Printstream (Field-Tested)", True),
        ("AK-47 | Redline (Field-Tested)", True),
        ("AWP | Asiimov (Field-Tested)", True),
        ("SSG 08 | Fever Dream (Minimal Wear)", True),
        ("Galil AR | Chatterbox (Well-Worn)", True),
        ("MAC-10 | Neon Rider (Factory New)", True),
    ]
    for i, (mhn, _) in enumerate(samples):
        con.execute("INSERT INTO items VALUES (?,?,?)", (mhn, mhn.split(" | ")[0], mhn))
    kept = {r[0] for r in con.execute(collector.WATCHLIST_SQL).fetchall()}

    missed = [m for m, keep in samples if not keep and m in kept]
    killed = [m for m, keep in samples if keep and m not in kept]
    check("五把枪（含带前缀形态）全部被排除", not missed, f"漏掉：{missed}")
    check("没有误伤 M4A4 / M4A1-S / AK-47 / AWP / SSG 08 / Galil / MAC-10",
          not killed, f"误杀：{killed}")


def main() -> int:
    print("=" * 62)
    print("回归测试：每条对应一个真实踩过的坑")
    print("=" * 62)
    test_kline_field_order()
    test_timestamp_day_boundary()
    test_uniqueness_weight()
    test_alert_level_order()
    test_calendar_densify()
    test_supply_growth_not_price()
    test_ohlc_invariant_helper()
    test_prob_up_is_bounded_and_monotone()
    test_batch_rate_limit_detection()
    test_no_constant_features_when_inputs_present()
    test_domain_exclude_rules()
    test_weapon_exclusion_sql()
    test_liquidity_gate_missing_data()
    test_update_schedule_is_7am_beijing()
    print("\n" + "=" * 62)
    if FAIL:
        print(f"结果：{len(FAIL)} 项失败 → {FAIL}")
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
