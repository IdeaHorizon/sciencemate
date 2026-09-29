# 技术栈决策文档

> 决策日期：2026-04-23
> 对应 TODO：P0-01
> 状态：已确定

---

## 总体架构

三层架构（已在 related_work.md 第 5.1 节确认）：
- **Graph 运行时层**：自研薄 agent loop（`app/core/agent_loop.py`）
  - R-00 调研曾建议基于 LangGraph 构建，P0-01 原型验证后否决。理由见决策 D1（`design_decisions.md` 附表）与 related_work.md 第 5.1 节。
- **模型层**：Claude Agent SDK + MCP
- **记忆层**：自研（参考 Mem0 图结构 + Hermes 有界记忆/技能提取 + OpenClaw workspace 模式）

---

## 后端：Python + FastAPI

**选择理由**：
1. **AI 生态系统一致性**：Claude SDK、embedding 模型、科学计算栈全部 Python 原生
2. **异步原生**：FastAPI 基于 ASGI，天然支持长时运行的 Agent 任务（节点执行可能持续数分钟）
3. **Pydantic 强类型**：与白皮书定义的复杂数据模型（Graph、Memory、Evidence Chain）完美匹配
4. **开发效率**：热重载 + 自动 OpenAPI 文档 + 类型推导
5. **社区**：AI/科研工具链中最主流的后端选择

**排除选项**：
- Node.js：AI 生态弱，Claude SDK / 科学计算栈都需要 Python 桥接
- Go：开发效率不匹配快速迭代需求，AI 生态几乎为零

**关键依赖**：
- `fastapi` + `uvicorn`：Web 框架
- `sqlalchemy[asyncio]`：ORM（异步）
- `alembic`：数据库迁移
- `pydantic` v2：数据验证和序列化
- `celery` + `redis`：异步任务队列
- `anthropic`：Claude API
- `boto3` / `minio`：对象存储

> Graph 运行时无第三方依赖：agent loop 自研（决策 D1），不引入 `langgraph` / `langchain`。

---

## 前端：Next.js 15 (App Router) + TypeScript

**选择理由**：
1. **React 生态**：图可视化（React Flow）、复杂交互组件生态最成熟
2. **App Router**：Server Components 减少客户端 JS，适合数据密集型科研界面
3. **TypeScript**：前后端类型一致性（通过 OpenAPI schema 生成前端类型）
4. **SSR/SSG**：未来公开文档页面需要

**UI 组件库**：shadcn/ui + Tailwind CSS
- 无运行时依赖，组件可深度定制
- 适合需要高度定制的科研 Workspace 界面

**关键前端依赖**：
- `@xyflow/react`（React Flow）：Research Graph 可视化（P1-20）
- `@monaco-editor/react`：代码编辑器（Experiment 节点）
- `react-markdown` + `rehype`：Markdown 渲染（对话面板、报告展示）
- `zustand`：状态管理
- `swr` 或 `@tanstack/react-query`：数据获取

---

## 数据库：PostgreSQL 16 + pgvector

**选择理由**：
1. **单一数据库简化运维**：早期阶段避免多数据库运维开销
2. **pgvector**：1536 维向量的 HNSW 索引，检索性能满足 KB 规模需求（10K-100K 论文段落级别）
3. **PostgreSQL JSONB**：适合存储半结构化数据（节点 metadata、harness 配置、memory source 等）
4. **事务一致性**：Research Graph 状态转换需要 ACID 保证
5. **成熟度**：生产验证，备份/恢复/监控工具链完整

**后期扩展路径**：
- 如果 KB 规模超过百万级 chunks，可迁移到专用向量数据库（Qdrant）
- pgvector 的 HNSW 索引在 100K 量级足够

**排除选项**：
- 独立向量 DB（Qdrant/Weaviate）：早期增加运维复杂度，pgvector 足够
- MongoDB：缺少关系完整性保证，Research Graph 的边/节点关系需要外键约束

