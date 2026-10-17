# Xero Demo Company (AU)

- 来源: Xero 网页版 Demo Company (AU), 用免费开发者登录 (https://www.xero.com/signup/developers/) 进入, 2026-10-17 从报表页与列表页手动导出.
- 性质: Xero 预置的虚构公司数据, 不是真实客户数据. Demo 每 28 天重置一次, 日期按重置当天平移, 所以这里是一次性快照, 重新导出时日期和金额会变.
- 条款: Xero 提醒不要往 Demo Company 写入客户数据; 这里只读导出, 未修改.
- 本目录被 `.gitignore` 的 `/data/*` 忽略, 不入库.

## 文件 (`raw/`)

| 文件 | Xero 报表 | 内容 |
|---|---|---|
| `journal_report.xlsx` | Journal Report, 2024-01-01 至 2027-12-31 | 全部日记账: 368 笔, 1017 行明细 (Journal ID 最大 756, 编号不连续; 页面上的 "1754 items" 含标题与合计行), 日期 2025-07-16 至 2026-11-25. 列: Date, Journal ID, Account Code, Account, Debit, Credit, Posted Date, Posted By. 按 Journal ID 分组, 组标题行是 "ID n 摘要" |
| `trial_balance_2026-06-30.xlsx` | Trial Balance, 截至 2026-06-30, 对比 2025-06-30 | 只有 6 个科目 (销售 4,200), 2025-26 财年数据很少 |
| `trial_balance_2026-11-30.xlsx` | Trial Balance, 截至 2026-11-30, 对比 2026-06-30 | 32 个科目, 覆盖全部数据 |
| `receivable_invoice_detail.xlsx` | Receivable Invoice Detail, 全部日期 | 171 行发票与贷项通知单明细. 列含 Invoice Date, Source, Reference, Item Code, Description, Quantity, Unit Price, GST, Gross, Invoice Total, Net, Contact, Due Date, Last Payment Date, Created Date, Status, Account Code. 按发票号分组 |
| `account_transactions.xlsx` | Account Transactions, 全部 60 个科目, 全部日期 | 773 笔账面明细, 按科目分组, 含 Source, Description, Reference, 期初余额与滚动余额 (公式) |
| `chart_of_accounts.csv` | 科目表导出 | 59 个科目: Code, Name, Type, Tax Code |
| `contacts.csv` | 往来单位导出 | 83 个 (客户 23, 供应商 24 等); TaxNumber (ABN) 只有 1 个非空 |
| `journal_report_full.xlsx` | Journal Report, 全部日期, 加选 Narration 与 Description 两列 | 付费 Journals 接口的模拟来源 (368 笔, 1017 行) |
| `bank_statement_090.xlsx`, `bank_statement_091.xlsx` | Bank Reconciliation 报表包里加 Bank Statement, 全部日期 | 付费 Bank Statements Plus 的模拟来源 (090 有 147 行, 091 有 4 行, 带勾对状态与余额) |

## 与系统所需数据的差距

- 数据量: 主要业务在 2026-07 至 2026-11, 不是完整财年; 2025-26 财年 (截至 6 月 30 日) 几乎为空.
- 缺发运单 (签收日, 签收人), 三单匹配与截止性程序缺一单.
- 银行只有账面一侧 (科目 090, 091 的明细), 没有银行对账单行, 流水账面核对需另造.
- 没有合同与函证回函 PDF; 合同可用 `../cuad/`.
- 客户 ABN 基本为空; Demo Company 自身 ABN 见 API 的 organisation.json (RegistrationNumber 11111111138).
- Excel 合计与滚动余额是公式 (`=SUM(...)`), 读取时按值重算, 不要信任缓存值.

## API 快照 (`api/`)

`uv run python -m backend.adapters.xero pull --period-start 2026-07-01 --period-end 2026-11-30 --prior-end 2026-06-30` 拉取, 每次一个新目录 (已存在则拒绝, 先写到同级的 .{名}.partial 再改名). 授权: `login` 只申请免费 scope; 应用获准付费接口后用 `login --journals --bank-statements` 重新授权. 2026-10-17 的快照 `api/20261017/`: 16 次请求约 25 秒; 组织 1, 科目 62 (含 3 个已归档), 往来 83, 发票 92 (应收 49, 应付 43), 贷项通知单 8, 收付款 71, 银行收支 88, 银行转账 1, 手工分录 4, 试算表 2 份. Journals 返回 401: 免费档应用申请 `accounting.journals.read` 会被授权页拒绝 (invalid_scope), 不带这个 scope 才能授权.

## 核对结果 (`compare.py`)

`uv run --with openpyxl python -I data/external/xero/compare.py data/external/xero/api/20261017`

| 项目 | 结果 |
|---|---|
| 科目表 | API 多 473, 620, 900 三个已归档科目 (网页导出不含归档), 其余一致 |
| 往来单位 | 83 = 83 |
| 试算表 2026-06-30, 2026-11-30 | 6 个, 32 个科目逐一相符 |
| 应收发票与贷项通知单 | 55 = 55, 每张金额相符 (含草稿, 待审, 作废) |
| 重建分录按科目净额 vs 科目明细账 | 223 笔重建分录; 除 8 个科目外全部相符, 差额正好等于 API 拿不到的来源: 期初转换余额 19,212.21 (840 与两个银行科目), 年末结转 10,749.54 (960), 费用报销 305.15 (420, 493, 820 及银行付款), 分位调整 0.09 (860) |
| 日记账笔数 | 报表 368 笔含作废单据的原分录与冲回; 重建只含生效单据, 223 笔 |

## 付费接口模拟快照 (`api/20261017-mock/`)

```
uv run python -m backend.adapters.xero pull --out data/external/xero/api/20261017-mock \
  --period-start 2026-07-01 --period-end 2026-11-30 --prior-end 2026-06-30 --paid mock
```

免费接口照常调 Xero; Journals 与 Bank Statements Plus 由 `adapters/xero_mock.MockPaidTransport` 截下, 用上面三个网页导出按官方结构作答.
- journals 368 笔: 先按科目与借贷方向推出单据类型, 再在同类单据里按日期, 金额与往来名称匹配 (286 笔带 SourceID); 14 笔没有来源: 期初转换余额, 对账调整, 存货期初, 跟踪类别重分类 (Tracking Transfers) 及一笔手工分录的冲回.
- bank_statements: 本期 (截至今天 2026-10-17) 090 有 142 行, 091 有 4 行.
- 作为数据集 `xero/20261017-mock` 载入: 科目 62 (只有 855 Clearing Account 未映射), 试算表 38 行, 分录 1017 行, 销售发票 42 (全部关联到分录),
  发运单 0, 银行账户 2, 银行流水 146; 生产的 gl_balance (上期试算表 + 本期分录) 对每个科目都等于本期试算表 (慢测试 test_xero_snapshot_loads_and_ties_to_trial_balance).
- 全部规则 SQL 在该数据上可跑; 流水账面核对报 090 差 13,472.70 (对账单只到今天, 总账含未来日期与未勾对项), 091 一致.
