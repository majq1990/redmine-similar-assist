"""钉钉知识库每日增量同步。

cron 每天 02:00 触发：
  1. 走 walk_documents 列所有当前 ALIDOC 文档
  2. 对每个 node：
     - 不在库 → 新增
     - 在库但 update_time 变 → 重 embed
     - 在库且 update_time 一致 → 跳过
  3. 库里有但当前不在列表 → 标记 stale（不删，让人工检查）

跟 dingtalk_backfill.py 共用大部分逻辑，复用 run(rebuild=False)。
"""
from __future__ import annotations

import datetime as dt
import json
import sys

from .config import cfg, project_root
from .dingtalk_backfill import run as backfill_run


def run_once() -> dict:
    """每日增量。等价于 backfill --rebuild=False，但只跑增量并写 sync_state。"""
    res = backfill_run(rebuild=False, limit=None)

    state_path = project_root() / cfg()["storage"]["sync_state"]
    state = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    state["last_dingtalk_sync_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["last_dingtalk_sync_inserted"] = res.get("inserted", 0)
    state["last_dingtalk_sync_skipped"] = res.get("skipped", 0)
    state["last_dingtalk_sync_failed"] = res.get("failed", 0)
    state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return res


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(json.dumps(run_once(), ensure_ascii=False, indent=2))
