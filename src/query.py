"""政通问答工具（zhengtong_query）。

输入：用户自然语言问题
输出：从 17 万工单 + 4600 文档中检索相关方案，LLM 精排+摘要后返回 markdown

数据流：
  query → embed → 双路召回（issues 全 tracker + doc chunks）
  → LLM 精排+摘要 → 渲染 markdown
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any

from .config import cfg
from .embedder import Embedder
from .llm_judge import _call, _extract_json_array
from .text_cleaner import build_issue_text, get_chunk_with_context
from .vector_store import get_chunk_store, get_doc_store, get_vector_store


_QUERY_PROMPT = """你是「政通问答」助理。用户提出了一个关于公司项目/产品/技术的问题，你需要基于检索到的历史工单和知识库文档，给出结构化的回答。

【用户问题】
{query}

【检索到的相关历史工单（共 {nissues} 条）】
{issues_block}

【检索到的知识库文档片段（共 {ndocs} 条）】
{docs_block}

请完成以下任务：
1. 判断每条历史工单是否与用户问题**真正相关**（related=true/false + score 0~1）
2. 判断每条文档片段是否与用户问题**主题相关**（related=true/false + score 0~1）
3. 基于所有相关结果，生成一段**结构化回答**（markdown 格式，含要点列表和参考链接）

要求：
1. 只输出 JSON 对象，形如 {{"results": [...], "answer": "..."}}。不要任何额外文字。
2. results 数组每条结构：{{"id": "<issue_id 或 node_id>", "type": "issue" 或 "doc", "related": <bool>, "score": <0~1>}}
3. answer 字段是一段 markdown 文本（≤800 字），结构：
   - 先用 2-3 句话直接回答用户问题
   - 然后列出关键要点（每条≤60 字）
   - 最后附上相关工单链接和文档链接
