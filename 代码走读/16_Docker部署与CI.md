# 16 · Docker 部署与 CI：一条命令从零到能聊天

> 目标：吃透 `docker compose up --build` 从零到"能聊天"的完整链条，以及 CI 到底在检查什么。
> 看完你能回答：镜像里干了哪些事？容器为什么能**自动**建表/建管理员/灌知识索引？
> 数据凭什么不丢？CI 和本地跑测试差在哪？文档和代码有哪些对不上的地方？

---

## 一、先建立心智模型：两套自动化，各管一段

| | docker compose（生产形态） | GitHub Actions CI（进门检查） |
|---|---|---|
| 触发 | 部署者手动敲命令 | push 到 main / 任何 PR（`ci.yml:3-6`） |
| 干什么 | 建镜像 → 起 3 服务 → 初始化数据 → 跑起来 | 起临时 PG → 装依赖 → pytest → 前端构建 → 红/绿 |
| 不干的事 | 不验证业务对不对 | 不部署、不评测（见五、Q8） |

一句话背诵版：**「容器能自动初始化」是 entrypoint 脚本的功劳；「数据不丢」是 named volume 的功劳；「代码敢上线」是 CI 变绿的功劳。**

上菜比喻：`Dockerfile` = 菜谱；`docker-compose.yml` = 宴会桌牌（哪道菜上哪桌、缺哪道先不上）；
`docker_entrypoint.sh` = 传菜员（主菜齐了才动筷、上菜顺序固定）；`ci.yml` = 后厨试吃员（出锅先尝，坏了不上桌）。

整条链路阶段图（背下来，面试画这个）：

```
git push / 开 PR
  └─ GitHub Actions：起临时 postgres → pip 装依赖 → pytest → npm ci+build → 绿/红（绿了才合并）
docker compose up --build（服务器上）
  ├─ ① 镜像构建（Dockerfile 两阶段）
  │     阶段1 node:20-alpine   npm ci && npm run build  → web/dist（前端成品）
  │     阶段2 python:3.11-slim pip 装依赖 → [DOWNLOAD_MODELS=1] 下载 bge 模型 ~0.4GB
  │                          → COPY 应用代码 + 拷贝阶段1 的 dist
  ├─ ② 起容器（compose 三服务，全部 restart: unless-stopped）
  │     postgres ── 空卷首次启动自动执行 sql/001_products.sql（281 条产品种子）
  │     qdrant   ── 独立向量服务（数据进 named volume）
  │     app      ── entrypoint：等PG → 等Qdrant → 建表+管理员 → 灌知识索引 → exec uvicorn
  └─ ③ 打开 http://服务器IP:8005 → /healthz ok → 登录 → 聊天
```

---

## 二、镜像：Dockerfile 多阶段逐行拆（全文 48 行）

### 2.1 阶段 1：前端构建（node:20-alpine）

```dockerfile
FROM node:20-alpine AS web                          # :7
WORKDIR /web                                        # :8
COPY web/package.json web/package-lock.json ./      # :9
RUN npm ci                                          # :10
COPY web/ ./                                        # :11
RUN npm run build                                   # :12
```

先 COPY **锁文件**再 `npm ci`，后 COPY 源码——缓存友好：锁文件没变，`npm ci` 层直接复用；`npm ci` = clean install（按锁文件精确安装、可复现）。
这阶段只产出 `/web/dist`，由下一阶段 `COPY --from=web` 带走（`:39`），**运行时镜像里不需要 node**——多阶段构建省体积的意义。

### 2.2 阶段 2：python:3.11-slim 运行时

```dockerfile
FROM python:3.11-slim                                # :15
ENV PYTHONUNBUFFERED=1 \        # 日志不缓冲，容器日志实时可见
    PYTHONDONTWRITEBYTECODE=1 \ # 不写 __pycache__，镜像更干净
    HF_HUB_OFFLINE=1 \          # 运行时 HuggingFace 强制离线
    TRANSFORMERS_OFFLINE=1      # （模型必须在镜像/缓存里，见 Q5）
```

