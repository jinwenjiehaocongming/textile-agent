"""
同一种功能，两种写法对比

功能：search_product — 查产品库存和报价

方式 A：@tool 装饰器
方式 B：手动 JSON Schema（production 推荐）
"""

PRODUCTS = [
    {"id": "P003", "name": "T400 复合弹力布", "color": "黑色", "price": 12.5, "stock": 6000, "moq": 800},
    {"id": "P004", "name": "T400 四面弹",     "color": "黑色", "price": 15.8, "stock": 3000, "moq": 800},
    {"id": "P001", "name": "300T 春亚纺",     "color": "藏青色", "price": 8.5,  "stock": 8000, "moq": 500},
]

# ============================================================================
# 方式 A：@tool 装饰器 — Schema 自动推断，省事
# ============================================================================
from langchain_core.tools import tool
from langchain_core.messages import ToolMessage

# ---------- A1. 定义工具 ----------
@tool
def search_product_a(query: str) -> str:
    """
    查询产品库存和报价。用于客户询问价格、库存、MOQ、交期。
    输入：产品名、颜色、品类等关键词。
    """
    keywords = query.replace("，", " ").replace(",", " ").split()
    matched = []
    for p in PRODUCTS:
        text = f"{p['name']} {p['color']}"
        score = sum(1 for kw in keywords if kw.lower() in text.lower())
        if score > 0:
            matched.append(p)
    if not matched:
        return "未找到匹配产品"
    return "\n".join(
        f"{p['name']} | {p['color']} | ¥{p['price']}/米 | 库存{p['stock']}米"
        for p in matched[:5]
    )

# ---------- A2. 绑定 + 调用 ----------
# LangChain 自动从函数签名推断 Schema，你不需要写
# llm_a = ChatOpenAI(...).bind_tools([search_product_a])  # 需要 API key，注释掉
# 调用：search_product_a.invoke({"query": "T400"})


# ============================================================================
# 方式 B：手动 JSON Schema — 完全控制每个参数
# ============================================================================

# ---------- B1. 写一个普通函数（不 import langchain 都能跑）----------
def search_product_b(query: str) -> str:
    keywords = query.replace("，", " ").replace(",", " ").split()
    matched = []
    for p in PRODUCTS:
        text = f"{p['name']} {p['color']}"
        score = sum(1 for kw in keywords if kw.lower() in text.lower())
        if score > 0:
            matched.append(p)
    if not matched:
        return "未找到匹配产品"
    return "\n".join(
        f"{p['name']} | {p['color']} | ¥{p['price']}/米 | 库存{p['stock']}米"
        for p in matched[:5]
    )

# ---------- B2. 手动写 Schema ----------
SEARCH_PRODUCT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_product",
        "description": "查询产品库存和报价。用于客户询问价格、库存、MOQ、交期。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "产品名、颜色或品类关键词，如 'T400 黑色'",
                }
            },
            "required": ["query"],
        },
    },
}

# ---------- B3. 绑定 ----------
# llm_b = ChatOpenAI(...).bind_tools([SEARCH_PRODUCT_SCHEMA])  # 需要 API key，注释掉
# 注意：bind_tools 传的是 字典 Schema，不是函数


# ============================================================================
# agent_node — 两种方式的伪代码对比（实际运行需要 LLM 实例）
# ============================================================================
# A: response = llm_a.invoke([system] + state["messages"])
# B: response = llm_b.invoke([system] + state["messages"])
# agent_node 内部两种方式完全一样，区别只在 llm 绑定方式不同


# ============================================================================
# tool_executor — 这里调用方式不同
# ============================================================================

# ---------- A 方式 ----------
def tool_executor_a(state):
    last_msg = state["messages"][-1]
    results = []
    for tc in last_msg.tool_calls:
        if tc["name"] == "search_product":
            result = search_product_a.invoke(tc["args"])    # ← .invoke() 调用
            results.append(ToolMessage(content=result, tool_call_id=tc["id"]))
    return {"messages": state["messages"] + results}

# ---------- B 方式 ----------
def tool_executor_b(state):
    last_msg = state["messages"][-1]
    results = []
    for tc in last_msg.tool_calls:
        if tc["name"] == "search_product":
            result = search_product_b(**tc["args"])          # ← 直接调普通函数
            results.append(ToolMessage(content=result, tool_call_id=tc["id"]))
    return {"messages": state["messages"] + results}


# ============================================================================
# 如果想加约束，方式 B 可以这样写 Schema（方式 A 做不到）
# ============================================================================
SCHEMA_STRICT = {
    "type": "function",
    "function": {
        "name": "search_product",
        "description": "查询产品",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "面料名称，如 T400、牛津布",
                },
                "color": {
                    "type": "string",
                    "enum": ["黑色", "白色", "藏青色", "深蓝色", "军绿色"],
                    "description": "颜色，必须是 enum 中之一，LLM 编造直接拒绝",
                },
                "action": {
                    "type": "string",
                    "enum": ["check_price", "check_stock", "full_detail"],
                    "description": "查询类型：只看价格 / 只看库存 / 全部信息",
                },
            },
            "required": ["name"],
        },
    },
}


# ============================================================================
# 总结
# ============================================================================
# ============================================================================
# Schema 示例集：不同场景的工具定义
# ============================================================================

