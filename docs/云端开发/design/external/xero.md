# Xero Demo Company (AU) 数据摸底

**结论**: Xero 可以作为第二套测试数据. 它的强项是"账与账能对上": 日记账汇总后和两份试算表逐科目一致, 应收发票和应收, 收入科目也一致. 分录里还有不少真实的异常可以拿来测 GEN-05, GEN-02, FIN-15 和 GEN-04. 它不适合测三个方面: 时间戳类信号 (Posted Date 和 Created Date 是 Demo 重置时生成的), 发运单, 独立的银行对账单. 期间方面推荐把项目期间设为 2026-07-01 至 2026-09-30. 下面的统计都由探查脚本按值重算, 脚本在 `/private/tmp/claude-501/xero_probe/scripts/` (`parse.py` 解析, `link.py` 关联来源, `a1.py` 到 `a13.py` 统计), 用系统自带 python3 (openpyxl 3.1.5) 跑; 项目 .venv 里没有 openpyxl, 也没有 pandas.

## 0. README 与实际文件不一致 (建议改 README)

| 项 | README 写的 | 实测 |
|---|---|---|
| journal_report | 756 笔, 1754 行 | 368 笔 (Journal ID 389-756, 1-388 不在文件里), 1017 行; 文件末行 A2128 的合计是写死的值 439,695.07, 等于重算借贷合计 |
| account_transactions | 773 笔, 60 个科目 | 35 个有发生额的科目组, 672 行 |
| receivable_invoice_detail | 171 行 | 55 张单据 (发票 49, 贷项通知单 6), 60 行明细 |
| 客户 23, 供应商 24 | — | contacts.csv 没有客户或供应商标志列; 按交易推出: 开票对象 23 个, 应收对方 22 个, 应付对方 21 个 |

Journal ID 1-388 为什么缺失查不出来. 但第 3 节会看到, 现有日记账与两份试算表逐科目一致, 所以就余额而言数据是完整的.

## 1. 各文件结构与解析坑

**journal_report.xlsx**
- 列: Date, Journal ID, Account Code, Account, Debit, Credit, Posted Date, Posted By. 表头在第 5 行.
- 每笔分录以组标题行开头, 格式是 `ID n 摘要`, 写在 A 列; 组尾有 `Total` 行 (公式 `=SUM`); 组之间有空行.
- Date 范围 2025-07-16 至 2026-11-25. 按月计: 2025 年 7 至 10 月共 8 笔, 2026-07 有 57 笔, 08 有 78 笔, 09 有 80 笔, 10 有 144 笔, 11 只有 1 笔 (ID 755, 净额为零).
- 每笔借贷平衡, 没有负数行, 没有同一行借贷都有金额的情况. 每笔 2 到 9 行: 2 行 132 笔, 3 行 213 笔.
- 坑:
  - Posted Date 带毫秒时间, 时区没有标注. 推断是 UTC: 例如 ID 755 原值是 11-24 23:53, 换算成悉尼时间是 11-25 10:53, 正好和 Date 一致. 这只是推断, 没有原文证实.
  - 摘要只是往来单位名或 `Payment: X`, 没有 Source 和 Reference 两列.
  - 包含 65 笔 `Reversed: ... - Reversal of ID n`. 它们与原分录逐行镜像 (65/65), 日期与原分录相同, 合计 85,840.00, 代表被作废或修改过的交易. 原分录加冲回共 130 笔, 剩下 238 笔有效分录, 675 行.
  - Account Code 是文本, 带前导零 (如 `090`).

**trial_balance_2026-06-30.xlsx 与 trial_balance_2026-11-30.xlsx**
- 列: Account Code, Account, Account Type, Debit - Year to date, Credit - Year to date, 对比列 (列名是日期).
- 0630 表有 6 个科目, 借贷合计都是 12,903.20. 1130 表有 32 个科目, 借贷合计都是 111,754.91.
- 对比列是带符号的净额 (借方为正). 1130 表的对比列与 0630 表逐科目一致, 已验证.
- 坑:
  - 损益类科目给的是本年累计 (1130 表只含 2026-07-01 起), 资产负债类给的是累计余额.
  - 960 Retained Earnings 的 7,912.00 (借方) 是 Xero 自动结转的上年亏损, 没有对应的日记账.
  - 合计行是公式.

