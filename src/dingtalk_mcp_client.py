"""钉钉知识库 MCP 客户端（streamable-http JSON-RPC）。

URL 来源优先级：
  1. data/dingtalk_mcp_url.txt（本机每天 scp 推送）
  2. config.yaml dingtalk_mcp.fallback_url
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

from .config import cfg, project_root


def load_mcp_url() -> str:
    c = cfg().get("dingtalk_mcp") or {}
    url_file = project_root() / (c.get("url_file") or "data/dingtalk_mcp_url.txt")
    if url_file.exists():
        url = url_file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        if url:
            return url
    return c.get("fallback_url", "")


class DingtalkMcpClient:
    def __init__(self, url: str | None = None) -> None:
        self.url = url or load_mcp_url()
        if not self.url:
            raise RuntimeError("no dingtalk MCP url available")
        self._id = 0

    def _call(self, method: str, params: dict | None = None, timeout: int = 30) -> dict:
        self._id += 1
        data = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        ).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        last_err = None
        for attempt in range(3):
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                return json.loads(resp.read().decode("utf-8", errors="replace"))
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = e
                time.sleep(1 + attempt * 2)
        raise RuntimeError(f"MCP call {method} failed after 3 retries: {last_err}")

    def call_tool(self, name: str, args: dict) -> Any:
        """调用 MCP tool，自动解构 result/content/structuredContent。"""
        resp = self._call("tools/call", {"name": name, "arguments": args})
        if "error" in resp:
            raise RuntimeError(f"MCP error: {resp['error']}")
        r = resp.get("result", {})
        # structuredContent 直接是结果对象
        sc = r.get("structuredContent")
        if sc is not None:
            return sc
        # content[0].text 是 JSON 字符串
        content = r.get("content") or []
        if content and content[0].get("type") == "text":
            try:
                return json.loads(content[0]["text"])
            except json.JSONDecodeError:
                return content[0]["text"]
        return r

    def list_nodes(self, workspace_id: str | None = None, folder_id: str | None = None) -> Iterator[dict]:
        """翻页列节点（直接子节点）。每页 50。"""
        next_token = None
        while True:
            args: dict = {"pageSize": 50}
            if folder_id:
                args["folderId"] = folder_id
            elif workspace_id:
                args["workspaceId"] = workspace_id
            if next_token:
                args["pageToken"] = next_token
            data = self.call_tool("list_nodes", args)
            for node in data.get("nodes") or []:
                yield node
            next_token = data.get("nextPageToken") or data.get("next_page_token")
            if not next_token:
                break

    def walk_documents(self, workspace_id: str) -> Iterator[dict]:
        """递归遍历整个 workspace，只返回叶子文档（contentType=ALIDOC 且 extension=adoc）。"""
        stack: list[tuple[str | None, str | None]] = [(workspace_id, None)]  # (ws_id, folder_id)
        seen_folders: set[str] = set()
        while stack:
            ws_id, folder_id = stack.pop()
            try:
                for node in self.list_nodes(workspace_id=ws_id, folder_id=folder_id):
                    nid = node.get("nodeId")
                    ntype = node.get("nodeType")
                    if ntype == "folder" and node.get("hasChildren") and nid and nid not in seen_folders:
                        seen_folders.add(nid)
                        stack.append((None, nid))
                    elif ntype == "file" and node.get("contentType") == "ALIDOC" and node.get("extension") == "adoc":
                        yield node
            except Exception as e:
                import sys as _sys
                _sys.stderr.write(f"[mcp] walk error at folder={folder_id or ws_id}: {e}\n")

    def get_document_markdown(self, node_id: str) -> str:
        """拉文档内容（markdown）。"""
        data = self.call_tool("get_document_content", {"nodeId": node_id, "format": "markdown"})
        # 不同版本可能返回 string 或 {content: ...} 或 {markdown: ...}
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for k in ("markdown", "content", "text", "data"):
                v = data.get(k)
                if isinstance(v, str) and v:
                    return v
        return ""

    def search_documents(
        self, keyword: str, workspace_ids: list[str] | None = None, page_size: int = 10
    ) -> list[dict]:
        """钉钉服务端关键词搜索（备用，作为 KNN 不可达时的兜底）。"""
        args: dict = {"keyword": keyword, "pageSize": page_size}
        if workspace_ids:
            args["workspaceIds"] = workspace_ids
        data = self.call_tool("search_documents", args)
        return data.get("documents") or []

    def health(self) -> dict:
        """探活，返回 tool 数量。"""
        try:
            r = self._call("tools/list")
            return {"ok": True, "tools": len(r.get("result", {}).get("tools", []))}
        except Exception as e:
            return {"ok": False, "err": str(e)[:200]}
