# CUAD 节选 (DOC-04 评测的离线 fixture)

本目录的逐页文本, mock 输出与金标准取自 CUAD v1, 供 tests/test_cuad_eval.py 离线使用与真实模型评测 (需求 R246, R247; 设计说明见 docs/云端开发/design/external/cuad.md).

## 署名与许可

- 署名: Contract Understanding Atticus Dataset (CUAD) v1, The Atticus Project, CC BY 4.0.
- 作者: The Atticus Project. 数据集: Contract Understanding Atticus Dataset (CUAD), 版本 v1.
- 许可: Creative Commons Attribution 4.0 International, https://creativecommons.org/licenses/by/4.0/
- 来源: Zenodo 记录 4595826, https://zenodo.org/records/4595826
  - 原数据由 `uv run python -m tests.tools.cuad_fetch` 取到 data/external/cuad/, 不入库.
- 免责 (CUAD README 原句): "We make no representations or warranties regarding the license status of the underlying contracts, which are publicly available and downloadable from EDGAR."

## 改动说明

- **pdfplumber 抽取**: `pages/<别名>.json` 是 PDF 文本层的逐页文本, 由 `uv run python -m tests.tools.cuad_eval` 从原 PDF 抽出. 抽取调用同系统的 FileEvidenceStore.pages, 即 `page.extract_text() or ""`, 没有改写. 文件里另记了原文件路径, PDF 的 sha256 与页数.
- **节选**: 逐页文本只取 510 份合同中的 3 份, 金标准取 10 份. mock 输出里的条款文本是逐页文本的连续片段, 换行合并为空格, 跨页的条款接上下一页.
- **人工标注**: mock 输出与金标准中的当事方, 签订日, 证据页码, 条款类型与页码, 都由开发者按原文人工标注.
  - `gold.json`: 10 份合同的金标准. 每份的 `review` 为 "开发者起草" (开发者逐份读 PDF 起草) 或 "用户抽查" (用户已按原文核对). 条款锚点是条款正文开头 (不含标题) 去掉非字母数字后的前 60 个字符, 由脚本从 PDF 文本取出.
  - 卖方 (被审计单位) 逐份人工定: SEC 申报公司不一定是卖方 (Agape, Ultragenyx, Apollo 的申报公司是买方).
  - 签订日与 CSV 不同或有取舍的, 写在 `sign_date_note`: LIMEENERGY 的 CUAD 原值 7/7/99 有误, 取首段的 1999-09-07; PAXMEDICA 首段 2018-05-25 与签字栏 25/05/2008 都接受; Agape 首段 2018-01-15 与签字页较晚的 2018-01-31 都接受; SPARKLING 的日空着, 不计分.
  - 货物合同 (CUAD-01 至 06) 另标付款, 控制权转移, 退货, 返利四类条款与 REV-03 的结论. Apollo (CUAD-07) 也是货物合同, 但只评 DOC-04: 21 页, 10 份中最长, 控制 REV-03 的输入 token. 终止条款的真值由 master_clauses.csv 的 Termination For Convenience, Notice Period To Terminate Renewal, Post-Termination Services 摘录在评测时生成, 不另标.
  - ABN 与合同金额为 null: CUAD 没有这两项, 10 份合同也都没有总价 (只定单价, 分成, 或被遮盖).
  - mock 输出的签订日取双方签字日中较晚的一个 (同 DOC-04 的规则); 金标准另接受首段或 CSV 的日期.
- **校验和**: `SHA256SUMS` 记金标准 10 份 PDF 与 master_clauses.csv, CUAD_v1.json 的 sha256 (路径相对 CUAD_v1/), 本地有 CUAD 时由 tests/test_cuad_local.py 核对.
- **模型生成**: 本目录暂无真实模型的输出. `llm/` 下的文件是人工写成的离线 mock, 格式同 DOC-04 的模型输出 (ContractExtraction). 以后放入真实模型生成的输出时, 在此逐份注明.
- 不入 PDF: 本目录不放合同 PDF 原件.

