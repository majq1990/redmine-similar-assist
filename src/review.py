"""AI 一楼后置质量回顾：评估 AI 推荐是否真的帮到了案件处理。

对已关闭 + 有实际解决方案（form_develop_finish/form_tester_verify 非空）的案件：
  1. 从 assist_log 拉当时 AI 推荐的 picks + doc_picks
  2. 从 redmine 拉 issue + journals（剔 AI user_id=6011） + 表单
  3. LLM 对每条推荐评估 hit/partial/miss/unknown + overall good/mixed/poor
  4. 结果存 assist_log.db 的 review_log 表
  5. 可选：对 overall=poor 的案件写"回顾楼"（gczx 身份）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from typing import Any

from .config import cfg, project_root
from .db_client import RedmineDB
from .llm_judge import _call
from .redmine_client import RedmineClient
from .text_cleaner import clean_html


AI_USER_ID_DEFAULT = 6011  # egova-gczx


# ---------- Review log 表 ----------

def _ensure_review_log() -> sqlite3.Connection:
    path = project_root() / cfg()["storage"]["log_db"]
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS review_log(
              issue_id            INTEGER PRIMARY KEY,
              reviewed_at         TEXT,
              picks_verdicts_json TEXT,
              doc_verdicts_json   TEXT,
              overall_quality     TEXT,   -- good / mixed / poor
              summary             TEXT,
              note_written        INTEGER DEFAULT 0,
              elapsed_ms          INTEGER
           )"""
    )
    conn.commit()
    return conn


# ---------- 样本挑选 ----------

def pick_reviewable_ids_stratified(
    per_segment: int = 4,
    segments: list[tuple[str, str, str]] | None = None,
    skip_reviewed: bool = True,
) -> list[int]:
    """按 processed_at 时间段分层抽样。

    segments: [(label, start_yyyymmdd, end_yyyymmdd), ...] 半开区间 [start, end)。
    默认覆盖上线到现在 5 个时段（首周/B1前/B1后1周/6月末/7月）。
    """
    if segments is None:
        segments = [
            ("首周 5/28-6/6", "2026-05-28", "2026-06-06"),
            ("B1前 6/6-6/15", "2026-06-06", "2026-06-15"),
            ("B1后1w 6/15-6/23", "2026-06-15", "2026-06-23"),
            ("6月末 6/23-7/1", "2026-06-23", "2026-07-01"),
            ("7月 7/1-", "2026-07-01", "2100-01-01"),
        ]

    log_path = project_root() / cfg()["storage"]["log_db"]
    lc = sqlite3.connect(str(log_path))

    reviewed = set()
    if skip_reviewed:
        try:
            reviewed = {
                r[0] for r in lc.execute("SELECT issue_id FROM review_log").fetchall()
            }
        except sqlite3.OperationalError:
            pass

    db = RedmineDB()
    all_picked: list[int] = []
    for label, start, end in segments:
        # 拿该段的候选（按 processed_at DESC 拉，再过滤）
        seg_candidates = [
            r[0]
            for r in lc.execute(
                "SELECT issue_id FROM assist_log WHERE note_written=1 "
                "AND processed_at >= ? AND processed_at < ? "
                "ORDER BY processed_at DESC",
                (start, end),
            ).fetchall()
            if r[0] not in reviewed
        ]
        # 过滤 closed + form 有内容
        segment_picked = _filter_closed_with_forms(db, seg_candidates, per_segment)
        print(
            f"[stratified] {label:<20} raw={len(seg_candidates):>3} "
            f"picked={len(segment_picked)}",
            flush=True,
        )
        all_picked.extend(segment_picked)
    return all_picked


def _filter_closed_with_forms(
    db: RedmineDB, candidates: list[int], limit: int
) -> list[int]:
    """从候选 issue_id 里筛：已关闭 + form 表有实际方案。返回前 limit 条按原顺序。"""
    if not candidates:
        return []
    BATCH = 500
    gold_set: set[int] = set()
    with db._conn() as (_, cur):
        for i in range(0, len(candidates), BATCH):
            batch = candidates[i : i + BATCH]
            ph = ",".join("%s" for _ in batch)
            cur.execute(
                f"""SELECT i.id FROM issues i
                    JOIN issue_statuses s ON s.id = i.status_id
                    WHERE i.id IN ({ph}) AND s.is_closed = 1""",
                tuple(batch),
            )
            closed_ids = {r["id"] for r in cur.fetchall()}
            if not closed_ids:
                continue
            ph2 = ",".join("%s" for _ in closed_ids)
            cur.execute(
                f"""SELECT DISTINCT issue_id FROM form_develop_finish
                    WHERE issue_id IN ({ph2})
                      AND function_description IS NOT NULL
                      AND function_description != ''
                    UNION
                    SELECT DISTINCT issue_id FROM form_tester_verify
                    WHERE issue_id IN ({ph2})
                      AND test_result IS NOT NULL
                      AND test_result != ''""",
                tuple(closed_ids) + tuple(closed_ids),
            )
            gold_set.update(r["issue_id"] for r in cur.fetchall())
    # 保原顺序取 limit 条
    out = []
    for iid in candidates:
        if iid in gold_set:
            out.append(iid)
            if len(out) >= limit:
                break
    return out


