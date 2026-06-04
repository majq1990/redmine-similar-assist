"""增量同步：每 N 分钟扫一次 MySQL updated_on 之后变更的 issue。

设计：
  - 读 last_sync_at（data/sync_state.json）
  - SELECT * FROM issues WHERE updated_on > last_sync_at LIMIT sync_batch_size
  - 对每条：
      新 embed_text = build(subject, description) + resolution
      hash 跟库里对比
        相同 → 便宜路径：UPDATE issues_meta (status/closed_on/...)
        不同 → 贵路径：bge-m3 → faiss remove_ids + add_with_ids
  - 把 last_sync_at = MAX(updated_on of processed)
  - 文件锁防并发
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .config import (
    cfg,
    invalidate_target_project_cache,
    is_project_targeted,
    project_root,
)
from .db_client import RedmineDB
from .embedder import Embedder
from .notify import post_dingtalk
from .text_cleaner import build_issue_text, detect_r_and_d_communication, find_resolution_notes
from .vector_store import VectorStore, get_vector_store


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _state_path() -> Path:
    p = project_root() / cfg()["storage"]["sync_state"]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    _state_path().write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _parse_ts(s: str) -> dt.datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"cannot parse timestamp: {s!r}")


def _build_journals_for_cleaner(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
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


class _FileLock:
    """文件锁，跨进程互斥。带 stale lock detection（pid 不存活时接管）。"""

    def __init__(self, path: Path, stale_after_sec: int = 3600) -> None:
        self.path = path
        self._held = False
        self._stale_after = stale_after_sec

    def _pid_alive(self, pid: int) -> bool:
        # 容器内 pid 1 是 webhook 主进程；其它 pid 可能是 sync 实际 worker
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    def _try_steal_stale(self) -> bool:
        """检查现有锁是否 stale（拥有者死了或文件太老），是的话删除让出。"""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            return True
        try:
            old_pid = int(self.path.read_text().strip())
        except (ValueError, OSError):
            old_pid = -1
        age = time.time() - st.st_mtime
        # pid 1 是容器主进程，永远活着，但锁是 worker thread 里持有
        # 若 lock 文件超过 stale_after_sec 且写它的进程是已不存在或就是 pid 1（已被 restart）
        # 都视为 stale
        if age > self._stale_after or not self._pid_alive(old_pid) or old_pid == 1:
            try:
                self.path.unlink()
                return True
            except FileNotFoundError:
                return True
        return False

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
            )
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self._held = True
            return True
        except FileExistsError:
            # 尝试接管 stale lock 一次
            if self._try_steal_stale():
                try:
                    fd = os.open(
                        str(self.path),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o644,
                    )
                    os.write(fd, str(os.getpid()).encode())
                    os.close(fd)
                    self._held = True
                    return True
                except FileExistsError:
                    return False
            return False

    def release(self) -> None:
        if self._held:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            self._held = False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"sync already running (lock {self.path} exists)")
        return self

    def __exit__(self, *a):
        self.release()


def run_once(max_items: int | None = None) -> dict:
    """增量同步一次。返回统计字典。"""
    c = cfg()
    state = _load_state()
    last_str = state.get("last_sync_at")
    if not last_str:
        return {
            "error": "no last_sync_at in sync_state.json; run db_backfill first",
        }
    since = _parse_ts(last_str)

    lock_path = project_root() / c["sync"].get("lock_file", "data/sync.lock")
    with _FileLock(lock_path):
        t0 = time.time()
        # 每次 sync 强制刷新项目白名单缓存，确保「项目支持」下新增的子项目立即生效
        invalidate_target_project_cache()
        db = RedmineDB()
        em = Embedder()
        vs = get_vector_store()
        status_map = db.get_status_map()

        rows = db.iter_issues_updated_since(
            since, limit=max_items or c["redmine_db"].get("sync_batch_size", 5000)
        )
        if not rows:
            return {"processed": 0, "since": last_str, "elapsed_ms": int((time.time() - t0) * 1000)}

        ids = [r["id"] for r in rows]
        journals_by_id = db.fetch_journals_bulk(ids)

        cheap = 0
        expensive_texts: list[str] = []
        expensive_metas: list[dict] = []
        expensive_ids: list[int] = []
        meta_only_updates: list[tuple[int, dict]] = []
        new_inserts: list[tuple[str, dict, int]] = []  # 兼容（其实并入 expensive）
        max_updated_on: dt.datetime = since

        for it in rows:
            iid = it["id"]
            subject = it.get("subject") or ""
            desc = it.get("description") or ""
            jrows = journals_by_id.get(iid, [])
            resolution = find_resolution_notes(_build_journals_for_cleaner(jrows))
            embed_text = build_issue_text(subject, desc) + (
                "\n[解决方案] " + resolution if resolution else ""
            )
            new_hash = _hash(embed_text)
            status_id = it.get("status_id")
            status_name = status_map.get(status_id, {}).get("name", "")
            new_meta: dict[str, Any] = {
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
                "embed_text_hash": new_hash,
            }
            old = vs.get_meta(iid)
            if old and old.get("embed_text_hash") == new_hash:
                # 便宜路径
                vs.update_meta_only(iid, new_meta)
                cheap += 1
            else:
                # 贵路径：重 embed
                expensive_texts.append(embed_text)
                expensive_metas.append(new_meta)
                expensive_ids.append(iid)

            if it.get("updated_on") and it["updated_on"] > max_updated_on:
                max_updated_on = it["updated_on"]

        # 批量 embed
        if expensive_texts:
            embs = em.embed(expensive_texts)
            for emb, m in zip(embs, expensive_metas):
                vs.upsert(m["issue_id"], emb, m)

        # === 新建工单检测 + AI 写回 ===
        # 判断条件：created_on > since 且通过 project/tracker 白名单
        # 调用 ingest_new_issue：内部 has assist_log 幂等 + project/tracker 二次校验
        tracker_whitelist = set(cfg().get("tracker_whitelist") or [])
        ai_triggered = 0
        ai_wrote = 0
        ai_errors = 0
        triage_skipped = 0

        # 分诊：预加载技术支持部成员列表
        triage_cfg = cfg().get("triage") or {}
        triage_enabled = triage_cfg.get("enabled", False)
        triage_status_id = triage_cfg.get("status_id", 19)
        triage_group_id = triage_cfg.get("group_id", 1137)
        notify_cfg = cfg().get("notify") or {}
        triage_webhook = triage_cfg.get("notify_webhook") or notify_cfg.get("dingtalk_webhook", "")
        triage_secret = triage_cfg.get("notify_secret") or notify_cfg.get("dingtalk_secret", "")
        group_member_ids: set[int] = set()
        if triage_enabled and triage_webhook:
            group_member_ids = db.get_group_member_ids(triage_group_id)

        # 延迟 import 避免循环
        from .pipeline import ingest_new_issue
        for it in rows:
            if not it.get("created_on") or it["created_on"] <= since:
                continue  # 不是新建（旧 issue 被 update）
            if not is_project_targeted(it.get("project_id")):
                continue
            if tracker_whitelist and it.get("tracker_id") not in tracker_whitelist:
                continue

            # === 分诊：检测已与研发沟通的案件，跳过 AI 并推送钉钉 ===
            if (
                triage_enabled
                and group_member_ids
                and it.get("status_id") == triage_status_id
                and it.get("assigned_to_id") in group_member_ids
            ):
                iid = it["id"]
                jrows = journals_by_id.get(iid, [])
                has_signal, keywords = detect_r_and_d_communication(
                    _build_journals_for_cleaner(jrows)
                )
                if has_signal:
                    triage_skipped += 1
                    # 旁路通知钉钉：issue 摘要 + 匹配关键词（不跳过 AI）
                    subj = it.get("subject") or ""
                    kw_str = "、".join(keywords[:5])
                    md = (
                        f"### 案件已与研发沟通\n\n"
                        f"- **案件号**: #{iid}\n"
                        f"- **标题**: {subj}\n"
                        f"- **匹配关键词**: {kw_str}\n"
                        f"- **状态**: 支持部受理\n\n"
                        f"> 系统检测到该案件 journal 中已有研发沟通记录，"
                        f"AI 相似案卷检索仍会照常执行。"
                    )
                    try:
                        post_dingtalk(
                            triage_webhook, triage_secret,
                            f"案件 #{iid} 已与研发沟通", md,
                        )
                    except Exception:
                        import traceback as _tb
                        _tb.print_exc()
                    # 注意：不 continue，继续走 AI pipeline

            try:
                res = ingest_new_issue(it["id"])
                ai_triggered += 1
                if res.get("wrote"):
                    ai_wrote += 1
            except Exception as e:
                ai_errors += 1
                # 不打断后续条目
                import traceback as _tb
                _tb.print_exc()

        # 更新 state（向前推 1 秒避免边界重复扫同一条）
        new_state = dict(state)
        new_state["last_sync_at"] = max_updated_on.strftime("%Y-%m-%d %H:%M:%S")
        new_state["last_run_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_state["last_run_processed"] = len(rows)
        new_state["last_run_cheap"] = cheap
        new_state["last_run_expensive"] = len(expensive_ids)
        new_state["last_run_ai_triggered"] = ai_triggered
        new_state["last_run_ai_wrote"] = ai_wrote
        new_state["last_run_ai_errors"] = ai_errors
        new_state["last_run_triage_skipped"] = triage_skipped
        _save_state(new_state)

        return {
            "processed": len(rows),
            "cheap_meta_only": cheap,
            "expensive_re_embed": len(expensive_ids),
            "ai_triggered": ai_triggered,
            "ai_wrote": ai_wrote,
            "ai_errors": ai_errors,
            "triage_skipped": triage_skipped,
            "since": last_str,
            "new_last_sync_at": new_state["last_sync_at"],
            "elapsed_ms": int((time.time() - t0) * 1000),
        }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--max", type=int, default=None)
    args = p.parse_args()
    res = run_once(max_items=args.max)
    print(json.dumps(res, indent=2, ensure_ascii=False))
