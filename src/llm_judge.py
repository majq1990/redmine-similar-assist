"""DeepSeek LLM 精排 + 解决方案抽取。

输入：新 issue 文本 + N 条候选（每条含 subject + resolution_text）
输出：JSON list，每条 {issue_id, related: bool, score: 0..1, solution: str}
"""
from __future__ import annotations

import json
import re
from typing import Any

import requests
from tenacity import retry, stop_after_attempt, wait_exponential

from .config import cfg

_PROMPT = """你是 Redmine 工单助理。你的任务：判断每条"历史案卷"和"新工单"是否真相关，并从历史案卷中提炼一句话解决方案。

【新工单】
{new_text}

【历史候选】
{candidates}

要求：
1. 只输出 JSON 对象，形如 {{"results": [...]}}。不要任何额外文字、Markdown、解释。
2. results 数组每条结构：{{"issue_id": <int>, "related": <bool>, "score": <0~1 浮点>, "solution": "<不超过 80 字>"}}
3. 不相关时 related=false，solution 留空字符串。
4. solution 必须是"当时怎么解的"的事实陈述，不要写"建议"二字。
5. 严格按 issue_id 顺序输出，每条候选必须有对应一条结果。
"""


@retry(stop=stop_after_attempt(2), wait=wait_exponential(min=2, max=8))
def _call(messages: list[dict]) -> str:
    c = cfg()["llm"]
    payload = {
        "model": c["model"],
        "messages": messages,
        "temperature": c.get("temperature", 0.1),
        "max_tokens": c.get("max_tokens", 800),
        "response_format": {"type": "json_object"},
    }
    r = requests.post(
        c["endpoint"],
        headers={
            "Authorization": f"Bearer {c['api_key']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _extract_json_array(raw: str) -> list[Any]:
    # DeepSeek json_object 模式有时把数组包成 {"results":[...]}
    raw = raw.strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\[[\s\S]*\]", raw)
        if not m:
            return []
        obj = json.loads(m.group(0))
    if isinstance(obj, list):
        return obj
    for k in ("results", "data", "items"):
        if isinstance(obj.get(k), list):
            return obj[k]
    return []


def judge(new_text: str, candidates: list[dict]) -> list[dict]:
    """candidates: [{issue_id, subject, resolution}]"""
    if not candidates:
        return []
    cand_lines = []
    for i, c in enumerate(candidates, 1):
        cand_lines.append(
            f"  {i}. issue_id={c['issue_id']}\n"
            f"     标题: {c.get('subject') or ''}\n"
            f"     当时处理记录: {c.get('resolution') or '(无)'}"
        )
    prompt = _PROMPT.format(new_text=new_text[:2000], candidates="\n".join(cand_lines))
    raw = _call(
        [
            {"role": "system", "content": "You output only JSON, no prose."},
            {"role": "user", "content": prompt},
        ]
    )
    return _extract_json_array(raw)


_DOC_PROMPT = """你是 Redmine 工单助理。你的任务：判断每条"知识库文档"和"新工单"是否**主题相关**，并提炼文档要点。

【新工单】
{new_text}

【知识库文档候选】
{candidates}

判定标准（**对文档放宽到"主题/模块相关即可"**，不要求文档直接给出解决方案）：
- 同模块/同业务领域/同技术组件 → related=true，score≥0.6
- 同关键词/同场景 → related=true，score≥0.5
- 仅个别名词重合但主题完全不同 → related=false
- 注意：文档是"参考资料"性质，宁可多保留一条相关的，也不要错杀。

要求：
1. 只输出 JSON 对象，形如 {{"results": [...]}}。不要任何额外文字、Markdown、解释。
2. results 数组每条结构：{{"node_id": "<str>", "related": <bool>, "score": <0~1 浮点>, "solution": "<不超过 80 字的要点摘录>"}}
3. 不相关时 related=false，solution 留空字符串。
4. solution 字段写文档关键要点（一两句），不是"建议"。
5. 严格按 node_id 顺序输出，每条候选必须有对应一条结果。
"""


def judge_docs(new_text: str, candidates: list[dict]) -> list[dict]:
    """文档专用 gate：用宽松 prompt 判主题相关性。

    candidates: [{node_id, title, summary}]
    返回: [{node_id, related, score, solution}]
    """
    if not candidates:
        return []
    cand_lines = []
    for i, c in enumerate(candidates, 1):
        nid = c.get("node_id") or ""
        cand_lines.append(
            f"  {i}. node_id={nid}\n"
            f"     标题: {c.get('title') or ''}\n"
            f"     摘要: {c.get('summary') or '(无)'}"
        )
    prompt = _DOC_PROMPT.format(
        new_text=new_text[:2000], candidates="\n".join(cand_lines)
    )
    raw = _call(
        [
            {"role": "system", "content": "You output only JSON, no prose."},
            {"role": "user", "content": prompt},
        ]
    )
    return _extract_json_array(raw)