---

## 消息队列：Redis + Celery

**选择理由**：
1. **Celery**：Python 生态最成熟的分布式任务队列
2. **Redis**：既做 Celery broker，也做缓存（Context Engine 缓存热 memory）
3. **任务场景**：
   - 节点执行（长时运行，可能 5-30 分钟）
   - KB 入库流水线（PDF 解析 + embedding 生成）
   - 空闲时记忆优化（后台定时任务）
   - 空闲时 KB 自动更新（定时检索新论文）

**Celery Beat**：定时任务调度器
- 空闲时 KB 更新：每日
- 记忆优化 refinement：容量触发 + 每周定时
- stale 记忆检测：每月

---

## 对象存储：MinIO（开发）/ S3（生产）

**选择理由**：
1. **S3 兼容 API**：开发和生产使用同一套代码
2. **MinIO**：本地开发零成本，Docker 一键启动
3. **存储内容**：Artifact 文件（论文 PDF、代码、数据集、图表、报告）

---

## 开发环境：Docker Compose

统一开发环境，一键启动：
- PostgreSQL 16 + pgvector
- Redis 7
- MinIO
- Backend（FastAPI，热重载）
- Frontend（Next.js，热重载）
- Celery Worker + Beat

---

## Monorepo 结构

```
Agent_research_platform/
├── docs/                           # 设计文档
│   ├── whitepaper.md
│   ├── design_decisions.md
│   ├── related_work.md
│   └── tech_stack.md               # 本文档
│
├── backend/                        # Python FastAPI 后端
│   ├── pyproject.toml              # 依赖管理（uv/poetry）
│   ├── alembic/                    # 数据库迁移
│   │   ├── alembic.ini
│   │   └── versions/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                 # FastAPI app 入口
│   │   ├── config.py               # 配置管理
│   │   ├── database.py             # 数据库连接
│   │   │
│   │   ├── models/                 # SQLAlchemy ORM 模型
│   │   │   ├── __init__.py
│   │   │   ├── graph.py            # P0-05: Node, Edge, Branch, Snapshot
│   │   │   ├── artifact.py         # P0-06: Artifact, ArtifactVersion
│   │   │   ├── knowledge.py        # P0-07: KBEntry, Chunk
│   │   │   ├── memory.py           # P0-08: MemoryEntry, ResearchSkill
│   │   │   ├── evidence.py         # P0-08b: EvidenceChain, Claim
│   │   │   ├── project.py          # P0-12: Project, ProjectConfig
│   │   │   ├── user.py             # P0-04: User
│   │   │   └── budget.py           # P0-10c: Budget, Consumption
│   │   │
│   │   ├── schemas/                # Pydantic 请求/响应模型
│   │   │   ├── __init__.py
│   │   │   ├── graph.py
│   │   │   ├── artifact.py
│   │   │   ├── knowledge.py
│   │   │   ├── memory.py
│   │   │   ├── evidence.py
│   │   │   └── project.py
│   │   │
│   │   ├── api/                    # API 路由
│   │   │   ├── __init__.py
│   │   │   ├── v1/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── graph.py
│   │   │   │   ├── artifacts.py
│   │   │   │   ├── knowledge.py
│   │   │   │   ├── memory.py
│   │   │   │   ├── evidence.py
│   │   │   │   └── projects.py
│   │   │   └── router.py
│   │   │
│   │   ├── core/                   # 核心引擎
│   │   │   ├── __init__.py
│   │   │   ├── context_engine.py   # P0-09: Context 组装
│   │   │   ├── harness/            # P0-10: Harness 执行框架
│   │   │   │   ├── __init__.py
│   │   │   │   ├── base.py
│   │   │   │   ├── loader.py
│   │   │   │   └── executor.py
│   │   │   ├── graph_runtime.py    # P0-11: Graph 运行时
│   │   │   ├── seed_graph.py       # P0-11b: Seed Graph 生成
│   │   │   ├── handoff.py          # Handoff 引擎
│   │   │   ├── tool_registry.py    # P0-10b: 工具注册与权限
│   │   │   └── budget_manager.py   # P0-10c: 预算管理
│   │   │
│   │   ├── services/               # 业务逻辑服务
│   │   │   ├── __init__.py
│   │   │   ├── graph_service.py
│   │   │   ├── kb_service.py
│   │   │   ├── memory_service.py
│   │   │   ├── evidence_service.py
│   │   │   └── project_service.py
│   │   │
│   │   ├── tasks/                  # Celery 异步任务
│   │   │   ├── __init__.py
│   │   │   ├── node_execution.py   # 节点执行任务
│   │   │   ├── kb_ingestion.py     # KB 入库流水线
│   │   │   ├── memory_refinement.py # 记忆优化
│   │   │   └── kb_auto_update.py   # KB 自动更新
│   │   │
│   │   ├── harnesses/              # 节点 Harness 定义文件
│   │   │   ├── survey.yaml
│   │   │   ├── planning.yaml
│   │   │   ├── experiment.yaml
│   │   │   └── analysis.yaml
│   │   │
│   │   └── llm/                    # P0-03: LLM 接入层
│   │       ├── __init__.py
│   │       ├── base.py             # 抽象 LLM provider
│   │       ├── claude.py           # Claude 实现
│   │       ├── openai.py           # OpenAI 实现
│   │       ├── router.py           # 模型路由和降级
│   │       └── metering.py         # Token 计量
│   │
│   └── tests/
│       ├── conftest.py
│       ├── test_models/
│       ├── test_api/
│       └── test_core/
│
├── frontend/                       # Next.js 前端
│   ├── package.json
│   ├── next.config.ts
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   ├── src/
│   │   ├── app/                    # App Router
│   │   │   ├── layout.tsx
│   │   │   ├── page.tsx
│   │   │   ├── projects/
│   │   │   │   ├── [id]/
│   │   │   │   │   ├── page.tsx    # Project workspace
│   │   │   │   │   ├── graph/
│   │   │   │   │   ├── knowledge/
│   │   │   │   │   └── memory/
│   │   │   │   └── new/
│   │   │   └── api/                # BFF 层（可选）
│   │   ├── components/
│   │   │   ├── ui/                 # shadcn/ui 组件
│   │   │   ├── graph/              # Research Graph 可视化
│   │   │   ├── chat/               # 对话面板
│   │   │   ├── evidence/           # 证据链展示
│   │   │   └── workspace/          # Workspace 布局
│   │   ├── lib/
│   │   │   ├── api.ts              # API 客户端
│   │   │   └── types.ts            # 从 OpenAPI 生成的类型
│   │   └── stores/                 # Zustand stores
│   │       ├── graph-store.ts
│   │       └── project-store.ts
│   └── tests/
│
├── docker/
│   ├── docker-compose.yml          # 开发环境
│   ├── docker-compose.prod.yml     # 生产环境
│   ├── backend.Dockerfile
│   └── frontend.Dockerfile
│
├── .github/
│   └── workflows/
│       └── ci.yml                  # CI 流水线
│
├── .gitignore
└── README.md
```

---

## 版本规划

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.12+ | match_case + 性能优化 |
| Node.js | 22 LTS | Next.js 要求 |
| PostgreSQL | 16 | 最新稳定版 |
| pgvector | 0.7+ | HNSW 索引支持 |
| Redis | 7.x | Streams + 性能优化 |
| FastAPI | 0.115+ | Pydantic v2 原生 |
| Next.js | 15 | App Router 稳定 |
| SQLAlchemy | 2.0+ | 异步原生 |

---

## 包管理

- **Python**: `uv`（快速、lockfile 支持、替代 pip/poetry）
- **Node.js**: `pnpm`（workspace 支持、磁盘效率）
- **Monorepo 编排**: 不使用额外工具（Turborepo 等），项目规模不需要
