# 需求草稿 X1-X8 对抗式审核

**结论:** 草稿里的大部分数据统计能复算对上，但有 4 处会让测试做不出来或判错:
- X7 中 REV-08 R03 的预期值算错，X8 的 REV-02 判断对象跟着错。
- X3 放 CUAD mock 输出的位置会让现有测试失败。
- X2 的超预算异常在全流程里会被节点吞掉，测试反而可能反复重跑。
- 同日从 Xero API 拉的快照 `data/external/xero/api/20261017` 草稿完全没有用上。

复算脚本在 `/private/tmp/claude-501/xero_audit/`（`je2.py`, `tb3.py`, `inv2.py`, `at.py`, `pln07.py`, `cuad1.py`）。

## 已复算核实的数字 (无需改)

**Xero 日记账与 GEN-05 计数**
- 日记账 368 笔 (ID 389-756), 1017 行, 每笔平衡, 借贷合计 439,695.07。
- 冲回 65 笔, 与原分录逐行镜像 65/65; 有效分录 238 笔 675 行。
- 季度期间 215 笔 582 行。
- JE-R01: 200 贷方 >300 的 30 行, 其中过账日为周末 25 行 (按 UTC 与按悉尼时间都是 25); 不限期间时 UTC 47, 悉尼 46。
- 按 posting_date 为周末 5 行 (ID 428, 437, 481, 484, 545)。
- JE-R02: 1,000.00 整数倍 6 行 (明细与草稿一致); 10,000.00 整数倍 0 行; 100.00 整数倍 84 行 68 笔。
- JE-R05: 09-27 至 09-30 有 8 笔, 摘要含 revers 0 笔。
- 有效分录中悉尼周日过账 173 笔。
- ID 499 的分录内容与草稿一致。

**试算表与余额**
- 0630 表 6 个科目, 借贷各 12,903.20; 1130 表 32 个科目, 借贷各 111,754.91。
- 推导的 0930 试算表 26 个科目, 合计为 0。
- 610 = 25,056.55; Approved 发票 13 张合计 25,906.55。
- 090 = -12,023.26; 825 = 9,042.00; 840 = 19,212.21; 477 = 40,200.00; 416 不在表中。
- 资产 28,106.56, 负债 38,856.10; 五个月亏损 2,837.54; 流动比率 0.63。

**PLN-07**
- 下限 100,000.00 时 0 命中; 1,000.00 加 20% 时 20; 500.00 加 20% 时 24; 毛利率 98.7%。

**发票与银行**
- 发票明细 55 张 (发票 49, 贷项 6), 60 行; Paid 35, Approved 13, Draft 4, Voided 2, Awaiting Approval 1。
- 期后发票 16 张; 期后应收收款 11 笔, 合计 15,417.60。
- 银行流水 133 + 5 = 138 行。
- 科目表 59 个, Type 22 种, 803 的 Type 为空。
- REV-08 R02 在期末 09-30 窗口内命中 7 张, Net 合计 8,945.00。

**CUAD**
- master_clauses.csv 510 行 × 83 列; Parties-Answer 非空 509; Agreement Date-Answer 465 (m/d/yy 416, 含 [] 49)。
- PDF: .PDF 311 个, .pdf 199 个; 规范化后 510 份一一对应 (全量也已复算, 不只七类)。
- 七类共 174 份; 全文含 ABN 0 份。
- 10 份候选样本: 页数合计 106, 字符数合计 301,601。
- LIMEENERGY 正文为 7th day of September 1999, 末页印 Page -14-。
- 定位统计: 只在一页 2299, 多页 550, 跨页 7, 找不到 8。

## 问题清单 (按严重程度)

### 严重

