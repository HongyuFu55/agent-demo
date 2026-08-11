# fastapi-langgraph-agent-zh

> 基于 [fastapi-langgraph-agent-production-ready-template](https://github.com/wassim249/fastapi-langgraph-agent-production-ready-template) 的中文适配版本，面向国内开发者的生产级 LangGraph 多智能体工作流编排与 3D 极客交互框架。

---

## 🌟 项目简介

本项目是一个开箱即用的 **LangGraph + FastAPI + MCP 生产级智能体应用框架**，专为国内团队构建高性能、高安全、可观测、高颜值的 AI Agent 应用而打造。

### ✨ 核心扩展与升级特性：
- 🛠️ **多工具生态与 MCP 集成**：内置高德地图 MCP 全套工具（15+）、网页搜索抓取、Python 沙箱执行、多模态视觉识别与 **163 网易邮箱 SMTP 异步通知**。
- 🛡️ **企业级安全与物理沙箱隔离**：多租户会话文件物理隔离存储（`storage/workspace/users/...`），具备防路径穿越（Anti-Path Traversal）双重硬拦截。
- 🧠 **双层防污染长期记忆架构 (mem0ai + pgvector)**：代码级 Gatekeeper 守门过滤 + 第一性原理记忆提炼 Prompt，杜绝即时计算与路况等临时任务污染长期记忆库。
- 🪐 **Three.js 3D 实时量子中枢与 Anime.js 动效体系**：前端全屏 Canvas 星座微光粒子网、Three.js 3D 旋转量子核心、Anime.js 全局点击能量粒子爆破与物理弹性阻尼交互。
- ⚡ **无感直达交互体验**：自动静默鉴权直达会话树、毫秒级网络延时探测、全屏图片灯箱预览（Lightbox Modal）。
- 📊 **全链路可观测性**：structlog 结构化日志（高频探针日志智能静默）、Prometheus + Grafana 监控大盘、Langfuse 链路追踪。

---

## 核心能力总览

| 维度 | 核心能力 | 实现方式 |
|---|---|---|
| **API 框架** | 异步 RESTful API & SSE 流式输出 | FastAPI · uvicorn · Pydantic v2 |
| **Agent 工作流** | 复杂状态机图编排、工具自动路由 | LangGraph（StateGraph + ToolNode） |
| **大模型生态** | 多模型负载、重试机制、降级回退 | DashScope (Qwen2.5 / DeepSeek) · Ollama · tenacity |
| **外部工具生态** | 高德地图全套 API (15+ 工具) | MCP (Model Context Protocol) streamable_http |
| **通知工具** | 163 邮箱 SMTP over SSL (465) 异步通知 | Python `smtplib.SMTP_SSL` + `asyncio.to_thread` |
| **长期记忆** | 用户画像提炼、向量语义搜索、防污染 | mem0ai + PostgreSQL + pgvector + 阿里云 embedding |
| **安全与沙箱** | 多租户文件隔离存储、严格防路径穿越 | `Path.relative_to` 断言 + 文件扩展名白名单 |
| **前端交互** | 3D 实时量子球、粒子爆破、极客毛玻璃 | Three.js + Anime.js + Marked.js + Highlight.js |
| **数据持久化** | 业务数据、对话历史、Agent Checkpoint | PostgreSQL 15+ · SQLModel · AsyncPostgresSaver |
| **系统鉴权** | 统一 JWT 认证、无感静默登录、会话归属校验 | JWT (HS256) · OAuth2 Password Bearer |
| **限流防刷** | 用户 ID / IP 智能限流与友好响应 | slowapi · 自定义 RateLimitHandler |
| **可观测性** | LLM 深度追踪、指标监控、探针静默日志 | structlog · Langfuse Callback · Prometheus · Grafana |

---

## 🛠️ 智能体工具库 (Agent Tools Ecosystem)

### 1. 静态 Tool 矩阵
- **`send_email_notification`**：通过 163 网易邮箱 SMTP SSL 异步发送科技风 HTML 富文本邮件，支持多收件人与指数退避重试；
- **`analyze_image`**：多模态图像视觉分析（发票、票据、图表等拍照识别）；
- **`calculator`**：高精度数学表达式安全计算；
- **`duckduckgo_results_json`**：实时互联网网页与新闻搜索；
- **`scrape_webpage`**：高效抓取目标 URL 网页正文 Markdown；
- **`execute_python_code`**：隔离沙箱内执行 Python 数据处理与精准爬虫；
- **`read_workspace_file` / `write_workspace_file`**：工作区文档与数据安全读写。

### 2. 高德地图 MCP Tool 矩阵 (15+ 工具)
- **`maps_weather`**：实时与未来天气预报查询；
- **`maps_direction_driving` / `walking` / `transit` / `bicycling`**：驾车、步行、地铁公交、骑行全模式路线规划与路况评估；
- **`maps_geocode` / `regeocode`**：地理编码与逆地理编码；
- **`maps_search_around` / `search_text`**：周边 POI 检索与关键字位置查询。

---

## 🛡️ 安全与多租户文件沙箱架构

上传的文件采用严格的用户与会话物理隔离分层架构：
```
storage/workspace/
  └── users/
      └── {user_id}/
          └── {session_id}/
              └── 20260810_143000_receipt.png
```
- **双重防路径穿越防护**：拦截文件名中包含 `..`、`/`、`\` 等非法字符，并在后端执行 `Path.resolve().relative_to(sandbox_dir)` 强制校验；
- **前端隐私安全**：对话气泡呈现优雅图片卡片与灯箱放大预览，**完全对用户隐藏底层服务器文件绝对路径**。

---

## 🧠 双层防污染长期记忆机制 (mem0ai)

为防止临时计算、即时路况、瞬时天气等瞬态信息污染长期记忆库，设计了双层防御网：

1. **第一层：代码级 Gatekeeper 守门人**（`app/services/memory.py`）
   - 针对非陈述性句式、计算指令、查询类动词进行前置过滤；仅当包含第一人称明确偏好/身份信息（如“我喜欢”、“我住在”）时才放行。
2. **第二层：第一性原理 3-Questions 抽取 Prompt**
   - **主体相关性**：是否属于用户核心属性？
   - **跨会话持久性**：未来会话是否具备复用价值？
   - **时间不变性**：是否不受时间推移快速失效？

---

## 🎨 极客前端终端交互 (Three.js & Anime.js)

访问内置测试台：`http://localhost:8000/test`

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Three.js 3D 实时量子智能中枢 (3D Quantum Core Reactor)   │
│ ──► 线框二十面体 + 双重倾斜轨道环 + 220 颗星云粒子 3D 视差倾斜│
├─────────────────────────────────────────────────────────────┤
│ 2. Anime.js 全局点击能量粒子爆破 (Click Particle Burst)     │
│ ──► 全屏任意点击产生 18 颗向四周辐射的霓虹流光光斑          │
├─────────────────────────────────────────────────────────────┤
│ 3. 页面与会话列表 Stagger & Spring 物理弹性动效             │
│ ──► 会话项交错滑入、气泡弹性回弹、图片灯箱 Elastic 弹发展开 │
├─────────────────────────────────────────────────────────────┤
│ 4. 全屏微光星空流光 Canvas (Constellation Network)          │
│ ──► 70 颗自发光微粒悬浮游动，鼠标移动产生引力互动           │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 快速启动

### 环境要求
- Python 3.11+
- PostgreSQL 15+（需启用 `pgvector` 扩展）
- [uv](https://github.com/astral-sh/uv) (推荐包管理器)
- Docker & Docker Compose

### 1. 安装依赖
```bash
uv sync
```

### 2. 配置环境变量
```bash
cp .env.example .env.development
# 在 .env.development 中配置数据库连接、DashScope API Key、163 邮箱授权码等
```

### 3. 启动服务

```bash
# 方式 A：Docker 完整栈启动（API + PostgreSQL + pgvector + Prometheus + Grafana）
make docker-compose-up ENV=development

# 方式 B：本地热加载启动
make dev
```

### 4. 访问服务
- **3D 极客 Agent 测试台**：[http://localhost:8000/test](http://localhost:8000/test)
- **API Swagger 文档**：[http://localhost:8000/docs](http://localhost:8000/docs)
- **Prometheus 监控指标**：[http://localhost:8000/metrics](http://localhost:8000/metrics)
- **Grafana 监控看板**：[http://localhost:3000](http://localhost:3000)

---

## 🛠️ 常用开发命令

```bash
make install          # 安装所有依赖（uv sync）
make dev              # 本地热重载启动（port 8000）
make lint             # ruff 代码检查
make format           # ruff 自动代码格式化
make typecheck        # pyright 严格类型检查 (0 errors)
make check            # lint + typecheck 全量校验
make eval-quick       # 运行 LLM 质量评测
```

---

## 📂 项目结构

```
app/
  api/v1/            # 路由层（auth.py · chatbot.py · debug.py · api.py）
  core/
    config.py        # Pydantic Settings 配置驱动
    langgraph/       # LangGraph Agent 图拓扑 + 工具库
      tools/         # 工具实现（email_sender · mcp_client · calculator 等）
    logging.py       # structlog 日志体系（智能静默高频探针）
    middleware.py    # ASGI 耗时监控与上下文绑定中间件
    metrics.py       # Prometheus 性能指标导出
    limiter.py       # slowapi 细粒度用户限流
    prompts/         # Agent 策略提示词
  models/            # SQLModel ORM 数据库实体
  schemas/           # Pydantic 校验模式与图状态
  services/          # 核心服务（database · memory · llm）
  static/            # 前端 3D 终端资源（index.html · anime.min.js）
evals/               # LLM 评测框架（基于 Langfuse）
storage/workspace/   # 用户与会话隔离沙箱存储
```

---

## 📄 开源许可证与致谢

本项目基于 [wassim249/fastapi-langgraph-agent-production-ready-template](https://github.com/wassim249/fastapi-langgraph-agent-production-ready-template) 与 [Anime.js](https://animejs.com/) 进行深度二次研发，感谢开源社区的卓越贡献。
