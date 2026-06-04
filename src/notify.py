"""钉钉机器人通知工具函数。

从 daily_stats.py 提取的共享模块，供 triage / daily_stats / key_health_check 等使用。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse

import requests


def sign_dingtalk(secret: str) -> tuple[str, str]:
    """钉钉加签：返回 (timestamp, sign)。"""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return ts, sign


def post_dingtalk(webhook: str, secret: str | None, title: str, markdown: str) -> dict:
    """发送钉钉 markdown 消息。

    Args:
        webhook: 完整 webhook URL（含 access_token）
        secret: 加签 secret（SEC 开头），为空则不加签
        title: 消息标题
        markdown: markdown 正文

    Returns:
        钉钉 API 响应 dict
    """
    url = webhook
    if secret:
        ts, sign = sign_dingtalk(secret)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={sign}"
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown},
    }
    r = requests.post(
        url, json=payload, timeout=15, headers={"Content-Type": "application/json"}
    )
    try:
        return r.json()
    except Exception:
        return {"http_status": r.status_code, "text": r.text[:200]}
