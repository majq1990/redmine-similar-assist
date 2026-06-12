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
_MAX_FORM_RECORDS_LEN = 5000


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


def build_form_records_text(
    records: list[dict], max_len: int = _MAX_FORM_RECORDS_LEN
) -> str:
    """把 form_* 研发/测试操作记录整理成适合 embedding 和 LLM 的文本。"""
    if not records:
        return ""

    lines: list[str] = []
    seen: set[str] = set()
    # 越靠后的流程记录通常越接近最终解决结论，优先保留，避免总长度截断
    # 时把测试结果和审核意见裁掉。
    for record in reversed(records):
        parts: list[str] = []
        for field in record.get("fields") or []:
            raw_value = field.get("value")
            value = clean_html(
                raw_value
                if isinstance(raw_value, str)
                else ("" if raw_value is None else str(raw_value))
            )
            if not value or value in ("/", "-", "无", "暂无"):
                continue
            if field.get("name") in ("result", "test_result"):
                value = {"1": "通过", "0": "不通过"}.get(value, value)
            parts.append(f"{field.get('label') or field.get('name')}: {value}")
        if not parts:
            continue
        line = f"[{record.get('label') or record.get('source')}] " + " | ".join(parts)
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)

    text = "\n".join(lines)
    if len(text) > max_len:
        text = text[:max_len] + "…(truncated)"
    return text


def build_resolution_text(journal_resolution: str, form_records_text: str) -> str:
    """合并普通 journal 解决记录与结构化研发/测试记录。"""
    parts: list[str] = []
    if journal_resolution:
        parts.append("[处理记录] " + journal_resolution)
    if form_records_text:
        parts.append(form_records_text)
    text = "\n".join(parts)
    if len(text) > _MAX_FORM_RECORDS_LEN:
        text = text[:_MAX_FORM_RECORDS_LEN] + "…(truncated)"
    return text


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
    # ---- 排除模式：匹配到这些表示是"请求研发帮助"而非"研发已处理" ----
    EXCLUDE_PATTERNS = [
        "需研发", "请研发", "需要研发", "研发支持",
        "研发配合", "研发协助", "研发处理", "研发排查",
    ]

    # ---- 肯定式关键词：出现任一即认为研发已沟通过 ----
    AFFIRM_PATTERNS = [
        # 研发已完成/确认（"研发已" 是最核心的信号）
        "研发已", "研发确认", "研发回复", "研发反馈",
        "研发回复已", "研发反馈已", "研发处理完成",
        # 补提
        "已补提", "已提单", "补提",
        # 沟通确认
        "已沟通", "已确认", "已反馈", "已告知", "已对接",
        # 状态类
        "已处理", "已解决", "已修复", "已修改",
        "重复问题", "已知问题", "非bug", "非缺陷", "设计如此",
    ]

    matched = []
    excluded = []
    for j in journals:
        notes_raw = j.get("notes") or ""
        notes_text = clean_html(notes_raw)
        if not notes_text:
            continue
        # 先检查排除模式
        for ep in EXCLUDE_PATTERNS:
            if ep in notes_text and ep not in excluded:
                excluded.append(ep)
        # 再检查肯定式模式
        for kw in AFFIRM_PATTERNS:
            if kw in notes_text and kw not in matched:
                matched.append(kw)

    # 如果同时命中排除模式和肯定模式，需要进一步判断
    # 排除模式优先：如果一个 journal 同时包含"需研发"和"研发已"，
    # "研发已"才是真正的处理证据；但如果只有"需研发"没有肯定式，则排除
    if matched and excluded:
        # 有肯定式证据存在，不排除
        pass
    elif excluded and not matched:
        # 只有排除模式，没有肯定式 → 不触发
        return False, []

    return bool(matched), matched


# ============ 处理路径已确认：代码迁移 / 发更新包 ============
# 这类案件研发/区域已给出明确处理方式（按 wiki 取包更新、组件迁移到项目分支等），
# 落到支持部时本质是"照单执行"，需要主动通知支持部群。
_MIGRATION_PATTERNS = [
    "代码迁移", "组件迁移", "迁移组件", "迁移代码", "迁移到", "迁移分支",
    "迁移一下", "迁分支", "迁到", "合并到分支", "合并分支", "合并代码",
    "代码合并", "cherry-pick", "cherrypick", "迁移升级", "迁移组件升级",
]
_PACKAGE_PATTERNS = [
    "更新包", "升级包", "补丁包", "全量包", "增量包",
    "发更新包", "发布更新包", "提供更新包", "发下更新包", "发下更新",
    "发包", "发版", "出包", "出更新包", "发下包", "发个包", "打个包",
    "请发包", "给测试发包", "请给测试发包", "发下最新版", "最新版更新包",
]


def detect_confirmed_handling_path(
    subject: str, description_html: str | None, journals: list[dict]
) -> tuple[bool, list[str], list[str]]:
    """检测案件是否属于"处理路径已明确"类型：代码迁移 / 发更新包。

    扫描 标题 + 描述 + journal notes 的纯文本。这类案件区域/研发已给出
    确定的处理方式，落到支持部就是照单执行，应主动通知支持部群。

    Returns:
        (has_signal, matched_keywords, path_types)
        path_types ⊆ {"代码迁移", "发更新包"}
    """
    blob_parts = [subject or "", clean_html(description_html)]
    for j in journals:
        blob_parts.append(clean_html(j.get("notes") or ""))
    blob = " ".join(p for p in blob_parts if p)
    if not blob:
        return False, [], []

    matched: list[str] = []
    types: list[str] = []
    for kw in _MIGRATION_PATTERNS:
        if kw in blob and kw not in matched:
            matched.append(kw)
            if "代码迁移" not in types:
                types.append("代码迁移")
    for kw in _PACKAGE_PATTERNS:
        if kw in blob and kw not in matched:
            matched.append(kw)
            if "发更新包" not in types:
                types.append("发更新包")

    return bool(matched), matched, types


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