**1. X7 验收 5 和 X8 验收 2, 6: R03 "只命中 ORC1002, 差额 250.00" 是错的**
- **窗口不对:** `REV-08.yaml` 第 41-50 行, R03 同样只看 posting_date 在 [期末-窗口, 期末] 内的发票。ORC1002 开票日 2026-07-26, 15 天窗口是 09-15 至 09-30, 不在内。
- **参考号连不上:** ORC1002 的收款参考号是 "Part" (`account_transactions.xlsx` 090 组 08-05 行; `api/payments.json` 相同)。position('ORC1002' IN 'Part') = 0, 不限窗口也连不上。
- **实际复算 (含税, 窗口 15 天):** R03 只命中 ORC1026。发票 7,150.00, 参考号含 ORC1026 的收款 3,850.00 (10-14), 差额 3,300.00。这是误报: 同日参考号为 "Part 1" 的收款 2,145.00 和贷项 1026C 的 1,155.00 没有被关联上。
- **不含税时:** 命中 5 张 (ORC1020, 1021, 1023, 1024 都是 500 对 550, 加 ORC1026)。
- **REV-02 判断对象:** 按 Q13 是 S01 ∪ REV-08 全部例外。S01 的 6 张都在 R02 的 7 张里, ORC1026 也在里面, 所以共 7 张, 不是 8。
- **改:**
  - X7 验收 5 的 R03 改为 [ORC1026], amount_cents 330000 (含税口径); 不含税时为 5 张。
  - X8 改为 7 个判断对象。
  - X6 写明 bank_transactions.reference 取 `account_transactions.xlsx` 的 Reference 列。API 的 payments.Reference 大多为空, 用它的话 R03 恒为 0。
  - 待拍板第 3 问里 ORC1002 的例子要换掉。

**2. X3 把 CUAD mock 输出放进 `tests/fixtures/llm/DOC-04/`, 会让两个现有测试失败**
- `tests/test_knowledge.py` 第 441-470 行 `test_fixture_document_ids_in_manifest` 遍历 `FIXTURES.rglob("*.json")`。它要求每个文档指针的 source_id 在 Numbat 的 manifest 里, 并且 checked 字典与预期精确相等。
- 第 503-512 行 `test_runtime_fixtures_match_test_fixtures` 要求 `tests/fixtures/llm` 与 `data/demo/llm` 的文件集合相同。
- **改:** 放到 `tests/fixtures/cuad/llm/DOC-04/CUAD-01.json, CUAD-03.json`, 测试用单独 root 的 MockLLMClient。

**3. X2 的 BudgetExceeded 在全流程里不会让测试失败**
- `orchestration/nodes.py` 第 350-363 行 `run_agent` 用 `except Exception` 接住 Agent 的异常, 转成 failed 任务。
- R59 验收 2 对 failed 任务提交 rerun; 按 Q17 是整组重跑, 结果又超预算, 又是 failed。测试会反复重跑而不是失败。
- **改:**
  - BudgetExceeded 继承 BaseException; 或者包装器置一个已超限标志, 测试每次提交决定前先检查。
  - 加一条验收: R59 全流程中超限时测试以失败结束, 不再提交 rerun。

**4. X2 验收 3 "按校验失败级别统计" 在包装客户端里拿不到**
- 校验发生在 `judgment_engine.py` 第 497-512 行的 validator 里, 结果只写进 llm_call 留痕的 payload.level_failed (第 528-552 行)。
- LLMClient 只能看到请求 (procedure_id, fixture_key) 和响应。
- 级别名是 syntax, structure, reference, semantic (`contracts/services.py` 第 49 行), 草稿写成了 "业务"。
- **改:** 调用次数和 token 由包装器计; 尝试次数分布与失败级别从 AuditTrail 的 llm_call 行汇总 (payload.attempt, level_failed); 级别名改为 semantic。

**5. 在 worktree 里跑, 真实 LLM 测试会因为没有 .env 而全部 skip**
- `core/config.py` 第 15 行 env_file = ROOT/.env。实测主 checkout 有 .env, WT 没有。X1 只处理了 data/external 在 worktree 里的定位。
- **改:** X1 验收 4 的 `require_live_llm` 依次取 LLM_ENV_FILE、git common dir 上级目录下的 .env、仓库根下的 .env。验收写成 "在 WT 中能取到配置", 只断言键存在, 不输出值。

