"""每日使用统计 + 钉钉机器人推送。

cron 每工作日（周一到周五）18:00 跑。
统计当日（00:00-23:59 UTC+8）的 assist_log 数据，
组装 Markdown，POST 到钉钉机器人 webhook（加签鉴权）。

节假日处理：先简单按 weekday 过滤（周一到周五）。
后续可接 apihubs.cn 节假日 API 区分调休（参考 reference_china_holiday_apis.md）。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import sqlite3
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import requests

from .config import cfg, project_root


def _today_range() -> tuple[str, str]:
    """返回当日 [00:00, 23:59:59] ISO 字符串（本地时间 UTC+8）。"""
    today = dt.date.today()
    return (
        f"{today.isoformat()}T00:00:00",
        f"{today.isoformat()}T23:59:59",
    )


def _query_today(log_db_path: Path) -> dict:
    conn = sqlite3.connect(str(log_db_path))
    start, end = _today_range()
    rows = conn.execute(
        "SELECT issue_id, processed_at, candidates_json, note_written "
        "FROM assist_log WHERE processed_at >= ? AND processed_at <= ? "
        "ORDER BY processed_at",
        (start, end),
    ).fetchall()
    total_all = conn.execute("SELECT COUNT(*) FROM assist_log").fetchone()[0]
    conn.close()
    return {"today_rows": rows, "total_all": total_all, "date": dt.date.today().isoformat()}


def _format_markdown(data: dict, base_url: str) -> tuple[str, str]:
    """返回 (title, markdown_text)。title 必须含「AI」关键词以满足钉钉机器人关键词校验。"""
    rows = data["today_rows"]
    total_today = len(rows)
    written = sum(1 for r in rows if r[3])
    empty_picks = sum(1 for r in rows if not r[2] or r[2] == "[]")
    has_picks = total_today - empty_picks
    total_picks = 0
    pick_counts: list[int] = []
    score_buckets: Counter = Counter()
    top_picks_sample: list[dict] = []  # 给用户展示几条最高分推荐

    for r in rows:
        try:
            picks = json.loads(r[2] or "[]")
        except Exception:
            picks = []
        pick_counts.append(len(picks))
        total_picks += len(picks)
        for p in picks:
            sc = p.get("score", 0)
            if sc >= 0.9:
                score_buckets["0.9+"] += 1
            elif sc >= 0.8:
                score_buckets["0.8-0.9"] += 1
            elif sc >= 0.7:
                score_buckets["0.7-0.8"] += 1
            elif sc >= 0.5:
                score_buckets["0.5-0.7"] += 1
            else:
                score_buckets["<0.5"] += 1
            top_picks_sample.append(
                {
                    "src_issue": r[0],
                    "rec_issue": p.get("issue_id"),
                    "score": sc,
                    "subject": p.get("subject", ""),
                }
            )

    # 取置信度 top 5 作为亮点
    top_picks_sample.sort(key=lambda x: -x["score"])
    highlights = top_picks_sample[:5]

    date = data["date"]
    keyword = (cfg().get("notify") or {}).get("dingtalk_keyword", "")
    # 把关键字嵌进 title 末尾（钉钉机器人校验需要）
    title = (
        f"AI 召回日报 {date}｜今日 {total_today} 单 / 推荐 {total_picks} 条 / 漏推 {empty_picks if total_today else 0} 单"
    )
    if keyword and keyword not in title:
        title = f"{title}（{keyword}监控）"

    if total_today == 0:
        md = (
            f"### 🤖 AI 召回日报 · {date}\n\n"
            f"> 今日无新建支持工单进入候选范围\n\n"
            f"累计处理工单（历史全部）: **{data['total_all']}**\n"
        )
        return title, md

    md_parts: list[str] = []
    md_parts.append(f"### 🤖 AI 召回日报 · {date}\n")
    md_parts.append(f"**今日处理工单**: {total_today}")
    write_rate = written * 100 // total_today if total_today else 0
    md_parts.append(f"**写回 AI 一楼**: {written} ({write_rate}%)")
    md_parts.append(
        f"**空召回**: {empty_picks} | **有推荐**: {has_picks}\n"
    )
    md_parts.append(f"**累计推荐相似案件**: {total_picks} 条")
    if total_today:
        md_parts.append(f"**平均每单**: {total_picks/total_today:.2f} 条\n")

    if score_buckets:
        md_parts.append("**置信度分布**:")
        for b in ("0.9+", "0.8-0.9", "0.7-0.8", "0.5-0.7", "<0.5"):
            n = score_buckets.get(b, 0)
            if n:
                tag = "✅" if b in ("0.9+", "0.8-0.9") else "⚠️" if b == "<0.5" else "·"
                md_parts.append(f"- {tag} {b}: {n}")
        md_parts.append("")

    if highlights:
        md_parts.append("**今日 Top 5 强匹配**:")
        for i, h in enumerate(highlights, 1):
            subj = (h["subject"] or "").replace("\n", " ")[:40]
            md_parts.append(
                f"{i}. [#{h['src_issue']}]({base_url}/issues/{h['src_issue']}) "
                f"→ [#{h['rec_issue']}]({base_url}/issues/{h['rec_issue']}) "
                f"`{h['score']:.2f}` {subj}"
            )
        md_parts.append("")

    md_parts.append(f"---")
    md_parts.append(
        f"累计历史处理 **{data['total_all']}** 单 | "
        f"[服务状态]({base_url.replace('faq.egova.com.cn:7787', 'demo.egova.com.cn')}/redmine-assist/health)"
    )

    return title, "\n".join(md_parts)


def _sign_dingtalk(secret: str) -> tuple[str, str]:
    """钉钉加签：返回 (timestamp, sign)。"""
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    return ts, sign


def _post_dingtalk(webhook: str, secret: str | None, title: str, markdown: str) -> dict:
    url = webhook
    if secret:
        ts, sign = _sign_dingtalk(secret)
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


def run() -> dict:
    c = cfg()
    notify = c.get("notify") or {}
    webhook = notify.get("dingtalk_webhook") or ""
    secret = notify.get("dingtalk_secret") or ""
    if not webhook:
        return {"error": "no dingtalk_webhook configured in config.notify"}

    log_db_path = project_root() / c["storage"]["log_db"]
    if not log_db_path.exists():
        return {"error": f"assist_log db not found at {log_db_path}"}

    data = _query_today(log_db_path)
    base_url = c["redmine"]["base_url"].rstrip("/")
    title, md = _format_markdown(data, base_url)

    resp = _post_dingtalk(webhook, secret, title, md)
    return {
        "date": data["date"],
        "today_count": len(data["today_rows"]),
        "total_all": data["total_all"],
        "dingtalk_response": resp,
        "title": title,
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    res = run()
    print(json.dumps(res, ensure_ascii=False, indent=2))
