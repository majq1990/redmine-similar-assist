# 对接前置避坑（precheck）—— 钉钉智能助理接入手册

> 让业务人员在钉钉群里 **@ 助理 + 一句业务描述**，自动得到「这类对接最容易踩的 N 个坑 + 历史案件 + 文档参考」。

整个链路：
```
钉钉用户 @助理 "做xx对接..."
    ↓
钉钉智能助理（AI Agent）
    ↓ Action 调用
HTTP POST https://demo.egova.com.cn/redmine-assist/precheck
    ↓
[底层] 双路 KNN（30 issues + 10 doc chunks）+ DeepSeek 聚类 → top N 高频坑
    ↓
markdown 报告 → 助理转给用户
```

---

## 一、HTTP 接口规范

### 端点

```
POST https://demo.egova.com.cn/redmine-assist/precheck
```

**当前状态（2026-06-25 已上线）**：
- nginx 反代已配置（default.conf line ~167-180）✅
- token 鉴权已启用，token 存 `demo:/etc/redmine-assist/precheck_token`（600 权限）
- 公网回归测试通过：59 秒返回 5 类 cluster，含车载对接Wiki 命中

### 请求

```json
{
  "description": "我要做车载GPS轨迹对接，对方使用808协议走TCP推送，已知现场是政务网+互联网双网环境",
  "top_issues": 30,
  "top_docs": 10
}
```

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `description` | string | ✓ | — | 业务描述，**4000 字以内**；越具体（含产品/协议/三方系统名）召回质量越好 |
| `top_issues` | int | | 30 | KNN 召回的工单候选数（限"支持/BUG/适配/安全/性能"5 类 tracker）|
| `top_docs` | int | | 10 | 钉钉知识库文档片段候选数 |

### 响应（200）

```json
{
  "markdown": "## 对接前置避坑提示\n\n基于历史 30 条相似工单...",
  "items": [
    {
      "title": "网络端口/防火墙配置问题",
      "count": 4,
      "case_ids": [504431, 393529, 413860, 466157],
      "doc_refs": ["AR4GpnMq..."],
      "doc_refs_with_url": [
        {"node_id": "AR4Gpn...", "title": "车载对接Wiki", "url": "https://alidocs.dingtalk.com/i/nodes/..."}
      ],
      "advice": "提前与网络管理员沟通，确保端口（如9061）开放..."
    }
  ],
  "stats": {
    "n_issues": 30,
    "n_docs": 10,
    "n_clusters": 5,
    "elapsed_ms": 45189
  }
}
```

### 错误响应

| HTTP | 含义 | 处理 |
|---|---|---|
| 400 | `description` 缺失或超过 4000 字 | 客户端修正 |
| 401 | 鉴权失败（见鉴权章节） | 检查 token |
| 503 | 服务正在加载向量库（重启后 ~8 min cold load） | 助理可提示"系统启动中，1 分钟后重试" |
| 500 | 服务端错误 | 看 `docker logs redmine-assist` |

**典型延迟**：30 issues + 10 docs 端到端 **~45 秒**（embed 1s + KNN 200ms + LLM reasoning 40s）。钉钉助理 Action 默认超时 30s，需把超时调到 **120s 以上**。

---

## 二、nginx 反代 + 鉴权配置（已部署，下文备查）

### 2.1 加 location（在 demo.egova.com.cn 的 default.conf 里追加）

```nginx
# 公网入口，给钉钉智能助理 Action 调用
location = /redmine-assist/precheck {
    # 简单 token 鉴权：必须带 X-Precheck-Token 且匹配
    if ($http_x_precheck_token != "REPLACE_WITH_RANDOM_TOKEN") {
        return 401;
    }
    # 钉钉服务端 IP 白名单（可选，进一步加固）
    # allow 47.99.x.x;  # 钉钉 outbound IP
    # deny all;

    proxy_pass http://127.0.0.1:8765/precheck;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_connect_timeout 10s;
    proxy_send_timeout    180s;
    proxy_read_timeout    180s;  # LLM 推理 ~45s，给足余量
    client_max_body_size  100k;
}
```

把 `REPLACE_WITH_RANDOM_TOKEN` 换成 32 字节随机串，例如 `openssl rand -hex 16` 生成的。

### 2.2 reload nginx

```bash
nginx -t && nginx -s reload
```

