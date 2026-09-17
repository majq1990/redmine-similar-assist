"""上线前 vs 上线后 工程处理率对比 v2 —— 对齐"创建后处理时间"消除时间窗口偏差。

思路：只看「创建后已过 X 天」的工单，这样上线前后都有 X 天充分时间被关闭。
- X=14 天：上线前 2026-04-28 ~ 2026-05-13（创建至少 14 天），
           上线后 2026-05-28 ~ 2026-06-12（同样 14 天处理时间）
- 也加 7 天和 30 天两个窗口对照

只看 tracker=3 + 「项目支持」子树（与 AI 触发范围一致）。
"""
import os, sys
sys.path.insert(0, "/app"); os.chdir("/app")
from datetime import datetime, timedelta
from src.db_client import RedmineDB
from src.config import get_target_project_ids, invalidate_target_project_cache


def query_window(cur, start_date, end_date, project_ids, max_processing_days=None):
    proj_clause = ""
    if project_ids:
        proj_clause = " AND project_id IN (" + ",".join(str(p) for p in project_ids) + ")"
    sql = f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN closed_on IS NOT NULL THEN 1 ELSE 0 END) AS closed,
          SUM(CASE WHEN closed_on IS NOT NULL AND TIMESTAMPDIFF(DAY, created_on, closed_on) <= 7 THEN 1 ELSE 0 END) AS closed_7d,
          SUM(CASE WHEN closed_on IS NOT NULL AND TIMESTAMPDIFF(DAY, created_on, closed_on) <= 14 THEN 1 ELSE 0 END) AS closed_14d
        FROM issues
        WHERE tracker_id = 3
          AND created_on >= %s
          AND created_on < %s
          {proj_clause}
    """
    cur.execute(sql, (start_date, end_date))
    r = cur.fetchone()
    return {
        "total": int(r["total"] or 0),
        "closed": int(r["closed"] or 0),
        "closed_7d": int(r["closed_7d"] or 0),
        "closed_14d": int(r["closed_14d"] or 0),
    }


def pct(n, d): return f"{n/max(d,1)*100:.1f}%"
def delta(after, before, d):
    a = after / max(d, 1) * 100
    b = before / max(d, 1) * 100
    return f"{a-b:+.1f}pp"


# 上线日
LAUNCH = "2026-05-28"

# 三对公平对比窗口：每对窗口里的工单都有相同的"成熟时间"
# 7 天成熟: 创建后 ≥7 天 → 截止到今天-7
# 14 天成熟: 创建后 ≥14 天 → 截止到今天-14
TODAY = datetime(2026, 6, 26)
windows = [
    (
        "处理 7 天后  (公平窗：各 21 天创建期)",
        # 上线前: 2026-04-28 ~ 2026-05-19（21 天，最晚的 5/19 创建到 6/26 已 38 天）
        ("2026-04-28", "2026-05-19"),
        # 上线后: 2026-05-28 ~ 2026-06-19（21 天，最晚的 6/19 创建到 6/26 已 7 天）
        ("2026-05-28", "2026-06-19"),
    ),
    (
        "处理 14 天后 (公平窗：各 16 天创建期)",
        ("2026-04-28", "2026-05-13"),
        ("2026-05-28", "2026-06-12"),
    ),
]

db = RedmineDB()
invalidate_target_project_cache()
proj_ids = list(get_target_project_ids())

print(f"=== 上线前 vs 上线后 工程处理率（项目支持子树 {len(proj_ids)} 项目，tracker=支持）===")
print(f"=== 上线日 {LAUNCH} | 数据快照 {TODAY.date()} ===\n")

with db._conn() as (_, cur):
    for label, (b_start, b_end), (a_start, a_end) in windows:
        bef = query_window(cur, b_start, b_end, proj_ids)
        aft = query_window(cur, a_start, a_end, proj_ids)
        print(f"━━━ {label} ━━━")
        print(f"  上线前 {b_start} → {b_end}: 总 {bef['total']:>4}, "
              f"已关闭 {bef['closed']:>4} ({pct(bef['closed'], bef['total'])})")
        print(f"  上线后 {a_start} → {a_end}: 总 {aft['total']:>4}, "
              f"已关闭 {aft['closed']:>4} ({pct(aft['closed'], aft['total'])})")
        b_pct = bef['closed']/max(bef['total'],1)*100
        a_pct = aft['closed']/max(aft['total'],1)*100
        print(f"  △ 关闭率: {b_pct:.1f}% → {a_pct:.1f}% ({a_pct-b_pct:+.1f}pp)")
        print()

    # 单看"创建后 7 天内已关闭"的硬指标（不受窗口长度影响，纯看处理速度）
    print("━━━ 严格指标：'创建后 7 天内已关闭' 比例 ━━━")
    bef_full = query_window(cur, "2026-04-28", LAUNCH, proj_ids)
    aft_full = query_window(cur, LAUNCH, "2026-06-19", proj_ids)  # 截止到 6/19 让最晚工单也有 7 天
    print(f"  上线前 30 天: {bef_full['closed_7d']}/{bef_full['total']} = "
          f"{pct(bef_full['closed_7d'], bef_full['total'])}")
    print(f"  上线后 23 天: {aft_full['closed_7d']}/{aft_full['total']} = "
          f"{pct(aft_full['closed_7d'], aft_full['total'])}")
    b_rate = bef_full['closed_7d']/max(bef_full['total'],1)*100
    a_rate = aft_full['closed_7d']/max(aft_full['total'],1)*100
    print(f"  △ {a_rate - b_rate:+.1f}pp")
    print()
    print("━━━ 严格指标：'创建后 14 天内已关闭' 比例 ━━━")
    bef_14 = query_window(cur, "2026-04-28", LAUNCH, proj_ids)
    aft_14 = query_window(cur, LAUNCH, "2026-06-12", proj_ids)
    print(f"  上线前 30 天: {bef_14['closed_14d']}/{bef_14['total']} = "
          f"{pct(bef_14['closed_14d'], bef_14['total'])}")
    print(f"  上线后 15 天: {aft_14['closed_14d']}/{aft_14['total']} = "
          f"{pct(aft_14['closed_14d'], aft_14['total'])}")
    b_rate = bef_14['closed_14d']/max(bef_14['total'],1)*100
    a_rate = aft_14['closed_14d']/max(aft_14['total'],1)*100
    print(f"  △ {a_rate - b_rate:+.1f}pp")
