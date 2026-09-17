"""探查最近 3 个月 subject/description 含"灵珑"的 issue 分布。

目的：判断"完整灵珑系统需求"候选池规模，决定后续 LLM 二次筛选策略。
"""
import sys, os
sys.path.insert(0, "/app")
os.chdir("/app")

from src.db_client import RedmineDB
from datetime import datetime, timedelta
from collections import Counter

db = RedmineDB()
since = (datetime.now() - timedelta(days=95)).strftime("%Y-%m-%d")
print(f"since: {since}")

with db._conn() as (_, cur):
    # 1) 总量：subject/description 含"灵珑"
    kw = "%灵珑%"
    cur.execute(
        """SELECT i.id, i.subject, i.tracker_id, t.name as tracker_name,
                  i.status_id, s.name as status_name,
                  i.project_id, p.name as project_name,
                  i.author_id, i.assigned_to_id, i.created_on,
                  CHAR_LENGTH(i.description) as desc_len
             FROM issues i
             JOIN trackers t ON t.id = i.tracker_id
             JOIN issue_statuses s ON s.id = i.status_id
             LEFT JOIN projects p ON p.id = i.project_id
            WHERE i.created_on >= %s
              AND (i.subject LIKE %s OR i.description LIKE %s)
            ORDER BY i.created_on DESC""",
        (since, kw, kw),
    )
    rows = cur.fetchall()

print(f"\n=== 最近 3 月（{since} 至今）灵珑相关工单 ===")
print(f"总数: {len(rows)}")

# tracker 分布
print("\n=== tracker 分布 ===")
tracker_counter = Counter()
for r in rows:
    tracker_counter[(r["tracker_id"], r["tracker_name"])] += 1
for (tid, name), n in tracker_counter.most_common():
    print(f"  tracker={tid:>3} {name:<12}  n={n}")

# 状态分布
print("\n=== 状态分布 ===")
status_counter = Counter()
for r in rows:
    status_counter[(r["status_id"], r["status_name"])] += 1
for (sid, name), n in status_counter.most_common(10):
    print(f"  status={sid:>3} {name:<12}  n={n}")

# description 长度分布
print("\n=== description 长度分档（完整判断依据）===")
buckets = {"<200": 0, "200-500": 0, "500-1000": 0, "1000-2000": 0, "2000-5000": 0, ">=5000": 0}
for r in rows:
    l = r["desc_len"] or 0
    if l < 200: buckets["<200"] += 1
    elif l < 500: buckets["200-500"] += 1
    elif l < 1000: buckets["500-1000"] += 1
    elif l < 2000: buckets["1000-2000"] += 1
    elif l < 5000: buckets["2000-5000"] += 1
    else: buckets[">=5000"] += 1
for k, v in buckets.items():
    print(f"  desc {k}: {v}")

# 项目分布（top 10）
print("\n=== 项目分布 top 10 ===")
proj_counter = Counter()
for r in rows:
    proj_counter[r["project_name"] or "?"] += 1
for name, n in proj_counter.most_common(10):
    print(f"  {name[:50]}  n={n}")

# "疑似完整系统"粗筛：desc >= 1000 + tracker in (需求2, 支持3, 里程碑15)
print("\n=== 疑似完整系统候选（desc>=1000 且 tracker∈{需求/支持/里程碑}）===")
candidates = [
    r for r in rows
    if (r["desc_len"] or 0) >= 1000
    and r["tracker_id"] in (2, 3, 15)
]
print(f"候选数: {len(candidates)}")
for r in candidates[:20]:
    print(f"  #{r['id']} [{r['tracker_name']}][{r['status_name']}] "
          f"desc={r['desc_len']:>5} | {(r['subject'] or '')[:60]}")
