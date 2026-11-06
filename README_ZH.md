# RavenAIService

中文 | [English](README.md)

> 发布、打包、Docker 启停流程已经统一到 [QUICKSTART.md](QUICKSTART.md)。请优先使用 `scripts/docker-start.sh`、`scripts/docker-stop.sh`、`scripts/docker-publish.sh`

RavenAIService 是 Raven 智能测试平台的核心服务仓库。平台正在从单一日志工具向多项目、多 Agent 的通用测试平台演进，围绕**项目化管理、多 Agent 协同、设备联动、版本资产治理**，为复杂测试场景提供一体化能力。

日志、AI、设备、代码资产和发布物不再是孤立的功能模块——它们以”项目”为单元串联，由多个专业 Agent 协同驱动，形成测试、研发、交付与运维之间的闭环协作。

## 配套桌面客户端

**[RavenClient](https://github.com/leoguolabs/RavenClient)** 是 RavenAIService 的跨平台桌面客户端。它基于 Electron、React 和 TypeScript 构建，为 Windows、macOS 和 Linux 用户提供 Raven 账号登录、AI 对话、服务端 Agent 工作流、项目、日志、软件包、本地文件与知识库，以及内嵌 AI 终端等原生桌面能力。

RavenClient 需要连接兼容的 RavenAIService 服务，以完成身份认证并使用平台统一管理的 AI 能力。RavenAIService 负责身份、模型路由、项目上下文、多 Agent 执行、设备、软件包和发布资产等平台核心能力；RavenClient 则负责原生桌面工作区和操作系统集成。

![RavenAIService 多 Agent 与项目上下文关系示意图](docs/diagrams/raven-ai-context-zh.png)

## 项目定位

Raven 平台聚焦测试流程中的几类典型痛点，并正在向平台化方向持续演进：

- 日志来源多、格式杂，测试资产难沉淀
- 复杂日志处理门槛高，问题定位周期长
- 人工排查依赖经验，知识难复用、难规模化
- 平台与设备割裂，测试执行和问题验证难闭环
- 包、版本与发布物分散，管理和追溯成本高
- 不同项目、不同团队的测试流程缺乏统一承载

围绕这些问题，RavenAIService 正在交付一条更完整的智能测试链路：

- 以**项目**为核心单元，将日志、代码仓库、Agent 技能和分析结论统一组织
- 通过**多 Agent 架构**（通用对话、日志分析、设备操作、代码专家、Bug 修复、包检索），让不同测试场景由最合适的 Agent 驱动
- 自动处理复杂日志，缩短从上传到分析的等待时间
- 打通平台与设备能力，实现”提问、执行、回传”的闭环联动
- 统一管理软件包和客户端发布物，支撑版本运营与交付
- 支持中英文多语言界面，服务全球化团队

## 平台价值

- **项目化管理**：日志、仓库、Agent 技能按项目组织，不同团队各有专属上下文
- **多 Agent 协同**：通用对话、日志分析、代码专家、设备操作、Bug 修复、包检索——平台根据场景自动路由到最合适的 Agent
- **提升测试效率**：把日志处理、AI 分析、设备联动集中到统一入口
- **缩短定位闭环**：从上传、解析到问答分析，减少跨工具切换和人工反复确认
- **沉淀测试资产**：让日志、分析结论、包信息和操作记录成为可复用资产
- **全球化支持**：前端界面支持中英文切换，服务多语言团队

## 典型场景

- 管理员为不同产品/团队创建项目，关联代码仓库并配置专属 Agent 技能
- 测试团队批量接入日志，按项目归档，快速筛选和分析
- 平台自动识别不同日志类型，进入对应处理流程
- 测试或研发人员在 AI Chat 中发起问答——通用 Agent 负责路由，日志分析、代码专家、设备操作等 Agent 各司其职
- Bug 修复 Agent 基于日志分析结论自动定位代码、生成修复建议
- 平台将 AI 指令转发到目标设备，完成能力调用、结果回传与联动验证
- 围绕产品版本和交付包形成统一资产中心，支撑发布与回溯

## 平台组成概览

当前仓库集成了以下平台模块：

- `FastAPI` 主服务：承接日志、AI、用户、设备、项目、发布等核心业务
- `Vue 3 + Vite` 控制台：提供统一的多语言 Web 工作台与运营界面
- `多 Agent 引擎`：GeneralAgent（路由）、LogAnalysisAgent、DeviceAgent、ProjectExpertAgent、BugFixAgent、PackageSearchAgent
- `Celery + Redis` 异步任务：支撑日志处理、AI 分析和定时维护
- `Nginx` 网关：对外提供统一入口，降低部署和访问复杂度

```text
Browser
  |
  | http://localhost:8085
  v
Nginx
  |-- /                -> Vue SPA (中/英)
  |-- /api/*           -> FastAPI (8085)
  |-- /raven/api/*     -> FastAPI Raven 包管理
  |-- /ws/device-link  -> FastAPI WebSocket
  |-- /raven           -> Vue SPA Raven 页面

FastAPI
  |-- /health
  |-- /api/v1/logs/*
  |-- /api/v1/ai-chat/*       -> 多 Agent 路由
  |-- /api/v1/users/*
  |-- /api/v1/device-links/*
  |-- /api/v1/releases/*
  |-- /api/v1/projects/*      -> 项目 & 仓库管理
  |-- /api/v1/bug-fixes/*
  |-- /api/v1/metrics/*       -> 系统 & 用户用量统计
  |-- /raven/api/packages/*
  |-- /admin/*
  |-- frontend/dist 静态站点

Agent 引擎
  |-- GeneralAgent          通用对话 & Agent 路由
  |-- LogAnalysisAgent      日志智能分析
  |-- DeviceAgent           设备操作联动
  |-- ProjectExpertAgent    代码仓库问答
  |-- BugFixAgent           Bug 定位与修复建议
  |-- PackageSearchAgent    软件包检索

Celery + Redis
  |-- 异步日志处理
  |-- AI 分析任务
  |-- 定时清理任务
```

## 平台能力地图

### 1. 项目化管理

- 日志不再只按类型分类——以 `project_id` 关联到具体项目，实现多项目隔离
- 每个项目可关联一个或多个代码仓库，为代码专家 Agent 提供知识来源
- 项目级 Agent 技能配置，管理员可为不同项目启停和定制 Agent 行为
- 管理后台提供项目仓库管理、技能配置、模型设置与用量统计界面

### 2. 多 Agent 协同引擎

平台内置六个专业 Agent，由 GeneralAgent 统一路由：

| Agent | 职责 |
| --- | --- |
| **GeneralAgent** | 通用对话入口，根据用户意图自动路由到专业 Agent |
| **LogAnalysisAgent** | 日志智能分析，结合日志解析、元数据与项目上下文 |
| **DeviceAgent** | 通过 WebSocket 连接设备，执行远程操作并回传结果 |
| **ProjectExpertAgent** | 基于项目关联的代码仓库回答源码级问题 |
| **BugFixAgent** | 从日志分析结论出发，定位代码问题并生成修复建议 |
| **PackageSearchAgent** | 在软件包资产中做检索，辅助包选型与版本回溯 |

- 所有 Agent 均支持流式响应，前端实时渲染 Markdown 与 Mermaid 图表
- AI 对话会话支持置顶、导出 Markdown、拖拽上传日志文件等交互能力
- **Agent 主动澄清（AskUserQuestion）**：当用户指令不清晰时，DeviceAgent 可自行决定向用户提出一个或多个澄清问题（每问 2–4 个预设选项 + 自由输入），用户作答后再继续执行。该能力复用人审（HITL）管线，问题卡片支持断线/刷新恢复。用户可在「设置」中自助调整三项偏好：
  - **全局禁用澄清**（默认开启）：关闭后 Agent 不再提问，直接基于自身理解执行；
  - **每轮最多提问次数**（默认 5）：超过上限后 Agent 自行决断、不再打扰用户；
  - **超时后行为**：等待回答 5 分钟后，可选「取消本轮」（默认）或「基于已知信息继续」。

### 3. 测试日志资产化

- 支持多种日志接入入口
- 自动识别不同日志类型，日志按项目归档
- 从压缩包中提取 `metadata.json`，自动补齐问题描述、环境信息与版本信息
- 复杂日志通过 Celery 异步处理，服务启动时自动重试失败任务
- 提供分页查询、筛选、排序、单文件下载、批量下载和批量删除

### 4. 平台与设备协同

- 设备通过 `WebSocket /ws/device-link` 注册到平台，形成统一连接入口
- 服务端维护设备在线状态、能力描述与最近心跳
- DeviceAgent 可将指令转发到指定设备，并等待设备结果回传
- 支持设备能力上报，帮助平台生成更匹配设备能力的动作链路

### 5. 版本资产中心

- FastAPI 后端负责软件包的上传、删除、详情、下载和批量下载
- 提供包搜索与筛选接口
- 后台可统一上传 Linux / macOS / Windows 客户端发布包

### 6. 平台运营与监控

- 系统级与用户级 AI 用量统计，管理员可在后台查看 Agent 调用趋势
- 在线编辑 Prompt 配置、模型设置、用户管理与发布包管理
- 管理员认证配置位于 `app/admin_auth.yaml`

## 仓库结构

```text
RavenAIService/
├── app/                         # FastAPI 主服务
│   ├── api/                     # HTTP / WebSocket 路由
│   ├── agents/                  # 多 Agent 引擎
│   │   ├── general_agent/       #   通用对话 & 路由
│   │   ├── log_analysis/        #   日志智能分析
│   │   ├── device_agent/        #   设备操作联动
│   │   ├── project_expert/      #   代码仓库问答
│   │   ├── bug_fix/             #   Bug 修复建议
│   │   └── package_search/      #   软件包检索
│   ├── middleware/              # 请求日志、文件大小限制等中间件
│   ├── models/                  # SQLAlchemy 与 Pydantic 模型
│   ├── services/                # 业务服务层
│   ├── tasks/                   # Celery 任务
│   ├── tools/                   # 日志/元数据处理工具
│   ├── prompts/                 # Prompt 配置
│   ├── config.py                # 主配置入口
│   └── main.py                  # FastAPI 应用入口
├── frontend/                    # Vue 3 + Vite 前端（中/英多语言）
├── data/                        # 本地占位目录，容器数据通过 Docker volumes 持久化
├── logs/                        # 本地占位目录，容器日志通过 Docker volumes 持久化
├── scripts/                     # Docker 启停、清理、发布脚本
├── alembic/                     # 数据库迁移
├── tests/                       # Python 侧测试
├── docker-compose.yml           # 前端/后端/任务/数据统一编排
├── Dockerfile                   # 后端与 Celery 镜像构建
└── QUICKSTART.md                # 发布、打包、容器启动规范
```

## 快速开始

完整的发布、打包、容器启动规范请阅读 [QUICKSTART.md](QUICKSTART.md)。常用入口如下：

```bash
./scripts/docker-start.sh
```

等服务就绪后访问：

- 主入口: `http://localhost:8085`
- 日志平台首页: `http://localhost:8085/`
- 软件包管理: `http://localhost:8085/raven`
- AI Chat: `http://localhost:8085/ai-chat`
- 管理后台: `http://localhost:8085/admin/prompts`
- 健康检查: `http://localhost:8085/health`
- Swagger 文档: `http://localhost:8085/docs` 仅在开发环境提供

常用脚本：

```bash
./scripts/docker-logs.sh
./scripts/docker-restart.sh
./scripts/docker-stop.sh
./scripts/docker-publish.sh <dockerhub_namespace> <tag>
```

## 配置说明

### 主配置文件

- `.env`: FastAPI、数据库、Redis、Celery、LLM、包管理服务等主配置
- `app/prompts/prompts_config.yaml`: AI Prompt 配置
- `app/admin_auth.yaml`: 管理员账户与 Token TTL 配置

### 关键配置项

#### FastAPI / 基础服务

- `ENVIRONMENT`: `development` 或 `production`
- `PORT`: FastAPI 端口，默认 `8085`
- `SERVE_FRONTEND`: 是否由 FastAPI 直接托管 `frontend/dist`，标准 Docker 编排保持 `false`
- `FRONTEND_DIST_DIR`: `SERVE_FRONTEND=true` 时可覆盖前端构建目录
- `MAX_FILE_SIZE`: 上传大小限制，默认 `1GB`
- `SQLITE_FILE`: 开发环境默认数据库文件，默认 `data/logs.db`
- `DATABASE_URL`: 如果配置，将优先使用该数据库连接串
- `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND`: Celery 与 Redis 配置

#### LLM / AI

- `LLM_PROVIDER`
- `DEEPSEEK_BASE_URL`
- `LLM_MODEL_NAME`
- `LLM_REASONING_MODEL`
- `ANTHROPIC_PROVIDER`: `deepseek | anthropic | custom`，日志分析 Agent 使用
- `ANTHROPIC_API_KEY`: 日志分析 Agent 必填
- `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL`: 自定义 provider 或覆盖默认 profile 时配置
- `PROMPTS_CONFIG_PATH`

#### Raven 包管理

- `RAVEN_BASE_PATH`: 默认 `/raven`
- `RAVEN_DATA_DIR`: 默认 `data/raven`

## 主要产品入口

| 入口 | 说明 |
| --- | --- |
| `/workbench` | AI 工作台（对话、Agent 交互） |
| `/logs` | 日志列表与筛选 |
| `/log/:id` | 日志详情与分析 |
| `/upload` | 日志上传 |
| `/devices` | 设备列表与状态 |
| `/bug-fixes` | Bug 修复工单列表 |
| `/raven-manager` | 软件包管理 |
| `/raven/package/:id` | 软件包详情 |
| `/download` | 客户端下载页 |
| `/admin/project-repos` | 项目仓库管理 |
| `/admin/agent-skills` | Agent 技能配置 |
| `/admin/model-settings` | 模型设置 |
| `/admin/metrics` | 用量统计 |
| `/admin/prompts` | Prompt 后台管理 |
| `/admin/users` | 用户管理 |
| `/admin/releases` | 发布包管理 |

## 相关文档

- [RavenClient](https://github.com/leoguolabs/RavenClient) — RavenAIService 的跨平台桌面客户端
- [QUICKSTART.md](QUICKSTART.md) — 发布、打包、Docker 启停
- [PROJECT_SETUP.md](PROJECT_SETUP.md)
- [DEPLOY_USAGE.md](DEPLOY_USAGE.md)
- [docs/DATABASE_USAGE.md](docs/DATABASE_USAGE.md)
- [docs/API_SUMMARY.md](docs/API_SUMMARY.md)
- [frontend/README.md](frontend/README.md)