用 **slim** 而非全量镜像（:15）省体积；注意是 **3.11**，CI 用 3.12（`ci.yml:34`）——版本漂移见第七节。
`HF_HUB_OFFLINE=1` 是运行时铁律：模型要么构建期打进镜像层、要么在 `hf_cache` 卷里，运行时绝不偷联网。

### 2.3 依赖层顺序：为什么先 requirements 再源码（面试高频）

```dockerfile
COPY requirements.txt .                              # :25
RUN pip install --no-cache-dir -r requirements.txt   # :26
# ……模型下载放中间（:31-35）……
COPY . .                                             # :38
```

Docker 镜像 = 一层层只读快照叠加：**某一层没变，它和下面的层全部走缓存；某一层变了，它上面的全部重跑。**
把 `requirements.txt` 先 COPY 单独 `pip install`，让它成为"很久才变"的底层——日常改业务代码只重跑
`COPY . .` 之后的层，pip 层命中缓存，秒级出镜像；反过来先 COPY 源码，改一行代码依赖层全失效、每次重装依赖。
模型下载放 `COPY . .` **之前**（注释 :28-30 原话："不会每次重下 0.4GB 模型"）同理。

### 2.4 DOWNLOAD_MODELS：构建期下载 embedding 模型

```dockerfile
ARG DOWNLOAD_MODELS=1                                # :31
RUN if [ "$DOWNLOAD_MODELS" = "1" ]; then \          # :32
      HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 HF_HUB_DISABLE_XET=1 \   # :33 临时开网 + 禁 Xet
        python -c "from sentence_transformers import SentenceTransformer; \
        SentenceTransformer('BAAI/bge-base-zh-v1.5')"; \                # :34 实际加载=真正下载
    fi                                               # :35
```

- `ARG` **只在构建期存在**，默认 1，可覆盖。compose 传参靠 `args: DOWNLOAD_MODELS: ${DOWNLOAD_MODELS:-1}`
  （`docker-compose.yml:12-13`，shell 环境变量插值）——正确写法见第七节大坑。
- 下载什么：**只有 embedding 模型 `BAAI/bge-base-zh-v1.5`**（768 维，`src/vector_store.py:26-27`）；
  **rerank cross-encoder 不下载**（默认关：`.env.example` 的 `RERANK_ENABLED=0`，DEPLOY.md:5 说省 ~1GB 内存）。
- 三连 env：构建时临时关 offline 允许联网；`HF_HUB_DISABLE_XET=1` 禁 Xet 协议（国内更稳）；注释（:30）提示源不可达可加 `HF_ENDPOINT` 镜像。
- 下到哪：HF 缓存 `/root/.cache/huggingface`——构建期成为独立镜像层；运行期 compose 用 named volume
  `hf_cache` 挂同路径（`docker-compose.yml:31`），重启不丢。
- 为什么构建期真加载一次：不 load 就不会下完模型，等运行时才加载就撞 `HF_HUB_OFFLINE=1` 失败。

### 2.5 COPY . . 与 .dockerignore：瘦身构建上下文

`COPY . .`（:38）把**构建上下文**整体拷进镜像，上下文越小构建越快镜像越瘦。`.dockerignore` 先砍掉：

| 排除项 | 为什么 |
|---|---|
| `.venv`、`web/node_modules`、`web/.npm-cache` | 本地依赖，镜像里 pip/npm ci 重新装 |
| `web/dist` | 前端产物由 `COPY --from=web`（:39）提供，不要宿主机旧货 |
| `index` | 索引由 entrypoint 重建 + 运行时 bind mount 供给（见 3.4） |
| `.git`、`logs`、`__pycache__`、`*.pyc`、`.DS_Store`、`.claude`、`.vscode`、`eval_results`、`data/users`、`dump.rdb` | 本地杂物，无用还可能藏密钥 |
| `.env` | **密钥文件**！compose 用 `env_file` 在运行期注入，镜像里绝不能有 |

