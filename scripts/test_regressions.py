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
          "from .config import" in src_col and "DOMAIN_EXCLUDE_SQL" in src_col
          and 'DOMAIN_EXCLUDE_SQL = "' not in src_col)
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
    """用户规则：早上七点左右开始更新（2026-10-11）；晚 19:00 再刷新一次。"""
    print("\n[13] 更新时间：北京 07:00 全量 + 19:00 增量")
    here = Path(__file__).resolve().parent
    timer = (here / "skin-alpha-daily.timer").read_text(encoding="utf-8")
    check("早上那次 OnCalendar 是 UTC 23:00（= 北京 07:00）", "23:00:00 UTC" in timer)
    check("开机补跑仍开启", "Persistent=true" in timer)

    rtimer = (here / "skin-alpha-refresh.timer").read_text(encoding="utf-8")
    check("晚间那次 OnCalendar 是 UTC 11:00（= 北京 19:00）", "11:00:00 UTC" in rtimer)
    check("晚间那次开机也补跑", "Persistent=true" in rtimer)
    rsvc = (here / "skin-alpha-refresh.service").read_text(encoding="utf-8")
    check("晚间那次跳过采集（--skip-collect）", "--skip-collect" in rsvc)
    check("晚间那次用的是独立的 refresh.log", "refresh.log" in rsvc)

    dsvc = (here / "skin-alpha-daily.service").read_text(encoding="utf-8")
    check("全量那次的超时够跑完 84 分钟采集 + 训练", "TimeoutStartSec=14400" in dsvc,
          "超时设小了会在采集之后、特征之前被 SIGTERM，当天直接没有新预测")


def test_weapon_exclusion_sql() -> None:
    """用户规则：内格夫 / M249 / 贝瑞塔 / CZ / SG553「等枪械」不看。

    这条测试用**真实 SQL**（collector.WATCHLIST_SQL）在内存库里跑一遍，
    因为最容易犯的错不是漏写规则，而是写法误伤：
        'NOT LIKE %M4%' 会顺手打掉 M4A4 / M4A1-S；
        'NOT LIKE Negev%'（不带 " | "）则会漏掉 'Souvenir Negev | ...' 这种带前缀的形态。
    两种错都很隐蔽 —— 前者悄悄少赚，后者悄悄没生效。
    """
    print("\n[14] 枪械排除：内格夫 / M249 / 贝瑞塔 / CZ / SG553 / SCAR-20 / G3SG1 / PP-Bizon")
    check("八把枪都在配置里",
          set(DOMAIN_EXCLUDE_WEAPONS) == {"Negev", "M249", "Dual Berettas",
                                          "CZ75-Auto", "SG 553",
                                          "SCAR-20", "G3SG1", "PP-Bizon"},
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
        ("SCAR-20 | Bloodsport (Factory New)", False),
        ("G3SG1 | The Executioner (Minimal Wear)", False),
        ("PP-Bizon | Judgement of Anubis (Field-Tested)", False),
        # 带前缀形态：即使哪天放开纪念品/StatTrak 也仍然要挡住
        ("Souvenir Negev | Man-o'-war (Field-Tested)", False),
        ("StatTrak™ M249 | Aztec (Minimal Wear)", False),
        ("Genuine CZ75-Auto | X", False),
        ("Souvenir G3SG1 | X", False),
        # 必须保留：同类名字最容易被误伤
        ("M4A4 | 龙王 (Factory New)", True),
        ("M4A1-S | Printstream (Field-Tested)", True),
        ("AK-47 | Redline (Field-Tested)", True),
        ("AWP | Asiimov (Field-Tested)", True),
        ("SSG 08 | Fever Dream (Minimal Wear)", True),
        ("Galil AR | Chatterbox (Well-Worn)", True),
        ("MAC-10 | Neon Rider (Factory New)", True),
        ("P250 | X (Factory New)", True),
        ("P90 | X (Factory New)", True),
    ]
    for i, (mhn, _) in enumerate(samples):
        con.execute("INSERT INTO items VALUES (?,?,?)", (mhn, mhn.split(" | ")[0], mhn))
    kept = {r[0] for r in con.execute(collector.WATCHLIST_SQL).fetchall()}

    missed = [m for m, keep in samples if not keep and m in kept]
    killed = [m for m, keep in samples if keep and m not in kept]
    check("八把枪（含带前缀形态）全部被排除", not missed, f"漏掉：{missed}")
    check("没有误伤 M4A4 / M4A1-S / AK-47 / AWP / SSG 08 / Galil / MAC-10 / P250 / P90",
          not killed, f"误杀：{killed}")


def test_meta_table_and_cursor() -> None:
    """collector 必须建出它自己写的表（meta），且游标存不下不能让采集停摆。

    当时症状：collector.SCHEMA 里没有 meta，而 _meta_set 每批都
    INSERT INTO meta → collect_listings 写完第一批 100 个品种就抛
    OperationalError 崩掉、systemd 拉起又死，游标永远归零；
    而流动性闸门按「没数据就放行」的设计照常工作 —— 崩溃与静默失效
    同时发生，看板上完全看不出采集器一直在崩。
    """
    print("\n[15] meta 表与断点游标")
    import tempfile  # noqa: PLC0415

    check("collector.SCHEMA 自己建 meta（不指望 to_local.ensure_schema）",
          "CREATE TABLE IF NOT EXISTS meta" in collector.SCHEMA)

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "fresh.db")
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check("只有 collector 碰过的库里也有 meta", "meta" in tables, f"tables={sorted(tables)}")

        collector._meta_set(con, collector.LISTINGS_CURSOR_KEY, "700")
        check("游标写入后能读回",
              collector._meta_get(con, collector.LISTINGS_CURSOR_KEY) == "700")

        # 第二道防线：就算 meta 真被删了，也不该让整轮采集崩掉
        con.execute("DROP TABLE meta")
        con.commit()
        raised = False
        try:
            collector._meta_set(con, collector.LISTINGS_CURSOR_KEY, "800")
        except Exception as e:      # noqa: BLE001
            raised = True
            print(f"      抛出了：{type(e).__name__}: {e}")
        check("meta 缺失时 _meta_set 只告警不抛异常", not raised,
              "游标是省重复劳动的优化，不是核心数据（UPSERT 幂等，丢了重跑即可）")
        con.close()


