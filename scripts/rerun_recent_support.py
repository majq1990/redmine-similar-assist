"""批量重跑今天+昨天所有「项目支持」子树下的支持工单。

用法（容器内）：python /app/rerun_recent_support.py
"""
from __future__ import annotations

import sqlite3
import sys
import time

from src.config import get_target_project_ids, invalidate_target_project_cache
from src.db_client import RedmineDB
from src.pipeline import ingest_new_issue


def main():
    invalidate_target_project_cache()
    target_projects = get_target_project_ids()
    print(f"[info] target projects: {len(target_projects)}")

    db = RedmineDB()
    with db._conn() as (_, cur):
        cur.execute(
            """SELECT id, subject, project_id, status_id, created_on
                 FROM issues
                WHERE tracker_id=3
                  AND created_on >= DATE_SUB(CURDATE(), INTERVAL 1 DAY)
                  AND project_id IN ("""
            + ",".join(str(p) for p in target_projects)
            + ") ORDER BY created_on"
        )
        rows = list(cur.fetchall())
    print(f"[info] {len(rows)} support issues in target projects since yesterday")

    log = sqlite3.connect("/app/data/assist_log.db")
    log.execute(
        f"DELETE FROM assist_log WHERE issue_id IN ({','.join(str(r['id']) for r in rows)})"
    )
    log.commit()
    print(f"[info] cleared assist_log for {len(rows)} ids")

    ok = wrote = empty = err = 0
    for r in rows:
        iid = r["id"]
        try:
            res = ingest_new_issue(iid)
            if res.get("wrote"):
                wrote += 1
            picks = res.get("picks") or []
            doc_picks = res.get("doc_picks") or []
            if not picks and not doc_picks:
                empty += 1
            ok += 1
            print(
                f"  #{iid} picks={len(picks)} docs={len(doc_picks)} wrote={res.get('wrote')} "
                f"| {(r['subject'] or '')[:50]}"
            )
        except Exception as e:
            err += 1
            print(f"  #{iid} ERR: {type(e).__name__}: {str(e)[:120]}")
        time.sleep(0.3)

    print(f"\n[done] total={len(rows)} ok={ok} wrote={wrote} empty={empty} err={err}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    main()