### 2.6 收尾三行：入口 + 健康检查

```dockerfile
RUN chmod +x scripts/docker_entrypoint.sh                        # :42
ENTRYPOINT ["bash", "scripts/docker_entrypoint.sh"]              # :43
EXPOSE 8005                                                      # :45
HEALTHCHECK --interval=30s --timeout=5s --start-period=240s --retries=5 \   # :47
  CMD python -c "...urlopen('http://127.0.0.1:8005/healthz', timeout=3)" || exit 1   # :48
```

- `ENTRYPOINT` = 容器主进程 = 第四节流水线；**没有 CMD**，启动命令由 entrypoint 内部 `exec`。
- `EXPOSE` 只是"声明我监听 8005"（文档性），对外开放靠 compose `ports`。
- `HEALTHCHECK` 给编排层问"你活了吗"：每 30s 打 `/healthz`；**`start-period=240s`** 是宽限期——
  首次启动要加载模型+灌索引，最多给 4 分钟不算死；之后连续 5 次失败（约 2.5 分钟）判 unhealthy。
  探活打的是应用自己的 `/healthz`（`app.py:303-310`：`status: ok` + 任务队列指标）。

---

## 三、docker-compose.yml：三个服务一台戏（全文 73 行）

服务总览表：

| 服务 | 来源 | 对外端口 | 数据卷 | 健康检查 | 重启 |
|---|---|---|---|---|---|
| app | `build .`（DOWNLOAD_MODELS 默认 1） | `8005:8005` | `./index`(bind) + `hf_cache` | `/healthz` | unless-stopped |
| postgres | `postgres:16-alpine` | `127.0.0.1:5432` | `pg_data` + sql 种子(:ro) | `pg_isready` | unless-stopped |
| qdrant | `qdrant/qdrant:latest` | `127.0.0.1:6333/6334` | `qdrant_data` | **无**（有意的） | unless-stopped |

### 3.1 app：主角

```yaml
build: { context: ., args: { DOWNLOAD_MODELS: ${DOWNLOAD_MODELS:-1} } }  # :10-13
ports: ["8005:8005"]                                  # :14-16（注释：加反代时改 127.0.0.1:8005:8005）
env_file: [ .env ]                                    # :17-18 密钥运行期注入，不进镜像
environment:
  HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE = 1          # :20-21
  DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/study1   # :22
  QDRANT_URL=http://qdrant:6333                       # :23
depends_on:
  postgres: { condition: service_healthy }            # :24-26 等 pg_isready 绿才启动
  qdrant:   { condition: service_started }            # :27-28 只等"启动"，真就绪交给 entrypoint
volumes: ["- ./index:/app/index", "- hf_cache:/root/.cache/huggingface"]   # :29-31
restart: unless-stopped                               # :32 崩溃自动拉起，手动停的不拉
healthcheck: [同 Dockerfile :33-38]
```

两个必懂点：
- **`environment` 覆盖 `env_file`**：连接串不在 .env 写死（本机是 `localhost`），由 compose 按**服务名**注入——
  `postgres`/`qdrant` 就是容器网络里的 DNS 主机名（Q3）。
- **`depends_on` 两档**：postgres 走 `service_healthy`（先过健康检查）；qdrant 没配 healthcheck，
  只能 `service_started`，真就绪由 entrypoint 的循环兜底（分工明确，见 3.3）。

### 3.2 postgres：281 条种子数据怎么自动进去的

```yaml
image: postgres:16-alpine                                            # :41
environment: POSTGRES_USER/PASSWORD=postgres, POSTGRES_DB=study1     # :42-46（注释：生产改强密码）
ports: ["127.0.0.1:5432:5432"]   # :47-48 仅本机可连，绝不暴露公网
volumes:
  - pg_data:/var/lib/postgresql/data                                # :50
  - ./sql/001_products.sql:/docker-entrypoint-initdb.d/001_products.sql:ro   # :52
restart: unless-stopped                                             # :53
healthcheck: { CMD-SHELL pg_isready -U postgres, interval 10s, timeout 5s, retries 5 }  # :54-58
```

