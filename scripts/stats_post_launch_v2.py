"""相似案件 AI 一楼上线后召回情况复盘 v2 — 用户偏好"直接看不引用"，
所以剔除"引用率"指标，改用更贴实际使用模式的指标。

新指标维度：
1. 总量
2. 召回数量分布
3. 置信度分布（LLM score）
4. 时间趋势
5. 工单生命周期：
   - AI 楼后是否有真人跟进
   - 关闭率（写楼 vs 同期支持类全量）
   - 平均关闭时长
   - AI 楼到首次真人回复的间隔时间（说明用户"看了再说"）
6. 召回的历史案件跨度（被推荐工单 vs 当前工单相距多久）
7. 召回案件分布（产品/模块/项目）
"""
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import mean, median

sys.path.insert(0, "/app"); os.chdir("/app")
from src.db_client import RedmineDB


def fmt_h(seconds):
    if seconds is None:
        return "-"
    h = seconds / 3600
    if h < 24:
        return f"{h:.1f}h"
    return f"{h/24:.1f}d"


log = sqlite3.connect("/app/data/assist_log.db")
log.row_factory = sqlite3.Row
rows = list(log.execute(
    "SELECT issue_id, processed_at, candidates_json, note_written FROM assist_log"
))
print(f"=== assist_log 累计 {len(rows):,} 条 ===")
written = sum(1 for r in rows if r["note_written"] == 1)
print(f"成功写楼 {written:,} ({written/len(rows)*100:.1f}%)，未写楼 {len(rows)-written}\n")

# 召回数量
both_empty = 0
picks_count, doc_picks_count = [], []
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
        picks_count.append(0); doc_picks_count.append(0)

print("=== 召回数量分布 ===")
print(f"两栏都空（暂无推荐）: {both_empty:,} ({both_empty/len(rows)*100:.1f}%)")
print(f"工单 picks  avg={mean(picks_count):.2f} 分布={dict(sorted(Counter(picks_count).items()))}")
print(f"文档 docs   avg={mean(doc_picks_count):.2f} 分布={dict(sorted(Counter(doc_picks_count).items()))}")

# 置信度
scores = []
for r in rows:
    try:
        cj = json.loads(r["candidates_json"] or "{}")
        for p in (cj.get("picks", []) if isinstance(cj, dict) else []):
            if "score" in p:
                scores.append(float(p["score"]))
    except Exception:
        pass
if scores:
    buckets = Counter()
    for s in scores:
        b = f"{int(s*10)/10:.1f}-{int(s*10)/10+0.1:.1f}"
        buckets[b] += 1
    print(f"\n=== picks 置信度分布（共 {len(scores)} 个 picks）===")
    for k in sorted(buckets):
        print(f"  {k}: {buckets[k]:,}  ({buckets[k]/len(scores)*100:.1f}%)")
    print(f"  score≥0.7 比例: {sum(1 for s in scores if s>=0.7)/len(scores)*100:.1f}%")

# 时间趋势
by_day = Counter()
for r in rows:
    d = (r["processed_at"] or "")[:10]
    if d:
        by_day[d] += 1
print(f"\n=== 按日处理量 ===")
for d in sorted(by_day.keys()):
    print(f"  {d}: {by_day[d]:,}")
print(f"工作日均: ~{int(sum(v for k,v in by_day.items() if datetime.fromisoformat(k).weekday()<5)/max(1,sum(1 for k in by_day if datetime.fromisoformat(k).weekday()<5)))} 条")

# === 工单生命周期对比 ===
db = RedmineDB()
written_ids = [r["issue_id"] for r in rows if r["note_written"] == 1]
sample = sorted(written_ids)[-300:]  # 取最近 300 个

# 拿这些 issue 的关键字段
with db._conn() as (_, cur):
    placeholders = ",".join("%s" for _ in sample)
    cur.execute(
        f"""SELECT i.id, i.tracker_id, i.status_id, i.created_on, i.closed_on, i.updated_on
              FROM issues i WHERE i.id IN ({placeholders})""",
        tuple(sample),
    )
    issue_info = {r["id"]: r for r in cur.fetchall()}

# 拿 AI 楼时间 + 首次真人跟进时间
ai_user_id = 6011
followup_after_ai = []  # AI 楼后到首次真人回复的间隔
no_followup = 0
followup_count = 0

with db._conn() as (_, cur):
    cur.execute(
        f"""SELECT j.journalized_id AS iid, j.user_id, j.created_on, j.notes
              FROM journals j
             WHERE j.journalized_type='Issue'
               AND j.journalized_id IN ({placeholders})
             ORDER BY j.journalized_id, j.id""",
        tuple(sample),
    )
    j_by_iid = defaultdict(list)
    for j in cur.fetchall():
        j_by_iid[j["iid"]].append(j)

for iid in sample:
    jrnl = j_by_iid.get(iid, [])
    ai_idx = next((i for i, j in enumerate(jrnl) if j["user_id"] == ai_user_id), None)
    if ai_idx is None:
        continue
    ai_time = jrnl[ai_idx]["created_on"]
    after = [j for j in jrnl[ai_idx+1:]
             if j["user_id"] != ai_user_id and (j.get("notes") or "").strip()]
    if after:
        followup_count += 1
        first = after[0]["created_on"]
        followup_after_ai.append((first - ai_time).total_seconds())
    else:
        no_followup += 1

