"""绕过 Redmine API，直接 DB 拉 issue 内容跑双路 KNN，看钉钉文档能否召回。

用法：docker cp 进容器后 python /app/diag_dual_knn.py 501963 502087
"""
from __future__ import annotations

import sys

from src.db_client import RedmineDB
from src.embedder import Embedder
from src.text_cleaner import build_issue_text
from src.vector_store import get_vector_store, get_doc_store


def run_one(iid: int) -> None:
    db = RedmineDB()
    with db._conn() as (_, cur):
        cur.execute("SELECT id, subject, description FROM issues WHERE id=%s", (iid,))
        r = cur.fetchone()
    if not r:
        print(f"  #{iid} NOT FOUND in db")
        return

    print(f"\n{'='*70}\n#{iid} {r['subject']}\n{'='*70}")
    text = build_issue_text(r["subject"], r["description"] or "")
    emb = Embedder().embed([text])[0]

    issues_idx = get_vector_store()
    docs_idx = get_doc_store()  # type: ignore

    issue_top = issues_idx.knn(emb, top=8, exclude_id=iid)
    print(f"\n--- 历史工单召回 top 8 ---")
    for i, x in enumerate(issue_top, 1):
        cos = x["cosine"]
        flag = "✓" if cos >= 0.65 else "·"
        subj = (x.get("subject") or "")[:50]
        print(f"  {flag} {i}. #{x['issue_id']} cos={cos:.3f} | {subj}")

    doc_top = docs_idx.knn(emb, top=5)
    print(f"\n--- 钉钉文档召回 top 5 ---")
    for i, x in enumerate(doc_top, 1):
        cos = x["cosine"]
        flag = "✓" if cos >= 0.65 else "·"
        title = (x.get("title") or "")[:50]
        print(f"  {flag} {i}. {x.get('node_id','?')[:12]}.. cos={cos:.3f} | {title}")


if __name__ == "__main__":
    ids = [int(x) for x in sys.argv[1:]] or [501963, 502087]
    for iid in ids:
        run_one(iid)
