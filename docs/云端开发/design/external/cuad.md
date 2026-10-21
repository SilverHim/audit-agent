# CUAD 用于本系统合同类测试的可用性

**结论**: CUAD 能给 DOC-04 的当事方, 签订日和终止类条款提供现成真值, 页码真值也能从 PDF 推出. 但 ContractExtraction 的 ABN 和金额, 以及 TermType 7 类里的 4 类 (付款, 控制权转移, 退货, 返利) CUAD 没有标注, REV-03 最依赖的正是这几类, 要人工补标. CUAD 只能单独测 DOC-04 和 REV-03, 接不上 Numbat 的全流程. 原因是没有发票, 也就跑不了 REV-02, 合同日期又在 1994-2020 之间.

## 0. 数据结构 (实测)

- **CSV**: master_clauses.csv 有 510 行 x 83 列. 第一列是 Filename, 后面 41 个类别各占两列, 一列是原文摘录, 一列是答案.
  - 摘录列存的是 Python list 字符串, 不连续的片段用 `<omitted>` 连接.
  - 33 类答案是 Yes/No. 我核对了 12 类, 摘录非空和答案为 Yes 完全一致 (例如 Minimum Commitment 165/165, Cap On Liability 275/275).
  - 官方 README 写 Warranty Duration 的答案格式是 "Number of months or years", CSV 实际是 Yes/No.
- **JSON**: CUAD_v1.json 是 SQuAD 2.0 格式, 510 份合同, 每份只有 1 个 paragraph, context 就是全文.
  - 每份 41 个问答, 共 20910 个, 其中 is_impossible 14208 个.
  - answer_start 是字符偏移. context 与 full_contract_txt 逐字相同 (510/510).
  - txt 里没有换页符 (0 个文件含 `\f`), **页码只能从 PDF 推**.
- **文件名对应**: PDF 实际是 311 个 .PDF 加 199 个 .pdf.
  - PDF 文件名把 `&` 和 `'` 改成了 `_`.
  - CSV 里 Kallo 一行的文件名末尾多出 `.pdf'`.
  - Harpoon 那份 PDF 多了 `_Option Agreement` 后缀, 另有 4 份 PDF 名末尾带空格或连字符.
  - 做 NFC 规范化, 去掉扩展名和首尾空白, 把 `&'` 换成 `_` 之后, 再特判 Harpoon, 七类合同 174 份全部对上 CSV.
  - Scansource ...Agreement1/2, Bellring ...Agreement1-4, Kubient ..._Part1 是同一合同拆开的几个文件, 不宜单独当样本.
- **label_group_xlsx**: 28 个 Excel, 按条款组把 CSV 内容再排一遍, 评测用不上.

## 1. CSV 字段与 ContractExtraction 和 TermType 的映射

括号里是非空行数, 全集 510 行.

| 本系统字段 | CUAD 列 | 可用程度 | 说明 |
|---|---|---|---|
| counterparty | Parties-Answer (509) | 可用 | 分号分隔, 括号里是合同中的定义名 (如 `Ingram Micro ("Distributor")`). 359 份是 2 方, 最多 54 方. 哪一方是被审计单位要逐份人工定, 剩下的才是对方 |
| counterparty_abn | 无 | 真值恒为 null | 510 份全文 grep `ABN`, `A.B.N`, `Australian Business Number` 都是 0 份 |
| amount_cents | 无 | 真值基本为 null | CUAD 没有合同总价这一类. 分销和供货合同多是框架协议, 只有单价, 金额又常被 `[***]` 遮盖. 按 DOC-04 规则应输出 null, 正好测模型会不会编数 |
| sign_date | Agreement Date-Answer (465), 备选 Effective Date-Answer (359) | 可用, 要清洗 | 格式是 m/d/yy (416 条), 另有 49 条含遮盖 `[]` (如 `05/[]/2000`). 年份只有 90-99 和 00-20 两段, 按 yy≥90 补成 19yy. 语义是 "the date of the contract", 和 DOC-04 的 "执行日, 取较晚者" 不完全一样. 抽样就发现 2 处标注错误 (见第 2 节), 金标准必须人工复核 |
| evidence[].page | 由 Parties 和 Agreement Date 摘录在 PDF 里定位推出 | 可推出 | 见第 2 节 |
| TermType.termination | Termination For Convenience (183), Notice Period To Terminate Renewal (111), Post-Termination Services (182, 对应终止后果), Expiration Date (413) | 部分 | CUAD 没有 "因违约终止" 这一类, 召回率只能按这几类算 |
| TermType.guarantee | Warranty Duration (75) | 弱, 方向可能相反 | DOC-04 的 guarantee 定义是 "超出标准质保的保证", CUAD 标的多为标准质保期. 只能作参考, 不宜当真值 |
| TermType.payment | 无 | 需人工标 | Revenue/Profit Sharing (166) 是分成或特许权使用费, 不是付款条件 |
| TermType.control_transfer | 无 | 需人工标 | 七类 174 份中, 文本含 FOB, Ex Works, title 转移, risk of loss 字样的很常见 (抽样见第 2 节) |
| TermType.return_right | 无 | 需人工标 | Post-Termination Services 偶尔含 "终止后退回库存", 如 Nanophase 13.3 在第 9 页 |
| TermType.rebate | 无, 只有相近类别 | 需人工标 | 相近的有 Price Restrictions (15), Most Favored Nation (28), Revenue/Profit Sharing (166). NETGEAR 2.C 的 price protection credit 属于返利类 |
| TermType.related_party | 无 | 需人工标 | Affiliate License 两类讲的是许可范围, 不是关联方 |

