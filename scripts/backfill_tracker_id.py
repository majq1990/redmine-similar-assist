"""一次性：给 issues_meta 加 tracker_id 列并从 redmine MySQL 回填。

只读 redmine，UPDATE 本地 sqlite。幂等：列已存在或值已填都安全。
"""
import sys, os, time
sys.path.insert(0, "/app"); os.chdir("/app")
from src.db_client import RedmineDB
from src.vector_store import get_vector_store

vs = get_vector_store()
conn = vs.conn

# 1. 加列（幂等）
cols = {r[1] for r in conn.execute("PRAGMA table_info(issues_meta)").fetchall()}
if "tracker_id" not in cols:
    conn.execute("ALTER TABLE issues_meta ADD COLUMN tracker_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_meta_tracker ON issues_meta(tracker_id)")
    conn.commit()
    print("[migrate] added tracker_id column + index")
else:
    print("[migrate] tracker_id column already exists")

# 2. 拉 redmine 全部 (id, tracker_id)
db = RedmineDB()
t0 = time.time()
with db._conn() as (_, cur):
    cur.execute("SELECT id, tracker_id FROM issues")
    rm_rows = cur.fetchall()
print(f"[migrate] fetched {len(rm_rows):,} (id,tracker_id) from redmine in {time.time()-t0:.1f}s")

# 3. 批量 UPDATE 本地 sqlite，分批 5000
data = [(r["tracker_id"], r["id"]) for r in rm_rows]
BATCH = 5000
updated = 0
t1 = time.time()
for i in range(0, len(data), BATCH):
    batch = data[i:i+BATCH]
    conn.executemany(
        "UPDATE issues_meta SET tracker_id=? WHERE issue_id=?", batch
    )
    conn.commit()
    updated += len(batch)
    if i % 50000 == 0:
        print(f"  ...updated {updated:,} / {len(data):,}", flush=True)

print(f"[migrate] executemany {len(data):,} rows in {time.time()-t1:.1f}s")

# 4. 核对
filled = conn.execute(
    "SELECT COUNT(*) FROM issues_meta WHERE tracker_id IS NOT NULL"
).fetchone()[0]
total = conn.execute("SELECT COUNT(*) FROM issues_meta").fetchone()[0]
print(f"[migrate] issues_meta tracker_id filled: {filled:,}/{total:,}")

# 5. 验证 tracker 分布
print("\n=== issues_meta 按 tracker_id 分布 ===")
for r in conn.execute(
    "SELECT tracker_id, COUNT(*) AS n FROM issues_meta "
    "GROUP BY tracker_id ORDER BY n DESC LIMIT 10"
).fetchall():
    print(f"  tracker_id={r[0]:<3} n={r[1]:,}")
