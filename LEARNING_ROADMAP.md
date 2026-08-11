# Agent Tools 开发学习与生产级进阶路线图

基于当前项目（FastAPI + LangGraph + PostgreSQL + MCP）的实战迭代路径，对标工业界生产级（Production-Ready）Agent 架构标准，从简到难分为 5 个阶段。

---

## 一、 当前项目已有能力与工具盘点

当前项目已实现完整的工程化底座与丰富的工具集：

| 分类 | 工具/能力名称 | 状态 | 对应实现文件 | 功能说明 |
|---|---|:---:|---|---|
| **搜索与计算** | `duckduckgo_search` / `calculator` | ✅ 已实现 | [duckduckgo_search.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/tools/duckduckgo_search.py) | 网络实时搜索与安全数学计算 |
| **人机协同 (HITL)** | `ask_human` | ✅ 已实现 | [ask_human.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/tools/ask_human.py) | 中断图流程等待人类输入，确认高危操作 |
| **位置与天气** | `get_weather_now`, `search_location_id` | ✅ 已实现 | [qweather.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/tools/qweather.py) | 和风天气 REST API 实时查询 |
| **文件与工作区** | `list_workspace_files`, `read_workspace_file`, `write_workspace_file` | ✅ 已实现 | [file_ops.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/tools/file_ops.py) | 沙箱文件读写（带路径穿越白名单防护） |
| **代码沙箱** | `execute_python_code` | ✅ 已实现 | [code_interpreter.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/tools/code_interpreter.py) | 独立子进程 Python 执行沙箱（超时防爆） |
| **数据库/SQL** | `query_session_history`, `query_user_sessions`, `execute_sql_query` | ✅ 已实现 | [db_query.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/tools/db_query.py) | Text-to-SQL 只读安全查询与历史检索 |
| **Web-RAG 抓取** | `scrape_webpage` | ✅ 已实现 | [web_scraper.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/tools/web_scraper.py) | DOM 净化、Markdown 结构化、大纲生成与工作区直存 |
| **多模态视觉** | `analyze_image` | ✅ 已实现 | [image_analyzer.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/tools/image_analyzer.py) | 本地 Ollama / 视觉模型图像识别与分析 |
| **外部通信** | `send_email_notification` | ✅ 已实现 | [email_sender.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/tools/email_sender.py) | 163 邮箱 SMTP 异步邮件通知 |
| **MCP 工具消费** | 高德地图全套 (`maps_*` 15+ 个) | ✅ 已实现 | [mcp_client.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/tools/mcp_client.py) | 远程 Streamable-HTTP MCP 动态工具加载与降级 |
| **语义记忆存储** | `mem0ai` + pgvector | ✅ 已实现 | [memory.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/services/memory.py) | 用户级长期语义记忆提取与持久化检索 |
| **工作流持久化** | LangGraph `AsyncPostgresSaver` | ✅ 已实现 | [graph.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/langgraph/graph.py) | 生产级线程 Checkpoint 状态恢复与流式输出 |

---

## 阶段一：静态 Tool 开发与安全沙箱（基础已掌握 ✅）

> **难度**：⭐  
> **状态**：**100% 已实现**  
> **核心目标**：掌握 LangChain `@tool` 装饰器、入参校验、异常重试与安全边界防护。

### 关键掌握点
1. **API 错误与重试**：统一使用 `tenacity` 库进行指数退避重试（`stop_after_attempt` + `wait_exponential`）。
2. **安全边界与沙箱设计**：
   - 文件操作：校验绝对路径必须锁定在 `storage/workspace/` 内部，拦截 `../` 路径穿越攻击。
   - 数据库操作：只暴露只读 SELECT 权限，结合正则与 AST 拦截 DROP/DELETE/UPDATE/INSERT。
   - 代码执行：设置 `EXEC_TIMEOUT_SECONDS`（如 30s）并捕获超时 SIGKILL，限制标准输出最大长度（如 4000 字符）。

---

## 阶段二：有状态工具与工具链工程（进阶进行中 🟡 85%）

> **难度**：⭐⭐  
> **状态**：**85% 已完成**  
> **核心目标**：掌握多工具协同传递数据、多模态融合、副作用管理与高级 RAG。

