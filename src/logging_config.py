"""
统一日志配置
==============
- 输出到 stderr（不污染 stdout；MCP Server 的 stdout 是 JSON-RPC 协议通道）
- 同时写入 logs/app.log（滚动，5MB × 3 个备份）
- 各模块用 get_logger(__name__) 获取，模块导入即自动配置（幂等）

用法:
    from src.logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("...")
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "logs"
_FMT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
_configured = False


def get_logger(name: str) -> logging.Logger:
    """获取模块级 logger。模块导入时会自动 setup，幂等。"""
    return logging.getLogger(name)


def setup_logging(level: int = None) -> None:
    global _configured
    if _configured:
        return
    _configured = True

    # 级别优先级：显式参数 > 环境变量 LOG_LEVEL > 默认 INFO
    if level is None:
        level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # 降噪：第三方库只记录 WARNING 以上，避免日志被 HTTP 请求/模型加载刷屏
    for noisy in ("httpx", "httpcore", "urllib3", "chromadb", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    formatter = logging.Formatter(_FMT, datefmt="%H:%M:%S")

    # 1. 控制台 → stderr
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(formatter)
    root.addHandler(sh)

    # 2. 文件 → logs/app.log（滚动）
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)


# 模块导入即配置（幂等）
setup_logging()
