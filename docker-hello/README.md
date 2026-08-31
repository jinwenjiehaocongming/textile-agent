# docker-hello — Docker 最小学习例子

3 个文件跑通 Docker 完整闭环：**构建 → 运行 → 访问 → 进容器 → 停止**。

## 文件说明

| 文件 | 作用 |
|---|---|
| `app.py` | 一个最小的 FastAPI 应用（容器里跑的就是它） |
| `requirements.txt` | 依赖清单 |
| `Dockerfile` | 把 app.py 打包成镜像的「菜谱」（每行 = 一层） |

## 剧本（照抄即可）

```bash
# 1. 构建镜像（-t = 起名字；末尾 . = 从当前目录取材料）
docker build -t hello-docker .

# 2. 看镜像（模板）
docker images

# 3. 运行容器（-p 8000:8000 = 你电脑的8000号门 ↔ 容器里的8000号门）
#    这条会占住终端，正常
docker run -p 8000:8000 hello-docker

# 4. 新开一个终端，访问它（你本机没装 fastapi 但它能跑！）
curl http://127.0.0.1:8000/

# 5. 看运行中的容器
docker ps

# 6. 钻进容器看「另一个世界」（ID 用上一步的）
docker exec -it <容器ID前几位> bash
#   进去后：ls /app、python -c "import fastapi" 试试，然后 exit

# 7. 停掉（回第 3 步终端按 Ctrl+C）
docker ps

# 8. 感受「一个镜像 = 多个独立实例」
docker run -d -p 8001:8000 hello-docker   # -d = 后台运行
curl http://127.0.0.1:8001/               # 也通！
docker stop <新容器ID>
```

## 操作 ↔ 概念对照表

| 操作 | 概念 | 一句话记法 |
|---|---|---|
| `docker build` | 镜像 | 打包好的环境模板（蓝图） |
| `docker images` | 查看模板 | 本机有哪些蓝图 |
| `docker run` | 容器 | 模板的运行实例（类 → 对象） |
| `-p 8000:8000` | 端口映射 | 给独立世界开一扇门 |
| `docker ps` | 运行中的实例 | 现在谁在跑 |
| `docker exec` | 进入实例 | 钻进容器看内部世界 |
| Ctrl+C / `docker stop` | 停止实例 | 实例没了，模板还在 |

## 看完后回去看项目 Dockerfile

项目的 `Dockerfile` = 这个最小版 + 三样：

1. `ENV` 三行 —— 容器启动时预设环境变量（关缓冲、HF 离线）
2. `ARG DOWNLOAD_MODELS` + 条件 RUN —— 构建时决定要不要预下载 600MB 模型
3. `HEALTHCHECK` —— 让 Docker 定期敲 `/healthz` 判断容器是否「生病」

`docker-compose.yml` = 把三个 `docker run`（app + postgres + qdrant）写进一个文件，
一次 `docker compose up` 全起来。
