"""主流程：ingest_new_issue(issue_id) → 检索 → LLM gate → 写 note。"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from .config import cfg, is_project_targeted, project_root
from .embedder import Embedder
from .llm_judge import judge, judge_docs
from .redmine_client import RedmineClient
from .text_cleaner import (
    build_issue_text,
    clean_html,
    find_resolution_notes,
    get_chunk_with_context,
)
from .vector_store import (
    VectorStore,
    get_chunk_store,
    get_doc_store,
    get_vector_store,
)


def _ensure_log_db() -> sqlite3.Connection:
    path = project_root() / cfg()["storage"]["log_db"]
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS assist_log(
              issue_id   INTEGER PRIMARY KEY,
              processed_at TEXT,
              candidates_json TEXT,
              note_written INTEGER DEFAULT 0
           )"""
    )
    conn.commit()
    return conn


def _already_processed(conn: sqlite3.Connection, issue_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM assist_log WHERE issue_id=?", (issue_id,)
    ).fetchone()
    return row is not None


def _html_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_note(picks: list[dict], doc_picks: list[dict] | None = None) -> str:
    """生成 Redmine CKEditor 友好的 HTML note，双栏：历史工单 + 知识库文档。"""
    c = cfg()["write_back"]
    base = cfg()["redmine"]["base_url"].rstrip("/")
    parts: list[str] = []
    parts.append(f'<p><strong>{_html_escape(c["note_header"])}</strong></p>')

    if picks:
        parts.append("<p><strong>[相似历史工单]</strong>：</p>")
        parts.append("<ol>")
        for p in picks:
            iid = p["issue_id"]
            url = f"{base}/issues/{iid}"
            subject = _html_escape(p.get("subject") or "")
            solution = _html_escape(p.get("solution") or "")
            score_pct = int(p["score"] * 100)
            item = (
                f'<li><a href="{url}">#{iid} {subject}</a> '
                f"<em>(置信度 {score_pct}%)</em>"
            )
            if solution:
                item += f"<br/>当时解决方案：{solution}"
            item += "</li>"
            parts.append(item)
        parts.append("</ol>")

    if doc_picks:
        parts.append("<p><strong>[相关知识库文档]</strong>：</p>")
        parts.append("<ol>")
        for d in doc_picks:
            url = _html_escape(d.get("url") or "")
            title = _html_escape(d.get("title") or "")
            solution = _html_escape(d.get("solution") or "")
            score_pct = int(d["score"] * 100)
            item = (
                f'<li><a href="{url}"> {title}</a> '
                f"<em>(置信度 {score_pct}%)</em>"
            )
            if solution:
                item += f"<br/>要点：{solution}"
            item += "</li>"
            parts.append(item)
        parts.append("</ol>")

    parts.append(f'<p><em>{_html_escape(c["note_footer"])}</em></p>')
    return "".join(parts)


def _build_empty_note() -> str:
    """picks=[] 时写"暂无推荐"一楼，让 Redmine 用户看到 AI 已检查过。"""
    c = cfg()["write_back"]
    parts: list[str] = []
    parts.append(f'<p><strong>{_html_escape(c["note_header"])}</strong></p>')
    parts.append(
        "<p>已检索全公司历史案件，<strong>未找到与本工单相似度足够高的历史记录</strong>。</p>"
    )
    parts.append(
        "<p>可能原因：（1）本工单涉及新业务或新模块；（2）历史记录中暂无同类问题；"
        "（3）问题描述较简，向量召回门槛不足。建议人工排查或在解决后将本工单设为相似案例参考。</p>"
    )
    parts.append(f'<p><em>{_html_escape(c["note_footer"])}</em></p>')
    return "".join(parts)