**receivable_invoice_detail.xlsx**
- 18 列, 详见 README. 每张单据一个组, 组标题是单据号; 组尾 `Total 单据号` 行是公式 (`=F8` 之类); 第 231 行总计中 G 列写死为 0.
- 状态: Paid 35, Approved 13, Draft 4, Voided 2, Awaiting Approval 1. 开票日 2025-08-16 至 2026-10-17.
- 坑:
  - Invoice Total 在多行单据的每一行都重复出现 (ORC1006, ORC1025, ORC1036, ORC1039), 按行求和会重复计算.
  - 数量乘单价不等于 Net 的有 3 行 (ORC1039, ORC1040, ORC1041), 原因是含税定价, 单价被四舍五入.
  - 金额有时是 int, 有时是 float. Description 里有换行.
  - 贷项通知单的数量为负.
  - 账户代码除一行为 425 外都是 200. GST 都等于 Net 的 10%.

**account_transactions.xlsx**
- 列: Date, Source, Description, Reference, Debit, Credit, Running Balance, Gross, GST.
- 按科目名分组, 没有科目代码列, 要靠科目表的名称反查 (35 组都能唯一对上).
- 坑:
  - 资产负债类有 Opening Balance 和 Closing Balance 行, 损益类没有.
  - Running Balance 是逐行公式, 方向随科目性质变化: 应付等负债类是 `F-E`, 资产类是 `E-F`.
  - 日期范围 2024-06-30 至 2027-06-30. 两端是 4 行 End of Period 结转行 (960, 其中 2027-06-30 那行是 Xero 预估的本年结果).
  - 这张表只列有效交易, 不含作废对. 同一交易的同一科目可能合并成一行 (例如 090: 日记账 197 行, 这里 133 行).
  - Source 分布: Spend Money 199, Receivable Invoice 131, Payable Invoice 128, Receivable Payment 62, Payable Payment 60, Manual Journal 3, Conversion Balance Journal 3, Adjustment 2, 空值 6 (Inventory Opening Balance), 其他略.

**chart_of_accounts.csv**
- 59 行. 表头带 `*` 前缀 (`*Code`). 逗号分隔, 无 BOM.
- 坑: 803 Wage Payables 的 Type 为空. Type 共 22 种, 要映射成系统的 5 类. Balance 列全空.

**contacts.csv**
- 83 个往来单位. 表头 73 列, 但每一数据行只有 53 个字段 (参差行, DictReader 会给出 None). CRLF 换行.
- TaxNumber 只有 Bayside Club 一个: `11 000 111 000`, ABN 校验位不通过 (mod 89 余 34).
- 有一个联系人 "[登录账号联系人]" 带用户本人邮箱, 而 Posted By 也是 "[登录账号联系人]". 两者都来自 Demo 登录账号. 转换数据或发给 LLM 前必须换成化名.

## 2. 与系统数据表逐字段映射

| 表.字段 | Xero 来源 | 状态 |
|---|---|---|
| accounts.code, name | chart_of_accounts `*Code`, `*Name` | 直接对应 |
| accounts.type | 按 `*Type` 归类: Bank, Current Asset, Fixed Asset, Accounts Receivable, Inventory 归资产; Current Liability, Accounts Payable, GST, Historical, Rounding, Unpaid Expense Claims, Tracking 归负债; Equity, Retained Earnings 归权益; Revenue, Other Income 归收入; Expense, Direct Costs, Depreciation 及三个汇兑类型归费用 | 推导. 803 的 Type 为空, 要人工指定 |
| trial_balance (current, prior) | current 取期末试算表 (导出的或按日记账推导的), prior 取 0630 表; balance = 借 - 贷 | 推导. 注意 prior 的损益是 FY26 全年, 资产负债是 06-30 余额 |
| journal_entries.id, entry_no | Journal ID, 例如 `XJ-389`, 行号 `XJ-389-1` | 推导 |
| posting_date | Date | 直接对应 |
| created_at | Posted Date, 按 UTC 加 `Z` | 有字段, 但值是 Demo 生成的 (见第 3 节) |
| created_by | Posted By, 只有 `[登录账号联系人]` 和 `System Generated` 两个值 | 有, 需要换成化名 |
| approved_by | 无 | 缺. JE-R04 应走 not_executable |
| account_code, debit, credit | 对应列 ×100 | 直接对应 |
| description | 组标题里的摘要 | 直接对应 |
| source_doc | 无. 能从科目明细的 Reference 关联得到: 按 (日期, 科目, 借, 贷) 关联, 238 笔有效分录中 232 笔能关联 | 推导, 而且不是一一对应 |
| is_adjustment | 无 | 全部为 false |
| (Source 类型) | 科目明细的 Source 列 | 现有表结构没有这一列 |
| sales_invoices | 单据号, Contact, Invoice Date; posting_date 取 Invoice Date; amount 取 Net 或 Gross (待定); journal_entry_id 按单位, 日期, 金额关联到收入贷方行 | 推导. customer_abn 基本为空, contract_id 为空 |
| delivery_notes | 无 | 缺. 而且以服务为主: Support-M 19 行, Train-MS 10 行; 实物只有高尔夫球 3 行 |
| bank_accounts | 090, 091 两个科目 | bank_name, bsb, account_no 只能补造 |
| bank_transactions | 科目明细里 090 的 133 行和 091 的 5 行 | 推导. 这只是账面一侧, 不是银行对账单 |
| client_profile, registry, planning | 无. Demo Company 的 ABN 没有导出 | 全部要补造 |

