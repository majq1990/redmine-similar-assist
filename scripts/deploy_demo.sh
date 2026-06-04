#!/bin/bash
# 本机执行：把本仓库 scp 到 demo:/opt/redmine-assist/code，build image，起容器
# 用法：scripts/deploy_demo.sh
set -euo pipefail

HOST=demo.egova.com.cn
REMOTE_DIR=/opt/redmine-assist
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "[1/6] 创建 demo 目录..."
ssh root@$HOST "mkdir -p $REMOTE_DIR/{code,data,logs}"

echo "[2/6] 同步代码（tar pipe，排除 data/.git/__pycache__/venv/bench）..."
cd "$LOCAL_DIR"
tar -cz \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='.venv' \
  --exclude='data' \
  --exclude='bench*' \
  --exclude='*.log' \
  -f - . | ssh root@$HOST "tar -xz -C $REMOTE_DIR/code/"

echo "[3/6] 在 demo 调整 config（host 改 172.16.4.222 直连）..."
ssh root@$HOST "sed -i 's|host: \"127.0.0.1\"|host: \"172.16.4.222\"|' $REMOTE_DIR/code/config.yaml"

echo "[4/6] build docker image..."
ssh root@$HOST "cd $REMOTE_DIR/code && docker build -t redmine-assist:latest ."

echo "[5/6] 启停容器..."
ssh root@$HOST "docker rm -f redmine-assist 2>/dev/null || true"
ssh root@$HOST "docker run -d --name redmine-assist \
  --restart unless-stopped \
  -p 127.0.0.1:8765:8765 \
  -v $REMOTE_DIR/code/src:/app/src:ro \
  -v $REMOTE_DIR/code/scripts:/app/scripts:ro \
  -v $REMOTE_DIR/code/config.yaml:/app/config.yaml:ro \
  -v $REMOTE_DIR/data:/app/data \
  -v $REMOTE_DIR/logs:/app/logs \
  redmine-assist:latest"

echo "[6/6] 等待 health..."
sleep 5
ssh root@$HOST "curl -s http://127.0.0.1:8765/health"
echo
echo "DONE. 容器状态："
ssh root@$HOST "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep redmine-assist"
