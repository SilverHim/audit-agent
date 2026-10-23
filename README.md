# 基于大语言模型的多智能体审计支持系统

面向澳大利亚注册会计师（RCA）的审计支持系统。系统按承接、计划、执行、完成、报告五个阶段推进法定审计：智能体执行审计程序、分析证据、形成初步判断，法定判断与签字由审计师在决策点作出。每条判断都附推理链与证据指针，可追溯到原始凭证。

> ELEC5620 Stage 2 原型 · Group 16

## 功能

- **五阶段审计流程**：状态机驱动 P0–P4，四道法定决策点暂停等待审计师决定，可从断点恢复
- **程序库**：161 条审计程序以 YAML 配置，按风险等级自动装配，规则、混合、大语言模型、人工四类引擎执行
- **可追溯判断**：判断对象含推理链、反方论证、置信度与证据指针，输出经四级校验
- **独立复核**：与生成方不同的模型对判断独立复核，不一致进入人工仲裁
- **单一对话入口**：审计师在对话面板中查询进度与来源、处理复核队列、作出决定
- **全程留痕**：模型调用、人工操作、状态迁移逐次记录

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.13 · FastAPI · Pydantic v2 |
| 智能体编排 | LangGraph · langgraph-checkpoint-postgres |
| 数据库 | PostgreSQL · SQLModel · Alembic |
| 前端 | React 19 · TypeScript · Vite · TanStack Router / Query · Tailwind CSS · shadcn/ui |
| 文档解析 | pdfplumber · openpyxl |
| 工程 | uv · ruff · mypy · pytest · Bun · Biome · Playwright · Docker Compose |

## 系统架构

```
前端                 React 对话面板 · 复核队列 · 下钻视图
API                  FastAPI 路由（认证、对话、复核、决定提交、状态查询、上传）
编排层               LangGraph 状态机：五阶段、决策点中断、并行调度与汇合
智能体层             10 个角色 Agent，共享基类
支撑服务             程序库 · 规则引擎 · 判断引擎与输出校验 · 复核队列 · 审计留痕
知识层               程序 YAML · 规则 · 准则片段（Git 版本化）
持久化               PostgreSQL（财务数据、审计产出、检查点）· 证据原件文件存储
```

智能体与服务只依赖接口，数据库与大语言模型的具体实现在启动时注入。

## 目录结构

```
.
├── docs/
│   ├── ELEC5620_设计规格说明书.md
│   ├── Demo需求文档.md
│   ├── 开发接口文档.md
│   ├── 合成数据集设计.md
│   ├── 协作规范.md
│   ├── diagrams/               UML 模型图（PlantUML）
│   ├── slides/                 系统设计讲解幻灯片
│   ├── 课程要求/               项目说明与评分标准
│   ├── 参考资料/               准则与法规原文
│   └── 归档/                   仓库早期文件
├── src/
│   ├── backend/
│   │   ├── main.py             应用入口
│   │   ├── api/                API 路由
│   │   ├── core/               配置与安全
│   │   ├── contracts/          领域对象与接口
│   │   ├── knowledge/          程序库、规则、准则片段
│   │   ├── services/           支撑服务
│   │   ├── agents/             智能体
│   │   ├── orchestration/      LangGraph 编排
│   │   ├── persistence/        数据库实现
│   │   ├── adapters/           大语言模型与外部数据源
│   │   └── migrations/         数据库迁移（Alembic）
│   └── frontend/               React 前端
├── tests/
├── eval/                       评估集与指标
├── data/                       合成演示数据
├── docker/                     数据库初始化脚本
├── pyproject.toml
├── langgraph.json
└── compose.yml
```

## 快速开始

环境要求：Docker、[uv](https://docs.astral.sh/uv/)、[Bun](https://bun.sh/)

```bash
git clone https://github.sydney.edu.au/ELEC5620-Wed-14-16-Group-16/ELEC5620-demo.git
cd ELEC5620-demo
cp .env.example .env
docker compose up -d db
```

本机 5432 端口被占用时，改用 `DB_PORT=55432 docker compose up -d db`，并把 `.env` 里两个数据库连接的端口改成 55432。默认 `LLM_MODE=mock`，不需要 API Key。然后启动后端：

```bash
uv sync
uv run python data/check_demo.py                       # 检查已随仓库提供的合成数据
uv run alembic upgrade head
uv run python -m backend.orchestration.checkpoint setup   # 建检查点表，首次执行一次
uv run python -m backend.persistence.seed                # 创建 .env 配置的演示审计师 (姓名 Jordan Ellis, 已有账号不改)
uv run fastapi dev src/backend/main.py
```

另开终端启动前端：

```bash
cd src/frontend
bun install
bun run dev
```

默认新建项目选用 `demo-compact` 精简合成演示：两份收入合同、一项分录审批例外、两项错报，支持管理层回复、更正验证和报告归档。完整 Numbat 与 Xero 演示仍可在新建项目时选择。工作台标明当前使用演示模型输出或真实模型 API；执行过程可刷新重放，批量复核操作须核对固定范围后授权。详见 [工作台与授权助理](docs/云端开发/工作台与授权助理.md)。

- 前端：http://localhost:5173
- API 文档：http://localhost:8000/docs

## 开发

```bash
uv run pytest                                      # 测试
uv run ruff check . && uv run mypy src/backend   # 代码检查
uv run langgraph dev                               # 在 LangGraph Studio 中调试编排图（仅限合成或公开数据）
cd src/frontend && bun run generate-client        # 后端接口变动后重新生成前端客户端
```

B 角色实现、数据重建与真实 PostgreSQL 验证步骤见 [B 角色交付说明](docs/Member-B交付说明.md)。

提交流程与代码检查见 [协作规范](docs/协作规范.md)。

## 团队

| 模块 | 负责 |
|---|---|
| 智能体与编排 | Member A |
| 数据库与数据 | Member B |
| 前端与 API | Member C |
