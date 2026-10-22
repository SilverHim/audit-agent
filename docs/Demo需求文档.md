# Demo 需求文档

本版是可运行的原型：以 Xero Demo Company (AU) 为演示公司，在演示配置下走通 P0–P4 全流程（承接、计划、执行、完成、报告），最后出具审计报告；Numbat（合成数据）作离线回归。程序库有 29 条程序，演示配置启用其中 24 条（第 2 节）。规格书的其余设计与未启用的功能本版不实现或不验证，汇总见第 6 节。

“以 AI 替代审计员、一人完成审计” 是项目的目标，本版只到原型：每道闸门、需要人的判断与人工程序都由审计师决定或执行，系统只给建议与材料。

## 1. 演示流程

演示公司 Xero Demo Company (AU)，数据集 `xero/20261017-q1`，期间 2026-07-01 至 09-30（第 3 节）。演示配置为 `.env.example` 的 FEATURES=critic,collab,journals,misstatement,planning（开发接口文档第 11 节）：基线的 A7 独立复核（critic），加智能体协作（collab，需求 R155）、分录测试 GEN-05 与 GEN-02（journals，需求 R146）、错报闭环 FIN-03、FIN-08、FIN-09（misstatement，需求 R195，Q34）与总体审计计划（planning，需求 R317）。共 5 道闸门：ACC-01、PLN-01/03、PLN-15、FIN-01、RPT-01。

```
P0 承接   新建项目：客户下拉第一行是演示公司，期间随它的默认数据集带出
          → ACC-04 查登记 mock（ABN 有效、名称一致、公司已注册）→【闸门】ACC-01 承接
          停在 ACC-01 时导入承接资料（演示数据导入的 acceptance 阶段）
P1 计划   等数据：导入外勤的账表与文件（fieldwork 阶段），审计师确认数据已齐
          → DOC-04（合同在上传时已抽取）→ PLN-07 初步分析 → PLN-01 重要性候选
          →【闸门】PLN-01/03：选收入 1%、执行重要性 50%、明显微小 5%（整体重要性 324.31）
          → 装配程序包 → PLN-15 起草总体审计计划
          →【闸门】PLN-15：先退回（要求写明银行函证的时间），按理由起草第 2 修订后批准，进入执行
P2 执行   收入：REV-08、REV-03 → REV-02；函证：CSH-04 → DOC-06，CSH-05 → CSH-02；分录：GEN-05 → GEN-02
          A7 按触发规则对模型类判断做独立复核（GEN-09）；需要人的结果进复核队列，审计师逐条决定：
          CSH-02 的 BA-091 认定错报（回函载明的应计利息 48.53 未入账），GEN-02 的 XJ-499 认定错报
          （无依据的办公设备折旧分录，710 多记 750.00），XJ-575 推翻为接受（调阅报销单 E003，系录入时
          姓名写错），其余确认
P3 完成   错报闭环第 1 轮：FIN-03 对 XJ-499 与 BA-091 两条错报分类，审计师逐条确认
          → FIN-08 人工任务：登记与管理层的书面沟通，管理层同意更正两笔 → 等客户更正
          → 更正通知：客户已过账 AJE-X01、AJE-X05，载入这两笔调整分录，GEN-05 增量扫描（无例外）
          → FIN-09 逐条验证更正，审计师确认，两条错报都已更正
          → FIN-04 累计未更正错报：合计 0，候选无保留意见 →【闸门】FIN-01 批准无保留意见并写理由
P4 报告   RPT-03 生成无保留意见的报告初稿 →【闸门】RPT-01 附批注退回（形成意见的基础段写明财务报告编制基础）
          → RPT-03 修订 →【闸门】RPT-01 批准签字 → 归档，项目只读
```

走通的每个停点、决定与终态（例外、判断、复核任务、错报各版本、汇总表、报告版本、文件与留痕的计数）由 `tests/test_demo_walkthrough.py` 断言（需求 R287、R260、R195），离线按工作语言 en 与 zh 各走一次（需求 R343）；连库、重启与真实模型的变体见第 8 节。

Xero 演示数据集的种子载入（`python -m backend.persistence.seed load`）只载快照；演示时还要做演示数据导入（需求 R269，数据管理页的按钮见 R270）：停在 ACC-01 闸门时导入承接资料，停在等数据时导入外勤的账表与文件，再由审计师确认数据已齐。财务报表以账套为准（Issue #46），演示资料里没有报表这一批。分阶段导入的演示用不经种子载入的新项目（数据版本 0）；种子载入过的项目视同已确认就绪，图不停在等数据，做外勤导入时快照七张表（含应付账单，需求 R178）会再写一版。

Numbat 回归只开基线功能（critic），走 ACC-01、PLN-01/03、FIN-01、RPT-01 四道闸门（第 3、8 节）。

## 2. 程序清单

程序库共 29 条（`knowledge/procedures/*.yaml`）。下表与 ProcedureLibrary 加载的程序编号集合相同，并逐行核对引擎、自主度、闸门、负责 Agent、依赖与功能（`tests/test_docs.py`）。功能一列为空的 17 条是基线程序，GEN-09 属功能 critic（基线，缺省开）；演示配置另开 journals（GEN-05、GEN-02）、misstatement（FIN-03、FIN-08、FIN-09）与 planning（PLN-15），共 24 条进演示；collab 不加程序，只改 A7 的触发、争议与仲裁等机制。其余 5 条属 acceptance（ACC-08）、related_parties（GEN-08、GEN-03）、going_concern（GEN-07、GEN-06），本版不开，也不在验证范围（第 6 节）。注了功能名的程序只在该功能启用时进程序包（开发接口文档第 8.1 节功能开关）；P2 的 GEN-05、GEN-02、GEN-08、GEN-03、GEN-07、GEN-06 是固定集，不按审计目标装配。