- 端口只绑 `127.0.0.1`（注释 :48 原话：数据库绝不暴露公网）。
- 种子魔法（注释 :51）：官方 postgres 镜像约定——**空数据卷首次初始化时**执行 `/docker-entrypoint-initdb.d/`
  下所有 .sql。`sql/001_products.sql` 是 pg_dump 产物，第 25 行起一条条 `INSERT INTO public.products (...)`，
  共 **281 条**（数过，sql 全文件 313 行）。**已初始化的卷不重跑**——灌数据只发生一次，靠 `pg_data` 卷记忆。
- healthcheck 用 `pg_isready`：那是 postgres 镜像自带的工具，app 镜像里没有（app 用 asyncpg）——
  所以 app 侧等 PG 换成了 Python 探活（4.2）。

### 3.3 qdrant：故意不配 healthcheck

```yaml
image: qdrant/qdrant:latest       # :62（注释：生产建议 pin 稳定版本如 v1.12.4）
ports: 127.0.0.1:6333 / 6334      # :63-65 REST + gRPC，只绑本机
volumes: - qdrant_data:/qdrant/storage   # :67
restart: unless-stopped           # :68
```

6333 是 REST、6334 是 gRPC。**整段没有 healthcheck**——这就是 app 的 `depends_on` 只要求它
`service_started`（:27-28）的原因，"能不能服务请求"交给 entrypoint 判断。

### 3.4 named volume vs bind mount：数据不丢的秘密

| | named volume | bind mount |
|---|---|---|
| 本例 | `pg_data`、`qdrant_data`、`hf_cache`（:70-73 声明） | `./index:/app/index`（:30） |
| 存在哪 | docker 管理（`/var/lib/docker/volumes/...`） | 你指定的宿主机路径 |
| 容器重建/升级 | 数据原样保留（新容器挂同一卷） | 数据原样保留（同一目录） |
| 谁能直接看 | 只有 docker | 宿主机直接可见可拷 |
| 为什么用它 | 数据库/向量库/模型缓存要与容器生命周期解耦 | 想让宿主机直接看/备份 bm25 索引（注释 :30 原话） |

---

## 四、docker_entrypoint.sh：容器里的自动初始化流水线（全文 62 行）

脚本头注释（`:2-7`）点出动机——**解决部署最大坑：容器 healthz 全绿但业务表空、知识库空**。

### 4.1 `set -euo pipefail`（:8）

任何一步失败立即退出 → 初始化失败 = 容器启动失败 = 编排层看到 unhealthy。**宁可不起来，绝不"起来一个空壳"。**

### 4.2 等 PostgreSQL 就绪（:10-34）

```python
# 内嵌 Python heredoc：用 SQLAlchemy 异步引擎对 DATABASE_URL 反复 SELECT 1
for i in range(60):                                  # :21
    try:
        await conn.execute(text("SELECT 1"))         # :24 最贴近"业务可用"的探活
        print("[init] PostgreSQL 就绪 ✓"); return
    except Exception:
        if i % 5 == 0: print(...)                    # :28-29 每 5 次打一条，别刷屏
        await asyncio.sleep(2)                       # :30
raise SystemExit("[init] PostgreSQL 60 秒内未就绪，退出")   # :31
```

- 为什么不用 `pg_isready`：app 镜像里没有 psql 客户端，而 SQLAlchemy/asyncpg 是应用本来就装的——
  用应用自己的连接串 `SELECT 1`，探的是"业务真能连上"。
- 60 次 × 失败睡 2s ≈ 最长 **120 秒**（报错文案写"60 秒"是笔误，第七节）。
  超时 `SystemExit` → entrypoint 退出 → `restart: unless-stopped` 再拉起，自带一点自愈。
