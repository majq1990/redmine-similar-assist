"""检查钉钉知识库 MCP URL 健康度，超 25 天或调用失败发钉钉机器人提醒。

每天 09:00 由 cron 触发。

判断 unhealthy 的两个条件（任一）：
  1. data/dingtalk_mcp_url.txt 文件 mtime > age_threshold 天前
  2. POST tools/list 失败（401 / 网络错误 / 5xx）

unhealthy 时推送独立钉钉机器人（加签），title 含 key_alert_keyword 通过关键字校验。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .config import cfg, project_root


def _read_mcp_url() -> tuple[str, float | None]:
    """返回 (url, file_mtime_or_None)。mtime 为 None 表示走 fallback。"""
    c = cfg().get("dingtalk_mcp") or {}
    url_file = project_root() / (c.get("url_file") or "data/dingtalk_mcp_url.txt")
    if url_file.exists():
        url = url_file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        return url, url_file.stat().st_mtime
    return c.get("fallback_url", ""), None


def _probe_mcp(url: str) -> dict:
    """POST tools/list 测可达性。返回 {ok, status, err}"""
    if not url:
        return {"ok": False, "err": "empty url"}
    data = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    ).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        body = resp.read().decode("utf-8", errors="replace")
        d = json.loads(body)
        tool_count = len(d.get("result", {}).get("tools", []))
        return {"ok": True, "status": resp.status, "tool_count": tool_count}
    except urllib.error.HTTPError as e:  # type: ignore
        return {"ok": False, "status": e.code, "err": str(e)[:200]}
    except Exception as e:
        return {"ok": False, "err": str(e)[:200]}


def _sign_dingtalk(secret: str) -> tuple[str, str]:
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return ts, sign


def _push_dingtalk(webhook: str, secret: str, title: str, markdown: str) -> dict:
    url = webhook
    if secret:
        ts, sign = _sign_dingtalk(secret)
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={sign}"
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": markdown},
        # @ 马健权 手机号 15928716057
        "at": {"atMobiles": ["15928716057"], "isAtAll": False},
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    try:
        body = urllib.request.urlopen(req, timeout=15).read().decode("utf-8")
        return json.loads(body)
    except Exception as e:
        return {"errmsg": str(e), "errcode": -1}


def run() -> dict:
    c = cfg().get("notify") or {}
    age_threshold = int(c.get("key_alert_age_days", 25))
    webhook = c.get("key_alert_webhook") or ""
    secret = c.get("key_alert_secret") or ""
    keyword = c.get("key_alert_keyword", "提醒")

    url, mtime = _read_mcp_url()
    now = time.time()
    age_days = (now - mtime) / 86400 if mtime else None
    probe = _probe_mcp(url)

    age_alert = age_days is not None and age_days > age_threshold
    file_missing = mtime is None
    probe_fail = not probe.get("ok")
    unhealthy = age_alert or file_missing or probe_fail

    result = {
        "url_present": bool(url),
        "file_mtime": dt.datetime.fromtimestamp(mtime).isoformat() if mtime else None,
        "age_days": round(age_days, 1) if age_days is not None else None,
        "file_missing": file_missing,
        "age_alert": age_alert,
        "probe": probe,
        "unhealthy": unhealthy,
    }

    if not unhealthy:
        result["action"] = "no-op (healthy)"
        return result

    if not webhook:
        result["action"] = "skipped (no key_alert_webhook configured)"
        return result

    # 组装告警 markdown
    reasons = []
    if file_missing:
        reasons.append(f"⚠️ MCP URL 文件不存在（本机推送失败/从未推送过）")
    if age_alert:
        reasons.append(f"⏰ Key 已使用 **{age_days:.1f} 天**（阈值 {age_threshold} 天）")
    if probe_fail:
        reasons.append(
            f"❌ MCP 调用失败：`{probe.get('status', 'N/A')} {probe.get('err', '')[:120]}`"
        )

    title = f"{keyword}：钉钉知识库 MCP key 健康告警"
    md = [
        f"### 🔑 {title}\n",
        f"@15928716057 马健权请检查并更新 demo 上的钉钉知识库 MCP key。\n",
        "**问题**:",
        *[f"- {r}" for r in reasons],
        "",
        "**修复步骤**:",
        "1. 本机 PowerShell 跑 `D:\\git\\redmine-similar-assist\\scripts\\push_mcp_key_to_demo.ps1`",
        "2. 如果本机也过期，重新登录钉钉 MCP 控制台拿新 URL，更新 `~/.claude.json`",
        "3. 重跑 push 脚本",
        "",
        f"**当前状态**: 文件时间 `{result['file_mtime']}` | age `{result['age_days']}` 天 | probe `{probe.get('ok')}`",
    ]
    resp = _push_dingtalk(webhook, secret, title, "\n".join(md))
    result["dingtalk_response"] = resp
    result["action"] = "alert sent"
    return result


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(run(), ensure_ascii=False, indent=2))
