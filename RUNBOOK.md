# redmine-similar-assist 部署接班文档

> 给"项目支持"子树下新建的「支持」工单自动添加 AI 一楼，附带历史相似案件和当时的解决方案。

历史案件的检索文本来自 `issues`、`journals`，以及通过 `issue_id`
关联的 `form_develop_*`、`form_tester_verify*`、`form_product_verify`
研发/测试操作记录。新增这些字段后需要重跑 `src.db_backfill --rebuild`
才能更新存量向量。

## 一、当前部署位置

| 维度 | 值 |
|---|---|
| 部署机器 | `demo.egova.com.cn`（`ssh root@demo.egova.com.cn`） |
| 部署目录 | `/opt/redmine-assist/` |
| Docker 容器 | `redmine-assist`（python:3.10-slim base） |
| 监听端口 | `127.0.0.1:8765`（仅本机暴露） |
| 公网入口 | `https://demo.egova.com.cn/redmine-assist/`（openresty 反代） |
| Redmine 数据源 | `172.16.4.222:13306` redmine 库（只读使用） |
| Embedding 服务 | SiliconFlow bge-m3（1024 维） |
| LLM 服务 | DeepSeek v4-flash（最便宜款，**严禁换 v4-pro**） |

```
/opt/redmine-assist/
├── code/              # 代码（rsync 同步自本地 D:\git\redmine-similar-assist）
│   ├── src/
│   ├── scripts/
│   ├── config.yaml    # 含真实 API key，受 .gitignore 保护
│   └── Dockerfile
├── data/              # 持久化数据（容器 bind mount）
│   ├── vectors.db     # sqlite-vec 向量库 ~670MB
│   ├── assist_log.db  # 写回幂等表
│   ├── sync_state.json
│   └── sync.lock      # 同步互斥锁
└── logs/
    ├── backfill.log
    └── sync.log
```

## 二、关键端点

| Path | 用途 | 鉴权 |
|---|---|---|
| `GET /redmine-assist/health` | 健康检查 | 公开 |
| `POST /redmine-assist/redmine-webhook` | Redmine 项目级 webhook（PoC 3355 项目用） | nginx IP allow 47.93.16.3 + 127.0.0.1 |
| `POST /redmine-assist/sync/incremental` | cron 触发的增量同步 + AI 写回 | nginx allow 127.0.0.1，Header `X-Webhook-Secret: rsa-sync-3355-2026` |

## 三、日常运维

### 看服务状态
```bash
ssh root@demo.egova.com.cn
docker ps | grep redmine-assist
curl -s http://127.0.0.1:8765/health
tail -50 /opt/redmine-assist/logs/sync.log
```

### 看实时日志（webhook 进程）
```bash
docker logs -f --tail 100 redmine-assist
```

### 看库里有多少 issue
```bash
docker exec redmine-assist python -c \
  "import sqlite3; print(sqlite3.connect('/app/data/vectors.db').execute('SELECT COUNT(*) FROM issues_meta').fetchone()[0])"
```

### 重启容器
```bash
docker restart redmine-assist
# 启动后会重新 load 全量 168k vectors 到 faiss 内存（约 30-60 秒）
# 期间 KNN 返回空，sync 会跳过；不影响数据
```

### 改代码后部署
```bash
# 本地 D:\git\redmine-similar-assist
bash scripts/deploy_demo.sh
# 该脚本：tar over ssh → 改 host 回 172.16.4.222 → docker build → restart
```

## 四、cron 配置

```
$ cat /etc/cron.d/redmine-assist-sync
*/5 * * * * root curl -fsS -X POST -H 'X-Webhook-Secret: rsa-sync-3355-2026' \
  -H 'Content-Type: application/json' \
  http://127.0.0.1:8765/sync/incremental >> /opt/redmine-assist/logs/sync.log 2>&1
```

**每 5 分钟跑一次** sync/incremental。每次：
1. 读 `last_sync_at` (data/sync_state.json)
2. 查 MySQL `updated_on > last_sync_at` 的 issue
3. 对每条：识别是否新建（created_on 在 last_sync_at 之后）
4. 新建 + tracker=支持 + project ∈「项目支持」子树 → 触发 AI 一楼写回
5. 文本变化 → 重 embed + 更新 faiss
6. 文本没变 → 仅更新 meta（status/closed_on）

调频率：改 `*/5` 为 `*/2` 或 `*/10`。

临时禁用（如大规模迁移期间）：
```bash
mv /etc/cron.d/redmine-assist-sync /etc/cron.d/redmine-assist-sync.disabled
```

## 五、项目白名单 - 「项目支持」子树自动识别

### 当前配置
```yaml
# /opt/redmine-assist/code/config.yaml
target_project_root_id: 3          # 「项目支持」project_id
target_projects: []                # 额外硬编码白名单（一般留空）
target_project_cache_ttl_sec: 600  # 兜底 TTL，10 分钟自动刷新
tracker_whitelist: [3]             # 仅「支持」类工单触发
```

