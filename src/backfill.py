"""全量回填历史 issue 到向量库。

对 config.target_projects 里的每个 project：
  - 拉所有 status 的 issue（含 closed）
  - 对每条 issue，include=journals 取完整内容
  - clean → embed → upsert
  - 已存在则跳过（除非 --rebuild）
"""
from __future__ import annotations

import argparse
import time

from .config import cfg
from .embedder import Embedder
from .redmine_client import RedmineClient
from .text_cleaner import build_issue_text, find_resolution_notes
from .vector_store import VectorStore


def run(rebuild: bool = False, limit: int | None = None) -> None:
    c = cfg()
    rc = RedmineClient()
    vs = VectorStore()
    em = Embedder()

    for proj in c["target_projects"]:
        print(f"=== project {proj} ===", flush=True)
        cnt = 0
        batch_texts: list[str] = []
        batch_meta: list[dict] = []
        BATCH = c["embedding"].get("batch_size", 16)

        def flush() -> None:
            nonlocal batch_texts, batch_meta
            if not batch_texts:
                return
            embs = em.embed(batch_texts)
            for emb, meta in zip(embs, batch_meta):
                vs.upsert(meta["issue_id"], emb, meta)
            print(f"  flushed {len(batch_texts)} (cumulative {cnt})", flush=True)
            batch_texts = []
            batch_meta = []

        for it in rc.iter_project_issues(proj, status_id="*", page_size=100):
            iid = it["id"]
            if not rebuild and vs.has(iid):
                continue
            try:
                full = rc.get_issue(iid, include="journals")
            except Exception as e:
                print(f"  skip {iid}: {e}", flush=True)
                continue
            text = build_issue_text(full.get("subject") or "", full.get("description") or "")
            resolution = find_resolution_notes(full.get("journals") or [])
            embed_text = text + ("\n[解决方案] " + resolution if resolution else "")
            batch_texts.append(embed_text)
            batch_meta.append(
                {
                    "issue_id": iid,
                    "subject": full.get("subject"),
                    "status": ((full.get("status") or {}).get("name")),
                    "closed_on": full.get("closed_on"),
                    "resolution": resolution,
                    "updated_on": full.get("updated_on"),
                }
            )
            cnt += 1
            if len(batch_texts) >= BATCH:
                flush()
            if limit and cnt >= limit:
                break
            # 节流，别把 Redmine 打爆
            time.sleep(0.05)
        flush()
        print(f"project {proj} done: {cnt} issues", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--rebuild", action="store_true", help="即使已存在也重新 embed")
    p.add_argument("--limit", type=int, default=None, help="每个项目最多处理 N 条（调试用）")
    args = p.parse_args()
    run(rebuild=args.rebuild, limit=args.limit)