## 3. 适合用 Xero 测的程序与实测数字

**FIN-12 财报与账勾稽 (最适合)**
- 日记账按科目汇总后, 与 0630, 1130 两份试算表逐科目一致, 差异为 0. 唯一例外是 960 的结转 7,912.00, 它没有日记账.
- 收入: Paid 加 Approved 单据记到 200 的 Net 合计, FY27 为 59,944.32, 等于 1130 表的 200; FY26 为 4,200.00, 等于 0630 表的 200.
- 应收: Approved 单据合计 25,906.55, 减去 ORC1002 的部分收款 850.00, 得 25,056.55, 等于 610.
- 资产负债类各科目在科目明细里的 Closing Balance 都与 1130 表一致.
- 缺: 没有导出 P&L 和 Balance Sheet 报表. 财报一侧只能由系统自己生成, 或者请用户再导出这两张表.

**GEN-05 全量分录扫描** (以 238 笔有效分录为基础)
- **周末过账**
  - 按 Date: 79 笔 (周六 28, 周日 51); 368 笔全量里是 143 笔.
  - JE-R01 (周末, 收入贷方, 金额 ≥ 300 元): 按 Date 有 8 行; 按 Posted Date 换算悉尼时间有 38 行, 而金额 ≥ 300 的收入行一共才 42 行.
  - 原因: 173 笔是在悉尼时间的周日集中过账的, 这是 Demo 重置留下的痕迹, 不是真实行为.
- **Posted Date 相对 Date 的滞后** (悉尼时间, 天): 0 天 3 笔, 1-7 天 50 笔, 8-30 天 69 笔, 31-90 天 101 笔, 超过 90 天 14 笔, 负值 1 笔 (ID 734).
  - 17 笔的 Posted Date 晚于导出日 10-18, 最晚到 2026-11-27.
  - 发票一侧: 36 张已付款单据中, 32 张的 Last Payment Date 早于 Created Date.
  - **结论: Posted Date 和 Created Date 都是 Demo 生成的. 用它们的规则能跑出数字, 但命中不能当成真实信号.**
- **期后补记上一财年**: 8 笔 FY26 日期的分录 (ID 389-396) 都在 2026-10-18 过账. 规则逻辑上能测, 但同样是 Demo 痕迹.
- **整数金额** (只看有效分录)
  - JE-R02 按规格 SQL 写死的 10,000 元整数倍: 0 行.
  - 1,000 元整数倍: 4 行 (ID 389 和 392 的 453 科目各 4,000; ID 437 的收入 1,000; ID 548 的应付 2,000).
  - 100 元整数倍: 85 行, 涉及 72 笔.
  - 要测出命中, 需要把整数门槛改成参数.
- **期末附近调整与冲销**
  - 06-30, 10-31, 11-30 前后 3 天都没有分录. 09-30 前 3 天有 8 笔, 后 3 天有 9 笔. 任何月末前 3 天内都没有冲回分录.
  - 另一个坑: Postgres 的 `LIKE '%reverse%'` 区分大小写, 匹配不到 `Reversed`, `Reversal`, 需要改用 ILIKE.
- **过账人**
  - [登录账号联系人]: 343 笔 (952 行).
  - System Generated: 25 笔 (65 行), 日期 09-28 至 10-17, 来源是银行规则自动入账 (Spend Money 14, Payable Payment 5, Receivable Payment 5, Payable Invoice 1). 都没有碰收入科目 200.
  - 只有两个值, 所以"非常规过账人"只能测"系统账户入了哪些科目".
