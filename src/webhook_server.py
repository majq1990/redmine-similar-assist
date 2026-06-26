"""Flask webhook 端点：接 redmine_webhooks 插件。

Redmine webhook 插件默认 body 形如：
  { "payload": { "action": "opened"|"updated", "issue": { "id": ..., ... } } }

我们这里只关心 action == "opened"，且 issue.project.id ∈ target_projects。
"""
from __future__ import annotations

import logging
import threading

from flask import Flask, Response, jsonify, request

from .config import cfg, is_project_targeted
from .pipeline import ingest_new_issue
from . import sync as sync_module
from .vector_store import get_vector_store, get_doc_store, get_chunk_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webhook")

app = Flask(__name__)

# 预热放到后台线程：消除冷启动空窗。
# 16.8w 向量 sqlite-vec → faiss 加载 ~7min，期间 Flask 端口立即可用（health 通），
# 但 KNN 尚未就绪——sync/webhook 在未就绪时直接跳过（不推进 state，恢复后自动补扫）。
# 只有这个后台线程会创建单例，请求路径用 _ready 守门，避免并发重复加载（单例非线程安全）。
_ready = threading.Event()


def _warmup() -> None:
    log.info("warming up VectorStore + DocStore + ChunkStore in background...")
    try:
        get_vector_store()
        get_doc_store()
        get_chunk_store()  # B1: chunk 粒度索引也预热
        _ready.set()
        log.info("VectorStore + DocStore + ChunkStore ready")
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


@app.post("/precheck")
def precheck_endpoint():
    """对接前置避坑：输入业务描述，返回 top N 高频问题模式。

    给钉钉智能助理 Action 调；也可 curl 调试。
    Body: {"description": "...", "top_issues": 50, "top_docs": 15}
    Response: {"markdown": "...", "items": [...], "stats": {...}}
    """
    if not _ready.is_set():
        return jsonify({"error": "warming_up", "ready": False}), 503
    body = request.get_json(silent=True) or {}
    description = (body.get("description") or "").strip()
    if not description:
        return jsonify({"error": "description required"}), 400
    if len(description) > 4000:
        return jsonify({"error": "description too long (max 4000 chars)"}), 400
    top_issues = int(body.get("top_issues") or 30)
    top_docs = int(body.get("top_docs") or 10)
    try:
        # 延迟 import 避免循环（precheck 拉 pipeline 依赖链）
        from .precheck import run_precheck
        res = run_precheck(
            description, top_issues=top_issues, top_docs=top_docs
        )
        log.info(
            "precheck -> n_issues=%s n_docs=%s n_clusters=%s ms=%s",
            res["stats"]["n_issues"],
            res["stats"]["n_docs"],
            res["stats"]["n_clusters"],
            res["stats"]["elapsed_ms"],
        )
        return jsonify(res), 200
    except Exception as e:
        log.exception("precheck failed")
        return jsonify({"error": str(e)}), 500


def _check_mcp_auth(req) -> bool:
    """MCP 端点 Bearer 鉴权。token 配在 cfg.precheck.token；nginx 也校验同 token 兜底。"""
    expected = ((cfg().get("precheck") or {}).get("token") or "").strip()
    if not expected:
        return True  # 未配 token = 不校验（依赖 nginx 层）
    auth = req.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return auth[7:].strip() == expected


@app.route("/mcp", methods=["DELETE"])
def mcp_delete_endpoint():
    """spec 2025-03-26 session termination: client 发 DELETE 终止 session。
    无状态 server 无需真终止，返回 204 让 Reactor 收到 onComplete。"""
    return Response("", status=204, mimetype="application/json",
                    headers={"mcp-session-id": _MCP_SESSION_ID})