**6. 漏项: `xero/api/20261017` 没有用上**
- **内容:** 15 个 JSON。manifest 记录 pulled_at 2026-10-17T07:33Z, 共有 accounts 62, invoices 92, credit_notes 8, payments 71, bank_transactions 88, manual_journals 4, contacts 83。journals 接口返回 401, 拿不到。
- **与 raw 是同一快照:** ACCREC Paid 加 Authorised 共 42 张, 合计 73,295.65, 与 raw 一致; 1130 试算表也是 32 行。
- **raw 没有而 API 有的字段:**
  - accounts.Class: 只有 5 类, 803 直接是 LIABILITY, 不用按 22 种 Type 手工归类。
  - contacts.IsCustomer / IsSupplier: 草稿说 "没有客户供应商标志", 只对 CSV 成立。
  - bank_transactions.IsReconciled, 以及 payments 与发票的关联。
  - organisation: 财年末 6 月 30 日, 时区 AUSEASTERNSTANDARDTIME (支持选 Australia/Sydney; 但 Posted Date 本身的时区仍没有原文佐证), RegistrationNumber。
- **改:** X6 依据加入 api 目录; 科目类别、发票、收款、贷项取自 API, 日记账仍取 xlsx。X1 的快照校验同时锁 api 目录。`contacts.json` 也含登录账号的姓名和邮箱, 同样不输出。

**7. X6 验收 5 的 "200 科目 FY27 Net 合计 59,944.32" 少写了过滤条件**
- Paid 加 Approved 的 FY27 全部明细行 Net 合计是 59,953.41。
- 多出的 9.09 是 Bayside Club 10-17 一行 Courier charge, Account Code 为 425。
- **改:** 写明 "Account Code 为 200 的行"。

**8. FIN-12 预期的 "唯一差异 960" 依赖口径, 而且 960 本身不是错报**
- 1130 表的列是 "Debit/Credit - Year to date"。损益科目要取 07-01 起的本财年发生额才对得上; 按全部累计汇总会有 5 个差异 (200, 412, 453, 461, 960)。
- 960 的 7,912.00 正是 FY26 损益 (12,112.00 - 4,200.00) 的年末结转。
- `stores.py` 第 92-98 行 gl_balance 已经有 "上期损益滚入留存收益" 的逻辑, 只是写死了 3200。改为留存收益科目参数后, 960 也一致, 结果是 32/32。
- **改:**
  - X6 写明口径: 资产负债类累计, 损益类取本财年。
  - X7 验收 3 改为: 滚入后 32/32; 不滚入时 960 应解释为年末结转, 不算例外。
  - 修 3200 写死升为 X7 的前置, 不再写 "暂不影响"。

**9. PAXMEDICA 的签订日金标准判错了**
- 原文首段写 "entered into on May 25th, 2018", 签字栏两处写 "Date 25/05/2008"。
- `DOC-04.md.j2` 规定 sign_date 取签署日, 双方日期不同时取较晚者。照此按签字栏是 2008-05-25。CUAD 标的 5/25/08 有原文依据。
- **改:** 金标准接受 {2018-05-25, 2008-05-25}, 或这一份不计分。风险项改为 "CUAD 标注错 1 处 (LIMEENERGY), 合同自身矛盾 1 处"。

### 中等

**10. token 上限覆盖不了重试的最坏情况 (待拍板第 9 问的说法不成立)**
- 重试时会把上一次的原文和错误追加进对话 (`judgment_engine.py` 第 509-515 行)。
- 估算: 10 份合同约 75k token, 加提示词约 85k。每份最多 3 次, 加上累积的输出 (max_tokens 8192), 最坏约 50 万 token, 超过 X5 的 35 万。
- **改:** 超预算不再列为 "质量失败" 的硬性项, 而是 "中止并标未完成"; 或者把上限提高到约 55 万, 并更正第 9 问。

**11. X8 与 Q17 冲突**
- Q17 规定 rerun 时整组重跑。X8 验收 6 要 rerun 一次, REV-02 的 7 个对象就要再调最多 21 次。
- X8 的上限 51 = 17 × 3, 没有给 rerun 留余量 (按 7 个对象算本来就是 16 × 3 = 48)。
- **改:** 上限改为 (6 + 1 + 2 + 7 × 2) × 3 = 69。