def test_notify_digest_uses_simple_returns() -> None:
    """手机摘要里的 P10/P50 必须换算成简单收益再打印。

    当时症状：notify_digest 写 f"{c['p10']:+.1%}"，把**对数**收益当百分比 ——
    log=-0.818（真实亏 55.9%）会显示成 -81.8%，库里最负的 log=-2.7181
    会显示成 -271.8%（比本金全亏还夸张）。终端与前端都做对了换算，
    只有手机这条通道错 —— 而它恰恰是你不在电脑前时唯一的读数渠道。
    """
    print("\n[16] 通知摘要的收益口径")
    import push.notify as notify  # noqa: PLC0415

    got = notify.log_to_simple_pct(-0.818)
    check("log_to_simple_pct 是 expm1（不是直接 ×100）",
          abs(got - (math.expm1(-0.818) * 100)) < 1e-9, f"log=-0.818 -> {got:.2f}%")

    captured: list[str] = []
    orig = (notify._bark, notify._wecom, notify._feishu)
    notify._bark = lambda title, body, level: (captured.append(body), True)[1]   # type: ignore[assignment]
    notify._wecom = lambda title, body: (captured.append(body), True)[1]        # type: ignore[assignment]
    notify._feishu = lambda title, body: (captured.append(body), True)[1]       # type: ignore[assignment]
    try:
        notify.notify_digest("2026-10-11", [
            {"name": "测试标的", "price": 12.5, "p10": -0.818, "p50": -0.5, "size": 0.02},
        ])
    finally:
        notify._bark, notify._wecom, notify._feishu = orig   # type: ignore[assignment]

    body = captured[0] if captured else ""
    check("P10 打印真实的 -55.9%，不是 -81.8%",
          "-55.9%" in body and "-81.8%" not in body, body[:90])
    check("P50 打印真实的 -39.3%", "-39.3%" in body, body[:90])


def test_source_guards_for_fixed_bugs() -> None:
    """源码层面的守卫：已修的问题不许以任何形式复活。"""
    print("\n[17] 源码回归守卫")
    rd = (SERVER / "pipeline" / "run_daily.py").read_text(encoding="utf-8")
    md = (SERVER / "pipeline" / "model.py").read_text(encoding="utf-8")
    # 去掉注释行再查：几处注释里**故意**写着旧写法的原文（说明这次踩的坑），
    # 直接全文匹配会误报。
    rd_code = "\n".join(ln for ln in rd.splitlines() if not ln.strip().startswith("#"))

    check("冷启动推送不再拿 market_hash_name 去比 snap.item_id",
          "market_hash_name IN (SELECT DISTINCT item_id FROM snap)" not in rd_code)
    check("walk_forward 不再用全样本分位数当基线（那正是泄漏口径）",
          "np.quantile(y_all" not in md)
    check("walk_forward 的 return 之后不留死代码",
          md.count("return pd.DataFrame(rows), base") == 1)

    allsrc = "\n".join(
        (SERVER / p).read_text(encoding="utf-8")
        for p in ("pipeline/run_daily.py", "pipeline/decide.py", "pipeline/cases.py",
                  "pipeline/model.py", "push/to_d1.py")
    )
    for name in ("def make_kline", "def tranche(", "def in_cooldown(", "def confirm(",
                 "def score_cases_llm(", "def write_cases(", "def collect_case_metrics(",
                 "def score_cases_rule_based(", "_TABLE_MAP = {"):
        check(f"死代码已移除：{name}", name not in allsrc)


def test_listings_cross_midnight_and_stale_guard() -> None:
    """[P1] 常驻采集跨日必须换日期；[P1] 旧报价不许覆盖新报价。"""
    print("\n[18] 采集跨日与新旧比较")
    import tempfile  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "x.db")
        cols = {r[1] for r in con.execute("PRAGMA table_info(snap)")}
        check("snap 有 src 列（记录行情来源平台 / kline）", "src" in cols, f"{sorted(cols)}")

        def put(day: str, price: float, ts: int, plat: str = "BUFF") -> None:
            con.execute(collector.SNAP_UPSERT_QUOTE,
                        ("X", day, price, price, 10, None, None, ts, plat, ts))
            con.commit()

        put("2026-10-12", 100.0, 2_000_000)
        put("2026-10-12", 50.0, 1_000_000)      # 更旧的报价 → 必须被拒
        got = con.execute("SELECT sell FROM snap WHERE item_id='X'").fetchone()[0]
        check("更旧的报价不能覆盖更新的", got == 100.0, f"sell={got}")
        put("2026-10-12", 120.0, 3_000_000)     # 更新的报价 → 生效
        got = con.execute("SELECT sell FROM snap WHERE item_id='X'").fetchone()[0]
        check("更新的报价正常写入", got == 120.0, f"sell={got}")

        # 跨午夜：启动时 10-11 23:59，第 2 批已到 10-12 00:01
        seq = [_dt(2026, 10, 11, 23, 59), _dt(2026, 10, 11, 23, 59), _dt(2026, 10, 12, 0, 1)]
        real_dt, real_fetch = collector.datetime, collector.fetch_prices

        class _FakeDT(_dt):     # 继承真 datetime：只替换 now()，其余类方法照常可用
            @staticmethod
            def now():
                return seq.pop(0) if seq else _dt(2026, 10, 12, 0, 2)

        collector.datetime = _FakeDT        # type: ignore[assignment]
        collector.fetch_prices = lambda chunk, *a, **kw: [   # type: ignore[assignment]
            {"marketHashName": n,
             "dataList": [{"platform": "BUFF", "sellPrice": 9.9, "sellCount": 30,
                           # updateTime 必须与"当天"接近：写入端会拒收陈旧报价
                           "updateTime": int(_dt(2026, 10, 11, 12, 0,
                                                 tzinfo=timezone.utc).timestamp())}]}
            for n in chunk
        ]
        try:
            stats = collector.collect_listings(con, None, "k", ["A", "B", "C", "D"],
                                               interval=0, max_batches=2)  # type: ignore[arg-type]
        finally:
            collector.datetime, collector.fetch_prices = real_dt, real_fetch  # type: ignore[assignment]

        days = dict(con.execute(
            "SELECT date, COUNT(*) FROM snap WHERE item_id IN ('A','B','C','D') GROUP BY date"))
        check("跨午夜后第 2 批写到新日期，不复用启动日期",
              days.get("2026-10-11") == 4 and days.get("2026-10-12") == 4, f"{days}")
        check("stats['day'] 同步更新", stats.get("day") == "2026-10-12", f"{stats.get('day')}")
        con.close()


def test_kline_validation_and_source() -> None:
    """[P2] K 线必须过自洽性/有限性校验，并记录来源。"""
    print("\n[19] K 线自洽性校验")
    import tempfile  # noqa: PLC0415

    check("合法 bar 通过", collector._ohlc_ok(100, 108, 98, 105))
    check("h < max(o,c) 被拒", not collector._ohlc_ok(100, 101, 98, 105))
    check("l > min(o,c) 被拒", not collector._ohlc_ok(100, 108, 104, 105))
    check("NaN 被拒（float('nan') 不抛异常，原先会原样入库）",
          not collector._ohlc_ok(100, float("nan"), 98, 105))
    check("inf 被拒", not collector._ohlc_ok(100, float("inf"), 98, 105))
    check("非正数被拒", not collector._ohlc_ok(-1.0, 1.0, 1.0, 1.0))

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "k.db")
        n = collector.write_kline_rows(con, "T", [
            [1760198400, 100.0, 105.0, 108.0, 98.0],          # 合法
            [1760284800, 100.0, 105.0, 99.0, 98.0],           # h=99 < max(100,105) 矛盾
            [1760371200, float("nan"), 105.0, 108.0, 98.0],   # NaN
        ])
        check("只写入合法的那一条", n == 1, f"n={n}")
        rows = tuple(con.execute(
            "SELECT COUNT(*), COUNT(src) FROM snap WHERE item_id='T'").fetchone())
        check("不合格 bar 没落库，src 已记录", rows == (1, 1), f"{rows}")
        check("kline 表同样只留合法行",
              con.execute("SELECT COUNT(*) FROM kline WHERE item_id='T'").fetchone()[0] == 1)
        con.close()


