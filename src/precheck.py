"""对接前置避坑工具（precheck）。

输入：用户的对接业务描述
输出：top N 高频问题模式 + 出现次数 + 典型案例链接 + 避坑建议 + 文档参考

数据流：
  description → embed → 双路召回（issues 限"实际故障 5 类 tracker" + doc chunks）
  → LLM 一次聚类（要求穷举每类 case_ids）→ 后端按 case_ids 数量排序
  → 渲染 markdown 报告
"""
from __future__ import annotations

import argparse
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


# 实际故障 tracker（precheck 召回池）—— 用户拍板：支持/BUG/适配/安全/性能
FAULT_TRACKERS = {3, 1, 22, 26, 27}


_CLUSTER_PROMPT = """你是 Redmine 工单分析助理。用户即将启动一个对接业务，希望提前知道历史上这类对接踩过哪些坑。

【业务描述】
{description}

【历史相似案件（按相似度降序，共 {nissues} 条）】
{issues_block}

【钉钉知识库相关文档片段（共 {ndocs} 条）】
{docs_block}

请按"相同问题模式"对这些案件聚类（粒度要粗,宁多合不细分,目标是抽出 5-8 个"这类对接的典型坑"）：

每类输出：
- 简短标题（≤20 字，表达"什么类型的问题"，例如"GPS 坐标系不一致 / 端口防火墙未开 / 心跳超时"）
- 该类所有 case_ids（穷尽该类工单，同一案件不能进多个 cluster；如某案件不属于任何典型类型，可不归类）
- 该类支持的文档 doc_refs（命中文档片段，可为空）
- 避坑建议 advice（≤80 字，针对"未来做这类对接"如何提前规避）

要求：
1. 只输出 JSON 对象，形如 {{"clusters": [...]}}。不要任何额外文字。
2. 每个 cluster: {{"title": "...", "case_ids": [int...], "doc_refs": ["nodeId"...], "advice": "..."}}
3. **必须输出 5-8 个 cluster**（即使部分类只有 1 个 case_id 也输出；类似问题尽量合并）。
4. 按 case_ids 数量降序排列。
5. 同一案件 id 不能出现在多个 cluster。
"""


def _build_issues_block(issues: list[dict], max_resolution_len: int = 80) -> str:
    """单条 ~150 字（id + 标题60 + 处理记录80），30 条约 4.5k 字。"""
    lines: list[str] = []
    for x in issues:
        iid = x["issue_id"]
        subj = (x.get("subject") or "").replace("\n", " ")[:60]
        res = (x.get("resolution") or "").replace("\n", " ")[:max_resolution_len]
        lines.append(f"#{iid} {subj} | 处理: {res or '(无)'}")
    return "\n".join(lines)


def _build_docs_block(docs: list[dict], max_text_len: int = 200) -> str:
    """每条 ~280 字（标题50 + 片段200），15 条约 4.2k 字。"""
    lines: list[str] = []
    for d in docs:
        nid = d.get("node_id") or ""
        title = (d.get("title") or "").replace("\n", " ")[:50]
        text = (d.get("text") or d.get("summary") or "").replace("\n", " ")[:max_text_len]
        lines.append(f"{nid} | {title} | {text}")
    return "\n".join(lines)


def _render_markdown(
    items: list[dict],
    description: str,
    n_issues: int,
    n_docs: int,
    base_redmine: str,
) -> str:
    parts: list[str] = []
    parts.append("## 对接前置避坑提示\n")
    parts.append(
        f"基于历史 **{n_issues}** 条相似工单 + **{n_docs}** 条知识库文档片段聚类得出。"
        "出现次数 = 同类历史案件数；建议在对接启动前逐项确认。\n"
    )
    if not items:
        parts.append("> 召回的历史案件未聚出 ≥2 次的重复问题模式，"
                     "可能业务较新或描述太泛。建议补充具体的产品/协议/三方系统名再试。")
        return "\n".join(parts)

    for i, item in enumerate(items, 1):
        title = item.get("title") or "(未命名)"
        count = item.get("count", 0)
        advice = item.get("advice") or ""
        cases = item.get("case_ids") or []
        docs = item.get("doc_refs_with_url") or []

        parts.append(f"### {i}. {title}（出现 **{count}** 次）")
        parts.append(f"**避坑建议**：{advice}\n")
        if cases:
            case_links = [
                f"[#{cid}]({base_redmine}/issues/{cid})" for cid in cases[:5]
            ]
            more = f" 等 {len(cases)} 条" if len(cases) > 5 else ""
            parts.append(f"**典型案例**：{' / '.join(case_links)}{more}")
        if docs:
            doc_links = [f"[{d['title']}]({d['url']})" for d in docs[:3]]
            parts.append(f"**参考文档**：{' / '.join(doc_links)}")
        parts.append("")
    parts.append("---\n*本报告由 AI 基于历史工单库自动生成。仅供参考。*")
    return "\n".join(parts)


