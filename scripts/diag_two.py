import json, sqlite3
c = sqlite3.connect("/app/data/assist_log.db")
for iid in (502749, 502087):
    r = c.execute("SELECT processed_at, note_written, candidates_json FROM assist_log WHERE issue_id=?", (iid,)).fetchone()
    if not r:
        print(f"#{iid} NOT IN LOG")
        continue
    d = json.loads(r[2] or "{}")
    picks = d.get("picks", [])
    docs = d.get("doc_picks", [])
    print(f"#{iid} processed={r[0]} wrote={r[1]} picks={len(picks)} doc_picks={len(docs)}")
    for p in picks[:3]:
        print(f"  P issue={p.get('issue_id')} score={p.get('score')}")
    for p in docs[:3]:
        nid = (p.get('node_id') or '?')[:12]
        print(f"  D {nid} score={p.get('score')} {(p.get('title') or '')[:40]}")
