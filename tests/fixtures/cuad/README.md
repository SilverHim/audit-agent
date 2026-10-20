# CUAD 节选 (DOC-04 评测的离线 fixture)

本目录的逐页文本与 mock 输出取自 CUAD v1, 供 tests/test_cuad_eval.py 离线使用 (需求 R246; 设计说明见 docs/云端开发/design/external/cuad.md).

## 署名与许可

- 署名: Contract Understanding Atticus Dataset (CUAD) v1, The Atticus Project, CC BY 4.0.
- 作者: The Atticus Project. 数据集: Contract Understanding Atticus Dataset (CUAD), 版本 v1.
- 许可: Creative Commons Attribution 4.0 International, https://creativecommons.org/licenses/by/4.0/
- 来源: Zenodo 记录 4595826, https://zenodo.org/records/4595826
  - 原数据由 `uv run python -m tests.tools.cuad_fetch` 取到 data/external/cuad/, 不入库.
- 免责 (CUAD README 原句): "We make no representations or warranties regarding the license status of the underlying contracts, which are publicly available and downloadable from EDGAR."

## 改动说明

- **pdfplumber 抽取**: `pages/<别名>.json` 是 PDF 文本层的逐页文本, 由 `uv run python -m tests.tools.cuad_eval` 从原 PDF 抽出. 抽取调用同系统的 FileEvidenceStore.pages, 即 `page.extract_text() or ""`, 没有改写. 文件里另记了原文件路径, PDF 的 sha256 与页数.
- **节选**: 只取 510 份合同中的 3 份. mock 输出里的条款文本是逐页文本的连续片段, 换行合并为空格, 跨页的条款接上下一页.
- **人工标注**: mock 输出中的当事方, 签订日, 证据页码, 条款类型与页码, 都由开发者按原文人工标注.
  - ABN 与合同金额为 null: CUAD 没有这两项, 合同也只定单价.
  - 签订日取双方签字日中较晚的一个.
- **模型生成**: 本目录暂无真实模型的输出. `llm/` 下的文件是人工写成的离线 mock, 格式同 DOC-04 的模型输出 (ContractExtraction). 以后放入真实模型生成的输出时, 在此逐份注明.
- 不入 PDF: 本目录不放合同 PDF 原件.

## 别名

原文件名含逗号, 空格与 #. 合同编号与文件 ID 一律用别名 CUAD-nn.

| 别名 | 原文件 (CUAD_v1/full_contract_pdf 下) | 页数 | 卖方 (被审计单位) | 对方 |
|---|---|---|---|---|
| CUAD-01 | `Part_II/Distributor/NETGEAR,INC_04_21_2003-EX-10.16-DISTRIBUTOR AGREEMENT.pdf` | 8 | NETGEAR, Inc. | Ingram Micro |
| CUAD-03 | `Part_III/Distributor/LIMEENERGYCO_09_09_1999-EX-10-DISTRIBUTOR AGREEMENT.PDF` | 10 | Electric City Corp. | Electric City of Illinois LLC |
| CUAD-05 | `Part_III/Supply/ULTRAGENYXPHARMACEUTICALINC_12_23_2013-EX-10.9-SUPPLY AGREEMENT.PDF` | 7 | Cremer Oleo GmbH & Co KG | Ultragenyx Pharmaceutical Inc. |

master_clauses.csv 的 Filename 为原文件的文件名部分.

## 入库的含 CUAD 文本的文件

- `pages/CUAD-01.json`, `pages/CUAD-03.json`, `pages/CUAD-05.json`: 逐页文本.
- `llm/DOC-04/CUAD-01.json`, `llm/DOC-04/CUAD-03.json`: DOC-04 的离线 mock 输出. 放在本目录, 不放 tests/fixtures/llm, 免得改动 Numbat 的 fixture 守卫.
- `tests/test_cuad_eval.py`: 用例里引用的签订日摘录与条款开头.
- `tests/tools/cuad_eval.py`: 别名到原文件名的映射.
- 本 README: 当事方名称与原文件名.
