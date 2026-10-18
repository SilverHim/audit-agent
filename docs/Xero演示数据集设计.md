# Xero 演示数据集设计

本文是演示公司 Xero Demo Company (AU) 的补造数据与各程序预期结果的定稿, 给实现者看, 结构仿 `docs/合成数据集设计.md`. 由 `docs/云端开发/design/xero/设计稿-v2.md` 定稿而来: **设计稿 v2.1 第 n 节即本文第 n 节**; 设计过程 (核查与修订) 只留在设计稿. 领域对象与表的字段定义见 `docs/开发接口文档.md` 第 2, 9 节. "决定 n" 指 `docs/云端开发/计划-第1至3级.md` 第 1 节的编号. 实现与验收的需求见 `docs/云端开发/需求清单.md` R62 起.

**通用口径** (各节不另说明时适用)
- 数据: Xero Demo Company (AU) 2026-10-17 的快照. 现有 `api/20261017-mock` (期间 07-01 至 11-30) 保持原样, 留给慢测试; 本数据集用新快照 `api/20261017-q1` (期间 07-01 至 09-30, 拉取方法见第 9.2 节). 所有对象 ID 与金额两份快照相同 (前提见第 9.2 节).
- 本期 2026-07-01 至 09-30. 上期 2025-07-01 至 2026-06-30. 期后 10-01 至 10-17 (快照日, 也是对账单最后一天).
- 分录 368 笔 1017 行, 含 65 对作废对; 本期 215 笔 582 行, 含 33 对作废对. 作废对同日逐行镜像, 不影响任何时点的余额.
- 金额单位为元, 借方记正. 损益类不含税, 资产负债类与银行行含税. 规则输出的金额单位为分.
- 重要性: 收入 1%, 执行重要性取 50% (决定 10: 审计师在闸门可选 50% 至 75%, 演示与测试取 50%), 明显微小取 5%, 即 32431 / 16216 / 1622 (分).
- 人名一律用化名. 登录账号的真实姓名与邮箱不写进任何设计, 补造文件与测试数据 (决定 12).
- 合成文件一律标 "Synthetic – for demonstration only" (决定 6).

---

## 0 要点

1. **Xero 原始数据一律不改.** 快照原样保存; 补造的东西全部放在独立目录 `data/xero_demo/20261017-q1/`, 逐个标明合成 (决定 6). 要验证系统能否识别错报, 只在测试临时目录里复制快照与补造文件再注入错误, 跑完核对原文件哈希不变 (第 8 节).
2. **演示数据里没有收入截止错报.** ORC1026 (DIISR 开发 6,500) 9 月照 Xero 原样确认, 补造合同写 "服务完成即转移", 服务完成记录写 09-17 (决定 8). 原来的 6,500 跨期场景改为注入测试 T1. 上一版的 M-1 (6,500), 由它引出的 PLN-19 重估 (25,931), 报告批注里的数字全部删除.
3. **演示错报 5 条, 管理层全部更正.** 4 条来自真实数据中的问题: #499 折旧分录 750 (GEN-02 发现), 超级年金 4,261.20, 应计工资 2,010.00, 年假 2,687.36 (FIN-15 发现); 1 条来自补造回函 (决定 15): 储蓄户 09-30 应计利息 48.53 账面未计提 (CSH-02 新规则 D04 发现, M-5). 毛额合计 9,757.09, 对损益影响 -8,910.03. 更正后本期亏损 22,136.99, 净负债 30,048.99; 出无保留意见加持续经营重大不确定性段与其他事项段 (决定 8).
4. **重要性全程不变.** 5 笔更正都不碰收入分类的科目 (AJE-X05 贷记的 270 属 other_income), PLN-19 两次重估的变动率都是 0, 不出例外, 计划不出新版本.
5. **现有程序改成通用写法, 跟公司有关的放数据与参数** (决定 9, 第 5.1 节). 合同与截止提示词由 "货物" 改为 "商品或服务", 转移事件按合同约定; REV-08 R03 只对 "字母前缀加纯数字" 的发票号补一条数字整词匹配 (Numbat 结果不变, 已复算); PLN-07 改为资产负债类对期初比较, 门槛按收入比例, 另加月度趋势 (Numbat 仍只命中 1100, 已复算); REV-02 上下文加入发票对应的分录行. 新程序里原先为 Numbat 写死的口径 (GEN-07 的参考号前缀, FIN-09 与 FIN-15 的科目代码, FIN-09 V01 的调整标志, GEN-03 按客户 ABN 取交易与 3xxx 权益科目) 一并改为按分类, 名称规范化或数据集参数.
6. **数据集声明文件 `dataset_traits.json`** 承载跟数据来源有关的事实: 录入时间不可靠, 作废冲回的说明格式, 数据截止日, 上期起始日, 薪酬科目对照. 没有这个文件的数据集 (Numbat) 按默认值运行.
7. **全量分录扫描 (GEN-05) 已定** (决定 13, 用户: "测试数据加上不可靠跳过, 真实数据应该需要检查"). 数据集声明录入时间不可靠时 (Xero 演示与测试数据), 看录入时间的 4 类规则自动跳过; 真实客户数据照常执行. 另加 4 条不看时间的通用规则 R09 至 R12 (手工非标准分录, 全额贷项冲销, 作废后重录且名称改变, 疑似重复开票), 共 7 条命中 (第 5.3 节).
8. **银行 09-30 余额账实完全相符**, CSH-02 在演示数据上不出余额差额; 在途款项场景放进注入测试 T4. 储蓄户回函载明 09-30 应计利息 48.53 (决定 15), 账面未计提, CSH-02 新规则 D04 出 1 条例外, 判断认定错报 M-5 (第 5.6, 6.5 节).
9. **剧本最终计数** (第 7.3 节): 例外 41, 判断 56 (另有 XJ-575 改判产生的人工判断 1 条), 复核任务 53, 闸门决定 10 (ACC-08 单列一次, 依 G5), 人工队列 4, 更正通知 2, 错报 5 (全部 corrected), 大模型调用约 66 次 (不含 A7 独立复核; 加 A7 约 84 次, 第 7.3 节), 文件 29 份.
10. **新快照已拉取** (计划第 0 节): `data/external/xero/api/20261017-q1/` 已在仓库中 (期间 07-01 至 09-30, 对账单到 10-17). 载入前仍按第 9.2 节逐文件比对 20261017-mock, 确认 Demo 没有在两次拉取之间重置.

---

## 1 文件布局与来源

```
data/
├── external/xero/                       Xero 原始数据, 一律不改; 本仓库已用 git add -f 入库 (其他检出可能没有)
│   ├── raw/                             10-17 网页导出 (含 Posted By 列, 只供 mock 拉取读)
│   └── api/
│       ├── 20261017/                    免费接口快照
│       ├── 20261017-mock/               期间 07-01 至 11-30, 留给慢测试, 不动
│       └── 20261017-q1/                 新拉: 期间 07-01 至 09-30, 对账单取到 10-17 (第 9.2 节)
└── xero_demo/
    └── 20261017-q1/                     补造文件, 与快照目录同名; 全部合成, 入库 (第 9 节)
```

**许可与去标识**
- Xero 条款提醒不要往 Demo Company 写入客户数据. 我们只读导出, 没有写入.
- 本仓库 (个人仓库 audit-agent) 已用 `git add -f` 带上 `data/external/xero/` (含快照与网页导出); 其他检出 (如悉尼大学仓库) 可能没有, 测试保留 "没有本地快照就跳过" 的守卫 (第 8.1 节). Xero 原始数据能否随公开仓库再分发没有条款原文 (待核 T1). 合成文件不含 Xero 原始记录的整份拷贝, 只按发票号, 科目号, 分录号引用, 入库.
- 登录账号联系人的姓名与邮箱出现在 (已逐文件核对, 只输出命中与否): 两个 api 快照的 contacts.json (姓名与邮箱), raw/contacts.csv (姓名与邮箱), raw/journal_report.xlsx 与 raw/journal_report_full.xlsx (姓名, 即 Posted By 列). 其余 api 文件与 raw 文件都没有. 拉取时不处理 (决定 12).
- 生成脚本与测试副本的读取白名单: 只读 api 快照的 accounts, invoices, credit_notes, journals, bank_statements, organisation, 试算表 (与 dataset.json); 一律不读 raw/ 目录, 不读 contacts.json. 测试副本与派生快照里的 contacts.json 只写过滤版 (去掉登录账号联系人, 第 8.1, 9.2 节). 校验脚本断言原值不出现在任何生成文件里 (第 9.4 节).
- 客户, 供应商与 3 名员工 (Odette Garrison, Oliver Gray, Tracy Green) 是 Xero 预置的虚构名字, 保留.
- DIISR 是已撤销的真实联邦部门名: 补造合同保留 Xero 原名, 不写 ABN, 每页页眉标明虚构 (决定 11).
- Numbat (`data/demo`) 保留为离线回归测试, 不改.

---

## 2 被审计单位, 人员与审计依据

### 2.1 主体

| 项 | 值 | 来源 |
|---|---|---|
| 法定名称 | Demo Company (AU) Pty Ltd | Xero LegalName "Demo Company (AU)" 加 s148(2) 要求的后缀 |
| ACN / ABN | 900 043 021 / 25 900 043 021 | 合成, 两者都通过校验位; Xero 的 RegistrationNumber 11111111138 由画像覆盖 |
| 类型 | Australian Proprietary Company, Limited by Shares; 注册日 2018-09-03 | 合成; 须早于 2025-07-01, 因为 s323D(2A) 只适用于首个财年之后的财年 |
| 地址 | 23 Main Street, Marineville NSW 2000 | `organisation.json` |
| 业务 | IT 支持月费, Office 培训, 项目管理, 软件开发; 员工 3 人 | Xero 发票的物品代码; ANZSIC 7000 待核 |
| 小型私人公司 | 远低于 s45A(2) 的任一口径 | 条例现行数额待核 |
| 适用框架 | Australian Accounting Standards - Simplified Disclosures (第 2 层, AASB 1060), 依据 AASB 1053 第 13(a) 段; AASB 1060 段号暂缓 (待 AASB 1060 正本) | 决定 3 |

### 2.2 人员, 股权与关联方

| 人或主体 | 设定 | 对应的 Xero 记录 |
|---|---|---|
| Alex Morgan (化名, 出生 1979-04-12) | 唯一董事兼公司秘书, 2018-09-03 起; 持 85 股 (85%); 记账人; 董事借款 19,212.21 的出借人; 090 透支的个人担保人 | 科目 880, 881, 970 名称里的 "Owner A" |
| Sam Whitfield (出生 1983-09-30) | 持 15 股 (15%), 不任职; 2026-06-15 从 Alex 处受让; 依 s293 指示公司编制财报并审计 | 无 |
| Dana Morgan (出生 1981-11-03) | Alex 的配偶; Victoria Property Agency 的董事 | 无 |
| Victoria Property Agency Pty Ltd (VPA) | ACN 900 043 030, ABN 27 900 043 030, 2014-03-03 注册; Dana 与 Alex 各持 50 股, 都任董事; 原为 23 Main Street 的业主, 2026-09-30 售出; **管理层没有申报为关联方** | 往来单位 VPA; XJ-420, 518, 593 |
| Morgan Advisory Pty Ltd | ACN 900 043 049, ABN 17 900 043 049, 2019-07-01 注册; Alex 是董事兼唯一股东; 已申报, 本期没有交易 | 无, 作对照 |
| Harbourview Nominees Pty Ltd | 物业买方, 与本公司无关, 不进关系图 | 无 |
| Tessellate Audit Partners / Jordan Ellis | 沿用 Numbat 的虚构事务所; Jordan Ellis 是合伙人 (Registered Company Auditor No. 300471) | 无 |
| Wattlebrook Bank Limited | 虚构银行, ABN 82 938 074 116 (校验通过); 090 与 091 两户都在这家行, BSB 306234 沿用 Xero | Xero 对账单与摘要里的 "NAB", "National Australia Bank", "Ridgeway Savings", "Business Savings Bank" 视为 Demo 的占位银行名 (第 2.6 节画像 notes 与生成规格 notes 写明) |

**股本**: 100 股普通股, 每股 0.01 元, 已缴足, 共 1.00 元, 注册时由 Alex 缴入, 包含在 840 的转换余额里. 1.00 低于明显微小 16.22, 不计入错报.

**申报的关联方**: Alex Morgan (董事借款, 透支担保) 与 Morgan Advisory. 故意不列 VPA.

Sam 持股 15%, 低于 AASB 128 第 5 段 20% 的推定线. 第 2 层下他是否属于关联方看 AASB 1060 (段号暂缓, 待 AASB 1060 正本).

### 2.3 时间线

| 日期 | 事件 | 文件 |
|---|---|---|
| 2018-09-03 | 公司注册; Alex 任董事兼秘书 | 登记摘录 |
| 2018-09-10 | Alex 与公司签董事借款协议: 无息, 无担保, 随时可要求偿还 (决定 8) | 借款协议 |
| 2026-06-15 | Alex 向 Sam 转让 15 股; 股东协议要求按截至 9-30 的年度出具经审计财报 | 画像 notes |
| 2026-06-25 | VPA 租金函: 自 07-01 起按自然月计租, 每月 1,500 加 GST, 21 日付当月租金, 任一方提前 1 个月通知终止 | related_party |
| 2026-07-16 | Xero 录入转换余额 (XJ-397) | 真实数据 |
| 2026-07-20 | 3 名员工入职; 双周发薪, 首期 07-20 至 08-02 | 工资册 |
| 2026-08-14 (五) | 唯一董事决议 (s248B(1)): 依 s323D(2A), 自 07-01 起的财年于 09-30 结束 | appointment.pdf 第 1 页 |
| 2026-08-20 (四) | Sam 依 s293 发出指示, 要求编制财报与董事报告并审计 | 第 2 页 |
| 2026-08-25 (二) | Tessellate 依 s328A 出具同意书 | 第 3 页 |
| 2026-08-27 (四) | 唯一董事决议, 依 s325(1) 委任审计师 | 第 4 页 |
| 2026-08-28 (五) | 约定书发出; **VPA 发出出售与终止通知, 安排于 09-30 终止 (提前 1 个月以上)** | engagement_letter.pdf; related_party |
| 2026-09-01 | DIISR SOW No.2 签订 (CON-X01) | contracts |
| 2026-09-02 | 董事签回约定书 | engagement_letter.pdf |
| 2026-09-03 至 09-16 | DIISR 模块开发 10 个人日 | 服务完成记录 |
| 2026-09-17 | DIISR Project Sponsor 签完工证书, 当日开出 ORC1026 | SR-ORC1026; 真实发票 |
| 2026-09-22 | 与 Harbourview 签新租约 (Truxton 代管, 2026-10-01 至 2028-09-30) | related_party |
| 2026-09-30 | 缩短财年结束; VPA 物业交割, 原安排终止 | — |
| 2026-10-06 | Ridgeway University 订单 CRM08-12 的 10 月驻场项目管理开票 (ORC01025) | 订单 |
| 2026-10-08 / 10-11 | Jakaranda Maple Systems 报价要求 2,000 订金 / 订金到账 | 报价单 |
| 2026-10-12, 10-15 | Alex 向公司转入 1,000 与 1,500 (对账单 "e-Bank dep", 总账未记) | 董事支持函 |
| 2026-10-13 | DIISR 与公司商定 1,050 的让步 (见第 6.4 节, 决定 14 已定: 10 月的价格让步, 不算 9 月错报) | DIISR 邮件 |
| 2026-10-15 | RM2031 分租给 Majestic Contracting, 月租 450 | 分租协议 |
| 2026-10-16 | 1026C 贷项 1,155 (真实); 管理层出持续经营备忘录, 现金流预测, 董事支持函, 董事借款确认函 | 真实数据; 补造 |
| 2026-10-17 | 快照日; 期后数据截止 | — |
| 2026-10-19 (一) | 第 1 轮错报沟通 (M-1, M-5) | 人工队列 |
| 2026-10-20 (二) | 管理层同意; AJE-X01 与 AJE-X05 入账 (录入 2026-10-20T03:00Z) | adjustments |
| 2026-10-21 (三) | FIN-09, PLN-19, FIN-04, FIN-15 第 1 轮; 15:00 第 2 轮错报沟通 (M-2 至 M-4) | 人工队列 |
| 2026-10-22 (四) | 09:00 管理层同意; AJE-X02 至 X04 入账 (录入 2026-10-21T23:30Z, 即悉尼 10-22 10:30); 第 2 轮验证与复核; 财报 v1 编成; FIN-02, FIN-01 闸门; REPORT-1; RPT-07 书面沟通 | 补造 |
| 2026-10-23 (五) | 董事批准财报并签董事声明; 声明书与 s307C 声明; RPT-01 退回一次后签字; 报告日 2026-10-23, 签字注入时间 2026-10-23T05:00Z (悉尼夏令时 16:00) | 补造 |
| 2027-01-31 | 依 s315(4) 向成员报告的截止日 (推算方法待核 T5) | — |
| 2027-09-30 | s293(2)(b) 指示的最晚日, ACC-08 T04 的上限 | — |

悉尼夏令时自 2026-10-04 (10 月第一个周日) 开始, 此后 UTC+11.

### 2.4 审计依据与期间

**法律依据** (公司法 C148 原文已核)
- 缩短的过渡财年: s323D(2A)(a)(b)(c).
- 审计依据: s293(1)(2)(3)(c) 的股东指示; 依 s301(2), 指示没有要求审计时可免审, 本例要求审计.
- 委任审计师: s325(1), s328A(1)(2)(4).

**期间**
- 本期 3 个月, 比较期为 FY26 的 12 个月 (未经审计). FY26 的损益全在 2025-07 至 09 月 (XJ-389, 390, 392).
- ASA 320 A7: 重要性按 3 个月的报告确定, 不年化.

**第 2 层下各准则的适用范围** (原文已核): AASB 101 (第 AusB1 段), AASB 107 (第 AusA1 段), AASB 124 (第 AusB1 段) 不适用于采用 AASB 1060 的主体, 相应列报与披露依据改引 AASB 1060 (段号暂缓, 待 AASB 1060 正本); AASB 110 只有第 13, 16, 17-22 段不适用 (第 AusA1 段); AASB 15 只有第 110-129 段与 B87-B89 不适用 (第 AusE1 段). 所以 GEN-06 引 AASB 110 第 3(b), 10 段与 AASB 15 第 88 段, GEN-03 引 AASB 124 只作参照.

**首次审计**: 期初余额 ASA 510 第 6 段; 上期未审, 报告加其他事项段 ASA 710 第 14 段; 比较数不完全可比的披露引 AASB 1060 (段号暂缓, 待 AASB 1060 正本).

**设置不一致**: Xero 组织的财年末仍设为 6-30, 画像 notes 写明.

### 2.5 登记 mock

```json
// registry/abn.json
{"25900043021": {"abn": "25900043021", "acn": "900043021", "entity_name": "Demo Company (AU) Pty Ltd", "status": "Active", "entity_type": "Australian Private Company"},
 "27900043030": {"abn": "27900043030", "acn": "900043030", "entity_name": "Victoria Property Agency Pty Ltd", "status": "Active", "entity_type": "Australian Private Company"},
 "17900043049": {"abn": "17900043049", "acn": "900043049", "entity_name": "Morgan Advisory Pty Ltd", "status": "Active", "entity_type": "Australian Private Company"}}
// registry/asic.json
{"900043021": {"acn": "900043021", "name": "Demo Company (AU) Pty Ltd", "status": "Registered", "registered_date": "2018-09-03"},
 "900043030": {"acn": "900043030", "name": "Victoria Property Agency Pty Ltd", "status": "Registered", "registered_date": "2014-03-03"},
 "900043049": {"acn": "900043049", "name": "Morgan Advisory Pty Ltd", "status": "Registered", "registered_date": "2019-07-01"}}
```

**摘录与人员检索** (按 G2 data_needs 的格式: 每份一个 JSON 加一个同名 PDF; MockRegistrySource 的 asic_extract, asic_person 读 JSON, 回执带 PDF 原件)

| 文件 | 内容 (JSON 字段同 G2 的 AsicExtract / PersonSearch) | PDF |
|---|---|---|
| `registry/extracts/900043021.json` | Demo: APTY / LMSH / PROP; officeholders [Alex Morgan (1979-04-12) director 2018-09-03, Alex Morgan secretary 2018-09-03]; shareholders [Alex Morgan person ORD 85 pct 0.85, Sam Whitfield person ORD 15 pct 0.15]; 已缴 1.00 | 同名 .pdf, 2 页 (第 1 页公司与高管, 第 2 页股本与股东) |
| `registry/extracts/900043030.json` | VPA: officeholders [Dana Morgan (1981-11-03) director 2014-03-03, Alex Morgan (1979-04-12) director 2014-03-03]; shareholders [Dana Morgan ORD 50 pct 0.5, Alex Morgan ORD 50 pct 0.5] | 同名 .pdf, 2 页 |
| `registry/extracts/900043049.json` | Morgan Advisory: officeholders [Alex Morgan director 2019-07-01]; shareholders [Alex Morgan ORD 100 pct 1.0] | 同名 .pdf, 2 页 |
| `registry/persons/MORGAN-ALEX-19790412.json` | roles [900043021 director 2018-09-03, 900043021 secretary 2018-09-03, 900043021 shareholder 85, 900043030 director 2014-03-03, 900043030 shareholder 50, 900043049 director 2019-07-01, 900043049 shareholder 100] | 同名 .pdf, 1 页 |