- 和 compose `depends_on: service_healthy` 是**双保险**：正常时秒过，防的是"编排层说健康了但应用还连不上"。

### 4.3 等 Qdrant 就绪（:36-53）

同样的 60×2s 循环，探活是 `urllib.request.urlopen("http://qdrant:6333/healthz")` 期待 200（:43-45）。

### 4.4 建表 + 管理员：一行 `python scripts/create_admin.py`（:55-56）

脚本头注释（`scripts/create_admin.py:12-15`）讲清幂等三件事：
1. **建全部业务表**：内部先调 `ensure_schema()`（create_admin.py:37；`src/db.py:176-181`）。
   所有 DDL 都是 `CREATE TABLE IF NOT EXISTS`（db.py:81,94,113,121,136,144,154,165）→ **重复执行不报错**。
2. **读环境变量**：`ADMIN_USERNAME`（默认 admin）/ `ADMIN_PASSWORD`（**必填**，缺了直接退出 :43-45）/ `ADMIN_NAME`。
3. **创建 or 重置**：查同用户名（:48-50）——已存在 → `UPDATE ... role='admin'` 重置密码（:54-59，兼当重置工具）；
   不存在 → `INSERT` 一个 `role='admin'`（:60-68）；`validate_password` 强校验（:46），弱密码报错。

防越权闭环（create_admin.py:15 原话）：注册接口只允许 customer，**admin 只能从这个脚本出**——容器每次启动
都跑一遍，管理员密码丢了改 .env 重启即可找回。

### 4.5 灌知识索引：`python scripts/build_index.py`（:58-59）

```python
# scripts/build_index.py:61-66
async def build_qdrant(chunks):
    reset_collections()          # 删 textile_knowledge 和 user_memory 两个集合（vector_store.py:85-90）
    await upsert_knowledge(chunks)   # 先 ensure_collections 再 embed + 每 100 条一批 upsert（vector_store.py:98-122）
```

- 数据源 `data/chunks.json`（**142 条**知识块，build_index.py:34；缺失提示先跑 ingest.py :45-48）；
  向量写进 compose 的独立 Qdrant；BM25 稀疏索引 pickle 覆盖写盘 `index/bm25_index.pkl`（:35、:139-151）。
- **每次容器启动都跑，没有跳过开关**（entrypoint 与 build_index.py 里都没有 SKIP 逻辑）。它的"幂等"= **删除重建、
  结果与最新 data/ 一致**，不是"存在就跳过"。
- 诚实说代价（面试主动讲加分）：
  1. `reset_collections` 连 **user_memory（用户偏好记忆）一起删**——每次重启，Qdrant 里沉淀的偏好被清空；
  2. 每次重启要重新 embed 142 条（加载模型 + 计算），冷启动变慢——生产应加"索引未变就跳过"的开关，演示项目没做。

### 4.6 最后一脚：exec 启动 uvicorn（:61-62）

```bash
exec python -m uvicorn app:app --host 0.0.0.0 --port 8005
```

- **`exec`** 让 uvicorn **替换** bash 成为容器 PID 1：`docker stop` 的 SIGTERM 直达 uvicorn → 优雅停机。
- **单进程无 --workers**：演示量级（内存态审批注册表 + 单模型实例，多 worker 反踩坑）。
- `app:app` = 模块:FastAPI 实例，与本地裸跑 `python app.py` 同一入口（`app.py:426-432` 里 `uvicorn.run(...port=8005)`）。

---

## 五、CI：GitHub Actions 进门检查（`.github/workflows/ci.yml`，全文 59 行）

触发（:3-6）：**push 到 main + 所有 PR**。一个 job `test`，ubuntu-latest（:8-10）。按步骤拆：

