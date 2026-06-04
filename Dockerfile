FROM python:3.10-slim-bookworm

WORKDIR /app

# 阿里云 pip mirror，国内拉包快
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    TZ=Asia/Shanghai

# 依赖单独一层，code 改动不会重装包
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 预建 data/logs 挂载点，避免 :ro 主目录下 mkdir 失败
RUN mkdir -p /app/data /app/logs

# 应用代码通过 bind mount 注入到 /app（部署时挂 /opt/redmine-assist/code）
EXPOSE 8765
CMD ["python", "-m", "src.webhook_server"]
