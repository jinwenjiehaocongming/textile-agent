"""
pytest 全局 fixtures
====================
- 临时 products/orders 数据库（隔离，不污染真实 data/*.db）
- 临时用户记忆目录
"""
import sqlite3
import sys
from pathlib import Path

import pytest

# 确保项目根在 sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PRODUCTS_SCHEMA = """
CREATE TABLE products (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT,
    color TEXT,
    width INTEGER,
    weight TEXT,
    stock INTEGER,
    moq INTEGER,
    price REAL,
    delivery_days INTEGER
);
"""

ORDERS_SCHEMA = """
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT UNIQUE NOT NULL,
    customer_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    color TEXT,
    quantity INTEGER NOT NULL,
    unit_price REAL NOT NULL,
    total REAL NOT NULL,
    status TEXT DEFAULT '待付款',
    created_at TEXT NOT NULL,
    paid_at TEXT,
    shipped_at TEXT,
    phone TEXT,
    address TEXT,
    delivery_date TEXT
);
CREATE TABLE refunds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT DEFAULT '待审核',
    created_at TEXT NOT NULL
);
"""


@pytest.fixture
def tmp_products_db(tmp_path, monkeypatch):
    """临时 products.db，注入 2 条测试产品"""
    from src.mcp_servers import product_server
    db = tmp_path / "products.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(PRODUCTS_SCHEMA)
    conn.execute(
        "INSERT INTO products (id, name, category, color, width, weight, stock, moq, price, delivery_days) "
        "VALUES ('P001', 'T400 复合弹力布', '弹力布', '黑色', 150, '100D', 5000, 1000, 13.2, 7)"
    )
    conn.execute(
        "INSERT INTO products (id, name, category, color, width, weight, stock, moq, price, delivery_days) "
        "VALUES ('P002', '380T 尼丝纺', '尼丝纺', '白色', 150, '380T', 3000, 800, 11.9, 5)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(product_server, "DB_PATH", db)
    return db


@pytest.fixture
def tmp_orders_db(tmp_path, monkeypatch):
    """临时 orders.db（含 orders + refunds 表）"""
    from src.mcp_servers import order_server, refund_server
    db = tmp_path / "orders.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(ORDERS_SCHEMA)
    conn.close()
    monkeypatch.setattr(order_server, "ORDERS_DB", db)
    monkeypatch.setattr(refund_server, "ORDERS_DB", db)
    return db


@pytest.fixture
def tmp_user_dir(tmp_path, monkeypatch):
    """临时用户记忆目录"""
    import src.memory as memory
    user_dir = tmp_path / "users"
    monkeypatch.setattr(memory, "DB_DIR", user_dir)
    return user_dir