def pick_reviewable_ids(
    limit: int,
    skip_reviewed: bool = True,
    only_closed_with_solution: bool = True,
) -> list[int]:
    """挑可回顾样本：note_written=1、案件已关闭、form 表填了实际方案。

    返回按 processed_at DESC 排的 issue_id 列表。
    """
    log_path = project_root() / cfg()["storage"]["log_db"]
    lc = sqlite3.connect(str(log_path))
    reviewed = set()
    if skip_reviewed:
        try:
            reviewed = {r[0] for r in lc.execute(
                "SELECT issue_id FROM review_log"
            ).fetchall()}
        except sqlite3.OperationalError:
            pass  # 表还不存在

    all_candidates = [
        r[0]
        for r in lc.execute(
            "SELECT issue_id FROM assist_log "
            "WHERE note_written=1 ORDER BY processed_at DESC"
        ).fetchall()
        if r[0] not in reviewed
    ]
    if not only_closed_with_solution:
        return all_candidates[:limit]

    # 筛选：已关闭 + form 表非空
    db = RedmineDB()
    filtered: list[int] = []
    BATCH = 500
    with db._conn() as (_, cur):
        for i in range(0, len(all_candidates), BATCH):
            batch = all_candidates[i : i + BATCH]
            ph = ",".join("%s" for _ in batch)
            cur.execute(
                f"""SELECT i.id FROM issues i
                    JOIN issue_statuses s ON s.id = i.status_id
                    WHERE i.id IN ({ph}) AND s.is_closed = 1""",
                tuple(batch),
            )
            closed_ids = {r["id"] for r in cur.fetchall()}
            if not closed_ids:
                continue
            ph2 = ",".join("%s" for _ in closed_ids)
            cur.execute(
                f"""SELECT DISTINCT issue_id FROM form_develop_finish
                    WHERE issue_id IN ({ph2})
                      AND function_description IS NOT NULL
                      AND function_description != ''
                    UNION
                    SELECT DISTINCT issue_id FROM form_tester_verify
                    WHERE issue_id IN ({ph2})
                      AND test_result IS NOT NULL
                      AND test_result != ''""",
                tuple(closed_ids) + tuple(closed_ids),
            )
            gold = {r["issue_id"] for r in cur.fetchall()}
            # 保 processed_at 顺序
            for iid in batch:
                if iid in gold and iid not in filtered:
                    filtered.append(iid)
                    if len(filtered) >= limit:
                        return filtered
    return filtered


# ---------- 组装"实际处理过程" ----------

def _build_actual_resolution(
    db: RedmineDB,
    issue_id: int,
    ai_user_id: int = AI_USER_ID_DEFAULT,
    max_len: int = 5000,
) -> str:
    """把 journals + forms 汇总成"实际处理过程"文本。"""
    parts: list[str] = []

    # 1) journals（剔除 AI 楼、剔除空 notes）
    journals = db.fetch_journals_bulk([issue_id]).get(issue_id, [])
    real_journals = [j for j in journals if j.get("user_id") != ai_user_id]
    if real_journals:
        parts.append("【journal 处理记录】")
        for j in real_journals[-8:]:  # 只取后 8 条（越靠后越接近关闭时的最终方案）
            notes = clean_html(j.get("notes") or "")
            if notes.strip():
                parts.append(f"- {notes[:400]}")

    # 2) forms（研发完成、测试验证等）
    forms = db.fetch_form_records_bulk([issue_id]).get(issue_id, [])
    if forms:
        parts.append("\n【表单填写内容】")
        for f in forms:
            label = f.get("label") or f.get("source") or ""
            for field in f.get("fields") or []:
                value = clean_html(str(field.get("value") or ""))
                if value.strip():
                    parts.append(
                        f"- [{label} / {field.get('label')}] {value[:400]}"
                    )

    text = "\n".join(parts)
    if len(text) > max_len:
        text = text[:max_len] + "…(截断)"
    return text or "(无实际处理内容)"


# ---------- LLM 评估 ----------

