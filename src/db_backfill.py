"""走 MySQL 直连的全量 backfill。

流程：
  1. 记录 backfill 开始时刻 -> 之后作为 sync 的 last_sync_at 起点（保守，可能重复同步少量）
  2. 流式拉所有 issues（按 id 翻页）
  3. 每攒够 page_size（默认 500）：批量拉这批 issue 的 journals → 抽 resolution
  4. 清洗文本 → bge-m3 批量 embed → upsert 进 sqlite-vec + faiss
  5. 完成后写 sync_state.json {last_sync_at: <开始时刻>}

参数：
  --projects 3355,3356        限定 PoC 项目
  --rebuild                   即使已存在也重新 embed
  --limit N                   总条数上限（调试）
  --skip-existing             遇到已存在的跳过（默认行为，无须显式传）
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

from .config import cfg, project_root
from .db_client import RedmineDB
from .embedder import Embedder
from .text_cleaner import (
    build_form_records_text,
    build_issue_text,
    build_resolution_text,
    find_resolution_notes,
)
from .vector_store import VectorStore


def _embed_text_hash(embed_text: str) -> str:
    return hashlib.sha1(embed_text.encode("utf-8")).hexdigest()


def _build_journals_for_cleaner(rows: list[dict], status_map: dict) -> list[dict]:
    """把 db_client 拉的 journal dict 转成 text_cleaner.find_resolution_notes 期望的格式。

    屏蔽 AI 写回账号(egova-gczx)的楼——避免 AI 自己写的内容回流污染 resolution 抽取。
    """
    from .config import cfg as _cfg
    ai_user_id = (_cfg().get("redmine") or {}).get("ai_user_id")
    out = []
    for r in rows:
        if ai_user_id and r.get("user_id") == ai_user_id:
            continue
        j = {"notes": r["notes"] or ""}
        if r.get("status_changed_to_id"):
            j["details"] = [
                {
                    "property": "attr",
                    "name": "status_id",
                    "new_value": str(r["status_changed_to_id"]),
                }
            ]
        else:
            j["details"] = []
        out.append(j)
    return out


def _process_chunk(
    chunk: list[dict],
    db: RedmineDB,
    status_map: dict[int, dict],
    vs: VectorStore,
    em: Embedder,
    skip_existing: bool,
) -> int:
    """处理一批 issue。返回这批新入库的条数。"""
    # 过滤已存在（如果 skip_existing）
    if skip_existing:
        chunk = [it for it in chunk if not vs.has(it["id"])]
    if not chunk:
        return 0

    # 一次性拉这批 issue 的 journals + 研发/测试表单
    ids = [it["id"] for it in chunk]
    journals_by_id = db.fetch_journals_bulk(ids)
    forms_by_id = db.fetch_form_records_bulk(ids)

    texts: list[str] = []
    metas: list[dict] = []
    for it in chunk:
        iid = it["id"]
        subject = it.get("subject") or ""
        desc = it.get("description") or ""
        jrows = journals_by_id.get(iid, [])
        journal_resolution = find_resolution_notes(
            _build_journals_for_cleaner(jrows, status_map)
        )
        form_text = build_form_records_text(forms_by_id.get(iid, []))
        resolution = build_resolution_text(journal_resolution, form_text)
        embed_text = build_issue_text(subject, desc) + (
            "\n[解决方案] " + resolution if resolution else ""
        )
        texts.append(embed_text)
        status_id = it.get("status_id")
        status_name = status_map.get(status_id, {}).get("name", "")
        metas.append(
            {
                "issue_id": iid,
                "subject": subject,
                "status": status_name,
                "closed_on": (
                    it["closed_on"].strftime("%Y-%m-%dT%H:%M:%S")
                    if it.get("closed_on")
                    else None
                ),
                "resolution": resolution,
                "updated_on": (
                    it["updated_on"].strftime("%Y-%m-%dT%H:%M:%S")
                    if it.get("updated_on")
                    else None
                ),
                "embed_text_hash": _embed_text_hash(embed_text),
            }
        )

    # 批量 embed
    embs = em.embed(texts)
    vs.upsert_many(
        [(m["issue_id"], emb, m) for emb, m in zip(embs, metas)]
    )
    return len(chunk)


def run(
    projects: list[int] | None = None,
    rebuild: bool = False,
    limit: int | None = None,
) -> None:
    c = cfg()
    db = RedmineDB()
    em = Embedder()
    vs = VectorStore()
    status_map = db.get_status_map()

    # 记录开始时刻作为 sync 起点
    start_ts = dt.datetime.now()
    print(f"[backfill] start at {start_ts.isoformat()}, projects={projects or 'ALL'}")

    total_in_redmine = db.count_issues()
    print(f"[backfill] redmine total issues: {total_in_redmine:,}")

    skip_existing = not rebuild
    processed = 0
    inserted = 0
    chunk: list[dict] = []
    PAGE = int(c["redmine_db"].get("backfill_page_size", 500))
    t0 = time.time()

    for it in db.iter_issues_for_backfill(project_ids=projects):
        chunk.append(it)
        processed += 1
        if len(chunk) >= PAGE:
            n = _process_chunk(chunk, db, status_map, vs, em, skip_existing)
            inserted += n
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed > 0 else 0
            eta_min = (total_in_redmine - processed) / rate / 60 if rate > 0 else -1
            print(
                f"[backfill] seen={processed:,}/{total_in_redmine:,} "
                f"inserted={inserted:,} elapsed={elapsed:.0f}s "
                f"rate={rate:.1f}/s eta={eta_min:.1f}min",
                flush=True,
            )
            chunk = []
            if limit and processed >= limit:
                break
    if chunk:
        n = _process_chunk(chunk, db, status_map, vs, em, skip_existing)
        inserted += n

    # 写 sync_state
    state_path = project_root() / c["storage"]["sync_state"]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "last_sync_at": start_ts.strftime("%Y-%m-%d %H:%M:%S"),
        "backfill_completed_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "backfill_processed": processed,
        "backfill_inserted": inserted,
    }
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    elapsed = time.time() - t0
    print(
        f"\n[backfill] DONE  processed={processed:,}  inserted={inserted:,}  "
        f"elapsed={elapsed/60:.1f}min  sync_state -> {state_path}"
    )


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    p = argparse.ArgumentParser()
    p.add_argument(
        "--projects",
        type=str,
        default=None,
        help="只跑这些 project_id，逗号分隔。例：--projects 3355",
    )
    p.add_argument("--rebuild", action="store_true", help="重新 embed 已存在的 issue")
    p.add_argument("--limit", type=int, default=None, help="总条数上限（调试）")
    args = p.parse_args()
    projects = [int(x) for x in args.projects.split(",")] if args.projects else None
    run(projects=projects, rebuild=args.rebuild, limit=args.limit)
