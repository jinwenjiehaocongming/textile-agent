# MCP (Model Context Protocol) 详解

## 一句话

MCP 就是 **LLM 调用工具的 USB 标准**。就像 USB 统一了电脑和鼠标键盘的连接方式，MCP 统一了 LLM 和外部工具/数据的连接方式。

## 没有 MCP 之前（你现在做的）

```python
# 每个 LLM 厂商的工具格式不同
# OpenAI/DeepSeek:
SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "search_product",
        "parameters": {"type": "object", "properties": {...}}
    }
}

# Anthropic (Claude) 是另一种格式:
# {"name": "search_product", "input_schema": {...}}

# Google Gemini 又另一种...
```

换个 LLM → 得改 Schema 格式 → 得改调用方式 → 得改工具执行逻辑。这叫 **工具格式碎片化**。

## 有了 MCP 之后

```
你的代码
  ↓ 标准 MCP 接口（跟 LLM 无关）
  MCP Server（跑你的工具：查产品、下订单）
  ↓ MCP 协议
  MCP Client（Claude/GPT/DeepSeek 都支持）
  ↓
LLM 调用工具
```

换 LLM 只需换 Client，Server 不改。一套工具定义通吃所有模型。

## MCP 三层架构

| 层 | 干什么 | 谁实现 |
|------|------|------|
| **MCP Host** | AI 应用（Claude Desktop / VSCode / 你的 Agent） | 你 |
| **MCP Client** | 与 Server 通信，转发 LLM 的工具调用请求 | 框架/你 |
| **MCP Server** | 真正执行工具的一方（查数据库、调 API） | 你 |

## 你项目如果改成 MCP

```
现在:
  Agent → search_product(json) → SQLite
  Agent → create_order(json)    → SQLite

MCP 版:
  Agent → MCP Client → MCP Server → search_product → SQLite
                                      create_order → SQLite
```

多了中间层，但好处是：
1. 换 LLM（从 DeepSeek 换到 Claude）→ 只换 Client，工具逻辑不动
2. Server 可以部署在不同机器上（工具和执行分离）
3. 工具发现：LLM 启动时自动列出所有可用工具，不需要你手动 bind

## Transport 两种模式

| 模式 | 怎么通 | 适合 |
|------|--------|------|
| **stdio** | 标准输入/输出，进程间通信 | 本地开发，Claude Desktop 用这个 |
| **HTTP + SSE** | HTTP 请求 + 服务端推送 | 生产部署，微服务 |

## MCP 不是你非要用的

你的项目不需要 MCP。什么时候用：
- 你和别人共享工具（他用 GPT 你用 DeepSeek，同一个工具）
- 工具要独立部署（跨机调用）
- 换 LLM 频繁
- 公司要求标准协议

你现在的做法（手写 JSON Schema）对单一 LLM 场景已经够了。面试时这样回答：

> "我目前项目里用的是手动 JSON Schema 定义工具——跟 MCP 的思路一致，都是标准化的工具描述，不依赖特定 LLM 的装饰器。MCP 本质上是把这个思路扩展到跨 LLM 厂商、跨部署场景，加了服务发现和标准化的 transport 层。"
