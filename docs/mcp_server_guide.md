# FastAPI + LangGraph MCP Server 对外提供与集成指南

本文档介绍如何在本项目中运行、配置和向外部客户端（如 Cursor、Claude Desktop、Antigravity、自定义 Agent 等）暴露 **Model Context Protocol (MCP)** 服务端。

---

## 1. 架构概览

本项目 MCP Server 基于官方 `mcp` SDK 与 `FastMCP` 构建，位于 `app/mcp` 目录下，具备以下特性：

- **双模传输支持 (Dual-Mode Transport)**：
  - **本地 Stdio 模式**：通过标准输入输出与本地客户端进程管道直接通信（零网络开销，配置最简）。
  - **FastAPI SSE / HTTP 挂载模式**：作为 ASGI 路由挂载在 FastAPI 主服务（`/mcp` 端点）中，支持远程网络调用。
- **全功能 MCP 原语支持**：
  - **Tools（工具）**：知识库混合检索 (`knowledge_search`)、系统健康探针 (`get_system_health`)、安全数学计算器 (`execute_calculator`)、订单分析统计 (`query_order_summary`)。
  - **Resources（资源）**：服务元数据 (`server://info`)、实时系统指标 (`server://system_metrics`)。
  - **Prompts（提示词）**：Agent 任务拆解规划师 (`agent_task_planner`)、SQL 分析专家 (`sql_expert_analyzer`)。
- **安全鉴权 (Authentication)**：
  - 支持配置 `MCP_SERVER_API_KEY`，对远程 HTTP/SSE 访问强制实施 Header 或 Query 校验（本地 Stdio 模式免密）。

---

## 2. 运行与启动方式

### 方式一：随 FastAPI 主服务一同启动（推荐，网络访问）

当通过常规方式启动后端 API 时，MCP Server 会自动挂载至 `/mcp` 路径：

```bash
# 开发环境热重载启动
make dev
# 或
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- **SSE 建立连接端点**：`http://localhost:8000/mcp/sse`
- **消息发送端点**：`http://localhost:8000/mcp/messages/`
- **API 元数据查看**：访问 `http://localhost:8000/` 可直接看到 `mcp_sse_url` 字段。

---

### 方式二：独立命令行运行（Stdio 模式，供桌面 IDE 接入）

```bash
# 方式 2.1：默认以 stdio 模式启动（供 IDE 子进程拉起）
uv run python -m app.mcp

# 方式 2.2：显式指定 stdio
uv run python -m app.mcp --transport stdio

# 方式 2.3：作为独立 SSE 进程启动在指定端口
uv run python -m app.mcp --transport sse
```

---

## 3. 外部客户端配置指南

### 3.1 Cursor IDE 配置

在项目根目录或全局的 `.cursor/mcp.json`（或在 Cursor Settings -> MCP 中添加）：

#### 方式 A：Stdio 本地拉起（推荐）
```json
{
  "mcpServers": {
    "fastapi-langgraph-agent": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "d:/Learn/fastapi-langgraph-agent-zh-master",
        "python",
        "-m",
        "app.mcp"
      ]
    }
  }
}
```

#### 方式 B：SSE 远程连接（连接已启动的 FastAPI 服务）
```json
{
  "mcpServers": {
    "fastapi-langgraph-remote": {
      "url": "http://localhost:8000/mcp/sse",
      "headers": {
        "x-api-key": "your-mcp-api-key-if-configured"
      }
    }
  }
}
```

---

### 3.2 Claude Desktop 配置

打开 Claude Desktop 配置文件：
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`

编辑添加以下内容：

```json
{
  "mcpServers": {
    "fastapi-langgraph-mcp": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "d:\\Learn\\fastapi-langgraph-agent-zh-master",
        "python",
        "-m",
        "app.mcp"
      ]
    }
  }
}
```

---

### 3.3 Antigravity / Windsurf / 其他 Agent 框架配置

在 Antigravity 或通用 MCP 配置文件中添加：

```json
{
  "mcpServers": {
    "agent-backend": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "d:/Learn/fastapi-langgraph-agent-zh-master",
        "python",
        "-m",
        "app.mcp"
      ]
    }
  }
}
```

---

## 4. 环境变量与安全鉴权

在 `.env` 或 `.env.development` 中支持以下配置：

| 环境变量 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `MCP_SERVER_ENABLED` | `true` | 是否启用 MCP Server 挂载 |
| `MCP_SERVER_NAME` | `fastapi-langgraph-mcp` | MCP 服务注册名称 |
| `MCP_SERVER_PREFIX` | `/mcp` | FastAPI 中挂载的 URL 路由前缀 |
| `MCP_SERVER_API_KEY` | `""` (留空) | 远程 HTTP/SSE 访问的密钥，设置后外部请求必须带上该 Key |

### API Key 传递方式（当设置了 `MCP_SERVER_API_KEY` 时）

外部客户端可通过以下任意一种方式提供密钥：
1. **HTTP 请求头**：`x-api-key: <YOUR_KEY>`
2. **Authorization 头**：`Authorization: Bearer <YOUR_KEY>`
3. **URL Query 参数**：`http://localhost:8000/mcp/sse?api_key=<YOUR_KEY>`

---

## 5. 自动化测试与验证

本项目内置了完整的 MCP 诊断与测试脚本：

```bash
uv run pytest tests/test_mcp_server.py -v
```

执行后将运行完整的 MCP 单元与集成测试，逐项验证 Tools、Resources、Prompts、安全鉴权中间件以及 FastAPI 端点挂载状态。
