"""MCP server（streamable-http JSON-RPC 2.0）—— 给钉钉 AI 助理 / deap 调用。

实现最小 MCP 集（spec 2024-11-05）：
  - initialize           返回服务能力
  - tools/list           返回可用工具列表
  - tools/call           执行 precheck

只支持 Content-Type: application/json 单响应模式（precheck 调用 ~60s 可同步返回）。
"""
from __future__ import annotations

import logging

log = logging.getLogger("mcp")

MCP_PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "redmine-precheck", "version": "1.0.0"}

_TOOL_PRECHECK = {
    "name": "precheck",
    "description": (
        "对接前置避坑：用户描述了一个对接业务（数据/视频/系统集成）时调用。"
        "从公司 17 万历史 Redmine 工单 + 4500 篇钉钉知识库文档中检索同类案件，"
        "聚类输出 top N 高频问题模式（每类含出现次数、典型案件链接、文档参考、避坑建议）。"
        "适用场景：业务人员准备做新对接前，想知道历史上这类对接踩过什么坑。"
        "输入越具体（含产品名/协议/三方系统）召回质量越好。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": (
                    "对接业务描述，必须含产品/协议/三方系统等关键词。"
                    "例如：'做车载GPS轨迹对接，对方808协议走TCP，政务网+互联网双网环境'。"
                    "禁止过短或过泛（如'做个对接'），应反问用户补充细节。"
                ),
            }
        },
        "required": ["description"],
    },
}


def _ok(rpc_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _err(rpc_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def handle_mcp(body: dict) -> dict | None:
    """JSON-RPC 2.0 dispatcher。返回 dict（要响应）或 None（notification 无响应）。"""
    rpc_id = body.get("id")
    method = body.get("method", "")
    params = body.get("params") or {}
    log.info("mcp.recv method=%s id=%s", method, rpc_id)

    # 通知类（无 id，无需响应）
    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _ok(
            rpc_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "serverInfo": SERVER_INFO,
                "capabilities": {"tools": {"listChanged": False}},
            },
        )

    if method == "tools/list":
        return _ok(rpc_id, {"tools": [_TOOL_PRECHECK]})

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}
        if tool_name != "precheck":
            return _err(rpc_id, -32601, f"unknown tool: {tool_name}")
        description = (args.get("description") or "").strip()
        if not description:
            return _ok(
                rpc_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "请告诉我具体的对接业务，例如：\n- 对接什么数据（GPS轨迹/视频/业务表）\n- 用什么协议（808/HTTP/库表）\n- 三方系统是谁",
                        }
                    ],
                    "isError": True,
                },
            )
        if len(description) > 4000:
            return _err(rpc_id, -32602, "description too long (max 4000)")
        try:
            # 延迟 import 避免循环
            from .precheck import run_precheck
            res = run_precheck(description)
            md = res.get("markdown") or ""
            stats = res.get("stats") or {}
            footer = (
                f"\n\n*[本次召回: {stats.get('n_issues', 0)} 工单 / "
                f"{stats.get('n_docs', 0)} 文档片段 / "
                f"{stats.get('n_clusters', 0)} 类问题模式 / "
                f"{stats.get('elapsed_ms', 0)/1000:.1f}s]*"
            )
            return _ok(
                rpc_id,
                {
                    "content": [{"type": "text", "text": md + footer}],
                    "isError": False,
                },
            )
        except Exception as e:
            log.exception("mcp tools/call precheck failed")
            return _err(rpc_id, -32603, f"internal error: {e}")

    return _err(rpc_id, -32601, f"method not found: {method}")
