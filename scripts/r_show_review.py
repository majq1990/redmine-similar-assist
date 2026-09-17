"""看某条 review_log 详情"""
import sqlite3, json, sys
c = sqlite3.connect("/app/data/assist_log.db")
iid = int(sys.argv[1]) if len(sys.argv) > 1 else 501928
row = c.execute(
    "SELECT issue_id, overall_quality, summary, picks_verdicts_json, "
    "doc_verdicts_json, elapsed_ms FROM review_log WHERE issue_id=?",
    (iid,),
).fetchone()
if not row:
    print("no row"); sys.exit(1)
iid, ov, sm, pv, dv, elm = row
print(f"issue: #{iid}")
print(f"overall: {ov}")
print(f"summary: {sm}")
print(f"elapsed: {elm}ms")
print("--- picks verdicts ---")
for v in json.loads(pv or "[]"):
    iid2 = v.get("issue_id")
    verdict = v.get("verdict")
    reason = v.get("reason")
    print(f"  #{iid2}: {verdict} - {reason}")
print("--- doc verdicts ---")
for v in json.loads(dv or "[]"):
    nid = v.get("node_id")
    verdict = v.get("verdict")
    reason = v.get("reason")
    print(f"  node={nid}: {verdict} - {reason}")