CUAD 有, 但 ContractExtraction 没有对应字段的类别:
- Expiration Date, Renewal Term, Governing Law (433, 只有 1 份是 Victoria, Australia, 即 INTRICONCORP Strategic Alliance)
- Minimum Commitment (165), Price Restrictions, Revenue/Profit Sharing, Volume Restriction (82)
- Cap On Liability, Uncapped Liability, Liquidated Damages (61)

其中 Minimum Commitment, Price Restrictions, Revenue/Profit Sharing, Liquidated Damages 涉及 AASB 15 的可变对价或主要责任人与代理人判断. 要不要扩 TermType 需要拍板.

## 2. 收入相关样本 (pdfplumber 实测)

**七类全量扫描** (174 份, 和 README 的类型计数一致):
- 全部能打开, 没有一份文本层全空. 9 份含空白页, 是签字页或附件页.
- 中位页数: Distributor 9, Supply 19, Reseller 11.5, Manufacturing 16, Service 8, Maintenance 9, License 10.
- 单份抽取最长 3.7 秒.
- 字符数分位: P10 5842, P50 31891, P90 94483.

**标注定位**: 把 20 个类别的摘录 (按 `<omitted>` 拆开) 去掉非字母数字后, 用前 60 个字符在逐页文本里查找. 共 2864 段, 结果如下:

| 结果 | 段数 | 说明 |
|---|---|---|
| 只出现在一页 | 2299 | 可直接作页码真值 |
| 出现在多页 | 550 | 465 段是 Parties, 名称本来就会反复出现, 取首页或接受页集合 |
| 跨页 | 7 | 后 40 个字符能找到 |
| 找不到 | 8 | |

可定位率 99.5%, 所以 **证据指针页码的真值能自动推出**, 而且和系统用的 pdfplumber 逐页切分一致 (stores.py 第 339-340 行).

**候选样本** (10 份, 共 106 页, 301601 字符, 约 75k token):

| 类型 | 文件 | 页数 | 已核实的标注页与条款线索 |
|---|---|---|---|
| Distributor | Part_II/Distributor/NETGEAR,INC_04_21_2003-EX-10.16-DISTRIBUTOR AGREEMENT.pdf | 8 | 签订日 11/5/96 在第 1 页. 第 3 页第 5 节 "SHIPMENT, CANCELLATION, RETURNS, TITLE, RISK OF LOSS". 第 2 页 FOB, 价格保护, payment terms. 退货在第 3, 4, 6 页. 第 8 页附了修订协议 |
| Distributor | Part_III/Distributor/NANOPHASETECHNOLOGIESCORP_11_01_2005-EX-99.1-DISTRIBUTOR AGREEMENT.PDF | 16 | 第 3 页: FOB/FCA (3.2), 5.1 Return Rights, 4.3 Net 30. 第 13 页还有风险转移条款. 签订日 10/24/05 出现在第 1 和第 11 页 |
| Distributor | Part_III/Distributor/LIMEENERGYCO_09_09_1999-EX-10-DISTRIBUTOR AGREEMENT.PDF | 10 | 第 4 页: FOB, 退货, 2.7 Payment Terms. **CUAD 签订日 7/7/99 有误**, 正文第 1 页写的是 "7th day of September, 1999". 末页印的是 "Page -14-" |
| Supply | Part_I/Supply/AgapeAtpCorp_20191202_10-KA_EX-10.1_11911128_EX-10.1_Supply Agreement.pdf | 13 | 第 5 页: Ex Works, RMA 退货, 第 5 节付款. 签订日在第 2 页. 申报公司 Agape 是买方 |
| Supply | Part_III/Supply/ULTRAGENYXPHARMACEUTICALINC_12_23_2013-EX-10.9-SUPPLY AGREEMENT.PDF | 7 | 第 2 页 EXW (Incoterms 2010). 签订日在第 6 页. 卖方是德国的 Cremer |
| Reseller | Part_I/Reseller/HealthcareIntegratedTechnologiesInc_..._Reseller Agreement.pdf | 5 | 第 2 页: Returns Discount Program (退货与返利交织), 6.3, 6.4 被遮盖. 关键词没扫到所有权或风险转移, REV-03 预期给 more_evidence (待人工确认) |
| Manufacturing | Part_III/Manufacturing/Apollo Endosurgery - Manufacturing and Supply Agreement.PDF | 21 | 第 6 页: Ex Works, RMA. 第 5 页 4.2 Payment Terms. 卖方是 Establishment Labs |
| Service | Part_II/Service/PAXMEDICA,INC_07_02_2020-EX-10.12-Master Service Agreement.PDF | 7 | **CUAD 签订日 5/25/08 有误**, 正文第 1 页是 "May 25th, 2018". 对方是 "(Pty) Limited" (南非) |
| Maintenance | Part_III/Maintenance/SPARKLINGSPRINGWATER...SOFTWARE LICENSE AND MAINTENANCE AGREEMENT.PDF | 7 | 签订日 05/[]/2000 被遮盖. 末页印的是 "PAGE 12" |
| License | Part_I/License_Agreements/EuromediaHoldingsCorp_..._Content License Agreement.pdf | 12 | 签订日在第 1 页. Revenue/Profit Sharing 在第 2, 5, 7 页 |

