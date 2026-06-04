"""统计今天写入 AI 一楼且包含钉钉文档段的工单数。"""
import json, sqlite3

c = sqlite3.connect("/app/data/assist_log.db")
rows = c.execute(
    "SELECT issue_id, processed_at, note_written, candidates_json "
    "FROM assist_log WHERE processed_at >= '2026-06-03T00:00:00' "
    "ORDER BY processed_at"
).fetchall()

total = len(rows)
wrote = 0
with_doc = 0
with_picks = 0
empty = 0
docs_examples = []

for iid, ts, w, cj in rows:
    if w:
        wrote += 1
    d = json.loads(cj or "{}")
    picks = d.get("picks") or []
    docs = d.get("doc_picks") or []
    if picks:
        with_picks += 1
    if docs and w:
        with_doc += 1
        docs_examples.append((iid, ts, len(picks), len(docs)))
    if not picks and not docs:
        empty += 1

print(f"=== 6/3 当日 AI 处理统计 ===")
print(f"  总处理工单:           {total}")
print(f"  成功写入 AI 一楼:     {wrote}")
print(f"  含相似工单推荐:       {with_picks}")
print(f"  含钉钉文档推荐(写入): {with_doc}  ← 这才是真带钉钉文档的")
print(f"  暂无推荐:             {empty}")
print()
if docs_examples:
    print("含钉钉文档的工单清单：")
    for iid, ts, p, dp in docs_examples:
        print(f"  #{iid}  picks={p} docs={dp}  ({ts})")
else:
    print("⚠️  今天没有任何工单的 AI 一楼包含钉钉文档段")