ASIC 个人检索是否列持股待核 (T5), 所以设计让 Alex 在三家都任职, 只凭职务也能检出.

**只持股不任职的个人没有出生日期**: 公司摘录的股东栏只有姓名, 而 G2 的人员节点 ID 是 `PER-{姓}-{名}-{yyyymmdd}`. Numbat 的个人股东都兼任董事, 没有暴露这个空档; 本数据集的 Sam Whitfield (以及 T6 的另一股东) 只持股. 节点 ID 按 `PER-{姓}-{名}-NODOB` 计, 例如 PER-WHITFIELD-SAM-NODOB. 第 2.2 节写的 Sam 出生日期只用于补造文件, 不进登记 mock.

### 2.6 客户画像 (覆盖载入器产出)

```json
{"id": "CLIENT-XERO-3C1332BB-Q1", "version": 1, "legal_name": "Demo Company (AU) Pty Ltd", "abn": "25900043021",
 "entity_type": "small_proprietary", "industry_anzsic": "7000", "listed": false, "directors": ["Alex Morgan"],
 "notes": "Synthetic – for demonstration only. Profile built on the Xero Demo Company (AU) data. IT support, training and project services, Marineville NSW; 3 employees from 20 July 2026. Shortened financial year 1 Jul-30 Sep 2026 under s323D(2A); future years end 30 September (the Xero organisation setting still shows 30 June). Audit required by a s293 direction from a 15% shareholder (Sam Whitfield). First audit; FY2026 unaudited. Owner A in the Xero chart of accounts is Alex Morgan (sole director, 85%), who also keeps the books. Both bank accounts are held with Wattlebrook Bank Limited; bank names such as NAB, Ridgeway Savings and Business Savings Bank in the Xero statement lines and narrations are placeholders of the demo data. Related parties declared by management: Alex Morgan (director loan, overdraft guarantee); Morgan Advisory Pty Ltd. Payment approval limit AUD 5,000. No director remuneration in the period."}
```

**画像 ID 带数据集后缀**: 载入器给 20261017-mock 生成的画像也是 CLIENT-XERO-3C1332BB v1, 内容不同; `PostgresEngagementStore.create` 遇到同 ID 同版本而内容不同的画像抛 VersionConflict, 同一个库里先后用两份快照建项目必有一个失败. 所以补造画像用 `CLIENT-XERO-3C1332BB-Q1`, 载入器校验它以快照组织 ID 推出的 `CLIENT-XERO-3C1332BB-` 开头 (第 9.3 节第 3 项). 不选 "库中已有同 ID 就沿用旧版本", 因为那样先建过 mock 项目的库会把通用画像套到演示项目上.

规格书 4.3 节其余字段 (accounting_system, key_personnel, ownership, related_parties, applicable_framework tier2_gpfs, governance all_in_management 为真, approval_limit_cents 500000, fees audit_fee_cents 480000 / non_audit 0 / firm_total_fee_cents 240000000) 由 G2 JR-2 扩展模型后写入; 扩展前写在 notes.

### 2.7 计划预置 (planning/)

保留通用模板的 3 个目标: OBJ-REV-OCC (significant), OBJ-REV-CUT (significant), OBJ-CASH-EXI (high). 只把风险描述换成本数据集的实数:

- **RISK-REV-OCC**: Presumed fraud risk (ASA 240 para 27). Revenue of AUD 32,431 for the three months is 7.7 times the AUD 4,200 recorded for the whole of FY2026; 26 invoices to 14 customers, DIISR 24%; all journals are entered by the sole director.
- **RISK-REV-CUT**: Six invoices totalling AUD 9,000 (excl. GST) were raised on 16-17 September 2026, including a AUD 6,500 development invoice to DIISR (ORC1026) whose service completion falls close to the period end.
- **RISK-CASH-EXI**: The Business Bank Account was overdrawn throughout the quarter (AUD 10,900.44 at 30 September 2026) and the Savings account held AUD 11,581.23; opening bank balances were keyed on 16 July 2026 as a conversion balance of AUD 19,212.21 credited to 840 Historical Adjustment, and the 30 June 2026 comparative omits them.

significant 下 REV-08 与 REV-02 窗口都是 20 天 (09-10 至 09-30), 与 high 的 15 天覆盖同样 6 张发票.

**其余重大科目** (|余额| 不小于 324.31) 写进 PLN-15 的计划叙述, 不进系统目标表 (否则覆盖校验 R60 会挡住闸门): 员工费用 477 及 PAYG 825, 养老金 826; 840 的分类; 设备 710/711; 应收 610; 应付与 GST; 期初余额.

### 2.8 更正分录 (adjustments/journal_entries.csv, 全部合成)

**共同字段**
- 表头与 `journal_entries.csv` 相同; posting_date 2026-09-30, is_adjustment 为真, created_by "xero" (与载入器一致, 录入人写在摘要里), approved_by 为空.
- entry_no 以 `AJE-` 开头 (FIN-09 V01 只取这个前缀; 前缀取自 `dataset_traits.audit_adjustment_entry_prefix`, 默认 "AJE-", 与 Numbat 的 AJE-2026-001 一致). 摘要以 "Audit adjustment AJE-X0n" 开头, 不含 revers 字样.
- source_doc 写对应错报的来源对象 (FIN-09 按来源对象与科目匹配).

| 编号 | 录入时间 (UTC) | 分录 (分) | source_doc | 对应错报 |
|---|---|---|---|---|
| AJE-X01 | 2026-10-20T03:00Z | 借 711 82500, 贷 710 75000, 贷 820 7500. "Audit adjustment AJE-X01: journal #499 removed; no office equipment held at 30 September 2026" | XJ-499 | M-1 |
| AJE-X02 | 2026-10-21T23:30Z | 借 478 426120, 贷 826 426120. 7 至 9 月工资 33,500 与应计工资 2,010 的 SG (12%) | 826 | M-2 |
| AJE-X03 | 2026-10-21T23:30Z | 借 477 201000, 贷 804 201000. 09-28 至 09-30 的应计工资 | 804 | M-3 |
| AJE-X04 | 2026-10-21T23:30Z | 借 477 268736, 贷 805 268736. 年假 | 805 | M-4 |
| AJE-X05 | 2026-10-20T03:00Z | 借 620 4853, 贷 270 4853. "Audit adjustment AJE-X05: interest accrued on the Business Savings Account to 30 September 2026, as confirmed by the bank" | BA-091 | M-5 (决定 15) |

应计工资的 SG 241.20 计入 M-2 (826), 不计入 M-4; 每笔更正分录只对应一条错报的一个科目, FIN-09 不会判成部分更正.

AJE-X02 至 X04 的摘要: "Audit adjustment AJE-X02: superannuation guarantee at 12% on wages of 33,500.00 and accrued wages of 2,010.00", "Audit adjustment AJE-X03: wages accrued for 28 to 30 September 2026", "Audit adjustment AJE-X04: annual leave accrued to 30 September 2026". 同一笔的各行摘要相同. 校验脚本复算: 每笔借贷平衡, 科目都在快照科目表, 09-30 试算表加 5 笔后的各科目与本期损益 -22,136.99; 工资数据自检 (应计工资 3/10, 年假 73/7 × 4/52 × 周工资, 养老金 12%, 后两项法定原文待核 T2).

AJE-X05: 与 AJE-X01 同属第 1 轮 (M-5 与 M-1 都在 P2 发现), 编号按设计先后排在 X04 之后, 不按入账先后重编 (避免改动 X02 至 X04 的全部引用). Xero 科目表没有应计收入科目, 借方用 620 Prepayments (ASS.CUR.REC.PRE, 标准分类 prepayments, 属流动资产中的应收与预付); 贷方 270 Interest Income 的标准分类是 other_income (Xero 科目类型 OTHERINCOME), 不进 PLN-01 与 PLN-19 的收入基准 (revenue_categories 只含 revenue), 重要性不变. 48.53 = 10-16 入账的利息 97.05 (BT-091-0002, XJ-718) 按计息期 09-16 至 10-15 折算到 09-30 的 15/30, 是补造回函的设定, 银行侧没有原始记录.

---

## 3 各表口径

### 3.1 从 Xero 映射的表 (载入器 `build_tables`)

| 表 | 来源 | 口径与 ID | 行数 |
|---|---|---|---|
| accounts | accounts.json | 26 类标准分类 (迁移 0002); 855 Clearing Account 未映射 | 62 |
| trial_balance | current = `trial_balance_2026-09-30.json` (新快照由 Xero 接口给出), prior = 0630 表 | prior 含 FY26 损益行 (销售 4,200 等), 载入器按 retained_earnings 分类结转 | current 27 (含余额为 0 的 801) / prior 6 |
| journal_entries | journals.json | 行 ID `XJ-{JournalNumber}-{k}`, k 只数非零行; 作废对全部入库; created_by 全是 "xero"; created_at 是 Demo 重置时生成的时间 | 1017 |
| sales_invoices | invoices.json (ACCREC, AUTHORISED 或 PAID) | `SI-{InvoiceID}`; amount_cents 取 Total, **含税**; journal_entry_id 指向最后一次过账分录中记应收的行; 贷项通知单不进此表; contract_id 由补造的 contract_links.csv 填 | 42 (本期 26) |
| bank_accounts | accounts.json | BA-090, BA-091; BSB 306234, 账号 12345678 与 12345679; bank_name 由补造 bank_accounts.csv 覆盖为 Wattlebrook Bank Limited | 2 |
| bank_transactions | bank_statements.json | `BT-{科目}-{4 位序号}`, 按 postedDate 稳定排序, 从 07-01 起编号 | 090: 142 (09-30 前 72), 091: 4 (09-30 前 1); 期后 73 行 (090 有 70, 091 有 3) |
| delivery_notes | 补造 delivery_notes.csv | 见 3.3 | 24 |
| adjustments | 补造 adjustments/journal_entries.csv | 不在载入时入库, 由更正通知按编号载入 (第 9.3 节第 10 项) | 5 笔 11 行 (X01 3 行, X02 至 X05 各 2 行) |

### 3.2 试算表与关键数

**09-30 主要余额 (未调整)**
- 资产: 090 -10,900.44 (透支); 091 11,581.23; 610 10,199.50; 710 750.00; 711 -825.00.
- 负债: 800 -4,005.16; 820 -1,191.97; 825 -7,535.00; 840 -19,212.21; 860 0.09.
- 权益: 960 7,912.00.
- 损益: 200 -32,431.00; 477 33,500.00; 469 4,500.00; 其余费用 7,657.96.
- 本期没有使用的科目: 970, 880, 881, 416, 478, 826, 804, 805, 620, 270 (后两个由 AJE-X05 用到).

| 项 | 未调整 (P1, P2 看到的数) | 5 笔更正后 (报表数) |
|---|---:|---:|
| 收入 (只有 200 科目) | 32,431.00 | 32,431.00 |
| 其他收入 (270 利息) | 0 | 48.53 |
| 本期损益 | -13,226.96 | -22,136.99 |
| 流动资产 (透支列为负债) | 21,780.73 | 21,829.26 |
| 流动负债 (含 840) | 42,844.69 | 51,878.25 |
| 流动比率 / 剔除 840 | 0.5084 / 0.9216 | 0.4208 / 0.6683 |
| 净资产 / 剔除 840 | -21,138.96 / -1,926.75 | -30,048.99 / -10,836.78 |
| 系统口径 total_assets (资产类全加, 含透支负数与设备净额 -75) | 10,805.29 | — |

**按月数据**

| 项 | 7 月 | 8 月 | 9 月 | 10 月 (期后) |
|---|---:|---:|---:|---:|
| 收入 200 | 4,600.00 | 18,036.00 | 9,795.00 | 27,513.32 |
| 费用合计 | 3,311.17 | 24,418.70 | 17,928.09 | 17,220.95 |
| 其中工资 477 | 0 | 20,100.00 | 13,400.00 | 6,700.00 |
| 应收 610 变动 | +2,860.00 | +6,245.00 | +1,094.50 | — |

10 月另有 270 科目利息 97.05.

**其他关键数**
- 现金: 两户对账单 07-01 期初合计 10,509.01, 09-30 为 680.79, 季度净流出 9,828.22; 本期没有筹资流入. 090 每天日终都透支, 最高 13,378.24 (09-13, BT-090-0048 后), 平均 7,149.28; 透支额度 15,000, 09-30 剩余 4,099.56.
- 工资: 5 次双周发薪 (08-02, 08-16, 08-30, 09-13, 09-27), 每次 6,700; PAYG 7,535.00 未缴; SG 为 0.
- 收入集中度 (发票净额减贷项): DIISR 7,836.00 (24.2%), Petrie McLoud Watson 5,600.00 (17.3%), Maddox 3,850.00 (11.9%).

### 3.3 补造表的口径

**delivery_notes.csv** (24 行, 只覆盖本期生效发票; 期后发票不建记录, REV-08 窗口不含它们)
- 列: id, delivery_no, invoice_no, shipped_date, signed_date, signed_by. 载入器按 invoice_no 换成 SI- ID.
- shipped_date 表示交付或服务开始日; signed_date 表示客户签收, 验收或确认服务完成之日. 签收人用化名.

| 发票 | 开始或交付日 | 签收或完成日 | 说明 |
|---|---|---|---|
| ORC1008 至 1011 / 1016 至 1019 / 1020 至 1023 | 当月 1 日 | 当月最后一天 | Support-M 月费, 按自然月提供服务 |
| ORC1006 | 07-21 | 07-23 | 3 场培训 |
| ORC1002 | 07-20 | 07-24 | |
| ORC1004 | 07-29 | 07-29 | |
| ORC1000 | 07-27 | 07-31 | |
| ORC1001 | 07-20 | 07-31 | |
| ORC1007 | 07-06 | 07-31 | 7 月每周一, 周二, 共 8 天 |
| ORC1012 | 08-13 | 08-14 | |
| ORC1013 | 08-14 | 08-20 | |
| ORC1014 | 08-11 | 08-26 | 实到 5.5 天, 与 CR1014 呼应 |
| ORC1015 | 09-01 | 09-04 | |
| ORC1024 | 08-19 | 08-19 | 培训在 8 月, 解释到期日 08-26 早于开票日 |
| **ORC1026** | **09-03** (开发开始) | **09-17** (完工证书) | 签收人为 DIISR Project Sponsor (化名) |

ORC1003 与 ORC1005 不做记录: 一张是重复开票, 一张已并入 ORC1006, 都没有对应服务.

签收人化名写在生成规格 delivery_notes 段, 每个客户一个; SR-ORC1026 与 SR-ORC1001 的签收人 Priya Natarajan 即 CON-X01 上签字的 DIISR Project Sponsor.

**contract_links.csv**: invoice_no, contract_id. ORC1026 → CON-X01; ORC1003, 1008, 1016, 1020 → CON-X02; ORC1014 → CON-X03.

### 3.4 关键对象 ID

| 对象 | ID |
|---|---|
| ORC1026 | SI-77b338ef-ecc0-4b95-a0d7-2617b0054611; 分录行 XJ-580-1 (200 科目 -650000), -2 (610 +715000), -3 (820 -65000) |
| ORC1020 / 1021 / 1022 / 1023 / 1024 | SI-5f6deadf-36a2-495a-9980-ceb11e8af9a9 / SI-3b28bf11-ed2f-4cf4-8e9e-fcae730cc292 / SI-a9f765e6-b9bc-4505-a47b-fb3ecb327e7b / SI-bcd8a71f-aa31-4d0f-8a01-13ea26363ddf / SI-5aadcd34-01a9-4b8d-a2bb-d7cc1de9fa45 |
| ORC1003 / ORC1008 | SI-4fad1af2-b871-4ac5-a15a-3c5e32d2e2c4 (XJ-405) / SI-1d1ba340-afa2-4f4c-8ff7-a147bda9a47b (XJ-408) |
| ORC01025 | SI-e4a0afbd-aea0-450b-ae23-0ce921e84a77; XJ-647 |
| ORC1002 / ORC1012 | SI-962ef33f-c9d2-4602-9b9f-93a02bea23b3 / SI-f5832195-5cd3-4660-ad3f-b73d9c64f263 |
| 1026C | XJ-720-1 (200 +105000), -2 (610 -115500), -3 (820 +10500) |
| 转换余额 | XJ-397-1 (091 +1508123), -2 (090 +413098), -3 (840 -1921221) |
| #499 | XJ-499-1 (710 +75000), -2 (711 -82500), -3 (820 +7500) |
| SMART Agency 账单 SM0195-70135 | XJ-548-1 (820 +18182), -2 (400 +181818), -3 (800 -200000); 作废对 XJ-547/549 同日 |
| Yarra 收款 (ORC1021) | XJ-637-1 (610 -55000), -2 (090 +55000), 10-06; 对账单 BT-090-0081 |
| 期后设备 | XJ-626-2 (710 +630000, ABC Furniture, 10-04); XJ-676-2 (720 +196999, PC Complete, 10-11) |
| 房租 | XJ-420-2, XJ-518-1, XJ-593-3 (469 科目各 150000); 对账单 BT-090-0004, 0033, 0058 |
| 09-30 对账单末行 | BT-090-0072 (09-29, 余额 -1090044), BT-091-0001 (09-26, 余额 1158123) |
| 期后流水 | BT-090-0085 (10-06, DIISR 214500, 参考号 "1028"); BT-090-0092 (10-10, Ridgeway University 618750); BT-090-0097 (10-11, SMART Agency -450000, "70135 70209"); BT-090-0099 (10-11, Jakaranda 200000); BT-090-0107 (10-12, e-Bank dep 100000); BT-090-0124 (10-14, DIISR 385000, "1026"); BT-090-0130 (10-15, e-Bank dep 150000); BT-090-0131 (10-15, Majestic 45000) |

下文 "SI-ORC1026" 等写法是简写.

---

## 4 补造单据清单与要点

全部 PDF 正文用英文, 版式照 Numbat; 每页页眉 `Synthetic – for demonstration only. Fictitious document based on Xero Demo Company (AU) data.` 文件名不写 synthetic (回函必须叫 `{account_no}.pdf`), 合成标志写在 manifest.

| # | 文件 | 页数 | 要点 | 使用的程序 |
|---|---|---|---|---|
| 1 | engagement/appointment.pdf | 4 | 4.1 | ACC-08 |
| 2 | engagement/engagement_letter.pdf | 3 | 4.2 | ACC-08 |
| 3-6 | registry/extracts/ 公司摘录 3 份 (各 2 页), 人员检索 1 份 | 7 | 2.5 | GEN-08 |
| 7 | contracts/CON-X01.pdf DIISR SOW No.2 | 3 | 4.3 | DOC-04, REV-03, REV-02 |
| 8 | contracts/CON-X02.pdf Hamilton Smith 支持协议 | 3 | 4.3 | 同上 |
| 9 | contracts/CON-X03.pdf Maddox 项目管理协议 | 3 | 4.3 | 同上 |
| 10 | confirmations/12345678.pdf (BA-090) | 2 | 4.4 | CSH-04, DOC-06 |
| 11 | confirmations/12345679.pdf (BA-091) | 2 | 4.4 | 同上 |
| 12 | VPA 租金函 (06-25) | 1 | 4.5 | GEN-03 |
| 13 | VPA 出售与终止通知 (08-28) | 1 | 4.5 | GEN-03 |
| 14 | Harbourview 新租约 (09-22, 前 2 页) | 2 | 4.5 | GEN-03, GEN-06 |
| 15 | RM2031 分租协议 (10-15) | 1 | 4.5 | GEN-06 |
| 16 | 固定资产登记表 (截至 09-30) | 1 | 截至 09-30 "Nil"; 另列 "Additions after the reporting date (for information)": 10-04 家具 6,300 (710), 10-11 笔记本 1,969.99 (720) | GEN-02 (XJ-499), GEN-06 |
| 17 | 报销单 E003 | 2 | 申请人 Odette Garrison (员工号 E003), 08-14 填报, 08-16 批准; 4 张收据 (08-03 至 08-12) 合计 296.95, 逐张对上 XJ-493 (08-16 入账: 差旅 493 三行, 含税 179.00, 30.45, 80.00; 招待 420 一行 7.50, 不抵扣进项税; 进项税 26.31); 系统录入时把姓误写为 "Grainger" | GEN-02 (XJ-575) |
| 18 | 工资册与雇佣条款 | 2 | 三人 07-20 入职全职; 双周发薪截至发薪日 (周日); 毛工资每期 Garrison 2,500, Gray 2,400, Green 1,800; 适用 NES 年假 | FIN-15 |
| 19 | 会计政策摘录 | 1 | 资本化门槛 1,000 (不含税), 排除 Oliver 笔记本净额 886 的资本化问题 | FIN-15, FIN-12 |
| 20 | 董事借款协议 (2018-09-10) | 2 | 4.6 | GEN-02 (XJ-397), FIN-12 |
| 21 | 董事借款确认函 (2026-10-16) | 1 | 4.6 | 同上, GEN-04 |
| 22 | 持续经营备忘录与 13 个月现金流预测 (10-16) | 3 | 4.6 | GEN-04, FIN-02 |
| 23 | 董事支持函 (10-16) | 1 | 4.6 | GEN-04, FIN-02, GEN-06 |
| 24 | DIISR 邮件 (10-13), 商定 1,050 让步 | 1 | 6.4 (决定 14) | GEN-06 |
| 25 | SMART Agency 税务发票 SM0195-70135 (09-05) | 1 | 按 Xero 账单原样: "Design concepts for advertising material", 2,000 含税, 服务交付 09-04, 到期 09-19; 抬头 SMART Agency Pty Ltd, ABN 23 900 043 076 (与 T6 的合成号码相同, 基线登记 mock 中没有这家) | GEN-06; 注入测试 T3, T6 |
| 26 | Ridgeway University 订单 CRM08-12 (10-02) | 1 | 10 月 5 日起每周 3 天驻场项目管理, 月初预开票 (对上 ORC01025) | GEN-06 |
| 27 | Jakaranda Maple Systems 报价单 (10-08) | 1 | 新项目报价, 要求 2,000 订金 | GEN-06 |
| 28 | financial_statements/fs_v1.pdf (10-22 编成, 10-23 董事声明) | 4 | 4.7 | FIN-12, RPT-03 |
| 29 | 管理层声明书 (10-23) | 2 | 4.8 | FIN-07, FIN-18 |