## 别名

原文件名含逗号, 空格与 #. 合同编号与文件 ID 一律用别名 CUAD-nn.

| 别名 | 原文件 (CUAD_v1/full_contract_pdf 下) | 页数 | 卖方 (被审计单位) | 对方 |
|---|---|---|---|---|
| CUAD-01 | `Part_II/Distributor/NETGEAR,INC_04_21_2003-EX-10.16-DISTRIBUTOR AGREEMENT.pdf` | 8 | NETGEAR, Inc. | Ingram Micro |
| CUAD-02 | `Part_III/Distributor/NANOPHASETECHNOLOGIESCORP_11_01_2005-EX-99.1-DISTRIBUTOR AGREEMENT.PDF` | 16 | Nanophase Technologies Corporation | Johnson Matthey Catalog Company, Inc. |
| CUAD-03 | `Part_III/Distributor/LIMEENERGYCO_09_09_1999-EX-10-DISTRIBUTOR AGREEMENT.PDF` | 10 | Electric City Corp. | Electric City of Illinois LLC |
| CUAD-04 | `Part_I/Supply/AgapeAtpCorp_20191202_10-KA_EX-10.1_11911128_EX-10.1_Supply Agreement.pdf` | 13 | Organic Preparations Inc. | Agape ATP International Holding Limited |
| CUAD-05 | `Part_III/Supply/ULTRAGENYXPHARMACEUTICALINC_12_23_2013-EX-10.9-SUPPLY AGREEMENT.PDF` | 7 | Cremer Oleo GmbH & Co KG | Ultragenyx Pharmaceutical Inc. |
| CUAD-06 | `Part_I/Reseller/HealthcareIntegratedTechnologiesInc_20190812_8-K_EX-10.1_11776966_EX-10.1_Reseller Agreement.pdf` | 5 | Vayyar Imaging Ltd. | Inde Living Holdings, Inc. |
| CUAD-07 | `Part_III/Manufacturing/Apollo Endosurgery - Manufacturing and Supply Agreement.PDF` | 21 | Establishment Labs, S.A | Apollo Endosurgery, Inc. |
| CUAD-08 | `Part_II/Service/PAXMEDICA,INC_07_02_2020-EX-10.12-Master Service Agreement.PDF` | 7 | CRO Consulting (Pty) Limited | Purinix Pharmaceuticals LLC |
| CUAD-09 | `Part_III/Maintenance/SPARKLINGSPRINGWATERHOLDINGSLTD_07_03_2002-EX-10.13-SOFTWARE LICENSE AND MAINTENANCE AGREEMENT.PDF` | 7 | Garman Routing Systems, Inc. | Sparkling Spring Water Group Limited |
| CUAD-10 | `Part_I/License_Agreements/EuromediaHoldingsCorp_20070215_10SB12G_EX-10.B(01)_525118_EX-10.B(01)_Content License Agreement.pdf` | 12 | EuroMedia Holdings Corp. | Rogers Cable Communications Inc. |

master_clauses.csv 的 Filename 为原文件的文件名部分.

## 入库的含 CUAD 文本的文件

- `pages/CUAD-01.json`, `pages/CUAD-03.json`, `pages/CUAD-05.json`: 逐页文本.
- `gold.json`: 金标准 (当事方名称, 条款锚点).
- `SHA256SUMS`: 原文件名与校验和.
- `llm/DOC-04/CUAD-01.json`, `llm/DOC-04/CUAD-03.json`: DOC-04 的离线 mock 输出. 放在本目录, 不放 tests/fixtures/llm, 免得改动 Numbat 的 fixture 守卫.
- `tests/test_cuad_eval.py`: 用例里引用的签订日摘录与条款开头.
- 本 README: 当事方名称与原文件名.
