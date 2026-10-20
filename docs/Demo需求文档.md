# Demo 需求文档

第一版可运行原型：P0–P4 全流程跑通，每个阶段至少有一两条程序演示，审计执行只做收入和银行函证两条线，最后出具审计报告。规格书中其余设计本版不实现。

## 1. 演示流程

```
P0 承接   录入客户 ABN → ACC-04 查询 ASIC / ABN（mock）→【闸门】ACC-01 是否承接
P1 计划   载入合成数据集 → DOC-04 合同抽取 → PLN-07 初步分析 → PLN-01 重要性候选
          →【闸门】PLN-01/03 确认重要性与风险清单 → 装配程序包
P2 执行   收入：REV-08 三单匹配、REV-03 合同条款评价 → REV-02 截止性判断
          函证：CSH-04 发函 → DOC-06 回函抽取；CSH-05 流水账面核对 → CSH-02 差异评价
          需要人的结果进复核队列，审计师逐条决定
P3 完成   FIN-04 错报累计与候选意见 →【闸门】FIN-01 确认意见类型
P4 报告   RPT-03 生成报告初稿 →【闸门】RPT-01 附批注退回修订，或签字归档
```

Xero 演示数据集（xero/20261017-q1）的种子载入（`python -m backend.persistence.seed load`）只载快照；演示时还要做演示数据导入（需求 R269，数据管理页的按钮见 R270）：停在 ACC-01 闸门时导入承接资料，停在等数据时导入外勤的账表与文件，再由审计师确认数据已齐；到完成阶段按补交请求导入待审财报。分阶段导入的演示用不经种子载入的新项目（数据版本 0）；种子载入过的项目视同已确认就绪，图不停在等数据，做外勤导入时快照七张表（含应付账单，需求 R178）会再写一版。

## 2. 程序清单

共 18 条：下表 17 条，加贯穿程序 GEN-09 判断独立复核（A7，由 A6 每生成一条模型类判断即同步调用，不单独派发）。

