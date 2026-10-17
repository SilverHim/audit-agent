"""Xero 演示数据集: 定稿文档 docs/Xero演示数据集设计.md 的内容守卫 (补造文件与校验脚本的测试由后续需求加在本文件)."""

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "Xero演示数据集设计.md"
SNAPSHOT = REPO / "data" / "external" / "xero" / "api" / "20261017-q1"


def doc() -> str:
    return DOC.read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """取以 heading 开头的一节, 到下一个同级或更高级标题为止."""
    level = len(heading) - len(heading.lstrip("#"))
    start = text.index(heading)
    rest = text[start + len(heading) :]
    m = re.search(rf"^#{{1,{level}}} ", rest, re.M)
    return text[start : start + len(heading) + (m.start() if m else len(rest))]


def login_contact_values(snapshot: Path) -> list[str]:
    """登录账号联系人的全名与邮箱 (小写), 口径同定稿第 9.4 节: IsCustomer 与 IsSupplier 都为假且有 FirstName 与
    EmailAddress, 恰好 1 条. 只在内存里用, 不打印."""
    data = json.loads((snapshot / "contacts.json").read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("Contacts", [])
    hits = [
        c
        for c in rows
        if not c.get("IsCustomer") and not c.get("IsSupplier") and c.get("FirstName") and c.get("EmailAddress")
    ]
    assert len(hits) == 1, f"登录账号联系人口径命中 {len(hits)} 条, 应为 1"
    c = hits[0]
    return [f"{c['FirstName']} {c.get('LastName', '')}".strip().lower(), str(c["EmailAddress"]).lower()]


def test_sections_match_design_numbering() -> None:
    text = doc()
    assert "设计稿 v2.1 第 n 节即本文第 n 节" in text
    assert "`docs/开发接口文档.md` 第 2, 9 节" in text
    for heading in (
        "## 1 文件布局与来源",
        "## 2 被审计单位, 人员与审计依据",
        "## 3 各表口径",
        "## 4 补造单据清单与要点",
        "## 5 程序改动与规则程序预期命中",
        "## 6 大模型程序预期输出要点",
        "## 7 演示剧本",
        "## 8 注入错误的测试设计",
        "## 9 补造文件的存放, 生成, 校验与载入",
    ):
        assert re.search(rf"^{re.escape(heading)}$", text, re.M), heading
    for n in range(1, 9):
        assert re.search(rf"^### 2\.{n} ", text, re.M), f"2.{n}"


def test_no_process_content() -> None:
    text = doc()
    for word in (
        "核查意见处理",
        "v2 审核意见处理",
        "v2.1 修订记录",
        "已统一的分歧",
        "默认采用的推荐项",
        "仍需用户拍板",
        "拍板",
        "/private/tmp",
        "核查 M",
        "核查 minor",
        "v2 审核",
        "本稿",
        "原稿",
        "本轮",
        "(T9)",
    ):
        assert text.count(word) == 0, word
    assert not re.search(r"`(?:s\d+[a-z]?_\w+|r\d_\w+|base)\.py`", text)  # 复算脚本名


def test_key_figures_kept() -> None:
    """关键数按所在节断言 (同一个数在别处出现不算); 本版改过的整行见 test_golden_lines."""
    text = doc()
    assert "即 32431 / 16216 / 1622 (分)" in text
    s0 = section(text, "## 0 要点")
    assert (
        "例外 41, 判断 56 (另有 XJ-575 改判产生的人工判断 1 条), 复核任务 53, 闸门决定 10 (ACC-08 单列一次, 依 G5), "
        "人工队列 4, 更正通知 2, 错报 5 (全部 corrected), 大模型调用约 66 次 (不含 A7 独立复核; 加 A7 约 84 次, "
        "第 7.3 节), 文件 29 份."
    ) in s0
    assert "毛额合计 9,757.09, 对损益影响 -8,910.03. 更正后本期亏损 22,136.99, 净负债 30,048.99" in s0
    s28 = section(text, "### 2.8 更正分录 (adjustments/journal_entries.csv, 全部合成)")
    for entry in (
        "借 711 82500, 贷 710 75000, 贷 820 7500.",
        "借 478 426120, 贷 826 426120.",
        "借 477 201000, 贷 804 201000.",
        "借 477 268736, 贷 805 268736.",
        "借 620 4853, 贷 270 4853.",
    ):
        assert entry in s28, entry
    s32 = section(text, "### 3.2 试算表与关键数")
    for row in (
        "| 本期损益 | -13,226.96 | -22,136.99 |",
        "| 流动资产 (透支列为负债) | 21,780.73 | 21,829.26 |",
        "| 流动比率 / 剔除 840 | 0.5084 / 0.9216 | 0.4208 / 0.6683 |",
        "| 净资产 / 剔除 840 | -21,138.96 / -1,926.75 | -30,048.99 / -10,836.78 |",
    ):
        assert row in s32, row
    s73 = section(text, "### 7.3 全部接入后的计数 (不含 GEN-09, 不含更正传导引起的重跑)")
    rows = {ln.split(" | ")[0][2:]: ln for ln in s73.splitlines() if ln.startswith("| ")}
    for name, head in (
        ("例外", "| 例外 | **41 条**:"),
        ("判断", "| 判断 | **56 条**:"),
        ("复核任务", "| 复核任务 | **53 条**,"),
        ("闸门决定", "| 闸门决定 | **10 次**:"),
        ("人工队列", "| 人工队列 | 4 项:"),
        ("更正通知", "| 更正通知 | 2 次 (AJE-X01 与 AJE-X05; AJE-X02 至 X04) |"),
        ("错报", "| 错报 | **5 条**, 毛额 9,757.09, 全部 corrected; 未更正 0 |"),
        ("document", "| document | 29 份 (第 4 节) |"),
        ("external_call", "| external_call | 9 行: ACC-04 2, CSH-04 2, GEN-08 5 |"),
        ("大模型调用", "| 大模型调用 | **约 66 次** (不含 A7 独立复核):"),
    ):
        assert rows[name].startswith(head), name
    assert "J-GEN-02-1-XJ-575-H1" in rows["判断"]
    assert "每条 2 次, 共 18 次, 全剧本 84 次" in s73
    s95 = section(text, "### 9.5 文件格式与载入端口 (实现时以此为准)")
    assert "共 29 份 55 页" in s95
    assert "CLIENT-XERO-3C1332BB-Q1" in section(text, "### 2.6 客户画像 (覆盖载入器产出)")
    assert "(24 行, 只覆盖本期生效发票" in section(text, "### 3.3 补造表的口径")
    assert "| adjustments |" in s31(text) and "5 笔 11 行" in s31(text)
    assert "19 行" in section(text, "### 9.1 目录与格式 (沿用 data/demo 的约定)")


def s31(text: str) -> str:
    return section(text, "### 3.1 从 Xero 映射的表 (载入器 `build_tables`)")


# 本版改过的关键行, 按所在节整行比对 (同步本文时改动这些行须同时改这里)
GOLDEN_LINES = [
    (
        "### 3.1 从 Xero 映射的表 (载入器 `build_tables`)",
        (
            "| trial_balance | current = `trial_balance_2026-09-30.json` "
            "(新快照由 Xero 接口给出), prior = 0630 表 | prior 含 FY26 损益行 (销售 4,20"
            "0 等), 载入器按 retained_earnings 分类结转 | current 27 (含余额为 0 的 801"
            ") / prior 6 |"
        ),
    ),
    (
        "### 6.6 计划, 重估, 勾稽与报告",
        ("- K02: 映射覆盖 09-30 试算表的 27 个科目 (含余额为 0 的 801) 加 804, 805, 826, 478, 620, 270, 共 33 个."),
    ),
    (
        "### 9.5 文件格式与载入端口 (实现时以此为准)",
        (
            "- fin08_round1.json: {procedure_id FIN-08, round 1, communic"
            'ated_at 2026-10-19T04:00:00Z (悉尼 15:00), recipient "Alex Mor'
            'gan, Director", form written, misstatement_sources [J-GEN-02'
            "-1-XJ-499, J-CSH-02-1-BA-091], responded_at 2026-10-19T22:00"
            ":00Z (悉尼 10-20 09:00, 早于 AJE-X01 的录入时间), response 第 4.8 节第 1"
            " 轮原文, adjustment_entry_nos [AJE-X01, AJE-X05]}."
        ),
    ),
    (
        "### 9.5 文件格式与载入端口 (实现时以此为准)",
        (
            "- fin08_round2.json: {procedure_id FIN-08, round 2, recipien"
            't "Alex Morgan, Director", form written, communicated_at 202'
            "6-10-21T04:00:00Z (悉尼 15:00), misstatement_sources [J-FIN-15"
            "-1-826, J-FIN-15-1-804, J-FIN-15-1-805], responded_at 2026-1"
            "0-21T22:00:00Z (悉尼 10-22 09:00), response 第 2 轮原文, adjustmen"
            "t_entry_nos [AJE-X02, AJE-X03, AJE-X04]}."
        ),
    ),
    (
        "### 7.3 全部接入后的计数 (不含 GEN-09, 不含更正传导引起的重跑)",
        (
            "| 例外 | **41 条**: PLN-07 9 (open, 闸门材料); REV-08 1, CSH-02 1 ("
            "D04, 决定 15), GEN-05 7, GEN-08 1, GEN-07 9, FIN-15 13 (第 1 轮 "
            "10, 第 2 轮 3), 均 attributed; 增量扫描 0; PLN-19 0 |"
        ),
    ),
    (
        "### 7.3 全部接入后的计数 (不含 GEN-09, 不含更正传导引起的重跑)",
        (
            "| 判断 | **56 条**: ACC-08 1, REV-03 3, REV-02 6, CSH-02 1, GEN"
            "-02 7, GEN-03 2, GEN-01 1, GEN-04 1, GEN-06 9, FIN-03 5, FIN"
            "-09 5, FIN-15 13, PLN-19 2. FIN-02 的建议是闸门材料 GOING_CONCERN v1"
            ', 不建判断 (设计稿 v2.1 计为 57 条, 含 "FIN-02 1"); 另有人工判断 1 条 (J-GEN-0'
            "2-1-XJ-575-H1, 审计师 override XJ-575 产生) |"
        ),
    ),
    (
        "### 7.3 全部接入后的计数 (不含 GEN-09, 不含更正传导引起的重跑)",
        (
            "| 复核任务 | **53 条**, 都已决定: REV-03 3, REV-02 6, CSH-02 1, GEN-0"
            "2 7, GEN-03 2, GEN-01 1, GEN-04 1, GEN-06 9, FIN-03 5, FIN-0"
            "9 5, FIN-15 13 (盲判恰为 9 条: REV-03 3, REV-02 6) |"
        ),
    ),
    (
        "### 7.3 全部接入后的计数 (不含 GEN-09, 不含更正传导引起的重跑)",
        (
            "| 大模型调用 | **约 66 次** (不含 A7 独立复核): DOC-04 3, DOC-06 2, ACC-0"
            "8 2 (按 G5 定), PLN-15 1, REV-03 3, REV-02 6, CSH-02 1, GEN-02"
            " 7, GEN-03 2, GEN-01 1, GEN-04 1, GEN-06 9, FIN-03 5, FIN-09"
            " 5, PLN-19 2, FIN-15 13, FIN-02 1, RPT-03 2 |"
        ),
    ),
    (
        "### 7.3 全部接入后的计数 (不含 GEN-09, 不含更正传导引起的重跑)",
        (
            "A7 独立复核 (GEN-09, 已在 main 上运行) 另复核 GEN-06 的 9 条判断 (L3 accept "
            "命中主规则), 每条 2 次, 共 18 次, 全剧本 84 次 (合并终验定值, 共用裁定第 8 批); 其余判断所在"
            "程序为 L1 或 L2, 不进 A7, 推理链含假设步骤或按现有抽样公式 (判断 ID 的 sha256, 抽样率 0."
            "05) 抽中的判断也因此不复核. 若实现改变 A7 的触发或程序的自主度, 同一提交同步本节."
        ),
    ),
    (
        "### 2.4 审计依据与期间",
        (
            "**第 2 层下各准则的适用范围** (原文已核): AASB 101 (第 AusB1 段), AASB 107 (第"
            " AusA1 段), AASB 124 (第 AusB1 段) 不适用于采用 AASB 1060 的主体, 相应列报与披"
            "露依据改引 AASB 1060 (段号暂缓, 待 AASB 1060 正本); AASB 110 只有第 13, 16,"
            " 17-22 段不适用 (第 AusA1 段); AASB 15 只有第 110-129 段与 B87-B89 不适用 "
            "(第 AusE1 段). 所以 GEN-06 引 AASB 110 第 3(b), 10 段与 AASB 15 第 88"
            " 段, GEN-03 引 AASB 124 只作参照."
        ),
    ),
    (
        "### 9.3 载入器需要新增的读取点",
        (
            "10. 更正分录: 不在载入时入库. G4 的 CorrectionNotice (adjustments_posted"
            ") 增加 `adjustment_entry_nos`, `load_adjustments` 只载入列出的分录 (本数"
            "据集分两轮: AJE-X01 与 AJE-X05; AJE-X02 至 X04). Numbat 只有一轮, 不填时载入"
            "全部, 行为不变."
        ),
    ),
    (
        "### 7.2 剧本 (顺序按规格书 1425 行: 更正验证 → PLN-19 → FIN-04 → FIN-15)",
        ("| 6 | — | PLN-01/03 | MATERIALITY v1; 风险清单 3 条 | — | approve → 32431 / 16216 / 1622 |"),
    ),
    (
        "### 7.1 审计师的选择与降级",
        (
            "- 动态降级 (需求清单 R168 的降级规则; 首次服务该客户不降级, 按决定 2 本版不做): 收入特别风险 -2,"
            " 现金 high -1; REV-02, REV-03 触到 L1 下限并开盲判. 执行重要性只有 162.16, 带金"
            '额的判断几乎都按 "单项金额超过执行重要性" 降到 L1.'
        ),
    ),
    (
        "### 9.4 是否入库, 生成与校验脚本",
        (
            "  - 复算第 3 节的快照关键数 (分录与作废对数, 09-30 试算表与主要余额, 对账单行数, 月度收入, 重要性"
            ", 录入时间晚于 10-19 的 15 笔) 并比对 20261017-mock (第 9.2 节); 第 5 节规则命"
            '中与第 7 节计数不在校验脚本复算 (见 "本版不做"), 分别由各程序的连库测试与合并终验核对.'
        ),
    ),
    (
        "### 9.2 快照怎么来, 怎么对应",
        ("- **新快照 (已拉取)**: 本数据集的快照已在 Xero Demo 重置前拉取, 重拉时用:"),
    ),
]


def test_golden_lines() -> None:
    text = doc()
    for heading, line in GOLDEN_LINES:
        assert line in section(text, heading).splitlines(), line[:60]
    assert "按决定 8 与 11 重写" in section(text, "### 4.3 收入合同")
    assert "快照不入库" not in section(text, "### 9.4 是否入库, 生成与校验脚本")
    assert "需要给 pull 加" not in section(text, "### 9.2 快照怎么来, 怎么对应")
    assert "AdjustmentEntrySummary]` (entry_no, posting_date, description, line_count)" in text


def test_repository_facts_and_skip_guard() -> None:
    text = doc()
    assert "git add -f" in section(text, "## 1 文件布局与来源")
    s81 = section(text, "### 8.1 原则与副本生成")
    assert "没有本地快照的检出怎么跑" in s81
    assert "快照不入库时怎么跑" not in s81


INDEX_ROWS = [
    "| documents/engagement/appointment.pdf | minutes | internal_copy | 2026-08-27 | |",
    "| documents/engagement/engagement_letter.pdf | engagement_letter | external_copy | 2026-08-28 | |",
    "| documents/related_party/vpa_rent_letter.pdf | agreement | external_copy | 2026-06-25 "
    "| ACN-900043030;XJ-420;XJ-518;XJ-593 |",
    "| documents/related_party/vpa_sale_termination_notice.pdf | correspondence | external_copy "
    "| 2026-08-28 | ACN-900043030 |",
    "| documents/related_party/harbourview_lease.pdf | agreement | external_copy | 2026-09-22 | ACN-900043030;XJ-613 |",
    "| documents/related_party/rm2031_sublease.pdf | agreement | external_copy | 2026-10-15 | BT-090-0131 |",
    "| documents/evidence/director_loan_agreement.pdf | agreement | internal_original | 2018-09-10 | XJ-397 |",
    "| documents/evidence/director_loan_confirmation.pdf | correspondence | internal_original | 2026-10-16 "
    "| XJ-397;CLIENT-XERO-3C1332BB-Q1 |",
    "| documents/evidence/going_concern_memo_forecast.pdf | internal_record | internal_original | 2026-10-16 "
    "| CLIENT-XERO-3C1332BB-Q1 |",
    "| documents/evidence/director_support_letter.pdf | correspondence | internal_original | 2026-10-16 "
    "| CLIENT-XERO-3C1332BB-Q1;BT-090-0107;BT-090-0130 |",
    "| documents/evidence/fixed_asset_register.pdf | internal_record | internal_original | 2026-09-30 "
    "| XJ-499;XJ-626;XJ-676 |",
    "| documents/evidence/expense_claim_E003.pdf | internal_record | internal_original | 2026-09-14 | E003 |",
    "| documents/evidence/payroll_register_terms.pdf | internal_record | internal_original "
    "| 2026-09-30 | 804;805;826 |",
    "| documents/evidence/accounting_policies_extract.pdf | internal_record | internal_original "
    "| 2026-07-01 | XJ-499 |",
    "| documents/evidence/diisr_email.pdf | correspondence | external_copy | 2026-10-13 | XJ-720 |",
    "| documents/evidence/smart_agency_invoice_SM0195-70135.pdf | supplier_invoice | external_copy | 2026-09-05 "
    "| XJ-548;BT-090-0097 |",
    "| documents/evidence/ridgeway_order_CRM08-12.pdf | agreement | external_copy | 2026-10-02 | XJ-647;BT-090-0092 |",
    "| documents/evidence/jakaranda_quote.pdf | internal_record | internal_copy | 2026-10-08 | BT-090-0099 |",
    "| financial_statements/fs_v1.pdf | other | internal_original | 2026-10-22 | FS |",
]
# 待审财报 18 行: (code, amount_cents, sign, account_codes, prior_amount_cents)
FS_ROWS = [
    ("SOFP-CASH", "1158123", "+1", "091", "1508123"),
    ("SOFP-REC", "1019950", "+1", "610", "0"),
    ("SOFP-OREC", "4853", "+1", "620", "0"),
    ("SOFP-PPE", "0", "+1", "710, 711", "0"),
    ("SOFP-TA (小计)", "2182926", "+1", "091, 610, 620, 710, 711", "1508123"),
    ("SOFP-OD", "1090044", "-1", "090", "457222"),
    ("SOFP-PAY", "400507", "-1", "800, 801, 860", "0"),
    ("SOFP-TAX", "880197", "-1", "820, 825", "-79120"),
    ("SOFP-EMP", "895856", "-1", "804, 805, 826", "0"),
    ("SOFP-DLOAN", "1921221", "-1", "840", "1921221"),
    ("SOFP-TL (小计)", "5187825", "-1", "090, 800, 801, 860, 820, 825, 804, 805, 826, 840", "2299323"),
    ("SOFP-EQ", "-3004899", "-1", "960 与全部损益科目", "-791200"),
    ("PL-REV", "3243100", "-1", "200", "420000"),
    ("PL-OI", "4853", "-1", "270", "0"),
    ("PL-EMP", "4245856", "+1", "477, 478", "0"),
    ("PL-OCC", "635600", "+1", "408, 445, 469", "0"),
    ("PL-OTH", "580196", "+1", "400, 404, 412, 420, 429, 449, 453, 461, 489, 493", "1211200"),
    ("PL-NP (小计)", "-2213699", "-1", "全部损益科目", "-791200"),
]


def test_formats_section() -> None:
    s95 = section(doc(), "### 9.5 文件格式与载入端口 (实现时以此为准)")
    lines = s95.splitlines()
    assert [ln for ln in lines if re.match(r"^\| (?:documents|financial_statements)/", ln)] == INDEX_ROWS
    fs = [tuple(c.strip() for c in ln.strip("|").split("|")) for ln in lines if re.match(r"^\| (?:SOFP|PL)-", ln)]
    assert fs == FS_ROWS
    assert (
        "E001 Oliver Gray 2026-07-20 full_time 14 240000; E002 Tracy Green 2026-07-20 full_time 14 180000; "
        "E003 Odette Garrison 2026-07-20 full_time 14 250000."
    ) in s95
    for token in (
        "load 20 (3 份收入合同加 index.csv 中除承接文件外的 17 份, 载入时登记), engagement 2",
        "registry 4",
        "confirmation 2",
        "upload 1",
        "`SR-{发票号}`",
        "gross_per_period_cents",
        "communicated_at 2026-10-19T04:00:00Z",
        "responded_at 2026-10-19T22:00:00Z",
        "adjustment_entry_nos [AJE-X01, AJE-X05]",
        "adjustment_entry_nos [AJE-X02, AJE-X03, AJE-X04]",
        "representation.json",
        "rpt07_reply.json",
        "tests/fixtures/llm_xero/<程序>/<对象>.json",
        "tb_shift",
        "`XeroDatasetLoader.tables(dataset)`",
        "`EvidenceStore.save(..., document_date=None, related_ids=())`",
        "load_adjustments(eid, dataset, entry_nos=None)",
        "adjustment_entries(dataset) -> list[AdjustmentEntrySummary]",
        "adjustment_entry_nos(dataset) -> list[str]",
        "load_engagement_documents(eid, dataset)",
        "supplements: Path | None = None, allow_injected: bool = False",
        "`RegistrySource.abn(eid, abn)`, `asic_company(eid, acn)`, `asic_extract(eid, acn)`, "
        "`asic_person(eid, family_name, given_name, birth_date)`",
    ):
        assert token in s95, token


def test_out_of_scope_listed() -> None:
    nd = section(doc(), "## 本版不做")
    for token in (
        "收款与发票对应",
        "reverses_entry_no",
        "derive_xero_period.py",
        "最小 fixture",
        "规则命中",
        "更正传导",
    ):
        assert token in nd, token
    demo = section((REPO / "docs" / "Demo需求文档.md").read_text(encoding="utf-8"), "## 6. 本版不做")
    assert "收款与发票对应表" in demo
    assert "reverses_entry_no" in demo


def test_aasb1060_paragraphs_deferred() -> None:
    text = doc()
    lines = [ln for ln in text.splitlines() if "AASB 1060" in ln]
    assert lines
    assert all("暂缓" in ln for ln in lines)
    assert not re.search(r"AASB 1060 (?:第 ?|para(?:graph)? ?)\d", text)
    rest = text
    for allowed in ("待 AASB 1060 正本", "## 待核与待正本", "待正本的段号"):
        rest = rest.replace(allowed, "")
    assert "待正本" not in rest  # 不许有 "(待正本)" 一类不规范写法


def test_pointers_from_other_docs() -> None:
    assert "| `docs/Xero演示数据集设计.md` |" in (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    design = (REPO / "docs" / "云端开发" / "design" / "xero" / "设计稿-v2.md").read_text(encoding="utf-8")
    assert "定稿见 `docs/Xero演示数据集设计.md`" in design.split("\n## ", 1)[0]


def test_no_login_contact_in_doc() -> None:
    if not (SNAPSHOT / "contacts.json").exists():
        pytest.skip("没有本地 Xero 快照")
    text = doc().lower()
    found = [i for i, value in enumerate(login_contact_values(SNAPSHOT)) if value in text]
    assert not found, f"{DOC.name} 含登录账号联系人信息"  # 只报文件名, 不打印原值
