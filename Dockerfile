# 纺织 B2B 交易智能体 — 生产镜像（多阶段）
# 阶段 1: Node 构建 React 前端（web/dist）
# 阶段 2: Python 运行时（FastAPI + 本地 embedding 模型）
# 部署入口 scripts/docker_entrypoint.sh：建表/管理员/知识索引 → uvicorn

# ── 阶段 1：前端构建 ─────────────────────────────────────
FROM node:20-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ── 阶段 2：Python 运行时 ────────────────────────────────
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

WORKDIR /app

# 依赖层（利用层缓存）
# pip 源可覆盖：国内部署默认走清华镜像（官方 PyPI 在容器内常超时）；
# 海外环境可用 --build-arg PIP_INDEX_URL=https://pypi.org/simple 覆盖
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

# 模型下载放【应用代码 COPY 之前】：以后只改业务代码时，
# COPY 层及后续层复用缓存，不会每次重下 0.4GB 模型
# （下载行临时关 offline + 禁 Xet；官方源不可达时可加 HF_ENDPOINT 换镜像）
ARG DOWNLOAD_MODELS=1
# HF 模型源可覆盖：国内部署默认走 hf-mirror.com（官方源不可达）；
# 官方源可用时 --build-arg HF_ENDPOINT=https://huggingface.co 覆盖
ARG HF_ENDPOINT=https://hf-mirror.com
RUN if [ "$DOWNLOAD_MODELS" = "1" ]; then \
      HF_ENDPOINT=${HF_ENDPOINT} HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 HF_HUB_DISABLE_XET=1 \
        python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-base-zh-v1.5')"; \
    fi

# 应用代码 + 数据 + 前端产物 + 部署入口
COPY . .
COPY --from=web /web/dist ./web/dist

# 入口：等依赖 → 初始化（建表/管理员/知识索引）→ 启动
RUN chmod +x scripts/docker_entrypoint.sh
ENTRYPOINT ["bash", "scripts/docker_entrypoint.sh"]

EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=5s --start-period=240s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8005/healthz', timeout=3)" || exit 1