| 阶段 | 程序 | 引擎 · 自主度 | Agent | 本版做法 |
|---|---|---|---|---|
| P0 | ACC-04 ASIC / ABN 核查 | 规则 · L3 | A2 | 查 mock 登记数据：ABN 有效、名称一致、公司状态正常，不符出 Exception |
| P0 | ACC-01 是否承接 | 闸门 · L1 | A2 | 呈递 ACC-04 结果与客户画像；承接进 P1，拒绝进 Declined，信息不足留在闸门；审计师更新客户资料后自动重跑 ACC-04 并重新呈报 |
| P1 | DOC-04 合同要素抽取 | LLM · L2 | A3 | 读合同 PDF 文本层，抽出合同对象与条款，每个字段带页码指针；结果直接入库，前端可查看，不建复核任务 |
| P1 | PLN-07 初步分析程序 | 规则 · L3 | A4 | 资产负债类本期末对期初（上期损益结转进留存收益）、损益类在两期等长时对上期逐科目比较，变动同时达到本期收入的 0.5% 与基数的 20% 出 Exception；算毛利率、收入增长率；按月汇总本期总账的收入、费用与员工费用（月度趋势，闸门页显示，不出 Exception） |
| P1 | PLN-01 重要性水平 | 闸门 · L1 | A4 | 规则按收入、总资产、税前利润三个基准算候选区间；审计师选定基准与比例，系统算出执行重要性与明显微小门槛 |
| P1 | PLN-03 风险清单终审 | 闸门 · L1 | A4 | 与 PLN-01 同一道闸门；审计目标与风险清单由数据集预置，审计师可改风险等级 |
| P2 | REV-08 凭证三单匹配 | 规则 · L3 | A6 | 期末前后 window_days 内的销售发票与发运单、收款匹配；缺单、跨期、金额不符出 Exception |
| P2 | REV-03 收入合同条款评价 | LLM · L2 | A6 | 读 DOC-04 的合同对象，判断控制权转移时点与异常条款，每份合同一条 Judgment |
| P2 | REV-02 收入截止性判断 | 混合 · L2 | A6 | 规则取 REV-08 的全部例外（跨期、缺发运单或无签收日、收款金额不符）所指的发票与期末窗口内超过执行重要性的发票；LLM 结合发运、签收或服务完成日与 REV-03 结论判断归属期间（上下文另带该发票的过账分录行，错报金额取不含 GST 的收入行），每笔一条 Judgment |
| P2 | CSH-04 函证发起 | 规则 · L4 | A5 | 按银行账户生成询证函，经 mock 函证平台发出，平台立即返回预置回函 PDF |
| P2 | DOC-06 函证回函抽取 | LLM · L3 | A3 | 读回函 PDF，抽出银行、账号、余额、日期、回函渠道与截至余额日尚未入账的应计利息（回函没提为空），写 Confirmation |
| P2 | CSH-05 流水账面核对 | 规则 · L4 | A6 | 银行对账单期末余额与总账银行科目余额比较，不符出 Exception |
| P2 | CSH-02 函证差异评价 | 混合 · L3 | A6 | 规则算回函余额与账面余额的差额，列出期后流水中的候选调节项，并核对回函写明的应计利息是否已入账；LLM 解释差异并建议是否为错报；相符且应计利息已入账才自动关闭，有差异或应计利息未入账进复核队列 |
| 贯穿 | GEN-09 判断独立复核 | LLM · L4 | A7 | 程序运行时自主度 L3、L4 且建议接受或关闭、推理链含假设、或 5% 抽样的判断由复核模型两轮复核：第一轮只看证据独立得出结论与推理链，第二轮再比对 A6 结论；一致计入置信度，不一致建争议任务由审计师仲裁（采用 A6、采用 A7 或人给结论，可附错报）；L1、L2 跳过 |
| P3 | FIN-04 错报累计 | 规则 · L3 | A9 | 累计高于明显微小门槛的未更正错报，出汇总表；与整体重要性比较得候选意见 |
| P3 | FIN-01 未更正错报评价 | 闸门 · L1 | A9 | 呈递汇总表与候选意见；审计师确认意见类型并写理由 |
| P4 | RPT-03 报告文本生成 | LLM · L2 | A10 | 固定段落套模板，LLM 只写意见段与形成意见的基础段，事实断言回指已确认的 Judgment；按批注修订，每轮版本 +1 |
| P4 | RPT-01 审计报告批准与签字 | 闸门 · L1 | A10 | 附批注退回则回 RPT-03 修订；签字后报告锁定，项目进 Archived，拒绝一切写入 |

**依赖**。YAML 的 `depends_on` 只写本表内的程序，规格书 6.1.2 中的其余前置不建模。FIN-04 在 P2 程序包全部结清后执行。

```
ACC-01 ← ACC-04            REV-03 ← DOC-04            DOC-06 ← CSH-04
REV-02 ← REV-03、REV-08     CSH-02 ← CSH-05、DOC-06     FIN-01 ← FIN-04
RPT-03 ← FIN-01            RPT-01 ← RPT-03
```

P2 程序包分三层：第 0 层 REV-08、REV-03、CSH-04、CSH-05；第 1 层 REV-02、DOC-06；第 2 层 CSH-02。

## 3. 合成数据集

一家虚构的非上市公司，一个财年，截至 2026-06-30。全部放在 `data/`，P1 时按项目载入。

| 数据 | 内容 |
|---|---|
| 客户与登记 | 客户画像；ABN Lookup 与 ASIC 查询的 mock 返回 |
| 财务数据 | 科目表、本期与上期试算表、总账分录、销售发票、发运单、银行流水（含期后 10 天） |
| 文件 | 销售合同 PDF 3 份、银行询证函回函 PDF 2 份，均带文本层 |
| 计划 | 审计目标与风险清单：收入的发生与截止为特别风险，货币资金的存在为高风险 |

预置场景与预期结果：

