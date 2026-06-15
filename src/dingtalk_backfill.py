"""钉钉知识库全量入库到 vec_docs。

递归遍历 config.dingtalk_mcp.workspace_id 下所有 ALIDOC 文档：
  list_nodes → 拉 markdown → 清洗 → embed → upsert
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from .config import cfg, project_root
from .dingtalk_mcp_client import DingtalkMcpClient
from .embedder import Embedder
from .text_cleaner import split_markdown_chunks
from .vector_store import get_chunk_store, get_doc_store


_WS_RE = re.compile(r"\s+")
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_markdown(md: str, max_len: int = 4000) -> str:
    if not md:
        return ""
    # 砍图片占位
    md = _IMG_RE.sub("[img]", md)
    # 砍内联 HTML 标签
    md = _HTML_TAG_RE.sub(" ", md)
    md = _WS_RE.sub(" ", md).strip()
    if len(md) > max_len:
        md = md[:max_len] + "…(truncated)"
    return md


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _build_embed_text(title: str, body: str) -> str:
    return f"[标题] {title}\n[正文] {body}"


def _build_chunk_embed_text(title: str, heading: str, text: str) -> str:
    """chunk 的 embedding 输入：拼上文档标题 + chunk heading 路径 + chunk 正文。

    标题前置可提升语义信号（避免 chunk 失去文档主题），heading 路径已含上下文。
    """
    head = f"{title} - {heading}" if heading else title
    return f"[标题] {head}\n[正文] {text}"


def run(rebuild: bool = False, limit: int | None = None) -> dict:
    c = cfg()
    ws_id = (c.get("dingtalk_mcp") or {}).get("workspace_id")
    if not ws_id:
        return {"error": "config.dingtalk_mcp.workspace_id missing"}

    mcp = DingtalkMcpClient()
    health = mcp.health()
    if not health.get("ok"):
        return {"error": "MCP unhealthy", "detail": health}

    ds = get_doc_store()
    cs = get_chunk_store()
    em = Embedder()

    t0 = time.time()
    processed = 0
    docs_updated = 0  # 整篇 vec_docs 更新数（保留旧索引）
    chunks_inserted = 0
    skipped = 0
    failed = 0

    for node in mcp.walk_documents(ws_id):
        nid = node.get("nodeId")
        title = node.get("name") or ""
        url = node.get("docUrl") or f"https://alidocs.dingtalk.com/i/nodes/{nid}"
        update_time = node.get("updateTime")

        processed += 1

        # 第一道增量：update_time 未变 → 跳过（最便宜）
        if not rebuild:
            existing = ds.get_meta(nid)
            if existing and existing.get("update_time") == update_time:
                skipped += 1
                continue

        # 拉 markdown
        try:
            md = mcp.get_document_markdown(nid)
        except Exception as e:
            sys.stderr.write(f"[backfill] fetch failed {nid} {title}: {e}\n")
            failed += 1
            continue

        # 1) 整篇 embed 输入（保留 DocStore 作为回退路径）
        cleaned = _clean_markdown(md)
        full_embed_text = _build_embed_text(title, cleaned)
        full_hash = _hash(full_embed_text)

        # 第二道增量：hash 未变 → 仍要确认 chunks 是否完整，否则 skip
        if not rebuild:
            existing = ds.get_meta(nid)
            if existing and existing.get("embed_text_hash") == full_hash:
                # docs_meta hash 未变；只要 chunks 也存在就 skip
                if cs.get_chunks_hash(nid):
                    skipped += 1
                    continue

        # 2) 切 chunks
        chunks = split_markdown_chunks(md)
        if not chunks:
            # 文档空/全图 → 跳过 chunk 入库，但仍更新 docs_meta（保留可见性）
            sys.stderr.write(f"[backfill] empty chunks {nid} {title}\n")

        # 3) 一次性 embed：[full_text, chunk0, chunk1, ...]
        chunk_inputs = [
            _build_chunk_embed_text(title, ch["heading"], ch["text"])
            for ch in chunks
        ]
        all_inputs = [full_embed_text] + chunk_inputs
        try:
            embs = em.embed(all_inputs)
        except Exception as e:
            sys.stderr.write(f"[backfill] embed failed {nid} {title}: {e}\n")
            failed += 1
            continue
        full_emb = embs[0]
        chunk_embs = embs[1:]

        # 4) 写 DocStore（整篇 + 元数据）
        ds.upsert(
            nid,
            full_emb,
            {
                "node_id": nid,
                "workspace_id": ws_id,
                "title": title,
                "url": url,
                "summary": cleaned[:300],
                "update_time": update_time,
                "embed_text_hash": full_hash,
            },
        )
        docs_updated += 1

        # 5) 写 ChunkStore（先删旧 chunks，再批量写新 chunks）
        if chunks:
            cs.delete_doc(nid)
            items = []
            for ch, emb in zip(chunks, chunk_embs):
                ehash = _hash(
                    _build_chunk_embed_text(title, ch["heading"], ch["text"])
                )
                items.append(
                    (
                        nid,
                        ch["idx"],
                        list(emb),
                        ch["heading"],
                        ch["text"],
                        ehash,
                    )
                )
            cs.upsert_many(items)
            chunks_inserted += len(items)

        if processed % 50 == 0:
            elapsed = time.time() - t0
            print(
                f"[backfill] processed={processed} docs_updated={docs_updated} "
                f"chunks_inserted={chunks_inserted} skipped={skipped} "
                f"failed={failed} elapsed={elapsed:.0f}s",
                flush=True,
            )

        if limit and processed >= limit:
            break

        # 钉钉 MCP 限速保护
        time.sleep(0.1)

    # 写 sync_state
    state_path = project_root() / c["storage"]["sync_state"]
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    state["last_dingtalk_backfill_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["dingtalk_doc_count"] = processed
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "processed": processed,
        "docs_updated": docs_updated,
        "chunks_inserted": chunks_inserted,
        "skipped": skipped,
        "failed": failed,
        "elapsed_sec": round(time.time() - t0, 1),
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser()
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()
    res = run(rebuild=args.rebuild, limit=args.limit)
    print(json.dumps(res, ensure_ascii=False, indent=2))