| 阶段 | 程序 | 引擎 · 自主度 | Agent | 依赖 | 功能 | 本版做法 |
|---|---|---|---|---|---|---|
| P0 | ACC-04 ASIC / ABN 核查 | 规则 · L3 | A2 | — | — | 查登记：数据集项目读补造的登记 mock，新客户经 ABN Lookup 实时查（需求 R289）；ABN 有效、名称一致、公司状态正常，不符出 Exception |
| P0 | ACC-01 是否承接 | 人工 · L1 · 闸门 ACC-01 | A2 | ACC-04 | — | 呈递 ACC-04 结果与客户画像；承接进 P1，拒绝进 Declined，信息不足留在闸门；审计师更新客户资料后自动重跑 ACC-04 并重新呈报 |
| P0 | ACC-08 约定条款与委任核验 | 混合 · L1 · 闸门 ACC-08 | A2 | ACC-01 | acceptance | 只有契约与程序模板（需求 R198）；核对规则、A2 执行、节点与闸门未做（R199–R202 暂停）。设计做法：读客户签回的约定书与委任文件，抽出事实，规则逐项核对 ASA 210 第 6、10 段与公司法 s293、s325(1)、s328A、s323D(2A)（T01–T07），再给建议判断，在 ACC-08 闸门呈报 |
| P1 | DOC-04 合同要素抽取 | LLM · L2 | A3 | — | — | 读合同 PDF 文本层，抽出合同对象与条款，每个字段带页码指针；合同在上传时由 A3 抽取，执行时已抽取的不再调模型；结果直接入库，可在合同页改（带版本），不建复核任务 |
| P1 | PLN-07 初步分析程序 | 规则 · L3 | A4 | — | — | 资产负债类本期末对期初（上期损益结转进留存收益）、损益类在两期等长时对上期逐科目比较，变动同时达到本期收入的 0.5% 与基数的 20% 出 Exception；算毛利率、收入增长率；按月汇总本期的收入、费用与员工费用（月度趋势，闸门页显示，不出 Exception） |
| P1 | PLN-01 重要性水平 | 人工 · L1 · 闸门 PLN-01/03 | A4 | — | — | 规则按收入、总资产、税前利润三个基准算候选区间（基准金额不为正的不列为候选，需求 R351）；审计师选定基准与比例（执行重要性 50%–75%，决定 10），系统算出执行重要性与明显微小门槛 |
| P1 | PLN-03 风险清单终审 | 人工 · L1 · 闸门 PLN-01/03 | A4 | — | — | 与 PLN-01 同一道闸门；审计目标与风险清单由数据集预置（没有预置的取通用模板），审计师可改风险等级 |
| P1 | PLN-15 总体审计计划 | LLM · L1 · 闸门 PLN-15 | A4 | PLN-01、PLN-03 | planning | 装配之后起草总体审计策略与审计计划（ASA 300），停在 PLN-15 闸门；退回时按理由起草下一修订，批准后计划置 approved、阶段进 P2，之后才派发（需求 R203–R206，R317 打开）；批准时记录是否需要项目质量复核与是否需要审计师的专家 |
| P2 | REV-08 凭证三单匹配 | 规则 · L3 | A6 | — | — | 期末前后 window_days 内的销售发票与发运或服务完成记录、收款匹配；缺单、跨期、金额不符出 Exception |
| P2 | REV-03 收入合同条款评价 | LLM · L2 | A6 | DOC-04 | — | 读 DOC-04 的合同对象，按合同约定的转移事件判断商品或服务的控制权转移时点与异常条款，每份合同一条 Judgment |
| P2 | REV-02 收入截止性判断 | 混合 · L2 | A6 | REV-03、REV-08 | — | 规则取 REV-08 的全部例外所指的发票与期末窗口内超过执行重要性的发票；LLM 结合发运、签收或服务完成日与 REV-03 结论判断归属期间（上下文另带该发票的过账分录行，错报金额取不含 GST 的收入行），每笔一条 Judgment |
| P2 | CSH-04 函证发起 | 规则 · L4 | A5 | — | — | 按银行账户生成询证函，经 mock 函证平台发出，平台立即返回预置回函 PDF |
| P2 | DOC-06 函证回函抽取 | LLM · L3 | A3 | CSH-04 | — | 读回函 PDF，抽出银行、账号、余额、日期、回函渠道与截至余额日尚未入账的应计利息（回函没提为空），写 Confirmation |
| P2 | CSH-05 流水账面核对 | 规则 · L4 | A6 | — | — | 银行对账单期末余额与总账银行科目余额比较，不符出 Exception |
| P2 | CSH-02 函证差异评价 | 混合 · L3 | A6 | CSH-05、DOC-06 | — | 规则算回函余额与账面余额的差额，列出期后流水中的候选调节项，并核对回函写明的应计利息是否已入账（D04）；LLM 解释差异并建议是否为错报；相符且应计利息已入账才自动关闭，有差异或应计利息未入账进复核队列 |
| P2 | GEN-05 全量分录扫描 | 规则 · L3 | A6 | — | journals | 对本期全部分录执行红旗规则，每笔凭证一条 Exception；看录入时间的规则在数据集声明录入时间不可靠时不执行，缺字段的规则记不可执行（第 4.2 节）；另有不看时间的 R09–R12（决定 13）；更正分录接入后的增量实例在 verify_correction 节点执行（功能 misstatement，需求 R194） |
| P2 | GEN-02 分录例外归因 | 混合 · L2 | A6 | GEN-05 | journals | 规则取 GEN-05 尚未归因的例外，按凭证分组；LLM 结合该凭证的全部行、例外所指的凭证与发票、银行流水与关联文件解释每条例外，每笔凭证一个 Judgment，建议动作不受限制（可建议认定错报） |
| P2 | GEN-07 期后监控 | 规则 · L4 | A5 | — | going_concern | 在期后窗口（期末后 31 天，截到数据截止日）内扫描总账未记的大额流水（S01）、非常规分录（S02）与资本性支出（S03），剔除作废对，例外原样作期后事项候选，不作判断，不建复核任务（需求 R177）；S04 本版不做 |
| P2 | GEN-06 期后事项研判 | 混合 · L3 | A6 | GEN-07 | going_concern | 对 GEN-07 尚未归因的每条例外所指的行，按参考号与对方名称在本期至期后窗口的总账中找相关凭证，带出相关发票、服务完成记录、已登记的文件与涉及科目的期末余额；LLM 判断是调整事项、非调整事项还是常规期后交易，每行一个 Judgment（需求 R179）；Xero 固定输出未做（R180 暂停） |
| P2 | GEN-08 股权穿透与关联方关系图 | 规则 · L4 | A5 | — | related_parties | 按客户画像的股东与董事查 ASIC 公司摘录与人员检索，建股权与任职关系图，与管理层申报的关联方比对，未申报的关联主体出 Exception（需求 R136–R138） |
| P2 | GEN-03 关联方图谱研判 | LLM · L2 | A6 | DOC-04、GEN-08 | related_parties | 对关系图中的关联主体，结合本期交易与相关文件判断关联关系与交易性质，每个主体一条 Judgment（需求 R139、R140）；完整性终审 GEN-01 未做（R141 暂停） |
| 贯穿 | GEN-09 判断独立复核 | LLM · L4 | A7 | — | critic | 由 A6 每生成一条模型类判断即同步调用，不单独派发：程序运行时自主度 L3、L4 且建议接受或关闭、推理链含假设、或抽样命中的判断（collab 另加引用内部副本与有步骤不受支持两条）由复核模型两轮复核：第一轮只看证据独立得出结论与推理链，第二轮再比对 A6 结论；一致计入置信度，不一致建争议任务由审计师仲裁（采用 A6、采用 A7 或人给结论，可附错报）；L1、L2 跳过 |
| P3 | FIN-03 错报性质分类 | 混合 · L2 | A8 | — | misstatement | 对每条未分类的已认定错报：审计目标与期间来源在代码中定，LLM 判类型（factual 或 judgmental）与是否有舞弊迹象，每条错报一个 Judgment，确认后写进错报的分类（需求 R157–R159）；舞弊迹象只呈报，不阻断 |
| P3 | FIN-08 错报沟通与回复记录 | 人工 · L1 | A8 | FIN-03 | misstatement | 在 communicate 节点派一条人工任务：材料逐条列待沟通的错报与更正请求；审计师在人工队列页登记与管理层的沟通与回复，同意的记已沟通、等更正，拒绝的记未更正（需求 R161） |
| P3 | FIN-09 更正验证 | 混合 · L2 | A8 | FIN-08、GEN-05 | misstatement | 对每条待验证的错报：规则取审计更正分录的行，按来源对象与科目匹配，算已更正额与剩余额；LLM 判断是否完整更正及是否影响其他科目，每条错报一个 Judgment（需求 R164、R165） |
| P3 | FIN-04 错报累计 | 规则 · L3 | A9 | — | — | 累计高于明显微小门槛的未更正错报，出汇总表；与整体重要性比较得候选意见（功能 misstatement 打开时按错报状态累计，需求 R166） |
| P3 | FIN-01 未更正错报评价 | 人工 · L1 · 闸门 FIN-01 | A9 | FIN-04 | — | 呈递汇总表与候选意见；审计师确认意见类型（可选范围见 allowed_opinions，需求 R184）并写理由 |
| P4 | RPT-03 报告文本生成 | LLM · L2 | A10 | FIN-01 | — | 固定段落套模板（按项目的工作语言，需求 R341），LLM 只写意见段与形成意见的基础段，事实断言回指已确认的 Judgment；按批注修订，每轮版本 +1 |
| P4 | RPT-01 审计报告批准与签字 | 人工 · L1 · 闸门 RPT-01 | A10 | RPT-03 | — | 附批注退回则回 RPT-03 修订；签字后报告锁定，项目进 Archived，拒绝一切写入 |

