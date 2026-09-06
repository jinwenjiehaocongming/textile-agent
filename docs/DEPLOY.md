# 部署到服务器 · 从零教学（让别人能访问）

> 适用：Ubuntu 22.04/24.04 + Docker。项目已在 Dockerfile/compose 里做好的事：
> 前端构建（多阶段）、依赖就绪后**自动**建表/管理员/灌知识索引（`docker_entrypoint.sh`），
> rerank 默认关闭（省 ~1GB 内存），Postgres/Qdrant 端口只绑本机。

---

## 0. 先想清楚三个选择

| 选择 | 建议 |
|---|---|
| 服务器 | 香港轻量 4G 起（免备案、快）；国内云 4G 起 + 域名备案（1-2 周） |
| 让别人怎么访问 | 演示期：`http://服务器IP:8005`；正式：域名 + HTTPS（免费证书） |
| 国内用户访问速度 | 服务器放国内或香港优化线路更稳；DeepSeek API 走云端，本机无 GPU 需求 |

---

## 1. 服务器基础准备

```bash
# SSH 登录（阿里云/腾讯云控制台会给公网 IP）
ssh root@你的服务器IP

# 系统更新 + 安装 Docker（含 compose 插件）
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker --version && docker compose version
```

## 2. 把代码放上服务器

```bash
# 方式 A：git clone（推荐，后续更新方便）
git clone <你的仓库地址> /opt/trading-agent && cd /opt/trading-agent

# 方式 B：本机 rsync 上传（没有远程仓库时）
rsync -av --exclude '.env' --exclude '.venv' --exclude 'web/node_modules' \
  --exclude 'logs' --exclude 'index' ./ root@服务器IP:/opt/trading-agent/
```

## 3. 配置生产环境变量（关键！）

```bash
cd /opt/trading-agent
cp .env.example .env
# 用编辑器打开 .env 逐项确认：
```

| 变量 | 必改项 | 示例 |
|---|---|---|
| `DEV_MODE` | **改 0**（否则 /dev/login 可冒充任意角色） | `DEV_MODE=0` |
| `JWT_SECRET` | 强随机（openssl rand -hex 32） | `openssl rand -hex 32` |
| `ADMIN_PASSWORD` | 换成你的强密码（entrypoint 会幂等建管理员） | 自定 |
| `DEEPSEEK_API_KEY` | 你的真实 key | — |
| `DASHSCOPE_*` / `SERPER_*` 等 | 保持或按需 | — |
| `RERANK_ENABLED` | `0`（省内存；本文件已默认 0） | `0` |
| `LANGSMITH_TRACING` | 演示期建议 `false` | — |

> `.env` 不要提交 git；服务器上的 .env 独立维护。

## 4. 构建并启动

```bash
docker compose up -d --build
# 首次会：npm ci + pip 装依赖 + 下载 embedding 模型（~0.4GB），约 3-10 分钟

# 看初始化日志（等 PG → 建表/管理员 → 灌知识索引 → uvicorn 启动）
docker compose logs -f app
# 出现 "[init] 依赖初始化完成，启动应用" 即就绪
```

就绪自检：

```bash
curl -s http://127.0.0.1:8005/healthz        # {"status":"ok",...}
curl -s http://127.0.0.1:8005/               # 返回 index.html（前端）
docker compose ps                            # app/postgres/qdrant 均 healthy/running
```

## 5. 让别人访问 —— 两档方案

### 方案 0：先让别人用 IP + 端口（最快，仅演示）
- 云控制台 **安全组**：放行入站 TCP 8005
- 告诉朋友：`http://你的公网IP:8005`
- 服务器防火墙若开着：`ufw allow 8005`
- ⚠️ 裸 HTTP + IP，仅临时演示；不要放生产数据

### 方案 1：域名 + HTTPS（正式）
**① DNS**：去域名商把 `A 记录` 指向服务器 IP（如 `agent.yourdomain.com`）
**② 让应用只服务本机**：把 `docker-compose.yml` 里 app 的 ports 改成
   `"127.0.0.1:8005:8005"`，然后 `docker compose up -d`
**③a 用 Caddy（最简单，自动 HTTPS 证书）：**

```bash
docker run -d --name caddy --restart unless-stopped \
  -p 80:80 -p 443:443 \
  -v /var/lib/caddy:/data \
  caddy:2 caddy reverse-proxy --from agent.yourdomain.com --to 127.0.0.1:8005
# 首次会自动申请 Let's Encrypt 证书；SSE 流式无需额外配置
```

**③b 或 nginx + certbot（想学主流栈）：**

```bash
apt install -y nginx certbot python3-certbot-nginx
cat > /etc/nginx/sites-available/agent <<'EOF'
server {
    listen 80;
    server_name agent.yourdomain.com;
    location / {
        proxy_pass http://127.0.0.1:8005;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        # SSE 流式：必须关缓冲，否则 AI 打字机一卡一卡
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
EOF
ln -s /etc/nginx/sites-available/agent /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
certbot --nginx -d agent.yourdomain.com     # 自动配 HTTPS
```

访问 `https://agent.yourdomain.com` ✅

## 6. 安全收尾清单（上线必查）

- [ ] `.env`：`DEV_MODE=0`、强 `JWT_SECRET`、强管理员密码（改完 compose 里 `docker compose exec app python scripts/create_admin.py` 重跑一次让新密码生效）
- [ ] 云安全组只开放：22(或改端口)、80、443；**不要**开 5432/6333（compose 已绑 127.0.0.1）
- [ ] 数据库默认口令 postgres/postgres 已够演示；生产请改并同步 `DATABASE_URL`
- [ ] 防火墙：`ufw allow 22,80,443/tcp`
- [ ] 反代方案下确认 8005 不再公网暴露

## 7. 日常运维

```bash
# 更新代码 + 重建（复用模型缓存，快）
git pull
docker compose up -d --build --build-arg DOWNLOAD_MODELS=0

# 日志 / 重启 / 状态
docker compose logs -f app
docker compose restart app
docker compose ps

# 备份数据库（volumes 数据也要定期备份）
docker compose exec postgres pg_dump -U postgres study1 > backup.sql

# 管理员密码或配置变更后重跑初始化（幂等，不会清数据）
docker compose exec app python scripts/create_admin.py
```

## 8. 常见问题

| 症状 | 排查 |
|---|---|
| 首次起很久没起来 | `docker compose logs app` 看卡在哪：卡模型下载 → 换 `DOWNLOAD_MODELS=0` + 挂载本地 HF 缓存 |
| 检索答非所问/知识库空 | 看日志里 build_index 是否完成；`docker compose exec app python -c "import asyncio,urllib.request; ..."` 或直接问"你们有哪些涤塔夫" |
| 页面 502 | app 还没就绪或崩了；先 `curl localhost:8005/healthz` |
| AI 回复像"卡住"（打字机一顿一顿） | 反代没关 SSE 缓冲（nginx 需 `proxy_buffering off`） |
| 登录即 401 | token 过期或 JWT_SECRET 变了 → 重新登录 |
| /dev/login 能访问 | `.env` 里 DEV_MODE 没改 0 |
| 重启后待审批丢了 | 已知边界：审批注册表在进程内存 → 生产可换 Redis/Postgres checkpoint（`src/approval.py` 注释有预留） |

## 9. 上线前已知边界（诚实清单）

- 单 worker：多副本需 PostgresSaver/Redis checkpoint + 审批表外置（代码只动一处 compile）
- 单机内存约 4G 够用；检索每轮 CPU embedding 几百 ms，人多再升级
- CORS 目前 `*`：同域反代没问题；跨域调用再收紧