def _parse_clusters_robust(raw: str) -> list[dict]:
    """LLM JSON 容错解析：截断/格式不全也尽量提取已闭合的 cluster 项。"""
    raw = (raw or "").strip()
    if not raw:
        return []
    # 1) 整段 JSON
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and isinstance(obj.get("clusters"), list):
            return [c for c in obj["clusters"] if isinstance(c, dict)]
        if isinstance(obj, list):
            return [c for c in obj if isinstance(c, dict)]
    except json.JSONDecodeError:
        pass
    # 2) 提取 "clusters": [ ... ] 数组的整体（即使整体 JSON 不全）
    m = re.search(r'"clusters"\s*:\s*\[', raw)
    if m:
        arr_str = raw[m.end() - 1 :]  # 从 [ 开始
        # 尝试加 ]} 闭合后再 parse
        for ending in ("", "]", "]}", '}]}', '"}]}'):
            try:
                obj = json.loads(arr_str + ending)
                if isinstance(obj, list):
                    return [c for c in obj if isinstance(c, dict) and c.get("title")]
            except json.JSONDecodeError:
                continue
    # 3) 用括号栈提取所有完整闭合的 {...}（任意嵌套层级）
    out: list[dict] = []
    stack: list[int] = []
    in_str = False
    escape = False
    for i, ch in enumerate(raw):
        if escape:
            escape = False
            continue
        if ch == "\\" and in_str:
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            snippet = raw[start : i + 1]
            if '"title"' in snippet and '"case_ids"' in snippet:
                try:
                    obj = json.loads(snippet)
                    if isinstance(obj, dict) and obj.get("title"):
                        out.append(obj)
                except json.JSONDecodeError:
                    pass
    return out