- **手工分录与系统来源** (用 Source 区分)
  - Manual Journal 1 笔: #499 (ID 499, 2026-08-16), "Being depreciation on office equipment". 分录是借 710 750.00, 借 820 75.00, 贷 711 825.00. 这是**真实的错记**: 应借 416 Depreciation, 但 416 没有余额. 结果是费用少记 825, 710 多记 750, GST 多记 75.
  - Conversion Balance Journal 1 笔: ID 397, 2026-07-16, 贷 840 19,212.21.
  - Adjustment 1 笔: ID 539, 0.09 元记入 860.
  - Inventory Opening Balance 1 笔: ID 756, Source 为空, 6 行净额为零.
- **其他可测异常**
  - 疑似重复: 同日, 同单位, 同金额的 2 组, Hamilton Smith 550 (07-17) 和 Bank West 550 (07-31). 两组后来都开了贷项通知单: CR1003 写明 "Duplicate invoice sent in error", CR1005 写明并入 ORC1006.
  - 费用报销收款人与报销人不一致: 报销单写 "Odette Grainger" 296.95, 2026-09-16 付款对象是 "Odette Garrison" (工资收款人) 296.95.
  - 877 Tracking Transfers 有 16 行, 净额为零.
- **JE-R03 与 JE-R06**: 没有审批限额, 也没有调整标志, 只能在测试里注入数据才能测.

**GEN-02 分录例外归因**
- 上面的异常大多能在数据里找到解释依据:
  - 重复发票: 贷项通知单的说明.
  - #499: 科目性质本身说明问题.
  - Grainger 与 Garrison: 科目明细里两种写法都有.
  - 作废对: 摘要里的 "Reversal of ID n".
- 可用的上下文: 分录摘要, 科目明细的 Source, Description, Reference, 发票行的 Description.
- 缺: 没有标准答案, 需要人工给几条预期归因.

**PLN-07 初步分析** (current 取 1130 表, prior 取 0630 表)
- 按 Numbat 参数 (变动额至少 100,000.00): 0 命中.
- 门槛改为 1,000.00 加 20%: 20 命中. 改为 500.00: 24 命中.
- 上期几乎全为零, 所以几乎每个科目都算"新增", 分析价值弱.
- A02 比率要把 `revenue_account_codes` 改成 `["200"]`, 销售成本改成 `["300","310"]`. 结果: 收入 59,944.32, 毛利率 98.7%. PLN-01, PLN-07 的 YAML 里写死了 `"4100"`, 需要按数据集覆盖参数.
- 按日记账做月度分析, 收入/费用: 7 月 4,600.00/3,311.17, 8 月 18,036.00/24,418.70, 9 月 9,795.00/17,928.09, 10 月 27,610.37/17,220.95. 工资 8, 9, 10 月分别为 20,100, 13,400, 6,700.

**FIN-15 期末总体复核与 GEN-04 持续经营** (这里的指标是真实的)
- 工资 40,200.00, 但 478 Superannuation 和 826 Superannuation Payable 都没有余额. 按 12% 估算, 少记约 4,824 (估算).
- 825 PAYG 代扣 9,042.00 一直没有缴纳.
- 090 透支 -12,023.26. 日内最低是 2026-10-15 的 -18,493.96.
- 840 Historical Adjustment 19,212.21 一直没有清理.
- 五个月亏损 2,837.54, FY26 亏损 7,912.00.
- 资产 28,106.56, 负债 38,856.10 (840 计入负债), 净资产 -10,749.54. 流动比率约 0.63.

**GEN-03 关联方** (只能部分测)
- 往来单位 83 个. 同时是客户和供应商的: Bayside Club, DIISR. 没有交易的: 22 个.
- 共用电话 `0800 4389376`: DIISR, Melrose Parking, PC Complete.
- 共用银行户名 `RBC`: ATO, Bankrock Station. 信号很弱.
- 联系人 "[登录账号联系人]" 与过账人同名. 这是 Demo 痕迹.
- 没有董事, 股东, 持股结构. GEN-08, GEN-01 需要的 key_personnel 和 ownership 只能补造.

**REV-08 与 REV-02** (没有发运单时)
- R01 无法执行.
- R02 的发运单表只有表头时, 会命中窗口内的全部发票. 以期末 09-30, 窗口 15 或 20 天计, 是 7 张, Net 合计 8,945.00, 都开于 09-15 至 09-17.
- R03 可以测:
  - 090 有 31 笔收款, 其中 27 笔的 Reference 含发票号.
  - ORC1002 实收 850.00, 发票 1,100.00, 是真实命中.
  - **坑**: 收款是含税金额. 如果 amount 取 Net, 每张发票都会因为 GST 差额误报.