| 步骤 | 干什么 | 证据与细节 |
|---|---|---|
| 1. service postgres | 起临时 PG | `postgres:16-alpine`（:14），账号 postgres/postgres、库 postgres（:15-18），`--health-cmd pg_isready`（:21-25） |
| 2. 设 env | 测试连哪个库 | `TEST_DATABASE_URL=...@localhost:5432/study1_test`（:27）——连 localhost 因为 service 与 job 同网络；`HF_HUB_OFFLINE: "0"`（:28）允许测试下模型 |
| 3. checkout + setup-python | Python 3.12 + pip 缓存 | :30-35 |
| 4. 缓存 HF 模型 | 避免每次重下模型 | 缓存 `~/.cache/huggingface`，key `hf-ubuntu-bge-v1`（:37-41，注释自称"600MB"） |
| 5. pip install | 装后端依赖 | :43-44 |
| 6. 前端构建 | 验证 web/ 能编译 | cd web → `npm ci` + `npm run build`（:46-50），只验证不发包 |
| 7. pytest | 单元测试 | `python -m pytest tests/ -q`（:55） |
| 8. py_compile | 语法兜底 | app.py / src/agent.py / src/stream_chat.py（:57-59） |

测试连库（conftest 联动）：`tests/conftest.py:19-23` 在 import `src.db` **之前**把 `DATABASE_URL`
setdefault 成 `TEST_DATABASE_URL`；再从连接串解析凭据（conftest.py:45-46）、连 PG 后幂等
`CREATE DATABASE study1_test`（conftest.py:57-61，不存在才建）——**独立测试库与开发库 study1 隔离**，本机（rain 无密码）与 CI（postgres/postgres）都兼容。

为什么只起 postgres 不起 qdrant？pytest 步骤注释写得很直白（:53-54）：**Qdrant 用 LocalMode
（`index/qdrant_storage` 随仓库提交），无需起 Qdrant 服务**——测试期向量库 = git 里那份本地嵌入式数据
（`src/vector_store.py:37-49`：QDRANT_URL 未设置 → LocalMode）。注意 `index/` 在 `.dockerignore` 被排除，
但 CI 是 git checkout 不是 docker build，所以拿得到。

**CI 没有的东西**（照实说）：没有评测 job、没有 artifact 上传、不构建镜像。评测是本地/手动跑
`scripts/eval_retrieval.py`、`eval_agent.py`、`eval_judge.py`，结果 JSON 提交在 `eval_results/`，
README 评估表（README.md:100-108）即来自那里（取舍见 Q8）。

---

## 六、本地开发 vs 生产部署：一张表看穿

| 维度 | 本地开发 | 生产（docker compose） |
|---|---|---|
| 后端 | `python app.py`（uvicorn.run，8005，app.py:426-432） | entrypoint → `exec uvicorn`（8005） |
| 前端 | `cd web && npm run dev` → vite **5173 热更新**（app.py:416 注释）；没 build 时 `/` 返回"前端未构建"提示（app.py:420-423） | 阶段 1 构建的 `web/dist`，app.py 最后 `mount StaticFiles`（app.py:417-419，必须最后挂载否则吞掉 /chat /api） |
| 业务库 | 本机 PG `study1`（.env 里 localhost 串） | postgres 容器 + `pg_data` 卷（连接串用服务名） |
| 向量库 | 可 LocalMode（`index/qdrant_storage`，零依赖）或 QDRANT_URL 指独立服务 | 独立 qdrant 容器 + `qdrant_data` 卷 |
| 数据库初始化 | 手动三步：migrate / create_admin / build_index | entrypoint 全自动 |
| 模型 | 本机 HF 缓存，offline 可开可关 | 构建期下载 + `hf_cache` 卷，运行时强制 offline |
| 别人怎么访问 | `127.0.0.1` 自己玩 | 服务器 IP:8005；正式走域名 + HTTPS 反代（DEPLOY.md:89-120，反代时 app 端口改绑 127.0.0.1:8005:8005） |