**12. X8 验收 6 的断言与 R41 不符**
- 按 R41, 判断成功时 (包括 more_evidence) 建 normal 任务; 只有失败才建 unattributed 或 failed 任务。
- 模型如果合规地答 "证据不足", 断言会误判测试失败。
- **改:** 每个对象要么是 proposed_action 为 more_evidence 的 Judgment, 要么是 unattributed / failed 任务; 不出现 accept, close, misstatement。normal 任务提交 confirm。

**13. X4 验收 5 的阈值正好卡在边界**
- (2299 + 550) / 2864 = 99.476%, 小于 99.5%; 算上跨页是 99.72%。
- **改:** 写成 "找不到 ≤ 8, 跨页 ≤ 7"。

**14. 去标识测试不能把原值写进仓库**
- 登录账号姓名在 journal_report 的 Posted By 中出现 952 行; contacts.csv 和 contacts.json 里有该姓名和 gmail 邮箱。
- **改:** 测试运行时从 raw 读出这些字符串 (Posted By 中非 System Generated 的值、contacts 中的邮箱), 再断言输出里不出现; 仓库里不出现原值。

**15. 窗口内 7 张发票里有 1 张是贷项通知单**
- ORC1037 是贷项, Net -55.00 (含税 -60.50)。R02 会把它报成 "缺发运单"。
- **改:** 加一个拍板项 "贷项是否进 sales_invoices"; 或者在 X7 中注明这一张, X8 也相应处理。

**16. X7 验收 4 与 "按科目类别取收入" 的修改建议冲突**
- API 中 Class 为 REVENUE 的有 200, 260, 270。按类别取收入时, A02 的收入是 60,041.37, 不是 59,944.32。
- **改:** 先定收入取数方式 (推荐: 参数化科目清单), 再写 X7 的数。

**17. JE-R04 的 "approved_by 全空 → not_executable" 需要显式检查**
- 规格书第 925-931 行的 SQL 是 created_by = approved_by。approved_by 全为 NULL 时返回 0 行, 看起来就是 "通过"。
- **改:** GEN-05 实装需求写明 "required_fields 在本期分录中全为空即视为缺失"。

**18. X1 的快照清单依赖 X3 的选样, 顺序倒了**
- **改:** 10 份 PDF 的哈希由 X3 加, X1 只锁 Xero (raw 加 api), CUAD 的 csv 与 json。

**19. 三个测试合计 159 次的上限没有实现手段**
- X2 只定义了每个测试各自的上限。
- **改:** X2 加验收: conftest 放一个会话级共享计数器。

**20. CC BY 4.0 署名要素不全**
- 许可第 3(a)(1) 节要求: 署名、许可声明、免责声明、许可的 URI 或链接、注明是否修改。
- CUAD README 第 343-347 行含许可声明和 "对底层合同许可状态不作保证" 的免责句。
- **改:** README 加入 https://creativecommons.org/licenses/by/4.0/ 、 https://zenodo.org/records/4595826 、原免责句, 并列出所有含 CUAD 文本的入库文件 (逐页文本、gold.json、mock 输出、extended.json)。

### 低

- **21. 期后笔数与数据截止日:** 10-01 至 10-17 实为 144 笔 (草稿写 145)。另有一笔 ID 755 日期为 11-25 (DIISR, 610 与 877 往返, 净额 0)。Posted Date 最大为 11-27, 晚于导出日 10-17。所以待拍板第 1 问 "数据到 10-17 就结束" 不准确。
- **22. 090 "日内最低 10-15 的 -18,493.96" 复现不出来:** 按日期加 ID 逐笔累计, 最低是 10-11 的 -18,938.11; 按日末余额, 最低是 10-12 的 -17,219.86。这个数不要进断言。
- **23. ClientProfile 必填字段没写全:** X6 验收 7 没给 entity_type, industry_anzsic, listed (`models.py` 第 414-423 行)。
- **24. Apollo 的处理没有说明理由:** Apollo (Manufacturing and Supply, 21 页) 是货物合同, 却没列入 6 份货物合同。

### 粒度与范围