- REV-02 没有合同和发运证据, 适合测 R59 的退路: 预期走 unattributed 或 failed.
- 其他可测点:
  - 发票号缺 ORC1043.
  - ORC1025 已作废, 同时存在号码相近的 ORC01025 (6,187.50, Approved).
  - ORC1024 的到期日早于开票日.
  - ORC1037 是贷项通知单, 但用了发票号段.

## 4. 财年问题

| 做法 | 好处 | 问题 |
|---|---|---|
| A. 期间设为 2026-07-01 至 2026-11-30 | 直接用导出的 1130 表 | 数据其实在 10-17 就结束了, 期末前 5 到 20 天的窗口里 0 张发票, 也没有期后数据, 截止性和期后程序全部是空跑; 本期 5 个月, 上期 12 个月 |
| **B. 期间设为 2026-07-01 至 2026-09-30 (推荐)** | 日期是真实的, 不改数据. 期后 10-01 至 10-17 有 145 笔分录, 16 张发票, 11 笔应收收款 (15,417.60), 能测截止性和期后. 推导出的 0930 试算表有 26 个科目, 合计为 0, 而推导方法已用两份导出表验证 | 试算表要推导, 960 要补上 7,912 的结转; 只有一个季度 |
| C. 日期平移 -91 天 (13 周, 保持星期几不变), 让期末对应原 09-29 映射成 06-30 | 和 Numbat 的财年及配置一致 | 改动了数据: 摘要里的月份, 到期日, Posted Date 都要一起改; 原 FY26 的分录会混进来; 削弱了"真实数据"的意义 |
| D. 保持 FY26 不动 | 不用处理 | 只有 8 笔分录, 6 个科目, 只能测"数据极少"的情况 |

**系统层面的限制** (用 Xero 前要处理):
- `PostgresFinancialDataStore.gl_balance` (`src/backend/persistence/stores.py` 第 97 行) 把留存收益科目写死成 `"3200"`. 对 960 会算错, 不过现有程序只对银行科目调用它.
- `main.py` 第 44 行把 registry 和函证 mock 写死在 `data/demo`.
- PLN-01, PLN-07 的收入科目写死为 `4100`.

## 5. 转换方案

- **建议: 由 A 写测试工具, 运行时转换**, 例如 `tests/tools/xero_dataset.py`.
  - pytest fixture 在 `data/external/xero/raw` 不存在时 skip.
  - 存在时转换到 tmp_path, 生成 `xero/` 下 7 份 CSV, `client_profile.json`, `planning/`, `registry/`, 然后用 `CsvDatasetLoader(root=tmp_path)` 载入.
  - 另提供 CLI, 需要时输出到 `data/xero/`. 这个目录会被 `/data/*` 忽略, 而且是单级目录, 能通过 `safe_child` 的检查. `data/external/xero/dataset` 是多级路径, 会被 `safe_child` 拒绝.
- **规模**:
  - 行数: accounts 59, trial_balance 约 26 加 6, journal_entries 1017 (只取有效分录是 675), sales_invoices 48 (Paid 加 Approved, 含 6 张负数贷项通知单), delivery_notes 0 (只有表头, 文件必须存在, 否则 loader 读文件会报错), bank_accounts 2, bank_transactions 138.
  - 脚本约 300 行, 结果确定.
- **归属**: 放在 A 的测试工具里, 不放进 B 的 `data/`. B 的 `data/generate_demo.py` 只负责 Numbat.
- **风险**:
  1. Demo 每 28 天重置. 重新导出会改变日期和金额, 所以测试的预期值应该由转换器从原始数据算出来, 或者用 sha256 锁定这份快照.
  2. openpyxl 不在项目依赖里. 可以加依赖, 也可以用 zipfile 直接解析 XML.
  3. 时间戳字段是 Demo 生成的, 时区是推断的.
  4. 用户的真实姓名和邮箱要换成化名.
  5. 要处理第 4 节列出的写死科目与路径问题 (`3200`, `data/demo`, `4100`).
  6. 银行流水只是账面一侧, CSH-05 的比对必然一致, 没有检验意义.
  7. 需要定几个口径:
     - sales_invoices 的金额取 Net 还是 Gross.
     - 冲回对是否保留.
     - 是否给 journal_entries 加 Source 列 (这是 B 的表结构改动).
     - 是否换成 Xero 的科目代码.