4. 对工单放宽判定：同模块/同产品/同技术组件 → related=true
5. 对文档进一步放宽：同业务领域/同关键词 → related=true
"""


def _build_issues_block(issues: list[dict], max_resolution_len: int = 100) -> str:
    lines: list[str] = []
    for x in issues:
        iid = x["issue_id"]
        subj = (x.get("subject") or "").replace("\n", " ")[:80]
        res = (x.get("resolution") or "").replace("\n", " ")[:max_resolution_len]
        status = x.get("status") or ""
        lines.append(f"#{iid} [{status}] {subj} | 处理: {res or '(无)'}")
    return "\n".join(lines)


def _build_docs_block(docs: list[dict], max_text_len: int = 250) -> str:
    lines: list[str] = []
    for d in docs:
        nid = d.get("node_id") or ""
        title = (d.get("title") or "").replace("\n", " ")[:60]
        text = (d.get("text") or d.get("summary") or "").replace("\n", " ")[:max_text_len]
        lines.append(f"{nid} | {title} | {text}")
    return "\n".join(lines)


def _render_markdown(
    answer: str,
    query: str,
    n_issues: int,
    n_docs: int,
    related_issues: list[dict],
    related_docs: list[dict],
    base_redmine: str,
) -> str:
    parts: list[str] = []
    parts.append(f"## 政通问答\n")
    parts.append(f"> **问题**：{query}\n")

    if answer:
        parts.append(answer)
    else:
        parts.append("> 未能从知识库中找到直接相关的信息，请尝试补充更多关键词。")

    parts.append("\n---\n")

    if related_issues:
        parts.append(f"### 相关工单（{len(related_issues)} 条）\n")
        for x in related_issues[:8]:
            iid = x["issue_id"]
            subj = (x.get("subject") or "").replace("\n", " ")[:60]
            parts.append(f"- [#{iid}]({base_redmine}/issues/{iid}) {subj}")
        parts.append("")

    if related_docs:
        parts.append(f"### 相关文档（{len(related_docs)} 篇）\n")
        for d in related_docs[:5]:
            title = (d.get("title") or "").replace("\n", " ")[:60]
            url = d.get("url") or ""
            if url:
                parts.append(f"- [{title}]({url})")
            else:
                parts.append(f"- {title}")
        parts.append("")

    parts.append(
        f"*本次检索: {n_issues} 工单 / {n_docs} 文档片段*"
    )
    return "\n".join(parts)


def run_query(
    query: str,
    top_issues: int = 15,
    top_docs: int = 10,
    min_cosine_issue: float = 0.45,
    min_cosine_doc: float = 0.40,
) -> dict:
    """政通问答主流程。

    Returns:
        {
            "markdown": "...",
            "stats": {n_issues, n_docs, elapsed_ms},
        }
    """
    c = cfg()
    t0 = time.time()
    text = build_issue_text("[政通问答]", query)
    emb = Embedder().embed([text])[0]

    # 1. issues 召回：不限 tracker（所有类型）
    vs = get_vector_store()
    issues_raw = vs.knn(emb, top=top_issues * 2)
    issues = [x for x in issues_raw if x["cosine"] >= min_cosine_issue][:top_issues]

    # 2. chunks 召回 + 按 doc 聚合
    cs = get_chunk_store()
    docs: list[dict] = []
    if cs._index.ntotal > 0:
        raw_hits = cs.knn(emb, top=top_docs * 3)
        best_by_nid: dict[str, dict] = {}
        for h in raw_hits:
            if h["cosine"] < min_cosine_doc:
                continue
            nid = h["node_id"]
            if nid not in best_by_nid or h["cosine"] > best_by_nid[nid]["cosine"]:
                best_by_nid[nid] = h
        sorted_hits = sorted(best_by_nid.values(), key=lambda x: -x["cosine"])[:top_docs]
        ds = get_doc_store()
        for h in sorted_hits:
            meta = ds.get_meta(h["node_id"]) or {}
            all_chunks = cs.get_doc_chunks(h["node_id"])
            pos = next((i for i, x in enumerate(all_chunks) if x["idx"] == h["chunk_idx"]), 0)
            ctx = get_chunk_with_context(all_chunks, hit_idx=pos, neighbors=1)
            docs.append(
                {
                    "node_id": h["node_id"],
                    "title": meta.get("title") or "",
                    "url": meta.get("url") or "",
                    "text": ctx,
                    "cosine": h["cosine"],
                }
            )

    # 3. LLM 精排 + 摘要
    prompt = _QUERY_PROMPT.format(
        query=query[:1500],
        nissues=len(issues),
        ndocs=len(docs),
        issues_block=_build_issues_block(issues) if issues else "(无)",
        docs_block=_build_docs_block(docs) if docs else "(无)",
    )
    raw = _call(
        [
            {"role": "system", "content": "You output only JSON, no prose."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=8000,
    )

    answer = ""
    related_issue_ids: set[int] = set()
    related_doc_nids: set[str] = set()

    if raw and raw.strip():
        try:
            obj = json.loads(raw.strip())
            answer = (obj.get("answer") or "").strip()
            for r in (obj.get("results") or []):
                if not r.get("related"):
                    continue
                rid = r.get("id", "")
                rtype = r.get("type", "")
                if rtype == "issue":
                    try:
                        related_issue_ids.add(int(rid))
                    except (ValueError, TypeError):
                        pass
                elif rtype == "doc":
                    related_doc_nids.add(str(rid))
        except json.JSONDecodeError:
            pass

    # fallback: 如果 LLM 没返回有效结果，取 top cosine 作为相关
    if not related_issue_ids and not related_doc_nids and issues:
        related_issue_ids = {x["issue_id"] for x in issues[:5]}
    if not related_doc_nids and docs:
        related_doc_nids = {d["node_id"] for d in docs[:3]}

    related_issues = [x for x in issues if x["issue_id"] in related_issue_ids]
    related_docs = [d for d in docs if d["node_id"] in related_doc_nids]

    base = c["redmine"]["base_url"].rstrip("/")
    md = _render_markdown(
        answer, query, len(issues), len(docs),
        related_issues, related_docs, base,
    )
    return {
        "markdown": md,
        "stats": {
            "n_issues": len(issues),
            "n_docs": len(docs),
            "elapsed_ms": int((time.time() - t0) * 1000),
        },
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import argparse
    p = argparse.ArgumentParser(description="政通问答 CLI")
    p.add_argument("query", help="自然语言问题")
    p.add_argument("--top-issues", type=int, default=15)
    p.add_argument("--top-docs", type=int, default=10)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    res = run_query(args.query, top_issues=args.top_issues, top_docs=args.top_docs)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(res["markdown"])
