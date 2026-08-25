# 纺织 B2B 智能客服 — 生产镜像
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

WORKDIR /app

# 依赖层（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码 + 数据 + 索引
COPY . .

# 构建时可选下载 embedding/rerank 模型（离线推理、镜像自包含）
# 跳过：docker build --build-arg DOWNLOAD_MODELS=0  然后将 ~/.cache/huggingface 挂载进容器
ARG DOWNLOAD_MODELS=1
RUN if [ "$DOWNLOAD_MODELS" = "1" ]; then \
      python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; \
                 SentenceTransformer('BAAI/bge-base-zh-v1.5'); \
                 CrossEncoder('BAAI/bge-reranker-base')"; \
    fi

EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8005/healthz', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8005"]