备选样本: Scansource Distributor Agreement1 (27 页), DIVERSINET Reseller (15 页), WestPharmaceutical Supply (25 页, 控制权转移关键词命中 24 次).

注意事项:
- 印刷页码和 PDF 页序不一致 (见 LIMEENERGY, SPARKLING), 正好能测模型是否按 `<page number>` 块填页码.
- DOC-04 的前提是 "被审计单位是供应方". client_name 要逐份人工设为卖方, 而 SEC 申报公司并不一定是卖方 (Agape, Ultragenyx, Apollo 的申报公司都是买方).

## 3. 评测设计

**规模**: 建议以第 2 节的 10 份作 DOC-04 金标准集, REV-03 只跑其中 6 份货物合同 (Distributor 3, Supply 2, Reseller 1).

**成本与时延** (粗估):
- 一轮的输入: DOC-04 约 75k token 合同文本加 10k 左右提示词, REV-03 约 6 x 4k, 合计约 110k.
- 输出: 约 30-40k (条款要逐字照抄).
- 费用按所用模型单价乘以上述 token 数算, 我手头没有权威价目.
- 并发跑约 2-5 分钟.
- 再加一档只比对 CSV 自动真值的扩展集 (3-15 页的有 94 份, 可抽 30 份), 不跑 REV-03, 在需要时手动跑.

**比对方法**:
- **当事方**: 统一小写, 去标点, 去掉公司后缀 (inc/corp/llc/ltd/limited/co/pty/d/b/a), 用 difflib 算相似度 ≥0.9 或互相包含即算命中.
  - 环境里没有 rapidfuzz, 有 dateutil.
  - 别名表收入括号中的定义名.
- **日期**: CSV 按 m/d/yy 解析, yy≥90 补 19yy. 含 `[]` 的不计入. 等于 Agreement Date 或 Effective Date 都算对. 人工改正已发现的 2 处错误.
- **ABN**: 真值 null. 输出非 null 即判为编造, 一票否决.
- **金额**: 金标准人工确认为 null, 或人工填值.
- **条款召回**: CUAD 摘录 (按 `<omitted>` 拆开, 去非字母数字后取前 60 字符) 包含在某条同类型模型条款的文本里, 或落在同一起始页, 都算召回. 只对 termination 自动计算, guarantee 只报不判, 另外 4 类用人工金标准.
- **页码命中**: 模型条款的 page 等于摘录首段所在页. 跨页的允许 ±1, 出现在多页的接受页集合.
- **反编造检查 (不需要真值)**: 每条模型条款的 text 去非字母数字后, 前 60 字符必须出现在它自己声明的那一页.
  - 开头的条款号可去掉再取 (R249 排查): 提示词要求子条款连号照抄, 不带标题, 号与正文在原文里不一定相连, 如原文
    "2.3 Delivery. Other than" 写成 "2.3 Other than", "E. PAYMENT. Except" 写成 "4.E. Except".
  - 页文本里单独一行的印刷页码 (如 CUAD-03 的 "Page -8-") 可不算: 它可夹在条款中间, 照抄时略去.

**建议通过标准** (需拍板):
- 硬性要求:
  - 10 份里至少 9 份在重试次数内通过 OutputValidator.
  - ABN 10/10 为 null.
  - 反编造检查 ≥95%.
  - 不出现未处理异常.