A10 的两项不单独派发，不建程序文件，不计入条数：

| 编号 | 名称 | Agent | 本版做法 |
|---|---|---|---|
| GEN-10 | 进度与来源解释 | A10 | 对话入口（开发接口文档第 6.1 节）：审计师提问时，模型按需调只读工具查进度、判断与证据来源后作答；只读，不提交决定；只作对话留痕的程序编号 |
| GEN-11 | 判断呈递与决定收集 | A10 | 复核任务与闸门以结构化卡片呈递（推理链、证据指针、反方论证、独立复核结论），审计师在卡片的表单上提交决定，经 DecisionSubmit 写入；规格书为 LLM · L4，本版不调模型 |

**依赖**。YAML 的 `depends_on` 只写库内的程序，规格书 6.1.2 中的其余前置不建模；依赖指向未启用功能的程序时，启用视图里去掉（开发接口文档第 8.1 节）。FIN-04 在 P2 程序包全部结清后执行。

```
ACC-01 ← ACC-04            ACC-08 ← ACC-01            PLN-15 ← PLN-01、PLN-03
REV-03 ← DOC-04            REV-02 ← REV-03、REV-08     DOC-06 ← CSH-04
CSH-02 ← CSH-05、DOC-06     GEN-02 ← GEN-05            GEN-06 ← GEN-07
GEN-03 ← DOC-04、GEN-08     FIN-08 ← FIN-03            FIN-09 ← FIN-08、GEN-05
FIN-01 ← FIN-04            RPT-03 ← FIN-01            RPT-01 ← RPT-03
```

P2 程序包分三层：第 0 层 REV-08、REV-03、CSH-04、CSH-05；第 1 层 REV-02、DOC-06；第 2 层 CSH-02。演示配置（journals）另加第 0 层 GEN-05、第 1 层 GEN-02；未开的功能另加第 0 层 GEN-07、GEN-08，第 1 层 GEN-03、GEN-06。GEN-05 的增量实例不在程序包分层内，在 verify_correction 节点执行。

## 3. 数据集

| 数据集 | 用途 | 位置 |
|---|---|---|
| Xero Demo Company (AU) 快照 `xero/20261017-q1` 加补造目录 | 演示公司（第 1 节），注入测试与真实模型评测 | `data/external/xero/api/20261017-q1/`，`data/xero_demo/20261017-q1/` |
| Numbat（合成） | 离线回归（新功能全关） | `data/demo/` |
| CUAD v1 节选 | 外部测试数据：合同抽取与条款评价的评测 | `tests/fixtures/cuad/`；原件取到 `data/external/cuad/`，不入库 |

**Xero Demo Company (AU)**。设计与各程序的预期结果见 `docs/Xero演示数据集设计.md`。

- 来源：Xero 预置的虚构公司（简化披露，第 2 层，适用 AASB 1060；决定 3），不是真实客户数据。2026-10-17 用免费开发者登录经 API 拉取：期间 2026-07-01 至 09-30（公司法 s323D(2A) 缩短的过渡财年，决定 4），上期截至 2026-06-30，对账单到 10-17。Xero 没有给出再分发的开发者条款原文（Xero 演示数据集设计 “待核” T1），快照按用户的决定随仓库带上。
- 原始数据一律不改（决定 6）：快照原样只读；要验证错报识别，只在测试临时副本里注入错误，原目录逐文件哈希守护（`tests/xero_injection.py`，第 8 节）。登录账号联系人的姓名与邮箱不写进任何设计、补造文件与测试数据（决定 12）。
- 补造目录：Xero 没有的单据按实际客户与发票补造（合同、银行回函、服务完成记录、登记 mock 与摘录、承接文件、证据文件、工资册、更正分录、管理层声明书等），合成文件一律标 “Synthetic – for demonstration only”；`manifest.json` 记每个文件的 sha256、synthetic 标志与对应的 Xero 记录，另记快照各文件的哈希；`dataset_traits.json` 声明跟数据来源有关的事实（录入时间不可靠、作废冲回的说明格式、数据截止日、上期起始日等）。由 `data/generate_xero_demo.py` 生成，`data/check_xero_demo.py` 校验。
- 载入：`XeroDatasetLoader` 读快照与补造目录，核对哈希；演示时的导入分两个阶段（第 1 节）。

**Numbat**。一家虚构的非上市公司，一个财年，截至 2026-06-30，全部放在 `data/demo/`，没有数据集声明（取缺省）。作离线回归：只开基线功能，R55、R56、R57、R59 的断言不变（第 8 节）；场景与预期见 `docs/合成数据集设计.md`。

| 数据 | 内容 |
|---|---|
| 客户与登记 | 客户画像；ABN Lookup 与 ASIC 查询的 mock 返回 |
| 财务数据 | 科目表、本期与上期试算表、总账分录、销售发票、发运单、银行流水（含期后 10 天） |
| 文件 | 销售合同 PDF 3 份、银行询证函回函 PDF 2 份，均带文本层 |
| 计划 | 审计目标与风险清单：收入的发生与截止为特别风险，货币资金的存在为高风险 |

| 场景 | 数据 | 预期 |
|---|---|---|
| 截止错报 | 一张 2026-06-28 入账的发票 AUD 142,000，发运单 2026-07-03 签收；合同约定签收时控制权转移 | REV-08 出跨期例外；REV-02 建议认定错报；审计师确认为错报 |
| 正常截止 | 一张 2026-06-29 的发票 AUD 95,000（高于执行重要性上限，会被 REV-02 选中），发运单 2026-06-30 签收 | REV-02 判断归属期间正确；审计师确认 |
| 函证差异 | 回函余额比账面少 AUD 50,000，差额为 2026-06-30 入账、2026-07-02 到账的在途存款 | CSH-02 解释为时间性差异，建议不认定错报；审计师确认 |
| 意见 | 收入约 AUD 1,200 万，按收入基准的整体重要性候选区间为 60,000–120,000 | 截止错报高于整体重要性，候选意见为保留意见 |

**CUAD**。Contract Understanding Atticus Dataset (CUAD) v1, The Atticus Project, CC BY 4.0（Zenodo 记录 4595826）。`tests/fixtures/cuad/` 只放节选：3 份合同的逐页文本、10 份合同的人工金标准与离线 mock 输出，署名、许可与改动说明见同目录 README；原件由 `uv run python -m tests.tools.cuad_fetch` 取到 `data/external/cuad/`，不入库。用于 DOC-04 抽取与 REV-03 条款评价的评测（需求 R245–R249），不进演示。

## 4. 骨架

### 4.1 编排

LangGraph `StateGraph`，检查点用 `AsyncPostgresSaver`（测试用 `InMemorySaver`），thread_id 即 engagement_id。拓扑以 `orchestration/graph.py` 的模块说明为准：

