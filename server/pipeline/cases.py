"""箱子稀有度：整套判断的支点。

核心因果链：
    箱子稀有 → 开箱量低 → 该箱物品新增供给趋近于零 → 存世量饱和
    → 价格下跌变成「存量恐慌抛售」而不是「供给洪水的价值毁灭」
    → 下方有硬支撑，这才是能赚钱的那种下跌

⚠️ 本文件现在只负责**读**（load_cases）。
   真正算稀有度、写 case_meta 的是 pipeline/cases_build.py —— 它用
   「箱子价格 + 箱子年龄 + 箱内物品价格中位数 + 30 日箱价趋势」四个
   可观测代理量评分，并且明确把拿不到的字段留空，不拿近似值冒充。

   这里历史上还有四套东西，都已删除（需要时从 git 历史取回）：

   score_cases_rule_based / score_cases_llm / RARITY_PROMPT
       —— 基于 case_volume_30d（Steam 成交量）与 supply_growth_30d
          （存世量 30 日增速）的规则/LLM 评分。这两个字段 SteamDT 都不提供、
          Steam 市场页面服务器又访问不到，所以整套方案从根上不可用 ——
          它和 cases_build 的关系不是「新旧」，而是「不可用 vs 可用」，
          两套并列摆着只会让人分不清哪个是线上口径。

   write_cases
       —— 直接 INSERT OR REPLACE 写 case_meta（REPLACE 会删行重插）。
          已被 cases_build.main() 的幂等 UPSERT 取代，后者还会清理已下线的箱子。

   collect_case_metrics
       —— 抓 steamcommunity.com/market/priceoverview 拿箱子成交量。
          **服务器访问不了 steamcommunity.com**，这个函数在此环境永远跑不通
          （这是任务里明确记过的事实），留着只会误导。

   DROP_POOL_OVERRIDES
       —— 掉落池人工兜底表，注释里说要靠 llm.extract_case_changes() 维护，
          而那个函数从未存在过。掉落池状态现在由 cases_build.infer_drop_pool()
          从「箱子价格 + 年龄」推断。
"""
from __future__ import annotations

import sqlite3
from typing import Any


def load_cases(con: sqlite3.Connection) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM case_meta").fetchall()
    return [dict(r) for r in rows]
