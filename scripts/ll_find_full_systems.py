"""筛选最近 3 个月"完整灵珑系统需求"。

策略：
  1. 排除"基础平台-灵珑平台"项目（灵珑核心团队自己的开发工单）
  2. 只留 tracker=需求(2) 或 里程碑(15)  —— 这两类天然是"新系统"
  3. desc >= 500 字（有实际需求内容）
  4. LLM 二次判"是否完整灵珑系统"
"""
import sys, os, json, time
sys.path.insert(0, "/app")
os.chdir("/app")

from src.db_client import RedmineDB
from src.llm_judge import _call
from src.text_cleaner import clean_html
from datetime import datetime, timedelta

db = RedmineDB()
since = (datetime.now() - timedelta(days=95)).strftime("%Y-%m-%d")

# Step 1: SQL 粗筛
with db._conn() as (_, cur):
    kw = "%灵珑%"
    cur.execute(
        """SELECT i.id, i.subject, i.description,
                  i.tracker_id, t.name as tracker_name,
                  i.status_id, s.name as status_name,
                  p.name as project_name,
                  i.created_on,
                  CHAR_LENGTH(i.description) as desc_len
             FROM issues i
             JOIN trackers t ON t.id = i.tracker_id
             JOIN issue_statuses s ON s.id = i.status_id
             LEFT JOIN projects p ON p.id = i.project_id
            WHERE i.created_on >= %s
              AND (i.subject LIKE %s OR i.description LIKE %s)
              AND (p.name IS NULL OR p.name NOT LIKE %s)
              AND i.tracker_id IN (2, 15)
              AND CHAR_LENGTH(i.description) >= 500
            ORDER BY CHAR_LENGTH(i.description) DESC""",
        (since, kw, kw, "%基础平台-灵珑平台%"),
    )
    rows = cur.fetchall()

print(f"since={since}")
print(f"SQL 粗筛得: {len(rows)} 条 (tracker=需求/里程碑, desc>=500, 排除灵珑核心项目)")

# Step 2: LLM 二次判
_PROMPT = """你是 Redmine 工单分析员，判断一个工单**是否是一个"完整的灵珑低代码系统建设需求"**。

【完整系统 hit 条件】（满足任一即算 hit）：
- 明确要求建一整个业务系统（案卷/流程/看板/移动端等多模块）
- 涉及"数据模型/表单/流程/工作台/移动端/打印/权限"中至少 3 个方面
- 描述含"整套/一整套/完整/全套/新建/迁移到灵珑/系统建设"等关键词
- 需求量足够大（页面/流程/角色多个），能作为一个完整交付项

【miss 条件】：
- 单点 bug 修复、字段调整、单模块优化
- 只是"帮忙看看XX配置"
- 只涉及 1-2 个页面的小改动
- 例行更新/发版/环境部署

【工单信息】
标题: {subject}
tracker: {tracker}  项目: {project}  desc_len={desc_len}
描述(前 2000 字):
{desc}

严格只输出 JSON: {{"verdict": "hit|miss", "why": "≤30字理由", "scope": "如果 hit,估算规模: S(3-5天)/M(1-3周)/L(1-3月)"}}
"""

hit_ids = []
results = []
BATCH_LOG = 10
t0 = time.time()

# 上限：粗筛出来太多就抽最长 200 条（desc 已 DESC 排序）
LIMIT = int(os.environ.get("LL_LIMIT", "50"))
sample = rows[:LIMIT]
print(f"送 LLM 判定: {len(sample)} 条（按 desc 长度前 {LIMIT}）")

for i, r in enumerate(sample, 1):
    subj = (r["subject"] or "").replace("\n", " ")[:100]
    desc = clean_html(r["description"] or "")[:2000]
    prompt = _PROMPT.format(
        subject=subj,
        tracker=r["tracker_name"],
        project=(r["project_name"] or "?")[:40],
        desc_len=r["desc_len"],
        desc=desc,
    )
    try:
        raw = _call(
            [
                {"role": "system", "content": "You output only JSON, no prose."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=1500,
        )
    except Exception as e:
        print(f"  {i}. #{r['id']} LLM err: {e}")
        continue
    try:
        obj = json.loads((raw or "").strip())
    except json.JSONDecodeError:
        import re
        m = re.search(r"\{[\s\S]*\}", raw or "")
        if m:
            try: obj = json.loads(m.group(0))
            except: obj = {}
        else: obj = {}
    verdict = obj.get("verdict", "miss")
    why = obj.get("why", "")
    scope = obj.get("scope", "")
    if verdict == "hit":
        hit_ids.append(r["id"])
        results.append({
            "id": r["id"],
            "subject": subj,
            "tracker": r["tracker_name"],
            "project": r["project_name"] or "",
            "status": r["status_name"],
            "desc_len": r["desc_len"],
            "created_on": str(r["created_on"]),
            "why": why,
            "scope": scope,
        })
    if i % BATCH_LOG == 0:
        elapsed = time.time() - t0
        print(f"  ...judged {i}/{len(sample)} hit={len(hit_ids)} elapsed={elapsed:.0f}s", flush=True)

print()
print(f"=== 结果：LLM 判 hit（完整灵珑系统）{len(results)} 条 ===")
for r in sorted(results, key=lambda x: -x["desc_len"]):
    print(
        f"#{r['id']:>6} [{r['tracker']:>3}][{r['status']:>10}] "
        f"desc={r['desc_len']:>5} scope={r['scope']} | "
        f"{r['subject'][:55]}"
    )
    print(f"        项目: {r['project'][:50]}")
    print(f"        理由: {r['why']}")

# 落地一份 JSON 给 Claude 拿去做后续
out_path = "/tmp/ll_full_systems.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print(f"\n结果 JSON: {out_path}")
