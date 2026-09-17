"""上线前 vs 上线后：支持类工单工程处理率对比。

上线日 = 2026-05-28（assist_log 最早一条）
- 上线前窗口：2026-04-28 → 2026-05-27（30 天）
- 上线后窗口：2026-05-28 → 2026-06-26（30 天）

只看 tracker=3（支持）、限"项目支持"子树（target_project_root_id=3 的子孙）。

工程处理率定义 = 关闭率（closed_on IS NOT NULL / 总数）。
另附"被处理但未关"的中间状态分析。
"""
import os
import sys
sys.path.insert(0, "/app"); os.chdir("/app")
from src.db_client import RedmineDB
from src.config import get_target_project_ids, invalidate_target_project_cache

LAUNCH = "2026-05-28"
BEFORE_START = "2026-04-28"
AFTER_END = "2026-06-27"  # 含 6/26


def window(cur, start_date, end_date, label, project_ids):
    proj_clause = ""
    if project_ids:
        proj_clause = " AND project_id IN (" + ",".join(str(p) for p in project_ids) + ")"
    sql = f"""
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN closed_on IS NOT NULL THEN 1 ELSE 0 END) AS closed,
          SUM(CASE WHEN status_id != 1 THEN 1 ELSE 0 END) AS not_new,
          SUM(CASE WHEN assigned_to_id IS NOT NULL THEN 1 ELSE 0 END) AS assigned
        FROM issues
        WHERE tracker_id = 3
          AND created_on >= %s
          AND created_on < %s
          {proj_clause}
    """
    cur.execute(sql, (start_date, end_date))
    r = cur.fetchone()
    return {
        "label": label,
        "window": f"{start_date} ~ {end_date}",
        "total": int(r["total"] or 0),
        "closed": int(r["closed"] or 0),
        "not_new": int(r["not_new"] or 0),
        "assigned": int(r["assigned"] or 0),
    }


def fmt_row(r):
    n = max(r["total"], 1)
    return (
        f"  {r['label']:<10}  窗口 {r['window']}\n"
        f"    总工单数:           {r['total']:>5}\n"
        f"    已关闭 (closed):    {r['closed']:>5}  ({r['closed']/n*100:>5.1f}%)\n"
        f"    已离开新建态:        {r['not_new']:>5}  ({r['not_new']/n*100:>5.1f}%)\n"
        f"    已分配给人:         {r['assigned']:>5}  ({r['assigned']/n*100:>5.1f}%)"
    )


db = RedmineDB()
invalidate_target_project_cache()
proj_ids = list(get_target_project_ids())
print(f"项目支持子树共 {len(proj_ids)} 个项目（含子孙）")
print(f"上线日 {LAUNCH}\n")

with db._conn() as (_, cur):
    before = window(cur, BEFORE_START, LAUNCH, "上线前 30 天", proj_ids)
    after = window(cur, LAUNCH, AFTER_END, "上线后 30 天", proj_ids)

    # 也跑一个全公司支持工单（不限项目支持子树）作为佐证
    before_all = window(cur, BEFORE_START, LAUNCH, "[全公司] 上线前 30 天", None)
    after_all = window(cur, LAUNCH, AFTER_END, "[全公司] 上线后 30 天", None)

print("=" * 70)
print("【项目支持子树 / tracker=支持】上线前 vs 上线后对比")
print("=" * 70)
print(fmt_row(before))
print()
print(fmt_row(after))
print()

# delta
def pct(r, k): return r[k] / max(r["total"], 1) * 100
print("=" * 70)
print("【关键差异 △】")
print("=" * 70)
print(f"  工单总量:        {before['total']} → {after['total']}    "
      f"({(after['total']-before['total'])/max(before['total'],1)*100:+.1f}%)")
print(f"  关闭率:          {pct(before,'closed'):.1f}% → {pct(after,'closed'):.1f}%   "
      f"({pct(after,'closed')-pct(before,'closed'):+.1f}pp)")
print(f"  已离开新建态率:   {pct(before,'not_new'):.1f}% → {pct(after,'not_new'):.1f}%   "
      f"({pct(after,'not_new')-pct(before,'not_new'):+.1f}pp)")
print(f"  分配率:          {pct(before,'assigned'):.1f}% → {pct(after,'assigned'):.1f}%   "
      f"({pct(after,'assigned')-pct(before,'assigned'):+.1f}pp)")
print()

print("=" * 70)
print("【对比：全公司 tracker=支持 同期数据（佐证）】")
print("=" * 70)
print(fmt_row(before_all))
print()
print(fmt_row(after_all))
print()
print("【全公司差异 △】")
print(f"  关闭率:    {pct(before_all,'closed'):.1f}% → {pct(after_all,'closed'):.1f}%   "
      f"({pct(after_all,'closed')-pct(before_all,'closed'):+.1f}pp)")

print()
print("说明：")
print("  - '关闭率' = 工单已 closed 数 / 总创建数（数据采样到 6/26，新近工单可能尚未关闭，是天然滞后）")
print("  - 上线后窗口包含 6/22-6/26 这一周，部分工单仍在处理中，故关闭率受时间窗影响")
print("  - 建议关注 '已离开新建态率'（被人接手处理），不受关闭滞后影响")