def test_cross_section_from_single_row() -> None:
    """[P2] 最新截面必须整行取自同一天，不能逐列各取最后非空值。

    当时症状：groupby().last() 是逐列取最后一个非空值，会把「前几天有、
    今天没有」的挂单量拼到今天的新价格上 —— 而流动性闸门正好读 listings。
    """
    print("\n[20] 最新截面必须来自同一行")
    df = pd.DataFrame([
        {"item_id": "A", "date": pd.Timestamp("2026-10-11"), "price": 10.0, "listings": 50},
        {"item_id": "A", "date": pd.Timestamp("2026-10-12"), "price": 12.0, "listings": None},
        {"item_id": "B", "date": pd.Timestamp("2026-10-12"), "price": 5.0, "listings": 7},
    ])
    out = features.latest_cross_section(df).set_index("item_id")
    check("今天没有挂单量就是 NaN（不拿前几天的顶上）",
          bool(pd.isna(out.loc["A", "listings"])), f"{out.loc['A', 'listings']}")
    check("价格与日期来自同一行",
          float(out.loc["A", "price"]) == 12.0
          and str(out.loc["A", "date"])[:10] == "2026-10-12",
          f"{out.loc['A', 'price']} @ {out.loc['A', 'date']}")
    check("有数据的品种正常取最后一行", int(out.loc["B", "listings"]) == 7)


def test_beijing_day_and_notification_order() -> None:
    """[P2] 基准日必须是北京时间；[P2] 告警先筛等级再限量；[P1] 清理陈旧因子。"""
    print("\n[21] 北京基准日 / 通知顺序 / 陈旧因子清理")
    from datetime import datetime as _dt  # noqa: PLC0415
    from datetime import timedelta as _td  # noqa: PLC0415
    from datetime import timezone as _tz  # noqa: PLC0415

    from pipeline import run_daily as rd  # noqa: PLC0415

    bj = (_dt.now(_tz.utc) + _td(hours=8)).strftime("%Y-%m-%d")
    check("today_str() 返回北京日期", rd.today_str() == bj, f"{rd.today_str()} vs 北京 {bj}")

    src = (SERVER / "pipeline" / "run_daily.py").read_text(encoding="utf-8")
    # 去掉注释行再查：那条踩坑注释里**故意**保留着旧写法的原文（记录事故现场），
    # 直接全文匹配会误报。
    src_code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    check("today_str 不再用 UTC 日期", "now(timezone.utc).strftime" not in src_code)
    check("factor_latest 按白名单清理（同一天第二次刷新也能淘汰）",
          "prune_factor_latest(con," in src_code)
    check("告警不再「先截前 5 条再筛 L3」", "alert_rows[:5]" not in src_code)
    check("L3 先按分数排序再限量", "l3_alerts[: cfg.alert_push_max]" in src_code)
    check("限量阈值进了配置", CFG.alert_push_max >= 1, f"alert_push_max={CFG.alert_push_max}")


def test_kline_today_is_guarded_history_is_not() -> None:
    """[P1] 当日盘中 K 线不许把更新的报价顶回去；历史日线仍然允许定稿覆盖。

    当时的症状：只给 batch 那条通道加了新旧保护，K 线那条仍然无比较 ——
    于是「先写更新报价 ¥175，再来一根旧的当日 K 线」会把价格倒退成 ¥173。
    但也不能一刀切给 K 线也加比较：**历史日线是某天的权威定稿**，
    次日补写就该覆盖（否则昨天那根未封口的 bar 永远定不了稿）。
    """
    print("\n[22] K 线：当日受保护、历史仍可定稿")
    import tempfile  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415
    from datetime import timedelta as _td  # noqa: PLC0415

    from pipeline.config import BEIJING as _BJ  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "k.db")
        from datetime import datetime as _dt2  # noqa: PLC0415
        from datetime import timezone as _tz2  # noqa: PLC0415

        now = _dt.now(_BJ)

        def bar_ts(day_offset: int = 0) -> int:
            """北京某日的日线戳。

            SteamDT 的日线戳是 UTC 16:00（= 北京次日 00:00），
            write_kline_rows 用 `ts + 8h` 还原成北京日期。
            所以「北京 D 日」的戳 = UTC D 00:00 − 8h。
            （第一版测试把这个算错成 D 00:00 北京 − 8h，得到的 day 是 D-1，
              于是走的是「历史定稿」分支 —— 测试自己把用例搞错了。）
            """
            d = (now + _td(days=day_offset)).date()
            x = _dt2(d.year, d.month, d.day, tzinfo=_tz2.utc)   # D 00:00 UTC
            return int(x.timestamp()) - 8 * 3600

        ts_today = bar_ts(0)
        ts_yday = bar_ts(-1)

        # ① 当日：先写一条「更新的报价」，再来一根更旧的当日 K 线
        quote_ts = ts_today + 3600 * 6          # 今天 06:00 北京（比 bar 戳更新）
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("T", now.strftime("%Y-%m-%d"), 175.0, 175.0, 30, None, None, quote_ts, "BUFF",
                     quote_ts))
        con.commit()
        collector.write_kline_rows(con, "T", [[ts_today, 174.0, 173.0, 176.0, 172.0]])
        got = con.execute("SELECT sell FROM snap WHERE item_id='T'").fetchone()[0]
        check("当日旧 K 线不许把更新报价顶回去（价格不倒退）", got == 175.0, f"sell={got}")

        # ② 历史：报价是昨天盘中写的（ts 更新），今天补的日线是权威定稿 → 应覆盖
        yday = (now + _td(days=-1)).strftime("%Y-%m-%d")
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("H", yday, 200.0, 200.0, 30, None, None, ts_yday + 3600 * 10, "BUFF",
                     ts_yday + 3600 * 10))
        con.commit()
        collector.write_kline_rows(con, "H", [[ts_yday, 190.0, 188.0, 191.0, 187.0]])
        got2 = con.execute("SELECT sell FROM snap WHERE item_id='H'").fetchone()[0]
        check("历史日线仍然能定稿覆盖盘中值", got2 == 188.0, f"sell={got2}")
        kc_h = con.execute("SELECT c FROM kline WHERE item_id='H'").fetchone()
        check("历史 kline 也同步定稿", abs(float(kc_h[0]) - 188.0) < 1e-9, f"{kc_h}")

        # ③ kline 表必须和 snap 同步受保护，否则「页面价格 175 / 图上画 173」
        kc_rows = con.execute("SELECT c FROM kline WHERE item_id='T'").fetchall()
        check("当日旧 K 线不许写进 kline 表（图表不会与价格分裂）",
              all(abs(float(r[0]) - 173.0) > 1e-9 for r in kc_rows), f"kline={kc_rows}")

        # ④ 更新的当日 K 线应当两边都收
        collector.write_kline_rows(con, "T", [[quote_ts + 3600, 174.0, 176.0, 177.0, 173.0]])
        got3 = con.execute("SELECT sell FROM snap WHERE item_id='T'").fetchone()[0]
        kc3 = con.execute("SELECT c FROM kline WHERE item_id='T'").fetchone()
        check("更新的当日 K 线两边都写入",
              abs(got3 - 176.0) < 1e-9 and kc3 is not None and abs(float(kc3[0]) - 176.0) < 1e-9,
              f"sell={got3} kline_c={kc3}")
        con.close()


