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
from .vector_store import get_doc_store


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
    em = Embedder()

    t0 = time.time()
    processed = 0
    inserted = 0
    skipped = 0
    failed = 0

    BATCH = int(c["embedding"].get("batch_size", 16))
    batch_texts: list[str] = []
    batch_metas: list[dict] = []
    batch_embed_inputs: list[str] = []

    def flush() -> None:
        nonlocal inserted, batch_texts, batch_metas, batch_embed_inputs
        if not batch_texts:
            return
        embs = em.embed(batch_embed_inputs)
        for emb, meta in zip(embs, batch_metas):
            ds.upsert(meta["node_id"], emb, meta)
            inserted += 1
        batch_texts = []
        batch_metas = []
        batch_embed_inputs = []

    for node in mcp.walk_documents(ws_id):
        nid = node.get("nodeId")
        title = node.get("name") or ""
        url = node.get("docUrl") or f"https://alidocs.dingtalk.com/i/nodes/{nid}"
        update_time = node.get("updateTime")

        processed += 1

        # 跳过已存在且未更新的（rebuild=False 时）
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

        cleaned = _clean_markdown(md)
        embed_text = _build_embed_text(title, cleaned)
        new_hash = _hash(embed_text)

        # 不变就只更新 meta
        if not rebuild:
            existing = ds.get_meta(nid)
            if existing and existing.get("embed_text_hash") == new_hash:
                skipped += 1
                continue

        batch_texts.append(cleaned)
        batch_embed_inputs.append(embed_text)
        batch_metas.append(
            {
                "node_id": nid,
                "workspace_id": ws_id,
                "title": title,
                "url": url,
                "summary": cleaned[:300],
                "update_time": update_time,
                "embed_text_hash": new_hash,
            }
        )
        if len(batch_texts) >= BATCH:
            flush()
            elapsed = time.time() - t0
            print(
                f"[backfill] processed={processed} inserted={inserted} "
                f"skipped={skipped} failed={failed} elapsed={elapsed:.0f}s",
                flush=True,
            )
        if limit and processed >= limit:
            break

        # 钉钉 MCP 限速保护
        time.sleep(0.1)

    flush()

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
        "inserted": inserted,
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
