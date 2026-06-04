"""找出失败工单 picks 里的 4-byte UTF-8 字符。"""
import json, sqlite3

c = sqlite3.connect("/app/data/assist_log.db")
ids = (502598, 502615, 502626, 502630, 502634, 502648, 502655, 502657, 502658, 502668, 502670, 502716)
for iid in ids:
    r = c.execute("SELECT candidates_json FROM assist_log WHERE issue_id=?", (iid,)).fetchone()
    if not r:
        continue
    d = json.loads(r[0])
    for k, v in d.items():
        if not isinstance(v, list):
            continue
        for p in v:
            iid2 = p.get("issue_id") or p.get("node_id") or "?"
            txt = (p.get("solution") or "") + " || " + (p.get("subject") or "") + " || " + (p.get("title") or "")
            bad = [ch for ch in txt if len(ch.encode("utf-8")) >= 4]
            if bad:
                preview = "".join(bad[:8])
                snippet = txt[:100].replace("\n", " ")
                print(f"#{iid} {k} src={iid2} BAD={preview!r} in: {snippet}")
