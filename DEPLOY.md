# 部署到 demo.egova.com.cn

## 1. 代码同步

本机 → demo：
```powershell
scp -r D:\git\redmine-similar-assist root@demo.egova.com.cn:/opt/redmine-similar-assist
ssh root@demo.egova.com.cn "ls /opt/redmine-similar-assist"
```

后续增量：仅同步 `src/`、`config.yaml`、`requirements.txt`：
```powershell
scp -r D:\git\redmine-similar-assist\src root@demo.egova.com.cn:/opt/redmine-similar-assist/
```

## 2. Python 环境

```bash
ssh root@demo.egova.com.cn
cd /opt/redmine-similar-assist
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> CentOS 7 注意：sqlite-vec 需要 sqlite >= 3.41。CentOS 7 自带 3.7.x，必须用 pyenv 装新 Python 或装 Anaconda Python。
> 替代：如已有 captcha-ocr 那个 docker 化的 Python 环境，可参考它的 Dockerfile 模式跑容器。

## 3. 首次 backfill（在 demo 上跑一次即可）

```bash
cd /opt/redmine-similar-assist
source .venv/bin/activate
python -m src.backfill
# 预期：5-10 分钟，落 data/vectors.db ~ 3-5MB
```

## 4. systemd unit

`/etc/systemd/system/redmine-assist.service`：

```ini
[Unit]
Description=Redmine similar-issue AI assistant (PoC project 3355)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/redmine-similar-assist
Environment=PYTHONIOENCODING=utf-8
ExecStart=/opt/redmine-similar-assist/.venv/bin/python -m src.webhook_server
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/redmine-assist.log
StandardError=append:/var/log/redmine-assist.log

[Install]
WantedBy=multi-user.target
```

启用：
```bash
systemctl daemon-reload
systemctl enable --now redmine-assist
systemctl status redmine-assist
curl http://127.0.0.1:8765/health
```

## 5. Nginx 反代

加到 demo 的 nginx 配置（参考 `/etc/nginx/conf.d/` 现有 server）：

```nginx
location /redmine-assist/ {
    proxy_pass http://127.0.0.1:8765/;
    proxy_set_header X-Real-IP        $remote_addr;
    proxy_set_header X-Forwarded-For  $proxy_add_x_forwarded_for;
    proxy_set_header X-Webhook-Secret $http_x_webhook_secret;
    proxy_read_timeout 60s;
}
```

```bash
nginx -t && systemctl reload nginx
curl -k https://demo.egova.com.cn/redmine-assist/health
```

## 6. Redmine 装 redmine_webhooks 插件

> 这一步要登录 **Redmine 服务器主机**（不是 demo），即 `faq.egova.com.cn:7787` 后端那台机器，需要 Redmine 管理员或 root 权限。

```bash
cd /path/to/redmine/plugins
git clone https://github.com/suer/redmine_webhooks.git
cd /path/to/redmine
bundle install --without development test
bundle exec rake redmine:plugins:migrate RAILS_ENV=production
systemctl restart redmine    # 或 unicorn / puma，看你们部署方式
```

如装不上（Redmine 版本太老 / Ruby 不兼容），备选方案：

- 用 Redmine 自带的 **"网络钩子（Webhook）"** REST API（4.0+ 内置）
- 或装更轻量的 `redmine_webhook_notifier`

## 7. Redmine 后台配 webhook

登录 Redmine 管理员，进入项目 **3355 住建部** → 设置 → Webhooks（或全局 → 管理 → Webhooks）：

| 字段 | 值 |
|---|---|
| URL | `https://demo.egova.com.cn/redmine-assist/redmine-webhook` |
| Event | **Issue created** ☑（only） |
| Custom Headers | `X-Webhook-Secret: rsa-poc-3355-2026` |
| Content-Type | application/json |

> 关键：**只勾 Issue created**，updated/closed 不勾，业务要求每个工单只触发一次。

## 8. 验收

1. 在 Redmine 项目 3355 下手动建一条"支持"类型测试工单
2. demo 上 `tail -f /var/log/redmine-assist.log` 看是否收到 webhook
3. 看是否落 `data/assist_log.db`：

```bash
sqlite3 /opt/redmine-similar-assist/data/assist_log.db "SELECT * FROM assist_log ORDER BY processed_at DESC LIMIT 5;"
```

4. 看生成的 note 文案：candidates_json 字段。
5. **此时 write_back.enabled=false**：人工 review note 文案，OK 后改成 true 再 reload service。

## 9. 灰度开闸

```bash
sed -i 's/enabled: false/enabled: true/' /opt/redmine-similar-assist/config.yaml
systemctl restart redmine-assist
```

再建一条测试工单，应该能在 Redmine 看到 gczx 用户回复的一楼。

## 10. 故障 / 回退

立即停止 AI 回写：
```bash
systemctl stop redmine-assist
# 或保留 service 但关闸：
sed -i 's/enabled: true/enabled: false/' config.yaml && systemctl restart redmine-assist
```

撤回错误回复：直接在 Redmine 上删除 gczx 用户的 journal note 即可。