```
p0_inputs → { p0_inputs | p0_intel } → gate_acc01 → { p1_await_data | declined | p0_inputs }
p1_await_data → p1_ingest → { p1_analytics | fan_in }
p1_analytics → gate_p1_risk → { assemble_pack | p1_analytics }
assemble_pack → plan_draft → gate_p1_plan → { schedule | plan_draft }      （功能 planning；关闭时 assemble_pack → schedule）
schedule ⇉ fieldwork × N ⇉ fan_in → { p1_ingest | schedule | await_events | await_data | completion }
await_events、await_data → fan_in
completion → gate_fin → { report_draft | completion }
report_draft → gate_rpt01 → { archive | report_draft }
```

功能 misstatement 打开时（需求 R160；演示配置打开，需求 R195，Q34；Numbat 回归不开），P2 结清后先走完成阶段的错报闭环，FIN-01 驳回回 summarize：

```
fan_in → misstatement_triage → { misstatement_triage | communicate }
communicate → { await_correction | verify_correction }，await_correction → verify_correction
verify_correction → { misstatement_triage | communicate | await_correction | p3_reassess }
p3_reassess → summarize → final_review → { misstatement_triage | completion }
completion → gate_fin → { report_draft | summarize }
```

- 错报闭环的各节点依次执行 FIN-03（分类）、FIN-08（沟通）、FIN-09（更正验证）、FIN-04（累计，每轮一次）；PLN-19（重评）、FIN-15（最终复核）与 FIN-02（持续经营）不在程序库（需求暂停，第 6 节），所在节点为空操作；await_correction 在节点内等审计师提交更正通知（需求 R162）。阶段 P3_Completion 在 misstatement_triage 写入。完成阶段新认定的错报都回 misstatement_triage 先分类再沟通。
- p0_inputs：新客户（项目没有数据集）先经 ABN Lookup 查是不是公司，是公司而没有上传 ASIC 摘录时把 ACC-04 置等资料（awaiting_data，A3 提出补交请求 REQ-registry_extract），在节点内 `interrupt()` 等审计师上传；上传后 A3 抽取，数据变动带动图回到 p0_inputs 再判一次，有摘录或请求标为客户无此类资料后进 p0_intel（需求 R290）。有数据集的项目、不是公司或查不到 ABN 的不等，直接进 p0_intel。
- p1_await_data：建项目不载入财务数据，ACC-01 通过后在节点内 `interrupt()` 等审计师在数据管理页接入数据并确认已齐（数据集载入视同确认，不等）。
- 静态中断：闸门节点按功能集取登记表（`orchestration/gates.GATES`）：基线 gate_acc01、gate_p1_risk、gate_fin、gate_rpt01，功能 planning 另加 gate_p1_plan；演示配置共 5 道。闸门决定写入时按 GateDecision 路由（approve / reject / insufficient_info）。
- 闸门的非通过出口：ACC-01 拒绝进 Declined 结束，客户画像更新后回 p0_inputs 重跑 ACC-04；PLN-01/03 驳回回 p1_analytics；PLN-15 驳回回 plan_draft 起草下一修订；FIN-01 驳回回 completion（功能 misstatement 打开时回 summarize）；RPT-01 驳回回 report_draft。各道闸门的信息不足都留在原闸门重新呈报。
- assemble_pack 按审计目标的风险等级匹配程序 trigger，加固定集，补齐依赖；每条审计目标至少被一条程序覆盖，否则阻断。功能 planning 打开时 assemble_pack 只写 P2 程序，不改阶段；plan_draft 由 A4 执行 PLN-15 起草计划，停在 gate_p1_plan；批准时计划置 approved、PLN-15 完成、阶段进 P2_Executing（同一事务），之后才派发。
- schedule 按依赖图拓扑分层，同层按循环分组，用 `Send` 派发 fieldwork 实例，上限 4；fieldwork 按程序的 owner 调用对应 Agent。
- fan_in 用按 ID 合并的 reducer 收各实例返回的 ProcedureRun、Exception、Judgment、ReviewTask，由 A1 重算程序状态：有待决任务为 awaiting_human（功能 collab 打开时有待决争议任务为 in_dispute），否则为 completed；rerun 决定时由 DecisionSubmit 先置 executing，不受待决任务影响。
- 程序模板声明的客户资料还没有时，程序置 awaiting_data，不执行；客户无此类资料或前置程序未执行时置 no_data（终态，没有执行记录）。资料到了，P2 以外的程序（DOC-04）经 p1_ingest 补跑（需求 R272、R280）。
- P2 以外的程序（ACC-04、DOC-04、PLN-07、PLN-01、PLN-15、FIN-04、RPT-03）由所在节点直接执行。整次失败（Agent 抛异常或片段校验不过）时，节点写入 failed 的执行记录与 failed 任务；单个对象失败（如 DOC-04 一份合同抽取失败）时，执行记录仍为 succeeded，片段照常写入，其中带该对象的 failed 任务。两种情况程序都置 awaiting_human，然后在节点内 `interrupt()`；审计师选 rerun 后在同一节点内重跑。
- after_fan_in：先执行后等。P2 以外等到了数据的程序回 p1_ingest；有可派发的程序（planned 或 executing，前置都已结清）回 schedule，不因别的程序等复核或等数据而停下；没有可派发的而有待决复核任务进 await_events，节点内 `interrupt()`，复核决定写入后恢复；P2 程序全部结清（completed 或 no_data）且没有程序在等数据进 completion；有程序在等数据进 await_data。程序状态与任务对不上时抛 ScheduleStuck，不空转。
- 项目阶段：P0_Acceptance、P1_Planning、P2_Executing、P3_Completion、P4_Reporting、Archived、Declined。程序状态：planned、executing、awaiting_human、in_dispute、completed、awaiting_data、no_data。
- EngagementState 的字段、reducer 与写入归属见 [开发接口文档.md](开发接口文档.md) 第 3 节。

### 4.2 判断与复核

- 规则程序出 Exception，不建复核任务：REV-08、CSH-05、GEN-05 的 Exception 由 REV-02、CSH-02、GEN-02 引用归因，ACC-04、PLN-07 的 Exception 作闸门材料。
- 规则可声明执行前检查（开发接口文档第 8.1 节 `checks`）：检查命中的规则不执行，执行记录里记 skipped 或 not_executable 与理由，不出 Exception，也不建复核任务，在程序详情页列出（需求 R130；规格书第 2.3 节写的 “进复核队列” 本版不做）。如 GEN-05 在数据集声明录入时间不可靠时跳过看录入时间的规则，没有审批人字段时 R04 记 not_executable；GEN-05 因此不建复核任务。
- 分录例外归因（GEN-02）的建议动作不受限制：同一笔可能被分录归因与科目程序各建议一次认定错报，由审计师避免重复（第 6 节 T12 一条）。
- LLM 与混合程序出 Judgment：结论、推理链、证据指针、反方论证、建议动作、置信度。推理链中 fact 步骤须带证据指针，standard 步骤指向准则片段，inference 步骤引用前序步骤。
- OutputValidator 做四级校验：语法、结构、引用、语义。任一级不通过时把错误回传 LLM，最多重试 2 次（需求 R348；只把步骤标为 unsupported 的不算不通过）；重试耗尽后，混合程序的对象有规则例外时建 unattributed 任务，否则建 failed 任务；LLM 程序按对象建 failed 任务，不影响同一 Run 的其他对象。
- 置信度由证据覆盖率、规则一致性、LLM 自报三项加权（A7 复核一致时另计一项），权重放在 rules.json。
- 以下情况需要人：自主度为 L1 或 L2；建议动作为 misstatement 或 more_evidence；CSH-02 有差异或回函应计利息未入账；A7 判为不一致（争议任务）或命中触发规则而复核没做成；校验失败或程序执行失败。其余自动关闭。
- 复核决定六种，各类任务允许的决定由 ReviewQueue.allowed 给出：