- **25. X3 偏大:** 建议拆成 X3a (评分函数、逐页文本、mock 输出, S, 离线) 和 X3b (10 份金标准加人工确认, M, 作为 X5 的前置)。
- **26. X7 与 X8 依赖尚未定稿的新程序接口:** 建议拆分:
  - X7a: 现有的 PLN-07, REV-08 在 Xero 上跑, 前置 X6, R56 和收入科目参数化, 可以先做。
  - GEN-05, FIN-12 的 Xero 验收, 以及 X8 中各程序的真实 LLM 验收, 并入各自的实装需求。
- **27. 用户原话 "测试用 xero 数据测试" 可能是指用 Xero 替代 Numbat:** 草稿默认 Xero 只作第二套, 应加一个拍板项。推荐: 作第二套。理由是 Xero 没有合同、发运单、银行对账单和函证, 跑全流程要大量补造。

## 修订后需求要点 (只列改动处)

- **X1**
  - `require_live_llm` 依次取 LLM_ENV_FILE、git common dir 上级目录的 .env、仓库根 .env。
  - 快照锁 Xero raw 7 个文件、api/20261017 的 15 个文件、CUAD master_clauses.csv 与 CUAD_v1.json; PDF 哈希移到 X3。
  - git 路径用 `--path-format=absolute`, git 不可用时回退。
  - skip 原因写进汇总。
- **X2**
  - BudgetExceeded 继承 BaseException (或用标志位), 全流程中超限即测试失败, 不进入 rerun。
  - 失败级别从 llm_call 留痕汇总, 级别名用 semantic。
  - 会话级合计上限。
- **X3**
  - 拆成 X3a 与 X3b。
  - mock 输出放在 `tests/fixtures/cuad/llm/`, 用单独的 MockLLMClient root。
  - PAXMEDICA 接受两个日期。
  - README 补齐许可 URI、免责句和全部入库文件清单。
  - 10 份 PDF 的哈希在这里加。
- **X4**
  - 验收 5 改为 "找不到 ≤ 8 (2864 段中), 跨页 ≤ 7"。
- **X5**
  - 硬性项去掉 "未超预算", 改为超预算即中止并记为未完成; 或把输入上限改为约 55 万 token。
  - 说明 Apollo 不纳入货物合同的理由。
- **X6**
  - 依据加入 api 目录; accounts.type 取 Class; 发票、收款、贷项关联取 API。
  - bank_transactions.reference 取 account_transactions 的 Reference 列。
  - 试算表交叉核对写明口径: 损益取本财年, 资产负债累计。
  - 200 合计只算 Account Code 为 200 的行。
  - 去标识测试运行时读取被禁字符串, 仓库不出现原值。
  - 补齐 ClientProfile 的 entity_type, industry_anzsic, listed。
  - 期后为 144 笔, 外加 11-25 一笔。
- **X7**
  - 拆出 X7a (PLN-07, REV-08), 可先做。
  - R03 改为 [ORC1026], 330000 (含税口径); 不含税时 5 张。
  - R02 的 7 张中注明 1 张为贷项。
  - FIN-12 按 "上期损益滚入留存收益" 预期 32/32, 修 3200 写死升为前置。
  - 收入口径先定。
  - 写明总体重要性 400.00, 使明显微小 20.00 满足 Q8。
- **X8**
  - REV-02 判断对象 7 个, 去掉 ORC1002。
  - 调用上限改为 69, 含一轮 rerun。
  - REV-02 断言按 R41 改写。
  - 拆入各程序自己的需求。
- **changes_to_existing**
  - stores.py 的 3200 由 "暂不影响" 改为 X7 的前置。
  - GEN-05 加一条: required_fields 列值全空视为缺失。
- **待拍板**
  - 第 3 问的例子改用 ORC1026 与 ORC1020-1024。
  - 第 1 问更正 "数据到 10-17 结束" 的说法。
  - 第 9 问更正 "上限覆盖最坏情况" 的说法。
  - 新增两问: "贷项是否进 sales_invoices"; "Xero 是作第二套数据还是替代 Numbat" (推荐: 作第二套)。