def run_precheck(
    description: str,
    top_issues: int = 30,
    top_docs: int = 10,
    min_cosine_issue: float = 0.50,
    min_cosine_doc: float = 0.45,
    fault_trackers: set[int] | None = None,
) -> dict:
    """precheck 主流程。

    Returns:
        {
            "items": [{title, count, case_ids, doc_refs, doc_refs_with_url, advice}],
            "markdown": "...",
            "stats": {n_issues, n_docs, n_clusters, elapsed_ms},
        }
    """
    c = cfg()
    t0 = time.time()
    text = build_issue_text("[对接前置]", description)
    emb = Embedder().embed([text])[0]

    trackers = fault_trackers if fault_trackers is not None else FAULT_TRACKERS

    # 1. issues 召回：限 tracker
    vs = get_vector_store()
    issues_raw = vs.knn(emb, top=top_issues * 2, tracker_filter=trackers)
    issues = [x for x in issues_raw if x["cosine"] >= min_cosine_issue][:top_issues]

    # 2. chunks 召回（已 backfill 完）
    cs = get_chunk_store()
    docs: list[dict] = []
    if cs._index.ntotal > 0:
        raw_hits = cs.knn(emb, top=top_docs * 3)
        # 按 node_id 聚合每 doc 取最高 chunk
        best_by_nid: dict[str, dict] = {}
        for h in raw_hits:
            if h["cosine"] < min_cosine_doc:
                continue
            nid = h["node_id"]
            if nid not in best_by_nid or h["cosine"] > best_by_nid[nid]["cosine"]:
                best_by_nid[nid] = h
        sorted_hits = sorted(best_by_nid.values(), key=lambda x: -x["cosine"])[:top_docs]
        # 补 title/url + 命中 chunk±1 段上下文
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

    if not issues:
        return {
            "items": [],
            "markdown": "## 对接前置避坑\n\n> 召回 0 条相似历史案件，可能业务描述过短或过于通用。请补充产品、协议、三方系统等关键词后重试。",
            "stats": {"n_issues": 0, "n_docs": len(docs), "n_clusters": 0,
                       "elapsed_ms": int((time.time() - t0) * 1000)},
        }

    # 3. LLM 聚类
    prompt = _CLUSTER_PROMPT.format(
        description=description[:1500],
        nissues=len(issues),
        ndocs=len(docs),
        issues_block=_build_issues_block(issues),
        docs_block=_build_docs_block(docs) if docs else "(无)",
    )
    # max_tokens 必须给足 reasoning_content + content 两份配额：
    # DeepSeek-v4 是 reasoning model，reasoning_content 也消耗 max_tokens 配额。
    # 实测 prompt 一大 reasoning 一思考就把 4500 全占了，content 输出为空。
    raw = _call(
        [
            {"role": "system", "content": "You output only JSON, no prose."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=12000,
    )
    if not raw or not raw.strip():
        # 仍空 → 缩半重试一次（更小输入 + 仍给足 max_tokens）
        sys.stderr.write("[precheck] LLM returned empty, retrying with smaller input\n")
        smaller_prompt = _CLUSTER_PROMPT.format(
            description=description[:800],
            nissues=min(15, len(issues)),
            ndocs=min(5, len(docs)),
            issues_block=_build_issues_block(issues[:15], max_resolution_len=60),
            docs_block=_build_docs_block(docs[:5], max_text_len=150) if docs else "(无)",
        )
        raw = _call(
            [
                {"role": "system", "content": "You output only JSON, no prose."},
                {"role": "user", "content": smaller_prompt},
            ],
            max_tokens=8000,
        )
    sys.stderr.write(
        f"[precheck DEBUG] LLM raw len={len(raw)}, head[:300]={raw[:300]!r}\n"
    )
    clusters = _parse_clusters_robust(raw)
    sys.stderr.write(
        f"[precheck DEBUG] parsed {len(clusters)} raw clusters; "
        f"case_id counts: {[len(c.get('case_ids') or []) for c in clusters[:10]]}\n"
    )

    # 4. 后处理：count = len(case_ids)，排序，补 doc 元数据
    doc_by_nid = {d["node_id"]: d for d in docs}
    items: list[dict] = []
    seen_case_ids: set[int] = set()
    for cl in clusters:
        case_ids = []
        for cid in (cl.get("case_ids") or []):
            try:
                cid_int = int(cid)
            except (ValueError, TypeError):
                continue
            if cid_int in seen_case_ids:
                continue  # 同案件不能进多个 cluster
            seen_case_ids.add(cid_int)
            case_ids.append(cid_int)
        if not case_ids:
            continue  # 至少 1 条，否则该 cluster 没意义
        doc_refs = cl.get("doc_refs") or []
        doc_refs_with_url = [
            {"node_id": d, "title": doc_by_nid[d]["title"], "url": doc_by_nid[d]["url"]}
            for d in doc_refs
            if d in doc_by_nid
        ]
        items.append({
            "title": cl.get("title") or "(未命名)",
            "count": len(case_ids),
            "case_ids": case_ids,
            "doc_refs": doc_refs,
            "doc_refs_with_url": doc_refs_with_url,
            "advice": cl.get("advice") or "",
        })
    items.sort(key=lambda x: -x["count"])
    items = items[:8]

    base = c["redmine"]["base_url"].rstrip("/")
    md = _render_markdown(items, description, len(issues), len(docs), base)
    return {
        "items": items,
        "markdown": md,
        "stats": {
            "n_issues": len(issues),
            "n_docs": len(docs),
            "n_clusters": len(items),
            "elapsed_ms": int((time.time() - t0) * 1000),
        },
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser(description="对接前置避坑 CLI")
    p.add_argument("description", help="对接业务描述，例如：'做车载GPS轨迹对接，对方808协议走TCP'")
    p.add_argument("--top-issues", type=int, default=50)
    p.add_argument("--top-docs", type=int, default=15)
    p.add_argument("--json", action="store_true", help="只输出 JSON")
    args = p.parse_args()
    res = run_precheck(args.description, top_issues=args.top_issues, top_docs=args.top_docs)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(res["markdown"])
        print(f"\n[stats] {res['stats']}")
