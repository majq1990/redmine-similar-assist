"""验证 max_tokens=12000 + 真实大 prompt 能不能产出完整 content。"""
import sys, os, json
sys.path.insert(0, "/app"); os.chdir("/app")
import requests
from src.config import cfg

c = cfg()["llm"]

# 模拟 30 issues + 10 docs 真实大小（约 9k 字 prompt）
issues_lines = []
for i in range(30):
    issues_lines.append(
        f"#{400000+i} 朝阳GPS轨迹对接案件{i} 出现异常网络中断需要重新连接 | 处理: "
        f"修改防火墙规则开放TCP 端口9000 等待{i}分钟后正常恢复重连成功重启服务恢复"
    )
docs_lines = []
for i in range(10):
    docs_lines.append(
        f"nodeId-{i} | 车载对接Wiki第{i}章 | "
        f"对接前必须先在 tc_vehicle 表中注册车辆 SIM 卡号 否则 808 协议无法建立连接 "
        f"需通过 jdbc 配置数据源 注意坐标系 GCJ-02 转换 心跳超时设置 60 秒"
    )
issues_block = "\n".join(issues_lines)
docs_block = "\n".join(docs_lines)

prompt = f"""你是 Redmine 工单分析助理。用户即将启动一个对接业务，希望提前知道历史上这类对接踩过哪些坑。

【业务描述】
我要做车载GPS轨迹对接，对方使用808协议走TCP推送，已知现场是政务网+互联网双网环境

【历史相似案件（按相似度降序，共 30 条）】
{issues_block}

【钉钉知识库相关文档片段（共 10 条）】
{docs_block}

请按"相同问题模式"对这些案件聚类（粒度要粗，目标 5-8 个典型坑）：
每类输出: title / case_ids / doc_refs / advice
只输出 JSON {{"clusters": [...]}}
"""

print(f"prompt 长度: {len(prompt)} chars")

payload = {
    "model": c["model"],
    "messages": [
        {"role": "system", "content": "You output only JSON, no prose."},
        {"role": "user", "content": prompt},
    ],
    "temperature": 0.1,
    "max_tokens": 12000,
    "response_format": {"type": "json_object"},
}
r = requests.post(
    c["endpoint"],
    headers={"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"},
    json=payload, timeout=180,
)
print(f"HTTP {r.status_code}")
data = r.json()
print("usage:", data.get("usage"))
ch = data["choices"][0]
print(f"finish_reason: {ch.get('finish_reason')}")
msg = ch.get("message", {})
content = msg.get("content") or ""
reasoning = msg.get("reasoning_content") or ""
print(f"content len: {len(content)}")
print(f"reasoning_content len: {len(reasoning)}")
print("--- reasoning preview (前 300) ---")
print(reasoning[:300])
print("--- content (前 1500) ---")
print(content[:1500])
