# Synthetic – for demonstration only

本目录是 Xero 演示数据集 xero/20261017-q1 的补造文件, 全部合成, 由 data/generate_xero_demo.py 按 data/xero_demo_spec.json 生成, 不要手改. 各文件的哈希与合成标志见 manifest.json, 设计见 docs/Xero演示数据集设计.md.

## 收入合同的底稿 (contracts/)

3 份收入合同各以下面这个数据集中的一份合同为底稿, 已改写:

Contract Understanding Atticus Dataset (CUAD) v1, The Atticus Project, CC BY 4.0.

- 来源: Zenodo 记录 4595826, https://zenodo.org/records/4595826, 版本 v1.
- 许可: Creative Commons Attribution 4.0, https://creativecommons.org/licenses/by/4.0/
- CUAD 的免责句: "We make no representations or warranties regarding the license status of the underlying contracts, which are publicly available and downloadable from EDGAR."
- CUAD 的使用条款: "The use of CUAD is subject to our privacy policy https://www.atticusprojectai.org/privacy-policy and disclaimer https://www.atticusprojectai.org/disclaimer."
- 改写方式: 当事方, 合同编号, 金额, 日期, 完工与控制权转移, 开票与付款, 贷项与终止条款按设计稿第 4.3 节编写; 合同结构与一般条款 (服务方式, 双方配合, 保密, 责任限制, 不可抗力, 转让, 变更与适用法律等) 取自底稿, 删改后改写为 Demo Company (AU) 与其客户之间适用新南威尔士州法律的合同. 每份合同第 3 页末注明出处, 已修改与许可链接; 取用的底稿原句记在生成规格各合同的 base.excerpts.
- 底稿 (CUAD master_clauses.csv 的文件名, manifest 的 files 段同记):
  - contracts/CON-X01.pdf: ClickstreamCorp_20200330_1-A_EX1A-6 MAT CTRCT_12089935_EX1A-6 MAT CTRCT_Development Agreement.pdf
  - contracts/CON-X02.pdf: TRIZETTOGROUPINC_08_18_1999-EX-10.17-TECHNICAL INFRASTRUCTURE MAINTENANCE AGREEMENT.PDF
  - contracts/CON-X03.pdf: MERITLIFEINSURANCECO_06_19_2020-EX-10.(XIV)-MASTER SERVICES AGREEMENT.PDF
- 合同 PDF 已入库, 使用时不需要 CUAD. 核对底稿要先取 CUAD 到 data/external/cuad/ (不入库): `uv run python -m tests.tools.cuad_fetch`.
