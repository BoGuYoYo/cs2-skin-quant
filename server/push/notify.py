"""通知通道：Bark(iOS) / 企业微信机器人 / 飞书机器人。

只负责「推出去」，去重与冷却在 decide/alerting 层做——不要在这里判断业务。
"""
from __future__ import annotations

import logging
import math
import os

import requests

log = logging.getLogger("notify")

BARK = os.environ.get("BARK_URL", "").rstrip("/")
WECOM = os.environ.get("WECOM_URL", "")
FEISHU = os.environ.get("FEISHU_URL", "")

ICON = {"L1": "🟡", "L2": "🟠", "L3": "🔴"}


def log_to_simple_pct(v: float) -> float:
    """对数净收益 → 简单净收益百分比。**全系统只此一处实现。**

    为什么值当单独抽一个函数：「对数还是简单收益」是这个项目反复踩的坑。
    forecast 表里的 q10_adj / q50 / q90_adj 都是**对数净收益**，
    直接按百分比打印会严重夸大亏损 —— log=-2.7181 真实是亏 93.4%，
    当百分比打出来却是 -271.8%（比本金全亏还夸张）。

    前端的对应实现是 web/src/lib/format.ts 的 logPct()，两边必须一致；
    run_daily._print_top（终端）与 notify_digest（手机）也都用这个函数。
    """
    return math.expm1(float(v)) * 100.0


def _bark(title: str, body: str, level: str) -> bool:
    if not BARK:
        return False
    try:
        # Bark 支持把内容拼在 path 上；用 JSON 更稳
        r = requests.post(
            f"{BARK}",
            json={
                "title": title,
                "body": body,
                "group": "skin-alpha",
                "level": "critical" if level == "L3" else "timeSensitive",
                "icon": "https://cdn-icons-png.flaticon.com/512/2917/2917995.png",
            },
            timeout=10,
        )
        return r.ok
    except Exception as e:
        log.warning("bark 推送失败：%s", e)
        return False


def _wecom(title: str, body: str) -> bool:
    if not WECOM:
        return False
    try:
        r = requests.post(
            WECOM,
            json={"msgtype": "markdown", "markdown": {"content": f"**{title}**\n{body}"}},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        log.warning("企业微信推送失败：%s", e)
        return False


def _feishu(title: str, body: str) -> bool:
    if not FEISHU:
        return False
    try:
        r = requests.post(
            FEISHU,
            json={"msg_type": "text", "content": {"text": f"{title}\n{body}"}},
            timeout=10,
        )
        return r.ok
    except Exception as e:
        log.warning("飞书推送失败：%s", e)
        return False


def notify_item(name: str, level: str, price: float, detail: str, link: str = "") -> bool:
    """单条暴跌提醒"""
    title = f"{ICON.get(level, '⚪')} {level} 异动 · {name}"
    body = f"现价 ¥{price:.2f}\n{detail}"
    if link:
        body += f"\n{link}"
    sent = False
    sent |= _bark(title, body, level)
    sent |= _wecom(f"{title}\n{detail}", f"> 现价 ¥{price:.2f}" + (f"\n> [打开详情]({link})" if link else ""))
    sent |= _feishu(title, body)
    if not sent:
        log.info("[本地打印] %s | %s | %s", level, name, detail)
    return sent


def notify_digest(date: str, candidates: list[dict], fails: list[str] | None = None) -> bool:
    """每日候选池摘要

    ⚠️ p10 / p50 是**对数净收益**，必须过 log_to_simple_pct 再打印。
    踩过的坑：原先写 f"{c['p10']:+.1%}"，把对数当百分比 ——
    于是「真实亏 55.9%」的单子在手机上显示成「-81.8%」，
    而 P10 恰恰是整套设计最倚重的那个数（下沿都亏就不碰），
    却是唯一一个你不在电脑前时阅读的渠道。终端和前端都是对的，只有手机错。
    """
    lines = [
        f"- {c['name']} ¥{c['price']:.2f}"
        f" · P10 {log_to_simple_pct(c['p10']):+.1f}%"
        f" · P50 {log_to_simple_pct(c['p50']):+.1f}%"
        f" · 仓位 {c['size']:.2%}"
        for c in candidates[:15]
    ]
    title = f"📋 候选池 {date} · {len(candidates)} 个"
    body = "\n".join(lines) or "今日无候选"
    if fails:
        body += "\n\n⚠️ 异常：" + "; ".join(fails)
    sent = _bark(title, body, "L1")
    sent |= _wecom(title, body)
    sent |= _feishu(title, body)
    if not sent:
        log.info("[本地打印] %s\n%s", title, body)
    return sent