### 2.3 服务端同步加 token 校验（可选但推荐）

如果只靠 nginx 鉴权够用，**服务端不用改**。如果要双重防护，在 `webhook_server.py` 的 `precheck_endpoint` 里加：

```python
expected_token = (cfg().get("precheck") or {}).get("token", "")
if expected_token and request.headers.get("X-Precheck-Token") != expected_token:
    return jsonify({"error": "unauthorized"}), 401
```

并在 `config.yaml` 加：
```yaml
precheck:
  token: "REPLACE_WITH_SAME_TOKEN_AS_NGINX"
```

### 2.4 验证公网可达

```bash
curl -sS -X POST https://demo.egova.com.cn/redmine-assist/precheck \
  -H "Content-Type: application/json" \
  -H "X-Precheck-Token: REPLACE_WITH_RANDOM_TOKEN" \
  -d '{"description":"做车载GPS轨迹对接，808协议走TCP，政务网+互联网双网"}' \
  | python3 -m json.tool | head -50
```

预期：返回完整 JSON 含 `markdown` 字段，`stats.n_clusters >= 3`。

---

## 三、钉钉智能助理（AI Agent）配置

### 3.1 创建助理

进入钉钉开放平台：https://open-dev.dingtalk.com/

侧栏选 **应用开发 → AI 助理 / 智能助理 → 创建助理**。

| 字段 | 推荐值 |
|---|---|
| 助理名称 | 对接前置避坑助手 |
| 助理人设 | 见 3.3 |
| 头像 | 自选（推荐齿轮/盾牌图标）|
| 可见范围 | 工程支持/技术支持部群组 |

### 3.2 配置 Action（核心）

在助理详情页 → **能力 → Action（动作）→ 新建自定义 Action**：

| 字段 | 值 |
|---|---|
| Action 名称 | `query_precheck` |
| Action 描述 | 给用户的对接业务描述查询历史踩坑记录，返回 markdown 报告 |
| 调用方式 | HTTP API |
| 请求方法 | `POST` |
| 请求 URL | `https://demo.egova.com.cn/redmine-assist/precheck` |
| 超时(秒) | **120**（默认 30 不够，LLM 推理要 45s）|

**请求头（Headers）**：
```
Content-Type: application/json
X-Precheck-Token: REPLACE_WITH_RANDOM_TOKEN
```

**请求体（Body, JSON Schema）**：
```json
{
  "type": "object",
  "properties": {
    "description": {
      "type": "string",
      "description": "用户的对接业务描述。必须包含产品/协议/三方系统名等关键词，越具体越好。例如：'做车载GPS轨迹对接，对方808协议走TCP，政务网+互联网双网环境'"
    }
  },
  "required": ["description"]
}
```

**响应解析（用 `data.markdown` 作为助理回复内容）**：
```json
{
  "type": "object",
  "properties": {
    "markdown": {
      "type": "string",
      "description": "格式化的避坑提示 markdown，可直接转发给用户"
    },
    "stats": {
      "type": "object",
      "description": "召回统计：n_issues / n_docs / n_clusters / elapsed_ms"
    }
  }
}
```

### 3.3 助理人设（System Prompt）

```
你是「对接前置避坑助手」，专门帮工程师在启动对接业务前查询历史踩坑记录。

## 核心规则

1. 当用户描述了一个**对接业务**（关键词：对接 / 接入 / 推送 / 接口 / 协议 / 三方 / 集成）时，**必须**调用 `query_precheck` Action，把用户原话作为 description 传入。

2. 拿到 Action 返回后：
   - **完整原样**把 `markdown` 字段返回给用户，不要改写、不要总结、不要省略。
   - 如果用户问"还有吗"/"再多一些"，可以补充说明这是基于历史 30 条相似工单聚类，再多召回意义不大，建议提供更具体的产品/协议/三方系统名重试。

3. 如果用户描述太短（< 10 字）或太泛（如"做个对接"），**先反问**：
   - "请告诉我具体的：① 对接什么数据（GPS轨迹/视频/业务表）；② 用什么协议（808/HTTP/库表）；③ 三方系统是谁。"
   - 不要直接调 Action（会返回低质量结果）。

4. 如果 Action 返回 503，告诉用户："系统正在加载向量库（~8 分钟），请稍后重试"。

5. 如果用户问与对接**无关**的问题（如闲聊、其他业务），礼貌说明你只负责对接前置查询，不要硬调 Action。

## 风格

- 输出 markdown 时**保留所有 [#数字](链接) 案件链接** —— 用户点过去能直接看到历史详情。
- 不要主动总结或翻译 Action 输出，因为它已经是 LLM 精心整理的结构化报告。
- 简短确认："已查询历史 N 个相似案件，以下是常见的 X 个坑：" 然后贴 markdown。
```

