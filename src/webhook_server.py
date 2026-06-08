"""Flask webhook 端点：接 redmine_webhooks 插件。

Redmine webhook 插件默认 body 形如：
  { "payload": { "action": "opened"|"updated", "issue": { "id": ..., ... } } }

我们这里只关心 action == "opened"，且 issue.project.id ∈ target_projects。
"""
from __future__ import annotations

import logging
import threading

from flask import Flask, jsonify, request

from .config import cfg, is_project_targeted
from .pipeline import ingest_new_issue
from . import sync as sync_module
from .vector_store import get_vector_store, get_doc_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webhook")

app = Flask(__name__)

# 预热放到后台线程：消除冷启动空窗。
# 16.8w 向量 sqlite-vec → faiss 加载 ~7min，期间 Flask 端口立即可用（health 通），
# 但 KNN 尚未就绪——sync/webhook 在未就绪时直接跳过（不推进 state，恢复后自动补扫）。
# 只有这个后台线程会创建单例，请求路径用 _ready 守门，避免并发重复加载（单例非线程安全）。
_ready = threading.Event()


def _warmup() -> None:
    log.info("warming up VectorStore + DocStore in background (faiss load ~7min)...")
    try:
        get_vector_store()
        get_doc_store()
        _ready.set()
        log.info("VectorStore + DocStore ready")
    except Exception:
        log.exception("warmup failed")


threading.Thread(target=_warmup, daemon=True).start()


def _check_secret(req) -> bool:
    c = cfg()["webhook"]
    expected = (c.get("shared_secret") or "").strip()
    # 标记 "DISABLED" 或空 = 不校验（适合 plugin 不支持自定义 header 的场景，靠 nginx IP 白名单兜底）
    if not expected or expected.upper() == "DISABLED":
        return True
    return req.headers.get(c.get("header_name", "X-Webhook-Secret")) == expected


def _process_async(issue_id: int) -> None:
    if not _ready.is_set():
        # 向量未就绪，跳过；DB 轮询 sync 就绪后会补处理该新建工单
        log.info("skip webhook ingest %s: warming up", issue_id)
        return
    try:
        res = ingest_new_issue(issue_id)
        log.info("processed %s -> %s", issue_id, res.get("wrote"))
    except Exception:
        log.exception("ingest failed for issue %s", issue_id)


@app.post("/redmine-webhook")
def hook():
    if not _check_secret(request):
        return jsonify({"error": "bad secret"}), 401
    body = request.get_json(silent=True) or {}
    payload = body.get("payload") or body  # 兼容两种插件格式
    action = payload.get("action") or body.get("action") or "opened"
    issue = payload.get("issue") or body.get("issue") or {}
    issue_id = issue.get("id")
    proj_id = (issue.get("project") or {}).get("id")
    if not issue_id:
        return jsonify({"ignored": "no issue id"}), 200
    if proj_id and not is_project_targeted(proj_id):
        return jsonify({"ignored": "not in whitelist", "project_id": proj_id}), 200
    if action not in ("opened", "created", "new"):
        # 业务规则：只在新建时触发一次，后续 updated/closed 一律不再处理
        return jsonify({"ignored": "action=" + str(action)}), 200
    tracker_id = (issue.get("tracker") or {}).get("id")
    tracker_whitelist = cfg().get("tracker_whitelist") or []
    if tracker_whitelist and tracker_id not in tracker_whitelist:
        return jsonify({"ignored": "tracker not in whitelist", "tracker_id": tracker_id}), 200

    # 异步处理，避免 Redmine 等待
    threading.Thread(target=_process_async, args=(issue_id,), daemon=True).start()
    return jsonify({"accepted": True, "issue_id": issue_id}), 202


@app.get("/health")
def health():
    # 始终 200（容器/反代视为存活）；ready 标记向量索引是否加载完
    return jsonify({"ok": True, "ready": _ready.is_set()})


@app.post("/sync/incremental")
def sync_incremental():
    """由 cron 每 N 分钟戳一下，跑增量同步。

    同步逻辑跑在 webhook 进程内，跟 faiss 内存索引同进程，零跨进程问题。
    带文件锁防 cron 跑慢叠加。

    /sync/incremental 独立 secret（与 /redmine-webhook 不同：本机 cron 一定带得了）
    """
    c = cfg()["webhook"]
    expected = (c.get("sync_secret") or "").strip()
    if expected and request.headers.get(c.get("header_name", "X-Webhook-Secret")) != expected:
        return jsonify({"error": "bad sync secret"}), 401
    if not _ready.is_set():
        # 向量索引还在后台加载，跳过本次（不推进 last_sync_at，就绪后下次自动补扫）
        return jsonify({"skipped": "warming_up", "ready": False}), 200
    body = request.get_json(silent=True) or {}
    max_items = body.get("max")
    try:
        res = sync_module.run_once(max_items=max_items)
        log.info("sync.run_once -> %s", res)
        return jsonify(res), 200
    except RuntimeError as e:
        # 已有锁
        return jsonify({"error": str(e)}), 423  # Locked
    except Exception as e:
        log.exception("sync failed")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    c = cfg()["webhook"]
    app.run(host=c["host"], port=c["port"], debug=False)