@app.route("/mcp", methods=["OPTIONS"])
def mcp_options_endpoint():
    """CORS / preflight 探测。钉钉 deap MCP 客户端会发 OPTIONS 探测，
    必须返回 200 + 正确 Content-Type（json），否则钉钉报 'Unknown media type text/html'。"""
    return jsonify({"ok": True}), 200, {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Max-Age": "86400",
    }


@app.get("/mcp")
def mcp_sse_endpoint():
    """MCP streamable-http GET 端点：钉钉 client initialize 后会 GET 此地址
    建立"服务端→客户端"的 SSE 流（spec 可选）。

    我们当前不需要主动推送，但钉钉客户端不接受 405 HTML，所以返回长保活 SSE：
    每 25s 发一个 SSE 注释行 ': ping\\n\\n' 维持连接，让钉钉客户端持续等待。
    """
    if not _check_mcp_auth(request):
        return Response("unauthorized", status=401, mimetype="text/plain")

    # 钉钉 Reactor 客户端在 SSE 流上等 terminal signal（onComplete），
    # 我们没有 server→client 主动推送需求，所以发个 SSE 注释后立即关流，
    # client 收到 EOF=onComplete，不会再卡 8 秒超时。
    def event_stream():
        yield ": mcp ready\n\n"
        # 不 sleep、不 loop，generator 结束即 close 连接

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # 让 nginx 不要 buffer SSE
            "mcp-session-id": _MCP_SESSION_ID,
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "mcp-session-id",
        },
    )


import uuid as _uuid

# 进程级固定 session id（无状态 server 无需真做会话管理，
# 只为满足 streamable-http spec 2025-03-26 client 期待响应含此 header）
_MCP_SESSION_ID = _uuid.uuid4().hex


def _sse_response(jsonrpc_msg: dict, status: int = 200):
    """把 JSON-RPC response 包成 SSE 单 message 返回。

    必须用 generator → chunked transfer encoding（参考 mcp 服务器实现），
    否则 client (钉钉 Reactor) 看到 Content-Length 不知道流是否结束，等 8s 超时。
    """
    import json as _json
    body_str = f"event: message\ndata: {_json.dumps(jsonrpc_msg, ensure_ascii=False)}\n\n"

    def gen():
        yield body_str

    return Response(
        gen(),  # generator 触发 chunked transfer，无 Content-Length
        status=status,
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "mcp-session-id": _MCP_SESSION_ID,
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "mcp-session-id",
        },
    )


@app.post("/mcp")
def mcp_endpoint():
    """MCP streamable-http 端点：钉钉 AI 助理 / deap 通过 JSON-RPC 2.0 调用。

    根据 Accept header 决定返回格式：
      - 含 text/event-stream → SSE
      - 否则 → application/json（spec 允许）
    钉钉 deap 客户端必须走 SSE。
    """
    # 先读 body 以拿到请求 id（JSON-RPC 协议要求响应 id 与请求 id 匹配）
    body = request.get_json(silent=True) or {}
    rpc_id = body.get("id")

    if not _check_mcp_auth(request):
        msg = {"jsonrpc": "2.0", "id": rpc_id,
               "error": {"code": -32001, "message": "unauthorized"}}
        return _sse_response(msg, status=401)
    if not _ready.is_set():
        msg = {"jsonrpc": "2.0", "id": rpc_id,
               "error": {"code": -32000, "message": "warming_up, retry in 5min"}}
        return _sse_response(msg, status=503)
    from .mcp_server import handle_mcp
    try:
        res = handle_mcp(body)
    except Exception as e:
        log.exception("mcp dispatch failed")
        return _sse_response({"jsonrpc": "2.0", "id": body.get("id"),
                              "error": {"code": -32603, "message": str(e)}},
                             status=500)
    if res is None:
        # notification 无响应（HTTP 200 空 SSE，钉钉接受空流）
        return Response("", status=200, mimetype="text/event-stream")
    return _sse_response(res)


if __name__ == "__main__":
    c = cfg()["webhook"]
    app.run(host=c["host"], port=c["port"], debug=False)