| 场景 | 数据 | 预期 |
|---|---|---|
| 截止错报 | 一张 2026-06-28 入账的发票 AUD 142,000，发运单 2026-07-03 签收；合同约定签收时控制权转移 | REV-08 出跨期例外；REV-02 建议认定错报；审计师确认为错报 |
| 正常截止 | 一张 2026-06-29 的发票 AUD 95,000（高于执行重要性上限，会被 REV-02 选中），发运单 2026-06-30 签收 | REV-02 判断归属期间正确；审计师确认 |
| 函证差异 | 回函余额比账面少 AUD 50,000，差额为 2026-06-30 入账、2026-07-02 到账的在途存款 | CSH-02 解释为时间性差异，建议不认定错报；审计师确认 |
| 意见 | 收入约 AUD 1,200 万，按收入基准的整体重要性候选区间为 60,000–120,000 | 截止错报高于整体重要性，候选意见为保留意见 |

## 4. 骨架

### 4.1 编排

LangGraph `StateGraph`，检查点用 `AsyncPostgresSaver`，thread_id 即 engagement_id。

```
p0_inputs → p0_intel → gate_acc01 → p1_await_data → p1_ingest → p1_analytics → gate_p1_risk → assemble_pack
  → schedule ⇉ fieldwork × N ⇉ fan_in → { schedule | await_events | completion }
completion → gate_fin → report_draft → gate_rpt01 → { report_draft | archive }
```

- p0_inputs：新客户（项目没有数据集）先经 ABN Lookup 查是不是公司，是公司而没有上传 ASIC 摘录时把 ACC-04 置等资料（awaiting_data，A3 提出补交请求 REQ-registry_extract），在节点内 `interrupt()` 等审计师上传；上传后 A3 抽取，数据变动带动图回到 p0_inputs 再判一次，有摘录或请求标为客户无此类资料后进 p0_intel（需求 R290）。有数据集的项目、不是公司或查不到 ABN 的不等，直接进 p0_intel。
- p1_await_data：建项目不载入财务数据，ACC-01 通过后在节点内 `interrupt()` 等审计师在数据管理页接入数据并确认已齐（数据集载入视同确认，不等）。
- 静态中断 4 个：gate_acc01、gate_p1_risk、gate_fin、gate_rpt01。闸门决定写入时按 GateDecision 路由（approve / reject / insufficient_info）。
- 闸门的非通过出口：ACC-01 拒绝进 Declined 结束，客户画像更新后回 p0_inputs 重跑 ACC-04；PLN-01/03 驳回回 p1_analytics；FIN-01 驳回回 completion；RPT-01 驳回回 report_draft。四道闸门的信息不足都留在原闸门重新呈报。
- assemble_pack 按审计目标的风险等级匹配程序 trigger，补齐依赖；每条审计目标至少被一条程序覆盖，否则阻断。
- schedule 按依赖图拓扑分层，同层按循环分组，用 `Send` 派发 fieldwork 实例，上限 4；fieldwork 按程序的 owner 调用对应 Agent。
- fan_in 用按 ID 合并的 reducer 收各实例返回的 ProcedureRun、Exception、Judgment、ReviewTask，由 A1 重算程序状态：有待决任务为 awaiting_human，否则为 completed；rerun 决定时由 DecisionSubmit 先置 executing，不受待决任务影响。
- P2 以外的程序（ACC-04、DOC-04、PLN-07、PLN-01、FIN-04、RPT-03）由所在节点直接执行。整次失败（Agent 抛异常或片段校验不过）时，节点写入 failed 的执行记录与 failed 任务；单个对象失败（如 DOC-04 一份合同抽取失败）时，执行记录仍为 succeeded，Agent 的片段照常写入，其中带该对象的 failed 任务。两种情况程序都置 awaiting_human，然后在节点内 `interrupt()`；审计师选 rerun 后在同一节点内重跑。
- after_fan_in：先派发后等复核。有可派发的程序（planned 或 executing，前置都已 completed）回 schedule，不因别的程序等复核而停下；没有可派发的而有待决复核任务进 await_events，节点内 `interrupt()`，复核决定写入后恢复；P2 程序全部 completed 进 completion。程序状态与任务对不上（awaiting_human 却无待决任务，或有待执行程序却一个也派不出）时抛 ScheduleStuck，不空转。
- 项目阶段：P0_Acceptance、P1_Planning、P2_Executing、P3_Completion、P4_Reporting、Archived、Declined。程序状态：planned、executing、awaiting_human、completed。
- EngagementState 的字段、reducer 与写入归属见 [开发接口文档.md](开发接口文档.md) 第 3 节。

