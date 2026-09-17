"""季节性对照：2025 同期 vs 2026 上线后，看"未经研发关闭率"是否本身有 4→5 月走势。

如果 2025-04→05 和 2025-05→06 也呈现 +2~3pp 提升 → 2026 +3.2pp 是季节性
如果 2025 没有类似走势 → 2026 +3.2pp 大概率是 AI 贡献
"""
import os, sys
sys.path.insert(0, "/app"); os.chdir("/app")
from src.db_client import RedmineDB
from src.config import get_target_project_ids, invalidate_target_project_cache

RND_STATUSES = (2, 17, 18, 30, 33, 34)

db = RedmineDB()
invalidate_target_project_cache()
proj_ids = list(get_target_project_ids())


def query(cur, start, end, project_ids):
    proj_clause = ""
    if project_ids:
        proj_clause = " AND i.project_id IN (" + ",".join(str(p) for p in project_ids) + ")"
    rnd_set = ",".join(f"'{s}'" for s in RND_STATUSES)
    cur.execute(
        f"""SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN i.closed_on IS NOT NULL THEN 1 ELSE 0 END) AS closed,
              SUM(CASE WHEN i.closed_on IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM journals j
                    JOIN journal_details jd ON jd.journal_id=j.id
                    WHERE j.journalized_type='Issue' AND j.journalized_id=i.id
                      AND jd.property='attr' AND jd.prop_key='status_id'
                      AND jd.value IN ({rnd_set})
                  ) THEN 1 ELSE 0 END) AS closed_no_rnd
            FROM issues i
            WHERE i.tracker_id=3 AND i.created_on>=%s AND i.created_on<%s
              {proj_clause}""",
        (start, end),
    )
    r = cur.fetchone()
    t = int(r["total"] or 0)
    c = int(r["closed"] or 0)
    nrn = int(r["closed_no_rnd"] or 0)
    return {"total": t, "closed": c, "no_rnd": nrn,
            "no_rnd_rate": nrn/max(c,1)*100}


windows = [
    # 2025 同期 4-5 月
    ("2025-04 [04-28→05-27]", "2025-04-28", "2025-05-28"),
    ("2025-05 [05-28→06-26]", "2025-05-28", "2025-06-27"),
    # 2026 实际
    ("2026-04 [04-28→05-27]", "2026-04-28", "2026-05-28"),
    ("2026-05 [05-28→06-26]", "2026-05-28", "2026-06-27"),
]

print("=== 季节性对照：2025 同期 vs 2026 上线前后 ===")
print("=== 指标：'未经研发关闭率' = 已关闭中 status 从未流转到研发态的占比 ===\n")

print(f"{'窗口':<28} {'创建':>5} {'关闭':>5} {'未经研发':>8} {'未经研发率':>10}")
print("-" * 65)

results = []
with db._conn() as (_, cur):
    for label, sd, ed in windows:
        r = query(cur, sd, ed, proj_ids)
        results.append((label, r))
        print(f"{label:<28} {r['total']:>5} {r['closed']:>5} {r['no_rnd']:>8} {r['no_rnd_rate']:>9.1f}%")

print("\n=== 同年内月间变化（4→5 月） ===")
y25 = results[1][1]["no_rnd_rate"] - results[0][1]["no_rnd_rate"]
y26 = results[3][1]["no_rnd_rate"] - results[2][1]["no_rnd_rate"]
print(f"  2025 年 4→5 月变化:  {results[0][1]['no_rnd_rate']:.1f}% → {results[1][1]['no_rnd_rate']:.1f}%   △ {y25:+.1f}pp")
print(f"  2026 年 4→5 月变化:  {results[2][1]['no_rnd_rate']:.1f}% → {results[3][1]['no_rnd_rate']:.1f}%   △ {y26:+.1f}pp")
print(f"\n  净 AI 贡献 (排季节后) ≈ {y26-y25:+.1f}pp")

print("\n=== 同期同月对比（5 月 vs 5 月）===")
m_may = results[3][1]["no_rnd_rate"] - results[1][1]["no_rnd_rate"]
m_apr = results[2][1]["no_rnd_rate"] - results[0][1]["no_rnd_rate"]
print(f"  2025-04 vs 2026-04:  {results[0][1]['no_rnd_rate']:.1f}% → {results[2][1]['no_rnd_rate']:.1f}%   △ {m_apr:+.1f}pp")
print(f"  2025-05 vs 2026-05:  {results[1][1]['no_rnd_rate']:.1f}% → {results[3][1]['no_rnd_rate']:.1f}%   △ {m_may:+.1f}pp")