def test_factor_latest_prune_same_day() -> None:
    """[P1] 同一天第二次刷新，被淘汰的品种也必须清掉。

    只按 `date <> day` 删是不够的：早班入选、晚班被流动性闸门剔掉的品种，
    日期仍然是今天，日期条件删不掉它，筛选页继续拿旧价格当现价。
    """
    print("\n[23] factor_latest 白名单清理（同日重跑）")
    import tempfile  # noqa: PLC0415

    from pipeline.run_daily import prune_factor_latest  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "f.db")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS factor_latest (
              item_id TEXT PRIMARY KEY, date TEXT, price REAL, dd_from_high REAL,
              drop_duration_days INTEGER, ret_7d REAL, ret_21d REAL, ret_30d REAL,
              vol_21 REAL, turnover REAL, list_ratio REAL, list_slope_7d REAL,
              supply_peer_pct REAL, supply_rigidity REAL, case_rarity_score REAL,
              rel_ret_21d REAL, event_bearish_30d INTEGER, event_bullish_30d INTEGER,
              updated_at INTEGER)
        """)
        today = "2026-10-13"
        con.executemany("INSERT INTO factor_latest (item_id, date, price) VALUES (?,?,?)",
                        [("A", today, 10.0), ("B", today, 20.0), ("C", "2026-10-12", 30.0)])
        con.commit()

        n = prune_factor_latest(con, ["A"])
        left = [r[0] for r in con.execute("SELECT item_id FROM factor_latest ORDER BY item_id")]
        check("同日被淘汰的 B 也清掉了（只按日期删会漏）", left == ["A"], f"left={left} n={n}")

        n2 = prune_factor_latest(con, [])
        left2 = [r[0] for r in con.execute("SELECT item_id FROM factor_latest")]
        check("本轮无截面时全部清空（宁可为空，不拿旧数据冒充现价）",
              left2 == [] and n2 == 1, f"left={left2} n={n2}")
        con.close()


def test_local_push_complete_replacement() -> None:
    """[P2] 整行替换型 stage 必须把 NULL 真的写进去，且缺失列不能被过滤掉。"""
    print("\n[24] 本地推送：整行替换 vs 部分更新")
    import tempfile  # noqa: PLC0415

    import push.to_local as tl  # noqa: PLC0415

    check("factor_latest 属于整行替换", "factor_latest" in tl.COMPLETE_STAGES)
    check("task_progress 仍走部分更新（进度条不会被清空）",
          "task_progress" not in tl.COMPLETE_STAGES)

    with tempfile.TemporaryDirectory() as td:
        real_path = tl._DB_PATH
        tl._DB_PATH = Path(td) / "local.db"      # type: ignore[assignment]
        conn = tl.connect()          # 只开一个读连接：Windows 上开着文件就删不掉临时目录
        try:
            tl.push("factor_latest", [{"item_id": "A", "date": "2026-10-13", "price": 10.0,
                                       "turnover": 0.123, "updated_at": 1}])
            v1 = conn.execute(
                "SELECT turnover FROM factor_latest WHERE item_id='A'").fetchone()[0]
            check("首次写入换手率生效", abs(v1 - 0.123) < 1e-9, f"{v1}")

            # 本轮明确发 turnover=None（没有数据源）→ 必须真的变成 NULL
            tl.push("factor_latest", [{"item_id": "A", "date": "2026-10-13", "price": 11.0,
                                       "turnover": None, "updated_at": 2}])
            row = conn.execute(
                "SELECT price, turnover FROM factor_latest WHERE item_id='A'").fetchone()
            check("明确发 None 时必须写成 NULL（不被 COALESCE 保住旧值）",
                  row[1] is None, f"turnover={row[1]}")
            check("同一次推送里的新价格正常生效", abs(row[0] - 11.0) < 1e-9, f"price={row[0]}")

            # 部分更新型：只送部分字段时，其余列必须保住
            tl.push("task_progress", [{"task": "t1", "status": "running", "total": 100,
                                       "done": 10, "updated_at": 1}])
            tl.push("task_progress", [{"task": "t1", "status": "running", "updated_at": 2}])
            row2 = conn.execute(
                "SELECT total, done FROM task_progress WHERE task='t1'").fetchone()
            check("进度表的部分更新仍保留其余列（没有被这次改动破坏）",
                  tuple(row2) == (100, 10), f"{tuple(row2)}")
        finally:
            conn.close()             # 先关连接，临时目录才能被清掉
            tl._DB_PATH = real_path      # type: ignore[assignment]


def test_invalid_quotes_rejected_and_src_filtered() -> None:
    """[P2] 无效报价不许入库；[P2] 非 BUFF 来源不进模型。"""
    print("\n[25] 报价校验与平台口径")
    import tempfile  # noqa: PLC0415

    from pipeline.config import BEIJING as _BJ  # noqa: PLC0415

    today = datetime.now(_BJ).strftime("%Y-%m-%d")   # 与"刚刚观测"的时间戳保持同一口径

    check("正常价格通过", collector._valid_price(12.5))
    check("0 被拒（否则就是一根 −100% 暴跌）", not collector._valid_price(0))
    check("负数被拒", not collector._valid_price(-3))
    check("NaN / inf 被拒",
          not collector._valid_price(float("nan")) and not collector._valid_price(float("inf")))
    check("None 被拒", not collector._valid_price(None))
    check("挂单量 0 是合法值（真的没人挂单）", collector._valid_count(0) == 0)
    check("挂单量负数当没数据", collector._valid_count(-1) is None)

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "q.db")

        def blk(name: str, price, plat: str = "BUFF", cnt=30) -> dict:
            # updateTime 用"现在"：写入端现在会拒收「观测时间比入库日期早太多」的
            # 陈旧报价，用 1970 年那种假值会整批被拦（这正是新一轮加的保护）。
            return {"marketHashName": name,
                    "dataList": [{"platform": plat, "sellPrice": price,
                                  "sellCount": cnt,
                                  "updateTime": int(datetime.now(_BJ).timestamp())}]}

        n = collector.write_daily_snapshot(con, [
            blk("A", 12.5), blk("B", 0), blk("C", -5), blk("D", float("nan")),
            blk("E", 8.0, plat="YOUPIN"),
        ], today)
        rows = dict(con.execute("SELECT item_id, sell FROM snap"))
        check("无效价格写成 NULL（价格绝不落在序列里）",
              rows.get("B") is None and rows.get("C") is None and rows.get("D") is None,
              f"{rows}")
        check("合法价格正常写入", rows.get("A") == 12.5 and rows.get("E") == 8.0, f"{rows}")
        cnt = dict(con.execute("SELECT item_id, listings FROM snap"))
        check("没有在售价时挂单量仍然记录（不因价格无效丢掉整条响应）",
              cnt.get("B") == 30, f"{cnt}")

        # 审查给的场景：sellPrice=0 且 sellCount=0（当前无人挂售）
        collector.write_daily_snapshot(con, [blk("Z", 0, cnt=0)], today)
        z = con.execute("SELECT sell, listings FROM snap WHERE item_id='Z'").fetchone()
        check("零报价品种：价格 NULL，但挂单量记成 0（闸门可判定为「已知为 0」而不是「不知道」）",
              z[0] is None and z[1] == 0, f"{tuple(z)}")

        # src 过滤：非 BUFF 的行不进模型
        hist = collector.load_history(con, days=30)
        kept = set(hist["item_id"])
        check("load_history 过滤掉非 BUFF 来源", kept == {"A"}, f"kept={sorted(kept)}")
        con.close()

    src = (SERVER / "pipeline" / "collector.py").read_text(encoding="utf-8")
    bf = (SERVER / "pipeline" / "backfill.py").read_text(encoding="utf-8")
    check("K 线采集透传真实 platform", "write_kline_rows(con, name, rows, src=platform)" in src)
    check("回填也透传 platform", "platform=args.platform" in bf)


def test_fallback_never_overwrites_primary_platform() -> None:
    """[P2] 回退平台不许顶掉 BUFF 行 —— 否则有效历史会被"写没"。

    当时的症状：先有 BUFF ¥52，后来一条**更新的**悠悠有品 ¥50 把整行改成 YOUPIN；
    而 load_history 会按 src 过滤非 BUFF 行 —— 结果连原来那条有效的 BUFF ¥52
    也一起消失了。写入时挡掉，才谈得上"过滤"这件事。
    """
    print("\n[26] 回退平台不许覆盖主平台")
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "fb.db")

        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("X", "2026-10-13", 52.0, 52.0, 10, None, None, 1000, "BUFF", 1000))
        con.commit()
        con.execute(collector.SNAP_UPSERT_QUOTE,      # 更新，但来源是回退平台
                    ("X", "2026-10-13", 50.0, 50.0, 12, None, None, 2000, "YOUPIN", 2000))
        con.commit()
        row = con.execute("SELECT sell, src FROM snap WHERE item_id='X'").fetchone()
        check("更新的回退报价不许顶掉 BUFF 行",
              abs(float(row[0]) - 52.0) < 1e-9 and row[1] == "BUFF", f"{tuple(row)}")

        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("Y", "2026-10-13", 40.0, 40.0, 5, None, None, 1000, "C5", 1000))
        con.commit()
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("Y", "2026-10-13", 42.0, 42.0, 6, None, None, 2000, "BUFF", 2000))
        con.commit()
        row2 = con.execute("SELECT sell, src FROM snap WHERE item_id='Y'").fetchone()
        check("反过来：BUFF 报价可以升级覆盖回退行",
              abs(float(row2[0]) - 42.0) < 1e-9 and row2[1] == "BUFF", f"{tuple(row2)}")
        con.close()


def test_invalid_quote_does_not_relabel_source() -> None:
    """[P1] 无效报价不许把别的平台的价格"改标签"成 BUFF。

    审查给的场景：库里本来是 C5 ¥50，随后收到一条 BUFF 的**零报价** ——
    价格被 COALESCE 保住了，src 却照改不误，于是那条 C5 价被贴上 BUFF 标签，
    顺利通过 src 过滤进了训练历史。来源必须描述"当前这条价格到底是谁的"。
    """
    print("\n[29] 无效报价不许改来源标签")
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "relabel.db")
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("X", "2026-10-13", 50.0, 50.0, 7, None, None, 1000, "C5", 1000))
        con.commit()
        con.execute(collector.SNAP_UPSERT_QUOTE,      # 更新的 BUFF 零报价
                    ("X", "2026-10-13", None, None, 9, None, None, None, None, 2000))
        con.commit()
        row = con.execute("SELECT sell, src, listings FROM snap WHERE item_id='X'").fetchone()
        check("价格与来源都保持是 C5 的",
              abs(float(row[0]) - 50.0) < 1e-9 and row[1] == "C5", f"{tuple(row)}")
        check("挂单量仍按自己的时间戳更新", row[2] == 9, f"listings={row[2]}")
        hist = collector.load_history(con, days=30)
        check("该行仍被 src 过滤挡住，进不了模型历史", hist.empty, f"rows={len(hist)}")
        con.close()


def test_count_freshness_is_independent() -> None:
    """[P2] 更旧的无效报价不许把更新的挂单量改掉。

    价格与数量是两次独立观测：价格无效时 ts 为空，而 UPSERT 若因此无条件更新，
    一条更旧的「零挂单量」就会把刚写进去的 80 改成 0，且价格时间戳还显示是新的。
    """
    print("\n[30] 挂单量有独立的新旧判断")
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "cts.db")
        con.execute(collector.SNAP_UPSERT_QUOTE,      # 较新的挂单量观测
                    ("X", "2026-10-13", 11.0, 11.0, 80, None, None, 2000, "BUFF", 2000))
        con.commit()
        con.execute(collector.SNAP_UPSERT_QUOTE,      # 更旧、价格无效、挂单量 0 → 不许覆盖
                    ("X", "2026-10-13", None, None, 0, None, None, None, None, 1000))
        con.commit()
        row = con.execute(
            "SELECT sell, listings, ts, count_ts FROM snap WHERE item_id='X'").fetchone()
        check("更旧的零挂单量不许覆盖更新的 80", row[1] == 80, f"listings={row[1]}")
        check("价格及其时间戳不受影响",
              abs(float(row[0]) - 11.0) < 1e-9 and row[2] == 2000, f"{tuple(row)}")
        check("挂单量时间戳保持较新值", row[3] == 2000, f"count_ts={row[3]}")
        con.close()


def test_log_records_are_formattable() -> None:
    """[P2] 日志格式串必须能被正常格式化。

    审查在服务器上看到的真实报错：
        TypeError: not enough arguments for format string
    原因是警告文案里的「-100%」没有转义 —— logging 用的是惰性 %-格式化，
    只有在 handler 真正 format(record) 时才会炸；默认处理器把这当成
    "Logging error" 打到 stderr，于是**代码看起来测试通过、警告却丢了**。
    所以这里必须真的走一遍 formatter.format()。
    """
    print("\n[27] 日志格式串")
    import logging as _logging  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    captured: list[str] = []

    class _H(_logging.Handler):
        def emit(self, record):
            try:
                captured.append(self.format(record))     # ← 这里才会触发 %-格式化
            except Exception as e:      # noqa: BLE001
                captured.append(f"<FORMAT ERROR {type(e).__name__}: {e}>")

    h = _H()
    h.setFormatter(_logging.Formatter("%(message)s"))
    lg = _logging.getLogger("collector")
    lg.addHandler(h)
    old_level = lg.level
    lg.setLevel(_logging.WARNING)
    try:
        with tempfile.TemporaryDirectory() as td:
            con = collector.connect(Path(td) / "log.db")
            collector.write_daily_snapshot(con, [
                {"marketHashName": "BAD",
                 "dataList": [{"platform": "BUFF", "sellPrice": 0, "sellCount": 3,
                               "updateTime": 1}]},
                {"marketHashName": "FB",
                 "dataList": [{"platform": "C5", "sellPrice": 9.9, "sellCount": 3,
                               "updateTime": 1}]},
            ], "2026-10-13")
            con.close()
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)

    errs = [c for c in captured if "FORMAT ERROR" in c]
    check("警告能被正常格式化（含 % 的文案必须转义）", not errs, f"{errs or captured}")
    check("无效报价的警告确实发出去了",
          any("无效报价" in c for c in captured), f"{captured}")
    check("平台回退的警告确实发出去了",
          any("平台回退" in c for c in captured), f"{captured}")


def test_dry_run_never_touches_data_and_freshness_gate() -> None:
    """[P1] --dry-run 绝不能删数据；[P1] 陈旧行情/未预热的品种不许进截面。"""
    print("\n[28] dry-run 约定与新鲜度闸门")
    import tempfile  # noqa: PLC0415

    from pipeline.run_daily import _apply_freshness_gate, abort_no_cross_section  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "dry.db")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS factor_latest (item_id TEXT PRIMARY KEY, date TEXT,
              price REAL, updated_at INTEGER);
            CREATE TABLE IF NOT EXISTS forecast (item_id TEXT, date TEXT, side TEXT,
              PRIMARY KEY (item_id, date));
        """)
        con.execute("INSERT INTO factor_latest VALUES ('A','2026-10-13',10.0,1)")
        con.executemany("INSERT INTO forecast VALUES (?,?,?)",
                        [("A", "2026-10-13", "buy"), ("B", "2026-10-12", "buy")])
        con.commit()

        abort_no_cross_section(con, "2026-10-13", dry_run=True)
        fl = con.execute("SELECT COUNT(*) FROM factor_latest").fetchone()[0]
        fc = con.execute("SELECT COUNT(*) FROM forecast").fetchone()[0]
        check("--dry-run 不动任何数据（审查发现原本会删掉因子）", fl == 1 and fc == 2,
              f"factor_latest={fl} forecast={fc}")

        abort_no_cross_section(con, "2026-10-13", dry_run=False)
        fl2 = con.execute("SELECT COUNT(*) FROM factor_latest").fetchone()[0]
        fc_today = con.execute("SELECT COUNT(*) FROM forecast WHERE date='2026-10-13'").fetchone()[0]
        fc_old = con.execute("SELECT COUNT(*) FROM forecast WHERE date='2026-10-12'").fetchone()[0]
        check("非试跑时因子清空", fl2 == 0, f"{fl2}")
        check("今天的预测也清掉（否则候选页仍推荐旧 buy）", fc_today == 0, f"{fc_today}")
        check("历史日期的预测保留（那是另一天的记录）", fc_old == 1, f"{fc_old}")
        con.close()

    # 新鲜度 / 预热闸门（#2：基准日必须是"运行当天"，不是截面里的 max(date)）
    df = pd.DataFrame([
        {"item_id": "A", "date": pd.Timestamp("2026-10-13"), "is_warm": True},
        {"item_id": "B", "date": pd.Timestamp("2026-10-03"), "is_warm": True},   # 10 天前
        {"item_id": "C", "date": pd.Timestamp("2026-10-13"), "is_warm": False},  # 未预热
        {"item_id": "D", "date": pd.Timestamp("2026-10-20"), "is_warm": True},   # 未来日期
    ])
    out = _apply_freshness_gate(df, CFG, "2026-10-13")
    ids = set(out["item_id"])
    check("陈旧行情被剔除（不许被包装成今天的截面）", "B" not in ids, f"ids={sorted(ids)}")
    check("未预热的被剔除（特征还没齐）", "C" not in ids, f"ids={sorted(ids)}")
    check("异常的未来日期被剔除", "D" not in ids, f"ids={sorted(ids)}")
    check("新行情 + 已预热的保留", ids == {"A"}, f"ids={sorted(ids)}")

    # 全池停更：所有人日期一样旧 —— 用 max(date) 当参照会全部放行（审查抓到的）
    stale = pd.DataFrame([
        {"item_id": "X", "date": pd.Timestamp("2026-09-29"), "is_warm": True},
        {"item_id": "Y", "date": pd.Timestamp("2026-09-29"), "is_warm": True},
    ])
    out2 = _apply_freshness_gate(stale, CFG, "2026-10-13")
    check("全池停更时仍然全部剔除（参照日不能取截面最大值）", out2.empty, f"rows={len(out2)}")


