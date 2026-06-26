"""诊断：用相同 prompt 直调 DeepSeek API，看返回什么（finish_reason/usage/content）。"""
import json
import os
import requests
import sys

# 从 config.yaml 读 key（避免硬编码）
sys.path.insert(0, "/app"); os.chdir("/app")
from src.config import cfg
c = cfg()["llm"]

# 复刻 precheck 的 prompt（缩水版，4 issue + 2 doc 测最小可用）
prompt = """你是 Redmine 工单分析助理。用户即将启动一个对接业务，希望提前知道历史上这类对接踩过哪些坑。

【业务描述】
我要做车载GPS轨迹对接，对方使用808协议走TCP推送，已知现场是政务网+互联网双网环境

【历史相似案件（按相似度降序，共 4 条）】
#401767 朝阳运管服 808 对接微服务部署支持 | 处理: 已完成部署测试无问题
#422112 徐州运管服 流动摊贩重复上报 | 处理: 按姓名电话30天去重
#430167 朝阳运管服 公厕车辆监控对接 | 处理: 第三方技术原因暂挂起
#365214 哈尔滨 GPS 数据接入 | 处理: 配置坐标系反向

【钉钉知识库相关文档片段（共 2 条）】
docA | 车载对接Wiki | 808 协议必须先在 tc_vehicle 表注册车辆 SIM 卡
docB | 车载轨迹综合指南 | 支持 TCP/HTTP/库表三种方式 注意坐标系一致

请按"相同问题模式"对这些案件聚类（粒度要粗,目标是 3-5 个典型坑）：
每类输出: 标题 / case_ids / doc_refs / advice
只输出 JSON 对象 {"clusters": [{"title":"...","case_ids":[],"doc_refs":[],"advice":"..."}]}
"""

payload = {
    "model": c["model"],
    "messages": [
        {"role": "system", "content": "You output only JSON, no prose."},
        {"role": "user", "content": prompt},
    ],
    "temperature": 0.1,
    "max_tokens": 2000,
    "response_format": {"type": "json_object"},
}
print(f"--- 调用 {c['endpoint']} 模型={c['model']} ---")
r = requests.post(
    c["endpoint"],
    headers={"Authorization": f"Bearer {c['api_key']}", "Content-Type": "application/json"},
    json=payload, timeout=120,
)
print("HTTP", r.status_code)
data = r.json()
print("usage:", data.get("usage"))
ch = data["choices"][0]
print("finish_reason:", ch.get("finish_reason"))
print("message keys:", list(ch.get("message", {}).keys()))
print("--- content ---")
print(ch["message"].get("content"))
