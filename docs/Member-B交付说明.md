# Member B 开发交付

对应《Demo需求文档》第 5 节和《开发接口文档》第 9 节。B 的数据库与数据任务已实现；前端页面与业务 API 仍按原分工由 C 负责。

## 已完成内容

| 范围 | 实现 |
|---|---|
| 数据库 | 初始 Alembic 迁移、财务表、审计产出版本表、文件登记、用户、客户画像及项目；按项目隔离的数据表强制 RLS（engagements、users、client_profiles 除外） |
| 财务读取 | FinancialDataStore 全部方法，包括期初结转后的总账余额、单行追溯、合同与条款、只读 SQL、10 秒 SQL 超时 |
| 审计产出 | 幂等版本追加、连续版本校验、历史查询、最新版本过滤、并发写入与项目锁定 |
| 事务 | 同项目业务操作共享事务、嵌套保存点、异步任务隔离；证据状态和项目画像更新参与回滚 |
| 证据文件 | PDF 校验、SHA-256 去重、内容寻址、原子文件发布、页文本提取、路径限制、读取时完整性核对 |
| 演示数据 | 23 个科目、46 行试算表、624 张发票、624 张发运单、2 个账户、696 行流水、2,686 行分录、5 份 PDF |
| 导入与 seed | CSV 类型校验、合同登记、事务化幂等导入、载入版本；演示用户 seed 不覆盖已有密码 |
| 规则 | PLN-01、PLN-07、REV-08、CSH-05、CSH-02 全部待实现 SQL；保留 REV-02 已有 SQL |
| 留痕 | 独立事务追加、查询与分页；权限和数据库触发器禁止修改、删除、清空 |
| 外部 mock | ABN/ASIC 查询；发函按项目和幂等键持久化，重启后回执时间及 PDF 字节一致 |
| 联调 | 应用显式共享数据库引擎；运行用 LLM fixtures 对齐真实 PDF 文档 ID；PostgreSQL 检查点恢复阶段和意见枚举 |

## 启动

从项目根目录执行。数据库启动方式沿用 README 中的 Docker Compose。

```bash
cp .env.example .env
uv sync
docker compose up -d db
uv run python data/check_demo.py
uv run alembic upgrade head
uv run python -m backend.orchestration.checkpoint setup
uv run python -m backend.persistence.seed
uv run fastapi dev src/backend/main.py
```

`.env.example` 已增加 `LLM_FIXTURES_ROOT=./data/demo/llm`。已有 `.env` 需要补上这一项；
`tests/fixtures/llm` 中的文档 ID 是离线测试占位 ID，不能用于真实证据库。
用户 seed 读取 `SEED_USER_EMAIL` 和 `SEED_USER_PASSWORD`；原有账户不会被重置。
创建项目并启动编排后，DatasetLoader 会自动载入 `demo`，不需要手动写财务表。

## 数据重建

```bash
uv run python data/generate_demo.py
uv run python data/check_demo.py
```

`data/demo_spec.json` 固化设计文档中的主数据和逐页 PDF 文本；生成脚本不在运行时解析 Markdown。
生成器固定随机种子和 PDF 元数据，自检会在临时目录再次生成并逐文件比较字节。
`manifest.json` 列出文件哈希、CSV 行数及 PDF 的文档 ID。
`.gitattributes` 保留生成数据的原始字节，并强制数据库初始化 shell 脚本使用 LF，防止 Windows 检出改变哈希或破坏脚本。
运行用 `data/demo/llm/` 由原有测试 fixtures 复制并映射文档 ID，未改变 A 的原始 fixtures。
测试配置固定使用原始 fixtures，避免复制 `.env.example` 后运行配置干扰离线测试。
更新测试 fixtures 后应重新运行生成器。

## 验证

本轮结果：常规回归 **964 passed, 8 deselected**；真实 PostgreSQL 集成 **6 passed**；
Ruff、格式检查、mypy（65 个后端源文件）、数据自检与重复生成一致性全部通过。
另在配置运行用 fixtures 的环境下复测原有端到端测试，**6 passed**，确认测试配置正确隔离。

```bash
uv run ruff check .
uv run mypy src/backend
uv run pytest -q
```

真实数据库测试使用**已迁移、已初始化检查点的独立测试数据库**。这些测试创建合成用户和项目，
不会自动删除测试数据；不要指向实际使用中的项目库。

