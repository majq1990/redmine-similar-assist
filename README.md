# redmine-similar-assist

新 issue 落库后，自动检索历史相似案卷 + 抽取解决方案 + 在 issue 下加一楼 note。

## PoC 范围（已锁定）

- **Redmine 项目**：`project_id=3355` 「住建部城市综合管理服务平台」
- **历史数据**：846 已关闭 + 6 在途 = 852 条
- **嵌入模型**：SiliconFlow bge-m3 (1024 维)
- **触发方式**：redmine_webhooks 插件
- **LLM 精排/摘要**：DeepSeek（备选本地 ollama）
- **向量库**：sqlite-vec（轻量、单文件、足够 3.5MB 量级）

## 数据流

```
[Redmine 新建/更新 issue]
     │  POST application/json
     ▼
[Flask /redmine-webhook]  → 入队（先简单同步处理，并发再换 RQ/celery）
     │
     ▼
[fetch issue detail]  GET /issues/{id}.json?include=journals
     │
     ▼
[clean text]  issue + journals + form_* 研发/测试记录，HTML→纯文本
     │
     ▼
[embed]  SiliconFlow bge-m3 → 1024 维向量
     │
     ▼
[KNN]  sqlite-vec 取 top 20
     │
     ▼
[LLM gate + 抽取]  DeepSeek：判断真相关 + 抽取"当时解决方案 1 句话"
     │
     ▼
[筛余 ≥3 条且 score ≥ 0.7 的]
     │
     ▼
[PUT /issues/{new_id}.json with notes]  以 ai-assistant 账号回写一楼
     │
     ▼
[落 ai_assist_log 表]  保证幂等
```

## 项目结构

```
redmine-similar-assist/
├─ config.example.yaml       配置模板（API keys / 端口 / 项目白名单）
├─ requirements.txt
├─ src/
│  ├─ config.py              加载 config.yaml
│  ├─ redmine_client.py      薄封装 Redmine REST
│  ├─ text_cleaner.py        HTML→text，重度去噪
│  ├─ embedder.py            SiliconFlow bge-m3
│  ├─ vector_store.py        sqlite-vec：upsert / knn / has_id
│  ├─ llm_judge.py           DeepSeek 相关性判断 + 解决方案抽取
│  ├─ pipeline.py            ingest_new_issue(issue_id) 主流程
│  ├─ webhook_server.py      Flask /redmine-webhook
│  └─ backfill.py            历史 846 条全量 embed 入库
├─ scripts/
│  ├─ run_webhook.ps1
│  └─ run_backfill.ps1
└─ data/
   └─ vectors.db             sqlite-vec 物理文件（gitignored）
```

## 上线步骤（建议顺序）

### 阶段 1：本地跑通（无 webhook）

1. `pip install -r requirements.txt`
2. 复制 `config.example.yaml` 为 `config.yaml`，填三个 key：
   - `redmine.api_key`：majianquan 的 key（或新建 `ai-assistant` 账号，强烈建议）
   - `embedding.api_key`：SiliconFlow key
   - `llm.api_key`：DeepSeek key
3. 跑 backfill：`python -m src.backfill`，全量 embed 846 条历史
4. 手动喂一个 open issue 测试：`python -m src.pipeline 501491`
5. 看终端打印的"待回写文案"，**先不真写**，人工验证质量

### 阶段 2：开 webhook 自动化

6. Redmine 装 `redmine_webhooks` 插件，配 webhook URL 指向部署机
7. 启 Flask：`python -m src.webhook_server`（生产用 gunicorn/waitress）
8. 在 Redmine 项目 3355 新建一个测试 issue，看 webhook 是否触发 + 是否成功回写

### 阶段 3：观察一周

9. 收集 ai_assist_log 表 + 人工反馈，调阈值（0.7 → 0.75 / 0.65）
10. 决定要不要铺到其他项目

## 安全 / 风险控制

- **专用账号**：用 `ai-assistant` 而非 majianquan，避免发言混淆
- **AI 标识**：所有回写都带 `🤖 AI 智能助理（仅供参考）` 前缀
- **幂等**：`ai_assist_log` 表记 `(issue_id, processed_at)`，同 issue 不重写
- **白名单**：webhook 只处理配置文件 `target_projects` 里的 project_id（首期只有 3355）
- **失败静默**：webhook 处理失败不抛错 500，记 log 即可，避免 Redmine 反复重试
- **数据出公司**：description 会发到 SiliconFlow + DeepSeek 云端。如不接受改用本机 ollama + qwen 跑

## 配置模板

见 `config.example.yaml`。
