"""按 id 列表批量 ingest（同进程，faiss 只 reload 一次）。

用法：python /app/ingest_ids.py 501963 501952 501983 502001 502002 502087
"""
import sys, sqlite3, time
from src.pipeline import ingest_new_issue

ids = [int(x) for x in sys.argv[1:]]
if not ids:
    print("Usage: ingest_ids.py <id> [<id>...]")
    sys.exit(2)

log = sqlite3.connect("/app/data/assist_log.db")
log.execute(f"DELETE FROM assist_log WHERE issue_id IN ({','.join(str(i) for i in ids)})")
log.commit()

ok = wrote = err = 0
for iid in ids:
    try:
        r = ingest_new_issue(iid)
        picks = r.get("picks") or []
        docs = r.get("doc_picks") or []
        w = r.get("wrote")
        if w: wrote += 1
        ok += 1
        print(f"  #{iid} picks={len(picks)} docs={len(docs)} wrote={w}")
    except Exception as e:
        err += 1
        print(f"  #{iid} ERR: {type(e).__name__}: {str(e)[:100]}")
    time.sleep(0.3)

print(f"\n[done] total={len(ids)} ok={ok} wrote={wrote} err={err}")