### 1. 网页内容深度抓取（Web-RAG）✅
- **进阶特性**：不仅提取纯文本，还将 HTML 转换为标准 Markdown，保留多级标题（`# ~ ######`）、Markdown 表格与代码块，并自动提取文档大纲目录（TOC）。
- **直存机制**：支持 `save_to_workspace_file` 参数，直接将数万字超长文本写入工作区文件，避免多次截断切片。

### 2. 多模态 Vision 图像识别 ✅
- 接收图片路径或 Base64 编码，调用多模态模型提取发票、图表、代码截图中的结构化数据。

### 3. 工具副作用治理与人机协同（HITL）✅
- 对于发送邮件、转账、删除数据等**不可逆副作用**操作，配合 `ask_human` 工具中断工作流，生成草稿并等待用户在前端确认后再执行。

### 4. 企业级 RAG 知识库检索（待实战 🎯）
*当前项目已实现 `mem0ai` 用户记忆，需进一步扩展企业级私有文档检索：*
```python
@tool
async def search_enterprise_kb(query: str, collection: str = "kb_docs", top_k: int = 5) -> str:
    """在企业知识库中进行混合检索（关键词 + 语义向量）并重排序。"""
    # 1. 密集向量检索 (Dense Retrieval - pgvector / Ollama bge-m3)
    # 2. 稀疏全文检索 (Sparse Retrieval - PostgreSQL tsvector / BM25)
    # 3. Reranker 模型二次打分重排 (BGE-Reranker)，剔除低分噪音
    ...
```

**下一步攻坚点**：
- [ ] 文档解析器（PDF / Word / Excel / Markdown）
- [ ] 语义切分（Chunking）策略与元数据关联
- [ ] 混合检索（Hybrid Search: BM25 + Vector）+ Rerank 过滤

---

## 阶段三：自建 MCP Server（高级待实践 🎯 40%）

> **难度**：⭐⭐⭐  
> **目标**：从“消费外部 MCP”进化为“独立开发并发布私有 MCP Server”，实现工具跨项目共享与微服务解耦。

### 架构示意
```
┌──────────────────────────────────────┐
│       LangGraph Agent 主服务         │
└──────────────────┬───────────────────┘
                   │ Streamable-HTTP / SSE
     ┌─────────────┼─────────────┐
     ▼             ▼             ▼
┌──────────┐ ┌───────────┐ ┌───────────────┐
│ 高德地图 │ │ 内部 ERP  │ │ 自动化办公    │
│ MCP (外部)│ │ MCP Server│ │ MCP (自建)    │
└──────────┘ └───────────┘ └───────────────┘
```

### 自建 FastMCP Server 示例
```python
# mcp_servers/office_mcp.py
from mcp.server.fastmcp import FastMCP
import docx

mcp = FastMCP("Office-Automation-MCP")

@mcp.tool()
async def generate_docx_report(title: str, sections: list[dict], output_name: str) -> str:
    """自动排版并生成企业级 Word 文档。"""
    doc = docx.Document()
    doc.add_heading(title, level=0)
    for sec in sections:
        doc.add_heading(sec["title"], level=1)
        doc.add_paragraph(sec["content"])
    doc.save(f"storage/workspace/{output_name}")
    return f"Word 文档 '{output_name}' 生成成功！"

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8001)
```

**推荐自建练习**：
- [ ] **企业数据分析 MCP**：上传 CSV/Excel $\to$ 自动化 Pandas 清洗 $\to$ 生成可视化图表。
- [ ] **Git/DevOps MCP**：操作 GitHub/GitLab 仓库、创建 Issue、自动生成 PR Review 建议。
- [ ] **个人知识库 MCP**：封装 Notion / Obsidian 笔记库双向同步。

---

## 阶段四：Multi-Agent 多智能体系统（专家级架构 🎯 25%）

> **难度**：⭐⭐⭐⭐  
> **目标**：解决单 Agent 处理复合任务时的步数超限、Token 爆炸与单点能力瓶颈。

### 架构演进：从单 Agent 到 Supervisor 协作网络