核心差异一句话：**本地是"源码形态"（vite 热更 + 随时改代码），生产是"产物形态"（dist + 冻结镜像 + 卷里长命的数据）**——同一套 Python、同一个 8005，差在"谁来构建前端、谁来初始化数据"。

---

## 七、文档与代码不一致点（诚实清单）

1. **模型体积对不上**：`Dockerfile:29/34`、`docker-compose.yml:3`、`docs/DEPLOY.md:66` 口径 **~0.4GB**；
   `README.md:115` 与 `ci.yml:37` 注释写 **~600MB**。实际构建期只下 `bge-base-zh-v1.5` embedding（~0.4GB），rerank 不下载。
2. **README/compose 注释教的跳过下载命令是错的**：`README.md:119-120` 与 `docker-compose.yml:4` 写
   `docker compose up --build --build-arg DOWNLOAD_MODELS=0`——实测 `docker compose up` **不支持 `--build-arg`**（help 只有 `--build`）；
   文件内真正机制是环境变量插值 `args: DOWNLOAD_MODELS: ${DOWNLOAD_MODELS:-1}`（docker-compose.yml:12-13）。
   正确姿势：`DOWNLOAD_MODELS=0 docker compose up --build`，或 `docker compose build --build-arg DOWNLOAD_MODELS=0 && docker compose up`。
   （README 同行的 `-v ~/.cache/...` 是 `docker run` 语法，compose up 也不认。）
3. **Python 版本漂移**：容器 `python:3.11-slim`（Dockerfile:15）vs CI `3.12`（ci.yml:34）vs README 自述 3.12。
4. **entrypoint 超时文案**：报错写"60 秒内未就绪"（docker_entrypoint.sh:31/:50），实际 60 次 × 2s ≈ **120 秒**。
5. **"~0.4GB"仅指 embedding**：若开 `RERANK_ENABLED=1`，本地 CrossEncoder 还要 ~1GB 内存（.env.example、DEPLOY.md:5），Dockerfile 没打包它。
6. **每次重启重灌索引并清 user_memory**：entrypoint 无条件跑 build_index（:58-59），它先 `reset_collections()`
   删两个集合（vector_store.py:85-90）——重启即清偏好记忆 + 冷启动重 embed，是"自动初始化"换来的真实代价。

---

## Q&A

**Q1：为什么 Dockerfile 一定要"先 COPY requirements.txt 单独 pip install，再 COPY 源码"？**
镜像构建逐层缓存：某层没变，它和下面的层直接复用。`requirements.txt` 极少变，放最底层意味着日常改业务代码
只重跑 `COPY . .` 之后的层，pip 层命中缓存几秒出镜像；反过来先 COPY 源码，改一行就依赖层全失效、每次重装。
模型下载放 COPY 之前同理（Dockerfile:28-30 注释原话"不会每次重下 0.4GB 模型"）。这就是 Docker 层缓存的基本工程题。

**Q2：entrypoint 为什么每一步都要幂等？重复执行（重启容器）会怎样？**
容器随时可能重启，初始化必须"重跑不出事且结果正确"：建表用 `CREATE TABLE IF NOT EXISTS`（db.py:81 等）；
管理员存在则 UPDATE 重置密码并确保 admin、不存在则 INSERT（create_admin.py:54-68）——改 .env 里 ADMIN_PASSWORD
重启即换密；索引删除重建（build_index.py:64-65），与最新 data/ 一致。代价：每次重启 `reset_collections`
连 user_memory 偏好集合一起删（vector_store.py:85-90），且 142 条要重新 embed——"幂等 = 可重复、结果一致"，
不等于"没变化不干活"，生产要的是后者（第七节第 6 条）。

**Q3：为什么容器里 DATABASE_URL/QDRANT_URL 用 postgres/qdrant 而不是 localhost？**
compose 默认给所有服务建桥接网络，**服务名即容器网络 DNS 主机名**（docker-compose.yml:22-23）。容器里
`localhost` 指容器自己，连不到别的容器；容器重建后 IP 会变，写死 IP 一重启就断，服务名永远解析到当前容器。
本地开发没有容器网络，.env 里自然是 `localhost`——compose 用 `environment` 覆盖 `env_file` 正是为区分两套环境。