### 4.2 判断与复核

- 规则程序出 Exception，不建复核任务：REV-08、CSH-05 的 Exception 由 REV-02、CSH-02 引用归因，ACC-04、PLN-07 的 Exception 作闸门材料。
- LLM 与混合程序出 Judgment：结论、推理链、证据指针、反方论证、建议动作、置信度。推理链中 fact 步骤须带证据指针，standard 步骤指向准则片段，inference 步骤引用前序步骤。
- OutputValidator 做四级校验：语法、结构、引用、语义。语法与结构不通过时把错误回传 LLM，最多重试 2 次；重试耗尽后，混合程序的对象有规则例外时建 unattributed 任务，否则建 failed 任务；LLM 程序按对象建 failed 任务，不影响同一 Run 的其他对象。
- 置信度由证据覆盖率、规则一致性、LLM 自报三项加权，权重放在 rules.json。
- 以下情况需要人：自主度为 L1 或 L2；建议动作为 misstatement 或 more_evidence；CSH-02 有差异或回函应计利息未入账；校验失败或程序执行失败。其余自动关闭。
- 复核决定只做五种：

| 决定 | 适用任务 | 效果 |
|---|---|---|
| confirm | normal | Judgment 置 confirmed |
| override | normal、unattributed | 按人的结论另建人工 Judgment；normal 任务的旧 Judgment 置 overridden 并记 superseded_by |
| misstatement | normal、unattributed | 按 misstatement_draft 建 Misstatement，本版一律按未更正处理 |
| rerun | failed | 程序回到 executing；P2 程序重新派发，P2 以外的程序在所在节点内重跑 |
| manual_result | failed（只限产出 Judgment 的程序，且任务带 subject_id） | 人的结果成为 Judgment |

- DecisionSubmit 是唯一写入口：校验目标版本和任务类型允许的决定，写入后重算程序状态并恢复流程。前端所有决定都走这里。
- 每次 LLM 调用、每个人的决定、每次状态迁移各写一行 audit_trail。

### 4.3 数据库

PostgreSQL。除 users、engagements、client_profiles 与检查点表外，所有表带 engagement_id 并启用行级安全策略，前三张由应用层按 owner_id 校验归属；金额以分为单位存 BIGINT。

| 类别 | 表 |
|---|---|
| 项目与用户 | users、engagements（含版本快照）、client_profiles |
| 财务数据（关系表） | accounts、trial_balance、journal_entries、sales_invoices、delivery_notes、bank_accounts、bank_transactions、documents、contracts、contract_terms |
| 审计产出（JSONB，按 id + version 追加） | audit_objectives、risk_register、procedures（含 ProcedureRun）、exceptions、judgments、review_tasks、decisions（GateDecision 与 ReviewRecord）、confirmations、misstatements、report_versions、gate_materials（闸门材料） |
| 留痕 | audit_trail，只追加 |
| 检查点 | LangGraph 自建的表 |

sales_invoices、delivery_notes、bank_accounts 为本版新增，供 REV-08、CSH-04、CSH-05 使用。PDF 原件存 `engagements/{id}/evidence/`，路径与哈希登记在 documents。逐列定义见开发接口文档第 9 节。

### 4.4 API

端点、请求与响应、错误码见开发接口文档第 10 节。所有端点校验登录与项目归属；流程在后台任务中推进，前端轮询项目总览。

### 4.5 前端

