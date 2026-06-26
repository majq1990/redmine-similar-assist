"""相似案件 AI 一楼上线后召回情况复盘统计。

输出：
1. 总量：处理数 / 写楼数 / 跳过原因分布
2. 召回质量：picks/doc_picks 数量分布、置信度分布
3. 时间趋势：按日累计
4. 召回有效性：
   - AI 楼后是否有人继续回复（说明被看了）
   - 召回的 #issue_id 是否被用户在 journal 里引用（说明被采纳）
   - 写楼工单的关闭率 / 平均处理时长
"""
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime

sys.path.insert(0, "/app"); os.chdir("/app")
from src.db_client import RedmineDB

# 1. 拉 assist_log
log = sqlite3.connect("/app/data/assist_log.db")
log.row_factory = sqlite3.Row
rows = list(log.execute(
    "SELECT issue_id, processed_at, candidates_json, note_written FROM assist_log"
))
print(f"=== assist_log 累计记录: {len(rows):,} 条 ===\n")

# 2. 总量统计
written = sum(1 for r in rows if r["note_written"] == 1)
not_written = len(rows) - written
print(f"成功写楼:       {written:,}  ({written/len(rows)*100:.1f}%)")
print(f"未写楼:         {not_written:,}  ({not_written/len(rows)*100:.1f}%)")

# 3. picks/doc_picks 数量分布
picks_count = []
doc_picks_count = []
both_empty = 0
for r in rows:
    try:
        cj = json.loads(r["candidates_json"] or "{}")
        picks = cj.get("picks", []) if isinstance(cj, dict) else []
        docs = cj.get("doc_picks", []) if isinstance(cj, dict) else []
        picks_count.append(len(picks))
        doc_picks_count.append(len(docs))
        if not picks and not docs:
            both_empty += 1
    except Exception:
        pass

print(f"\n=== 召回数量分布 ===")
print(f"两栏都空（暂无推荐）: {both_empty:,}  ({both_empty/len(rows)*100:.1f}%)")
print(f"工单召回 (picks)  : avg={sum(picks_count)/len(picks_count):.2f}, "
      f"max={max(picks_count)}, 分布={dict(Counter(picks_count).most_common())}")
print(f"文档召回 (doc_picks): avg={sum(doc_picks_count)/len(doc_picks_count):.2f}, "
      f"max={max(doc_picks_count)}, 分布={dict(Counter(doc_picks_count).most_common())}")

# 4. 置信度分布（取 picks 的 score）
all_scores = []
for r in rows:
    try:
        cj = json.loads(r["candidates_json"] or "{}")
        for p in (cj.get("picks", []) if isinstance(cj, dict) else []):
            if "score" in p:
                all_scores.append(float(p["score"]))
    except Exception:
        pass
if all_scores:
    buckets = Counter()
    for s in all_scores:
        b = f"{int(s*10)/10:.1f}-{int(s*10)/10+0.1:.1f}"
        buckets[b] += 1
    print(f"\n=== picks 置信度分布（共 {len(all_scores)} 个 picks）===")
    for k in sorted(buckets):
        print(f"  {k}: {buckets[k]:,}  ({buckets[k]/len(all_scores)*100:.1f}%)")

# 5. 时间趋势（按日）
by_day = Counter()
for r in rows:
    d = (r["processed_at"] or "")[:10]
    if d:
        by_day[d] += 1
print(f"\n=== 按日处理量（最近 15 天）===")
for d in sorted(by_day.keys())[-15:]:
    print(f"  {d}: {by_day[d]:,}")

# 6. 召回有效性 - 检查 AI 楼之后是否有 journal（说明被看 + 跟进）
db = RedmineDB()
written_ids = [r["issue_id"] for r in rows if r["note_written"] == 1]
print(f"\n=== 召回有效性（采样最近 200 个写楼工单）===")
sample = sorted(written_ids)[-200:]