### 新增子项目自动识别
1. 在 Redmine 任意「项目支持」子项目下新建一个子项目
2. 等 ≤5 分钟（下一次 cron 触发 sync）
3. sync 启动时调 `invalidate_target_project_cache()` 强制刷新
4. 重新查 SQL：`WHERE lft >= 430 AND rgt <= 10153 AND status = 1`
5. 立即生效

### 归档/关闭项目自动剔除
项目 status 改为非 1（已关闭/归档）后，下次 sync 自动从白名单移除，不再触发。

### 手动验证当前白名单
```bash
docker exec redmine-assist python -c \
  "from src.config import get_target_project_ids, invalidate_target_project_cache; \
   invalidate_target_project_cache(); \
   print('active projects:', len(get_target_project_ids()))"
```

## 六、AI 一楼格式（HTML 超链接）

写回到 Redmine 的 note 用 HTML：

```html
<p><strong>🤖 AI 智能助理建议（仅供参考，请人工判断）</strong></p>
<p>根据语义相似度匹配，以下历史案卷可能相关：</p>
<ol>
  <li><a href="https://faq.egova.com.cn:7787/issues/499326">#499326 标题</a>
      [置信度 95%]
      <ul><li><strong>当时解决方案</strong>：xxx</li></ul>
  </li>
  ...
</ol>
<p><em>*以上由 AI 自动检索历史相似案卷生成。如有错漏请忽略。</em></p>
```

CKEditor 渲染时 `<a>` 自动可点击。

## 七、排障 Checklist

### Symptom: 工单建好了但没看到 AI 一楼
1. 看 cron 是否在跑：`tail -20 /opt/redmine-assist/logs/sync.log`
2. 看是否被 tracker_whitelist 过滤（必须是「支持」tracker_id=3）
3. 看是否被 project 白名单过滤：
   - 该项目是否在「项目支持」子树下？
   - 该项目 status 是否为 1？
4. 看是否被幂等表跳过：
   ```bash
   docker exec redmine-assist python -c \
     "import sqlite3; print(sqlite3.connect('/app/data/assist_log.db').execute('SELECT * FROM assist_log WHERE issue_id=<id>').fetchall())"
   ```
5. 看 webhook 进程日志：`docker logs --tail 50 redmine-assist`
6. 看 sync 锁是否卡死：`ls -la /opt/redmine-assist/data/sync.lock`（有 stale lock 会自动接管，但仍可手动 `rm` 强制）

### Symptom: AI 评论召回质量差
- 阈值在 `config.yaml`:
  - `recall.min_cosine: 0.65`（向量召回门槛）
  - `recall.final_top: 3`（最终展示数量上限）
- LLM gate 用 DeepSeek 判断 `related: true/false` + 抽取 solution

### Symptom: cron 报 "sync already running"
- 上次 sync 卡了（一般是 DeepSeek 超时或 SiliconFlow 限速）
- `_FileLock` 带 stale lock 接管：默认 3600 秒后自动认为死，强制释放
- 紧急可手动：`rm /opt/redmine-assist/data/sync.lock`

### Symptom: SiliconFlow / DeepSeek 返回 429 / 5xx
- 看 `docker logs redmine-assist | grep -E '429|5xx|Error'`
- 临时降并发：改 `config.yaml`:
  ```yaml
  embedding:
    concurrency: 4   # 默认 8，限速时降到 4 或 2
  ```
- 重启容器生效

### Symptom: 容器 OOM
- faiss 索引常驻 ~700MB；如果开几个 backfill exec 同时跑会翻倍
- demo 内存 15G，正常占用 11-12G。如果 free < 1G 要警惕

## 八、成本

| 项目 | 单位 | 数量级 |
|---|---|---|
| SiliconFlow bge-m3 | 免费 | 不计费 |
| DeepSeek v4-flash input (uncached) | ¥1/1M tokens | 单工单 ~2k tokens = ¥0.002 |
| DeepSeek v4-flash output | ¥2/1M tokens | 单工单 ~200 tokens = ¥0.0004 |
| **每张支持工单触发成本** | ≈ ¥0.0024 |  |
| **预估月度成本（千张工单）** | ≈ **¥2-3** | 可忽略 |

## 九、密钥 / 凭据

| 凭据 | 位置 |
|---|---|
| Redmine API key (gczx) | `config.yaml` redmine.api_key |
| Redmine DB 密码 | `config.yaml` redmine_db.password |
| SiliconFlow key | `config.yaml` embedding.api_key |
| DeepSeek key | `config.yaml` llm.api_key |
| sync 端点 secret | `config.yaml` webhook.sync_secret |

**所有凭据都在 `/opt/redmine-assist/code/config.yaml`**（`.gitignore` 保护，不进版本库）

**轮换密钥流程**：
1. 改 `config.yaml`
2. `docker restart redmine-assist`（30-60 秒 reload）

## 十、备份建议

需要备份的：
- `/opt/redmine-assist/data/vectors.db`（~670MB，全量向量）
- `/opt/redmine-assist/data/assist_log.db`（幂等表）
- `/opt/redmine-assist/data/sync_state.json`（同步水位线）
- `/opt/redmine-assist/code/config.yaml`（密钥）

