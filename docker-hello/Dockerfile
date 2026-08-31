# ============================================
# 最简单的 Dockerfile：把 app.py 打包成镜像
# 每一行 = 盖楼的一层，从下往上盖
# ============================================

# 第 1 步：选地基 —— 一个自带 Python 3.11 的干净系统
FROM python:3.11-slim

# 第 2 步：在容器里创建目录 /app，之后所有操作都在这里
WORKDIR /app

# 第 3 步：把依赖清单拷进镜像（先拷清单，才能让第 4 步利用缓存）
COPY requirements.txt .

# 第 4 步：安装依赖（装进镜像后，容器里自带 fastapi/uvicorn）
#   -i 清华 PyPI 镜像：国内直连快（默认 PyPI 源在国外，容器里访问会超时）
RUN pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 第 5 步：把代码拷进镜像
COPY . .

# 第 6 步：容器启动时执行的命令
#   注意 --host 0.0.0.0：容器是独立世界，默认只听自己内部的 127.0.0.1，
#   不写 0.0.0.0 的话，你在宿主机上永远访问不到它
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
