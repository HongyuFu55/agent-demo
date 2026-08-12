# AI Agent & RAG 系统开发学习与生产级进阶路线图

基于当前项目（FastAPI + LangGraph + PostgreSQL + MCP）的实战迭代路径，并融合 [nageoffer.com（拿个offer）](https://nageoffer.com/) 的 **《AI 工程师学习手册》** 与 **《Ragent AI 企业级 Agentic RAG 实战课》** 核心精髓，对标工业界生产级（Production-Ready）标准制定本路线图。

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

## 阶段二：企业级 RAG 检索增强与向量数据库（进阶进行中 🟡 70%）

> **难度**：⭐⭐⭐  
> **状态**：**进行中 70%**（Web 抓取与用户记忆已完成，知识库多路混合检索与 Milvus 待落地）  
> **核心目标**：攻克文档入库 Pipeline、Milvus 向量存储与 HNSW 索引、BM25 混合检索、加权 RRF 融合、BGE-Reranker 精排与 Grounding 溯源。

```
                                  【 企业级 RAG 架构全景 】
                                             │
      ┌──────────────────────────────────────┼──────────────────────────────────────┐
      ▼                                      ▼                                      ▼
【1. Ingestion 流水线】             【2. 混合检索与精排漏斗】             【3. 受控生成与溯源】
 ├─ Apache Tika / Markdown 解析      ├─ 密集向量: Milvus / pgvector (HNSW) ├─ Prompt 注入与负向约束
 ├─ 带 Overlap 递归分块 (500~1000)   ├─ 稀疏全文: BM25 / ES 关键词精确匹配  ├─ 引用角标标注 ([1], [2])
 ├─ 元数据绑定 (doc_id, source)      ├─ 加权 RRF 倒数排名无监督融合         └─ SSE 单向打字机流式推送
 └─ Embedding 向量化 (bge-m3)        └─ BGE-Reranker 交叉二次打分过滤
```

### 1. RAG 核心生命周期（六步标准流程）
- **准备阶段（离线）**：
  1. `Ingest`：解析 PDF/Word/Markdown，清洗 HTML 标签与表格。
  2. `Chunk`：按 500~1000 字符切块，保留 100 字符重叠（Overlap），防止上下文割裂。
  3. `Embed`：调用 `bge-m3` / `text-embedding-v4` 将文本转化为高维密集向量。
  4. `Index`：存入向量数据库，建立 HNSW / IVF 索引加速近邻检索。
- **运行阶段（在线）**：
  5. `Retrieve`：将用户提问向量化，执行 Top-K 相似度检索。
  6. `Answer`：组装参考资料与负向提示词（“未提及请明确说明不知”），受控流式生成。

### 2. 双擎协同架构设计（PostgreSQL + Milvus）
- **架构决策**：**不全盘推翻 PG，而是采用“双擎协同 / RAG as a Tool”**。
  - **PostgreSQL**：负责业务表（User/Session/Message）+ LangGraph 状态机检查点（`AsyncPostgresSaver`）+ 轻量长期记忆（`mem0`）。
  - **Milvus**：负责 **海量企业知识库切片（百万+）+ HNSW 高速近邻搜索 + 多租户 Partition Key 隔离**。

### 3. Milvus Standalone 部署与 Attu 可视化
- 使用 Docker Compose 一键拉起 `etcd` + `rustfs/minio` + `milvus-standalone` + `attu`（管理面板映射到宿主机 `8001` 端口，避免占用 FastAPI `8000`）。
- 常用索引对比与选型：
  - **HNSW**：速度最快（1~5ms）、召回率极高（>98%），适合百万级高要求场景（内存开销较大）。
  - **IVF_FLAT / IVF_SQ8**：基于聚类分区，SQ8 标量量化可将内存压缩 75%，适合千万级大规模场景。

### 4. 攻坚清单（已全部完成 ✅）
- [x] Web-RAG 网页结构化抓取与工作区直存 (`scrape_webpage`)
- [x] `mem0ai` + pgvector 用户级偏好记忆检索
- [x] 编写 `docker-compose.milvus.yml` 启动本地 Milvus + Attu（`v2.4.15` 容器群稳定运行）
- [x] 实现 `app/services/rag/` 模块：文档 Ingestion Pipeline + 语义分块 (`text_splitter.py`)
- [x] 实现 BM25 + Milvus 向量多路并行召回 + 加权 RRF 融合算法 (`hybrid_retriever.py`)
- [x] 实现多级降级与确定性 Embedding 向量服务 (`embedding_service.py`)
- [x] 将知识库封装为 LangGraph 工具 `@tool query_knowledge_base` 并挂载至全局图工作流
- [x] 提供完整的 FastAPI RESTful API（文档录入、文件上传、混合检索、状态统计）并经全链路自测验证通过

---

## 阶段三：自建 MCP Server 微服务（高级待实践 🎯 40%）

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

## 阶段四：Multi-Agent 多智能体系统与 Agentic RAG（专家级架构 🎯 25%）

> **难度**：⭐⭐⭐⭐  
> **目标**：解决单 Agent 处理复合任务时的步数超限、Token 爆炸与单点能力瓶颈，实现自适应决策与分工协作。

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

### 核心设计模式
1. **LangGraph Subgraphs（子图模块化）**：
   - 将特定垂直领域（如代码沙箱执行、超长网页切片研究、Word 排版）拆为独立子图。
   - 每个子图拥有独立的 `recursion_limit` 与局部 State，完成后仅返回精简摘要给主图。
2. **Agentic RAG（智能体自适应检索）**：
   - 模型将 RAG 作为一种 Tool，自主判断“何时需要查资料”、“何时直接作答”、“检索不理想时自我反思并改写问题重试”。
3. **MessageHub 与通信治理**：
   - 通过共享状态字典消除多 Agent 间的上下文割裂，并在传递过程中裁剪冗余 Observation。

---

## 阶段五：生产级工程治理与上线保障（顶尖企业标准）

> **难度**：⭐⭐⭐⭐  
> **目标**：保障 Agent 在高并发、复杂长链路、海量数据下的稳定性、低成本与安全性。

### 1. 递归步数与熔断保护策略
- 在 [config.py](file:///d:/Learn/fastapi-langgraph-agent-zh-master/app/core/config.py) 中合理配置 `LANGGRAPH_RECURSION_LIMIT`（当前已优化为 50）。
- **死循环侦测**：当连续 3 次调用同名工具且入参完全相同时，主动中断并请求人类协助。
- **三态熔断机制**：对上游 LLM 供应商设置健康探测与熔断降级。

### 2. 上下文预算管理（Context & Token Management）
- **Tool Output Summarization**：当工具返回超过 10,000 字符时，在存入 State 前调用轻量级模型生成关键摘要。
- **Token 窗口修剪**：集成 LangChain `trim_messages`，保留首轮 System Prompt + 最近 N 轮有效对话，防止超长会话导致 Token 费用激增或注意力衰减。

### 3. 沙箱安全隔离进阶
- 生产环境将 `execute_python_code` 迁移至轻量容器沙箱（如 Docker SDK、Firecracker 或 E2B 沙箱云），实现严格的 CPU、内存限制与只读网络隔离。

### 4. LLM-Ops 持续评测与监控闭环
- **指标大盘**：Prometheus + Grafana 持续监控 LLM TTFT（首 Token 延迟）、工具调用成功率、TP99 耗时。
- **质量评测**：基于 `evals/` 框架，每日对线上真实 Trace 采样，进行幻觉率（Hallucination）、检索忠实度与任务达成率（Helpfulness）自动化打分。

---

## 📅 8 周系统学习与实战规划路线图

```
┌──────────────┬─────────────────────────────────────────────────────────────────────────┐
│ 阶段 / 时间   │ 核心实战攻坚任务                                                         │
├──────────────┼─────────────────────────────────────────────────────────────────────────┤
│ 第 1~2 周    │ 【基础与静态工具】已掌握 ✅                                              │
│              │ • 静态 Tool 注册、Pydantic 入参校验、Tenacity 重试与沙箱安全防护          │
│              │ • Web 抓取 Markdown 结构化清洗、视觉识别、邮件 HITL 人机协同               │
├──────────────┼─────────────────────────────────────────────────────────────────────────┤
│ 第 3~4 周    │ 【企业级 RAG 检索增强体系】🎯 当前攻坚重点                              │
│              │ • 启动 Docker Milvus + Attu 可视化管理面板                              │
│              │ • 搭建 Ingestion 流水线：长文本语义分块 (Chunking + Overlap)             │
│              │ • 实现 BM25 + Milvus (HNSW) 混合检索与加权 RRF 融合算法                  │
│              │ • 接入 BGE-Reranker 精排与 Grounding 溯源角标标注                       │
├──────────────┼─────────────────────────────────────────────────────────────────────────┤
│ 第 5~6 周    │ 【自建 FastMCP 与 Agentic RAG】                                          │
│              │ • 使用 FastMCP 构建独立的 Office/数据分析微服务                          │
│              │ • 将 Milvus 混合检索封装为 `@tool`，跑通 Agent 自主反思与检索闭环       │
├──────────────┼─────────────────────────────────────────────────────────────────────────┤
│ 第 7~8 周    │ 【Multi-Agent 协作与生产级治理】                                         │
│              │ • LangGraph Subgraphs 子图拆分与 Supervisor 主从协作路由                 │
│              │ • 上下文修剪压缩 (Token Management) 与三态熔断保护                      │
│              │ • 基于 Evals + Langfuse 的 RAG 准确率与无幻觉率量化评测                   │
└──────────────┴─────────────────────────────────────────────────────────────────────────┘
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