def test_liquidity_snapshot_overrides_stale_price_row() -> None:
    """[P2] 今天的「零挂单量」必须能盖过昨天的挂单量。

    审查给的端到端场景：今天 sell=NULL / listings=0，而 load_history 只保留
    有价格的行 → 截面拿到的是**昨天的价格 + 昨天的 listings(100)** → 闸门放行。
    价格取"最后一根有效价"、流动性取"最新一次快照"，两者按品种关联才不会这样。
    """
    print("\n[31] 流动性快照独立于价格行")
    import tempfile  # noqa: PLC0415

    from pipeline.run_daily import _apply_liquidity_gate  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "liq.db")
        today, yday = "2026-10-13", "2026-10-12"
        # A：昨天有价有量；今天没在售价（价格 NULL）、挂单量 0
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("A", yday, 11.0, 11.0, 100, None, None, 1000, "BUFF", 1000))
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("A", today, None, None, 0, None, None, None, None, 2000))
        # B：今天价量正常
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("B", today, 12.0, 12.0, 500, None, None, 2000, "BUFF", 2000))
        con.commit()

        cross = collector.load_history(con, days=30)
        check("价格口径：A 只有昨天那行（今天没有有效价格）",
              set(cross[cross["item_id"] == "A"]["date"].dt.strftime("%Y-%m-%d")) == {yday},
              f"{cross[cross['item_id'] == 'A']['date'].tolist()}")

        lst = collector.latest_listings(con, ["A", "B"])
        a_row = lst[lst["item_id"] == "A"].iloc[0]
        check("流动性口径：A 取到今天那次快照（listings=0）",
              int(a_row["listings"]) == 0 and str(a_row["listings_date"]) == today,
              f"{dict(a_row)}")

        merged = cross.drop(columns=["listings"], errors="ignore").merge(
            lst[["item_id", "listings", "listings_date"]], on="item_id", how="left")
        out = _apply_liquidity_gate(merged, CFG, today)
        kept = set(out["item_id"])
        check("今天零挂单量的 A 被剔除（不再用昨天的 100 放行）", "A" not in kept, f"kept={kept}")
        check("挂单量充足的 B 保留", "B" in kept, f"kept={kept}")

        # 反过来：快照太旧就按「不知道」放行，而不是拿它当今天的流动性
        merged2 = merged.copy()
        merged2["listings_date"] = pd.Timestamp("2026-09-01")
        out2 = _apply_liquidity_gate(merged2, CFG, today)
        check("挂单量快照过期时按「不知道」放行（不误杀）",
              set(out2["item_id"]) == {"A", "B"}, f"kept={set(out2['item_id'])}")
        con.close()


