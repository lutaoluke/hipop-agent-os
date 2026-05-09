FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

# 先 copy 业务代码（变更频率高，最后 layer）
COPY hipop ./hipop
COPY db ./db
COPY scripts ./scripts

# 飞书 / inbox / logs 只挂 volume，不打包
RUN mkdir -p /app/inbox /app/logs

EXPOSE 8765

# 健康检查（容器内自检）
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8765/health || exit 1

CMD ["python", "-m", "uvicorn", "hipop.server.main:app", "--host", "0.0.0.0", "--port", "8765"]