with db._conn() as (_, cur):
    placeholders = ",".join("%s" for _ in sample)
    cur.execute(
        f"""SELECT j.journalized_id AS iid, j.user_id, j.created_on, j.notes
              FROM journals j
             WHERE j.journalized_type='Issue'
               AND j.journalized_id IN ({placeholders})
             ORDER BY j.journalized_id, j.id""",
        tuple(sample),
    )
    journals_by_issue = defaultdict(list)
    for j in cur.fetchall():
        journals_by_issue[j["iid"]].append(j)

# 找每个 issue 的 AI 楼时间 + 之后是否有真人回复
ai_user_id = 6011  # egova-gczx
has_followup = 0
no_followup = 0
ai_referenced = 0  # AI 召回的 #issue_id 是否被用户在 journal 里提到

# 重新读 candidates_json 拿 picks 的 issue_id
picks_by_issue = {}
for r in rows:
    if r["issue_id"] in sample:
        try:
            cj = json.loads(r["candidates_json"] or "{}")
            picks_by_issue[r["issue_id"]] = [p["issue_id"] for p in (cj.get("picks") or [])]
        except Exception:
            pass

import re as _re
for iid in sample:
    jrnl = journals_by_issue.get(iid, [])
    # 找 AI 楼位置
    ai_idx = None
    for i, j in enumerate(jrnl):
        if j["user_id"] == ai_user_id:
            ai_idx = i
            break
    if ai_idx is None:
        continue
    # AI 楼之后是否有真人 journal（user_id != 6011 且 notes 非空）
    after = jrnl[ai_idx + 1:]
    real_follow = [j for j in after if j["user_id"] != ai_user_id and (j.get("notes") or "").strip()]
    if real_follow:
        has_followup += 1
    else:
        no_followup += 1
    # AI 推荐的 #id 是否被用户回复引用
    recommended_ids = set(picks_by_issue.get(iid, []))
    if recommended_ids:
        user_text = " ".join((j.get("notes") or "") for j in real_follow)
        mentioned = set(int(m) for m in _re.findall(r"#(\d{5,7})", user_text))
        if mentioned & recommended_ids:
            ai_referenced += 1

total_check = has_followup + no_followup
if total_check:
    print(f"  AI 楼后有真人跟进:     {has_followup}/{total_check} = {has_followup/total_check*100:.1f}%")
    print(f"  AI 楼后无任何跟进:     {no_followup}/{total_check} = {no_followup/total_check*100:.1f}%")
    print(f"  用户在跟进里引用 AI 推荐的 #id: {ai_referenced}/{total_check} = {ai_referenced/total_check*100:.1f}%")
    print("  （引用率 = 召回真正被采纳的硬指标，行业内 >5% 算合格、>10% 优秀）")

# 7. 关闭率对比（写楼工单 vs 全公司"支持"tracker 工单同期）
print(f"\n=== 关闭率对比（写楼工单 vs 同期支持类全量）===")
with db._conn() as (_, cur):
    placeholders = ",".join("%s" for _ in sample)
    cur.execute(
        f"""SELECT COUNT(*) AS n,
                   SUM(CASE WHEN closed_on IS NOT NULL THEN 1 ELSE 0 END) AS closed
              FROM issues WHERE id IN ({placeholders})""",
        tuple(sample),
    )
    r = cur.fetchone()
    print(f"  写楼工单关闭率: {r['closed']}/{r['n']} = {r['closed']/r['n']*100:.1f}%")

    # 比对：sample 中最早处理日期之后所有支持类工单的关闭率
    earliest = min((row["processed_at"] for row in rows if row["issue_id"] in sample), default="")
    if earliest:
        cur.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN closed_on IS NOT NULL THEN 1 ELSE 0 END) AS closed
                 FROM issues
                WHERE tracker_id = 3
                  AND created_on >= %s""",
            (earliest[:10],),
        )
        r2 = cur.fetchone()
        print(f"  同期支持类全量关闭率: {r2['closed']}/{r2['n']} = "
              f"{r2['closed']/r2['n']*100:.1f}% (创建于 {earliest[:10]} 后)")