### 3.4 触发示例（用户在群里的表达）

| 用户说 | 助理动作 |
|---|---|
| `@助理 做xx城市的车载GPS轨迹对接，对方808协议` | 调 Action |
| `@助理 微信支付对接，金额异步通知` | 调 Action |
| `@助理 数据库异构同步 mysql→pg` | 调 Action |
| `@助理 帮我做个对接` | 反问要细节，不调 Action |
| `@助理 今天午饭吃啥` | 礼貌拒绝 |

---

## 四、运维 / 故障排查

### 4.1 关键监控点

```bash
# 检查服务状态
curl -s http://127.0.0.1:8765/health
# {"ok":true, "ready":true} 才能正常处理 precheck

# 看最近 precheck 调用日志
docker logs --tail 30 redmine-assist 2>&1 | grep precheck

# 看实际召回 + LLM 输出
docker logs --tail 50 redmine-assist 2>&1 | grep "precheck DEBUG"
```

### 4.2 常见问题

| 症状 | 根因 | 处理 |
|---|---|---|
| ready=false 持续 > 10 min | 向量库加载异常 | 重启容器 + 看 `docker logs` 看 faiss 加载到哪一步 |
| n_clusters=0 | LLM 输出空（DeepSeek 推理占满 max_tokens）| 已修复：`max_tokens=12000`。若仍空，看 `[llm_judge] empty content! finish_reason=...` 日志 |
| n_issues=0 | 召回为空，业务描述太短/太泛 | 提示用户补充关键词 |
| HTTP 504 | LLM 超时 > 180s | 看 DeepSeek API 是否限流；考虑临时降级 top_issues=20 |
| 钉钉端报 timeout | Action 默认 30s 不够 | 把 Action 超时改 **120s** |

### 4.3 直接 CLI 调试

不经过 HTTP/钉钉，直接在 demo 容器内：

```bash
ssh root@demo.egova.com.cn
docker exec -e PYTHONPATH=/app -w /app redmine-assist \
  python -m src.precheck "做车载GPS对接，808协议" --json | head -80
```

---

## 五、迭代方向（已知可优化）

| 优先级 | 项 | 说明 |
|---|---|---|
| 中 | tracker 黑名单可配置 | 当前固定 `{3,1,22,26,27}`（支持/BUG/适配/安全/性能），可下放 config 让不同业务调整召回池 |
| 中 | 按时间衰减加权 | 老案件出现 10 次可能不如新案件出现 3 次；可对 case_id 按 created_on 衰减 |
| 低 | 增量预生成 | 高频业务关键词（GPS/支付/数据库同步）可定时跑 precheck 缓存结果，钉钉调用命中缓存秒回 |
| 低 | 接 wukong 大屏 | 把"高频坑"以大屏形式展示，作为团队知识沉淀 |

---

## 六、文件位置速查

| 项 | 路径 |
|---|---|
| 代码主文件 | `D:\git\redmine-similar-assist\src\precheck.py` |
| webhook 端点 | `src/webhook_server.py:precheck_endpoint` |
| HTTP 接口 | `https://demo.egova.com.cn/redmine-assist/precheck`（待加 nginx 反代）|
| 容器内服务 | `http://127.0.0.1:8765/precheck` |
| 部署 | demo `/opt/redmine-assist/code/src/precheck.py` |
| 日志 | `docker logs redmine-assist`（含 `[precheck]`/`[llm_judge]` 前缀）|
| 故障 tracker 白名单 | `src/precheck.py:FAULT_TRACKERS = {3,1,22,26,27}` |
| 服务端 LLM max_tokens | `src/precheck.py` 内显式传 `max_tokens=12000`（覆盖 cfg.llm.max_tokens=3000）|

---

*维护者：马健权 (id=49) + Claude Code。最后更新：2026-06-25。*