**Q4：要备份哪些数据？named volume 怎么备份/迁移？**
必备份：`pg_data`（订单/用户/审计/对话，丢了业务就没了）、`qdrant_data`（知识向量，可靠 build_index 重建但麻烦）、
`./index/bm25_index.pkl`（bind mount 直接拷目录）。`hf_cache` 是模型缓存，重下即可不用备。
named volume 备份/迁移标准姿势是借临时容器打包：
`docker run --rm -v pg_data:/data -v $(pwd):/backup alpine tar czf /backup/pg_data.tar.gz -C /data .`；
新机器上反向 `tar xzf` 解回卷后再 `docker compose up -d`。

**Q5：如果 DOWNLOAD_MODELS=0 而本地又没有模型缓存，会怎样？**
构建期跳过下载（Dockerfile:32 的 if），镜像里没模型层、`hf_cache` 卷也空；运行时 `HF_HUB_OFFLINE=1`
（docker-compose.yml:20-21）禁联网——entrypoint 里 build_index 加载 `SentenceTransformer('BAAI/bge-base-zh-v1.5')`
（vector_store.py:52-57）找不到模型直接抛错，`set -euo pipefail`（entrypoint:8）让脚本退出 → 容器起不来。
这正是 compose 注释（:4）写"跳过模型下载（挂载 HF 缓存）"的原因：DOWNLOAD_MODELS=0 **必须配套**已有模型的缓存卷，否则自断粮草。

**Q6：compose 里想改端口（8005 换 9000），要注意什么？**
至少同步四处：① entrypoint 的 uvicorn `--port 8005`（docker_entrypoint.sh:62）与 app.py:432 本地默认；
② compose `ports` 右半 `8005:8005`（:16）；③ **两处 healthcheck 写死的 `http://127.0.0.1:8005/healthz`**
（Dockerfile:48 与 docker-compose.yml:34）；④ 外部引用（安全组、反代目标，DEPLOY.md:91-92 建议反代时改 `127.0.0.1:8005:8005`）。
先分清"容器内监听端口 ≠ 对外映射端口"（`9000:8005` 也可以：healthcheck 打容器内 8005 仍有效），再动手改。

**Q7：CI 为什么只起 postgres service，不起 qdrant service？**
pytest 步骤注释给了答案（ci.yml:53-54）：测试用 Qdrant **LocalMode**，`index/qdrant_storage` **随仓库提交**
（git 检出即有），向量测试读本地嵌入式数据，零网络依赖更快更稳；而 SQLAlchemy async + 事务 + 行级隔离要真库才测得出，
且 Actions 的 service 容器现成低成本（:13-25）。注意这依赖"index/ 已提交进 git"的约定——若哪天把索引目录移出 git，
CI 这条 LocalMode 路径就断了，得改起 qdrant 服务。

**Q8：CI 只跑测试不跑评测，评测在哪跑？为什么这么分？**
按 ci.yml 实际内容：只有 `test` 一个 job（pytest + 前端构建 + py_compile），**没有评测 job、没有 artifact**。
评测本地/手动跑：`scripts/eval_retrieval.py`（检索 MRR/Hit@3）、`eval_agent.py`（端到端规则断言）、
`eval_judge.py`（LLM-as-Judge），结果 JSON 提交进 `eval_results/`，README 评估表（README.md:100-108）是静态呈现。
为什么不放 CI：评测要真调 LLM（花钱、慢、输出有随机性），指标还跟模型/数据版本耦合，每次 PR 都跑又贵又红得莫名其妙；
离线测试（快、确定性）放 CI 当进门检查，评测做发布前手动/定时关卡是常见取舍——面试可以说"更严谨可加 nightly job 跑评测归档结果"。
