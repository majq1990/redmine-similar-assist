"""R-1 探查：assist_log + redmine 案件状态 + 灵珑表单存量。

目的：先摸清可回顾样本池，再决定 review 主流程。
"""
import sys, os
sys.path.insert(0, "/app")
os.chdir("/app")

import sqlite3
from src.config import cfg, project_root
from src.db_client import RedmineDB

# --- 1) 本地 assist_log ---
log_path = project_root() / cfg()["storage"]["log_db"]
lc = sqlite3.connect(str(log_path))
print(f"=== assist_log at {log_path} ===")
for r in lc.execute("SELECT COUNT(*) FROM assist_log").fetchall():
    total_written = r[0]
print(f"assist_log 总数: {total_written}")

# 只看真正 note_written=1 的
note_written = lc.execute(
    "SELECT COUNT(*) FROM assist_log WHERE note_written=1"
).fetchone()[0]
print(f"note_written=1 (AI 真的写了楼): {note_written}")

# 拿一部分 issue_id
issue_ids = [
    r[0]
    for r in lc.execute(
        "SELECT issue_id FROM assist_log WHERE note_written=1 "
        "ORDER BY processed_at DESC"
    ).fetchall()
]
print(f"取到 issue_ids: {len(issue_ids)} 条")

# --- 2) Redmine 里这些案件的状态分布 ---
db = RedmineDB()
if not issue_ids:
    print("no issue_ids"); sys.exit(0)

# 分批查
from collections import Counter
status_counter = Counter()
closed_ids = []
BATCH = 2000
with db._conn() as (_, cur):
    for i in range(0, len(issue_ids), BATCH):
        batch = issue_ids[i:i+BATCH]
        ph = ",".join("%s" for _ in batch)
        cur.execute(
            f"""SELECT i.id, i.status_id, s.name, s.is_closed, i.closed_on
                  FROM issues i JOIN issue_statuses s ON s.id=i.status_id
                 WHERE i.id IN ({ph})""",
            tuple(batch),
        )
        for r in cur.fetchall():
            status_counter[(r["status_id"], r["name"], bool(r["is_closed"]))] += 1
            if r["is_closed"]:
                closed_ids.append(r["id"])

print(f"\n=== AI 写楼案件的 redmine 状态分布 ===")
for (sid, name, closed), n in sorted(status_counter.items(), key=lambda x: -x[1]):
    print(f"  status_id={sid:>3} name={name:<12} closed={closed}  n={n}")
print(f"\n合计已关闭 (is_closed=1): {len(closed_ids)} 条")

# --- 3) 这些关闭案件里有 form_develop_finish 或 form_tester_verify 内容的 ---
if not closed_ids:
    print("no closed"); sys.exit(0)

with db._conn() as (_, cur):
    # 检查两张表是否存在
    cur.execute("SHOW TABLES LIKE 'form_develop_finish'")
    has_dev_finish = bool(cur.fetchone())
    cur.execute("SHOW TABLES LIKE 'form_tester_verify'")
    has_test_verify = bool(cur.fetchone())
print(f"\nform_develop_finish 表存在? {has_dev_finish}")
print(f"form_tester_verify  表存在? {has_test_verify}")

has_dev_ids = set()
has_test_ids = set()
with db._conn() as (_, cur):
    for i in range(0, len(closed_ids), BATCH):
        batch = closed_ids[i:i+BATCH]
        ph = ",".join("%s" for _ in batch)
        if has_dev_finish:
            cur.execute(
                f"""SELECT DISTINCT issue_id FROM form_develop_finish
                     WHERE issue_id IN ({ph})
                       AND (function_description IS NOT NULL AND function_description!='')""",
                tuple(batch),
            )
            has_dev_ids.update(r["issue_id"] for r in cur.fetchall())
        if has_test_verify:
            cur.execute(
                f"""SELECT DISTINCT issue_id FROM form_tester_verify
                     WHERE issue_id IN ({ph})
                       AND (test_result IS NOT NULL AND test_result!='')""",
                tuple(batch),
            )
            has_test_ids.update(r["issue_id"] for r in cur.fetchall())

print(f"\n=== 关闭案件里的表单填写情况 ===")
print(f"有 form_develop_finish.function_description: {len(has_dev_ids)} 条")
print(f"有 form_tester_verify.test_result:          {len(has_test_ids)} 条")
print(f"至少一个非空:                                {len(has_dev_ids | has_test_ids)} 条")
print(f"两个都有:                                    {len(has_dev_ids & has_test_ids)} 条")

# --- 4) 推荐结论 ---
gold_ids = has_dev_ids | has_test_ids
print(f"\n=== 决策建议 ===")
print(f"回顾样本池 (关闭 + 有实际解决方案): {len(gold_ids)} 条")
if len(gold_ids) >= 20:
    print("  → 足够跑 20 条样本 + 后续全量")
else:
    print(f"  → 样本池太小，考虑放宽条件（比如允许仅 journal 有解决内容）")

# 打印几个样本 issue_id 便于人工核对
sample = sorted(gold_ids)[:10] if gold_ids else []
print(f"\n样本 issue_id (前 10): {sample}")
