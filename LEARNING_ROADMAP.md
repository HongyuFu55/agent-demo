# Agent Tools 开发学习路线图

基于当前项目（FastAPI + LangGraph + MCP）的迭代学习路径，从简到难分为 4 个阶段。

---

## 当前项目已有的工具

| 工具 | 类型 | 说明 |
|---|---|---|
| `duckduckgo_results_json` | 静态 Tool | 网页搜索 |
| `calculator` | 静态 Tool | 数学计算 |
| `ask_human` | 静态 Tool | 人机交互中断 |
| `maps_*` (15个) | MCP Tool | 高德地图全套 |

---

## 阶段一：静态 Tool 开发（入门）

> 难度：⭐  
> 目标：掌握 LangChain `@tool` 装饰器的基本开发模式  
> 对应文件：`app/core/langgraph/tools/`

### 推荐练习项目

#### 1. 天气查询工具（接第三方 REST API）
```python
# 使用和风天气或 OpenWeatherMap 免费 API
@tool
async def get_weather(city: str, days: int = 1) -> str:
    """查询指定城市的天气预报。"""
    # 调用 REST API → 解析 JSON → 返回结构化文字
```

**学习点**：
- 如何在工具里调用 HTTP API
- 如何处理 API 错误和超时（`tenacity` 重试）
- 工具的 `docstring` 就是 LLM 看到的 description，写好很重要

---

#### 2. 文件读写工具（操作本地/云存储）
```python
@tool
async def read_file(path: str) -> str:
    """读取指定路径的文件内容。"""

@tool
async def write_file(path: str, content: str) -> str:
    """将内容写入指定文件。"""
```

**学习点**：
- 工具的安全边界设计（路径白名单）
- 多工具协同（先 read → LLM 分析 → 再 write）

---

#### 3. 数据库查询工具（结合项目已有的 PostgreSQL）
```python
@tool
async def query_chat_history(user_id: int, limit: int = 10) -> str:
    """查询用户的历史对话记录。"""
    # 复用项目里的 database_service
```

**学习点**：
- 工具如何复用项目已有的 Service 层
- 异步数据库操作
- 结果序列化（Pydantic → 字符串）

---

#### 4. 代码执行工具（沙箱运行 Python）
```python
@tool
async def execute_python(code: str) -> str:
    """在安全的沙箱中执行 Python 代码并返回结果。"""
    # 使用 subprocess 或 RestrictedPython
```

**学习点**：
- 危险工具的安全设计（超时、内存限制）
- 这是 Code Agent 的核心能力

---

## 阶段二：有状态工具 + 工具链（进阶）

> 难度：⭐⭐  
> 目标：掌握工具之间的数据传递、多步骤工具链设计

### 推荐练习项目

#### 5. 网页内容抓取工具
```python
@tool
async def scrape_webpage(url: str) -> str:
    """抓取网页的主要文字内容。"""
    # playwright 或 httpx + BeautifulSoup
```

**进阶组合**：`duckduckgo_search` → `scrape_webpage` → LLM 总结  
这就是 RAG Agent 最基础的形态。

---

#### 6. 图片分析工具（多模态）
```python
@tool
async def analyze_image(image_url: str, question: str) -> str:
    """分析图片内容并回答关于图片的问题。"""
    # 调用 DeepSeek Vision 或 OpenAI GPT-4V
```

**学习点**：
- 多模态工具如何传递图片（base64 / URL）
- Vision LLM 的调用方式

---

#### 7. 发送通知工具（邮件/企业微信/钉钉）
```python
@tool
async def send_email(to: str, subject: str, body: str) -> str:
    """发送电子邮件通知。"""

@tool
async def send_wechat_work(webhook: str, message: str) -> str:
    """发送企业微信机器人消息。"""
```

**学习点**：
- 工具的副作用管理（发送后不可撤回）
- 配合 `ask_human` 工具：先让 LLM 生成草稿 → 用 `ask_human` 让用户确认 → 再发送

---

#### 8. 知识库检索工具（RAG）
```python
@tool
async def search_knowledge_base(query: str, top_k: int = 3) -> str:
    """在企业知识库中检索相关文档。"""
    # 使用项目已有的 pgvector + Ollama bge-m3 做向量检索
```