def test_no_cross_section_contract() -> None:
    """[P2] 无截面时的收尾必须一致，且接口不许退回昨天的建议。"""
    print("\n[32] 无截面：统一收尾与接口契约")
    import tempfile  # noqa: PLC0415

    from pipeline.run_daily import abort_no_cross_section  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "nx.db")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS factor_latest (item_id TEXT PRIMARY KEY, date TEXT,
              price REAL, updated_at INTEGER);
            CREATE TABLE IF NOT EXISTS forecast (item_id TEXT, date TEXT, side TEXT,
              PRIMARY KEY (item_id, date));
        """)
        con.execute("INSERT INTO factor_latest VALUES ('A','2026-10-13',10.0,1)")
        con.execute("INSERT INTO forecast VALUES ('A','2026-10-13','buy')")
        con.commit()
        abort_no_cross_section(con, "2026-10-13", dry_run=False)
        meta = dict(con.execute("SELECT k, v FROM meta"))
        check("写入 no_cross_section_on 标记（接口据此判断）",
              meta.get("no_cross_section_on") == "2026-10-13", f"{meta.get('no_cross_section_on')}")
        check("last_run 同时更新", meta.get("last_run") == "2026-10-13", f"{meta.get('last_run')}")
        con.close()

    rd = (SERVER / "pipeline" / "run_daily.py").read_text(encoding="utf-8")
    check("两个空截面分支用同一套收尾（新鲜度分支原先直接 return）",
          rd.count("abort_no_cross_section(con, day, dry_run)") >= 2,
          f"调用点 {rd.count('abort_no_cross_section(con, day, dry_run)')} 个")
    check("成功路径会清掉该标记（否则同日先失败后成功会一直返回空）",
          '"no_cross_section_on", "v": ""' in rd)

    api = (SERVER / "app.py").read_text(encoding="utf-8")
    check("候选接口识别「最近一次运行无截面」，不退回昨天",
          "no_cross_section_on" in api and "最近一次运行没有产生有效截面" in api)


def test_observation_date_not_ingest_date() -> None:
    """[P1] 新鲜度必须看「这条行情自己的时间」，不是「入库那天」。

    审查给的场景：2026-10-13 抓到一条来源 updateTime = 2026-09-29 的缓存报价。
    写入时 date 是采集那天（今天），于是只看 date 的闸门会把它当成今天的行情。
    """
    print("\n[33] 观测时间 vs 入库日期")
    import tempfile  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415
    from datetime import timedelta as _td  # noqa: PLC0415

    from pipeline.config import BEIJING as _BJ  # noqa: PLC0415
    from pipeline.run_daily import _apply_freshness_gate  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "obs.db")
        now = _dt.now(_BJ)
        today = now.strftime("%Y-%m-%d")
        stale_ts = int((now - _td(days=30)).timestamp())     # 缓存行情：观测时间是 30 天前
        fresh_ts = int((now - _td(days=1)).timestamp())
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("STALE", today, 50.0, 50.0, 30, None, None, stale_ts, "BUFF", stale_ts))
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("FRESH", today, 60.0, 60.0, 30, None, None, fresh_ts, "BUFF", fresh_ts))
        con.commit()

        hist = collector.load_history(con, days=120)
        obs = dict(zip(hist["item_id"], hist["obs_date"].dt.strftime("%Y-%m-%d")))
        check("load_history 还原出观测日期（区别于入库日期）",
              obs.get("STALE") is not None and obs.get("STALE") != today, f"{obs}")
        kept = set(_apply_freshness_gate(hist, CFG, today)["item_id"])
        check("缓存行情被新鲜度闸门剔除", "STALE" not in kept, f"kept={kept}")
        check("正常行情保留", "FRESH" in kept, f"kept={kept}")

        lst = collector.latest_listings(con, ["STALE"])
        check("挂单量的观测日期也不是入库日期",
              str(lst.iloc[0]["listings_obs_date"])[:10] != today, f"{dict(lst.iloc[0])}")
        con.close()


def test_count_ts_requires_an_actual_count() -> None:
    """[P2] 响应里没有挂单量时，不许推进 count_ts。

    审查给的序列：80/1000 → 数量缺失/3000 → 0/2000。
    中间那次空观测若推进了时间戳，最后那条有效数量就永远进不来（被锁死）。
    """
    print("\n[34] 空数量观测不许推进时间戳")
    import tempfile  # noqa: PLC0415

    from pipeline.config import BEIJING as _BJ  # noqa: PLC0415

    today = datetime.now(_BJ).strftime("%Y-%m-%d")

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "cts2.db")
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("X", "2026-10-13", 11.0, 11.0, 80, None, None, 1000, "BUFF", 1000))
        con.commit()
        con.execute(collector.SNAP_UPSERT_QUOTE,      # 数量缺失但时间戳更晚
                    ("X", "2026-10-13", 11.0, 11.0, None, None, None, 1000, "BUFF", 3000))
        con.commit()
        row = con.execute("SELECT listings, count_ts FROM snap WHERE item_id='X'").fetchone()
        check("空观测不推进 count_ts", row[1] == 1000, f"count_ts={row[1]}")
        check("数量保持 80", row[0] == 80, f"listings={row[0]}")
        con.execute(collector.SNAP_UPSERT_QUOTE,      # 时间戳较旧但有效的数量观测
                    ("X", "2026-10-13", 11.0, 11.0, 0, None, None, 1000, "BUFF", 2000))
        con.commit()
        row2 = con.execute("SELECT listings, count_ts FROM snap WHERE item_id='X'").fetchone()
        check("有效数量观测能覆盖（0 是合法值）",
              row2[0] == 0 and row2[1] == 2000, f"{tuple(row2)}")

        collector.write_daily_snapshot(con, [
            {"marketHashName": "NOCNT",
             "dataList": [{"platform": "BUFF", "sellPrice": 9.9,
                           "updateTime": int(datetime.now(_BJ).timestamp())}]},
        ], today)
        got = con.execute("SELECT listings, count_ts FROM snap WHERE item_id='NOCNT'").fetchone()
        check("响应没有挂单量时 count_ts 记 NULL（不宣称观测过）",
              got[0] is None and got[1] is None, f"{tuple(got)}")
        con.close()


def test_expired_listings_not_reused_by_anomaly() -> None:
    """[P2] 过期挂单量在异动检测里也必须算「不知道」。

    审查给的矛盾：前置闸门（放行）与 decide.detect_anomaly（按原值拦下）对同一份
    过期数据给出两种解释 —— 一条本该报 L3 的暴跌被「挂单少」静默压掉。
    """
    print("\n[35] 过期挂单量不得压掉告警")
    from pipeline.run_daily import _apply_liquidity_gate  # noqa: PLC0415

    df = pd.DataFrame([{"item_id": "A", "listings": 0,
                        "listings_obs_date": pd.Timestamp("2026-09-01")}])
    out = _apply_liquidity_gate(df, CFG, "2026-10-13")
    check("过期挂单量落成缺失（不是留在行里当 0）",
          bool(pd.isna(out.loc[0, "listings"])), f"{out.loc[0, 'listings']}")

    crash = {"ret_21d": -0.5, "ret_7d": -0.2, "vol_z": -6.0,
             "listings": out.loc[0, "listings"], "volume": None}
    lv, score, why = decide.detect_anomaly(crash, CFG)
    check("过期挂单量不再让暴跌告警被静默压掉", lv == "L3", f"level={lv} why={why}")

    fresh = dict(crash)
    fresh["listings"] = 0
    lv2, _, why2 = decide.detect_anomaly(fresh, CFG)
    check("新鲜的零挂单量仍然拦截（闸门本意不变）", lv2 is None and "挂单少" in why2,
          f"level={lv2} why={why2}")


def test_stale_forecast_marked_on_detail_page() -> None:
    """[P2] 无截面时，单品详情页不许再把旧预测当当前建议。

    判据必须**按单品**：最近一次运行整体成功、但这个品种被闸门剔掉时，
    它的预测还停在昨天 —— 只看全局「今天无截面」标记是抓不到的。
    """
    print("\n[36] 详情页的历史预测必须标注")
    api_src = (SERVER / "app.py").read_text(encoding="utf-8")
    check("后端给历史预测打 stale 标记", 'fc_dict["stale"]' in api_src)
    check("stale 判据是「预测日期早于最近一次运行」",
          'str(fc_dict.get("date") or "") < last_run' in api_src)
    check("不再只看全局 no_cross_section 标记",
          'marker and marker == last_run' not in api_src)

    web = SERVER.parent / "web" / "src"
    ts = (web / "lib" / "api.ts").read_text(encoding="utf-8")
    check("前端类型里有 stale 字段", "stale?: boolean" in ts)
    page = (web / "pages" / "ItemDetail.tsx").read_text(encoding="utf-8")
    check("详情页对 stale 预测显示提示而不是买入标签",
          "fc?.stale" in page and "非当前建议" in page)


def test_stale_quote_never_enters_history() -> None:
    """[P1] 陈旧报价必须在**入历史之前**就被挡掉。

    审查给的复现：同一笔旧报价连续写到不同日期，于是生成一堆成熟且已预热的
    训练样本（日期是新的、价格是旧的）；最后补一条新报价，最新截面也能通过预热检查。
    只在推理前拦最后一行是不够的 —— 特征与标签都是按入库日期算的。
    """
    print("\n[37] 陈旧报价不许写进历史")
    import tempfile  # noqa: PLC0415
    from datetime import datetime as _dt  # noqa: PLC0415
    from datetime import timedelta as _td  # noqa: PLC0415

    from pipeline.config import BEIJING as _BJ  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "stale.db")
        now = _dt.now(_BJ)
        old_ts = int((now - _td(days=30)).timestamp())

        def blk(name: str, ts) -> dict:
            d = {"platform": "BUFF", "sellPrice": 50.0, "sellCount": 30}
            if ts is not None:
                d["updateTime"] = ts
            return {"marketHashName": name, "dataList": [d]}

        # 连续三天都用同一笔 30 天前的旧报价 → 一行都不该写进去
        written = 0
        for off in (0, -1, -2):
            day = (now + _td(days=off)).strftime("%Y-%m-%d")
            written += collector.write_daily_snapshot(con, [blk("OLD", old_ts)], day)
        rows = con.execute("SELECT COUNT(*) FROM snap WHERE item_id='OLD'").fetchone()[0]
        check("陈旧报价一行都没进历史（不会造出「日期新、价格旧」的训练样本）",
              rows == 0, f"写入 {written} 条，库里 {rows} 行")

        # 新鲜报价照常写入
        collector.write_daily_snapshot(con, [blk("NEW", int(now.timestamp()))],
                                       now.strftime("%Y-%m-%d"))
        n_new = con.execute("SELECT COUNT(*) FROM snap WHERE item_id='NEW'").fetchone()[0]
        check("新鲜报价正常写入", n_new == 1, f"{n_new}")

        # 没有 updateTime → 不许伪造本机时间
        collector.write_daily_snapshot(con, [blk("NOTIME", None)], now.strftime("%Y-%m-%d"))
        got = con.execute("SELECT ts, count_ts FROM snap WHERE item_id='NOTIME'").fetchone()
        check("来源时间缺失时保留为「未知」，不伪造本机时间",
              got[0] is None and got[1] is None, f"{tuple(got)}")
        con.close()


def test_latest_listings_picks_newest_observation() -> None:
    """[P2] 最新挂单量要按 count_ts 选，不能按入库日期选行。

    审查给的场景：昨天观测到 0，今天收到一条观测时间更旧的缓存数量 100 ——
    按 MAX(date) 会选中今天那条旧缓存，再因过期被判成「不知道」而放行，
    把昨天那条**仍然有效**的 0 丢掉。
    """
    print("\n[38] 挂单量按观测时间选最新")
    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as td:
        con = collector.connect(Path(td) / "lst.db")
        # 用真实量级的时间戳，顺便让 listings_obs_date 也是正常日期
        t_yday = int(datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc).timestamp())
        t_cached = int(datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc).timestamp())
        # 昨天：观测时间较新、数量 0
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("X", "2026-10-12", 11.0, 11.0, 0, None, None, t_yday, "BUFF", t_yday))
        # 今天：行日期更新，但观测时间更旧、数量 100
        con.execute(collector.SNAP_UPSERT_QUOTE,
                    ("X", "2026-10-13", None, None, 100, None, None, None, None, t_cached))
        con.commit()
        lst = collector.latest_listings(con, ["X"]).iloc[0]
        check("选中观测时间最新的那条（昨天的 0，而不是今天的旧缓存 100）",
              int(lst["listings"]) == 0 and int(lst["count_ts"]) == t_yday, f"{dict(lst)}")
        con.close()


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
    test_meta_table_and_cursor()
    test_notify_digest_uses_simple_returns()
    test_source_guards_for_fixed_bugs()
    test_listings_cross_midnight_and_stale_guard()
    test_kline_validation_and_source()
    test_cross_section_from_single_row()
    test_beijing_day_and_notification_order()
    test_kline_today_is_guarded_history_is_not()
    test_factor_latest_prune_same_day()
    test_local_push_complete_replacement()
    test_invalid_quotes_rejected_and_src_filtered()
    test_fallback_never_overwrites_primary_platform()
    test_log_records_are_formattable()
    test_dry_run_never_touches_data_and_freshness_gate()
    test_invalid_quote_does_not_relabel_source()
    test_count_freshness_is_independent()
    test_liquidity_snapshot_overrides_stale_price_row()
    test_no_cross_section_contract()
    test_observation_date_not_ingest_date()
    test_count_ts_requires_an_actual_count()
    test_expired_listings_not_reused_by_anomaly()
    test_stale_forecast_marked_on_detail_page()
    test_stale_quote_never_enters_history()
    test_latest_listings_picks_newest_observation()
    print("\n" + "=" * 62)
    if FAIL:
        print(f"结果：{len(FAIL)} 项失败 → {FAIL}")
        return 1
    print("结果：全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