另有结构化文件 (不计入 document): fs_v1.json, delivery_notes.csv, contract_links.csv, bank_accounts.csv, receipts.json, registry JSON, planning JSON, client_profile.json, dataset_traits.json, payroll/employees.csv, adjustments CSV, manual/*.json, llm/ fixture.

**文件类型, 证据种类与可靠性**

现状: `DocumentType` 只有 contract, confirmation, other; `judgment_engine.DOCUMENT_EVIDENCE` 只映射 contract → contract, confirmation → external, 其他类型的文件指针在引用级被拒 ("{type} 类文件不作证据"). 各组草稿已加: registry_extract → external (G2); engagement_letter → contract, minutes → voucher, representation_letter → voucher, correspondence → voucher (G5); governance_communication (G6). 本数据集还要新增 3 类 (契约一次合并, 见需求清单 R89):

| 新增 DocumentType | 证据种类 | 用于 | 为什么不复用已有类型 |
|---|---|---|---|
| agreement | contract | 收入合同以外的协议: 租金函, 新租约, 分租协议, 董事借款协议, 客户订单 | 不能用 contract: `agents/ingest.py` 的 DOC-04 取全部 contract 类文件逐份抽取收入条款, 租约与借款协议混进去会多出合同与大模型调用 |
| supplier_invoice | invoice | 供应商税务发票 | 外来发票, 证据种类应为 invoice |
| internal_record | voucher | 公司内部记录: 登记表, 报销单, 工资册, 会计政策, 备忘录与预测, 报价单 | other 不作证据 |

29 份文件逐一 (reliability 取 DocumentRef, 登记时给定):

| # | 文件 | DocumentType | 证据种类 | reliability | 登记途径 |
|---|---|---|---|---|---|
| 1 | appointment.pdf | minutes | voucher | internal_copy (G5) | load_engagement_documents (G5) |
| 2 | engagement_letter.pdf | engagement_letter | contract | external_copy (G5) | 同上 |
| 3-5 | 公司摘录 3 份 | registry_extract | external | external_original (G2) | GEN-08 运行时由 RegistrySource 回执存档 |
| 6 | 人员检索 | registry_extract | external | external_original | 同上 |
| 7-9 | CON-X01 至 X03 | contract | contract | external_copy (stores.py 现状) | 载入器 build() |
| 10-11 | 银行回函 2 份 | confirmation | external | 按回函渠道 (direct_email, `reply_reliability`) | CSH-04 运行时存档 |
| 12 | VPA 租金函 | agreement | contract | external_copy | 载入器按 documents/index.csv |
| 13 | VPA 出售与终止通知 | correspondence | voucher (G5 映射) | external_copy | 同上 |
| 14 | 新租约 | agreement | contract | external_copy | 同上 |
| 15 | 分租协议 | agreement | contract | external_copy | 同上 |
| 16 | 固定资产登记表 | internal_record | voucher | internal_original | 同上 |
| 17 | 报销单 E003 | internal_record | voucher | internal_original | 同上 |
| 18 | 工资册与雇佣条款 | internal_record | voucher | internal_original | 同上 |
| 19 | 会计政策摘录 | internal_record | voucher | internal_original | 同上 |
| 20 | 董事借款协议 | agreement | contract | internal_original (双方是公司与其唯一董事, 没有独立第三方) | 同上 |
| 21 | 董事借款确认函 | correspondence | voucher | internal_original (出自管理层) | 同上 |
| 22 | 持续经营备忘录与预测 | internal_record | voucher | internal_original | 同上 |
| 23 | 董事支持函 | correspondence | voucher | internal_original | 同上 |
| 24 | DIISR 邮件 | correspondence | voucher | external_copy | 同上 |
| 25 | SMART Agency 发票 | supplier_invoice | invoice | external_copy | 同上 |
| 26 | Ridgeway 订单 | agreement | contract | external_copy | 同上 |
| 27 | Jakaranda 报价单 | internal_record | voucher | internal_copy (公司发出的报价的留存副本) | 同上 |
| 28 | fs_v1.pdf | other | 不作证据; 判断引用 financial_statement_line (G6) | internal_original | 载入器按 index.csv (只供查看) |
| 29 | 声明书 | representation_letter | voucher | internal_original (G5 示例) | FIN-07 人工任务上传 (G5) |

documents/index.csv 列 19 份: #1, 2 (engagement 类, 供 load_engagement_documents) 与 #12 至 28; 摘录, 回函, 收入合同, 声明书按各自途径登记, 不重复列. related_ids 列写关联对象 ID (如 #12 至 #14 写 ACN-900043030, #25 写 XJ-548 与 BT-090-0097), 供 GEN-03, GEN-06 按对象取文件.

### 4.1 appointment.pdf

- 第 1 页, 董事决议 (08-14, s248B, s323D(2A)): 自 1 July 2026 起的财年于 30 September 2026 结束, 以后各年截至 9 月 30 日. 逐条确认: (a) 紧接 30 June 2026 结束的上一财年; (b) FY2022 至 FY2026 均为 12 个月, 没有依本款缩短过; (c) 履行 06-15 股东协议并对齐 10 月的合同续约周期.
- 第 2 页, s293 指示 (08-20): Sam 持 100 股中的 15 股; 要求编制财务报告与完整的董事报告; 依 s293(3)(c) 要求审计.
- 第 3 页, s328A 同意书 (08-25): Jordan Ellis 以事务所和本人名义双签.
- 第 4 页, 董事决议 (08-27, s325(1)): 委任 Tessellate Audit Partners.

### 4.2 engagement_letter.pdf

- 第 1 页: ASA 210 第 10(a)(b) 段; 首次审计对期初余额执行的程序 (ASA 510 第 6 段); 上期未审, 报告将加其他事项段 (ASA 710 第 14 段).
- 第 2 页: 第 10(c)(d) 段; 第 6(b)(i)-(iii) 段的确认; 比较数不完全可比 (AASB 1060, 段号暂缓, 待 AASB 1060 正本).
- 第 3 页: 第 10(e) 段; 收费 AUD 4,800, 不含 GST.

### 4.3 收入合同

**CON-X01: DIISR - Small Business Services, "Statement of Work No. 2 - Portal Module Development"** (按决定 8 与 11 重写)
- 第 1 页: 对方写 Xero 原名, 不写 ABN; SOW 日期 1 September 2026; 2.1 Contract Price $6,500.00 excluding GST (10 developer days at $650.00); 2.2 开发期 3 至 16 September 2026.
- 第 2 页:
  - 3.1 供方在自己的开发环境完成开发, 完成后把模块部署到客户环境.
  - 4.1 "The Services are complete when the Supplier has deployed the Module to the Customer's environment and the Customer's Project Sponsor has signed the Completion Certificate (the Completion Date). Title to and control of the Module pass to the Customer on the Completion Date."
  - 5.1 供方可在 Completion Date 开具税务发票; 5.2 发票到期日付款, 不晚于开票后 30 天 (Xero 到期日 10-06).
- 第 3 页:
  - 6.1 合同价格的任何变更须双方书面同意 (不设迟延或里程碑贷项).
  - 7.1 任一方提前 10 Business Days 通知终止; 7.2 在 Completion Date 前终止的, 供方无权就模块收款. 所以第 35(b)(c) 段都不满足, 收入在某一时点 (完工日) 确认.
- 关联发票 ORC1026; 完工日 09-17 在期内, 9 月确认正确.

CON-X02 的 4.1 写 "the Customer simultaneously receives and consumes the benefit of the Support Services as the Supplier provides them" (贴近 AASB 15 第 35(a) 段用语); CON-X03 的项目写 "CRM integration project", 同 ORC1014 的发票行描述. 括号中的 ACN 是 ABN 后 9 位, 合同只印 ABN. ORC1026 的 Xero 行描述 "Development work - developer onsite per day" 与 CON-X01 3.1 字面不同, 载入器不带发票行描述, 不影响 REV-03 的预期.

签字人 (化名): 供方 Alex Morgan (Director); DIISR Priya Natarajan (Project Sponsor), Hamilton Smith Tom Reilly (Operations Manager), Maddox Nora Fitzgerald (Production Director). manifest 的 document_date 为签订日, xero_refs 为关联发票的 SI- ID.

**CON-X02: Hamilton Smith Pty, "Managed Support Agreement"** (不变)
- 第 1 页: ABN 19 900 043 058 (ACN 900 043 058); 2026-06-24 签订; 期限 2026-07-01 至 2027-06-30; Monthly Fee $500.00 excl GST, 不写总价.
- 第 2 页: 4.1 客户在每个自然月内随服务提供而取得利益; 5.1 每月 17 日前后开当月发票; 5.2 开票后 14 天内付款.
- 第 3 页: 6.1 Priority 1 请求 4 个工作小时内未响应, 客户可索赔当月月费的 10%; 7.1 30 天通知终止.

**CON-X03: Maddox Publishing Group, "Project Management Services Agreement"** (不变)
- 第 1 页: ABN 21 900 043 067 (ACN 900 043 067); 采购单号 H008-346; 2026-08-10; 6 个驻场日 × $700, 共 $4,200.
- 第 2 页: 4.1 按客户签字的工时表在每个驻场日确认; 5.1 第六个驻场日后开票, 10 天付款.
- 第 3 页: 6.1 项目经理缺席时每半日贷记 $350 (对上 CR1014); 7.1 5 个工作日通知终止.

### 4.4 银行回函

**12345678.pdf (BA-090)**
- 第 1 页: Wattlebrook Bank Limited 致 Tessellate; 客户 Demo Company (AU) Pty Ltd (ABN 25 900 043 021); Balance at 30 September 2026: **AUD 10,900.44 (debit - account overdrawn)**; accrued interest and fees not yet debited: nil.
- 第 2 页: Facilities: 透支额度 AUD 15,000, 即期偿还, 2027-03-31 年审, Alex Morgan 个人担保; 各账户之间没有抵销安排. Other accounts: 12345679, 11,581.23 (credit). Delivery: 直接以电子邮件发给审计师. 页脚 Ref WBL-AC-2026-093001.

**12345679.pdf (BA-091)** (决定 15): 第 1 页余额 AUD 11,581.23 (credit); "No interest was credited to this account between 1 July 2026 and 30 September 2026"; **"Accrued interest at 30 September 2026, not yet credited to the account: AUD 48.53 (in the customer's favour; interest period 16 September to 15 October 2026, payable on 16 October 2026)"**; 第 2 页 Facilities: none; Ref WBL-AC-2026-093002. 48.53 的推算见第 2.8 节表下说明.

**BA-090 回函**的 "accrued interest and fees not yet debited: nil" 不变. 抽取口径 (第 6.1 节): 应计利息记银行欠客户为正, 客户欠银行为负, 写 nil 为 0, 回函没提为空.

**receipts.json**: 两个账号的 receipt_channel 都是 direct_email.

两份回函的 manifest document_date 用确认日 2026-09-30, reliability 为 external_original (direct_email 是直接渠道); 银行签发人 Helen Marsh (化名); 12345679 第 2 页的其他账户一节只写 "See our separate reply for account 12345678". 生成规格每份回函另记 confirmation {bank_account_id, balance_cents, accrued_interest_cents}, 校验脚本据此核对 PDF 第 1 页的写法, 对账单 09-30 前末行余额 (BT-090-0072, BT-091-0001) 与 4853 的推算 (BT-091-0002 × 15 / 30, 四舍五入).

### 4.5 关联方与租赁文件 (documents/related_party)

- **VPA 租金函 (06-25)**: Dana 以 VPA 董事身份签署, Alex 以本公司董事身份签收; 按月租赁, 1,500 加 GST, 每月 21 日付当月租金; 不签书面租约; 任一方提前 1 个月通知终止.
- **出售与终止通知 (08-28)**: 物业 09-30 交割给 Harbourview; 现有安排于 09-30 终止 (提前 1 个月以上); 10-01 起由 Truxton 代管.
- **新租约 (09-22)**: 同一物业, 2026-10-01 至 2028-09-30; 年租 12,886.32 加 GST, 每月预付 1,073.86 加 GST (对上 XJ-613); 允许经出租人同意后分租.
- **分租协议 (10-15)**: RM2031 分租给 Majestic Contracting, 月租 450 (对上 BT-090-0131).
- 推算: 关联方租金比同一物业此后的租金高 (1,500.00 - 1,073.86) / 1,073.86 = 39.7%, 本期多付 3 × 426.14 = 1,278.42.
- 补造细节: VPA 地址 14 Harbour Road, Marineville NSW 2000 (虚构); 新租约由 Truxton 的 Mia Truscott (化名) 代出租人签署, 租金付到代管人信托账户 (10-10 的 1,181.25 即 1,073.86 加 GST); 分租协议的分租人签字人 Lewis Pham (化名), 月租 450.00 含 GST, 出租人 10-14 以邮件同意.

### 4.6 董事借款与持续经营文件 (决定 8)

**董事借款协议 (2018-09-10)**: Alex 向公司提供无息, 无担保的营运资金借款, 随时可要求偿还; 借款余额按公司账户余额与董事垫付记录确定.

**董事借款确认函 (2026-10-16)**: 840 的 19,212.21 是 Alex 历年投入的无息借款 (含 1.00 股款); 公司在 Xero 开账时按期初银行余额转入; 报表中列为 "Loan from director" (流动负债, 关联方).

**持续经营备忘录与现金流预测 (2026-10 至 2027-10, 共 13 个月)**
- 相关期间约到 2027-10 下旬 (ASA 570 Aus 13.2).
- 列明: 10 月已实现的回款; 10-28 应缴的 PAYG 7,535 与 Q1 的 GST, SG 4,261.20 (截止日待核 T2); 笔记本应付 2,166.99 (11-05 到期); 每月工资约 14,500.
- 2026-11 与 2027-01 两个月出现缺口, 由董事注资补足.

**预测表 (备忘录第 2 页)**: 期初净额 680.79 (两户 11,581.23 - 10,900.44); 10 月合并 10-01 至 10-16 的实际收付 (收款 26,991.65, 其中客户回款 21,705.10 按对账单逐笔列在第 1 页; 付款 14,318.72; 16 日净额 13,353.72) 与 10-17 至 10-31 的预测. 每月列收款, 付款, 注资前净额, 董事注资, 期末净额, 加 15,000 额度后的可用资金; 注资前可用资金为负的月份标星, 只有 2026-11 (注资 6,000) 与 2027-01 (注资 14,000, 当月付 Q2 养老金), 注资合计 22,500 (含 10 月已转入的 2,500). 10 月付款含 10-11 与 10-25 两次发薪代扣的 PAYG 3,014.00, 11 月起工资按毛额约 14,500 计 (已含 PAYG). 收款按月费 2,200 加项目与培训收入 (由 11 月约 3,500 增至 2027-09 约 21,350) 估算; 养老金与 GST 按季在季后一月的 28 日前缴 (10-12 月的 GST 在 2 月), 金额为估计.

**董事支持函 (2026-10-16)**: 自报告日起 12 个月内不要求偿还 19,212.21; 需要时追加最多 40,000; 已于 10-12 与 10-15 转入 1,000 与 1,500 (对账单 "e-Bank dep", 总账未记, 作董事借款增加). **不附个人资产证明**, 所以结论是 "存在重大不确定性" (ASA 570 A13, A19).

### 4.7 管理层报表 fs_v1 (5 笔更正后)

**财务状况表**
- 资产: 现金 11,581.23; 应收 10,199.50; 其他应收 (应计利息, 620) 48.53; 合计 21,829.26.
- 负债: 银行透支 10,900.44; 应付 4,005.07 (800 加 860); GST 与 PAYG 8,801.97 (820 为 1,266.97, 825 为 7,535.00); 员工薪酬负债 8,958.56 (804 为 2,010.00, 805 为 2,687.36, 826 为 4,261.20); 董事借款 19,212.21; 合计 51,878.25.
- 权益: 累计亏损 (30,048.99).

**损益表**: 收入 32,431.00; 其他收入 (利息, 270) 48.53; 员工薪酬 42,458.56 (477 为 38,197.36, 478 为 4,261.20); 场地费用 6,356.00 (469, 445, 408); 其他费用 5,801.96; 本期亏损 (22,136.99).

**比较数** (截至 2026-06-30 的 12 个月, 未经审计): 按 0630 表加 XJ-397 重述: 现金 15,081.23, GST 应收 791.20, 透支 4,572.22, 董事借款 19,212.21, 累计亏损 (7,912.00); 损益: 收入 4,200, 费用 12,112, 亏损 7,912.

**附注** (AASB 1060 段号暂缓, 待 AASB 1060 正本)
1. 编制基础, 期间只有 3 个月.
2. 持续经营: ASA 570 第 19(b) 段句式; 写明亏损 22,136.99, 净流动负债 30,048.99, 依赖董事支持与透支额度.
3. 关联方: 董事借款 (含 10 月新增 2,500); 透支担保; VPA 房租 4,500 及其条款 (审计后补入, 第 7 节第 18 步).
4. 期后事项: 新租约 (同一物业, 10-01 起租) 与 10-15 起的分租, 家具 6,300 (新租约下的装修), 笔记本 1,969.99, 董事 10 月借款 2,500.

**补造写法** (`financial_statements/fs_v1.pdf` 4 页, 2026-10-22 编成, 董事声明 2026-10-23):
- 标题与叙述的期间写 "financial year ended 30 September 2026" (同第 6 批对报告与声明书的写法; 缩短的财年仍是 financial year); 行名 "Loss for the period" 照 R216 不改.
- 第 1 页财务状况表, 第 2 页损益表, 两页都列本期与比较数 (比较数注明未经审计); 0 写 "-", 负数与损益表的费用行加括号. 各行名称, 本期与比较数与 fs_v1.json 逐行一致, 校验按表核对所在页.
- 第 3 页附注 1 (编制基础: 公司法与 Simplified Disclosures, 依 s323D(2A) 缩短的 3 个月财年, 比较数为 12 个月且按 XJ-397 重述, 不完全可比) 与附注 2 (持续经营: 亏损 22,136.99, 流动负债超过流动资产 30,048.99, 透支额度 15,000 随时可收回; 董事自审计报告日起 12 个月不要求还款并追加最多 40,000; 预测在 2026-11 与 2027-01 有缺口; 结论用 ASA 570 第 19(b) 段句式).
- 第 4 页附注 3 (董事借款 19,212.21 含 1.00 股款, 10 月新增 1,000 与 1,500; 透支个人担保; VPA 为董事与其配偶的公司, 本期房租 4,500 不含 GST, 按月 1,500, 无书面租约, 9-30 终止; Morgan Advisory 无交易; 董事不领薪酬), 附注 4 (10-01 起向 Harbourview 租两年, 年租 12,886.32 加 GST; 10-15 起分租 RM2031; 家具 6,300 用于新租约下的装修, 笔记本 1,969.99, 均不含 GST; 董事 10 月借款 2,500) 与董事声明 (s295(4)(c), (d) 两项意见, 按 s295(5) 写明依董事决议作出, 签署与日期).
- 不写 AASB 1060 的段号 (段号暂缓, 待 AASB 1060 正本, T7).
- 只补造财务状况表与损益表 (FIN-12 只勾稽这两张表). 权益变动表与现金流量表是否可省略暂缓到拿到 AASB 1060 正本 (T7) 再定, 本版不补造; 委托书 (第 4.2 节) 照常按完整财报列举组成.

### 4.8 人工队列提交物 (manual/)

- **FIN-08 第 1 轮回复 (10-20)**: "We agree with both items. Journal AJE-X01 dated 30 September 2026 removes journal #499, as no office equipment was held at that date. Journal AJE-X05 dated 30 September 2026 accrues interest of AUD 48.53 on the Business Savings Account, as confirmed by the bank."
- **FIN-08 第 2 轮回复 (10-22)**: "We agree with all three items. Journals AJE-X02 to AJE-X04 dated 30 September 2026 record superannuation guarantee of AUD 4,261.20, wages accrued for 28-30 September of AUD 2,010.00 and annual leave of AUD 2,687.36."
- **声明书 (10-23)**: ASA 580 第 10, 11 段; ASA 560 第 9 段; ASA 570 第 16(e) 段; ASA 550 第 26 段; ASA 240 第 40 段; ASA 250 第 17 段; 未更正错报一项写 "All misstatements communicated have been corrected; there are no uncorrected misstatements", 附表为空. 补造为 `representation/representation_letter.pdf` 2 页: 致 Tessellate Audit Partners, 期间写 "for the financial year ended 30 September 2026", 引用 2026-08-28 的委托书. 措辞取 ASA 580 Appendix 2 (主体名改为 the Company, 框架写 Australian Accounting Standards - Simplified Disclosures 与公司法), 只有持续经营与 "无未更正错报" 两条自拟 (同 R219 模板的 drafted 项). 第 1 页: 财报责任 (Appendix 2 首条, 按 ASA 580 第 12 段与委托书写 true and fair view), 提供信息 (委托书 (iii) a-c 三小项), 交易完整, 舞弊 (ASA 240 第 40(a)-(d) 段 4 条). 第 2 页: 法规 (ASA 250), 关联方 (ASA 550 两条, 指向附注 3), 期后事项 (ASA 560), 持续经营计划可行 (ASA 570, 指向附注 2 与 10-16 的备忘录), 错报与空附表; Alex Morgan, Director 签署. 段号已按 `docs/参考资料` 原文核对. representation.json 的 standards 用准则段落 ID (ASA580-P10, ASA580-P11, ASA560-P09, ASA570-P16, ASA550-P26, ASA240-P40, ASA250-P17).
- **RPT-07 董事回复 (10-22)**: "Thank you for your letter of 22 October 2026. We have read the matters you communicated: the significant qualitative aspects of our accounting practices, the significant difficulties encountered during the audit, the expected form and content of your auditor's report, including the material uncertainty related to going concern, and your statement of independence. We confirm that the five misstatements you communicated have been corrected and that we have no further matters to raise."

---

## 5 程序改动与规则程序预期命中

### 5.1 现有程序的通用化改动 (决定 9)

原则: 提示词和规则只写审计逻辑; 跟具体公司或数据来源有关的事实 (科目代码, 参考号格式, 数据截止日, 录入时间是否可信) 放在数据与参数里. 每处改动都要跑 Numbat 回归.

| 程序 | 改动 | Numbat 影响 (已复算或待跑) |
|---|---|---|
| DOC-04 提示词 | 条款表 control_transfer 改为 "when title, risk or control of the goods or services passes to the customer (for example on delivery, acceptance, or completion of the service)" | 合同仍是货物条款, 抽取结果不变; live 回归待跑 |
| REV-03 提示词与准则 | "control of the goods" 改为 "goods or services"; 转移事件列出签收, 验收, 服务完成, 随履约在一段时间内转移; standards 加 AASB15-P35 | mock 不变; live 回归待跑 |
| REV-02 提示词 | 判断 "控制权按合同约定的转移事件 (签收, 验收或服务完成) 转移的期间"; 没有挂合同时按发运或服务完成记录判断; 说明 delivery_notes 两个日期在服务业务中的含义 | mock 不变; live 回归待跑 |
| REV-02 上下文 | data 加 `journal_lines`: 发票 journal_entry_id 所在分录的全部行, 每行带 source_type journal_entry, 进入 supported_amounts; 这样不含税的收入行金额可作错报金额 | Numbat 的 SJ-2026-0612 分录行可被引用, 原 fixture 金额 14200000 仍有依据 |
| REV-08 R03 | 匹配条件在原 "参考号包含发票号" 之外, 只对 "字母前缀加纯数字" 的发票号 (正则 `^[A-Za-z]+[0-9]+$`) 补一条: 参考号里出现该发票号的数字串, 且前后都不是数字 (SQL 写法见表下) | Numbat 发票号带连字符, 不触发新条件, 变化为 0 |
| PLN-07 A01 | 只比资产负债类 (asset, liability, equity), 本期末对期初; 期初 = 上期末余额, 上期损益按 retained_earnings 分类结转. 门槛改为参数 `min_change_revenue_pct: 0.005` (本期收入乘以它) 且变动不小于期初绝对值的 `change_pct` 20%; 本期与上期长度相同时损益类照旧比较, 长度不同时不比. 上期长度 = prior_end 减 `dataset.prior_period_start` (5.2 节); 表里只有 prior_end, 没有上期起始日, 所以由数据集声明给出, 缺省视为与本期等长 | Numbat 收入 12,014,000, 门槛 60,070, 仍只命中 1100 (52500000) |
| PLN-07 新增 A03 | 月度趋势, emit rows: 本期内按月的收入类, 费用类合计与员工费用类, 来自 journal_entries; 不出例外 | Numbat 多 12 行数据, NUMBAT 预置 rows 要补 |

REV-08 R03 的匹配条件 (Postgres):

```sql
position(i.invoice_no IN t.reference) > 0
OR (i.invoice_no ~ '^[A-Za-z]+[0-9]+$'
    AND t.reference ~ ('(^|[^0-9])' || substring(i.invoice_no from '[0-9]+$') || '([^0-9]|$)'))
```

新程序 (各组草稿) 里的同类问题一并按此原则改, 见需求清单对应条:
- GEN-07 S01 的 "常规参考号前缀" (SI-, RENT-, AP- 等) 是 Numbat 的写法, 改为不依赖参考号的 "期后未入账流水" (5.4 节).
- FIN-09 的 account_codes 写死 Numbat 科目 (4100, 1010, 1020), 改为按 accounts.category 取. V01 的 SQL 是 `is_adjustment = true`, 在 Xero 上会取进 XJ-397, XJ-499 等手工分录; 改为 `entry_no LIKE {audit_adjustment_entry_prefix} || '%'` (前缀取自 dataset_traits, 默认 "AJE-", Numbat 结果不变) (G4).
- FIN-15 的 revenue_account_codes, receivable_account_codes 同上; F01 门槛与 PLN-07 一致; F03 至 F05 的薪酬科目从 dataset_traits 取 (5.5 节).
- GEN-05 的录入时间, 作废冲回格式从 dataset_traits 取 (5.3 节).
- GEN-03 (G2): C01 原口径 "按客户 ABN 汇总本期销售" 在 Xero 上取不到 VPA 与 SMART Agency 的交易: 快照 83 个联系人只有 1 个有 TaxNumber, VPA 既不是客户也不是供应商, SMART 是供应商, 两者都没有 TaxNumber; 要用的交易都是采购分录与银行流水. 改为通用写法:
  - C01 "与关联方的往来": 对象名称 (登记摘录的公司名) 规范化后, 在本期与期后窗口内匹配 (1) 分录: 任一行的 description 或 source_doc 规范化后含该名称 (整词), 取整笔凭证的全部行, 剔除作废对; (2) 银行流水: payee 规范化后含该名称; (3) sales_invoices: customer 规范化后等于该名称, 或 customer_abn 等于对象 ABN (ABN 只作附加条件). 规范化沿用 R11 (转小写, 去掉 counterparty_prefixes, 非字母数字换空格), 另去掉末尾的公司后缀 (参数 `entity_suffixes`: pty ltd, pty limited, pty, ltd, limited), 使 "Victoria Property Agency Pty Ltd" 与 Xero 的 "Victoria Property Agency" 相等. 必须取整笔凭证: SMART 账单 XJ-548 的费用行 (XJ-548-2) 说明是物品描述, 不含对方名称.
  - C02 原口径 "3xxx 权益科目本期分录" 是 Numbat 科目号, 改为按 accounts.category 的 equity 分类.
  - 上下文加入 documents/index.csv 中 related_ids 含该节点 ID 的文件 (VPA: 租金函, 出售与终止通知, 新租约).
  - Xero 复算: VPA 命中 XJ-420, 518, 593 (各 3 行) 与 BT-090-0004, 0033, 0058; SMART 命中 XJ-548 与 XJ-641 (期后) 及 BT-090-0097. Numbat: 按 customer_abn 匹配仍是条件之一, 原有的销售汇总不会减少; 名称匹配新增的分录与流水行未复算, 见需求清单对应条.

### 5.2 数据集声明 dataset_traits.json

```json
{"dataset": "xero/20261017-q1",
 "data_as_of": "2026-10-17",
 "prior_period_start": "2025-07-01",
 "entry_timestamps_reliable": false,
 "entry_timestamps_note": "Created timestamps are regenerated when the Xero demo company is reset; 15 journals carry created times after 19 October 2026 and up to 26 November 2026.",
 "reversal_reference_pattern": "Reversal of ID (\\d+)",
 "reversal_entry_no_template": "XJ-{1}",
 "counterparty_prefixes": ["payment to", "payment from", "payment:"],
 "audit_adjustment_entry_prefix": "AJE-",
 "payroll_accounts": {"wages_expense": ["477"], "super_expense": ["478"], "super_payable": ["826"],
                      "wages_payable": ["804"], "leave_provision": ["805"]},
 "payroll_register": "payroll/employees.csv"}
```

缺这个文件时的默认值 (Numbat): 上期与本期等长 (损益类照比, Numbat 结果不变); 录入时间可信; 没有冲回格式 (R11 不适用); counterparty_prefixes 同上; audit_adjustment_entry_prefix "AJE-"; 没有薪酬科目对照 (F03 至 F05 记 not_applicable); data_as_of 不设上限.

程序参数用 `{ref: dataset.<键>}` 引用, 由 procedure_library 解析 (G2 JR-2 的画像引用同一机制).

### 5.3 GEN-05 全量分录扫描 (决定 13 已定)

用户原话: "测试数据加上不可靠跳过, 真实数据应该需要检查". 即: 数据集声明 `entry_timestamps_reliable: false` 时 (Xero 演示数据与由它生成的测试副本) 跳过看录入时间的规则; 没有声明或声明为真时 (真实客户数据, Numbat) 照常执行. 另加 4 条不看时间的通用规则 R09 至 R12.

**通用约定**
- 凭证级规则每笔凭证一条例外: source_id 取该凭证内绝对值最大的行, 并列取 ID 最小的行; amount_cents 取这一行的绝对值. 例外 ID 按日期加分录号排序.
- "作废对" 的识别: 凭证任一行说明 (description, 或载入器写入的 Reference) 匹配 `reversal_reference_pattern`, 取出编号 n, 原凭证为 `reversal_entry_no_template` 填入 n; 原凭证与冲回凭证都算作废对. 不依赖 is_adjustment 标志 (XJ-400 是 XJ-401 的冲回, 却不是调整分录).
- 金额门槛: 超过明显微小 (1622 分).

**看录入时间的规则, 数据集声明 `entry_timestamps_reliable: false` 时自动跳过**, 运行记录写 skipped 与理由 (同 R54 not_executable 的写法):
- R01 周末录入的重大收入, R08 非工作时间录入, 期末后补记 (录入日晚于期末而业务日期在期内), R06 审计调整期间的其他分录.
- 跳过的理由由数据给出: 本期有效分录 149 笔中, 悉尼时间周末录入 112 笔, 非工作时间 33 笔, 录入日晚于 09-30 的 137 笔; 另有 15 笔录入时间晚于 10-19 (XJ-434, 453, 464, 466, 501, 503, 535, 538, 543, 550, 557, 562, 577, 690, 755, 最晚 2026-11-26), 一旦写入沟通时间, R06 会全部命中.

**照常执行的原有规则**: R02 整数金额 0 条; R03 恰低于审批限额 (5,000) 的付款 0 条; R05 期末前三日冲销 0 条; R04 (approved_by 全空) 与 R07 (created_by 只有 "xero") 记 not_executable.

**新增四条不看时间的通用规则** (决定 13; 依据 ASA 240 第 33(a) 段与 A44. A44 原文列出 "nonstandard journal entries" 与 "processed outside the normal course of business", 可直接支撑 R09; R10, R11, R12 是据此自定的口径, 不是 A44 原文所列)

| 规则 | 口径 | 本期命中 |
|---|---|---|
| R09 非标准分录 | is_adjustment 为真, 剔除作废对, 剔除 entry_no 以 `audit_adjustment_entry_prefix` 开头的审计更正分录 (由 FIN-09 验证, 不当舞弊信号) | X-GEN-05-1-R09-1: XJ-397-3, 1921221 (转换余额, 07-16); X-GEN-05-1-R09-2: XJ-499-2, 82500 (手工折旧, 08-16). XJ-539 (0.09) 低于明显微小; XJ-398/399, 495/498, 400/401 是作废对 |
| R10 收入全额冲减 | 同一凭证借记收入类, 贷记应收类, 不涉银行科目, 剔除作废对; 应收贷方金额等于开票日不晚于它的某张本期销售发票全额. 系统没有贷项分配数据, 只能按金额匹配, 可能配到别的同额发票, 由 GEN-02 判断 | X-GEN-05-1-R10-1: XJ-430-2, 55000 (CR1003, 07-26); X-GEN-05-1-R10-2: XJ-449-2, 55000 (CR1005, 07-31). CR1001 (26.40), CR1014 (385), ORC1037 (60.50) 是部分冲减, 不命中 |
| R11 作废后重记且对方名称改变 | 对每个本期作废对的原凭证, 在未作废的凭证中找重记: 同一业务日期, 同一组 (科目, 金额) 行. 第一步: 若有规范化名称相同的重记, 视为正常更正, 不命中. 第二步: 没有时取号码最接近原凭证的一笔, 名称不同即命中. 名称取 source_doc; 规范化 = 转小写, 去掉开头的 counterparty_prefixes, 非字母数字换成空格, 去首尾空格. 重记的号码可以小于冲回凭证甚至小于原凭证 (如 572 → 562) | X-GEN-05-1-R11-1: XJ-575-1, 29695 (原 XJ-560 "Odette Grainger" 改为 "Odette Garrison"); X-GEN-05-1-R11-2: XJ-601-1, 350000 (原 XJ-599 改了转账说明). XJ-585 (原 XJ-582, "NAB" 改 "Bank", 10.50) 低于明显微小. 第一步消除了 429/432 与 442/444 的错配 |

**第四条: 疑似重复开票** (决定 13 已定采用; 也是注入测试 T2 的检出规则)

| 规则 | 口径 | 本期命中 |
|---|---|---|
| R12 疑似重复开票 | sales_invoices 中同一客户, 同一开票日, 同一含税金额的本期发票不少于 2 张, 且其中有发票没有发运或服务完成记录; 例外指向没有记录的那张发票的 journal_entry_id. detail 带 `paired_invoices` (同组其余发票的 SI- ID 与 journal_entry_id) 与 `full_credits` (该客户在开票日至期末之间, 未作废, 贷记应收类且金额等于该发票全额的凭证号; 客户按 C01 的名称规范化与分录行 description 匹配), 供 GEN-02 上下文取行 | X-GEN-05-1-R12-1: XJ-405-2, 55000 (ORC1003 与 ORC1008 同为 Hamilton Smith 07-17 的 550, ORC1003 无服务记录). detail: paired_invoices [SI-ORC1008 / XJ-408-2], full_credits [XJ-430]. Numbat 同日同额组有 56 组, 但 624 张发票都有发运单, 命中 0 |

GEN-05 合计 7 条. Numbat 现有数据上 R09 至 R12 都是 0.

**增量扫描**: AJE-X01 至 X05 接入后执行增量实例 (第 1 轮 X01, X05; 第 2 轮 X02 至 X04). R09 剔除 AJE- 前缀; R02 (10,000 整数倍), R03 (4,750 至 5,000 的付款), R05 (说明不含 revers) 均不命中 (X05 为 48.53, 不是付款); 增量扫描 0 条.

### 5.4 GEN-07 期后监控 (G3 草稿按 5.1 原则改写)

窗口: (period_end, min(period_end + subsequent_days, data_as_of)], 即 10-01 至 10-17.

| 规则 | 口径 | 命中 |
|---|---|---|
| S01 (改写) 期后未入账流水 | 窗口内对账单行, 金额绝对值超过整体重要性, 且总账同一银行科目在 ±10 天内没有同额的行 (剔除作废对). 原 G3 口径 (超过明显微小, 参考号不以 Numbat 前缀开头) 在 Xero 上命中 47 条 | 6 条: BT-090-0092 (618750), BT-090-0097 (-450000), BT-090-0099 (200000), BT-090-0107 (100000), BT-090-0130 (150000), BT-090-0131 (45000) |
| S02 (改写) 期后非常规分录 | 窗口内不涉银行科目的分录, 剔除作废对, 剔除常规的赊销过账 (借应收, 贷收入类) 与赊购过账 (贷应付类), 剔除各科目净额都为 0 的分录; 每笔取资产或负债科目中绝对值最大的行, 超过明显微小. 原 G3 口径在 Xero 上命中 33 条 (31 条是常规赊销赊购) | 1 条: XJ-720-2 (1026C, 115500). XJ-756 (存货期初, 各行净额为 0) 不命中 |
| S03 (新增) 期后资本性支出 | 窗口内借记 property_plant_equipment 分类科目, 单行超过整体重要性 | 2 条: XJ-626-2 (630000), XJ-676-2 (196999) |
| S04 (暂缓) 期末应收到截止日仍未收回 | 需要收款与发票的对应关系; 现有表没有 (收款分录的 source_doc 有 "Payment: 客户名" 与 "Payment from 客户名" 两种, 都不带发票号). Xero 的 payments.json 有分配记录, 待载入器提供收款对应表后再加 | — |

GEN-07 合计 9 条. 对 Numbat 的影响: S02 改写后 G3 的 GJ-202607-ECL-2 (借 6500, 贷 1150) 仍会命中; S01 改写后, G3 设计的期后股利付款 BT-01-20260729-01 只有在总账未记这笔付款时才命中, 由需求清单对应条处理.

### 5.5 FIN-15 期末总体复核 (按 5.1 原则改写)

- **F01**: 与 PLN-07 A01 同一口径 (资产负债类对期初, 门槛 = 收入 × 0.5% 且不小于期初的 20%; 期间长度相同时才比损益类, 上期长度按 dataset.prior_period_start 算), 取数为 "试算表加已验证更正分录". 重跑时只对余额自上一轮以来有变化或新出现的科目出例外 (规格书 "无关结果不重算").
  - 第 1 轮 (加 AJE-X01 与 AJE-X05): 7 条, 090 -219724, 091 1158123, 610 1019950, 800 -400516, 820 -205817, 825 -753500, 840 -1921221. AJE-X05 使 620 新出现 4853, 低于门槛 16216 (收入 3243100 × 0.5%), 不出例外; 270 是损益类, 本期与上期长度不同, 不比.
  - 第 2 轮 (再加 AJE-X02 至 X04): 新出现 3 条, 804 -201000, 805 -268736, 826 -426120.
  - Numbat: 两期都是 12 个月, 损益类照比, 仍只命中 1100.
- **F02**: 本期末 3 个月收入与应收变动: 7 月 460000 / 286000, 8 月 1803600 / 624500, 9 月 979500 / 109450.
- **F03 至 F05** (只在数据集声明了 payroll_accounts 时执行, 否则 not_applicable; 参数 sg_rate 0.12, leave_weeks 4 待核 T2). 每条都是 "应有金额减已计提余额", 差额超过明显微小才出例外; 负债科目余额取贷方为正 (若不扣已计提余额, 第 2 轮会重复认定):
  - F04 应计工资差额 = 应计工资 - wages_payable (804) 余额. 应计工资 = 末次发薪截至日 (09-27) 到期末的工作日 / 每期工作日 × 每期毛工资 (payroll/employees.csv) = 3/10 × 6,700 = 2,010.00.
  - F05 年假差额 = 应计年假 - leave_provision (805) 余额. 应计年假 = 在职员工入职日到期末天数 / 7 × leave_weeks / 52 × 周薪 = 73/7 × 4/52 × 3,350 = 2,687.36.
  - F03 SG 差额 = round((wages_expense (477) 余额 - leave_provision (805) 余额 + max(F04 差额, 0)) × sg_rate) - super_expense (478) 余额. 减 805 余额的前提: 年假计提记入工资费用科目 (AJE-X04 即如此), 年假计提不计 SG; 加 F04 差额是因为尚未入账的应计工资也要计 SG.
  - 第 1 轮 (加 AJE-X01 与 AJE-X05, 薪酬科目都没变; 477 为 33,500.00, 其余为 0): F04 = 2,010.00 - 0 = 2,010.00 → X-FIN-15-1-F04-1, 对象 804, 201000; F05 = 2,687.36 - 0 = 2,687.36 → X-FIN-15-1-F05-1, 对象 805, 268736; F03 = (33,500.00 - 0 + 2,010.00) × 12% - 0 = 4,261.20 → X-FIN-15-1-F03-1, 对象 826, 426120.
  - 第 2 轮 (再加 AJE-X02 至 X04; 477 为 38,197.36, 478 为 4,261.20, 804 为 2,010.00, 805 为 2,687.36): F04 = 2,010.00 - 2,010.00 = 0; F05 = 2,687.36 - 2,687.36 = 0; F03 = (38,197.36 - 2,687.36 + 0) × 12% - 4,261.20 = 0. 三条都为 0, 不出例外 (按原公式字面算第 2 轮 F03 为 241.20, F04, F05 仍为 2,010.00 与 2,687.36, 会重复认定).

### 5.6 其他规则程序预期命中

| 程序与规则 | 预期结果 |
|---|---|
| ACC-04 | 按 2.5 与 2.6 补造后 abn_active, name_match, company_registered 全部通过; 2 行 external_call |
| ACC-08 T01-T07 | TERMS v1 全部通过 (T07 为缩短财年检查); J-ACC-08-1-TERMS accept |
| PLN-07 A01 | 9 条, medium: X-PLN-07-1-A01-1 至 -9, 依次为 090 -219724, 091 1158123, 610 1019950, 710 75000, 711 -82500, 800 -400516, 820 -198317, 825 -753500, 840 -1921221. 改写前 0 条 |
| PLN-07 A02, A03 | A02 收入 3243100; A03 月度: 收入 460000 / 1803600 / 979500, 费用 331117 / 2441870 / 1792809, 员工费用 0 / 2010000 / 1340000 |
| PLN-01 M01 | revenue 3243100 (候选 16216 至 32431); total_assets 1080529 (10805 至 21611); 税前利润 -1322696, 按 Q18 报 INVALID_VALUE |
| REV-08 R01 | 0 条 (ORC1026 完工日 09-17 在期内) |
| REV-08 R02 | 0 条 (窗口内 6 张都有记录) |
| REV-08 R03 | X-REV-08-1-R03-1: SI-ORC1026, 330000; 收到 385000 (BT-090-0124, 10-14, "1026"), 发票 715000. 差额 = BT-090-0085 的 214500 (10-06, 参考号误写 "1028") + 1026C 的 115500. 其余 4 张 (ORC1020 至 1023) 收款全额相符, ORC1024 参考号为空不参与 |
| REV-02 S01 | 选中 6 张 (含税都不小于 16216): ORC1020 至 1024, ORC1026 |
| CSH-05 B01 | 0 条: BT-090-0072 余额 -1090044, BT-091-0001 余额 1158123, 都等于试算表 |
| CSH-02 D01 | 两户 difference 0, agreed. D02 (10-01 至 10-10, 090 共 21 行, BT-090-0073 至 0093) 与 D03 (09-25 至 09-30) 的行会生成; BA-090 不调大模型 |
| CSH-02 D04 (新增, 决定 15) | 口径: 回函载明截至期末尚未记入账户的应计利息 (银行欠客户为正, 客户欠银行为负) 绝对值超过明显微小, 且总账在 (期末 - lookback_days, 期末] 内没有金额绝对值与之相等的行 (剔除作废对, 科目不限) → 一条例外, source 为该银行账户, amount_cents 为回函应计利息 (带符号), detail 写搜索区间与匹配到的行 (空). 编号 n 按 bank_account_id 排序. 余额相符时 result 仍为 agreed, 但该账户要调大模型 (always_review 照常建任务). 命中: X-CSH-02-1-D04-1, BA-091, 4853; BA-090 回函为 nil 不命中. Numbat BA-01 回函写 "Accrued interest at 30 June 2026: nil" (抽为 0), BA-02 回函只写 6-30 已入账的利息 1,190.00, 没提应计利息 (抽为空); 两户都不命中. Numbat 现有 DOC-06 fixture 不补新字段 (字段可空, 缺省为空), data/demo 不改, 回归不变 |
| GEN-05 | 7 条 (5.3 节) |
| GEN-08 R01 | X-GEN-08-1-R01-1, high: source_ids [ACN-900043030], via PER-MORGAN-ALEX-19790412. Morgan Advisory 与申报清单匹配, 不出例外. OWNERSHIP 节点 6: ACN-900043021, ACN-900043030, ACN-900043049, PER-MORGAN-ALEX-19790412, PER-MORGAN-DANA-19811103, PER-WHITFIELD-SAM-NODOB (2.5 节); 关系 10: Demo 4 (Alex 董事, Alex 秘书, Alex 持 85, Sam 持 15), VPA 4 (Dana 与 Alex 各任董事, 各持 50), Morgan Advisory 2 (Alex 董事, 持 100). external_call 5 行 (ABN 1, 公司摘录 3, 人员检索 1) |
| GEN-07 | 9 条 (5.4 节) |
| FIN-15 | 第 1 轮 10 条, 第 2 轮 3 条 (5.5 节) |
| PLN-19 P01 (两次) | 5 笔更正都不碰收入分类, 基数 32431 不变, change 0, 不出例外 |
| FIN-09 V01 | 只取 entry_no 以 `audit_adjustment_entry_prefix` (AJE-) 开头的分录; 第 1 轮匹配 AJE-X01-1..3 与 AJE-X05-1..2, 第 2 轮匹配 AJE-X02 至 X04 的 6 行 |
| FIN-04 (两次) | SCHEDULE v1 / v2: items 与 excluded_trivial_ids 为空, corrected_ids 依次为 [M-1, M-5] 与 [M-1 至 M-5], total 0, materiality 32431, 候选 unqualified |
| FIN-12 K01, K02 | 全部通过 (6.6 节) |
| FIN-18 W01-W07 | 全部通过, must_disclaim 为假 |
| FIN-10 E01-E05 | E01-E04 通过; E05 attention (GEN-12 未实现); 收费依赖度 0.0020 |

---

## 6 大模型程序预期输出要点

### 6.1 合同与回函抽取

**DOC-04**: 3 个 Contract, 11 条 ContractTerm. 金额与签订日的证据页都是第 1 页.

| 合同 | ABN | amount_cents | 签订日 | 条款 |
|---|---|---:|---|---|
| CON-X01 | null | 650000 | 2026-09-01 | control_transfer 4.1 (第 2 页), payment 5.1-5.2 (第 2 页), termination 7.1-7.2 (第 3 页); 6.1 只是变更须书面同意, 不属表中任何类型 |
| CON-X02 | 19900043058 | null | 2026-06-24 | control_transfer 4.1, payment 5.1-5.2, rebate 6.1, termination 7.1 |
| CON-X03 | 21900043067 | 420000 | 2026-08-10 | control_transfer 4.1, payment 5.1, rebate 6.1, termination 7.1 |

**DOC-06**: C-BA-090 confirmed_balance_cents **-1090044**, C-BA-091 1158123, balance_date 都是 2026-09-30, confirming_party Wattlebrook Bank Limited, receipt_channel direct_email (第 2 页). 风险: "(debit)" 是银行视角, 模型可能抽成正数, 一旦抽错 CSH-02 会算出 21,800.88 的假差额.

应计利息 (决定 15, 抽取结果新增字段, 口径见第 4.4 节): C-BA-090 accrued_interest_cents 0 (回函 "nil"), C-BA-091 **4853** (第 1 页), 各带一条第 1 页的证据指针. 风险: 模型可能把 "in the customer's favour" 抽成负数, 或把 "payable on 16 October" 的 10 月利息全额 97.05 当作应计; fixture 与实时断言都按 4853.

**CSH-02 判断** (决定 15): 1 条, J-CSH-02-1-BA-091, misstatement.
- 上下文: bank_account BA-091, confirmation C-BA-091 (余额 agreed, accrued_interest_cents 4853), 回函 document, book_balance_cents 1158123, difference_cents 0, subsequent_transactions 为空 (D02 窗口 10-01 至 10-10 没有 091 的流水; 10-16 的 BT-091-0002 在窗口外), period_end_entries 为 BA-091 的 D03 行 (09-25 至 09-30 只有 XJ-601 的 091 行), rule_findings [X-CSH-02-1-D04-1].
- 结论: 银行确认 09-30 已产生, 尚未记入账户的利息 48.53, 期末前 5 天内总账没有同额的计提, 利息收入与应收少记; 48.53 高于明显微小 16.22, 依 ASA 450 第 5 段须累计 (原文已核). 草稿 {factual, Interest income (270), Completeness, 4853, understated}; 4853 等于归因例外金额 (Q12 口径).
- 证据: 回函 document (accrued_interest_cents, 第 1 页), trial_balance 091, 例外 X-CSH-02-1-D04-1.
- 审计师决定 misstatement → **M-5**. 因 always_review 建复核任务 T-J-CSH-02-1-BA-091-v1.

### 6.2 收入程序 (REV-03, REV-02)

**REV-03**: 3 条判断, 都是 accept.
- J-REV-03-1-CON-X01: 控制权在完工日 (完工证书签署) 转移, 某一时点确认 (第 38 段; 7.2 使第 35(b)(c) 段不满足).
- CON-X02, CON-X03: 服务随履行转移 (第 35(a) 段); 6.1 的贷项属可变对价.

**REV-02**: 6 条判断, 都是 accept.
- J-REV-02-1-SI-ORC1026: 完工记录 SR-ORC1026 签于 09-17, 合同 4.1 约定完工即转移, 9 月确认正确. R03-1 只说明期后窗口内按发票号匹配到的收款 (BT-090-0124, 3,850) 少于发票额 7,150; 差额是收款与对账问题, 不改变控制权转移日, 不构成截止错报; 不要求解释差额的构成. exception_ids [X-REV-08-1-R03-1], 确认后 R03-1 置为 attributed. 证据只引上下文里有的行: 发票, XJ-580-1..3, SR-ORC1026, CON-X01 第 2 页, BT-090-0124 (R03-1 的 detail).
  - 不引 BT-090-0085 与 XJ-720-2: `rev02_subjects` 的上下文只有发票, 服务记录, 合同, REV-03 判断, 该发票的例外与 (本版新增的) 发票分录行; 实时模型看不到这两行, 预期写成引用它们的话 T0 实时跑法不可复现. 差额 3,300 = BT-090-0085 的 2,145 (参考号误写 "1028", Xero payments.json 记为 ORC1026 的 Part 1) 加 1026C 的 1,155, 这是设计者的核对 (5.6 节), 不是模型的预期输出; 1026C 由 GEN-06 处理.
- ORC1020: 9 月服务 09-30 完成, CON-X02.
- ORC1021 至 1024: 没有挂合同, 按服务完成记录判断 (月费 09-01 至 09-30; ORC1024 培训 08-19).

### 6.3 分录与关联方 (GEN-02, GEN-03, GEN-01)

**GEN-02**: 7 条判断. G2 草稿 "分录例外归因不能建议认定错报" 的限制, 对没有科目程序覆盖的分录放开 (XJ-499 要用).

| 判断 | 建议 | 要点 |
|---|---|---|
| J-GEN-02-1-XJ-397 | accept | 银行两侧与对账单期初一致 (090: -8,703.20 + 4,130.98 = -4,572.22; 091: 15,081.23); 840 为董事借款, 有 2018 年借款协议与 10-16 确认函 (决定 8); 0630 比较数漏记的余额转 FIN-12 比较数检查 |
| J-GEN-02-1-XJ-405 (R12) | accept | ORC1003 与 ORC1008 重复, ORC1003 已由 CR1003 在 07-26 全额冲销; ORC1008 有 7 月服务记录并已收款. 上下文按 R12 detail 取行: paired_invoices 的 XJ-408 全部行与 SR-ORC1008, full_credits 的 XJ-430 全部行 (GEN-02 上下文要加这两项) |
| J-GEN-02-1-XJ-430 | accept | CR1003 说明是重复开票, 全额冲销 |
| J-GEN-02-1-XJ-449 | accept | ORC1005 当天全额冲销, 已并入 ORC1006 (3 × 450) |
| J-GEN-02-1-XJ-499 | misstatement | 08-16 时 710 余额为 0, 固定资产登记表截至 09-30 为 Nil, 这张分录没有依据, 还对折旧抵扣了进项税. 草稿 {factual, Office equipment (710), Existence, 75000, overstated}; 说明写明 711 多记 825, 820 少记 75, 不影响损益与净资产; 75000 是 XJ-499-1 的金额 |
| J-GEN-02-1-XJ-575 | more_evidence | "Grainger" 不在往来单位和工资名单; 银行只有批量付款 BT-090-0052 (889.80); 最坏情况下费用多记 270.64, 进项税多抵 26.31 |
| J-GEN-02-1-XJ-601 | accept | 091 转 090 共 3,500, 科目金额没变, 只把 Demo 的占位银行名换成账户名; 两边对账单 BT-090-0065 与 BT-091-0001 对应; fixture 写明两户同在 Wattlebrook |

**GEN-03**
- J-GEN-03-1-ACN-900043030 (VPA): more_evidence. 关联关系: 能控制本公司的 Alex 任 VPA 董事并持股 50% (第 2 层下的依据段号暂缓, 待 AASB 1060 正本; AASB 124 第 9(b)(vii) 段只作参照). 本期交易 (改写后的 C01 取到, 5.1 节): 房租 4,500 / 含税 4,950 (XJ-420, 518, 593; BT-090-0004, 0033, 0058); 无书面租约, 只有租金函 (上下文按 related_ids 取到租金函, 出售与终止通知, 新租约); 比新租约租金高 39.7%. 后续程序 ASA 550 第 22(b)(c)(d) 段; 是否属 (e) 项故意不申报需评估. 属正常经营, 不触发第 23 段.
- J-GEN-03-1-ACN-900043049 (Morgan Advisory): accept.

**GEN-01** (J-GEN-01-1-CLIENT-XERO-3C1332BB-Q1, L1): more_evidence, 申报清单漏了 VPA.
- 第 26 段书面声明交 FIN-07.
- 第 27 段原文是 "Unless all of those charged with governance are involved in managing the entity": 本公司唯一董事兼任管理层, 这个 "除非" 条件成立, 所以第 27 段的沟通要求不适用.
- 管理层不补披露时依 Aus 27.2 修改意见.
- 规格书第 11 节把 "发现未披露关联方" 列为环 3 阻断条件; Demo需求文档第 6 节本版不做环 3. 按 G2 推荐, 在 FIN-01 闸门材料里只读列出关联方完整性结论.

### 6.4 期后事项与持续经营 (GEN-06, GEN-04, FIN-02)

**GEN-06 上下文的通用扩展**: 每个对象除 G3 设计的 "点名客户的期末未收发票" 外, 再给出与流水参考号或对方名称相关的总账行与已登记文件. 参考号拆成长度不小于 4 的数字串或字母数字串, 在 source_doc 与 description 中按整词匹配; 名称按 R11 的规范化规则匹配. 这条扩展对注入测试 T3 是必需的.

GEN-06: 9 条判断, 演示决定都是 confirm, **不产生新错报**.

| 判断 | 结论 |
|---|---|
| J-GEN-06-1-BT-090-0092 | Ridgeway University 支付 ORC01025 (10-06 开票), 订单 CRM08-12 写明 10 月起驻场, 属 10 月交易, accept |
| J-GEN-06-1-BT-090-0097 | 4,500 付清 SM0195-70135 (09-05 入账, 09-30 已列应付 2,000) 与 SM0210-70209 (10-06 入账的 10 月账单 2,500); 总账未记付款属期后记账滞后, 不影响 09-30, accept |
| J-GEN-06-1-BT-090-0099 | Jakaranda 新项目订金, 10 月产生合同负债, 不影响 09-30, accept |
| J-GEN-06-1-BT-090-0107, BT-090-0130 | 董事 10 月转入的借款 (支持函载明), 非调整事项; 支持持续经营判断, accept |
| J-GEN-06-1-BT-090-0131 | 新租约下的分租收入, 10 月起, accept |
| J-GEN-06-1-XJ-720-2 | (决定 14 已定) 1026C 是 10-13 商定的价格让步 (DIISR 邮件), 完工与验收都在 09-17, 09-30 不存在导致让步的情况; 按 AASB 15 第 88 段在价格变动的期间 (10 月) 冲减收入, 属 AASB 110 第 3(b), 10 段的非调整事项, accept |
| J-GEN-06-1-XJ-626-2, XJ-676-2 | 非调整事项 (AASB 110 第 3(b), 10 段), 附注 4 已披露 (披露段号暂缓, 待 AASB 1060 正本) |

**GEN-04** (J-GEN-04-1-CLIENT-XERO-3C1332BB-Q1): accept.
- 依据 ASA 570 A3 识别的事项: 净负债, 流动负债超过流动资产 (未调整比率 0.5084), 经营亏损, 经营现金流为负, 09-30 已逾期的应付 3 张共 3,283.56 (Central Copiers 945-ORC 1,063.56 到期 09-15, SMART Agency SM0195-70135 2,000.00 到期 09-19, MCO Cleaning M000442 220.00 到期 09-27; 应付 800 明细合计 4,005.16, 其余 3 张 09-30 未到期), PAYG 7,535 未缴.
- 缓解因素: 董事支持函, 10 月董事已转入 2,500, 透支额度余量 4,099.56, 10 月扭亏.
- 限制: 董事的支付能力没有证据.
- 引用 ASA 570 第 10, 16 段, Aus 13.2, A13.

**FIN-02**: GOING_CONCERN v1, 建议 material_uncertainty_disclosed, covered_to 2026-10-17; 取更正后数字 (亏损 22,136.99, 净流动负债 30,048.99); 依据 ASA 570 第 18, 19, 22 段; 10-18 至 10-23 只靠询问与书面声明覆盖 (ASA 560 第 7(b), 9 段).

### 6.5 错报闭环 (FIN-03, FIN-09) 与总体复核 (FIN-15)

**FIN-03**: 第 1 轮 M-1 factual, 本期产生, 无舞弊迹象, 另注 "手工分录无复核, 属控制缺陷线索"; M-5 factual, 本期产生, 无舞弊迹象 (期末未计提应计利息); 第 2 轮 M-2, M-3 factual, M-4 judgmental.

**FIN-09**: 每条错报 1 条判断. 按来源对象与科目匹配 AJE 行, 剩余额为 0 → corrected.

| 错报 | 来源 | 草稿 | 更正 |
|---|---|---|---|
| M-1 | J-GEN-02-1-XJ-499 | {factual, Office equipment (710), Existence, 75000, overstated} | AJE-X01 贷 710 75000; other_accounts [711, 820] |
| M-2 | J-FIN-15-1-826 | {factual, Superannuation payable (826), Completeness, 426120, understated} | AJE-X02 贷 826 426120 |
| M-3 | J-FIN-15-1-804 | {factual, Wages payable (804), Completeness, 201000, understated} | AJE-X03 贷 804 201000 |
| M-4 | J-FIN-15-1-805 | {judgmental, Annual leave (805), Completeness, 268736, understated} | AJE-X04 贷 805 268736 |
| M-5 | J-CSH-02-1-BA-091 | {factual, Interest income (270), Completeness, 4853, understated} | AJE-X05 贷 270 4853; other_accounts [620] |

M-1 是毛额口径: 报表层面设备净额与 GST 各差 75, 净资产与损益影响为 0. 错报汇总表合计 9,757.09 加注 "M-1 按成本毛额列示, 对净资产无影响".

**FIN-15 判断**: 第 1 轮 10 条 (F01 7 条, F03 至 F05 3 条), 第 2 轮 3 条, 共 13 条.
- F01 各科目都 accept, 由 P2 的证据解释: 090 透支扩大 (经营亏损, 持续经营已评价); 091 与 840 来自转换余额 (董事借款); 610 为 9 月发票, 10 月收回; 800 与 820 为正常应付与本季 GST; 825 PAYG 10-28 到期; 第 2 轮 804, 805, 826 由已更正错报解释.
- F03 至 F05 建议认定错报, 审计师认定后成为 M-2 至 M-4.

### 6.6 计划, 重估, 勾稽与报告

**PLN-15**: PLAN-1 v1 (draft) → v2 approved. 装配 CSH-02 (period_end 2026-09-30, subsequent_days 10, lookback_days 5), CSH-04 (as_of 2026-09-30), CSH-05, DOC-06, REV-02 (window 20, threshold_cents 16216), REV-03, REV-08 (window 20), 以及新程序固定集. 叙述: 范围写明 s323D(2A) 与 s293; 首次审计 (ASA 300 第 13 段, ASA 510 第 6 段, ASA 710 第 14 段); 管理层凌驾为特别风险 (ASA 240 第 32, 33(a) 段), 唯一董事兼记账人; 持续经营迹象 (ASA 570 第 10 段); 2.7 的其余重大科目; 送达股东不晚于 2027-01-31. eqr_required 为 false (ASQM 1 第 34(f) 段).

**PLN-19** (两次): 基数不变, J-PLN-19-1-MATERIALITY 与 J-PLN-19-2-MATERIALITY 都是 accept (revise 假); 闸门 approve 不带 materiality, 计划不出新版本.

**FIN-12**: 勾稽对象是冻结的 fs_v1.
- K01: 每行报表金额等于 "09-30 试算表加已验证更正分录" 的映射科目之和.
- K02: 映射覆盖 09-30 试算表的 27 个科目 (含余额为 0 的 801) 加 804, 805, 826, 478, 620, 270, 共 33 个.
- 报表内部: 资产 21,829.26 = 负债 51,878.25 + 权益 (30,048.99); 期初累计亏损 7,912.00 加本期 22,136.99 = 30,048.99.
- 外部核对: 现金与透支等于回函余额; 其他应收 48.53 等于 BA-091 回函的应计利息; 应收 10,199.50 = 未结发票合计 (1026C 属 10 月, 不冲减).
- 比较数等于 "0630 表加 XJ-397"; 若直接比 0630 表, 090, 091, 840 会分别差 4,130.98, 15,081.23, 19,212.21, 另设一条带解释的比较数检查.

**RPT-03**: REPORT-1, 无保留意见, 依 s308(1).
- 持续经营段排在形成意见的基础段之后: 指向附注 2; 写明本期亏损 22,136.99, 流动负债超过流动资产 30,048.99; 写明依赖董事不催收 19,212.21 的借款并提供资金, 以及透支额度; 意见未因此修改.
- 其他事项段: 截至 2026-06-30 年度的比较数未经审计 (ASA 710 第 14 段). 报告结构目前没有这一段, 需要新增.
- 签署: Jordan Ellis, Sydney, 2026-10-23.

**RPT-08**: 依 s307C(3)(c) 出具无违规声明, 同时交给董事 (s307C(5)(a)(i)), 附入董事报告 (s298(1AA)(c)).

**GEN-09** (独立复核) 与 **GEN-10 / GEN-11**: 不进计数.

---

## 7 演示剧本

### 7.1 审计师的选择与降级

- ACC-01 approve; ACC-08 approve (G5: 单独闸门, 依赖 ACC-01, 批准后进 P1); PLN-01/03 approve, materiality = {revenue, 0.01, 0.5, 0.05}; PLN-15 approve; 两次 PLN-19 approve 不修订; FIN-02 approve material_uncertainty_disclosed; FIN-01 approve unqualified; RPT-01 先退回一次再批准; 签字注入时间 2026-10-23T05:00Z.
- 动态降级 (需求清单 R168 的降级规则; 首次服务该客户不降级, 按决定 2 本版不做): 收入特别风险 -2, 现金 high -1; REV-02, REV-03 触到 L1 下限并开盲判. 执行重要性只有 162.16, 带金额的判断几乎都按 "单项金额超过执行重要性" 降到 L1.

### 7.2 剧本 (顺序按规格书 1425 行: 更正验证 → PLN-19 → FIN-04 → FIN-15)

**更正传导**: 本版不实现规格书 1427 行的 "更正后试算表版本加一, 依赖科目的已完成程序标 needs_review" (G4 范围外). 系统试算表不随更正改版; FIN-12, FIN-15, FIN-02 按 "试算表加已验证更正分录" 取数. 若实现, AJE-X01 会使 PLN-07 (710, 711, 820), PLN-01 (total_assets), GEN-04 标 needs_review, AJE-X02 至 X04 会使 PLN-01 (税前利润), GEN-04 标 needs_review; 这些重跑不计入下表.

| # | 日期 | 程序 | 产出 | 例外 | 复核任务与演示决定 |
|---|---|---|---|---|---|
| 1 | — | ACC-04 | SCORECARD v1 三项通过; 2 行 external_call | 无 | 无 |
| 2 | — | ACC-01 | 闸门 | — | approve → p0_terms (阶段仍为 P0, G5) |
| 3 | — | ACC-08 | TERMS v1 T01-T07 通过; J-ACC-08-1-TERMS accept | 无 | ACC-08 闸门 approve → P1 (G5 单列闸门, depends_on [ACC-01]) |
| 4 | — | DOC-04 | 3 个 Contract, 11 条 ContractTerm | 无 | 无 |
| 5 | — | PLN-07 | ANALYTICS v1 (A01, A02, A03) | A01-1 至 -9 (open, 闸门材料) | 无 |
| 6 | — | PLN-01/03 | MATERIALITY v1; 风险清单 3 条 | — | approve → 32431 / 16216 / 1622 |
| 7 | — | PLN-15 | PLAN-1 v1 → v2 | — | approve → 装配 P2 |
| 8 | — | REV-08 | 窗口 09-10 至 09-30 | R03-1 | 无; 由 REV-02 归因 |
| 9 | — | REV-03 | 3 条判断 accept | 无 | 3 条 (盲判), 全部 confirm |
| 10 | — | REV-02 | 6 条判断 accept | — | 6 条 (盲判), 全部 confirm; R03-1 → attributed |
| 11 | — | CSH-04, DOC-06, CSH-05, CSH-02 | C-BA-090, C-BA-091 余额 agreed; C-BA-091 回函应计利息 4853 | D04-1 (决定 15) | 1 条 (always_review), J-CSH-02-1-BA-091 决定 misstatement → **M-5** (48.53) |
| 12 | — | GEN-05 | RUN-GEN-05-1; 4 类时间规则 skipped, R04/R07 not_executable | 7 条 | 无 |
| 13 | — | GEN-02 | 7 条判断 | 归因上述 7 条 | XJ-499 决定 misstatement → **M-1**; XJ-575 审计师调阅报销单 E003 后 override 为 accept (录名错误); 其余 5 条 confirm |
| 14 | — | GEN-08 | OWNERSHIP v1: 6 个节点, 10 条关系; 5 行 external_call; 4 份 registry_extract | R01-1 | 无 |
| 15 | — | GEN-03 | 2 条判断 | 归因 GEN-08 R01-1 | 2 条 confirm |
| 16 | — | GEN-07 | RUN-GEN-07-1 (L4) | 9 条 | 无 |
| 17 | — | GEN-04 | J-GEN-04-1 | — | 1 条 confirm |
| 18 | — | GEN-06 | 9 条判断 | 归因上述 9 条 | 9 条 (L1), 全部 confirm |
| 19 | — | GEN-01 | J-GEN-01-1 (L1) | — | 1 条 confirm; 管理层同意在附注 3 补披露 VPA |
| 20 | 10-19 | FIN-03 第 1 轮 | M-1, M-5 分类 | — | 2 条 confirm |
| 21 | 10-19/20 | FIN-08 第 1 轮 | 人工队列 1 项 (两条错报) | — | 登记 10-20 回复 "同意" |
| 22 | 10-20 | 更正通知 1 | 载入 AJE-X01 与 AJE-X05; M-1, M-5 → verifying; GEN-05 增量扫描 | 0 条 | — |
| 23 | 10-21 | FIN-09 第 1 轮 | 2 条判断 | — | 2 条 confirm → M-1, M-5 corrected |
| 24 | 10-21 | PLN-19 第 1 次 | REASSESSMENT v1, change 0 (AJE-X05 贷 270, 属 other_income, 不进收入基准) | 无 | 闸门 approve, 不修订 |
| 25 | 10-21 | FIN-04 第 1 次 | SCHEDULE v1: corrected [M-1, M-5], total 0, unqualified | 无 | 无 |
| 26 | 10-21 | FIN-15 第 1 轮 | 10 条判断 | F01-1 至 -7, F03-1, F04-1, F05-1 | F01 7 条 confirm; F03 至 F05 决定 misstatement → **M-2** (4,261.20), **M-3** (2,010.00), **M-4** (2,687.36) |
| 27 | 10-21 | FIN-03 第 2 轮 | M-2 至 M-4 分类 | — | 3 条 confirm |
| 28 | 10-21/22 | FIN-08 第 2 轮 | 人工队列 1 项 | — | 登记 10-22 回复 "同意" |
| 29 | 10-22 | 更正通知 2 | 载入 AJE-X02 至 X04; GEN-05 增量扫描 | 0 条 | — |
| 30 | 10-22 | FIN-09 第 2 轮 | 3 条判断 | — | 3 条 confirm → M-2 至 M-4 corrected |
| 31 | 10-22 | PLN-19 第 2 次 | REASSESSMENT v2, change 0 | 无 | 闸门 approve, 不修订 |
| 32 | 10-22 | FIN-04 第 2 次 | SCHEDULE v2: corrected [M-1 至 M-5], total 0, unqualified | 无 | 无 |
| 33 | 10-22 | FIN-15 第 2 轮 | 3 条判断 | F01 (804, 805, 826) | 3 条 confirm |
| 34 | 10-22 | FIN-12 | CHECK-FIN-12 全部通过 | 无 | 无 |
| 35 | 10-22 | FIN-02 | GOING_CONCERN v1 | — | 闸门 approve material_uncertainty_disclosed. 理由: "Events or conditions indicate a material uncertainty that may cast significant doubt on the Company's ability to continue as a going concern; the going concern basis is appropriate given the director's undertaking, and Note 2 adequately discloses the uncertainty." |
| 36 | 10-22 | FIN-01 | 呈递 SCHEDULE v2 与关联方完整性结论 | — | approve unqualified. 理由: "All five misstatements communicated (AUD 9,757.09 gross) were corrected by management; no uncorrected misstatements remain." |
| 37 | 10-23 | FIN-07, FIN-18, FIN-10 | REPLETTER v1 (obtained, 10-23, Alex Morgan, 附表空); CHECK-FIN-18, CHECK-FIN-10 通过 | — | FIN-07 人工队列 1 项 |
| 38 | 10-22/23 | RPT-03 | REPORT-1 | 无 | 无 |
| 39 | 10-22/23 | RPT-08, RPT-07 | S307C v1 → 签字时 v2; GC-RPT-07-1 (10-22, 书面; 议程 SIG_PRACTICES, DIFFICULTIES, REPORT_FORM, INDEPENDENCE; 已更正错报 5 条; 无 UNCORRECTED 项) | — | RPT-07 人工队列 1 项 |
| 40 | 10-23 | RPT-01 | 第一次 reject, 批注挂在 going_concern 段: "State the amount by which current liabilities exceed current assets and refer to Note 2." → REPORT-2; 第二次 approve | — | REPORT-2 signed, report_date 2026-10-23, 阶段转 Archived |

### 7.3 全部接入后的计数 (不含 GEN-09, 不含更正传导引起的重跑)

| 对象 | 数量与状态 |
|---|---|
| 例外 | **41 条**: PLN-07 9 (open, 闸门材料); REV-08 1, CSH-02 1 (D04, 决定 15), GEN-05 7, GEN-08 1, GEN-07 9, FIN-15 13 (第 1 轮 10, 第 2 轮 3), 均 attributed; 增量扫描 0; PLN-19 0 |
| 判断 | **56 条**: ACC-08 1, REV-03 3, REV-02 6, CSH-02 1, GEN-02 7, GEN-03 2, GEN-01 1, GEN-04 1, GEN-06 9, FIN-03 5, FIN-09 5, FIN-15 13, PLN-19 2. FIN-02 的建议是闸门材料 GOING_CONCERN v1, 不建判断 (设计稿 v2.1 计为 57 条, 含 "FIN-02 1"); 另有人工判断 1 条 (J-GEN-02-1-XJ-575-H1, 审计师 override XJ-575 产生) |
| 复核任务 | **53 条**, 都已决定: REV-03 3, REV-02 6, CSH-02 1, GEN-02 7, GEN-03 2, GEN-01 1, GEN-04 1, GEN-06 9, FIN-03 5, FIN-09 5, FIN-15 13 (盲判恰为 9 条: REV-03 3, REV-02 6) |
| 闸门决定 | **10 次**: ACC-01, ACC-08, PLN-01/03, PLN-15, PLN-19 两次, FIN-02, FIN-01, RPT-01 两次. ACC-08 单列一次 (G5 AP-1: GateDecision.gate 单列 ACC-08, 一次闸门一条决定; 验收顺序 ACC-01 approve → ACC-08 approve) |
| 人工队列 | 4 项: FIN-08 两项, FIN-07, RPT-07 |
| 更正通知 | 2 次 (AJE-X01 与 AJE-X05; AJE-X02 至 X04) |
| Confirmation | C-BA-090, C-BA-091 都是 agreed (余额); C-BA-091 accrued_interest_cents 4853 |
| 错报 | **5 条**, 毛额 9,757.09, 全部 corrected; 未更正 0 |
| 候选意见 / 最终意见 | unqualified / unqualified, 加持续经营重大不确定性段与其他事项段 |
| 报告版本 | 2 个 (REPORT-2 已签) |
| document | 29 份 (第 4 节) |
| external_call | 9 行: ACC-04 2, CSH-04 2, GEN-08 5 |
| 大模型调用 | **约 66 次** (不含 A7 独立复核): DOC-04 3, DOC-06 2, ACC-08 2 (按 G5 定), PLN-15 1, REV-03 3, REV-02 6, CSH-02 1, GEN-02 7, GEN-03 2, GEN-01 1, GEN-04 1, GEN-06 9, FIN-03 5, FIN-09 5, PLN-19 2, FIN-15 13, FIN-02 1, RPT-03 2 |

A7 独立复核 (GEN-09, 已在 main 上运行) 另复核 GEN-06 的 9 条判断 (L3 accept 命中主规则), 每条 2 次, 共 18 次, 全剧本 84 次 (合并终验定值, 共用裁定第 8 批); 其余判断所在程序为 L1 或 L2, 不进 A7, 推理链含假设步骤或按现有抽样公式 (判断 ID 的 sha256, 抽样率 0.05) 抽中的判断也因此不复核. 若实现改变 A7 的触发或程序的自主度, 同一提交同步本节.

**只开现有 17 条时** (新程序都关, 第 5.1 节改动与 CSH-02 D04 已做)
- PLN-07 9 条例外, REV-08 1 条, CSH-02 D04 1 条; 判断 10 条 (REV-03 3, REV-02 6 accept; CSH-02 1 misstatement); 复核任务 10 条.
- 错报 1 条 (M-5, 48.53, 低于整体重要性 32,431, 未更正; 本路径没有错报闭环) → FIN-04 total 4853, 未超过重要性 → FIN-01 approve unqualified → RPT-03 → RPT-01.
- 这条路径的报告没有持续经营段, 因为持续经营程序 (GEN-04, FIN-02) 没开; 与数据显示的持续经营问题不符, 只能作为回归路径, 不作演示结论.

---

## 8 注入错误的测试设计

### 8.1 原则与副本生成

- 只在测试临时目录里改副本 (决定 6). 原快照 `data/external/xero/api/20261017-q1/` 与补造目录 `data/xero_demo/20261017-q1/` 一律只读.
- 测试夹具 `xero_case(tmp_path, scenario)`:
  1. 对两个原目录逐文件算 sha256, 记下.
  2. 复制, 层级与载入器参数一一对应: 快照复制到 `tmp_path/api/20261017-q1/`, 补造复制到 `tmp_path/xero_demo/20261017-q1/`; 载入器构造为 `XeroDatasetLoader(root=tmp_path/"api", supplements=tmp_path/"xero_demo", ...)` (第 9.3 节第 1 项的显式参数), 不靠目录层级推算.
  3. contacts.json 不原样复制 (决定 12): 在内存里读原文件, 去掉登录账号联系人 (口径同第 9.4 节: IsCustomer 与 IsSupplier 都为假且有 FirstName 与 EmailAddress, 断言恰好 1 条), 只把过滤版写进副本. 其余 82 个联系人原样保留, 所以 sales_invoices 的 customer 与 customer_abn (Bayside Club 的 TaxNumber 来自 contacts.json) 与原快照逐行相同. 夹具断言副本中任何文件都不含原值 (全名与邮箱整串匹配, 同 9.4 节), 断言失败时只报文件名, 不打印原值. raw/ 目录不复制.
  4. 读场景补丁 `tests/fixtures/xero_injections/<场景>.json`, 逐条执行. 每条补丁写明文件, 定位 (JSON 路径或 CSV 主键), 字段, 改前值, 改后值; 执行前断言当前值等于改前值, 防止快照或补造文件变化后补丁悄悄失效.
  5. 试算表按补丁同步调整: 用辅助函数 `shift_tb(report, code, delta_cents)` 改副本 `trial_balance_2026-09-30.json` 中该科目的 YTD Debit / YTD Credit, 保证副本自身借贷平衡, 且与副本分录推出的 09-30 余额一致 (夹具断言).
  6. 重写副本的补造 manifest: 写 `"injected": "<场景>"` 并更新副本快照哈希 (contacts.json 不在快照哈希清单内, 第 9.2 节); 载入器只在构造参数 `allow_injected=True` 时接受带 injected 标志的目录.
  7. 会话级 autouse 夹具在全部测试结束后重算原目录哈希, 与第 1 步逐文件比对, 不同即失败.
- 补丁文件不写登录账号的姓名与邮箱; 生成补丁的脚本不读 contacts 与 raw.
- 预期断言按对象 ID 与字段比, 不按例外序号比 (删掉一条命中会让后面的序号前移).

**没有本地快照的检出怎么跑**: 本仓库已带上快照, 下面两部分测试都会实际执行; 其他检出 (快照在 `.gitignore` 的 `/data/*` 之下, 没有 `git add -f` 的仓库, CI 或 worktree) 可能没有快照, 补造目录则始终入库. 所以每个场景的测试拆成两部分:
- 需要快照的部分 (副本生成, `build_tables` 的表层断言, 哈希守护, 连库与实时跑法): 没有 `data/external/xero/api/20261017-q1/` 时 `pytest.skip("没有本地 Xero 快照")`, 哈希守护同样跳过.
- 不需要快照的部分 (FakeRuleEngine 预置例外, FakeLLM 读 fixture, 走 Agent, 判断引擎, 四级校验与 Q12 金额校验): 只读入库的 fixture 与补造目录, 默认跑, CI 可跑.
- 从快照裁出最小 fixture 入库本版不做 (见文末 "本版不做").

**三种跑法与各自能证明什么**
- 离线 mock (默认跑): 不连库. 有快照时对副本调 `build_tables` 与补造表的读入, 断言表层事实 (改动的行确实变了, 其余表逐行不变); 再用 FakeRuleEngine 预置该场景的例外, FakeLLM 读 `tests/fixtures/llm_injected/<场景>/<程序>/<对象>.json`, 走 Agent 与判断引擎, 断言判断, 错报草稿, 四级校验与 Q12 金额校验. 检的是编排与校验链路; 判断结论是预写的 fixture, **不证明系统能识别错报**.
- 连库 (`-m slow`, 需 TEST_DATABASE_URL 与本地快照): 副本经 XeroDatasetLoader 载入测试库, 用真实 SqlRuleEngine 跑相关程序, 断言例外 (对象 ID, 金额, 规则号) 与送给大模型的上下文数据; 大模型仍用 fixture. 检的是真实 SQL 与取数能否把注入的事实带到判断面前. 规则层与基线相同的场景 (T2 的 R12, T3 的 S01), 另加有区分力的数据断言 (各场景 (b) 项).
- 真实大模型 (`-m slow` 且 LLM_MODE=live, 需 .env 的 LLM 配置): 同连库, 判断由真实模型给出. 断言 proposed_action 与草稿的认定, 方向, 金额 (金额必须等于预期值, 科目只要求包含预期科目代码或名称), 允许多一次重跑 (同 test_e2e_live 的 RERUN_LIMIT). **识别能力只由这种跑法证明**: 每个场景对同一判断对象做成对断言 (基线 T0 为 accept 或无例外, 注入后为 misstatement 或相应例外), 成对断言是必过项, 不允许只断言注入一侧.

### 8.2 场景一览

| 场景 | 改什么 | 应检出的程序 | 跑法 |
|---|---|---|---|
| T0 基线对照 | 不改 | 第 5 至 7 节的全部结果 | 三种 |
| T1 收入截止 | 服务完成日 09-17 → 10-02 | REV-08 R01, REV-02 | 三种 |
| T2 重复开票未冲销 | 删 CR1003 | GEN-05 R12, GEN-02 | 三种 |
| T3 未记录负债 | 9 月账单改在 10-06 入账 | GEN-07 S01, GEN-06 | 三种 |
| T4 银行回函余额差异 (在途款项) | 10-06 的收款提前到 09-30 记账 | CSH-05 B01, CSH-02 | 三种 |
| T5 折旧错记 | 不注入: 真实数据已有 #499 (基线即正例); 另设反向对照 T5' 删 #499 | GEN-05 R09, GEN-02 | 离线, 连库 |
| T6 关联方交易未披露 | 登记 mock 增加 Alex 任董事的供应商 | GEN-08 R01, GEN-03, GEN-01 | 三种 |

### 8.3 T1 收入截止 (原 ORC1026 跨期场景)

(a) 改动
- `xero_demo/20261017-q1/delivery_notes.csv`, 主键 id = SR-ORC1026: signed_date `2026-09-17` → `2026-10-02`. 其余列与合同 PDF 不改.

(b) 应检出
- REV-08 R01: 新例外, source_type sales_invoice, source_id SI-77b338ef-ecc0-4b95-a0d7-2617b0054611, amount_cents 715000, posting_date 2026-09-17, signed_date 2026-10-02.
- REV-08 R03: 仍为 330000 (不受影响).
- REV-02 J-REV-02-1-SI-77b338ef-…: misstatement, 草稿 {type factual, account 含 "200" 或 "Sales/Revenue", assertion Cutoff, amount_cents 650000, direction overstated}, exception_ids 含 R01 与 R03 两条例外. 650000 是上下文里 XJ-580-1 的金额 (以第 5.1 节 REV-02 上下文加入发票分录行为前提); 证据含 XJ-580-1, SR-ORC1026, CON-X01 第 2 页 4.1.
- 依据: AASB 15 第 38 段 (完工证书即客户验收), 合同 7.2 使第 35 段不满足.

(c) 不该受影响
- REV-08 R02 0 条; REV-02 其余 5 条判断仍 accept; REV-03 3 条判断不变 (合同未改).
- 试算表未改, PLN-07, PLN-01, CSH, GEN-05, GEN-07 的结果与基线逐条相同.

(d) 跑法: 离线 (FakeRuleEngine 预置 R01 与 R03 例外, fixture 给 misstatement); 连库 (断言 R01 由真实 SQL 产出); 真实大模型 (断言 misstatement 与 650000).

### 8.4 T2 重复开票未冲销

(a) 改动 (快照副本)
- `journals.json`: 删除 JournalNumber 430 (CR1003 的过账).
- `credit_notes.json`: CR1003 的 Status `PAID` → `VOIDED`.
- `invoices.json`: ORC1003 的 Status `PAID` → `AUTHORISED`, AmountDue `0.0` → `550.0`.
- `trial_balance_2026-09-30.json`: 200 余额 -3243100 → -3293100; 610 1019950 → 1074950; 820 -119197 → -124197.

(b) 应检出
- GEN-05 R10: 只剩 XJ-449-2 一条 (XJ-430 不再存在).
- GEN-05 R12: source_id XJ-405-2, amount_cents 55000 (与基线相同); detail.full_credits 为 [] (基线为 [XJ-430]), paired_invoices 仍为 [SI-ORC1008]. 这是连库跑法中有区分力的断言.
- GEN-02 对 XJ-405 的上下文中没有 XJ-430 的行 (基线有; 连库跑法断言送给大模型的 data).
- GEN-02 J-GEN-02-1-XJ-405: misstatement, 草稿 {factual, account 含 "200", Occurrence, 50000, overstated}; 证据 XJ-405-1 (200 科目 -50000), XJ-408-1, SR-ORC1008 存在而 ORC1003 没有服务记录, CON-X02 第 1 页 (每月一张月费); 说明写明应收多记 550, GST 多记 50.

(c) 不该受影响
- R09, R11 命中不变; GEN-02 对 XJ-397, 449, 499, 575, 601 的判断不变; REV 程序 (窗口内发票) 不变.
- 会随之变化但不是检出目标: 收入 32,931 → 重要性 32931 / 16466 / 1647; PLN-07 A01 中 610 为 1074950, 820 为 -203317. 测试按新阈值断言, 并断言 R09 中 XJ-539 (9 分) 与 R11 中 XJ-585 (1050 分) 仍低于新的明显微小.

(d) 跑法: 三种. 规则层能区分基线与注入的只有 "R10 少一条" 与 R12 的 full_credits; 判断从 accept 变 misstatement 只由实时跑法证明 (成对断言必过).

### 8.5 T3 未记录负债 (期后付款对应期内服务)

(a) 改动 (快照副本)
- `journals.json`: JournalNumber 547, 548, 549 的 JournalDate 由 2026-09-05 改为 2026-10-06 (548 是 SM0195-70135 的生效过账, 547/549 是同日作废对, 一起移).
- `trial_balance_2026-09-30.json`: 800 -400516 → -200516; 400 183018 → 1200; 820 -119197 → -137379.
- `invoices.json` 中账单日期 09-05 不改 (载入器不读应付账单); 补造的 SMART Agency 发票 PDF (第 4 节第 25 项, 账单日 09-05, 服务交付 09-04) 不改.

(b) 应检出
- GEN-07 S01: BT-090-0097 (10-11, -450000, 参考号 "70135 70209") 仍是期后未入账流水 (与基线相同, 规则层没有区分点).
- GEN-06 对 BT-090-0097 的上下文 (6.4 节通用扩展按参考号数字串 "70135" 匹配 source_doc SM0195-70135): XJ-548 各行的 posting_date 为 2026-10-06 (基线为 2026-09-05). 这是连库跑法中有区分力的断言.
- GEN-06 J-GEN-06-1-BT-090-0097: misstatement, 草稿 {factual, account 含 "800" 或 "Accounts payable", Completeness, 200000, understated}; 证据 BT-090-0097, XJ-548-3 (800 科目 -200000, 入账日 10-06), SMART Agency 发票第 1 页. 说明写明广告费少记 1,818.18, GST 进项少记 181.82. 依据 AASB 110 第 3(a) 段 (调整事项), ASA 560 第 6 段.
- 前提: 第 6.4 节 GEN-06 上下文的通用扩展 (按参考号 "70135" 找到 XJ-548 与发票文件).

(c) 不该受影响
- GEN-07 其余 8 条与 GEN-06 其余 8 条判断不变; REV, CSH, GEN-05 不变 (547/549 移出本期, 但它们本来就是正常重记, 不进 R11 命中).
- 会变化但不是目标: PLN-07 A01 中 800 为 -200516, 820 为 -216499; GEN-04 的比率.

(d) 跑法: 三种. 判断的区分只由实时跑法证明: 基线 T0 中 J-GEN-06-1-BT-090-0097 为 accept, 注入后为 misstatement, 成对断言必过.

### 8.6 T4 银行回函余额差异 (在途款项)

(a) 改动 (快照副本)
- `journals.json`: JournalNumber 637 (Yarra Transport 支付 ORC1021 的 550) 的 JournalDate 由 2026-10-06 改为 2026-09-30.
- `trial_balance_2026-09-30.json`: 090 -1090044 → -1035044; 610 1019950 → 964950.
- 对账单与回函 PDF 不改 (银行 10-06 入账; 10-03, 10-04 为周末, 10-05 为新州劳动节, 10-06 是 09-30 之后的第 3 个营业日).

(b) 应检出
- CSH-05 B01: source BA-090, amount_cents = 对账单 - 账面 = -1090044 - (-1035044) = -55000.
- CSH-02: C-BA-090 book -1035044, difference -55000, result difference; D01 例外 amount_cents -55000; J-CSH-02-1-BA-090 accept: D03 有 XJ-637-2 (09-30, 090 +55000), D02 有 BT-090-0081 (10-06, +55000, "1021"), 属在途存款, 时间性差异不是错报 (ASA 505 A22).
- C-BA-091 仍 agreed; 其 D04 例外 (4853) 与基线相同.

(c) 不该受影响
- REV-08 R03 (按银行行匹配, 银行侧未改) 不变; GEN-05, GEN-07 不变 (BT-090-0081 在 ±10 天内仍找到 XJ-637).
- 会变化: PLN-07 A01 中 090 变动 -164724 小于期初绝对值的 20% (174064), 090 这条消失, 共 8 条; 610 为 964950.

(d) 跑法: 三种. 这个场景与 Numbat 的 BA-01 在途存款同构, 可复用 CSH-02 的提示词与校验.

### 8.7 T5 折旧错记 (不重复注入)

- 真实数据已有 #499 (08-16 无资产的折旧分录, 资产 750 与累计折旧 825, 进项税 75), 基线 T0 就是正例: GEN-05 R09-2 (XJ-499-2, 82500), GEN-02 misstatement 75000, M-1.
- 反向对照 T5' (可选): 副本 `journals.json` 删除 JournalNumber 499; 试算表 710 75000 → 0, 711 -82500 → 0, 820 -119197 → -126697. 预期 R09 只剩 XJ-397-3; GEN-02 没有 XJ-499 判断; 不产生 M-1; PLN-07 A01 少 710, 711 两条 (共 7 条). 跑法: 离线与连库.

### 8.8 T6 关联方交易未披露

(a) 改动 (补造副本; 格式同 2.5 节)
- `registry/abn.json` 加键 "23900043076": 改前不存在, 改后 {abn 23900043076, acn 900043076, entity_name SMART Agency Pty Ltd, status Active, entity_type Australian Private Company} (合成号码, 校验通过; 基线的 SMART Agency 发票已印此号码 (第 4 节表 #25), 基线登记 mock 中没有这家).
- `registry/asic.json` 加键 "900043076": 改前不存在, 改后 {acn 900043076, name SMART Agency Pty Ltd, status Registered, registered_date 2021-02-01}.
- 新增 `registry/extracts/900043076.json`: officeholders [Alex Morgan (1979-04-12) director 2021-02-01]; shareholders [Alex Morgan person ORD 40 pct 0.4, Casey Turner (化名, 与本公司无关) person ORD 60 pct 0.6]. 同名 PDF 2 页.
- 改 `registry/persons/MORGAN-ALEX-19790412.json` 的 roles: 改前为 2.5 节的 7 项; 改后在末尾加 [900043076 director 2021-02-01, 900043076 shareholder 40], 共 9 项. 同名 PDF 重新生成, 多两行.
- 两份 PDF 由生成脚本以 `--inject T6` 在副本目录生成, 页眉同样标合成.
- 画像申报清单不改.

(b) 应检出
- GEN-08 R01: 新增一条, source_ids [ACN-900043076], via PER-MORGAN-ALEX-19790412.
- OWNERSHIP: 8 个节点 (基线 6 个, 加 ACN-900043076 与 PER-TURNER-CASEY-NODOB), 13 条关系 (基线 10 条, 加 Alex 任董事, Alex 持 40, Casey 持 60; 摘录里的个人股东也建节点与关系). external_call 6 行; registry_extract 5 份.
- GEN-03 J-GEN-03-1-ACN-900043076: more_evidence; 交易由改写后的 C01 按名称规范化取到 ("SMART Agency Pty Ltd" 去后缀后等于 "smart agency", 5.1 节): 9 月广告费 1,818.18 (XJ-548-2, 整笔凭证取行), 09-30 应付 2,000 (XJ-548-3), 期后账单 XJ-641 与 10-11 付款 BT-090-0097 (4,500). 连库跑法断言送给大模型的 data 含这些行.
- GEN-01: more_evidence, 未申报的关联方列出 VPA 与 SMART Agency 两家.

(c) 不该受影响: VPA 与 Morgan Advisory 的判断; 所有非关联方程序; 试算表与重要性.

(d) 跑法: 三种. 规则层有区分点 (R01 多一条, 节点与关系数); 判断的区分由实时跑法的成对断言证明 (基线没有 ACN-900043076 这个判断对象, GEN-01 只列 VPA).

### 8.9 测试文件安排

| 文件 | 内容 |
|---|---|
| `tests/test_xero_injections.py` | 夹具 `xero_case`, 原目录哈希守护, T0 至 T6 的离线断言; 需要快照的用例在没有本地快照时跳过, 只读 fixture 与补造目录的用例默认跑 (8.1 节) |
| `tests/test_xero_injections_db.py` | `pytestmark = pytest.mark.slow`; 连库与真实大模型两组, 没有 TEST_DATABASE_URL 或本地快照时跳过 |
| `tests/fixtures/xero_injections/T1.json` 等 | 场景补丁 |
| `tests/fixtures/llm_injected/<场景>/<程序>/<对象>.json` | 离线 mock 的大模型输出 |

---

## 9 补造文件的存放, 生成, 校验与载入

### 9.1 目录与格式 (沿用 data/demo 的约定)

```
data/xero_demo/20261017-q1/              与快照目录同名, 一个快照一套补造
├── manifest.json                        每个文件: path, sha256, synthetic: true, generator, 对应的 Xero 记录;
│                                        另记 snapshot "xero/20261017-q1" 与快照各文件的 sha256 (不含 contacts.json)
├── dataset_traits.json                  5.2 节
├── client_profile.json                  整份覆盖载入器画像 (2.6 节)
├── planning/audit_objectives.json, risk_register.json      覆盖通用模板 (2.7 节)
├── registry/abn.json, asic.json         ACC-04, GEN-08 的 mock (2.5 节)
├── registry/extracts/{acn}.json, .pdf   公司摘录 3 份: 900043021, 900043030, 900043049 (G2 格式)
├── registry/persons/{姓-名-生日}.json, .pdf  人员检索 1 份: MORGAN-ALEX-19790412
├── contracts/CON-X01.pdf … CON-X03.pdf  只放收入合同 (DOC-04, REV-03 逐份分析)
├── contract_links.csv                   invoice_no, contract_id
├── delivery_notes.csv                   id, delivery_no, invoice_no, shipped_date, signed_date, signed_by (3.3 节)
├── bank_accounts.csv                    表头同 data/demo; 只用 bank_name 覆盖 (按 gl_account_code)
├── confirmations/12345678.pdf, 12345679.pdf, receipts.json
├── payroll/employees.csv                employee_id, name, start_date, employment_type, pay_period_days, gross_per_period_cents
├── adjustments/journal_entries.csv      表头同 journal_entries.csv (2.8 节)
├── documents/index.csv                  path, document_type, reliability, document_date, related_ids; 19 行, 取值见第 4 节类型表
├── documents/engagement/                appointment.pdf, engagement_letter.pdf
├── documents/related_party/             租金函, 出售与终止通知, 新租约, 分租协议
├── documents/evidence/                  固定资产登记表, 报销单 E003, 工资册与雇佣条款, 会计政策, 董事借款协议与确认函,
│                                        持续经营备忘录与预测, 董事支持函, DIISR 邮件, SMART Agency 发票, Ridgeway 订单, Jakaranda 报价单
├── financial_statements/fs_v1.json, fs_v1.pdf
├── representation/representation_letter.pdf
├── manual/                              人工队列提交物 (FIN-08 两次回复, 声明书登记, RPT-07 回复), 演示脚本与测试读, 载入器不读
└── llm/<程序>/<对象>.json                mock 大模型 fixture (同 data/demo/llm)
```

### 9.2 快照怎么来, 怎么对应

- **新快照 (已拉取)**: 本数据集的快照已在 Xero Demo 重置前拉取, 重拉时用:
  `uv run python -m backend.adapters.xero pull --out data/external/xero/api/20261017-q1 --period-start 2026-07-01 --period-end 2026-09-30 --prior-end 2026-06-30 --paid mock --statements-end 2026-10-17`
  - `--statements-end` 使对账单取到 10-17 (期后 73 行流水, CSH-02 D02, GEN-07, REV-08 R03 都要用).
  - 这样 trial_balance_2026-09-30.json 由 Xero 接口给出, 不再离线推导; dataset.json 期间就是 07-01 至 09-30, 不改任何旧快照.
  - 拉完逐文件比对 20261017-mock 的同名文件: 除 dataset.json, manifest.json 与试算表外的 13 个文件 sha256 相同 (contacts.json 也只比哈希), 0630 试算表按 trial_balance_cents 规范化后相同. 有差异说明 Demo 已重置, 新快照作废. 校验脚本每次运行都做这一比对 (第 9.4 节).
- **备选 (Demo 已重置时, 本版不做)**: 脚本 `data/derive_xero_period.py` 从 20261017-mock 生成 `api/20261017-q1d/`: contacts.json 只写去掉登录账号联系人的过滤版 (口径同 9.4 节), 其余文件逐字节复制; dataset.json 写新期间; trial_balance_2026-09-30.json 按 "0630 表 + 07-01 至 09-30 的分录" 推出, 以 Xero 报表结构写出, manifest 标 `"derived": true` 并写验证结果 (同法推 1130 表与接口 1130 表逐科目零差异). 也不入库.
- **对应关系**: 补造目录与快照目录同名; 补造 manifest 记快照各文件的 sha256, contacts.json 除外 (测试副本与派生快照里它是过滤版, 生成脚本也不读它). 载入器载入时核对, 不一致即报错 "补造文件与快照不匹配", 防止 Demo 重置后的新快照套上旧的补造文件.
- **ID 稳定**: XJ 号取自 10-17 的网页导出, BT 号依赖对账单起始日 (07-01) 与排序, SI 号依赖 InvoiceID; 只要按上面的命令在重置前拉取, 三者与 20261017-mock 相同.

### 9.3 载入器需要新增的读取点

1. XeroDatasetLoader 增加构造参数 `supplements: Path | None` (补造目录的上级; main.py 传 `data/xero_demo`, 测试传 `tmp_path/xero_demo`). 数据集 "xero/<快照>" 对应 `{root}/<快照>` 与 `{supplements}/<快照>`; 补造目录不存在或参数为空时照现状运行 (纯快照). 两个根目录显式传入, 不靠层级推算.
2. 核对补造 manifest 与快照哈希 (9.2 节); 测试副本带 injected 标志时只在 `allow_injected=True` 下放行.
3. `client_profile()`: 有 client_profile.json 时整份取它, 校验 id 以快照组织 ID 推出的 `CLIENT-XERO-{短 ID}-` 开头 (本数据集 CLIENT-XERO-3C1332BB-Q1). 带后缀是为了与 20261017-mock 的载入器画像 CLIENT-XERO-3C1332BB v1 区分, 避免同库建项目时 VersionConflict (2.6 节).
4. `planning_presets()`: 有 planning/ 时取它, 否则通用模板.
5. 登记 mock 与回函 mock 按数据集定位目录: main.build_deps 现在把 MockRegistrySource 与 MockConfirmationChannel 固定在 `data/demo`, 改为按项目的数据集解析 (Numbat → data/demo, Xero → 补造目录).
6. 合同 PDF: `build()` 返回补造目录的 contracts/*.pdf (现在 XeroDatasetLoader 传 `()`).
7. sales_invoices.contract_id 按 contract_links.csv 的 invoice_no 填.
8. delivery_notes 按 delivery_notes.csv 读入, invoice_no 换成 SI- ID; 找不到发票时报错.
9. bank_accounts.bank_name 按 bank_accounts.csv 覆盖.
10. 更正分录: 不在载入时入库. G4 的 CorrectionNotice (adjustments_posted) 增加 `adjustment_entry_nos`, `load_adjustments` 只载入列出的分录 (本数据集分两轮: AJE-X01 与 AJE-X05; AJE-X02 至 X04). Numbat 只有一轮, 不填时载入全部, 行为不变.
11. documents/index.csv 列出的 19 份文件登记为证据文件 (类型, 可靠性, 文件日期, 关联对象); 类型取值与证据映射见第 4 节类型表, 未知类型报错.
12. fs_v1.json 作为 FIN-12 的财报输入 (G6 的 financial_statements 端口).
13. dataset_traits.json 供程序参数 `{ref: dataset.<键>}` 解析; payroll/employees.csv 供 FIN-15.
14. 快照里收款分录的 source_doc 有 "Payment: 客户名" 与 "Payment from 客户名" 两种, 都不带发票号; 若要做 GEN-07 S04, 需从 payments.json 的分配记录生成 "收款与发票对应" 表.
15. 作废对: 现阶段由规则按 `reversal_reference_pattern` 识别; 若载入器愿意加 `reverses_entry_no` 列 (B 的表结构), 规则可改为直接用列.

### 9.4 是否入库, 生成与校验脚本

- 入库: `data/xero_demo/` 与生成规格入库; .gitignore 在 `/data/*` 之后加 `!/data/xero_demo_spec.json` 与 `!/data/xero_demo/` (`!/data/*.py` 已覆盖脚本). 本仓库已带上 Xero 快照, 其他检出可能没有 (第 1 节).
- 生成: `data/generate_xero_demo.py` 读 `data/xero_demo_spec.json` 与指定快照 (只读), 用 reportlab 写 PDF (同 generate_demo.py), 写 CSV/JSON 与 manifest; 参数 `--snapshot 20261017-q1`, `--out`, `--inject <场景>` (只在测试副本目录用).
  - 只读 accounts, invoices, credit_notes, journals, bank_statements, organisation, dataset.json 与试算表; 不读 contacts.json, 不读 raw/ 目录 (第 1 节白名单).
  - 所有合成内容 (人名, 日期, 条款, 号码) 写在 spec 里, 脚本不现编.
- 校验: `data/check_xero_demo.py`:
  - 重算 manifest 哈希; 核对快照哈希.
  - 合成标志: 领域对象模型 `extra="forbid"` (contracts/models.py), client_profile.json, registry 各行, planning JSON, delivery_notes.csv 等按模型严格校验, 不能加 synthetic 字段或列. 所以合成标志只放在 manifest (每个文件 `synthetic: true`) 与文本字段 (画像 notes, PDF 页眉, 分录摘要). 校验脚本核对: manifest 中每个文件都有 `synthetic: true`; 每份 PDF 每页含合成页眉; 画像 notes 以 "Synthetic – for demonstration only" 开头.
  - 登录账号联系人: 快照里 IsCustomer 与 IsSupplier 都为假的联系人有 39 个 (含 Victoria Property Agency, 3 名员工, Business Savings Bank, 这些名字在设计与补造文件里合法出现), 不能按 "双否" 认定. 口径改为 "双否且有 FirstName 与 EmailAddress", 断言恰好 1 条 (当前快照唯一). 在内存里取它的全名 (FirstName 加 LastName) 与邮箱, 不区分大小写整串匹配, 断言不出现在任何生成文件中; 不单独匹配姓或名 (短字符串会在普通单词里误中); 不打印, 不写入任何文件; 断言失败只报文件名.
    扫描范围: 补造目录全部文件 (PDF 查文本层), 生成规格, 本文, `tests/fixtures/` 下的 llm_xero, llm_xero_only17, xero_injections, llm_injected (不存在的跳过). 连续空白视为一个空格.
  - 模型与键: client_profile.json 用 ClientProfile, planning 两份用 AuditObjective 与 RiskItem 严格校验 (不做类型转换); dataset_traits.json 的键集合等于第 5.2 节的 11 个键.
  - 确定性: 另生成到临时目录, 逐文件逐字节比较. 生成脚本写完后自动调用校验, 任一项不过即非零退出.
  - 没有本地快照时跳过快照哈希, 画像 ID 前缀, 补造表与快照的核对 (服务完成记录, 合同关联, 开户行, xero_refs, 回函余额与应计利息, 更正后试算表, 财报), 快照关键数复算, 新旧快照比对, 联系人扫描与确定性 (说明写 "没有本地 Xero 快照"), 补造文件的哈希, 合成标志与模型校验照常做. 快照缺 dataset, invoices, accounts, bank_statements, journals 任一文件时, 与快照的核对, 关键数复算与新旧快照比对都跳过并写说明. 问题全部收集后一次报出, 每行一项, 只写文件名.
  - 快照关键数 (第 3 节, 只用白名单文件): 分录 368 笔 1017 行, 作废对 65 (按 reversal_reference_pattern 识别; 本期 33, 指作废与原分录都在本期), 每笔借贷平衡; 本期分录 215 笔 582 行; 09-30 试算表 27 行, 合计 0, 主要余额 (第 3.2 节), 本期损益 -13,226.96, 流动资产 21,780.73 与流动负债 42,844.69 (按 Xero 科目类型, 透支列流动负债); 对账单 090 共 142 行 (期末前 72, 末行 BT-090-0072 余额 -10,900.44), 091 共 4 行 (期末前 1, BT-091-0001 余额 11,581.23); 月度收入 4,600.00 / 18,036.00 / 9,795.00 与重要性 324.31 / 162.16 / 16.22 (收入取标准分类 revenue 的科目, 同 PLN-01; 270 属 other_income); 录入日 (悉尼) 晚于 10-19 的 15 笔 (在全部分录中数: 本期 13 笔, 本期之后 2 笔即 XJ-690, XJ-755), 与 dataset_traits 的 entry_timestamps_note 一致 (说明中的 "up to 26 November 2026" 是 UTC 日期, 悉尼为 11-27). 按文件 (journals.json, 期末试算表, bank_statements.json, dataset_traits.json) 分步, 某步意外中断只报该文件; 不符时按文件报出项目名, 不写数值.
  - 新旧快照比对 (第 9.2 节): 20261017-mock 在快照同级目录时做, 不一致报 "快照与 20261017-mock 不一致 (Demo 可能已重置)" 并只列文件名; 没有时, 或快照本身就是 20261017-mock 时, 跳过并写说明; 0630 表读不出或缺 prior_end 也算不一致, 已得出的文件名照常列出.
  - 第 5 节规则命中与第 7 节计数不在校验脚本复算 (见 "本版不做"), 分别由各程序的连库测试与合并终验核对.

---

### 9.5 文件格式与载入端口 (实现时以此为准)

**manifest.json**: dataset ("xero/20261017-q1"), generator ("data/generate_xero_demo.py"), spec_sha256, snapshot {name "xero/20261017-q1", files: 快照 17 个文件去掉 contacts.json 的 16 个 path 与 sha256}, files (补造目录除 manifest.json 外每个文件: path, sha256, synthetic true, generator, xero_refs), documents (每份 PDF: path, sha256, document_id, type, reliability, page_count, document_date, related_ids, registration, synthetic true). 不含时间戳与绝对路径; 正式目录没有 injected 键, 测试副本写 `"injected": "<场景>"`. xero_refs 中每个对象 (SI-, XJ- 分录号或分录行号如 XJ-580-1, BT-, BA-, 科目代码) 都须在快照中找到, 否则生成失败; --inject 重写的副本 manifest 不在生成脚本核对, 由校验脚本对照副本快照核对.

**document 的登记途径 (registration)** 共 29 份 55 页: load 20 (3 份收入合同加 index.csv 中除承接文件外的 17 份, 载入时登记), engagement 2 (appointment.pdf, engagement_letter.pdf, 由 `load_engagement_documents` 在 P0 登记), registry 4 (3 份公司摘录与 1 份人员检索, GEN-08 运行时由登记 mock 回执存档), confirmation 2 (两份银行回函, CSH-04 运行时存档), upload 1 (声明书, FIN-07 人工任务经上传接口登记).

**登记摘录与人员检索 JSON** (`registry/extracts/{acn}.json`, `registry/persons/{姓-名-yyyymmdd}.json`, 同名 PDF 由 JSON 渲染):
- 公司摘录: {acn, name, type, class, subclass, status, registered_date, officeholders [{name, birth_date, role (director | secretary), appointed}], shareholders [{name, kind (person | company), acn, share_class, shares, pct}], paid_up_cents, file_name}.
- 人员检索: {family_name, given_name, birth_date, roles [{acn, company_name, role (director | secretary | shareholder), appointed, shares}], file_name}.
- 公司摘录 PDF 各 2 页 (第 1 页公司与高管, 第 2 页股本与股东), 人员检索 1 页.
- 已缴股本只有 Demo 有 (paid_up_cents 100, 即 1.00, 第 2.2 节); VPA 与 Morgan Advisory 的 paid_up_cents 为 null, PDF 写 "not shown". 人员检索 JSON 的姓名按自然大小写 (Morgan, Alex), 文件名与节点 ID 用大写.
- PDF 内容只取自同名 JSON (页眉取生成规格): 公司摘录两页都写名称, 带空格的 ACN (900 043 021) 与注册日; 人员检索写姓名, 出生日期与各公司 ACN. 测试副本改 JSON 后用 `render_registry_pdfs` 重绘.
- 登记 PDF 的登记信息按目录规则推出 (registry/extracts 与 registry/persons 下的 PDF: registry_extract, external_original, registration registry, document_date 为检索日即 data_as_of 2026-10-17), 不在生成规格 documents 段逐份登记; 测试副本新增的摘录 (T6, 第 8.8 节) 也按此登记.

**生成规格的 documents 段**: 每份 PDF 一项 {path, type, reliability, document_date, related_ids, registration, index, xero_refs}; index 为真的进 documents/index.csv. 正文写在规格里的 PDF (承接文件, 合同, 回函等) 另有 footer 与 pages (每页一组行); 登记 PDF 不在本段, 由登记 JSON 渲染. 校验脚本核对每页文本层按顺序含来源的每个非空行 (区分大小写, 空白归一).

**delivery_notes.csv**: id 与 delivery_no 都是 `SR-{发票号}` (如 SR-ORC1026), 24 行, 日期见第 3.3 节; 载入器把 invoice_no 换成 SI- ID, 发票号找不到或重复即报错.

**documents/index.csv** (列 path, document_type, reliability, document_date, related_ids; related_ids 以 ";" 分隔), 19 行:

| path | document_type | reliability | document_date | related_ids |
|---|---|---|---|---|
| documents/engagement/appointment.pdf | minutes | internal_copy | 2026-08-27 | |
| documents/engagement/engagement_letter.pdf | engagement_letter | external_copy | 2026-08-28 | |
| documents/related_party/vpa_rent_letter.pdf | agreement | external_copy | 2026-06-25 | ACN-900043030;XJ-420;XJ-518;XJ-593 |
| documents/related_party/vpa_sale_termination_notice.pdf | correspondence | external_copy | 2026-08-28 | ACN-900043030 |
| documents/related_party/harbourview_lease.pdf | agreement | external_copy | 2026-09-22 | ACN-900043030;XJ-613 |
| documents/related_party/rm2031_sublease.pdf | agreement | external_copy | 2026-10-15 | BT-090-0131 |
| documents/evidence/director_loan_agreement.pdf | agreement | internal_original | 2018-09-10 | XJ-397 |
| documents/evidence/director_loan_confirmation.pdf | correspondence | internal_original | 2026-10-16 | XJ-397;CLIENT-XERO-3C1332BB-Q1 |
| documents/evidence/going_concern_memo_forecast.pdf | internal_record | internal_original | 2026-10-16 | CLIENT-XERO-3C1332BB-Q1 |
| documents/evidence/director_support_letter.pdf | correspondence | internal_original | 2026-10-16 | CLIENT-XERO-3C1332BB-Q1;BT-090-0107;BT-090-0130 |
| documents/evidence/fixed_asset_register.pdf | internal_record | internal_original | 2026-09-30 | XJ-499;XJ-626;XJ-676 |
| documents/evidence/expense_claim_E003.pdf | internal_record | internal_original | 2026-08-14 | E003 |
| documents/evidence/payroll_register_terms.pdf | internal_record | internal_original | 2026-09-30 | 804;805;826 |
| documents/evidence/accounting_policies_extract.pdf | internal_record | internal_original | 2026-07-01 | XJ-499 |
| documents/evidence/diisr_email.pdf | correspondence | external_copy | 2026-10-13 | XJ-720 |
| documents/evidence/smart_agency_invoice_SM0195-70135.pdf | supplier_invoice | external_copy | 2026-09-05 | XJ-548;BT-090-0097 |
| documents/evidence/ridgeway_order_CRM08-12.pdf | agreement | external_copy | 2026-10-02 | XJ-647;BT-090-0092 |
| documents/evidence/jakaranda_quote.pdf | internal_record | internal_copy | 2026-10-08 | BT-090-0099 |
| financial_statements/fs_v1.pdf | other | internal_original | 2026-10-22 | FS |

报销单 E003 只关联员工号 (系统录名错误使它挂不到 XJ-575 上): GEN-02 对 XJ-575 给 more_evidence, 审计师调阅报销单后 override (第 7.2 节第 13 步).

**payroll/employees.csv** (列 employee_id, name, start_date, employment_type, pay_period_days, gross_per_period_cents): E001 Oliver Gray 2026-07-20 full_time 14 240000; E002 Tracy Green 2026-07-20 full_time 14 180000; E003 Odette Garrison 2026-07-20 full_time 14 250000.

**adjustments/journal_entries.csv**: 表头同 journal_entries.csv; 11 行, 行 ID 为 `AJE-X0n-k`, 金额与 source_doc 见第 2.8 节.

**financial_statements/fs_v1.json**: {id "FS", version 1, prepared_at 2026-10-22, directors_declaration_date 2026-10-23, document_id (fs_v1.pdf 的文件 ID, 即 DOC- 加其 sha256 前 12 位), comparative_restatement_entry_nos [XJ-397], lines 18 行} (字段同需求清单 R216 的 FinancialStatements), 每行 {code, statement (financial_position | profit_or_loss), name, amount_cents, sign, account_codes, subtotal, prior_amount_cents}; manifest 中它的 xero_refs 为 [XJ-397]:

| code | amount_cents | sign | account_codes | prior_amount_cents |
|---|---:|---|---|---:|
| SOFP-CASH | 1158123 | +1 | 091 | 1508123 |
| SOFP-REC | 1019950 | +1 | 610 | 0 |
| SOFP-OREC | 4853 | +1 | 620 | 0 |
| SOFP-PPE | 0 | +1 | 710, 711 | 0 |
| SOFP-TA (小计) | 2182926 | +1 | 091, 610, 620, 710, 711 | 1508123 |
| SOFP-OD | 1090044 | -1 | 090 | 457222 |
| SOFP-PAY | 400507 | -1 | 800, 801, 860 | 0 |
| SOFP-TAX | 880197 | -1 | 820, 825 | -79120 |
| SOFP-EMP | 895856 | -1 | 804, 805, 826 | 0 |
| SOFP-DLOAN | 1921221 | -1 | 840 | 1921221 |
| SOFP-TL (小计) | 5187825 | -1 | 090, 800, 801, 860, 820, 825, 804, 805, 826, 840 | 2299323 |
| SOFP-EQ | -3004899 | -1 | 960 与全部损益科目 | -791200 |
| PL-REV | 3243100 | -1 | 200 | 420000 |
| PL-OI | 4853 | -1 | 270 | 0 |
| PL-EMP | 4245856 | +1 | 477, 478 | 0 |
| PL-OCC | 635600 | +1 | 408, 445, 469 | 0 |
| PL-OTH | 580196 | +1 | 400, 404, 412, 420, 429, 449, 453, 461, 489, 493 | 1211200 |
| PL-NP (小计) | -2213699 | -1 | 全部损益科目 | -791200 |

比较数按 "0630 试算表加 XJ-397" 机械计算 (第 4.7, 6.6 节); 上期 GST 应收 791.20 记在 SOFP-TAX 行的上期 -79120, 不单列资产行, 以免 820 映射两次.

校验脚本对财报的核对:
- 不需要快照: 合模型, 与规格一致, 行代码不重复, document_id 正确; 同一张表的非小计行之间科目不重复; 小计等于 sign × Σ (所含各行 sign × 金额), 所含各行的科目合起来等于小计的科目; 资产 = 负债 + 权益 (本期与比较数); 期初累计亏损加本期损益等于期末; PDF 文本层有每行的名称, 本期与比较数.
- 需要快照: 每行等于 sign × Σ (09-30 试算表加 5 笔更正) 映射科目, 比较数等于 sign × Σ (0630 表加 XJ-397); 非小计行恰好映射 33 个科目; 损益科目只在损益表与累计亏损行.
- documents 段 29 份 55 页与各登记途径的份数只核正式目录 (测试副本会增删 PDF, 如注入 T6 新增摘录).

**manual/** (人工队列提交物的中性格式, 演示脚本与测试读, 载入器不读; 各程序按自己的结果模型映射, 提交后由程序 owner 整理成领域对象, 不建人工判断):
- fin08_round1.json: {procedure_id FIN-08, round 1, communicated_at 2026-10-19T04:00:00Z (悉尼 15:00), recipient "Alex Morgan, Director", form written, misstatement_sources [J-GEN-02-1-XJ-499, J-CSH-02-1-BA-091], responded_at 2026-10-19T22:00:00Z (悉尼 10-20 09:00, 早于 AJE-X01 的录入时间), response 第 4.8 节第 1 轮原文, adjustment_entry_nos [AJE-X01, AJE-X05]}.
- fin08_round2.json: {procedure_id FIN-08, round 2, recipient "Alex Morgan, Director", form written, communicated_at 2026-10-21T04:00:00Z (悉尼 15:00), misstatement_sources [J-FIN-15-1-826, J-FIN-15-1-804, J-FIN-15-1-805], responded_at 2026-10-21T22:00:00Z (悉尼 10-22 09:00), response 第 2 轮原文, adjustment_entry_nos [AJE-X02, AJE-X03, AJE-X04]}.
- representation.json: {procedure_id FIN-07, status obtained, dated_at 2026-10-23, signed_by [Alex Morgan], period_end 2026-09-30, document "representation/representation_letter.pdf", standards (第 4.8 节七项), uncorrected_schedule []}.
- rpt07_reply.json: {procedure_id RPT-07, communicated_on 2026-10-22, form written, recipients [Alex Morgan], matters [SIG_PRACTICES, DIFFICULTIES, REPORT_FORM, INDEPENDENCE], corrected_misstatements 5, uncorrected_misstatements 0, responded_on 2026-10-22, response (董事回复原文, 写在生成规格里, 见第 4.8 节)}.
- 校验: FIN-08 两轮键集合相同, 时刻为 UTC (以 Z 结尾), 回复晚于沟通且早于所涉更正分录的录入时间, 两轮的 adjustment_entry_nos 恰好覆盖 5 笔更正分录; 声明书登记的文件在 manifest 中 (类型 representation_letter, 日期同 dated_at), 签署人在其文本层, 附表为空; RPT-07 的已更正数等于更正分录笔数, 未更正数等于声明书附表行数.

**大模型 fixture**: Xero 的 fixture 正本放 `tests/fixtures/llm_xero/<程序>/<对象>.json` (各程序的需求写), 生成脚本原样复制到 `data/xero_demo/20261017-q1/llm/` 并列入 manifest; mock 模式按项目数据集选目录 (Xero 项目读补造目录的 llm/, 其余读 LLM_FIXTURES_ROOT). document 指针直接用 manifest 的文件 ID. Numbat 的 fixture (`tests/fixtures/llm`, `data/demo/llm`) 不动.

**注入补丁** (`tests/fixtures/xero_injections/<场景>.json`): {scenario, title, ops}; op 六种: set (file, 定位, field, before, after), delete (file, 定位, before), add (file, key 或 append, after), add_file (file, content), tb_shift (code, delta_cents), render (what: registry_pdfs). file 以 "api/" 或 "xero_demo/" 开头; 定位三种: match (JSON 数组中按字段相等, 须恰好命中 1 个), key (JSON 对象的键), row (CSV 主键列); before 为 null 表示改前不存在. 执行前断言当前值等于 before.

**载入端口与构造** (签名):
- `XeroDatasetLoader(root, planning, engine=None, evidence_store=None, *, supplements: Path | None = None, allow_injected: bool = False)`; 补造与快照不匹配抛 `SupplementMismatch` (ValueError 子类), 消息以 "补造文件与快照不匹配" 开头, 只列文件名.
- `DatasetLoader.load_adjustments(eid, dataset, entry_nos=None) -> list[JournalEntry]` (按凭证号载入更正分录, 幂等, 试算表不动); `DatasetLoader.adjustment_entries(dataset) -> list[AdjustmentEntrySummary]` (entry_no, posting_date, description, line_count) 与 `adjustment_entry_nos(dataset) -> list[str]` (可载入的凭证号, 更正通知提交时校验; 没有补造更正分录的数据集为 []).
- `DatasetLoader.load_engagement_documents(eid, dataset) -> list[DocumentRef]` (P0 登记承接文件).
- `DatasetLoader.dataset_traits(dataset) -> DatasetTraits` (缺文件时为缺省值); `DatasetLoader.period(dataset) -> tuple[date, date] | None`.
- `XeroDatasetLoader.tables(dataset)` (不连库的纯函数, 返回七张表与合同 PDF 路径, load 与测试共用); `EvidenceStore.save(..., document_date=None, related_ids=())` (同哈希已登记时返回已有记录, 不改字段).
- `FinancialDataStore.documents(eid, type=None, related_id=None)`; `FinancialDataStore.employees(eid) -> list[Employee]`; `FinancialDataStore.financial_statements(eid) -> FinancialStatements | None`, `record(eid, "financial_statement_line", code)`.
- `RegistrySource.abn(eid, abn)`, `asic_company(eid, acn)`, `asic_extract(eid, acn)`, `asic_person(eid, family_name, given_name, birth_date)`: 按项目数据集定位登记 mock (Numbat → data/demo, Xero → 补造目录).
- `LLMRequest.eid`: mock 按它选 fixture 目录.


## 本版不做

- 从 payments.json 的分配记录生成 "收款与发票对应" 表 (第 9.3 节第 14 项), 因而 GEN-07 S04 "期末应收到截止日仍未收回" 暂缓.
- journal_entries 加 `reverses_entry_no` 列 (第 9.3 节第 15 项): 作废对仍由规则按 `reversal_reference_pattern` 识别.
- Demo 重置时的派生快照脚本 `data/derive_xero_period.py` (第 9.2 节备选): 本仓库已有 `20261017-q1`.
- 从快照裁出最小 fixture 入库 (第 8.1 节).
- 校验脚本复算第 5 节的规则命中与第 7 节的计数: 分别由各程序的连库测试与合并终验 (需求清单 R258-R263) 核对.
- 更正传导 (第 7.2 节): 更正后试算表不改版, 依赖科目的已完成程序不标 needs_review; FIN-12, FIN-15, FIN-02 按 "试算表加已验证更正分录" 取数.
- fs_v1 的权益变动表与现金流量表 (第 4.7 节): 是否可省略暂缓, 待 AASB 1060 正本 (T7). 在此之前, R64 委托书第 1 节与 RPT-03 审计报告第 1 段 (`knowledge/prompts/RPT-03.md.j2`) 列举的报表组成多于 fs_v1 实有的两张表; T7 定稿时要么补造两张表, 要么同步改这两处的列举.

## 待核与待正本

**T1 Xero 条款**: Xero Demo 数据与据此生成的 fixture 能否随公开仓库再分发, 没有开发者条款原文. 本仓库已带上快照 (用户的决定); 补造目录只按编号引用 Xero 记录, 入库.

**T2 法定数据** (都没有本地原文): SG 费率 12% (M-2), NES 每年 4 周年假 (M-4), PAYG, BAS 与 SG 的缴款截止日 10-28. 拿到原文前这三项金额在测试里以参数注入, 不写死.

**T3 GST** (本地没有 GST 法原文): 1026C 的 GST 调整归属哪个申报期; #499 对折旧申报的 75 进项税冲回.

**T4 编码与名称查重**: ANZSIC 7000; 合成号码 25/27/17/19/21 900 043 0xx 与测试专用 23 900 043 076 是否已被登记; 银行 ABN 82 938 074 116; BSB 306 段归属; Wattlebrook, Victoria Property Agency, Harbourview Nominees, Morgan Advisory 是否与真实公司重名; ASIC 对名称中括号的规则; 新州劳动节日期 (2026-10-05) 以官方公告为准.

**T5 公司法与解释法**: s45A(2) 条例数额的现行值; s315(4) "4 个月" 的计算方法 (Acts Interpretation Act); ASIC 个人检索是否列持股; ATO 对缩短会计期间是否另有要求.

**T6 审计准则**: ASA 501 关于诉讼的书面声明段号; APES 110 R410 各段.

**T7 AASB 1060 (段号暂缓, 待 AASB 1060 正本)**: 本地只有 Key facts 摘要. 待正本的段号: 关联方定义与披露; 持续经营披露; 期后事项披露; 期间短于 12 个月的披露与比较数不可比; 流动与非流动分类及抵销 (透支能否与储蓄户抵销); 现金流量表与可否省略权益变动表; 首次编制通用目的财报是否适用 AASB 1; 比较数重述的披露.

**T8 载入器已知的数据特点**
- XJ-400 与 XJ-401 的调整标志不一致 (规则已不依赖它, 作废对按 reversal_reference_pattern 识别).
- 收款分录 source_doc 有 "Payment: X" 与 "Payment from X" 两种格式, 都不带发票号.

**T9 各组程序的口径改写**: 第 5.1 至 5.6 节对各组新程序的通用写法 (GEN-05, GEN-03, GEN-07, GEN-06, FIN-09, FIN-15, PLN-19, 报告其他事项段, 文件类型合并) 已写进需求清单 R62 起各条.

**T10 时效**: Xero Demo 每 28 天重置一次. 9.2 节的新快照已在重置前拉取 (`api/20261017-q1/`); 载入器仍按 9.2 节比对 20261017-mock 防止误用重置后的快照.

---