**学习点**：
- 利用项目已有的 pgvector 构建知识库
- 向量检索（embedding → 余弦相似度）
- 这是 RAG Agent 的核心

---

## 阶段三：自建 MCP Server（高级）

> 难度：⭐⭐⭐  
> 目标：不再只是消费 MCP，而是**自己发布一个 MCP Server**

### 什么时候需要自建 MCP Server？

- 你有私有的内部系统（ERP、CRM、内部数据库）
- 你想把工具发布给多个 Agent 共享使用
- 你想复用现有的 REST API，让它 MCP 化

### 自建 MCP Server 示例

```python
# 使用官方 mcp 库搭建
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("企业内部工具")

@mcp.tool()
async def query_erp_order(order_id: str) -> str:
    """查询 ERP 系统中的订单状态。"""
    # 调用内部 ERP API
    return f"订单 {order_id} 状态：已发货"

@mcp.tool()
async def get_employee_info(employee_id: str) -> str:
    """查询员工信息。"""
    # 查询 HR 数据库

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

然后在项目的 `load_all_tools()` 里接入：

```python
# app/core/langgraph/tools/__init__.py
mcp_servers = {
    "amap": {"url": "https://mcp.amap.com/mcp?key=...", "transport": "streamable_http"},
    "erp":  {"url": "http://your-erp-mcp-server:8080", "transport": "streamable_http"},  # 自建
}
```

### 推荐自建练习

| MCP Server | 说明 |
|---|---|
| 个人笔记 MCP | 封装 Notion / Obsidian API |
| Git 操作 MCP | 查看仓库、创建 Issue、Review PR |
| 数据分析 MCP | 上传 CSV → 自动用 pandas 分析 |
| 日历 MCP | 接入 Google Calendar / 飞书日历 |

---

## 阶段四：Multi-Agent 系统（专家级）

> 难度：⭐⭐⭐⭐  
> 目标：多个 Agent 协同完成复杂任务

### 架构演进

```
当前项目（单 Agent）:
  用户 → LangGraph Agent → 工具池 → 返回

Multi-Agent（下一步）:
  用户
    └─► Orchestrator Agent（规划分解）
          ├─► Search Agent    （专门搜索）
          ├─► Code Agent      （专门写代码）
          ├─► Data Agent      （专门分析数据）
          └─► Report Agent    （专门生成报告）
```

### 在项目中实现的方式

1. **`call_subagent` 工具**：主 Agent 通过工具调用其他 Agent
2. **LangGraph 多图编排**：在 `graph.py` 里定义多个子图，用 `Command` 路由
3. **Supervisor 模式**：一个 LLM 负责分配任务给其他 LLM

---

## 学习顺序建议

```
第1周: 完成阶段一练习 1-2（天气工具 + 文件工具）
第2周: 完成阶段一练习 3-4（数据库工具 + 代码执行）
第3周: 完成阶段二练习 5-6（爬虫 + 知识库检索）
第4周: 自建第一个 MCP Server（接入内部系统）
第5周+: 探索 Multi-Agent 架构
```

---

## 工具开发核查清单

每开发一个新工具，确认以下几点：

- [ ] `docstring` 清晰描述功能、参数含义（这是 LLM 理解工具的唯一途径）
- [ ] 参数使用 Python 类型注解
- [ ] 有错误处理（API 超时、返回空值等）
- [ ] 返回值是人类可读的字符串（避免返回巨大 JSON）
- [ ] 使用 `tenacity` 做重试
- [ ] 在 `tools/__init__.py` 的 `static_tools` 或 `load_all_tools()` 里注册
- [ ] 添加 `structlog` 日志（工具调用开始 / 结束 / 异常）

---

## 推荐参考资源

| 资源 | 链接 |
|---|---|
| LangChain Tool 开发文档 | https://python.langchain.com/docs/concepts/tools/ |
| MCP 官方规范 | https://modelcontextprotocol.io/docs |
| FastMCP（快速构建 MCP Server）| https://github.com/jlowin/fastmcp |
| LangGraph 多 Agent 示例 | https://langchain-ai.github.io/langgraph/tutorials/multi_agent/ |
| 公开 MCP Server 列表 | https://github.com/modelcontextprotocol/servers |