_REVIEW_PROMPT = """你是 Redmine AI 助理的质量评估员。评估 AI 一楼当时推荐的相似案件/文档，是否真的帮到了这个案件的实际处理。

【原始案件】
{issue_text}

【案件的实际处理过程（不含 AI 楼）】
{actual_resolution}

【AI 当时推荐的相似历史案件】
{picks_block}

【AI 当时推荐的知识库文档】
{docs_block}

# 判定规则（严格按顺序）

对每条推荐先看是否 hit，若非再看 miss，**最后**才考虑 partial。**不要因为不确定就默认 partial** —— 那是错误的兜底。

## verdict 定义

- **hit**（推荐对处理有直接价值）：满足**任一**条件即算 hit
  - 推荐案件的解决方案思路与实际处理**方向一致**（都改配置/都改接口/都改字段/都改脚本）
  - 推荐涉及的**技术组件/模块与实际处理相同**（都涉及"星桥/灵珑/pgsql/悟空/apk"等具体组件）
  - 推荐的**关键动作/关键字**在实际处理过程中出现（同一脚本名/表名/配置项）
  - 文档的核心要点与实际处理"步骤 or 原理"一致

- **partial**（严格限定）：能看出**主题相关但解决方案思路明显不同**。例：都是"报表卡顿"但一个改索引一个改前端渲染 → partial

- **miss**：与实际处理完全无关（不同模块 / 不同产品线 / 内容误导）

- **unknown**（尽量不用）：仅当实际处理过程"完全没有可评估的解决信息"（只有一句"已处理"/"已确认"）才用

## overall_quality

- **good**：至少 1 条 hit
- **mixed**：无 hit 但至少 1 条 partial
- **poor**：全部 miss / unknown

# 输出格式

严格只输出 JSON 对象，不要额外文字：
{{
  "picks_verdicts": [{{"issue_id": <int>, "verdict": "hit|partial|miss|unknown", "reason": "≤40字，说明判定依据"}}],
  "doc_verdicts":   [{{"node_id":  "<str>", "verdict": "hit|partial|miss|unknown", "reason": "≤40字"}}],
  "overall_quality": "good|mixed|poor",
  "summary": "≤80字整体评价"
}}
"""


def _build_picks_block(picks: list[dict]) -> str:
    if not picks:
        return "(AI 当时未推荐相似案件)"
    lines = []
    for i, p in enumerate(picks, 1):
        iid = p.get("issue_id")
        subj = (p.get("subject") or "")[:80].replace("\n", " ")
        sol = (p.get("solution") or "(无)")[:400].replace("\n", " ")
        lines.append(f"{i}. issue_id={iid} 标题={subj}\n   当时给的解决方案: {sol}")
    return "\n".join(lines)


def _build_docs_block(doc_picks: list[dict]) -> str:
    if not doc_picks:
        return "(AI 当时未推荐文档)"
    lines = []
    for i, d in enumerate(doc_picks, 1):
        nid = d.get("node_id") or ""
        title = (d.get("title") or "")[:80].replace("\n", " ")
        sol = (d.get("solution") or "(无)")[:400].replace("\n", " ")
        lines.append(f"{chr(64+i)}. node_id={nid} 标题={title}\n   要点: {sol}")
    return "\n".join(lines)


