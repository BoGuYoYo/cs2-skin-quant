"""通知通道：Bark(iOS) / 企业微信机器人 / 飞书机器人。

只负责「推出去」，去重与冷却在 decide/alerting 层做——不要在这里判断业务。
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("notify")

BARK = os.environ.get("BARK_URL", "").rstrip("/")
WECOM = os.environ.get("WECOM_URL", "")
FEISHU = os.environ.get("FEISHU_URL", "")

ICON = {"L1": "🟡", "L2": "🟠", "L3": "🔴"}


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
    """每日候选池摘要"""
    lines = [f"- {c['name']} ¥{c['price']:.2f} · P10 {c['p10']:+.1%} · P50 {c['p50']:+.1%} · 仓位 {c['size']:.2%}"
             for c in candidates[:15]]
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