| 决定 | 适用任务 | 效果 |
|---|---|---|
| confirm | normal | Judgment 置 confirmed |
| override | normal、unattributed | 按人的结论另建人工 Judgment；normal 任务的旧 Judgment 置 overridden 并记 superseded_by |
| misstatement | normal、unattributed | 按 misstatement_draft 建 Misstatement；初始状态按功能 misstatement：打开为 identified（之后分类、沟通、等待更正与验证，接口文档第 2.5 节），关闭为未更正（uncorrected） |
| rerun | failed、unattributed | 程序回到 executing；P2 程序重新派发，P2 以外的程序在所在节点内重跑；一轮失败算一组，整组重跑（Q17） |
| manual_result | manual；failed（只限模板允许且任务带 subject_id） | 人工程序提交结果；或人的结果成为 Judgment |
| dispute_resolution | dispute | 仲裁 A7 与 A6 的分歧：采用 A6、采用 A7 或人给结论，可附错报 |

- DecisionSubmit 是唯一写入口：校验目标版本和任务类型允许的决定，写入后重算程序状态并恢复流程。前端所有决定都走这里。
- 每次 LLM 调用、每个人的决定、每次状态迁移各写一行 audit_trail。

### 4.3 数据库

PostgreSQL。除 users、engagements、client_profiles、llm_settings 与检查点表外，所有表带 engagement_id 并启用行级安全策略，users、engagements、client_profiles 由应用层按 owner_id 校验归属，llm_settings 是系统级设置；金额以分为单位存 BIGINT。全部表与逐列定义见开发接口文档第 9 节（`tests/test_docs_contracts.py` 核对表名都写到）。

| 类别 | 表 |
|---|---|
| 项目、用户与设置 | users、engagements（含版本快照）、client_profiles、llm_settings、xero_connections |
| 财务数据（关系表） | accounts、trial_balance、journal_entries、sales_invoices、supplier_bills、delivery_notes、bank_accounts、bank_transactions、employees、documents、contracts、contract_terms |
| 数据管理 | data_imports、import_batches、data_changes、data_checks、data_requests、dataset_loads、extraction_versions、account_category_history |
| 审计产出（JSONB，按 id + version 追加） | audit_objectives、risk_register、procedures（含 ProcedureRun）、exceptions、judgments、review_tasks、decisions（复核决定、闸门决定、重跑请求、更正通知）、confirmations、misstatements、report_versions、gate_materials（闸门材料）、ownership_graphs、completion_records |
| 留痕 | audit_trail，只追加 |
| 检查点 | LangGraph 自建的表 |

financial_statements、financial_statement_lines 随 Issue #46 不再使用（表与迁移保留）。PDF 原件存 `engagements/{id}/evidence/`，路径与哈希登记在 documents。

### 4.4 API

端点、请求与响应、错误码见开发接口文档第 10 节。所有端点校验登录与项目归属；流程在后台任务中推进，前端轮询项目总览。

### 4.5 前端

| 页面 | 内容 |
|---|---|
| 登录、注册、项目列表 | 新建项目：选客户（`GET /clients`，每个客户一行，演示公司在第一行）与期间、工作语言；数据集客户的期间随默认数据集带出；下拉末项 “新建客户” 登记没有数据集的客户（需求 R288） |
| 项目总览与审计助理 | P0–P4 进度、当前闸门、程序状态与进度、复核待办数；审计助理对话（GEN-10，只读） |
| 客户 | 客户资料与画像 |
| 数据管理 | 数据概览与体检、账表导入与查看、文件上传与补登记、补交请求、演示数据导入、确认数据已齐 |
| 闸门 | 各道闸门一个表单：展示材料，选 approve / reject / insufficient_info，填理由和逐项修改（PLN-01/03 选重要性与改风险等级，RPT-01 写批注）；ACC-01 另可更新客户资料，保存后自动重跑 ACC-04 |
| 审计计划 | PLN-15 的计划各修订与审批 |
| 程序 | 程序列表与详情（执行记录、规则执行情况、被测总体与对象） |
| 复核队列、人工队列 | 任务列表；复核详情页并排显示结论、推理链、证据、反方论证、独立复核与置信度，只显示 ReviewQueue.allowed 给出的决定；人工任务按材料提交结果 |
| 证据查看、合同 | 点击证据指针打开 PDF 并跳到对应页；分录、发票、发运单明细；DOC-04 抽出的合同与条款，条款链接到原文页 |
| 错报与意见、报告 | 错报列表、汇总表、候选意见；报告当前版本预览、版本历史与批注、签字入口 |
| 待决定、审计留痕 | 待决定的复核任务与例外一览（可稍后处理）；审计留痕按时间列出 LLM 调用、人的决定、状态迁移 |
| 设置 | 主模型与复核模型的配置 |

## 5. 代码清单与分工

用户一人负责全部代码，没有分工（开发接口文档第 1 节）；下表的模块划分沿用原来的 A（智能体与编排）、B（数据库与数据）、C（前端与 API），只说明代码属于哪一块。

```
src/backend/
├── main.py                        C  组装根 build_deps：构造 Store、适配器、检查点，挂路由
├── core/                          C  配置（开发接口文档第 11 节）、JWT
├── api/routes/                    C  各端点（开发接口文档第 10 节）
├── contracts/                     A  领域类型：models、records、data、knowledge、services、state、views、llm_outputs、
│                                     manual_results、conversation、errors、ports；定义见开发接口文档
├── knowledge/
│   ├── procedures/*.yaml          A  29 条程序（第 2 节）；规则程序的 SQL 写在 YAML（B）
│   ├── standards/*.yaml           A  准则与公司法片段（第 7 节）
│   ├── prompts/                   A  LLM 程序的提示词模板
│   ├── planning/                  A  通用审计目标与风险清单模板
│   ├── templates/                 A  报告固定段落（中英两种工作语言）
│   ├── workpapers.yaml            A  底稿索引号与所属区（需求 R329）
│   └── rules.json                 A  置信度权重
├── services/                      A  程序库、判断引擎与 OutputValidator、复核队列、功能开关、意见范围、呈递、过期判定、
│                                     自主度降级；B  规则引擎、留痕、数据服务（导入、体检、演示数据导入）、被测总体
├── agents/                        A  base、registry、judging（判断公共流程）；A2 client_intel；A3 ingest、sheets、checkup；
│                                     A4 risk_assessor、audit_plan；A5 evidence、ownership；A6 fieldwork、journals、
│                                     going_concern；A7 review；A8 workpaper；A9 qc；A10 concierge（按程序分派、RPT-03 与人工
│                                     程序）、concierge_chat（审计助理对话）
├── orchestration/                 A  graph、nodes、gates（闸门登记表）、scheduler、decisions（DecisionSubmit）、runner、
│                                     recovery、query、merge、waits、trail、inputs、checkpoint、studio
├── persistence/                   B  表模型、各 Store 与 DatasetLoader（Numbat 的 CSV、Xero 快照与补造目录）、数据管理、
│                                     行级安全、事务、种子脚本
├── adapters/                      A  llm（mock 与 live）、chat_llm；B  registry 与 registry_mock、abn_lookup、
│                                     confirmation_mock、xero（API 拉取）、xero_mock、xero_dataset、xero_supplements
└── migrations/                    B
src/frontend/                      C  React 19 + Vite
data/                              B  Numbat（demo/）、Xero 原始数据（external/xero/）与补造目录（xero_demo/）、生成与校验脚本
tests/                             默认套件不连库；slow 连库或调真实模型，external 读外部数据，live 调真实模型（CLAUDE.md 测试一节）
compose.yml                        B
```