def review_one(issue_id: int, dry_run_write: bool = True) -> dict:
    """回顾单个案件。返回 dict 结果。dry_run_write=True 表示"推荐质量差"时不真写楼。"""
    t0 = time.time()
    c = cfg()
    log_path = project_root() / c["storage"]["log_db"]
    lc = sqlite3.connect(str(log_path))

    # 1) 从 assist_log 拿当时推荐
    row = lc.execute(
        "SELECT candidates_json FROM assist_log WHERE issue_id=?", (issue_id,)
    ).fetchone()
    if not row:
        return {"issue_id": issue_id, "skipped": "not_in_assist_log"}
    try:
        cand = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        cand = {}
    # 兼容两种历史格式：
    #   早期 = 直接是 picks 列表 [{issue_id, subject, score, solution}, ...]
    #   现在 = {"picks":[...], "doc_picks":[...]}
    if isinstance(cand, list):
        picks = cand
        doc_picks = []
    elif isinstance(cand, dict):
        picks = cand.get("picks") or []
        doc_picks = cand.get("doc_picks") or []
    else:
        picks, doc_picks = [], []
    if not picks and not doc_picks:
        return {"issue_id": issue_id, "skipped": "no_recommendations_saved"}

    # 2) redmine 案件详情
    db = RedmineDB()
    rc = RedmineClient()
    ai_user_id = (c.get("redmine") or {}).get("ai_user_id", AI_USER_ID_DEFAULT)
    issue = rc.get_issue(issue_id, include="")
    subject = issue.get("subject") or ""
    description = clean_html(issue.get("description") or "")
    issue_text = f"[标题] {subject}\n[原始描述] {description[:1500]}"

    actual_resolution = _build_actual_resolution(db, issue_id, ai_user_id=ai_user_id)

    # 3) LLM 评估
    prompt = _REVIEW_PROMPT.format(
        issue_text=issue_text,
        actual_resolution=actual_resolution,
        picks_block=_build_picks_block(picks),
        docs_block=_build_docs_block(doc_picks),
    )
    raw = _call(
        [
            {"role": "system", "content": "You output only JSON, no prose."},
            {"role": "user", "content": prompt},
        ],
        max_tokens=8000,  # DeepSeek-v4 reasoning model 占配额
    )
    verdict = _safe_parse_review(raw)
    if not verdict:
        return {"issue_id": issue_id, "skipped": "llm_parse_failed", "raw_head": raw[:200]}

    picks_v = verdict.get("picks_verdicts") or []
    doc_v = verdict.get("doc_verdicts") or []
    overall = verdict.get("overall_quality") or "unknown"
    summary = verdict.get("summary") or ""

    # 4) 存 review_log
    rl = _ensure_review_log()
    rl.execute(
        """INSERT OR REPLACE INTO review_log
              (issue_id, reviewed_at, picks_verdicts_json, doc_verdicts_json,
               overall_quality, summary, note_written, elapsed_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            issue_id,
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            json.dumps(picks_v, ensure_ascii=False),
            json.dumps(doc_v, ensure_ascii=False),
            overall,
            summary[:500],
            0,
            int((time.time() - t0) * 1000),
        ),
    )
    rl.commit()

    result = {
        "issue_id": issue_id,
        "overall": overall,
        "picks_v": picks_v,
        "doc_v": doc_v,
        "summary": summary,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "wrote_note": False,
    }

    # 5) 推荐质量差时写楼（可选）
    if overall == "poor" and not dry_run_write:
        note = _build_review_note(picks, picks_v, doc_picks, doc_v, summary)
        try:
            rc.add_note(issue_id, note)
            rl.execute(
                "UPDATE review_log SET note_written=1 WHERE issue_id=?", (issue_id,)
            )
            rl.commit()
            result["wrote_note"] = True
        except Exception as e:
            sys.stderr.write(f"[review] write note failed for {issue_id}: {e}\n")

    return result


def _safe_parse_review(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # 尝试提取 { ... }
        import re
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(obj, dict):
        return None
    return obj


# ---------- 回顾楼渲染 ----------

def _build_review_note(
    picks: list[dict],
    picks_v: list[dict],
    doc_picks: list[dict],
    doc_v: list[dict],
    summary: str,
) -> str:
    """渲染"AI 推荐质量回顾"楼。CKEditor 友好 HTML，无 emoji。"""
    v_by_iid = {int(x.get("issue_id") or 0): x for x in picks_v}
    v_by_nid = {str(x.get("node_id") or ""): x for x in doc_v}
    parts = []
    parts.append("<p><strong>[AI 智能助理] 推荐质量回顾</strong></p>")
    parts.append(f"<p>{_esc(summary)}</p>")
    if picks:
        parts.append("<p><strong>[对当时推荐相似工单的评估]</strong></p><ul>")
        for p in picks:
            iid = p.get("issue_id")
            v = v_by_iid.get(int(iid or 0)) or {}
            verdict = v.get("verdict") or "unknown"
            reason = v.get("reason") or ""
            parts.append(
                f"<li>#{iid} <em>{_verdict_label(verdict)}</em>：{_esc(reason)}</li>"
            )
        parts.append("</ul>")
    if doc_picks:
        parts.append("<p><strong>[对当时推荐文档的评估]</strong></p><ul>")
        for d in doc_picks:
            title = d.get("title") or ""
            nid = d.get("node_id") or ""
            v = v_by_nid.get(str(nid)) or {}
            verdict = v.get("verdict") or "unknown"
            reason = v.get("reason") or ""
            parts.append(
                f"<li>{_esc(title)} <em>{_verdict_label(verdict)}</em>：{_esc(reason)}</li>"
            )
        parts.append("</ul>")
    parts.append(
        "<p><em>*本回顾由 AI 基于案件的实际处理过程（journal + 表单）"
        "对当时推荐质量的评价。结果已归档用于改进召回质量。</em></p>"
    )
    return "".join(parts)


def _verdict_label(v: str) -> str:
    return {
        "hit": "命中",
        "partial": "部分相关",
        "miss": "不相关",
        "unknown": "无法判断",
    }.get(v, v or "无法判断")


def _esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------- CLI ----------

def _cmd_run(args) -> None:
    if args.issue_id:
        ids = [args.issue_id]
    elif args.stratified:
        ids = pick_reviewable_ids_stratified(
            per_segment=args.per_segment,
            skip_reviewed=not args.rerun,
        )
        print(f"[review] stratified picked {len(ids)} reviewable issues")
    else:
        ids = pick_reviewable_ids(
            limit=args.limit,
            skip_reviewed=not args.rerun,
        )
        print(f"[review] picked {len(ids)} reviewable issues")

    results = []
    for i, iid in enumerate(ids, 1):
        print(f"[{i}/{len(ids)}] reviewing #{iid} ...", flush=True)
        try:
            r = review_one(iid, dry_run_write=not args.write_note)
            print(
                f"    → overall={r.get('overall')} "
                f"elapsed={r.get('elapsed_ms', 0)}ms "
                f"wrote={r.get('wrote_note')}"
            )
            results.append(r)
        except Exception as e:
            sys.stderr.write(f"[review] failed {iid}: {e}\n")
            import traceback as _tb
            _tb.print_exc()

    # 汇总
    good = sum(1 for r in results if r.get("overall") == "good")
    mixed = sum(1 for r in results if r.get("overall") == "mixed")
    poor = sum(1 for r in results if r.get("overall") == "poor")
    skipped = sum(1 for r in results if r.get("skipped"))
    print(
        f"\n[review] done: total={len(results)} "
        f"good={good} mixed={mixed} poor={poor} skipped={skipped}"
    )


def pick_all_reviewable_unreviewed(limit: int = 1000) -> list[int]:
    """拉所有 note_written=1 + is_closed=1 + form 有内容 + review_log 未评的 issue_id。

    按 processed_at DESC 排序（新写楼的优先），上限保护 limit。
    """
    log_path = project_root() / cfg()["storage"]["log_db"]
    lc = sqlite3.connect(str(log_path))
    reviewed = set()
    try:
        reviewed = {
            r[0] for r in lc.execute("SELECT issue_id FROM review_log").fetchall()
        }
    except sqlite3.OperationalError:
        pass

    # 全部 note_written=1 案件（无时间截断），按 processed_at DESC
    all_written = [
        r[0]
        for r in lc.execute(
            "SELECT issue_id FROM assist_log "
            "WHERE note_written=1 ORDER BY processed_at DESC"
        ).fetchall()
        if r[0] not in reviewed
    ]
    if not all_written:
        return []
    db = RedmineDB()
    return _filter_closed_with_forms(db, all_written, limit)


def _cmd_backfill_all(args) -> None:
    """补跑：所有历史 AI 楼里 (关闭+form 有内容+未评过) 的案件都跑一遍 review。"""
    ids = pick_all_reviewable_unreviewed(limit=args.limit)
    print(f"[backfill] {len(ids)} cases to review")
    results = []
    for i, iid in enumerate(ids, 1):
        try:
            r = review_one(iid, dry_run_write=not args.write_note)
            print(
                f"[{i}/{len(ids)}] #{iid} overall={r.get('overall')} "
                f"elapsed={r.get('elapsed_ms', 0)}ms wrote={r.get('wrote_note')}",
                flush=True,
            )
            results.append(r)
        except Exception as e:
            sys.stderr.write(f"[backfill] failed {iid}: {e}\n")

    good = sum(1 for r in results if r.get("overall") == "good")
    mixed = sum(1 for r in results if r.get("overall") == "mixed")
    poor = sum(1 for r in results if r.get("overall") == "poor")
    print(f"\n[backfill] done: total={len(results)} good={good} mixed={mixed} poor={poor}")


def _cmd_weekly(args) -> None:
    """出上周报告 → HTML 落到 out-dir → 钉钉推送访问 URL。"""
    import datetime as _dt
    from pathlib import Path as _Path
    from .notify import post_dingtalk

    now = _dt.datetime.now()
    end = now
    start = now - _dt.timedelta(days=args.days)
    fname_stamp = now.strftime("%Y%m%d")
    fname = f"{fname_stamp}.html"

    # 拉窗口内案件（用 review_log.reviewed_at）
    log_path = project_root() / cfg()["storage"]["log_db"]
    lc = sqlite3.connect(str(log_path))
    rows = lc.execute(
        "SELECT r.issue_id, r.overall_quality, r.summary, "
        "r.picks_verdicts_json, r.doc_verdicts_json, r.reviewed_at "
        "FROM review_log r WHERE r.reviewed_at >= ? AND r.reviewed_at < ? "
        "ORDER BY r.reviewed_at DESC",
        (start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")),
    ).fetchall()

    total = len(rows)
    good = sum(1 for r in rows if r[1] == "good")
    mixed = sum(1 for r in rows if r[1] == "mixed")
    poor = sum(1 for r in rows if r[1] == "poor")

    def _flatten_verds(col: int) -> dict:
        counter = {"hit": 0, "partial": 0, "miss": 0, "unknown": 0}
        n = 0
        for r in rows:
            try:
                arr = json.loads(r[col] or "[]")
            except json.JSONDecodeError:
                arr = []
            for v in arr:
                vd = v.get("verdict") or "unknown"
                counter[vd] = counter.get(vd, 0) + 1
                n += 1
        return {"counter": counter, "n": n}

    pv = _flatten_verds(3)
    dv = _flatten_verds(4)
    base = cfg()["redmine"]["base_url"].rstrip("/")

    def _pct(n, t): return f"{n/max(t,1)*100:.0f}%"

    # HTML
    html_lines = [
        "<!doctype html>",
        f"<html lang='zh-CN'><head><meta charset='utf-8'>",
        f"<title>AI 一楼质量周报 {fname_stamp}</title>",
        "<style>",
        "body{font-family:'PingFang SC',system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#222;line-height:1.6}",
        "h1{border-bottom:2px solid #0066cc;padding-bottom:8px}",
        "h2{color:#0066cc;margin-top:32px}",
        "table{border-collapse:collapse;width:100%;margin:8px 0}",
        "th,td{border:1px solid #ddd;padding:6px 10px;text-align:left}",
        "th{background:#f6f8fa}",
        ".good{color:#28a745;font-weight:bold}",
        ".mixed{color:#ffc107;font-weight:bold}",
        ".poor{color:#dc3545;font-weight:bold}",
        ".hit{color:#28a745}",
        ".partial{color:#ffc107}",
        ".miss{color:#dc3545}",
        ".footer{color:#888;font-size:12px;margin-top:32px;border-top:1px solid #eee;padding-top:8px}",
        "</style></head><body>",
        f"<h1>AI 一楼质量周报</h1>",
        f"<p>报告生成时间：{now.strftime('%Y-%m-%d %H:%M')}<br>",
        f"回顾窗口：{start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}（{args.days} 天）</p>",
        "<h2>汇总</h2>",
        f"<p>本周期新评估案件总数：<strong>{total}</strong></p>",
    ]
    if total == 0:
        html_lines.append(
            "<p><em>本周期无新评估案件（可能是自动触发未跑，或所有已评过的都在窗口外）。</em></p>"
        )
    else:
        html_lines.extend([
            "<table><tr><th>整体质量</th><th>案件数</th><th>占比</th></tr>",
            f"<tr><td class='good'>good（至少 1 条命中）</td><td>{good}</td><td>{_pct(good,total)}</td></tr>",
            f"<tr><td class='mixed'>mixed（部分相关）</td><td>{mixed}</td><td>{_pct(mixed,total)}</td></tr>",
            f"<tr><td class='poor'>poor（全 miss/unknown）</td><td>{poor}</td><td>{_pct(poor,total)}</td></tr>",
            "</table>",
            "<h2>每条推荐 verdict 分布</h2>",
            f"<h3>工单推荐（共 {pv['n']} 条）</h3><ul>",
        ])
        for k in ("hit", "partial", "miss", "unknown"):
            n = pv["counter"].get(k, 0)
            html_lines.append(f"<li class='{k}'>{k}: {n} ({_pct(n, pv['n'])})</li>")
        html_lines.append("</ul>")
        html_lines.append(f"<h3>文档推荐（共 {dv['n']} 条）</h3><ul>")
        for k in ("hit", "partial", "miss", "unknown"):
            n = dv["counter"].get(k, 0)
            html_lines.append(f"<li class='{k}'>{k}: {n} ({_pct(n, dv['n'])})</li>")
        html_lines.append("</ul>")

        # poor 案件列表
        html_lines.append("<h2>推荐质量差（overall=poor）的案件</h2>")
        poors = [r for r in rows if r[1] == "poor"]
        if poors:
            html_lines.append("<ul>")
            for r in poors:
                iid = r[0]
                html_lines.append(
                    f"<li><a href='{base}/issues/{iid}' target='_blank'>#{iid}</a>"
                    f" - {_esc(r[2] or '')}</li>"
                )
            html_lines.append("</ul>")
        else:
            html_lines.append("<p>无</p>")

        # good 抽样
        html_lines.append("<h2>表现好（overall=good）的案件（前 20）</h2><ul>")
        goods = [r for r in rows if r[1] == "good"][:20]
        for r in goods:
            iid = r[0]
            html_lines.append(
                f"<li><a href='{base}/issues/{iid}' target='_blank'>#{iid}</a>"
                f" - {_esc(r[2] or '')}</li>"
            )
        html_lines.append("</ul>")

    html_lines.append(
        "<div class='footer'>本报告由 redmine-similar-assist review 模块自动生成。"
        "回顾对象：case 关闭后自动触发的 LLM 评估结果；"
        "评估依据：case 的实际处理过程（journal 剔除 AI 楼 + form_develop_finish + form_tester_verify）</div>"
    )
    html_lines.append("</body></html>")
    html = "\n".join(html_lines)

    # 落地
    out_dir = _Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / fname
    out_path.write_text(html, encoding="utf-8")
    print(f"[weekly] wrote {out_path} ({len(html)} bytes)")

    # 推钉钉
    url = f"{args.url_prefix.rstrip('/')}/{fname}"
    n = (cfg().get("notify") or {})
    review_webhook = n.get("review_webhook")
    review_secret = n.get("review_secret")
    review_keyword = n.get("review_keyword") or "周报"
    if args.no_push or not review_webhook:
        print(f"[weekly] URL={url}  （no-push={args.no_push} webhook={bool(review_webhook)}）")
        return

    md = (
        f"### AI 一楼质量周报 {fname_stamp}\n\n"
        f"- 回顾窗口：{start.strftime('%m/%d')} ~ {end.strftime('%m/%d')}\n"
        f"- 评估案件：**{total}**（good {good} / mixed {mixed} / poor {poor}）\n"
    )
    if total > 0:
        md += (
            f"- 工单推荐 hit 率：**{_pct(pv['counter'].get('hit',0), pv['n'])}**\n"
            f"- 文档推荐 hit 率：**{_pct(dv['counter'].get('hit',0), dv['n'])}**\n"
        )
    md += f"\n[完整报告 →]({url})\n\n> （本消息含关键字「{review_keyword}」）"
    try:
        r = post_dingtalk(
            review_webhook, review_secret, f"AI 一楼质量周报 {fname_stamp}", md
        )
        print(f"[weekly] dingtalk: {r}")
    except Exception as e:
        sys.stderr.write(f"[weekly] dingtalk failed: {e}\n")


def _cmd_write_poor(args) -> None:
    """从 review_log 拿 overall=poor 且未写楼的案件，用 gczx 写"AI 推荐质量回顾"楼。"""
    log_path = project_root() / cfg()["storage"]["log_db"]
    lc = sqlite3.connect(str(log_path))
    rows = lc.execute(
        "SELECT issue_id, picks_verdicts_json, doc_verdicts_json, summary "
        "FROM review_log WHERE overall_quality='poor' AND note_written=0 "
        "ORDER BY issue_id LIMIT ?",
        (args.limit,),
    ).fetchall()
    if not rows:
        print("[write-poor] no poor case to write")
        return
    print(f"[write-poor] {len(rows)} poor cases to write")

    rc = RedmineClient()
    written = 0
    failed = 0
    for issue_id, pv_json, dv_json, summary in rows:
        # 从 assist_log 拿原推荐（兼容 list/dict）
        arow = lc.execute(
            "SELECT candidates_json FROM assist_log WHERE issue_id=?", (issue_id,)
        ).fetchone()
        cand = {}
        if arow:
            try:
                cand = json.loads(arow[0] or "{}")
            except json.JSONDecodeError:
                cand = {}
        if isinstance(cand, list):
            picks = cand
            doc_picks = []
        elif isinstance(cand, dict):
            picks = cand.get("picks") or []
            doc_picks = cand.get("doc_picks") or []
        else:
            picks, doc_picks = [], []

        try:
            picks_v = json.loads(pv_json or "[]")
            doc_v = json.loads(dv_json or "[]")
        except json.JSONDecodeError:
            picks_v, doc_v = [], []

        note = _build_review_note(picks, picks_v, doc_picks, doc_v, summary)
        if args.dry_run:
            print(f"\n=== dry-run #{issue_id} ===")
            print(note[:600] + ("..." if len(note) > 600 else ""))
            continue
        try:
            rc.add_note(issue_id, note)
            lc.execute(
                "UPDATE review_log SET note_written=1 WHERE issue_id=?", (issue_id,)
            )
            lc.commit()
            written += 1
            print(f"  wrote #{issue_id}")
        except Exception as e:
            failed += 1
            sys.stderr.write(f"  failed #{issue_id}: {e}\n")

    if not args.dry_run:
        print(f"\n[write-poor] done: written={written} failed={failed}")


def _cmd_report(args) -> None:
    """从 review_log 生成 Markdown 汇总报告。"""
    log_path = project_root() / cfg()["storage"]["log_db"]
    lc = sqlite3.connect(str(log_path))
    rows = list(lc.execute(
        "SELECT issue_id, reviewed_at, overall_quality, summary, "
        "picks_verdicts_json, doc_verdicts_json FROM review_log "
        "ORDER BY reviewed_at DESC"
    ))
    if not rows:
        print("no review data"); return

    total = len(rows)
    good = sum(1 for r in rows if r[2] == "good")
    mixed = sum(1 for r in rows if r[2] == "mixed")
    poor = sum(1 for r in rows if r[2] == "poor")

    def _flatten(rows_col: int) -> list[str]:
        out = []
        for r in rows:
            try:
                arr = json.loads(r[rows_col] or "[]")
            except json.JSONDecodeError:
                arr = []
            for v in arr:
                out.append(v.get("verdict") or "unknown")
        return out

    pick_verds = _flatten(4)
    doc_verds = _flatten(5)
    def _pct(vs: list[str], k: str) -> str:
        return f"{sum(1 for v in vs if v == k)}/{len(vs)} = {sum(1 for v in vs if v == k)/max(len(vs),1)*100:.0f}%"

    base = cfg()["redmine"]["base_url"].rstrip("/")

    md = []
    md.append(f"# AI 一楼质量回顾报告")
    md.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M')}")
    md.append("")
    md.append(f"## 汇总")
    md.append(f"- 回顾案件总数: **{total}**")
    md.append(f"- 整体质量分布:")
    md.append(f"  - good (至少 1 条命中): **{good}** ({good/total*100:.0f}%)")
    md.append(f"  - mixed (部分相关): **{mixed}** ({mixed/total*100:.0f}%)")
    md.append(f"  - poor (全 miss/unknown): **{poor}** ({poor/total*100:.0f}%)")
    md.append("")
    md.append(f"## 每条推荐的 verdict 分布")
    md.append(f"### 工单推荐 (共 {len(pick_verds)} 条)")
    for k in ("hit", "partial", "miss", "unknown"):
        md.append(f"- {k}: {_pct(pick_verds, k)}")
    md.append(f"### 文档推荐 (共 {len(doc_verds)} 条)")
    for k in ("hit", "partial", "miss", "unknown"):
        md.append(f"- {k}: {_pct(doc_verds, k)}")
    md.append("")
    md.append(f"## 推荐质量差 (overall=poor) 的案件")
    poors = [r for r in rows if r[2] == "poor"]
    if not poors:
        md.append(f"（无）")
    else:
        for r in poors[:50]:
            iid = r[0]
            md.append(
                f"- [#{iid}]({base}/issues/{iid}) - {r[3]}"
            )
    md.append("")
    md.append(f"## 表现好的案件 (overall=good) 抽样")
    goods = [r for r in rows if r[2] == "good"][:20]
    for r in goods:
        iid = r[0]
        md.append(f"- [#{iid}]({base}/issues/{iid}) - {r[3]}")

    out = "\n".join(md)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(out)
        print(f"[report] wrote {args.out}")
    else:
        print(out)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="跑评估")
    p_run.add_argument("--issue-id", type=int, help="只跑单条")
    p_run.add_argument("--limit", type=int, default=20)
    p_run.add_argument(
        "--stratified", action="store_true",
        help="按 processed_at 5 个时段各抽 per_segment 条（覆盖上线到今）"
    )
    p_run.add_argument("--per-segment", type=int, default=4)
    p_run.add_argument("--rerun", action="store_true", help="重跑已评过的")
    p_run.add_argument(
        "--write-note", action="store_true",
        help="推荐质量差(overall=poor) 时用 gczx 写回顾楼（默认 dry run 不写）",
    )
    p_run.set_defaults(func=_cmd_run)

    p_rep = sub.add_parser("report", help="出汇总报告")
    p_rep.add_argument("--out", type=str, default=None)
    p_rep.set_defaults(func=_cmd_report)

    p_wp = sub.add_parser("write-poor", help="从 review_log 拿 overall=poor 案件写回顾楼 (gczx 身份)")
    p_wp.add_argument("--dry-run", action="store_true", help="打印 HTML 但不写")
    p_wp.add_argument("--limit", type=int, default=100)
    p_wp.set_defaults(func=_cmd_write_poor)

    p_bf = sub.add_parser(
        "backfill-all",
        help="补跑所有已关闭+form有内容+未评过的 AI 楼案件",
    )
    p_bf.add_argument("--limit", type=int, default=1000, help="上限保护，默认 1000")
    p_bf.add_argument(
        "--write-note", action="store_true", help="poor 立即写楼(gczx 身份)"
    )
    p_bf.set_defaults(func=_cmd_backfill_all)

    p_wk = sub.add_parser("weekly", help="出上周周报 → /app/report/YYYYMMDD.html（容器内挂载点）+ 钉钉推送")
    p_wk.add_argument(
        "--out-dir", type=str,
        default="/app/report",
        help="报告落地目录。容器内挂载点是 /app/report（映射宿主机 /egova/MediaRoot/redmine）；"
             "勿写宿主机绝对路径，否则容器内会落进可写层幻影目录导致 nginx 404",
    )
    p_wk.add_argument(
        "--url-prefix", type=str,
        default="https://demo.egova.com.cn/redmine-assist/report",
        help="公网 URL 前缀",
    )
    p_wk.add_argument("--no-push", action="store_true", help="不推钉钉")
    p_wk.add_argument("--days", type=int, default=7, help="回顾多少天窗口（默认 7）")
    p_wk.set_defaults(func=_cmd_weekly)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