```bash
TEST_DATABASE_URL=postgresql://audit_app:audit_app@localhost:5432/audit \
  uv run pytest tests/test_persistence_postgres.py -m slow -q
```

6 项真实 PostgreSQL 测试覆盖：

- 并发重复导入、账面余额、全部规则查询及未绑定项目时 RLS 拒绝读取。
- 连续版本与幂等写入、证据/画像/项目状态回滚、独立留痕、锁定后禁止写入。
- 证据去重与路径限制、登记 mock、持久化发函幂等。
- 真实 Agent、真实 Store、真实 SQL、mock LLM 下完整 P0–P4 流程。
- PostgreSQL 检查点关闭连接、重新组装后继续复核和归档。
- 缺发运单、分次不足额收款、缺对账单、上期余额为零等 SQL 边界。

完整流程最终得到 4 条例外、6 条确认判断、6 条已决定复核任务、1 条 14,200,000 分错报、
2 版报告、5 份已提取证据，最终项目为 `Archived` 且 `locked=true`。

本轮真实数据库验证使用本机独立 PostgreSQL 18 实例；项目 Compose 保持 PostgreSQL 17。
当前 WSL 未启用 Docker 集成，因此没有通过 Docker 启动本轮测试实例。

## 与 A、C 的衔接

- Store 及外部接口的 Protocol 没有变更。构造注入和新增导入标记见开发接口文档 9.1。
- `main.py` 只增加实际存储需要的共享引擎与目录注入及连接释放。
- `orchestration/checkpoint.py` 修复 PostgreSQL 对顶层 StrEnum 的 JSON 序列化读取问题；
  内存检查点逻辑保持原样。
- 三个原本依赖“数据库尚未实现”的启动单元测试改为显式模拟恢复或项目列表；
  真实存储验证由 PostgreSQL 测试覆盖。
- 本次提交范围为 B 的实现、数据、验证与必要的 A/C 衔接改动；用户原有的报告、视频和幻灯片变动保留在工作区。


## A、C 接手清单

### A：智能体与编排

1. 拉取 main 后执行 `uv sync --frozen`，数据库迁移与检查点初始化按上面的启动步骤执行。
2. 使用真实 Store 演示时设置 `LLM_FIXTURES_ROOT=./data/demo/llm`。修改原始 LLM fixtures 或 PDF 后，重新生成数据并核对 `manifest.json`；不要手工修改文档哈希。
3. 保留 `AuditPostgresSaver`：PostgreSQL 会将顶层阶段和意见枚举存成 JSON 字符串，读取时必须恢复枚举类型。
4. 业务决定应继续通过 `AuditOutputStore.transaction(eid)` 写入，最后才锁定项目；留痕独立提交。现有 Protocol 与领域对象签名不变。

### C：前端与 API

1. `build_deps(settings)` 已注入可工作的真实 Store。接 API 时复用 `app.state.deps`、runner、query 和 decisions，避免在路由中另建一套引擎。
2. 登录通过 `users.by_email()` 读取用户和密码哈希；初始化账号由 seed 创建。项目访问的用户所有权校验仍须在 API 层完成：数据库 RLS 隔离项目数据，不代替用户授权。
3. 创建项目时先通过 DatasetLoader 读取客户画像，再调用 EngagementStore.create，随后由 runner.start 启动编排；DatasetLoader 会按编排要求导入财务数据。
4. 下载证据使用 `evidence_store.path(eid, document_id)`；业务写操作遵守项目 locked 状态，并将 VersionConflict、EngagementLocked 等按接口文档映射成 API 错误。
5. 前端验收路径：承接批准 → 风险及重要性批准 → 六项复核 → 完成阶段批准 → 报告退回补充合同条款 → 再次签字归档。

### 可直接转发的交接摘要

> B 已完成数据库、全部 Store、事务与项目隔离、证据文件管理、规则 SQL、审计留痕、登记/函证 mock、合成数据和 seed。真实数据库下已跑通 P0–P4，并验证中途断开连接后的恢复。常规回归 964 项通过，真实 PostgreSQL 集成 6 项通过。A 需要注意运行用 fixtures 路径及 PostgreSQL 枚举恢复；C 可基于现有 build_deps 继续实现登录、项目、复核、证据和报告 API/UI。完整启动及验证步骤见本文件。