def ingest_new_issue(issue_id: int, dry_run: bool | None = None) -> dict:
    c = cfg()
    rc = RedmineClient()
    issue = rc.get_issue(issue_id, include="journals")
    proj_id = (issue.get("project") or {}).get("id")
    if not is_project_targeted(proj_id):
        return {"skipped": "project_not_in_whitelist", "project_id": proj_id}
    tracker_id = (issue.get("tracker") or {}).get("id")
    tracker_whitelist = c.get("tracker_whitelist") or []
    if tracker_whitelist and tracker_id not in tracker_whitelist:
        return {"skipped": "tracker_not_in_whitelist", "tracker_id": tracker_id}

    log = _ensure_log_db()
    if _already_processed(log, issue_id):
        return {"skipped": "already_processed", "issue_id": issue_id}

    # 第二层幂等：检查 Redmine journals 看是否已有 AI 一楼（防止 assist_log 被清掉后重复写）
    ai_user_id = (c.get("redmine") or {}).get("ai_user_id")
    note_header = (c.get("write_back") or {}).get("note_header", "[AI 智能助理]")
    # 取 note_header 的特征前缀（去掉变化部分）作匹配
    marker = note_header.split("（")[0].split("(")[0].strip()
    for j in issue.get("journals") or []:
        ju = (j.get("user") or {}).get("id")
        jn = j.get("notes") or ""
        if (not ai_user_id or ju == ai_user_id) and marker in jn:
            return {
                "skipped": "ai_note_already_exists",
                "issue_id": issue_id,
                "existing_journal_id": j.get("id"),
            }

    subject = issue.get("subject") or ""
    text = build_issue_text(subject, issue.get("description") or "")

    emb = Embedder().embed([text])[0]

    vs = get_vector_store()
    top_n = c["recall"]["knn_top"]
    candidates = vs.knn(emb, top=top_n, exclude_id=issue_id)
    candidates = [x for x in candidates if x["cosine"] >= c["recall"]["min_cosine"]]
    candidates = candidates[: c["recall"]["llm_filter_top"]]

    # 同时从钉钉知识库召回 top docs
    # 默认走 chunk 模式（B1）：先在 chunks 召回，按 node_id 聚合每 doc 取最高分 chunk
    # 退路：config.recall.doc_chunks_mode='doc' 走旧的整篇 embed
    doc_candidates: list[dict] = []
    docs_top = int(c["recall"].get("docs_knn_top", 5))
    docs_min = float(c["recall"].get("docs_min_cosine", 0.55))
    mode = (c["recall"].get("doc_chunks_mode") or "chunk").lower()
    try:
        ds = get_doc_store()
        if mode == "chunk":
            cs = get_chunk_store()
            # 守护：ChunkStore 还没数据（B1 全量 backfill 未完成）时自动回退 doc 模式
            if cs._index.ntotal == 0:
                import sys as _sys
                _sys.stderr.write(
                    "[pipeline] chunk mode but ChunkStore empty, "
                    "fallback to doc mode\n"
                )
                mode = "doc"
        if mode == "chunk":
            # 多召 3 倍 chunks，按 node_id 聚合再截到 docs_top
            raw_hits = cs.knn(emb, top=docs_top * 3)
            # 按 node_id 取最高分 chunk
            best_by_nid: dict[str, dict] = {}
            for h in raw_hits:
                if h["cosine"] < docs_min:
                    continue
                nid = h["node_id"]
                if nid not in best_by_nid or h["cosine"] > best_by_nid[nid]["cosine"]:
                    best_by_nid[nid] = h
            # 按 cosine 排序取 docs_top，从 docs_meta 补 title/url
            sorted_hits = sorted(
                best_by_nid.values(), key=lambda x: -x["cosine"]
            )[:docs_top]
            for h in sorted_hits:
                meta = ds.get_meta(h["node_id"])
                if not meta:
                    continue
                doc_candidates.append(
                    {
                        "node_id": h["node_id"],
                        "title": meta.get("title") or "",
                        "url": meta.get("url") or "",
                        "summary": meta.get("summary") or "",
                        "cosine": h["cosine"],
                        "chunk_idx": h["chunk_idx"],
                        "chunk_heading": h["heading"],
                        "chunk_text": h["text"],
                    }
                )
        else:
            # 旧路径：整篇 embed 召回
            doc_candidates = [
                d for d in ds.knn(emb, top=docs_top) if d["cosine"] >= docs_min
            ]
    except Exception as e:
        import sys as _sys
        _sys.stderr.write(
            f"[pipeline] doc knn failed for {issue_id} (mode={mode}): {e}\n"
        )

    if not candidates and not doc_candidates:
        # 既没工单也没文档召回 → 写"暂无推荐"一楼
        do_write = (dry_run is False) if dry_run is not None else c["write_back"]["enabled"]
        empty_note = _build_empty_note()
        wrote = False
        if do_write:
            try:
                rc.add_note(issue_id, empty_note)
                wrote = True
            except Exception as e:
                import sys as _sys
                _sys.stderr.write(f"[pipeline] empty-note write failed for {issue_id}: {e}\n")
        log.execute(
            "INSERT INTO assist_log(issue_id, processed_at, candidates_json, note_written) "
            "VALUES(?,?,?,?)",
            (issue_id, time.strftime("%Y-%m-%dT%H:%M:%S"), "[]", 1 if wrote else 0),
        )
        log.commit()
        return {"issue_id": issue_id, "candidates": [], "doc_candidates": [], "wrote": wrote, "note": empty_note}

    # 1) Redmine 工单 LLM gate
    picks: list[dict] = []
    if candidates:
        verdicts = judge(
            text,
            [
                {"issue_id": x["issue_id"], "subject": x["subject"], "resolution": x["resolution"]}
                for x in candidates
            ],
        )
        by_id = {x["issue_id"]: x for x in candidates}
        for v in verdicts:
            if not v.get("related"):
                continue
            src = by_id.get(int(v.get("issue_id", 0)))
            if not src:
                continue
            picks.append(
                {
                    "issue_id": src["issue_id"],
                    "subject": src["subject"],
                    "score": float(v.get("score", src["cosine"])),
                    "solution": v.get("solution", ""),
                }
            )
        picks.sort(key=lambda x: -x["score"])
        picks = picks[: c["recall"]["final_top"]]

    # 2) 知识库文档 LLM gate（独立、宽松 prompt）
    # chunk 模式：summary 用"命中 chunk + 前后各一段"代替整篇前 600 字，让 LLM 看到的是真正相关的段落
    doc_picks: list[dict] = []
    if doc_candidates:
        try:
            # chunk 模式下批量取每个 doc 的全部 chunks，构造上下文
            ctx_by_nid: dict[str, str] = {}
            if mode == "chunk":
                try:
                    cs = get_chunk_store()
                    for d in doc_candidates:
                        all_chunks = cs.get_doc_chunks(d["node_id"])
                        hit_idx = d.get("chunk_idx", 0)
                        # chunks 已按 idx 升序，hit_idx 就是位置
                        pos = next(
                            (i for i, x in enumerate(all_chunks) if x["idx"] == hit_idx),
                            0,
                        )
                        ctx_by_nid[d["node_id"]] = get_chunk_with_context(
                            all_chunks, hit_idx=pos, neighbors=1
                        )
                except Exception as e:
                    import sys as _sys
                    _sys.stderr.write(
                        f"[pipeline] build chunk context failed: {e}\n"
                    )
            doc_verdicts = judge_docs(
                text,
                [
                    {
                        "node_id": d["node_id"],
                        "title": d.get("title") or "",
                        "summary": (
                            ctx_by_nid.get(d["node_id"])
                            or (d.get("summary") or "")
                        )[:1500],
                    }
                    for d in doc_candidates
                ],
            )
            by_nid = {d["node_id"]: d for d in doc_candidates}
            min_doc_score = float(c["recall"].get("docs_min_llm_score", 0.5))
            for v in doc_verdicts:
                if not v.get("related"):
                    continue
                score = float(v.get("score", 0))
                if score < min_doc_score:
                    continue
                nid = v.get("node_id")
                src = by_nid.get(str(nid)) if nid else None
                if not src:
                    continue
                doc_picks.append(
                    {
                        "node_id": src["node_id"],
                        "title": src["title"],
                        "url": src["url"],
                        "score": score,
                        "solution": v.get("solution", ""),
                    }
                )
            doc_picks.sort(key=lambda x: -x["score"])
            doc_picks = doc_picks[: int(c["recall"].get("docs_final_top", 3))]
        except Exception as e:
            import sys as _sys
            _sys.stderr.write(f"[pipeline] doc judge failed: {e}\n")

    do_write = (dry_run is False) if dry_run is not None else c["write_back"]["enabled"]
    # 两栏合并：工单 + 文档。任一栏有就走 _build_note，全空走 _build_empty_note
    if picks or doc_picks:
        note = _build_note(picks, doc_picks)
    else:
        note = _build_empty_note()

    wrote = False
    if do_write:
        try:
            rc.add_note(issue_id, note)
            wrote = True
        except Exception as e:
            import sys as _sys
            _sys.stderr.write(f"[pipeline] note write failed for {issue_id}: {e}\n")

    # assist_log 仍存 picks（工单类）兼容老逻辑；双栏完整数据存 candidates_json
    log.execute(
        "INSERT INTO assist_log(issue_id, processed_at, candidates_json, note_written) "
        "VALUES(?,?,?,?)",
        (
            issue_id,
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            __import__("json").dumps(
                {"picks": picks, "doc_picks": doc_picks}, ensure_ascii=False
            ),
            1 if wrote else 0,
        ),
    )
    log.commit()
    return {
        "issue_id": issue_id,
        "picks": picks,
        "doc_picks": doc_picks,
        "note": note,
        "wrote": wrote,
    }


if __name__ == "__main__":
    import io, sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser()
    p.add_argument("issue_id", type=int)
    p.add_argument("--write", action="store_true", help="真写回 Redmine（覆盖 config）")
    p.add_argument("--dry-run", action="store_true", help="只生成不写")
    p.add_argument("--out", type=str, default=None, help="把结果写文件而不是 stdout")
    args = p.parse_args()
    dry = True if args.dry_run else (False if args.write else None)
    res = ingest_new_issue(args.issue_id, dry_run=dry)
    text = __import__("json").dumps(res, ensure_ascii=False, indent=2)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
        print(f"wrote {args.out}")
    else:
        print(text)