不需要备份：
- `logs/`（运行日志，可丢）
- Docker 镜像（可重新 build）

向量库丢了的话：
- 重跑 `docker exec redmine-assist python -m src.db_backfill`
- 全公司 16.8w 条约 **60 分钟**完整重建

## 十一、Git 仓库

本地：`D:\git\redmine-similar-assist`

`config.yaml` 在 `.gitignore`，不进 git。其他全部入库即可。

远程：
- GitHub: `origin = github.com/majq1990/redmine-similar-assist`（fetch 走 ghfast.top）
- gitlab 内网: `gitlab.egova.com.cn:222/devops/redmine-similar-assist`（main 是保护分支，提交走 `jscz-yyyymmdd` 分支再 MR）

---

# 十二、precheck 对接前置避坑工具（2026-06-25 上线）

业务人员/工程师在启动一个对接业务前，**贴一句业务描述**自动从 17 万历史工单 + 4500 钉钉文档中聚类高频踩坑，提前规避。

## 12.1 入口（按使用便捷度排序）

| 入口 | URL/命令 | 适用 |
|---|---|---|
| **Claude Code skill** | 用户说"对接避坑/precheck/做xx对接想知道坑" 自动触发 | **业务/工程师推荐** |
| HTTP API 直调 | `POST https://demo.egova.com.cn/redmine-assist/precheck` | 自动化/脚本/集成 |
| 容器内 CLI | `docker exec -e PYTHONPATH=/app -w /app redmine-assist python -m src.precheck "<描述>"` | 调试 |

## 12.2 HTTP 接口

```bash
curl -sS -m 180 -X POST https://demo.egova.com.cn/redmine-assist/precheck \
  -H "Content-Type: application/json" \
  -H "X-Precheck-Token: <token>" \
  -d '{"description": "做车载GPS轨迹对接，对方808协议走TCP"}'
```

- token 存：`/etc/redmine-assist/precheck_token`（600 权限）
- nginx 反代：`/etc/nginx/conf.d/default.conf` 内 `location = /redmine-assist/precheck` 校验 `X-Precheck-Token` header
- 端到端 45-60 秒（含 LLM 推理）

返回 JSON：`{markdown, items[], stats{n_issues,n_docs,n_clusters,elapsed_ms}}`

## 12.3 核心代码

| 模块 | 职责 |
|---|---|
| `src/precheck.py` | 主流程：双路 KNN（issues 限 5 类故障 tracker + chunks）→ LLM 聚类→ markdown |
| `src/webhook_server.py` `precheck_endpoint` | HTTP /precheck 端点（Bearer/X-Precheck-Token 鉴权） |
| `src/llm_judge.py` `_call(messages, max_tokens=...)` | LLM 调用，**max_tokens=12000** 应对 DeepSeek-v4-flash reasoning_content 占配额 |

## 12.4 故障 tracker 池

固定 5 类（实际"出过事"的工单）：
```python
FAULT_TRACKERS = {3, 1, 22, 26, 27}  # 支持/BUG/适配/安全/性能 共 5.3 万
```
排除：需求/开发/UI/代码审核/里程碑 等纯流程类。

## 12.5 钉钉 deap MCP 集成尝试（已弃）

`src/mcp_server.py` 是 streamable-http MCP server（JSON-RPC 2.0），原计划接钉钉 deap AI 助理。
反复调试发现 deap 的 Java Reactor MCP client **不严格遵循 MCP spec**：
- 已修：必返 `mcp-session-id` header、SSE chunked transfer、DELETE 终止、OPTIONS preflight
- 仍卡：Reactor `map` operator `8000ms no item or terminal signal` 错误，参考其他 MCP 也有类似现象

**结论**：放弃 MCP 集成路线，改走 **Claude Code skill** 直调 HTTP /precheck（已上线）。
`src/mcp_server.py` 保留作技术参考，nginx `/redmine-assist/mcp` 仍可用（curl 测试 OK）。

## 12.6 Claude Code skill

| 项 | 值 |
|---|---|
| skill 名 | `precheck-integration`（"对接前置避坑"）|
| 本地位置 | `~/.claude/skills/对接前置避坑/SKILL.md` |
| 企业分发 | `demo:8091`（skill-server）→ `precheck-integration` |
| visibility | `support-dept`（技术支持部+技术经理可见，可后续扩 global） |
| 触发词 | 对接前置 / 对接避坑 / 对接踩坑 / 启动对接 / precheck |
| 维护 | majianquan |

## 12.7 已知小问题/后续优化

- chunk_store 重启冷加载 39k chunks 需 ~120s（已并行 warmup 不阻塞端口）
- 钉钉 deap MCP 不兼容（见 12.5），如未来 deap Reactor 改了 spec 可重试
- 文档增量 backfill 跑过一次后有 172 个文档未入库（96.2% 覆盖），可日后补全
- 接口/skill 都默认走"支持部"可见性，业务侧用户需手动 install