- 软性要求:
  - 当事方 ≥9/10.
  - sign_date ≥8/10 (只算未遮盖的).
  - 页码命中 ≥85%.
  - termination 召回 ≥80%.
  - 货物合同的 control_transfer 抽到 ≥5/6.
  - REV-03 的 proposed_action 与人工金标准一致 ≥5/6.

**为适配澳洲场景要注意的问题**:
1. 第 1 节里 ABN 恒为 null, 依赖 ABN 的登记查询和关联方程序对 CUAD 合同跑不了.
2. 币种是美元, 金额多被遮盖.
3. 控制权转移是用 FOB, EXW, FCA 等贸易术语表达的, 不是 Numbat 那种 "签收送货单即转移". REV-03 只带了 AASB15-P31 和 P38 两个准则片段 (见 REV-03.yaml 第 15 行).
4. 许可, 服务, 维护类合同涉及 B52-B63, 现有提示词针对的是货物交付时点, 所以 REV-03 只评货物合同.
5. 风险: DOC-04 的 max_tokens 默认 4096 (contracts/knowledge.py 第 42 行), 逐字照抄的条款可能被截断, 导致语法级失败. 评测时可能要覆盖到 8192.
6. context_budget 8000 只是声明, 代码里没有执行. 10 份里有 4 份 (NETGEAR, Nanophase, LimeEnergy, Apollo) 按 字符数/4 估算超过 8000 token.
7. LiveLLMClient 默认超时 120 秒 (adapters/llm.py 第 121 行), 长合同调用可能接近这个上限.
8. pyproject 只注册了 `slow` 标记, R59 写的 `live` 标记还没定义.

## 4. 离线单元测试与 slow 测试的划分

**不联网, 默认就跑**: 需要入库一份 fixture, 内容是 1-3 份合同的逐页文本 JSON, 外加人工核对过的金标准和 mock 响应.
- OutputValidator._extraction 的引用级校验. 例如 LIMEENERGY 填了印刷页码 14, 而 page_count=10, 应判引用级失败. 正常输出应该通过.
- 评分函数的单元测试: 名称规范化, m/d/yy 带世纪补全与 `[]` 遮盖, 摘录的 `<omitted>` 拆分与页定位, 反编造检查.
- DOC-04 的 mock fixture 放在 tests/fixtures/llm/DOC-04/<键>.json, 测 ingest 能正常处理 ABN 为 null, 金额为 null 的输出.
- 补充一点: 现有校验只检查页码在 1 到 page_count 之间, 不检查条款文本是否真的在那一页. 反编造检查可以先放在评测里, 要不要并入正式校验需要拍板.

**依赖 data/external/cuad, 目录缺失就 skip**:
- 文件名映射全量核对 (510 份).
- 10 份金标准的页码定位, 抽取约 30 秒, 可以不标 slow.
- 174 份全量定位约 1-3 分钟, 标 slow.

**只能在 R59 真实 LLM 下跑** (标 slow 和 live, 并加上 API Key 与目录两个 skipif): DOC-04 跑 10 份金标准, REV-03 跑 6 份, 扩展集 30 份.

## 5. 许可与署名 (CC BY 4.0)

- **允许做的**: 允许复制, 修改, 再分发, 包括商用. 没有相同方式共享 (SA) 和禁止演绎 (ND) 的限制, 所以摘录, 逐页文本和模型输出都可以入库, 仓库本身也不必改用 CC BY.
- **署名要求**: 写明作者 The Atticus Project, 数据集名和版本, CC BY 4.0 及链接, 来源 (Zenodo 4595826). 如有修改, 要注明 "经 pdfplumber 抽取文本 / 截取 / 由模型生成" 等改动. 不得附加限制他人使用的条款或技术措施.
  - 本地 README.md 已经给出建议署名句.
- **建议做法**: 在 tests/fixtures/cuad/ 下放一份 README, 列出每份 fixture 对应的 CUAD 原文件名, 改动说明和署名.
  - 模型输出 fixture 含逐字条款, 属于改编材料, 同样要署名.
- **原始合同**: CUAD README 对底层 EDGAR 合同的许可状态不作保证 ("no representations ... license status of the underlying contracts").
  - 建议优先入库逐页文本或节选, 不放整份 PDF.
  - 如果要入库 PDF, 候选文件都很小: NETGEAR 72 KB, LimeEnergy 77 KB, Ultragenyx 83 KB, Nanophase 147 KB, Healthcare 219 KB, 和仓库 "大文件不入库" 的规定不冲突. 是否入库需要拍板.
- 仓库 CLAUDE.md 第 100 行已允许使用 CUAD.

探查脚本和扫描结果都在 /private/tmp/claude-501/cuad_probe/ 下, 主要是 scan.py, scan.json 和 inspect.py. 仓库和外部数据都没有改动.