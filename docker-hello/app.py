# 最小的 FastAPI 应用：容器里跑的就是这个
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {"message": "Hello Docker! 我是容器里的 Python"}
