"""把 Redmine issue 的富文本 description/notes 清成嵌入友好的纯文本。

Redmine 上观察到的噪声：
  - <p><img src=...> 大量图片占位
  - data-clipboard-cangjie 钉钉富文本嵌套 JSON
  - <pre> 里整段 Java 堆栈（对召回有用但太长，截断）
  - <diffs-container> / <code> 带海量 CSS 变量
  - &nbsp; / &#39; 等 HTML 实体

策略：
  1. BeautifulSoup 拿纯 text
  2. 单独抽出 <pre>/<code> 里的代码块，截前 400 字
  3. 去 cangjie JSON、CSS 变量段
  4. 折叠多空白
  5. 长度截到 4000 字符（bge-m3 上限 8192 token，但太长意义不大）
"""
from __future__ import annotations

import html
import re

from bs4 import BeautifulSoup

_CANGJIE_RE = re.compile(r'data-clipboard-cangjie="[^"]*"')
_CSS_VAR_RE = re.compile(r"--[a-z][a-z0-9-]*:\s*[^;\"]+;?", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_MAX_LEN = 4000
_MAX_CODE_LEN = 400


def clean_html(text: str | None) -> str:
    if not text:
        return ""

    # 1. 砍 cangjie 属性（里面是 JSON 占用大量 token）
    text = _CANGJIE_RE.sub("", text)

    soup = BeautifulSoup(text, "lxml")

    # 2. 单独处理代码块，截断后塞回
    code_blocks: list[str] = []
    for tag in soup.find_all(["pre", "code"]):
        code = tag.get_text(" ", strip=True)
        if code:
            if len(code) > _MAX_CODE_LEN:
                code = code[:_MAX_CODE_LEN] + "…(truncated)"
            code_blocks.append(code)
        tag.decompose()

    # 3. 去 diffs-container（CSS 变量地狱）
    for tag in soup.find_all("diffs-container"):
        tag.decompose()
    # img 标签替换为 [img]
    for img in soup.find_all("img"):
        img.replace_with("[img]")

    body = soup.get_text(" ", strip=True)
    body = html.unescape(body)
    body = _CSS_VAR_RE.sub("", body)
    body = _WS_RE.sub(" ", body).strip()

    if code_blocks:
        body = body + "\n[code] " + " || ".join(code_blocks)

    if len(body) > _MAX_LEN:
        body = body[:_MAX_LEN] + "…(truncated)"
    return body


def build_issue_text(subject: str, description_html: str) -> str:
    """主题 + 清洗后正文。用于 embedding。"""
    desc = clean_html(description_html)
    return f"[标题] {subject}\n[正文] {desc}"


def detect_r_and_d_communication(journals: list[dict]) -> tuple[bool, list[str]]:
    """检测 journals 中是否有研发沟通/补提/已处理等信号。

    只分析 journal notes 文本（不含截图/附件），按用户要求：
    - "有截图，不能作为已经沟通的证据"
    - "案件的文字内容进行识别，发现有和研发沟通过的记录、有提到补提等信息"

    Returns:
        (has_signal, matched_keywords)
        has_signal: True 表示已与研发沟通过，应跳过 AI 分析
        matched_keywords: 命中的关键词列表（用于通知文案）
    """
    # 研发相关关键词（出现任一即认为已沟通）
    PATTERNS = [
        # 直接提到研发
        "研发", "开发", "RD", "技术部", "研发部", "研发同事",
        # 补提/已处理
        "补提", "已补提", "已处理", "已解决", "已修复",
        # 沟通确认
        "已沟通", "已确认", "已反馈", "已告知", "已对接",
        # 状态类
        "重复问题", "已知问题", "非bug", "非缺陷", "设计如此",
    ]
    matched = []
    for j in journals:
        notes_raw = j.get("notes") or ""
        notes_text = clean_html(notes_raw)
        if not notes_text:
            continue
        for kw in PATTERNS:
            if kw in notes_text and kw not in matched:
                matched.append(kw)
    return bool(matched), matched


def find_resolution_notes(journals: list[dict]) -> str:
    """从 journals 里挑解决方案候选：

    优先返回最后一次状态切到 closed 那条 journal 的 notes（+ 前后各一条），
    都没有则返回最后一条非空 notes。
    """
    if not journals:
        return ""
    close_idx = -1
    for i, j in enumerate(journals):
        for d in j.get("details") or []:
            if d.get("property") == "attr" and d.get("name") == "status_id":
                # 5 = 已关闭（Redmine 默认）
                if str(d.get("new_value")) in ("5", "6"):
                    close_idx = i
    if close_idx >= 0:
        window = journals[max(0, close_idx - 1) : close_idx + 2]
    else:
        window = [j for j in journals if (j.get("notes") or "").strip()][-2:]
    parts = []
    for j in window:
        notes = clean_html(j.get("notes"))
        if notes:
            parts.append(notes)
    return " || ".join(parts)