| 页面 | 内容 |
|---|---|
| 登录、项目列表 | 新建项目：选客户（`GET /clients`，每个客户一行）与期初日、期末日；数据集客户传其数据集，客户名称与 ABN 取数据集的客户画像；下拉末项"新建客户"登记没有数据集的客户（法定名称、ABN、主体类型、行业、是否上市、董事、备注与 4 项常用扩展字段，ABN 按 ABR 的校验位就地核对，重复时改选已有客户），按 client_id 建项目（需求 R288） |
| 项目总览 | P0–P4 进度条、当前闸门、程序状态表、复核待办数 |
| 闸门 | 四道闸门各一个表单：展示材料，选 approve / reject / insufficient_info，填理由和逐项修改（P1 改重要性与风险等级，RPT-01 写批注）；ACC-01 另可更新客户资料，保存后自动重跑 ACC-04 |
| 复核队列 | 任务列表；详情页并排显示结论、推理链、证据、反方论证、置信度；只显示 ReviewQueue.allowed 给出的决定 |
| 证据查看 | 点击证据指针打开 PDF 并跳到对应页；分录、发票、发运单明细 |
| 合同 | DOC-04 抽出的合同与条款，条款链接到原文页 |
| 错报与意见 | 错报列表、汇总表、候选意见 |
| 报告 | 当前版本预览、版本历史与批注、签字入口 |
| 审计留痕 | 按时间列出 LLM 调用、人的决定、状态迁移 |

## 5. 代码清单与分工

A：智能体与编排；B：数据库与数据；C：前端与 API。

```
src/backend/
├── main.py                        C  组装根：注入 Store、适配器、检查点，挂路由
├── core/                          C  配置、JWT
├── api/routes/                    C  4.4 各端点
├── contracts/                     A  models.py、records.py、knowledge.py、services.py、state.py、views.py、llm_outputs.py、errors.py、
│                                     ports.py（Store 部分与 B 共同确定）；定义见开发接口文档
├── knowledge/
│   ├── procedures/*.yaml          A  17 个；规则程序的 SQL 由 B 写
│   ├── standards/*.yaml           A  准则片段（第 7 节）
│   ├── prompts/                   A  LLM 程序的 prompt 模板
│   ├── templates/                 A  报告固定段落
│   └── rules.json                 A  置信度权重
├── services/
│   ├── procedure_library.py       A  加载 YAML、trigger 匹配、依赖闭包
│   ├── judgment_engine.py         A  上下文组装、LLM 调用、OutputValidator、置信度
│   ├── review_queue.py            A  建任务、任务类型与决定的对应
│   ├── rule_engine.py             B  绑定参数执行 YAML 中的 SQL，结果包装为 Exception
│   └── audit_trail.py             B  只追加写入
├── agents/                        A  base.py、client_intel.py（A2）、ingest.py（A3）、risk_assessor.py（A4）、
│                                     sheets.py（A3，上传账表识别）、evidence.py（A5）、fieldwork.py（A6）、qc.py（A9）、
│                                     concierge/（A10：agent.py 按程序分派，report.py 为 RPT-03）
├── orchestration/                 A  graph.py、nodes.py、scheduler.py、decisions.py、runner.py、query.py
├── persistence/                   B  表模型、五个 Store 与 DatasetLoader 的实现、行级安全、事务
├── adapters/
│   ├── llm.py                     A  真实提供商；MockLLMClient 按程序返回固定输出
│   ├── registry_mock.py           B  ABN / ASIC
│   └── confirmation_mock.py       B  发函即返回预置回函
└── migrations/                    B
src/frontend/                      C
data/                              B  合成数据集与 seed 脚本
tests/                             B  持久化与规则 SQL；C  API 与端到端；A  LLM fixtures（tests/fixtures/llm/）
compose.yml                        B
```

## 6. 本版不做

