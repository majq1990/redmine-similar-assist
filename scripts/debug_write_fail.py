"""重新构造 #502598 的 note 并 PUT，捕获 Redmine 错误响应。"""
import json, sqlite3
import requests, urllib3
urllib3.disable_warnings()

from src.config import cfg
from src.pipeline import _build_note

c = sqlite3.connect("/app/data/assist_log.db")
r = c.execute("SELECT candidates_json FROM assist_log WHERE issue_id=502598").fetchone()
d = json.loads(r[0])
picks = d.get("picks") or []
doc_picks = d.get("doc_picks") or []
note = _build_note(picks, doc_picks)
print("=== note length:", len(note), "bytes")
print("=== first 300 chars ===")
print(note[:300])
print("=== last 200 chars ===")
print(note[-200:])

# 直接 PUT
api_key = cfg()["redmine"]["api_key"]
base = cfg()["redmine"]["base_url"].rstrip("/")
url = f"{base}/issues/502598.json"
resp = requests.put(
    url,
    headers={"X-Redmine-API-Key": api_key, "Content-Type": "application/json"},
    json={"issue": {"notes": note}},
    verify=False,
    timeout=20,
)
print(f"\n=== HTTP {resp.status_code} ===")
print(resp.text[:1500])