```
【当前架构 (Single ReAct)】
用户 ──► [ LangGraph 核心循环 (Chat ⇄ ToolCall) ] ──► 输出 (易受限步数上限)

【进阶架构 (Supervisor / Hierarchical Agents)】
                       ┌───────────────────────┐
                       │   Supervisor Agent    │
                       │   (意图理解/任务规划)   │
                       └───────────┬───────────┘
            ┌──────────────────────┼──────────────────────┐
            ▼                      ▼                      ▼
┌───────────────────────┐ ┌──────────────────────┐ ┌───────────────────────┐
│   Researcher Agent    │ │     Coder Agent      │ │     Reporter Agent    │
│ (专属搜索/抓取/清洗)   │ │ (专职代码/调试/执行) │ │ (专职格式化排版/发送) │
└───────────────────────┘ └──────────────────────┘ └───────────────────────┘
```

### 在当前项目中的演进实现方式
1. **LangGraph Subgraphs（子图模块化）**：
   - 将特定垂直领域的任务（如代码编写、文档生成）抽离为独立的 `CompiledStateGraph`。
   - 每个子图拥有独立的 `recursion_limit` 和私有 State，执行完毕后仅把最终摘要返回给主图。
2. **Supervisor 路由编排**：
   - 主模型根据当前进度决定下一步转交给哪个专职子智能体，直到所有子任务全部完成。

---

## 阶段五：生产级工程治理与上线保障（顶尖企业标准）

> **难度**：⭐⭐⭐⭐  
> **目标**：保障 Agent 在高并发、复杂长链路、海量数据下的稳定性、低成本与安全性。

### 1. 递归步数与熔断保护策略
- 在 [config.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/config.py) 中合理配置 `LANGGRAPH_RECURSION_LIMIT`（当前已优化为 50）。
- **死循环侦测**：当连续 3 次调用同名工具且入参完全相同时，主动中断并请求人类协助。

### 2. 上下文预算管理（Context & Token Management）
- **Tool Output Summarization**：当工具返回超过 10,000 字符时，在存入 State 前调用轻量级模型生成关键摘要。
- **Token 窗口修剪**：集成 LangChain `trim_messages`，保留首轮 System Prompt + 最近 N 轮有效对话，防止超长会话导致 Token 费用激增或注意力衰减。

### 3. 沙箱安全隔离进阶
- 生产环境将 `execute_python_code` 迁移至轻量容器沙箱（如 Docker SDK、Firecracker 或 E2B 沙箱云），实现严格的 CPU、内存限制与只读网络隔离。

### 4. LLM-Ops 持续评测与监控闭环
- **指标大盘**：Prometheus + Grafana 持续监控 LLM TTFT（首 Token 延迟）、工具调用成功率、TP99 耗时。
- **质量评测**：基于 `evals/` 框架，每日对线上真实 Trace 采样，进行幻觉率（Hallucination）与任务达成率（Helpfulness）自动化打分。

---

## 学习规划路线推荐（时间表）

```
[已完成] 阶段一：静态工具全部实战 + 沙箱安全 (天气/文件/SQL/代码)
[已完成] 阶段二（上）：网页结构化抓取 + 视觉多模态 + 邮件 HITL
-------------------------------------------------------------------------
[当前计划 第 1 周]：阶段二（下）企业级混合检索 RAG（Hybrid Search + Reranker）
[当前计划 第 2 周]：阶段三 使用 FastMCP 搭建第一个独立的私有 MCP 微服务
[当前计划 第 3-4 周]：阶段四 LangGraph Subgraphs 与 Multi-Agent 协作架构设计
[当前计划 第 5 周]：阶段五 上下文修剪压缩 (Token Management) 与沙箱隔离加固
```

---

## 工具开发生产级核查清单 (Checklist)

每开发一个新工具或 Agent 节点，确认以下指标：

- [ ] `docstring` 精准详细（这是 LLM 决策工具调用的唯一依据）。
- [ ] 严格的入参类型标注与 Pydantic 校验。
- [ ] 具备安全防御边界（路径白名单 / SQL 只读拦截 / 沙箱超时控制）。
- [ ] 结果格式化为清晰的人类/LLM 可读 Markdown（避免直接倾倒海量 JSON）。
- [ ] 统一使用 `tenacity` 做网络超时指数退避重试。
- [ ] 注入 `structlog` 结构化日志（包含入参特征、耗时、状态码）。
- [ ] 危险操作具备 HITL (`ask_human`) 中断确认能力。
