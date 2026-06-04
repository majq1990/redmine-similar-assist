"""检查 webhook 进程内 docs 索引状态 + 跑一次 #502749 看为啥 wrote=0"""
from src.vector_store import get_vector_store, get_doc_store
from src.text_cleaner import build_issue_text
from src.embedder import Embedder
from src.db_client import RedmineDB
from src.redmine_client import RedmineClient

print("=== docs store size ===")
ds = get_doc_store()
print(f"  ntotal in faiss: {ds._index.ntotal}")
print(f"  rows in docs_meta: {ds.conn.execute('SELECT COUNT(*) FROM docs_meta').fetchone()[0]}")

print("\n=== 502749 KNN docs ===")
rc = RedmineClient()
issue = rc.get_issue(502749, include="journals")
print(f"  subject: {issue.get('subject')}")
print(f"  journals: {len(issue.get('journals') or [])}")
for j in issue.get('journals') or []:
    n = (j.get('notes') or '')[:60]
    user = (j.get('user') or {}).get('name', '?')
    if n:
        print(f"    [{j.get('created_on')}] {user}: {n}")

text = build_issue_text(issue['subject'], issue.get('description') or '')
emb = Embedder().embed([text])[0]
print(f"\n  embed text len: {len(text)}")
docs = ds.knn(emb, top=5)
print(f"  top 5 doc KNN:")
for d in docs:
    print(f"    cos={d['cosine']:.3f} {(d.get('title') or '')[:50]}")
