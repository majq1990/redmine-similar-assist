"""上线前 vs 上线后：支持工单"未经研发中心就关闭"比例对比。

核心指标：支持部能否独立解决案件不甩给研发。
- 已关闭工单 = closed_on IS NOT NULL
- 研发介入 = 工单 journal 里 status 曾流转到 {2 开发中, 17 研发分派, 18 测试中,
            30 代码审核, 33 研发完成审核, 34 研发文档终审}
- 未经研发关闭率 = (已关闭且未经研发) / 已关闭
"""
import os, sys
sys.path.insert(0, "/app"); os.chdir("/app")
from src.db_client import RedmineDB
from src.config import get_target_project_ids, invalidate_target_project_cache

LAUNCH = "2026-05-28"
RND_STATUSES = (2, 17, 18, 30, 33, 34)  # 研发流程相关状态

db = RedmineDB()
invalidate_target_project_cache()
proj_ids = list(get_target_project_ids())


def query(cur, start_date, end_date, project_ids):
    proj_clause = ""
    if project_ids:
        proj_clause = " AND i.project_id IN (" + ",".join(str(p) for p in project_ids) + ")"
    rnd_set = ",".join(f"'{s}'" for s in RND_STATUSES)

    # 总创建数
    cur.execute(
        f"""SELECT COUNT(*) AS n FROM issues i
            WHERE i.tracker_id=3 AND i.created_on >= %s AND i.created_on < %s
            {proj_clause}""",
        (start_date, end_date),
    )
    total = cur.fetchone()["n"] or 0

    # 已关闭数
    cur.execute(
        f"""SELECT COUNT(*) AS n FROM issues i
            WHERE i.tracker_id=3 AND i.created_on >= %s AND i.created_on < %s
              AND i.closed_on IS NOT NULL
            {proj_clause}""",
        (start_date, end_date),
    )
    closed = cur.fetchone()["n"] or 0

    # 已关闭且**未经研发**的工单数
    cur.execute(
        f"""SELECT COUNT(*) AS n FROM issues i
            WHERE i.tracker_id=3 AND i.created_on >= %s AND i.created_on < %s
              AND i.closed_on IS NOT NULL
              {proj_clause}
              AND NOT EXISTS (
                SELECT 1 FROM journals j
                JOIN journal_details jd ON jd.journal_id = j.id
                WHERE j.journalized_type='Issue'
                  AND j.journalized_id = i.id
                  AND jd.property='attr'
                  AND jd.prop_key='status_id'
                  AND jd.value IN ({rnd_set})
              )""",
        (start_date, end_date),
    )
    closed_no_rnd = cur.fetchone()["n"] or 0

    # 已关闭且**经过研发**
    closed_via_rnd = closed - closed_no_rnd

    return {
        "total": total,
        "closed": closed,
        "closed_no_rnd": closed_no_rnd,
        "closed_via_rnd": closed_via_rnd,
    }


def render(label, r):
    closed = max(r["closed"], 1)
    print(f"\n━━━ {label} ━━━")
    print(f"  创建总数:                {r['total']:>4}")
    print(f"  已关闭:                  {r['closed']:>4}  ({r['closed']/max(r['total'],1)*100:.1f}% of total)")
    print(f"  其中 未经研发 关闭:        {r['closed_no_rnd']:>4}  "
          f"({r['closed_no_rnd']/closed*100:.1f}% of closed)  ⭐")
    print(f"  其中 经研发后 关闭:        {r['closed_via_rnd']:>4}  "
          f"({r['closed_via_rnd']/closed*100:.1f}% of closed)")
    return r['closed_no_rnd']/closed*100


with db._conn() as (_, cur):
    # 多个时间窗对比
    windows = [
        ("上线前 30 天 [2026-04-28 → 2026-05-27]", "2026-04-28", LAUNCH),
        ("上线后 14 天 [2026-05-28 → 2026-06-10]", LAUNCH, "2026-06-11"),
        ("上线后 30 天 [2026-05-28 → 2026-06-26]", LAUNCH, "2026-06-27"),
    ]
    print(f"=== '未经研发关闭率' 对比（项目支持子树 {len(proj_ids)} 项目, tracker=支持）===")
    print(f"=== 研发态 = status ∈ {{2 开发中, 17 研发分派, 18 测试中, 30 代码审核, 33 研发完成审核, 34 研发文档终审}} ===")
    rates = []
    for label, sd, ed in windows:
        r = query(cur, sd, ed, proj_ids)
        rate = render(label, r)
        rates.append((label, rate))

    print("\n" + "=" * 70)
    print("【核心结论 △】")
    print("=" * 70)
    base = rates[0][1]
    for label, rate in rates[1:]:
        delta = rate - base
        arrow = "↑" if delta > 0 else "↓"
        print(f"  {label.split('[')[0]}: {rate:.1f}%   vs 上线前 {base:.1f}%   △ {delta:+.1f}pp {arrow}")
