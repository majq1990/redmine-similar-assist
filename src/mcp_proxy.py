"""MCP 代理 server —— 基于官方 MCP Python SDK (FastMCP)。

背景：手写 Flask JSON-RPC 模拟 MCP（src/mcp_server.py）被钉钉 deap 的
Java Reactor client 报 "no item or terminal signal within 8000ms" 拒收；
而同机的 jztan/redmine-mcp-server（官方 SDK 实现）钉钉能正常连。
→ 结论：手写模拟缺 spec 细节（session 握手/SSE 语义），改用官方 SDK。

架构（薄代理，无向量库依赖，秒启动）：
  钉钉 deap → nginx /redmine-assist/mcp → 本容器 :8766 (FastMCP streamable-http)
    → tool 内 HTTP 调 http://redmine-assist:8765 /precheck | /query（现有 Flask）

部署：独立容器跑本文件（与 redmine-assist 同 docker 网络 mcpnet）：
  docker run -d --name redmine-mcp-proxy --network mcpnet \
    -p 127.0.0.1:8766:8766 -v /opt/redmine-assist/code:/app:ro -w /app \
    --restart unless-stopped redmine-assist:latest \
    sh -c "pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple 'mcp>=1.0' && python -m src.mcp_proxy"
"""
from __future__ import annotations

import logging
import os

import requests
from mcp.server.fastmcp import FastMCP

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mcp_proxy")

# 后端 Flask 地址：同容器 127.0.0.1，独立容器走 docker 网络别名
BACKEND = os.environ.get("PRECHECK_BACKEND", "http://redmine-assist:8765")

mcp = FastMCP(
    "redmine-assist",
    host="0.0.0.0",
    port=8766,
)


@mcp.tool()
def precheck(description: str) -> str:
    """对接前置避坑：用户描述了一个准备启动的对接业务（数据/视频/系统集成）时调用。
    从公司 17 万历史 Redmine 工单 + 4500 篇钉钉知识库文档中检索同类案件，
    聚类输出 top N 高频问题模式（每类含出现次数、典型案件链接、文档参考、避坑建议）。
    适用场景：业务人员准备做新对接前，想知道历史上这类对接踩过什么坑。
    输入越具体（含产品名/协议/三方系统）召回质量越好，例如
    '做车载GPS轨迹对接，对方808协议走TCP，政务网+互联网双网环境'。
    禁止过短或过泛（如'做个对接'），应先反问用户补充细节。耗时约 45 秒。
    """
    description = (description or "").strip()
    if not description:
        return "请告诉我具体的对接业务，例如：\n- 对接什么数据（GPS轨迹/视频/业务表）\n- 用什么协议（808/HTTP/库表）\n- 三方系统是谁"
    if len(description) > 4000:
        return "业务描述过长（上限 4000 字），请精简后重试。"
    log.info("precheck len=%s", len(description))
    try:
        r = requests.post(
            f"{BACKEND}/precheck",
            json={"description": description},
            timeout=180,
        )
    except requests.RequestException as e:
        log.exception("precheck backend failed")
        return f"检索服务暂时不可用（{e}），请稍后重试。"
    if r.status_code == 503:
        return "检索服务正在加载向量库（约 8 分钟），请稍后重试。"
    if r.status_code != 200:
        return f"检索服务返回异常（HTTP {r.status_code}），请稍后重试或联系维护。"
    data = r.json()
    md = data.get("markdown") or ""
    stats = data.get("stats") or {}
    footer = (
        f"\n\n*[本次召回: {stats.get('n_issues', 0)} 工单 / "
        f"{stats.get('n_docs', 0)} 文档片段 / "
        f"{stats.get('n_clusters', 0)} 类问题模式 / "
        f"{stats.get('elapsed_ms', 0)/1000:.1f}s]*"
    )
    return md + footer


@mcp.tool()
def zhengtong_query(query: str) -> str:
    """政通问答：从公司 17 万历史 Redmine 工单 + 4500 篇钉钉知识库文档中检索方案和经验。
    适用于：查找某类问题的历史解决方案、了解某产品/模块的实施经验、
    查询公司内部技术文档要点。返回综合答案 + 相关工单/文档链接。
    输入越具体召回质量越好，例如'麒舰第三方对接怎么做鉴权'比'对接怎么做'更好。
    禁止过短或过泛（如'怎么做'），应先反问用户补充细节。耗时约 15 秒。
    """
    query = (query or "").strip()
    if not query:
        return "请输入具体问题，例如：\n- 麒舰第三方对接怎么做鉴权\n- 星桥数据接入SQL脚本怎么写\n- 悟空大屏组件数据源配置方法"
    if len(query) > 4000:
        return "问题过长（上限 4000 字），请精简后重试。"
    log.info("zhengtong_query len=%s", len(query))
    try:
        r = requests.post(
            f"{BACKEND}/query",
            json={"query": query},
            timeout=180,
        )
    except requests.RequestException as e:
        log.exception("query backend failed")
        return f"检索服务暂时不可用（{e}），请稍后重试。"
    if r.status_code == 503:
        return "检索服务正在加载向量库（约 8 分钟），请稍后重试。"
    if r.status_code != 200:
        return f"检索服务返回异常（HTTP {r.status_code}），请稍后重试或联系维护。"
    data = r.json()
    md = data.get("markdown") or ""
    stats = data.get("stats") or {}
    footer = f"\n\n*[耗时: {stats.get('elapsed_ms', 0)/1000:.1f}s]*"
    return md + footer


if __name__ == "__main__":
    log.info("starting FastMCP proxy on :8766, backend=%s", BACKEND)
    mcp.run(transport="streamable-http")