## 6. 本版不做

本节汇总各需求写明的本版不做与 Q32 暂停的功能，每条括号里写来由（需求编号、决定号或待拍板 Q 号；“决定 n” 指计划第 1 节，“第 13 批” 指 10-19 的裁定，“初版” 指 Demo 第一版的范围）。

### 6.1 未开的功能（Q32）

验证范围只有演示配置（critic、collab、journals、misstatement、planning）与 Numbat 基线；下列功能的程序或代码可能已在库里，本版不开，也不验证（Q32），打开后的行为不作保证。

- 关联方（related_parties）：GEN-08、GEN-03 已有（R136–R140）；GEN-01 完整性终审、FIN-01 闸门只读的关联方结论、关系图接口与页面、Agent 层集成与接入未做（R141–R145、R147 暂停）；注入测试 T6 暂停（R254，R255 的 T6 部分）。
- 持续经营与期后事项（going_concern）：GEN-07、GEN-06 已有（R177、R179；FIN-02 的输出模型 R183）；GEN-06 与其独立复核的 Xero 固定输出、GEN-04、FIN-02 与 gate_fin 两步、报告的持续经营段与其他事项段、闸门页与接入未做（R180–R182、R185–R193 暂停）；注入测试 T3 暂停（R256、R257）。演示走通因此没有持续经营段，不是设计稿第 7 节的无保留意见加持续经营重大不确定性段。
- 自主度动态降级（autonomy）：降级规则、装配与判断两个时点的接入、关键证据缺失的判断交人已做（R168、R170），不开时程序按模板的自主度运行。
- 承接的约定条款与委任核验（acceptance）：ACC-08 只有契约与模板（R198）；核对规则、A2 执行、节点与闸门、闸门页与接入未做（R199–R202、R211、R213、R214 暂停）。
- 重要性重估（reassessment）：PLN-19 与其闸门未做（R207–R210、R212–R214 暂停）。
- 签字前提（presign）：FIN-15、FIN-07、FIN-18、FIN-10、RPT-08、RPT-07、签字前清单、无法表示意见与签字确认（契约 R215 已有；R218–R222、R225、R227–R244 暂停；R224、R226 随 Issue #46 取消）。

这些功能（与演示配置已开的错报闭环）在各自需求里另写了打开后也不做的部分，随功能一并不做：

- 持续经营（R192，G3 段约定）：GEN-07 S04（见第 6.3 节）；规格书 12.2 的 SubsequentEvent 对象与 18.3 的 subsequent_events 状态字段；ASA 560 第 7(a)(c)(d) 段的程序；FIN-02 的前置 GEN-18、FIN-17、FIN-19；“持续经营假设不恰当” 的否定意见（T8）；环 3 的重大期后事项阻断。
- 签字前提（R244、R228、R234）：FIN-14（由 RPT-07 代为覆盖 ASA 450 第 12 段）、ACC-10 改出修正意见或撤出、项目质量复核的执行（EQR）、RPT-09、RPT-10、GEN-23、法定通报、收费依赖度的往年数据。
- 关联方（R125、R142）：环 3 阻断与项目级风险重评，改为在 FIN-01 闸门只读列出关联方完整性结论。
- 错报闭环（R158、R162、R62）：舞弊迹象只呈报，不进环 3 阻断，不重评舞弊风险（ASA 240 第 37 段）；等待更正的期限与到期提醒（规格书第 10 节 “由 A10 提醒”），本版停在 await_correction 一直等审计师提交更正通知；更正传导（更正后试算表不改版，依赖科目的已完成程序不标 needs_review）。
- 自主度（R168，决定 2）：首次服务该客户 −1、财报层次风险 −1、GEN-17 强制 significant 与升级规则（规格书第 7.4 节）。

### 6.2 暂停的其他需求（Q32）

- 错报闭环（misstatement，Q34 只开它，不开 autonomy）：演示只走第 1 轮管理层全部更正（R195）；管理层拒绝更正的未更正路径（R196 暂停），第 2 轮（M-2 至 M-4）与 FIN-15 依赖签字前提，不做。
- 工作底稿：只有底稿索引号、所属区、必填项与准则对照表（R329）；底稿记录、生成、判断类与决定类底稿、错报汇总与索引、导引表、审计档案页、归档锁定与归档后修改、Xero 演示的底稿接入未做（R330–R338 暂停）；底稿保存期限（ASA 230 第 15 段）与报告日后新增程序（第 13 段）随之不做（R337）。
- 分录与期后程序的被测总体（R328 暂停）：GEN-05、GEN-07 的被测总体记为未记录。

### 6.3 各需求写明的本版不做

智能体协作与呈递：

- 复核任务批量确认与复核范围 batch（规格书第 9.1、12.2 节）（R148）
- 在对话里提交决定；闸门到达时的呈递与 A10 主动通报；转达一致率统计（规格书第 18.2、5.4、15 节）。对话只读，决定只走卡片上的表单（R148、R152）
- 呈递时由大模型生成的白话讲解：GEN-11 只给结构化卡片，需要解释时在对话里问（R148、R152）
- 复核过快预警（规格书第 9.3 节，阈值规格书未给）：只记录停留时长；闸门决定不算停留时长（R153）
- GEN-09 的 “提示词或配置刚改过且复核数不足 30” 与 “人机一致率低于 80%” 两条触发；A7 复核率、一致率、错误发现率与误阻断率的统计（规格书第 6.3、15 节）（R149）
- 环 1 至环 3 与 GATE-R，含 A7 以证据不足判不一致时的追加取证、仲裁 neither 时的追加程序（R148）
- 会话结束清除对话记忆（规格书第 8.5 节）：一个项目一条连续对话，超出预算时自动压缩（R148）
- 盲判（L1 的 blind 标志）、9.2 盲测抽查、人机一致率统计与按一致率的降级和升级（规格书第 7.1、7.4、9.2、9.5 节）：用户删去，降级越过 L1 时截在 L1（第 13 批 T10；R154、R169、R171–R173 取消）

判断与复核：

- 认定错报不按程序限制：分录归因与科目程序可能对同一笔各建议一次，分类与更正验证的判断也可认定错报，由审计师避免重复（第 13 批 T12；R133 取消）
- more_evidence 判断的后续（追加程序、GEN-23 管理层询问与答复取证）：审计师只能 confirm 表示知悉，或 override（R125）
- 规则执行前检查命中的规则进复核队列（规格书第 2.3 节）：只在执行记录与程序视图列出（R130）

承接、计划与函证：

- 项目质量复核与审计师的专家只在 PLN-15 批准时记录，不在签字闸门强制检查；没有审计师专家程序（R206）
- 约定书的年度报告构成与其他信息安排（规格书第 1345 行与第 2146 行）（R198）
- 函证催函与截止定时器、未回函替代程序、管理层拒绝寄发（初版）

数据与证据：

