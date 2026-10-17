# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目

ELEC5620 Group 16：基于大语言模型的多智能体审计支持系统，按 P0 承接 → P4 报告推进澳洲法定审计。后端 FastAPI + LangGraph + PostgreSQL（`src/backend/`），前端 React 19 + Vite（`src/frontend/`）。

当前是 Demo 版：程序库只放 17 条程序（README 写的 161 条是完整设计），处于里程碑 M2（骨架跑通）。未实现的部分用 `NotImplementedError` 占位，`grep -rn NotImplementedError src/backend` 可看全貌。

## 权威文档

实现以 `docs/` 为准，代码 docstring 常按节号引用：

| 文档 | 内容 |
|---|---|
| `docs/开发接口文档.md` | 接口契约。第 0 节约定与 ID 规则，第 1 节接口归属，第 3 节编排状态，第 4–5 节 ports 与服务，第 6 节 Agent，第 7 节 runner、recover 规则、DecisionSubmit，第 8 节程序 YAML 格式，第 9 节数据库表，第 10 节 HTTP API，第 11 节配置项 |
| `docs/Demo需求文档.md` | Demo 范围。第 4.1 节主图流程，第 5 节逐文件分工，第 6 节本版不做，第 9 节里程碑 |
| `docs/合成数据集设计.md` | 合成公司 Numbat 的数据与各程序预期结果；测试样例数据出自这里 |
| `docs/Xero演示数据集设计.md` | Xero 演示公司的补造数据与各程序预期结果 |
| `docs/ELEC5620_设计规格说明书.md` | Stage 1 完整设计，范围大于 Demo |
| `docs/协作规范.md` | 提交流程与分工边界 |
| `docs/参考资料/` | ASA、AASB 准则原文 PDF，准则段落号以此为准 |

`docs/归档/` 是早期文件，不再维护。`learn/` 是本地 LangGraph 示例，经 `.git/info/exclude` 排除，不入库。

## 常用命令

```bash
# 首次准备
cp .env.example .env
docker compose up -d db          # 5432 被占用时用 DB_PORT=55432，并改 .env 里两个连接串的端口
uv sync
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
uv run alembic upgrade head
uv run python -m backend.orchestration.checkpoint setup   # 以属主角色建检查点表，执行一次

# 后端
uv run fastapi dev src/backend/main.py    # API 文档 http://localhost:8000/docs
uv run langgraph dev                      # LangGraph Studio 调试主图（入口 orchestration/studio.py）

# 测试与检查（pre-commit、pre-push 与 CI 跑同一套）
uv run pytest                             # 默认 -m 'not slow'，不需要数据库
uv run pytest tests/test_graph.py::test_name   # 单个测试；也可 -k 关键字
uv run pytest -m slow                     # 连数据库或真实 LLM 的测试
uv run ruff check . && uv run ruff format . && uv run mypy src/backend   # mypy 为 strict

# 前端（在 src/frontend/ 下）
bun install && bun run dev                # http://localhost:5173
bun run lint && bun run typecheck         # biome + tsc -b
bun run generate-client                   # 后端接口变动后重新生成 src/client/

# UML 图
docs/diagrams/render.sh [file.puml]       # 需要 ~/.local/lib/plantuml.jar 与 graphviz，输出到 out/
```

## 后端架构

### 分层与依赖注入

- 领域类型只在 `contracts/` 定义，其他包只引用。`contracts/ports.py` 用 Protocol 定义存储、外部系统与 LLM 接口，上层只依赖 Protocol。
- 组装根 `main.build_deps()` 构造全部具体实现，打包成 `AgentDeps`（frozen dataclass）。`lifespan` 打开检查点、编译主图，把 `GraphRunner`、`GraphStateQuery`、`GraphDecisionSubmit` 挂到 `app.state`，路由经 `api/deps.py` 取用。
- 领域对象为 `frozen=True, extra="forbid"`。存入审计产出的对象继承 `Versioned`（`id` + `version`），修改一律 `model_copy(update=...)` 并 version +1，旧版本不覆盖。ID 确定性生成（接口文档第 0 节），重放得到同样的 ID，写入因此幂等。
- 金额是整数分，字段以 `_cents` 结尾；时间存 UTC。

### 编排（`orchestration/`）