total = followup_count + no_followup
print(f"\n=== 工单生命周期（采样最近 {total} 个写楼工单）===")
print(f"AI 楼后有真人跟进: {followup_count}/{total} = {followup_count/total*100:.1f}%")
print(f"AI 楼后无跟进:     {no_followup}/{total} = {no_followup/total*100:.1f}%")
if followup_after_ai:
    print(f"AI 楼→首次真人回复 间隔:")
    print(f"  中位数 (median): {fmt_h(median(followup_after_ai))}")
    print(f"  平均   (mean):   {fmt_h(mean(followup_after_ai))}")
    quick = sum(1 for s in followup_after_ai if s < 3600)
    print(f"  ≤1 小时 内回复: {quick}/{len(followup_after_ai)} = {quick/len(followup_after_ai)*100:.1f}%")
    print(f"  ≤4 小时 内回复: {sum(1 for s in followup_after_ai if s < 4*3600)}/{len(followup_after_ai)} = {sum(1 for s in followup_after_ai if s < 4*3600)/len(followup_after_ai)*100:.1f}%")

# 关闭率
closed = sum(1 for i in sample if issue_info.get(i, {}).get("closed_on"))
print(f"\n=== 关闭率对比 ===")
print(f"写楼工单关闭: {closed}/{len(sample)} = {closed/len(sample)*100:.1f}%")

# 同期支持类全量
earliest = min((row["processed_at"] for row in rows if row["issue_id"] in sample), default="")
if earliest:
    with db._conn() as (_, cur):
        cur.execute(
            """SELECT COUNT(*) AS n,
                      SUM(CASE WHEN closed_on IS NOT NULL THEN 1 ELSE 0 END) AS closed
                 FROM issues
                WHERE tracker_id = 3 AND created_on >= %s""",
            (earliest[:10],),
        )
        r2 = cur.fetchone()
        print(f"同期支持类全量: {r2['closed']}/{r2['n']} = {r2['closed']/r2['n']*100:.1f}% (since {earliest[:10]})")

# 平均关闭时长（写楼 vs 同期）
def avg_close_dur(ids, label):
    durs = []
    for i in ids:
        info = issue_info.get(i)
        if info and info.get("closed_on") and info.get("created_on"):
            durs.append((info["closed_on"] - info["created_on"]).total_seconds())
    if durs:
        print(f"{label}: 中位关闭时长 {fmt_h(median(durs))}, 平均 {fmt_h(mean(durs))} (n={len(durs)})")

avg_close_dur(sample, "  写楼工单")

# 同期支持类全量平均关闭时长（限定相同时间窗）
if earliest:
    with db._conn() as (_, cur):
        cur.execute(
            """SELECT created_on, closed_on FROM issues
                WHERE tracker_id=3 AND created_on>=%s AND closed_on IS NOT NULL
                LIMIT 1000""",
            (earliest[:10],),
        )
        durs = [(r["closed_on"] - r["created_on"]).total_seconds() for r in cur.fetchall()]
        if durs:
            print(f"  同期支持全量: 中位 {fmt_h(median(durs))}, 平均 {fmt_h(mean(durs))} (n={len(durs)})")

# === 召回历史案件跨度（被推荐工单距当前工单多久了）===
print(f"\n=== 召回历史案件跨度（被推荐工单 vs 当前工单的时间距离）===")
recommended_ids = set()
current_to_recommended = []  # (current_iid, [recommended_iids])
for r in rows:
    if r["note_written"] != 1:
        continue
    try:
        cj = json.loads(r["candidates_json"] or "{}")
        picks = [p["issue_id"] for p in (cj.get("picks") or [])]
        if picks:
            recommended_ids.update(picks)
            current_to_recommended.append((r["issue_id"], picks))
    except Exception:
        pass

if recommended_ids:
    all_check = list(recommended_ids) + [c[0] for c in current_to_recommended]
    with db._conn() as (_, cur):
        placeholders = ",".join("%s" for _ in all_check)
        cur.execute(
            f"SELECT id, created_on FROM issues WHERE id IN ({placeholders})",
            tuple(all_check),
        )
        created_by_id = {r["id"]: r["created_on"] for r in cur.fetchall()}

    spans_days = []
    for cur_id, recs in current_to_recommended:
        cur_time = created_by_id.get(cur_id)
        if not cur_time:
            continue
        for rec_id in recs:
            rec_time = created_by_id.get(rec_id)
            if rec_time and rec_time < cur_time:
                spans_days.append((cur_time - rec_time).days)
    if spans_days:
        print(f"  样本数: {len(spans_days)} pair")
        print(f"  中位距离: {median(spans_days)} 天")
        print(f"  平均距离: {mean(spans_days):.0f} 天")
        buckets = Counter()
        for d in spans_days:
            if d < 30: buckets["<30天"] += 1
            elif d < 90: buckets["30-90天"] += 1
            elif d < 180: buckets["90-180天"] += 1
            elif d < 365: buckets["180-365天"] += 1
            elif d < 730: buckets["1-2年"] += 1
            else: buckets[">2年"] += 1
        for k in ["<30天","30-90天","90-180天","180-365天","1-2年",">2年"]:
            v = buckets[k]
            if v:
                print(f"    {k}: {v} ({v/len(spans_days)*100:.1f}%)")

# === 召回案件涉及哪些产品/模块 ===
print(f"\n=== 召回案件涉及产品/模块 top 10 ===")
if recommended_ids:
    rec_list = list(recommended_ids)
    with db._conn() as (_, cur):
        placeholders = ",".join("%s" for _ in rec_list)
        cur.execute(
            f"""SELECT cfv.value, COUNT(*) AS n
                  FROM custom_values cfv
                  JOIN custom_fields cf ON cfv.custom_field_id=cf.id
                 WHERE cfv.customized_type='Issue'
                   AND cfv.customized_id IN ({placeholders})
                   AND cf.name='产品'
                   AND cfv.value IS NOT NULL AND cfv.value != ''
                 GROUP BY cfv.value ORDER BY n DESC LIMIT 10""",
            tuple(rec_list),
        )
        for r in cur.fetchall():
            print(f"  {r['value']}: {r['n']}")