- 补交请求的其余功能：只有 A3 提出请求、按请求上传（带 request_id）与标为客户无此类资料；人工程序的证据文件在人工队列页经同一上传接口上传（R94）
- 上传文件补登记的关联对象不校验是否存在（R90）
- 向量索引与 OCR：只读 PDF 文本层（初版）
- 分录 “类型” 列：手工分录以数据给出的 is_adjustment 为准，GEN-05 R07 不分凭证类型（R125）
- ASIC 人员检索同名多人时的人工确认（R125）
- Xero 收款与发票对应表（payments.json 的分配记录），因而 GEN-07 S04 “期末应收到截止日仍未收回” 不做（收款分录的 source_doc 不带发票号）（R62、R177）
- 分录表加 reverses_entry_no 列：作废对仍由规则按数据集声明的冲回说明格式识别（R62）
- Demo 重置时的派生快照脚本 `data/derive_xero_period.py`（本仓库已有 20261017-q1）；从快照裁最小 fixture 入库（本仓库已带快照，测试只保留 “没有快照就跳过” 的守卫）（R62）
- 校验脚本复算 Xero 演示数据集设计第 5 节的规则命中与第 7 节的计数：由各程序的连库测试与合并终验核对（R62、R69）
- 为新程序增补 Numbat 数据（承接文件、更正分录、工资册、登记摘录）：对应端口在 Numbat 上返回空或 None（数据段约定，需求起草说明 R62 起）
- 财务报表以账套为准：不取得报表草稿；不做财务报告与会计记录的核对（ASA 330 第 20(a) 段，本版按报表与账一致假设）；报表列报与重分类，上年比较数的核对与重述（ASA 710 第 7、8 段），附注与现金流量表的核对都不在本版范围；报告不引附注编号（Issue #46，Q21；R320）
- 数据安全与敏感信息的设计（规格书第 4.3 节外部 LLM 数据边界：字段最小化、姓名与税号及账号的占位替换和本地对照表、留痕记外发字段清单、服务商的 APP 8 条件；以及多角色授权、证据静态加密、归档哈希）：统一不做；已实现的行级安全、密钥加密与出生日期守卫保留（Q23）

程序范围：

- GEN-12 独立性与利益冲突持续监控、EXP-04 关联方费用识别、GEN-17 超出正常经营过程的重大交易识别与评价（R125）
- 原型替身与 not_implemented 注册：程序库只放第 2 节所列的程序（初版、R125）
- 中文的固定输出：mock 模式只出英文，不另备中文固定输出；中文工作语言的项目照常走通，执行记录的生成语言照实记 en（组装根在 LLM_MODE=mock 时填 AgentDeps.fixed_language），页面在判断文字旁标 “以英文生成”，报告按执行记录的语言出英文；中文输出只在 live 模式由真实模型生成（R343）。理由：固定输出是整套英文正本，再备一套中文要双份维护，校验与守卫也要成双；代价：mock 下看不到中文的判断与报告，中文效果只能用真实模型验

外部数据与评测：

- 反编造检查（条款原文必须出现在声明页）不进正式 OutputValidator，只在 CUAD 评测里算（R246、R249）
- DOC-04 的 context_budget 执行与超长合同截断；生产配置项 LLM_TIMEOUT（评测只在测试内传 300 秒）（R249）
- 扩 TermType（CUAD 的最低采购量、价格限制、分成、违约金等可变对价类条款）；REV-03 评服务与许可类合同（只评 6 份货物合同）（R247）
- CUAD 扩展集的分数判失败：只看趋势（R249）
- 真实大模型的费用金额：只在给出单价环境变量时折算（R115）
- T5 的真实大模型跑法；注入场景的全流程：注入只在程序级跑，全流程只跑基线即演示走通（R253，Xero 演示数据集设计第 8.2 节）

## 7. 准则片段

段落号已对照 `参考资料/` 中的现行编译版核对。原文摘录在 `knowledge/standards/`，由各需求从 `参考资料/` 的 PDF 填入。

片段 ID 写作 `{YAML 文件名}-{段码}`（如 ASA450-P05，编号段补足两位），段码规则见开发接口文档第 8.2 节；`tests/test_knowledge.py` 按该规则核对全部片段的 ID。下表与程序库中各程序 `standards` 引用的片段 ID 集合相同，“程序” 一列为引用该片段的程序（含未启用功能的程序），`tests/test_docs.py` 逐行核对。`knowledge/standards/` 里另有暂停需求预先摘录、现在没有程序引用的片段（PLN-19、GEN-01、GEN-04 与 FIN-02 要用的），不列入本表。

| 片段 ID | 内容 | 程序 |
|---|---|---|
| AASB15-P31 | 履约义务满足时确认收入 | REV-03、REV-02 |
| AASB15-P35 | 随时间转移控制权的三项条件（任一成立即在一段时间内确认收入） | REV-03、REV-02 |
| AASB15-P38 | 时点履约义务的控制权转移迹象 | REV-03、REV-02 |
| ASA320-P10、ASA320-P11 | 整体重要性、执行重要性 | PLN-01 |
| ASA450-P05 | 明显微小的错报不累计 | PLN-01、FIN-04、CSH-02 |
| ASA450-P07 | 管理层按审计师要求更正后，执行追加程序确定是否仍有错报 | FIN-09 |
| ASA450-P11 | 评价未更正错报是否重大 | FIN-01 |
| ASA505-P07 | 审计师控制询证函 | CSH-04 |
| ASA505-P14 | 调查回函差异 | CSH-02 |
| ASA705-P07、ASA705-P08 | 保留意见、否定意见的适用条件 | FIN-01 |
| ASA700-P23、ASA700-P28 | 意见段、形成意见的基础段 | RPT-03 |
| ASA705-P16、ASA705-P20、ASA705-P21 | 非无保留意见的标题、基础段描述、错报金额影响 | RPT-03 |
| ASA240-P32 | 管理层凌驾于控制之上的风险属特别风险 | GEN-05、GEN-02、PLN-15 |
| ASA240-P33 | 分录测试（只摘第 33(a) 段） | GEN-05、GEN-02、PLN-15 |
| ASA240-A44 | 选取分录与其他调整时考虑的事项 | GEN-02 |
| ASA240-P36 | 识别出错报时评价其是否有舞弊迹象 | FIN-03 |
| ASA450-A6 | 事实错报、判断错报与推断错报的区分 | FIN-03 |
| ASA450-P08 | 及时与管理层沟通累计的错报并请其更正 | FIN-08 |
| ASA450-P09 | 管理层拒绝更正时了解其理由并在评价时考虑 | FIN-08 |
| ASA550-P10 | 关联方的定义（只摘第 10(b) 项） | GEN-03 |
| ASA550-P15 | 检查记录与文件时留意未识别的关联方 | GEN-08 |
| ASA550-P21 | 发现可能有管理层未识别的关联关系或交易时，确定其是否存在 | GEN-03 |
| ASA550-P22 | 新识别关联方的应对 | GEN-03 |
| ASA550-P23 | 超出正常经营过程的关联方交易 | GEN-03 |
| ASA560-P06 | 识别需要调整或披露的期后事项的程序 | GEN-07、GEN-06 |
| ASA560-P07 | 期后事项程序覆盖的期间与应包括的程序 | GEN-07 |
| ASA560-P08 | 识别出的期后事项是否已在财务报告中恰当反映 | GEN-06 |
| AASB110-P03 | 调整事项与非调整事项的定义 | GEN-06 |
| AASB110-P08、AASB110-P10 | 调整事项要调整，非调整事项不调整 | GEN-06 |
| AASB110-P09 | 调整事项的例子 | GEN-06 |
| AASB15-P88 | 交易价格的后续变动在变动的期间确认（冲减）收入 | GEN-06 |
| ASA210-P06 | 审计的前提条件：财务报告编制基础可接受，管理层认可其责任 | ACC-08 |
| ASA210-P08 | 前提条件不具备时不承接 | ACC-08 |
| ASA210-P10 | 审计业务约定书的内容 | ACC-08 |
| ASA300-P07 | 总体审计策略 | PLN-15 |
| ASA300-P09 | 审计计划的内容 | PLN-15 |
| ASA300-P13 | 首次审计开始前的活动 | PLN-15 |
| ASA510-P06 | 首次审计取得期初余额的证据 | PLN-15 |
| ASA570-P10 | 风险评估时考虑是否存在可能导致对持续经营能力产生重大疑虑的事项或情况 | PLN-15 |
| ASA710-P14 | 上期未经审计时在其他事项段说明 | PLN-15 |
| ASQM1-P34f | 须做项目质量复核的业务（只摘第 34(f) 项） | PLN-15 |
| CA2001-S293 | 小型私人公司的股东指示（整条，第 (1) 至 (3) 款） | ACC-08、PLN-15 |
| CA2001-S301-2 | 小型私人公司财务报告免于审计的条件 | ACC-08 |
| CA2001-S315-4 | 私人公司在财年结束后 4 个月内向成员报告 | PLN-15 |
| CA2001-S323D-2A | 缩短的财年 | ACC-08、PLN-15 |
| CA2001-S325-1 | 董事委任私人公司的审计师 | ACC-08 |
| CA2001-S328A | 审计师同意委任（整条，第 (1) 至 (4) 款） | ACC-08 |