- A8 底稿、A10 对话入口
- 自主度动态降级（需求清单 R168、R170 接入前）、环 1 至环 3 与 GATE-R
- 盲判（L1 的 blind 标志）、9.2 盲测抽查、人机一致率统计与按一致率的降级和升级：用户 10-19 删去（规格书第 7.1、7.4、9.2、9.5 节），降级越过 L1 时截在 L1
- 认定错报不按程序限制（T12）：分录归因与科目程序可能对同一笔各建议一次，分类与更正验证的判断也可认定错报，由审计师避免重复
- 错报沟通与更正（FIN-03、FIN-08、FIN-09）、持续经营（FIN-02）、完整性自检（FIN-05）
- 函证催函与截止定时器、未回函替代程序、管理层拒绝寄发
- ACC-08、PLN-15 两个闸门步骤，EQR
- 原型替身与 not_implemented 注册：程序库只放本版 17 条
- 向量索引、文档分类与 OCR：数据集自带文档类型，只读 PDF 文本层
- Xero 收款与发票对应表（payments.json 的分配记录），因而期后监控 GEN-07 S04 “期末应收到截止日仍未收回” 暂缓
- 分录表加 reverses_entry_no 列：作废对仍由规则按数据集声明的冲回说明格式识别

## 7. 准则片段

段落号已对照 `参考资料/` 中的现行编译版核对。原文摘录由 A 从 `参考资料/` 的 PDF 填入 `knowledge/standards/`。

片段 ID 写作 `{YAML 文件名}-{段码}`（如 ASA450-P05，编号段补足两位），段码规则见开发接口文档第 8.2 节；`tests/test_knowledge.py` 按该规则核对全部片段的 ID。

| 片段 ID | 内容 | 程序 |
|---|---|---|
| AASB15-P31 | 履约义务满足时确认收入 | REV-03、REV-02 |
| AASB15-P35 | 随时间转移控制权的三项条件（任一成立即在一段时间内确认收入） | REV-03、REV-02 |
| AASB15-P38 | 时点履约义务的控制权转移迹象 | REV-03、REV-02 |
| ASA320-P10、ASA320-P11 | 整体重要性、执行重要性 | PLN-01 |
| ASA450-P05 | 明显微小的错报不累计 | PLN-01、FIN-04、CSH-02 |
| ASA450-P11 | 评价未更正错报是否重大 | FIN-01 |
| ASA505-P07 | 审计师控制询证函 | CSH-04 |
| ASA505-P14 | 调查回函差异 | CSH-02 |
| ASA705-P07、ASA705-P08 | 保留意见、否定意见的适用条件 | FIN-01 |
| ASA700-P23、ASA700-P28 | 意见段、形成意见的基础段 | RPT-03 |
| ASA705-P16、ASA705-P20、ASA705-P21 | 非无保留意见的标题、基础段描述、错报金额影响 | RPT-03 |

报告模板以 ASA 700 [Aus] Illustration 1A（无保留意见）和 ASA 705 [Aus] Illustration 1A（因重大错报发表保留意见）为底本，去掉关键审计事项与其他信息两段。

## 8. 验收

1. 使用 MockLLMClient 离线跑完第 1 节全流程：四道闸门都中断并按决定恢复，结果与第 3 节预期一致。
2. 接真实 LLM 跑通同一流程；校验失败时走 failed 或 unattributed 退路。
3. P2 进行中重启后端，流程从检查点恢复，已完成的程序不重跑，不重复发函。
4. 每条 Judgment 的证据指针都能在前端打开到原文对应页。
5. 签字后项目只读；audit_trail 能查到每次 LLM 调用和每个人的决定。
6. 行级安全生效：以应用角色连接时，项目数据表不设 engagement_id 的查询取不到数据。

## 9. 里程碑

| 里程碑 | 内容 |
|---|---|
| M1 接口对齐 | contracts 包、数据库表、API 路由定义完成并合入，三人以此并行开发 |
| M2 骨架跑通 | 17 条程序先用固定返回值占位，全流程在 mock 下走通四道闸门，前端能操作每一步 |
| M3 程序实装 | 规则程序接真实 SQL，LLM 程序接 prompt 与校验；合成数据集完成，第 3 节场景全部命中 |
| M4 演示 | 接真实 LLM 联调，逐项验收第 8 节，定演示脚本 |
