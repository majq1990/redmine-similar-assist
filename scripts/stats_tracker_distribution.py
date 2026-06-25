"""统计向量库 issues 涉及哪些 tracker。"""
import sys, os
sys.path.insert(0, "/app"); os.chdir("/app")
from src.db_client import RedmineDB
from src.vector_store import get_vector_store

db = RedmineDB()
vs = get_vector_store()

# 1. redmine 全公司 tracker 分布
with db._conn() as (_, cur):
    cur.execute("""SELECT t.id, t.name, COUNT(*) AS n
                     FROM issues i JOIN trackers t ON i.tracker_id=t.id
                    GROUP BY t.id, t.name ORDER BY n DESC""")
    redmine_rows = cur.fetchall()

# 2. 向量库实际入库的 issue_id 列表 → 反查 tracker
in_vs = [r[0] for r in vs.conn.execute("SELECT issue_id FROM issues_meta").fetchall()]
print(f"向量库 issues_meta 入库: {len(in_vs):,}")
print(f"向量库 faiss 向量数:     {vs._index.ntotal:,}")

# 分批查 tracker
from collections import Counter
counter = Counter()
BATCH = 5000
with db._conn() as (_, cur):
    for i in range(0, len(in_vs), BATCH):
        batch = in_vs[i:i+BATCH]
        placeholders = ",".join("%s" for _ in batch)
        cur.execute(f"""SELECT t.id, t.name, COUNT(*) AS n
                         FROM issues i JOIN trackers t ON i.tracker_id=t.id
                        WHERE i.id IN ({placeholders})
                        GROUP BY t.id, t.name""", tuple(batch))
        for r in cur.fetchall():
            counter[(r["id"], r["name"])] += r["n"]

total_vs = sum(counter.values())
total_rm = sum(r["n"] for r in redmine_rows)
rm_by_id = {(r["id"], r["name"]): r["n"] for r in redmine_rows}

print(f"\n=== Redmine 全公司 tracker 分布（共 {total_rm:,}）vs 向量库（共 {total_vs:,}）===")
print(f"{'id':>3}  {'name':<14}  {'redmine':>10}  {'vector_db':>10}  {'cov%':>6}")
print("-" * 60)
for (tid, name), rm_n in sorted(rm_by_id.items(), key=lambda x: -x[1]):
    vs_n = counter.get((tid, name), 0)
    cov = vs_n / rm_n * 100 if rm_n else 0
    print(f"{tid:>3}  {name[:14]:<14}  {rm_n:>10,}  {vs_n:>10,}  {cov:>5.1f}%")
print(f"\n合计覆盖率: {total_vs}/{total_rm} = {total_vs/total_rm*100:.2f}%")
