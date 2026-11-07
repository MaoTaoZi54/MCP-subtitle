# 不使用 BuildKit 专有语法：部分 NAS 镜像站会拒绝 docker/dockerfile 前端镜像
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NAS_APP_HOST=0.0.0.0 \
    NAS_APP_PORT=8669 \
    NAS_APP_DATA=/data \
    NAS_APP_MCP_ENABLED=1 \
    NAS_APP_MCP_HOST=0.0.0.0 \
    NAS_APP_MCP_PORT=8670

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY nas_app ./nas_app
COPY nas_mcp ./nas_mcp

RUN mkdir -p /data && chmod 755 /data

VOLUME ["/data"]
EXPOSE 8669 8670

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8669/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "nas_app"]