关联方、持续经营等披露在第 2 层下依据 AASB 1060。正本（第 7 次汇编）已在 `参考资料/03_会计准则_AASB/AASB_1060.pdf`，用到的段号见 Xero 演示数据集设计 “待核与待正本” T7（需求 R292）；本版没有程序引用 AASB 1060，不建它的片段。AASB 124 只作参照，不建片段。

报告模板以 ASA 700 [Aus] Illustration 1A（无保留意见）和 ASA 705 [Aus] Illustration 1A（因重大错报发表保留意见）为底本，去掉关键审计事项与其他信息两段。

## 8. 验收

本版的验证只覆盖演示配置与 Numbat 基线（Q32）；未开的功能不验证（第 6.1 节）。连库的测试标 slow，读外部数据的标 external，调真实模型的另标 live 并写调用上限（需求 R115，超出即停发并失败）；没有测试库、数据或模型配置时跳过（CLAUDE.md 测试一节）。

| 验证 | 跑法 | 测试 | 需求 |
|---|---|---|---|
| Numbat 全流程回归：基线功能（只开 critic），四道闸门中断并按决定恢复，结果与第 3 节预期一致；新功能在程序包、状态、任务与闸门里没有痕迹 | 离线 mock；真实存储变体；三处中断的重启；真实模型 | `tests/test_e2e_mock.py`、`tests/test_e2e_restart.py`、`tests/test_e2e_live.py`、`tests/test_numbat_switches.py` | R55–R57、R59、R258 |
| Xero 演示走通（第 1 节）：每个停点、闸门与复核决定、例外、判断、错报各版本与汇总表（错报闭环第 1 轮全部更正）、报告版本与归档、文件与留痕的计数；工作语言 en、zh 各一次 | 离线 mock，经 HTTP 接口 | `tests/test_demo_walkthrough.py` | R260、R343、R195 |
| Xero 演示走通的连库变体：真实 Postgres 存储与检查点、规则 SQL、真实 DataService 的演示数据导入，结果同离线 | 连库 | `tests/test_demo_walkthrough.py`（slow） | R261 |
| Xero 演示走通的重启：P2 发函后被杀、PLN-15 批准、认定错报的决定与更正通知写进 Store 后中断，用同一检查点与存储起新进程恢复，照页面走到归档；函证不重发，已持久化的程序不重跑，审计调整分录只载入一次，终态与留痕同不中断时 | 离线 | `tests/test_e2e_xero_restart.py` | R262、R195 |
| Xero 演示走通的真实模型全流程：重要性、汇总表与意见同候选，偏离只在人给了结论的对象上；中文工作语言跑一次 | 连库，真实模型 | `tests/test_demo_walkthrough_live.py` | R263、R343 |
| 注入测试（只改临时副本，原目录哈希不变）：T0 基线副本，T1 收入截止，T4 银行回函差异，T2 重复开票未冲销，T5 与反向对照 T5' 折旧错记；真实模型成对断言 T1、T4、T2 | 离线；连库；真实模型 | `tests/test_xero_injections.py`、`tests/test_xero_injections_db.py` | R121–R124、R252、R253、R255 |
| Xero 上的真实模型程序评测：现有程序与分录测试程序（GEN-05 真实 SQL，GEN-02 交真实模型）在真实异常上的结论 | 连库，真实模型 | `tests/test_eval_xero_live.py` | R250、R251 |
| CUAD 评测：金标准 10 份与人工确认，本地原件的哈希与页码核对，DOC-04 与 REV-03 的真实模型评分 | 离线；本地原件；真实模型 | `tests/test_cuad_eval.py`、`tests/test_cuad_local.py`、`tests/test_eval_cuad_live.py` | R246–R249 |
| 文档与代码一致：开发接口文档的枚举、状态字段、端口、表、路由、配置与错误码都写到；本文档第 2、7 节与程序库一致 | 离线 | `tests/test_docs_contracts.py`、`tests/test_docs.py` | R265、R264 |

各项共同的要求：每条 Judgment 的证据指针都能解析到原文对应页；签字后项目只读，提交任何决定被拒；audit_trail 能查到每次 LLM 调用、每个人的决定与每次状态迁移；以应用角色连接时，不设 engagement_id 的查询取不到项目数据（行级安全）。

## 9. 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 接口对齐 | contracts 包、数据库表、API 路由定义完成并合入 | 完成 |
| M2 骨架跑通 | 17 条程序先用固定返回值占位，全流程在 mock 下走通四道闸门，前端能操作每一步 | 完成 |
| M3 程序实装 | 规则程序接真实 SQL，LLM 程序接提示词与校验；Numbat 合成数据集完成，场景全部命中 | 完成（R01–R61） |
| M4 演示（Numbat） | 接真实 LLM 联调，逐项验收 | 完成（第 0 级：R54、R56、R59） |
| 阶段 A 设计与数据收尾 | 演示公司改用 Xero Demo Company (AU)（决定 3），补造数据与载入，Xero 演示数据集设计定稿 | 完成（R62–R74） |
| 阶段 B 公共底座 | 功能开关、新状态字段、判断公共流程、闸门登记表、人工处理队列、数据集声明，现有程序改成通用写法（决定 9），只开现有 17 条的 Xero 全流程 | 完成（R75–R124） |
| 第 1 级 | 分录测试（GEN-05、GEN-02）；智能体协作（A7 触发规则、争议与仲裁、判断呈递、停留时长） | 完成并接入演示配置（R146、R155） |
| 第 2 级 | 关联方；持续经营与期后事项；错报闭环；自主度降级 | 错报闭环接入演示配置（R195，Q34，只走第 1 轮全部更正）；其余部分完成，功能不开（第 6.1 节，Q32 暂停） |
| 第 3 级 | 总体审计计划 PLN-15；约定条款 ACC-08；重要性重估 PLN-19；签字前提 | PLN-15 完成并接入演示配置（R317）；其余暂停（Q32） |
| 收尾验证 | 演示配置的 Xero 走通与其连库、重启、真实模型变体，Numbat 回归，注入测试，CUAD 与 Xero 的真实模型评测，文档定稿（第 8 节） | 进行中（Q32，F 组） |


工作底稿归档边界 (R337): 报告签字后整套 Excel 底稿锁定为最终版本, 未签署底稿仍定稿并标记未签署. 归档后仅能追加带理由的补充, 保留原文和历史版本; 保存期限管理 (ASA 230 第 15 段) 与报告日后执行新增程序 (第 13 段) 本版不实现.