- `graph.py` 定义主图拓扑，模块 docstring 是流程总览。一个项目对应一个线程，`thread_id = engagement_id`。
- 四道闸门 `gate_acc01`、`gate_p1_risk`、`gate_fin`、`gate_rpt01` 编译为 `interrupt_before` 静态中断。闸门节点是空操作；出口由 `gate_route` 按本闸门最后一条 `GateDecision` 选择。决定须绑定当前材料的 `(id, version)`，缺失或过期就走 `represent` 回到闸门自身重新呈报。ACC-01 另有 `rerun`：客户画像更新后回 `p0_intel` 重跑 ACC-04。
- 审计师决定的唯一写入口是 `decisions.GraphDecisionSubmit`：取项目锁 → 校验（失败抛 `DecisionRejected(code)`）→ 一个事务内写 Store → `aupdate_state(..., as_node=闸门节点)` 越过闸门 → `runner.launch` 后台继续。先写 Store 后写检查点；两者不一致时拒绝新决定并报 `RESUME_REQUIRED`。
- `runner.GraphRunner`：每个项目一把内存锁（取不到报 `ENGAGEMENT_BUSY`），用 `asyncio.create_task` 在后台 `ainvoke` 到下一个中断。后台异常记入 `run_error`，不外抛。只支持单 worker。
- P2 实质性程序：`assemble_pack` 按审计目标匹配程序 `triggers` 并补齐依赖闭包 → `scheduler.build_schedule` 按依赖拓扑分层，同层按业务循环分组，组数不超过 `PARALLEL_CAP` → `route_schedule` 用 `Send` 并行派发 `fieldwork` 实例 → `fan_in` 汇合 `staged_results` → `after_fan_in` 决定再调度、`await_events`（`interrupt` 等复核）或进入 `completion`。派不出程序却仍有未完成的程序时抛 `ScheduleStuck`，避免空转到递归上限。
- `GraphNodes.execute` 执行 P2 以外的程序：建 `ProcedureRun` → 调 Agent → 在一个事务内用 `save_outputs` 写 Store，状态取 Store 返回的对象。
- `contracts/state.py` 的 `EngagementState` 中列表字段用自定义 reducer：`upsert_by_id`（版本更低抛 `VersionConflict`）、`replace_by_id`、`append_unique`、`staged`（收到 `None` 清空）。节点返回 `StatePatch`，即只含要更新字段的 dict。
- 检查点序列化白名单（`checkpoint.checkpoint_serde`）只自动登记 `contracts/models.py` 与 `contracts/services.py` 中的 Pydantic 类与枚举。放进状态的新类型若定义在别处，读回时会被拒绝；枚举不报错，而是退化成字符串，`is Phase.X` 恒为假。

### Agent 与知识层

- `agents/base.ProcedureAgent[F]`：子类声明 `agent_id` 与 `procedures`，实现 `execute`（返回原始发现）和 `interpret`（转成 `StatePatch`，须含本次 `procedure_runs` 的终态）。`run` 是模板方法，不覆写。Agent 只读 Store，写入由节点完成。
- `agents/registry.build_agents` 建“程序编号 → Agent”映射；Agent 声明的程序在 `knowledge/procedures/` 没有 YAML 时启动即报错。闸门步骤（ACC-01、PLN-01/03 的决定、FIN-01、RPT-01）不经 Agent。
- `knowledge/procedures/*.yaml` 定义程序：`engine`（rule、llm、hybrid、manual）、`autonomy_level`、`triggers`、`params`（字面量、`{by_risk: ...}` 或 `{ref: ...}`）、`depends_on`、`rules[].sql`、`llm.prompt`（指向 `prompts/*.md.j2`）。复核规则读 YAML 的 `always_review`、`manual_result_allowed`，不按程序编号写分支。`ProcedureLibrary.commit` 记录 knowledge 目录的 git HEAD。
- LLM：`LLM_MODE=mock` 时读 `tests/fixtures/llm/{procedure_id}/{fixture_key}.json`，找不到读同目录 `default.json`；live 模式尚未实现。

### 持久化

`DATABASE_URL` 是受行级安全约束的应用角色，每个事务开头用 `persistence/db.bind_engagement` 设 `app.engagement_id`；迁移与检查点建表用 `DATABASE_OWNER_URL`。存储方法的第一个参数都是 `eid`（`EngagementStore`、`UserStore` 除外）。

## 测试

- `tests/conftest.py` 强制 `CHECKPOINT_BACKEND=memory`、`LLM_MODE=mock`。
- `tests/fakes.py` 提供假 Store、Trail、Registry 与 Numbat 样例数据。`make_deps` 在 `build_deps()` 基础上替换依赖，`make_runner` 编译带 `InMemorySaver` 的主图。runner 在后台推进，断言前须 `await wait_idle(runner, eid)`。
- 连数据库或调真实 LLM 的测试标 `@pytest.mark.slow`。

## 约定

- 分工：A 智能体与编排；B 数据库与数据（`persistence/`、`rule_engine.py`、`audit_trail.py`、mock 适配器、迁移、`data/`、程序 YAML 中的 SQL）；C 前端与 API（`api/`、`core/`、`main.py`、`src/frontend/`）。逐文件归属见接口文档第 1 节、Demo需求文档第 5 节。
- 改 `contracts/` 时，在同一次提交里同步改 `docs/开发接口文档.md`。实现与文档不一致时，先确认再一起改。
- Python 注释与 docstring 写中文，用半角标点（`说明: 内容, 内容.`）。
- 只用 `main` 分支，提交信息用 Conventional Commits（`type: 中文标题`），不 force push。未完成的功能用 `NotImplementedError` 占位，保证 main 随时能启动。
- `.gitignore` 忽略了 `data/`，其下新文件须 `git add -f`。
- 测试与发给 LLM 的数据可用合成数据，或公开发布且许可允许的数据集（如 CUAD 合同、Xero Demo Company）；不用非公开的真实客户数据。外部数据集放 `data/external/<名称>/`，同目录 README 写明来源与许可，大文件不入库。
- `src/frontend/src/client/` 与 `src/frontend/src/routeTree.gen.ts` 是生成文件，不手改。后端 `operation_id` 为 `{tag}-{name}`，决定生成的前端函数名。

## 云端开发

本仓库 (个人仓库 audit-agent) 用于云端开发, 无法连接悉尼大学 GitHub. 云端会话先读 `docs/云端开发/开发提示词.md`, 计划总纲在 `docs/云端开发/计划-第1至3级.md`, 开发流程与审核规则在同目录. Xero 数据已随仓库放在 `data/external/xero/`.