# ---- 例1: 最简单的 — 一个字符串参数 ----
def get_weather(city: str) -> str:
    return f"{city}天气晴 25°C"

GET_WEATHER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的实时天气",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "城市名，如 '北京'、'上海'",
                }
            },
            "required": ["city"],
        },
    },
}
# LLM 会这样调: get_weather(city="杭州")

# ---- 例2: 多个参数 ----
def create_order(product_name: str, quantity: int, color: str) -> str:
    return f"下单成功: {product_name} {color} x{quantity}米"

CREATE_ORDER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_order",
        "description": "为客户创建面料采购订单",
        "parameters": {
            "type": "object",
            "properties": {
                "product_name": {
                    "type": "string",
                    "description": "面料名称，如 'T400 复合弹力布'",
                },
                "quantity": {
                    "type": "integer",
                    "description": "订购数量（米）",
                },
                "color": {
                    "type": "string",
                    "description": "颜色",
                },
            },
            "required": ["product_name", "quantity"],
        },
    },
}
# LLM: create_order(product_name="T400弹力布", quantity=500, color="黑色")

# ---- 例3: 枚举约束 — 颜色/状态只能选固定的 ----
def update_order_status(order_id: str, status: str) -> str:
    return f"订单 {order_id} → {status}"

UPDATE_ORDER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_order_status",
        "description": "更新订单状态",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "订单编号",
                },
                "status": {
                    "type": "string",
                    "enum": ["待付款", "生产中", "已发货", "已签收", "已取消"],
                    "description": "新状态，只能选 enum 中的值",
                },
            },
            "required": ["order_id", "status"],
        },
    },
}
# LLM 必须从 enum 选，传 "快好了" 或 "pending" 直接报错

# ---- 例4: 数字范围约束 ----
def filter_products(min_price: float = 0, max_price: float = 999) -> str:
    return f"价格 {min_price}-{max_price} 的产品"

FILTER_PRODUCTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "filter_products",
        "description": "按价格范围筛选面料产品",
        "parameters": {
            "type": "object",
            "properties": {
                "min_price": {
                    "type": "number",
                    "description": "最低单价（元/米），默认 0",
                },
                "max_price": {
                    "type": "number",
                    "description": "最高单价（元/米），默认 999",
                },
            },
            "required": [],   # 两个都不强制，有默认值
        },
    },
}
# LLM: filter_products(max_price=10) 或 filter_products(min_price=5, max_price=15)

# ---- 例5: 数组参数 — 传列表 ----
def batch_query(product_ids: list) -> str:
    return f"查询 {len(product_ids)} 个产品"

BATCH_QUERY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "batch_query",
        "description": "批量查询多个产品的库存",
        "parameters": {
            "type": "object",
            "properties": {
                "product_ids": {
                    "type": "array",
                    "description": "产品货号列表",
                    "items": {
                        "type": "string",
                        "description": "货号，如 'P001'",
                    },
                },
            },
            "required": ["product_ids"],
        },
    },
}
# LLM: batch_query(product_ids=["P001", "P003", "P005"])

# ---- 例6: 布尔参数 ----
def check_availability(product_name: str, include_similar: bool = False) -> str:
    return f"查 {product_name}，相似={include_similar}"

CHECK_AVAIL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "check_availability",
        "description": "检查面料是否有现货",
        "parameters": {
            "type": "object",
            "properties": {
                "product_name": {
                    "type": "string",
                    "description": "面料名称",
                },
                "include_similar": {
                    "type": "boolean",
                    "description": "是否包含相似替代品（没货时推荐类似的）",
                },
            },
            "required": ["product_name"],
        },
    },
}
# LLM: check_availability(product_name="T400", include_similar=True)


# ============================================================================
# 字段类型速查表
# ============================================================================
SCHEMA_TYPE_REFERENCE = """
Schema 支持的字段类型:

  "type": "string"    字符串  → "T400"、"黑色"
  "type": "integer"   整数    → 500、1000
  "type": "number"    数字    → 12.5、99.9
  "type": "boolean"   布尔    → true / false
  "type": "array"     数组    → ["P001", "P003"]
  "type": "object"    对象    → {"key": "value"}

Schema 支持的约束:

  "enum": [...]            只能从这些值中选
  "description": "..."     告诉 LLM 这个字段什么意思
  "required": [...]        哪些字段必须传
  "items": {...}           array 里每个元素什么类型
"""

print(SCHEMA_TYPE_REFERENCE)

print("""
对比总结：

                  方式 A (@tool)              方式 B (JSON Schema)
────────────────────────────────────────────────────────────────────
定义工具   @tool 装饰器                 普通 Python 函数 + 手写 Schema
Schema     自动从函数签名推断            你写什么 LLM 就用什么
参数约束   类型自动                      类型 + enum + 描述 全手控
调用方式   func.invoke(dict)            func(**dict)
绑定方式   bind_tools([函数对象])         bind_tools([Schema字典])
颜色限制   做不到                        {"enum": ["黑色","白色"]}
查询类型   做不到                        {"enum": ["check_price",...]}
复杂验证   做不到                        可以写 {"minimum": 0, "maximum": 999}
适用场景   快速原型、工具少               生产环境、需要精确控制
""")
