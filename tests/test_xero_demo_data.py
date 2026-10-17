"""Xero 演示数据集: 定稿文档 docs/Xero演示数据集设计.md 的内容守卫, 补造文件 (data/xero_demo/) 与生成, 校验脚本."""

import json
import re
import shutil
import sys
from pathlib import Path

import pdfplumber
import pytest

REPO = Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "Xero演示数据集设计.md"
SNAPSHOT = REPO / "data" / "external" / "xero" / "api" / "20261017-q1"
SUPPLEMENTS = REPO / "data" / "xero_demo" / "20261017-q1"
sys.path.insert(0, str(REPO / "data"))  # 生成与校验脚本在 data/, 以脚本方式互相导入

import check_xero_demo as checker  # noqa: E402
import generate_xero_demo as generator  # noqa: E402

HEADER = "Synthetic – for demonstration only. Fictitious document based on Xero Demo Company (AU) data."
needs_snapshot = pytest.mark.skipif(not SNAPSHOT.is_dir(), reason="没有本地 Xero 快照")


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


# ── 补造文件: 生成规格, 生成脚本与校验脚本 ─────────────────────


def spec() -> dict:
    return json.loads(generator.SPEC.read_text(encoding="utf-8"))


def json_block(text: str) -> object:
    """取一节里第一个 ```json 代码块."""
    m = re.search(r"```json\n(.*?)```", text, re.S)
    assert m, "没有 json 代码块"
    return json.loads(m.group(1))


def read(name: str) -> object:
    return json.loads((SUPPLEMENTS / name).read_text(encoding="utf-8"))


def copy_supplements(tmp_path: Path) -> Path:
    target = tmp_path / "supplements" / "20261017-q1"
    shutil.copytree(SUPPLEMENTS, target)
    return target


def fake_snapshot(tmp_path: Path, contacts: list[dict]) -> Path:
    """tmp 下自造的快照: 只有合成的组织信息与联系人, 没有真实快照的任何内容."""
    root = tmp_path / "snapshot" / "20261017-q1"
    root.mkdir(parents=True)
    org = [{"OrganisationID": "3c1332bb-0000-4000-8000-000000000000"}]
    (root / "organisation.json").write_text(json.dumps(org), encoding="utf-8")
    (root / "contacts.json").write_text(json.dumps(contacts), encoding="utf-8")
    return root


def check_error(directory: Path, snapshot: Path, *, determinism: bool = False, **options: object) -> str:
    with pytest.raises(checker.CheckError) as raised:
        checker.check(directory, snapshot, determinism=determinism, **options)  # type: ignore[arg-type]
    return str(raised.value)


def test_spec_layout() -> None:
    data = spec()
    assert list(data) == [
        "dataset",
        "snapshot",
        "header",
        "traits",
        "profile",
        "planning",
        "registry",
        "documents",
        "contract_links",
        "delivery_notes",
        "bank_accounts",
        "receipts",
        "payroll",
        "adjustments",
        "financial_statements",
        "manual",
        "notes",
    ]
    assert (data["dataset"], data["snapshot"], data["header"]) == ("xero/20261017-q1", "20261017-q1", HEADER)
    for bank in ("NAB", "National Australia Bank", "Ridgeway Savings", "Business Savings Bank", "Wattlebrook Bank"):
        assert bank in data["notes"], bank
    assert data["notes"].startswith("Synthetic – for demonstration only.")


def test_generated_values_match_design() -> None:
    text = doc()
    assert read("dataset_traits.json") == json_block(section(text, "### 5.2 数据集声明 dataset_traits.json"))
    assert set(read("dataset_traits.json")) == checker.TRAIT_KEYS  # type: ignore[arg-type]
    assert read("client_profile.json") == json_block(section(text, "### 2.6 客户画像 (覆盖载入器产出)"))
    from backend.contracts.models import ClientProfile

    profile = ClientProfile.model_validate(read("client_profile.json"))
    assert (profile.id, profile.abn, profile.directors) == ("CLIENT-XERO-3C1332BB-Q1", "25900043021", ["Alex Morgan"])
    planning = REPO / "src" / "backend" / "knowledge" / "planning"
    template = json.loads((planning / "audit_objectives.json").read_text(encoding="utf-8"))
    assert read("planning/audit_objectives.json") == template  # 同通用模板
    risks = read("planning/risk_register.json")
    generic = json.loads((planning / "risk_register.json").read_text(encoding="utf-8"))
    assert [{k: v for k, v in r.items() if k != "description"} for r in risks] == [  # type: ignore[union-attr]
        {k: v for k, v in r.items() if k != "description"} for r in generic
    ]
    s27 = section(text, "### 2.7 计划预置 (planning/)")
    for risk in risks:  # type: ignore[union-attr]
        assert f"- **{risk['id']}**: {risk['description']}" in s27.splitlines(), risk["id"]
    assert [(r["level"], r["fraud_related"]) for r in risks] == [  # type: ignore[union-attr]
        ("significant", True),
        ("significant", True),
        ("high", False),
    ]


def test_manifest_layout() -> None:
    manifest = read("manifest.json")
    assert list(manifest) == ["dataset", "generator", "spec_sha256", "snapshot", "files", "documents"]  # 无 injected
    assert manifest["generator"] == "data/generate_xero_demo.py"  # type: ignore[index]
    assert manifest["snapshot"]["name"] == "xero/20261017-q1"  # type: ignore[index]
    assert [f["path"] for f in manifest["files"]] == [  # type: ignore[index]
        "client_profile.json",
        "dataset_traits.json",
        "documents/engagement/appointment.pdf",
        "documents/engagement/engagement_letter.pdf",
        "documents/index.csv",
        "planning/audit_objectives.json",
        "planning/risk_register.json",
        "registry/abn.json",
        "registry/asic.json",
        "registry/extracts/900043021.json",
        "registry/extracts/900043021.pdf",
        "registry/extracts/900043030.json",
        "registry/extracts/900043030.pdf",
        "registry/extracts/900043049.json",
        "registry/extracts/900043049.pdf",
        "registry/persons/MORGAN-ALEX-19790412.json",
        "registry/persons/MORGAN-ALEX-19790412.pdf",
    ]
    for item in manifest["files"]:  # type: ignore[index]
        assert list(item) == ["path", "sha256", "synthetic", "generator", "xero_refs"]
        assert item["synthetic"] is True
    raw = (SUPPLEMENTS / "manifest.json").read_text(encoding="utf-8")
    assert str(REPO) not in raw and "/home/" not in raw and not re.search(r"\d{4}-\d{2}-\d{2}T\d", raw)
    assert not [p for p in SUPPLEMENTS.rglob("*") if "synthetic" in p.name.lower()]  # 文件名不写 synthetic
    if SNAPSHOT.is_dir():
        names = sorted(p.name for p in SNAPSHOT.iterdir() if p.is_file() and p.name != "contacts.json")
        assert [f["path"] for f in manifest["snapshot"]["files"]] == names  # type: ignore[index]
        assert len(names) == 16


def test_check_passes() -> None:
    notes = checker.check(SUPPLEMENTS, SNAPSHOT)  # 有快照时含确定性重生成与联系人扫描
    if SNAPSHOT.is_dir():
        assert notes == []
    else:
        assert len(notes) == 1 and "没有本地 Xero 快照" in notes[0]


def test_check_without_snapshot_still_checks_files(tmp_path: Path) -> None:
    notes = checker.check(SUPPLEMENTS, tmp_path / "absent")
    assert len(notes) == 1 and "没有本地 Xero 快照" in notes[0]
    copy = copy_supplements(tmp_path)
    (copy / "dataset_traits.json").write_bytes((copy / "dataset_traits.json").read_bytes() + b" ")
    assert "dataset_traits.json: sha256 与 manifest 不符" in check_error(copy, tmp_path / "absent")


@pytest.mark.parametrize(
    "name",
    ["client_profile.json", "dataset_traits.json", "planning/audit_objectives.json", "planning/risk_register.json"],
)
def test_one_byte_change_names_the_file(tmp_path: Path, name: str) -> None:
    copy = copy_supplements(tmp_path)
    data = bytearray((copy / name).read_bytes())
    data[len(data) // 2] ^= 1
    (copy / name).write_bytes(bytes(data))
    message = check_error(copy, tmp_path / "absent")
    assert f"{copy / name}: sha256 与 manifest 不符" in message.splitlines()


def test_unlisted_missing_and_unflagged_files(tmp_path: Path) -> None:
    copy = copy_supplements(tmp_path)
    (copy / "extra.csv").write_text("a\n", encoding="utf-8")
    (copy / "planning" / "risk_register.json").unlink()
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["synthetic"] = False
    (copy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    lines = check_error(copy, tmp_path / "absent").splitlines()
    assert f"{copy / 'extra.csv'}: 不在 manifest 清单中" in lines
    assert f"{copy / 'planning/risk_register.json'}: manifest 列出但文件不存在" in lines
    assert f"{copy / 'client_profile.json'}: manifest 中 synthetic 不为 true" in lines


def test_strict_models_and_trait_keys(tmp_path: Path) -> None:
    copy = copy_supplements(tmp_path)
    profile = json.loads((copy / "client_profile.json").read_text(encoding="utf-8"))
    profile["synthetic"] = True  # 领域模型 extra="forbid", 合成标志不能加成字段
    profile["notes"] = "Profile without the synthetic prefix."
    (copy / "client_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    traits = json.loads((copy / "dataset_traits.json").read_text(encoding="utf-8"))
    del traits["payroll_register"]
    (copy / "dataset_traits.json").write_text(json.dumps(traits), encoding="utf-8")
    risks = json.loads((copy / "planning" / "risk_register.json").read_text(encoding="utf-8"))
    risks[0]["fraud_related"] = "yes"  # 严格模式不做类型转换
    (copy / "planning" / "risk_register.json").write_text(json.dumps(risks), encoding="utf-8")
    generator.write_manifest(copy, fake_snapshot(tmp_path, []))
    lines = check_error(copy, tmp_path / "absent").splitlines()
    assert f"{copy / 'client_profile.json'}: 不符合领域模型 (1 处)" in lines
    assert f"{copy / 'client_profile.json'}: notes 不以 'Synthetic – for demonstration only' 开头" in lines
    assert f"{copy / 'planning/risk_register.json'}: 不符合领域模型 (1 处)" in lines
    assert f"{copy / 'dataset_traits.json'}: 键与第 5.2 节不符 (多 [], 缺 ['payroll_register'])" in lines


def test_login_contact_scan_reports_file_names_only(tmp_path: Path) -> None:
    contacts = [
        {"FirstName": "Zed", "LastName": "Example", "EmailAddress": "zed@example.invalid"},
        {"Name": "Synthetic Customer", "FirstName": "Cy", "EmailAddress": "cy@example.invalid", "IsCustomer": True},
        {
            "Name": "Synthetic Vendor",
            "FirstName": "Sy",
            "LastName": "Vendor",
            "EmailAddress": "sy@x.invalid",
            "IsSupplier": True,
        },
        {"Name": "Synthetic Bank", "IsCustomer": False, "IsSupplier": False},
    ]
    snapshot = fake_snapshot(tmp_path, contacts)
    copy = copy_supplements(tmp_path)
    profile = json.loads((copy / "client_profile.json").read_text(encoding="utf-8"))
    profile["notes"] += " Prepared by ZED  EXAMPLE."  # 大小写与连续空白不影响整串匹配
    (copy / "client_profile.json").write_text(json.dumps(profile), encoding="utf-8")
    risks = json.loads((copy / "planning" / "risk_register.json").read_text(encoding="utf-8"))
    risks[0]["description"] += " Contact Zed@Example.invalid."
    risks[1]["description"] += " Example only; Zed alone is fine. Cy and Sy Vendor are not the login contact."
    (copy / "planning" / "risk_register.json").write_text(json.dumps(risks), encoding="utf-8")
    generator.write_manifest(copy, snapshot)
    outside = tmp_path / "fixtures" / "llm_xero" / "x.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("Reviewed by zed\nexample", encoding="utf-8")  # 换行同空格
    escaped = outside.parent / "escaped.json"
    escaped.write_text(json.dumps({"text": "Zed\nExample"}), encoding="utf-8")  # JSON 里转义的换行
    in_pdf = outside.parent / "letter.pdf"  # PDF 内容流是压缩的, 只能查文本层
    generator.write_pdf(in_pdf, ["Prepared by Zed\nExample", "Contact ZED@EXAMPLE.INVALID"], "Letter", HEADER)
    generator.write_pdf(outside.parent / "clean.pdf", ["Example only"], "Letter", HEADER)
    (outside.parent / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
    message = check_error(copy, snapshot, scan=[tmp_path / "fixtures", tmp_path / "missing"])
    assert message.splitlines() == [
        f"{copy / 'client_profile.json'}: 含登录账号联系人的姓名或邮箱",
        f"{copy / 'planning/risk_register.json'}: 含登录账号联系人的姓名或邮箱",
        f"{outside.parent / 'broken.pdf'}: 无法读取文本层, 未能扫描登录账号联系人",
        f"{escaped}: 含登录账号联系人的姓名或邮箱",
        f"{in_pdf}: 含登录账号联系人的姓名或邮箱",
        f"{outside}: 含登录账号联系人的姓名或邮箱",
    ]
    assert "zed example" not in message.lower() and "zed@example.invalid" not in message.lower()


def test_login_contact_must_be_unique(tmp_path: Path) -> None:
    contacts = [{"FirstName": n, "LastName": "Example", "EmailAddress": f"{n}@example.invalid"} for n in ("A", "B")]
    snapshot = fake_snapshot(tmp_path, contacts)
    copy = copy_supplements(tmp_path)
    generator.write_manifest(copy, snapshot)
    message = check_error(copy, snapshot, scan=[])
    assert message == f"{snapshot / 'contacts.json'}: 双否且有 FirstName 与 EmailAddress 的联系人应恰好 1 条, 实有 2 条"
    assert "@example.invalid" not in message


@needs_snapshot
def test_login_contact_absent_from_repository_files() -> None:
    assert checker.scan_login_contact(SNAPSHOT, [SUPPLEMENTS, *checker.SCAN_PATHS]) == []


@needs_snapshot
def test_snapshot_mismatch_is_reported(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot" / "20261017-q1"
    shutil.copytree(SNAPSHOT, snapshot)
    (snapshot / "payments.json").write_bytes((snapshot / "payments.json").read_bytes() + b"\n")
    (snapshot / "contacts.json").write_bytes((snapshot / "contacts.json").read_bytes() + b"\n")  # 不进哈希清单
    lines = check_error(SUPPLEMENTS, snapshot, scan=[]).splitlines()
    assert lines == [f"{snapshot / 'payments.json'}: 快照文件与 manifest 所记不符 (补造文件与快照不匹配)"]


@needs_snapshot
def test_reads_only_whitelisted_snapshot_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.adapters import xero_dataset

    parsed: list[str] = []

    class Recording(generator.Snapshot):
        def load(self, name: str) -> object:
            parsed.append(name)
            return super().load(name)

    def forbidden(*args: object) -> None:
        raise AssertionError("不得调用会读 contacts.json 的映射")

    monkeypatch.setattr(generator, "Snapshot", Recording)
    monkeypatch.setattr(xero_dataset, "build_tables", forbidden)
    monkeypatch.setattr(xero_dataset, "map_sales_invoices", forbidden)
    generator.generate(SNAPSHOT, tmp_path / "out")
    index = generator.RefIndex(Recording(SNAPSHOT))
    assert index.missing(["SI-77b338ef-ecc0-4b95-a0d7-2617b0054611", "XJ-580", "XJ-580-3", "BT-090-0081"]) == []
    assert parsed and set(parsed) <= generator.READABLE | {"trial_balance_2026-06-30", "trial_balance_2026-09-30"}
    with pytest.raises(ValueError, match="不在读取白名单内"):
        Recording(SNAPSHOT).load("contacts")


@needs_snapshot
def test_output_does_not_depend_on_contacts(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot" / "20261017-q1"
    snapshot.mkdir(parents=True)
    for path in SNAPSHOT.iterdir():
        if path.is_file() and path.name != "contacts.json":
            shutil.copy2(path, snapshot / path.name)
    out = generator.generate(snapshot, tmp_path / "out")
    produced = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    expected = {p.relative_to(SUPPLEMENTS).as_posix(): p.read_bytes() for p in SUPPLEMENTS.rglob("*") if p.is_file()}
    assert produced == expected


@needs_snapshot
def test_xero_refs_must_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = spec()
    good = ["SI-77b338ef-ecc0-4b95-a0d7-2617b0054611", "XJ-580-1", "BT-091-0001", "BA-090", "840"]
    data["documents"] = [*data["documents"], {"path": "client_profile.json", "xero_refs": good}]
    patched = tmp_path / "spec.json"
    patched.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(generator, "SPEC", patched)
    manifest = json.loads((generator.generate(SNAPSHOT, tmp_path / "ok") / "manifest.json").read_text())
    assert manifest["files"][0]["path"] == "client_profile.json" and manifest["files"][0]["xero_refs"] == good
    invoices = json.loads((SNAPSHOT / "invoices.json").read_text(encoding="utf-8"))
    draft = next(i for i in invoices if i["Type"] == "ACCREC" and i["Status"] not in {"AUTHORISED", "PAID"})
    bad = ["XJ-580-4", "SI-00000000", f"SI-{draft['InvoiceID']}", "BT-090-9999", "BA-200", "99999"]
    data["documents"][-1]["xero_refs"] = [*good, *bad]  # 草稿, 作废的发票不进载入器, 也不算能找到
    patched.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SystemExit, match="xero_refs 在快照中找不到") as raised:
        generator.generate(SNAPSHOT, tmp_path / "bad")
    assert str(raised.value) == f"xero_refs 在快照中找不到: {', '.join(sorted(bad))}"


@needs_snapshot
def test_generator_exits_nonzero_when_check_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert generator.main(["--out", str(tmp_path / "ok")]) == 0
    data = spec()
    data["profile"]["notes"] = "Not flagged."
    patched = tmp_path / "spec.json"
    patched.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(generator, "SPEC", patched)
    assert generator.main(["--out", str(tmp_path / "bad")]) == 1


def no_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """把会写文件的函数换成立即失败的替身: 守卫一旦退化, 测试当场失败, 不会先写进仓库的原始数据."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("守卫没有拦住, 已走到写文件的步骤")

    for name in ("generate", "write_manifest", "render_registry_pdfs"):
        monkeypatch.setattr(generator, name, refuse)
    monkeypatch.setattr(checker, "check", refuse)


def test_inject_refuses_repository_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_writes(monkeypatch)
    before = {p: p.read_bytes() for p in SUPPLEMENTS.rglob("*") if p.is_file()}
    assert generator.main(["--inject", "T1"]) == 2
    assert generator.main(["--inject", "T1", "--out", str(generator.OUT_ROOT / "other")]) == 2
    assert not (generator.OUT_ROOT / "other").exists()
    assert {p: p.read_bytes() for p in SUPPLEMENTS.rglob("*") if p.is_file()} == before


def test_inject_rewrites_copy_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = copy_supplements(tmp_path)
    snapshot = SNAPSHOT if SNAPSHOT.is_dir() else fake_snapshot(tmp_path, [])
    (copy / "dataset_traits.json").write_text('{"changed": true}\n', encoding="utf-8")
    assert generator.main(["--inject", "T1", "--out", str(copy), "--snapshot", str(snapshot)]) == 0
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["injected"] == "T1"
    listed = {f["path"]: f["sha256"] for f in manifest["files"]}
    assert listed["dataset_traits.json"] == generator.sha256(copy / "dataset_traits.json")
    assert [p.relative_to(copy).as_posix() for p in generator.render_registry_pdfs(copy)] == [
        *(f"registry/extracts/{acn}.pdf" for acn in ("900043021", "900043030", "900043049")),
        "registry/persons/MORGAN-ALEX-19790412.pdf",
    ]
    monkeypatch.setattr(generator, "OUT_ROOT", copy.parent)  # 当作正式目录: 不许带 injected
    lines = check_error(copy, tmp_path / "absent").splitlines()
    assert f"{copy / 'manifest.json'}: 正式目录的 manifest 不得有 injected 键" in lines


def test_pdf_layout_and_determinism(tmp_path: Path) -> None:
    pages = ["Line one\nLine two", "Second page"]
    first, second = tmp_path / "a" / "doc.pdf", tmp_path / "b" / "doc.pdf"
    generator.write_pdf(first, pages, "Test document", HEADER)
    generator.write_pdf(second, pages, "Test document", HEADER)
    assert first.read_bytes() == second.read_bytes()
    with pdfplumber.open(first) as pdf:
        assert [(p.width, p.height) for p in pdf.pages] == [(595, 842), (595, 842)]
        texts = [p.extract_text() for p in pdf.pages]
        assert [t.splitlines()[0] for t in texts] == [HEADER, HEADER]
        assert all(f"Test document | Page {n} of 2" in t for n, t in enumerate(texts, 1))
        assert "Line two" in texts[0] and "Second page" in texts[1]
    assert pdf.pages[0].chars[0]["fontname"].endswith("Courier") and pdf.pages[0].chars[0]["size"] == 9
    generator.write_pdf(tmp_path / "c" / "bad.pdf", pages, "Test document", "Other header")
    problems = checker.check_synthetic(tmp_path / "c", {"files": [], "documents": []}, HEADER)
    assert problems == [f"{tmp_path / 'c' / 'bad.pdf'}: 第 1, 2 页文本层没有合成页眉"]


def test_output_guard_protects_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--inject 只许仓库外已存在且没有链接的副本; 普通生成只许 data/xero_demo/<快照名> 或仓库外. 拒绝时不写任何文件."""
    no_writes(monkeypatch)
    protected = [REPO / "data" / "demo" / "manifest.json", generator.SNAPSHOTS / "20261017-q1" / "manifest.json"]
    before = {p: p.read_bytes() for p in protected if p.exists()}
    monkeypatch.chdir(REPO)
    for argv in (
        ["--inject", "T1", "--out", "data/demo"],
        ["--inject", "T1", "--out", "data/external/xero/api/20261017-q1"],
        ["--inject", "T1", "--out", "data/xero_demo/../xero_demo/20261017-q1"],
        ["--out", "data/demo"],
        ["--out", "data/external/xero/api/20261017-q1"],
    ):
        assert generator.main(argv) == 2, argv
    link = tmp_path / "link"
    link.symlink_to(SUPPLEMENTS, target_is_directory=True)
    assert generator.main(["--inject", "T1", "--out", str(link)]) == 2  # 顺着目录链接指回正式目录
    assert generator.main(["--inject", "T1", "--out", str(tmp_path / "missing")]) == 2
    assert not (tmp_path / "missing").exists()
    copy = copy_supplements(tmp_path)
    (copy / "manifest.json").unlink()
    (copy / "manifest.json").symlink_to(SUPPLEMENTS / "manifest.json")
    assert generator.main(["--inject", "T1", "--out", str(copy)]) == 2  # 副本里的文件链接
    (copy / "manifest.json").unlink()
    (copy / "manifest.json").hardlink_to(copy / "dataset_traits.json")
    assert generator.main(["--inject", "T1", "--out", str(copy)]) == 2
    assert {p: p.read_bytes() for p in protected if p.exists()} == before


def test_malformed_inputs_are_reported_not_raised(tmp_path: Path) -> None:
    copy = copy_supplements(tmp_path)
    absent = tmp_path / "absent"
    (copy / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
    lines = check_error(copy, absent).splitlines()
    assert f"{copy / 'broken.pdf'}: 无法解析 PDF" in lines and f"{copy / 'broken.pdf'}: 不在 manifest 清单中" in lines
    (copy / "broken.pdf").unlink()
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    del manifest["files"][0]["path"]
    (copy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert check_error(copy, absent) == f"{copy / 'manifest.json'}: manifest 结构不符 (1 处)"
    (copy / "manifest.json").write_text("{", encoding="utf-8")
    assert check_error(copy, absent) == f"{copy / 'manifest.json'}: 没有 manifest.json 或不是合法的 JSON"


def test_bad_snapshot_inputs_are_reported(tmp_path: Path) -> None:
    snapshot = fake_snapshot(tmp_path, [{"FirstName": "Zed", "LastName": "Example", "EmailAddress": "z@x.invalid"}])
    copy = copy_supplements(tmp_path)
    generator.write_manifest(copy, snapshot)
    assert checker.check(copy, snapshot, determinism=False, scan=[]) == []
    (snapshot / "organisation.json").write_text("[{}]", encoding="utf-8")
    generator.write_manifest(copy, snapshot)  # 快照哈希随之更新, 只剩组织信息的问题
    assert check_error(copy, snapshot, scan=[]).splitlines() == [
        f"{snapshot / 'organisation.json'}: 快照缺组织信息, 无从核对画像 ID 前缀"
    ]
    (snapshot / "organisation.json").write_text('[{"OrganisationID": "3c1332bb-0000-4000-8000-000000000000"}]')
    generator.write_manifest(copy, snapshot)
    (snapshot / "contacts.json").write_text("[", encoding="utf-8")
    message = f"{snapshot / 'contacts.json'}: 快照没有 contacts.json 或无法解析, 无从扫描登录账号联系人"
    assert check_error(copy, snapshot, scan=[]) == message
    (snapshot / "contacts.json").unlink()  # contacts.json 不在快照哈希清单内, 缺了也要报, 不静默跳过扫描
    assert check_error(copy, snapshot, scan=[]) == message


def test_regeneration_failure_keeps_other_problems(tmp_path: Path) -> None:
    """Demo 重置后的新快照套上旧补造: 快照不符与无法重新生成都要报出."""
    snapshot = fake_snapshot(tmp_path, [{"FirstName": "Zed", "LastName": "Example", "EmailAddress": "z@x.invalid"}])
    copy = copy_supplements(tmp_path)
    generator.write_manifest(copy, snapshot)
    (snapshot / "organisation.json").write_text('[{"OrganisationID": "aaaabbbb-0000-4000-8000-000000000000"}]')
    lines = check_error(copy, snapshot, scan=[], determinism=True).splitlines()
    assert lines == [
        f"{snapshot / 'organisation.json'}: 快照文件与 manifest 所记不符 (补造文件与快照不匹配)",
        f"{copy / 'client_profile.json'}: 画像 ID 不以 CLIENT-XERO-AAAABBBB- 开头",
        f"{copy}: 无法重新生成 (画像 ID CLIENT-XERO-3C1332BB-Q1 不以 CLIENT-XERO-AAAABBBB- 开头)",
    ]


def test_snapshot_argument_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "20261017-q1").mkdir()
    monkeypatch.chdir(tmp_path)  # 当前目录下的同名目录不会被当成快照
    assert generator.resolve_snapshot("20261017-q1") == generator.SNAPSHOTS / "20261017-q1"
    assert generator.resolve_snapshot("./20261017-q1") == Path("./20261017-q1")
    assert generator.resolve_snapshot(tmp_path / "x") == tmp_path / "x"


def test_pdf_page_limits(tmp_path: Path) -> None:
    fits = "\n".join("x" * generator.LINE_WIDTH for _ in range(generator.PAGE_LINES))
    generator.write_pdf(tmp_path / "fits.pdf", [fits], "Footer", HEADER)
    with pdfplumber.open(tmp_path / "fits.pdf") as pdf:
        assert max(c["x1"] for c in pdf.pages[0].chars) <= 595 - 40 + 1
        body = [c for c in pdf.pages[0].chars if c["fontname"].endswith("Courier")]
        footer = [c for c in pdf.pages[0].chars if c["fontname"].endswith("Helvetica")]
        assert max(c["bottom"] for c in body) < min(c["top"] for c in footer)  # 正文压不到页脚
    with pytest.raises(ValueError, match="第 2 页有 57 行"):
        generator.write_pdf(tmp_path / "long.pdf", ["one", fits + "\nmore"], "Footer", HEADER)
    with pytest.raises(ValueError, match=r"第 1 页有 1 行超过 95 字$"):
        generator.write_pdf(tmp_path / "wide.pdf", ["y" * 96], "Footer", HEADER)
    with pytest.raises(ValueError, match=r"第 1 页有 1 行超过 95 字$"):  # 页眉也算
        generator.write_pdf(tmp_path / "wide.pdf", ["short"], "Footer", HEADER.ljust(generator.LINE_WIDTH + 1, "x"))
    assert not (tmp_path / "long.pdf").exists() and not (tmp_path / "wide.pdf").exists()


@needs_snapshot
def test_regeneration_in_fresh_process_with_other_hash_seed(tmp_path: Path) -> None:
    """另起进程并换 PYTHONHASHSEED 重新生成, 与入库目录逐字节相同 (校验脚本的确定性检查在同一进程内)."""
    import os
    import subprocess

    out = tmp_path / "out"
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    script = REPO / "data" / "generate_xero_demo.py"
    result = subprocess.run([sys.executable, str(script), "--out", str(out)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-500:]
    produced = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    expected = {p.relative_to(SUPPLEMENTS).as_posix(): p.read_bytes() for p in SUPPLEMENTS.rglob("*") if p.is_file()}
    assert produced == expected


def test_line_endings_are_protected() -> None:
    """补造文件, 生成规格与快照的哈希都按字节算, Windows 检出不得转换换行."""
    rules = (REPO / ".gitattributes").read_text(encoding="utf-8").splitlines()
    for rule in (
        "/data/xero_demo/** -text",
        "/data/xero_demo/**/*.pdf binary",
        "/data/xero_demo_spec.json text eol=lf",
        "/data/external/xero/** -text",
    ):
        assert rule in rules, rule
    if not (REPO / ".git").exists():
        return
    import subprocess

    paths = ["data/xero_demo/20261017-q1/manifest.json", "data/external/xero/api/20261017-q1/accounts.json"]
    result = subprocess.run(["git", "check-attr", "text", "--", *paths], cwd=REPO, capture_output=True, text=True)
    assert result.stdout.splitlines() == [f"{p}: text: unset" for p in paths]
    result = subprocess.run(
        ["git", "check-attr", "eol", "--", "data/xero_demo_spec.json"], cwd=REPO, capture_output=True, text=True
    )
    assert result.stdout.strip() == "data/xero_demo_spec.json: eol: lf"


def test_guard_recognises_aliases_without_normalisation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟大小写不敏感的文件系统: 路径不做规范化 (resolve 不跟随链接), 另一个写法指向同一目录, 守卫仍认得出."""
    import os

    alias = tmp_path / "alias"
    alias.symlink_to(REPO, target_is_directory=True)
    monkeypatch.setattr(Path, "resolve", lambda self, strict=False: Path(os.path.abspath(self)))
    assert generator.inside(alias / "data" / "demo", REPO)
    assert generator.same_path(alias / "data" / "xero_demo" / "20261017-q1", SUPPLEMENTS)
    assert generator.same_path(alias / "data" / "xero_demo" / "new-snapshot", generator.OUT_ROOT / "new-snapshot")
    snapshot = generator.SNAPSHOTS / "20261017-q1"
    assert generator.out_problem(alias / "data" / "demo", snapshot, inject=False)
    assert generator.out_problem(alias / "data" / "xero_demo" / "20261017-q1", snapshot, inject=False) is None
    assert generator.out_problem(alias / "data" / "xero_demo" / "20261017-q1", snapshot, inject=True)


def test_normal_generation_refuses_links_in_existing_output(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "manifest.json").symlink_to(tmp_path / "elsewhere.json")
    problem = generator.out_problem(out, generator.SNAPSHOTS / "20261017-q1", inject=False)
    assert problem == f"输出目录中有符号链接或硬链接: {out / 'manifest.json'}"
    assert generator.main(["--out", str(out)]) == 2
    assert not (tmp_path / "elsewhere.json").exists()


def test_unexpected_error_in_one_step_keeps_others(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy = copy_supplements(tmp_path)
    (copy / "extra.csv").write_text("a\n", encoding="utf-8")

    def broken(directory: Path) -> list[str]:
        raise RuntimeError("Zed Example appears in the failing file")  # 异常消息不得进报告

    monkeypatch.setattr(checker, "check_models", broken)
    message = check_error(copy, tmp_path / "absent")
    assert message.splitlines() == [f"{copy / 'extra.csv'}: 不在 manifest 清单中", f"{copy}: 校验中断 (RuntimeError)"]
    assert "Zed" not in message


def test_regeneration_error_message_is_not_copied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = fake_snapshot(tmp_path, [{"FirstName": "Zed", "LastName": "Example", "EmailAddress": "z@x.invalid"}])
    copy = copy_supplements(tmp_path)
    generator.write_manifest(copy, snapshot)

    def broken(snapshot_dir: Path, out: Path) -> Path:
        raise ValueError("Invalid isoformat string: 'Zed Example'")

    monkeypatch.setattr(generator, "generate", broken)
    message = check_error(copy, snapshot, scan=[], determinism=True)
    assert message == f"{copy}: 无法重新生成 (ValueError)"


def test_manifest_file_items_take_no_extra_keys(tmp_path: Path) -> None:
    copy = copy_supplements(tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["note"] = "extra"
    (copy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert check_error(copy, tmp_path / "absent") == f"{copy / 'manifest.json'}: manifest 结构不符 (1 处)"


# ── 承接文件与登记 mock ─────────────────────────────────────


def pdf_texts(path: Path) -> list[str]:
    with pdfplumber.open(path) as pdf:
        return [re.sub(r"\s+", " ", page.extract_text() or "") for page in pdf.pages]


def test_appointment_pages() -> None:
    pages = pdf_texts(SUPPLEMENTS / "documents" / "engagement" / "appointment.pdf")
    assert len(pages) == 4
    expected = [
        ["14 August 2026", "section 248B(1)", "section 323D(2A)", "30 September 2026", "(a) ", "(b) ", "(c) "],
        ["20 August 2026", "Sam Whitfield", "15 of the 100 ordinary shares", "(section 293(3)(c))", "section 293(1)"],
        [
            "25 August 2026",
            "Section 328A",
            "Tessellate Audit Partners",
            "Jordan Ellis, Registered Company Auditor No. 300471",
        ],
        ["27 August 2026", "section 325(1)", "appoints Tessellate Audit Partners as auditor"],
    ]
    for n, (page, words) in enumerate(zip(pages, expected, strict=True), 1):
        assert all(w in page for w in words), (n, [w for w in words if w not in page])
    assert "the previous 5 financial years (FY2022 to FY2026) was 12 months long" in pages[0]
    assert "Signed in the firm name" in pages[2] and "Signed in the member's own name" in pages[2]


def test_engagement_letter_pages() -> None:
    pages = pdf_texts(SUPPLEMENTS / "documents" / "engagement" / "engagement_letter.pdf")
    assert len(pages) == 3
    first, second, third = pages
    assert "28 August 2026" in first and "Alex Morgan, Director" in first
    assert (
        "1. Objective and scope of the audit" in first and "2. Our responsibilities" in first
    )  # ASA 210 第 10(a)(b) 段
    assert (
        "(ASA 510, paragraph 6)" in first and "(ASA 710, paragraph 14)" in first and "Other Matter paragraph" in first
    )
    assert "4. Responsibilities of the directors" in second and "5. Applicable financial reporting framework" in second
    for item in ("(i) for the preparation", "(ii) for such internal control", "(iii) to provide us with"):
        assert item in second, item  # 第 6(b)(i)-(iii) 段
    assert "Australian Accounting Standards - Simplified Disclosures." in second and "not entirely comparable" in second
    assert "6. Form and content of our report" in third and "differs from its expected form and content" in third
    assert "AUD 4,800, excluding GST" in third and "Date: 2 September 2026" in third
    text = " ".join(pages)
    assert "AASB 1060" not in text and "paragraph" not in second  # 简化披露框架不写 AASB 1060 段号


def registry_blocks() -> tuple[object, object]:
    """第 2.5 节的 abn.json 与 asic.json 代码块 (块内用 // 注释分隔两份)."""
    block = re.search(r"```json\n(.*?)```", section(doc(), "### 2.5 登记 mock"), re.S)
    assert block
    parts = re.split(r"^// registry/\w+\.json\n", block.group(1), flags=re.M)
    abn, asic = (json.loads(part) for part in parts if part.strip())
    return abn, asic


def test_registry_json_matches_design() -> None:
    from backend.contracts.models import AbnRecord, AsicCompanyRecord

    abn, asic = registry_blocks()
    assert read("registry/abn.json") == abn and list(abn) == ["25900043021", "27900043030", "17900043049"]  # type: ignore[arg-type]
    assert read("registry/asic.json") == asic
    assert [r["registered_date"] for r in asic.values()] == ["2018-09-03", "2014-03-03", "2019-07-01"]  # type: ignore[attr-defined]
    for row in abn.values():  # type: ignore[attr-defined]
        AbnRecord.model_validate(row)
    for row in asic.values():  # type: ignore[attr-defined]
        AsicCompanyRecord.model_validate(row)


def test_extracts_and_person_search() -> None:
    alex = {"name": "Alex Morgan", "birth_date": "1979-04-12"}
    demo = read("registry/extracts/900043021.json")
    assert list(demo) == [  # type: ignore[arg-type]
        "acn", "name", "type", "class", "subclass", "status", "registered_date",
        "officeholders", "shareholders", "paid_up_cents", "file_name",
    ]  # fmt: skip
    assert (demo["type"], demo["class"], demo["subclass"], demo["paid_up_cents"]) == ("APTY", "LMSH", "PROP", 100)  # type: ignore[index]
    assert demo["officeholders"] == [  # type: ignore[index]
        {**alex, "role": "director", "appointed": "2018-09-03"},
        {**alex, "role": "secretary", "appointed": "2018-09-03"},
    ]
    assert demo["shareholders"] == [  # type: ignore[index]
        {"name": "Alex Morgan", "kind": "person", "acn": None, "share_class": "ORD", "shares": 85, "pct": 0.85},
        {"name": "Sam Whitfield", "kind": "person", "acn": None, "share_class": "ORD", "shares": 15, "pct": 0.15},
    ]  # 只持股的 Sam 没有出生日期
    vpa = read("registry/extracts/900043030.json")
    assert vpa["officeholders"] == [  # type: ignore[index]
        {"name": "Dana Morgan", "birth_date": "1981-11-03", "role": "director", "appointed": "2014-03-03"},
        {**alex, "role": "director", "appointed": "2014-03-03"},
    ]
    assert [(h["name"], h["shares"], h["pct"]) for h in vpa["shareholders"]] == [  # type: ignore[index]
        ("Dana Morgan", 50, 0.5),
        ("Alex Morgan", 50, 0.5),
    ]
    advisory = read("registry/extracts/900043049.json")
    assert advisory["officeholders"] == [{**alex, "role": "director", "appointed": "2019-07-01"}]  # type: ignore[index]
    assert [(h["name"], h["shares"], h["pct"]) for h in advisory["shareholders"]] == [("Alex Morgan", 100, 1.0)]  # type: ignore[index]
    person = read("registry/persons/MORGAN-ALEX-19790412.json")
    assert (person["family_name"], person["given_name"], person["birth_date"]) == ("Morgan", "Alex", "1979-04-12")  # type: ignore[index]
    assert [(r["acn"], r["role"], r["appointed"], r["shares"]) for r in person["roles"]] == [  # type: ignore[index]
        ("900043021", "director", "2018-09-03", None),
        ("900043021", "secretary", "2018-09-03", None),
        ("900043021", "shareholder", None, 85),
        ("900043030", "director", "2014-03-03", None),
        ("900043030", "shareholder", None, 50),
        ("900043049", "director", "2019-07-01", None),
        ("900043049", "shareholder", None, 100),
    ]
    assert person["file_name"] == "MORGAN-ALEX-19790412.pdf"  # type: ignore[index]


def test_registry_pdfs_follow_json(tmp_path: Path) -> None:
    for acn, name in (("900043021", "Demo Company (AU) Pty Ltd"), ("900043030", "Victoria Property Agency Pty Ltd")):
        pages = pdf_texts(SUPPLEMENTS / "registry" / "extracts" / f"{acn}.pdf")
        assert len(pages) == 2 and all(name in p and f"{acn[:3]} {acn[3:6]} {acn[6:]}" in p for p in pages)
        assert "Office holders" in pages[0] and "Members" in pages[1]
    demo = pdf_texts(SUPPLEMENTS / "registry" / "extracts" / "900043021.pdf")
    assert "Alex Morgan 12 April 1979 Secretary 3 September 2018" in demo[0]
    assert "ORD 100 AUD 1.00" in demo[1] and "Sam Whitfield person - ORD 15 15%" in demo[1]
    person = pdf_texts(SUPPLEMENTS / "registry" / "persons" / "MORGAN-ALEX-19790412.pdf")
    assert len(person) == 1 and "MORGAN, Alex" in person[0] and "12 April 1979" in person[0]
    assert "900 043 030 Victoria Property Agency Pty Ltd Shareholder - 50" in person[0]
    copy = copy_supplements(tmp_path)
    target = copy / "registry" / "extracts" / "900043030.json"
    data = json.loads(target.read_text(encoding="utf-8"))
    data["officeholders"].append(
        {"name": "Quinn Example", "birth_date": None, "role": "secretary", "appointed": "2020-01-02"}
    )
    target.write_text(json.dumps(data), encoding="utf-8")
    before = target.with_suffix(".pdf").read_bytes()
    generator.render_registry_pdfs(copy)  # 测试副本改登记 JSON 后重绘
    assert target.with_suffix(".pdf").read_bytes() != before
    assert "Quinn Example - Secretary 2 January 2020" in pdf_texts(target.with_suffix(".pdf"))[0]


def test_documents_in_manifest_and_index() -> None:
    manifest = read("manifest.json")
    rows = [
        (d["path"], d["type"], d["reliability"], d["document_date"], d["registration"], d["page_count"])
        for d in manifest["documents"]  # type: ignore[index]
    ]
    assert rows == [
        ("documents/engagement/appointment.pdf", "minutes", "internal_copy", "2026-08-27", "engagement", 4),
        (
            "documents/engagement/engagement_letter.pdf",
            "engagement_letter",
            "external_copy",
            "2026-08-28",
            "engagement",
            3,
        ),
        *(
            (f"registry/extracts/{acn}.pdf", "registry_extract", "external_original", "2026-10-17", "registry", 2)
            for acn in ("900043021", "900043030", "900043049")
        ),
        (
            "registry/persons/MORGAN-ALEX-19790412.pdf",
            "registry_extract",
            "external_original",
            "2026-10-17",
            "registry",
            1,
        ),
    ]
    assert sum(r[-1] for r in rows) == 14
    for item in manifest["documents"]:  # type: ignore[index]
        assert item["document_id"] == "DOC-" + item["sha256"][:12] and item["related_ids"] == [] and item["synthetic"]
    assert (SUPPLEMENTS / "documents" / "index.csv").read_text(encoding="utf-8") == (
        "path,document_type,reliability,document_date,related_ids\n"
        "documents/engagement/appointment.pdf,minutes,internal_copy,2026-08-27,\n"
        "documents/engagement/engagement_letter.pdf,engagement_letter,external_copy,2026-08-28,\n"
    )


def test_registry_numbers_pass_check_digits() -> None:
    for abn in ("25900043021", "27900043030", "17900043049", "82938074116"):
        assert checker.abn_valid(abn), abn
    for acn in ("900043021", "900043030", "900043049"):
        assert checker.acn_valid(acn), acn
    assert not checker.abn_valid("25900043022") and not checker.abn_valid("12345678901")
    assert not checker.acn_valid("900043022") and not checker.acn_valid("90004302")


def test_registry_inconsistencies_are_reported(tmp_path: Path) -> None:
    snapshot = fake_snapshot(tmp_path, [{"FirstName": "Zed", "LastName": "Example", "EmailAddress": "z@x.invalid"}])
    copy = copy_supplements(tmp_path)
    registry = copy / "registry"
    abn = json.loads((registry / "abn.json").read_text(encoding="utf-8"))
    abn["17900043049"]["entity_name"] = "Morgan Advisory Services Pty Ltd"  # 与摘录名称不一致
    abn["18900043049"] = {**abn["17900043049"], "abn": "18900043049"}  # 校验位不对
    (registry / "abn.json").write_text(json.dumps(abn), encoding="utf-8")
    person_path = registry / "persons" / "MORGAN-ALEX-19790412.json"
    person = json.loads(person_path.read_text(encoding="utf-8"))
    del person["roles"][4]  # 少了 VPA 的 50 股
    person_path.write_text(json.dumps(person), encoding="utf-8")
    vpa_path = registry / "extracts" / "900043030.json"
    vpa = json.loads(vpa_path.read_text(encoding="utf-8"))
    vpa["registered_date"] = "2014-03-04"  # 与 asic.json 不一致, 且不重绘 PDF
    vpa_path.write_text(json.dumps(vpa), encoding="utf-8")
    generator.write_manifest(copy, snapshot)
    lines = check_error(copy, snapshot, scan=[]).splitlines()
    assert lines == [
        f"{vpa_path}: 与 asic.json 的名称或注册日不一致",
        f"{registry / 'extracts' / '900043049.json'}: abn.json 中没有同 ACN 同名称的主体",
        f"{person_path}: 职务与持股和公司摘录不一一对应",
        f"{registry}: ABN 18900043049 校验位不对",
        f"{vpa_path.with_suffix('.pdf')}: 第 1 页文本层缺 1 行来源内容或顺序不对",
        f"{vpa_path.with_suffix('.pdf')}: 第 2 页文本层缺 1 行来源内容或顺序不对",
        f"{vpa_path.with_suffix('.pdf')}: 应为 2 页且每页含名称, ACN 与注册日",  # 改了 JSON 没重绘 PDF
    ]


def test_pdf_page_count_and_text_mismatch(tmp_path: Path) -> None:
    snapshot = fake_snapshot(tmp_path, [{"FirstName": "Zed", "LastName": "Example", "EmailAddress": "z@x.invalid"}])
    copy = copy_supplements(tmp_path)
    letter = copy / "documents" / "engagement" / "engagement_letter.pdf"
    spec_pages = next(d for d in spec()["documents"] if d["path"].endswith("engagement_letter.pdf"))["pages"]
    pages = ["\n".join(p) for p in spec_pages]
    generator.write_pdf(letter, [pages[0], pages[1], pages[2].replace("4,800", "4,900")], "x", HEADER)
    generator.write_manifest(copy, snapshot)
    lines = check_error(copy, snapshot, scan=[]).splitlines()
    assert lines == [f"{letter}: 第 3 页文本层缺 1 行来源内容或顺序不对"]
    generator.write_pdf(letter, pages[:2], "x", HEADER)
    generator.write_manifest(copy, snapshot)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    assert [d["page_count"] for d in manifest["documents"] if d["path"].endswith("letter.pdf")] == [2]
    assert check_error(copy, snapshot, scan=[]).splitlines() == [f"{letter}: 页数 2 与来源的 3 页不符"]


def zed_snapshot(tmp_path: Path) -> Path:
    return fake_snapshot(tmp_path, [{"FirstName": "Zed", "LastName": "Example", "EmailAddress": "z@x.invalid"}])


def edit_json(path: Path, change: object) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)  # type: ignore[operator]
    path.write_text(json.dumps(data), encoding="utf-8")


def test_injected_registry_extract_gets_derived_metadata(tmp_path: Path) -> None:
    """注入测试 T6 在副本新增一份公司摘录: 重绘后 manifest 按目录规则登记, 不要求规格逐份写登记信息."""
    snapshot = zed_snapshot(tmp_path)
    copy = copy_supplements(tmp_path)
    extract = json.loads((copy / "registry" / "extracts" / "900043049.json").read_text(encoding="utf-8"))
    extract.update(acn="900043076", name="Synthetic Supplier Pty Ltd", file_name="900043076.pdf")
    (copy / "registry" / "extracts" / "900043076.json").write_text(json.dumps(extract), encoding="utf-8")
    generator.render_registry_pdfs(copy)
    manifest = generator.write_manifest(copy, snapshot, injected="T6")
    added = next(d for d in manifest["documents"] if d["path"] == "registry/extracts/900043076.pdf")
    assert (added["type"], added["reliability"], added["registration"], added["document_date"]) == (
        "registry_extract",
        "external_original",
        "registry",
        "2026-10-17",
    )
    assert added["page_count"] == 2


def test_numbers_outside_registry_are_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = zed_snapshot(tmp_path)
    copy = copy_supplements(tmp_path)
    edit_json(copy / "client_profile.json", lambda d: d.update(abn="25900043012"))
    data = spec()
    letter = next(d for d in data["documents"] if d["path"].endswith("appointment.pdf"))
    letter["pages"][0][1] = "ACN 900 043 022   ABN 25 900 043 021"
    patched = tmp_path / "spec.json"
    patched.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(generator, "SPEC", patched)
    generator.write_manifest(copy, snapshot)
    lines = check_error(copy, snapshot, scan=[]).splitlines()
    assert f"{copy / 'client_profile.json'}: 画像的 ABN 与法定名称不在 abn.json 中" in lines
    assert f"{copy / 'client_profile.json'}: ABN 25900043012 校验位不对" in lines
    assert f"{patched}: documents/engagement/appointment.pdf 正文中的号码校验位不对: 900 043 022" in lines


def test_more_registry_inconsistencies(tmp_path: Path) -> None:
    snapshot = zed_snapshot(tmp_path)
    copy = copy_supplements(tmp_path)
    registry = copy / "registry"
    edit_json(registry / "asic.json", lambda d: d.update({"900043022": {**d["900043021"], "acn": "900043022"}}))
    demo = registry / "extracts" / "900043021.json"
    edit_json(demo, lambda d: d["shareholders"][0].update(pct=0.8))  # 85 股写成 80%
    person = registry / "persons" / "MORGAN-ALEX-19790412.json"
    edit_json(person, lambda d: d["roles"][0].update(shares=0))  # 职务行的 shares 应为 null
    advisory = registry / "extracts" / "900043049.json"
    advisory.rename(registry / "extracts" / "900043050.json")
    advisory.with_suffix(".pdf").rename(registry / "extracts" / "900043050.pdf")
    generator.render_registry_pdfs(copy)  # PDF 随 JSON 重绘, 只剩登记数据本身的问题
    generator.write_manifest(copy, snapshot)
    lines = check_error(copy, snapshot, scan=[]).splitlines()
    assert lines == [
        f"{demo}: 股东持股比例与股数不符",
        f"{registry / 'extracts' / '900043050.json'}: 文件名与 acn 或 file_name 不符",
        f"{person}: 职务与持股和公司摘录不一一对应",
        f"{registry}: ACN 900043022 校验位不对",
    ]


def test_registry_key_names_and_broken_json(tmp_path: Path) -> None:
    """摘录键写成 company_class 报格式问题; 坏的登记 JSON 不妨碍其他 PDF 的内容核对."""
    snapshot = zed_snapshot(tmp_path)
    copy = copy_supplements(tmp_path)
    vpa = copy / "registry" / "extracts" / "900043030.json"
    edit_json(vpa, lambda d: d.update(company_class=d.pop("class")))
    letter = copy / "documents" / "engagement" / "engagement_letter.pdf"
    spec_pages = next(d for d in spec()["documents"] if d["path"].endswith("engagement_letter.pdf"))["pages"]
    pages = ["\n".join(p) for p in spec_pages]
    generator.write_pdf(letter, [pages[0], pages[1], pages[2].replace("4,800", "4,900")], "x", HEADER)
    generator.write_manifest(copy, snapshot)
    lines = check_error(copy, snapshot, scan=[]).splitlines()
    assert lines == [
        f"{vpa}: 不符合摘录格式 (1 处)",
        f"{copy / 'registry/persons/MORGAN-ALEX-19790412.json'}: 职务与持股和公司摘录不一一对应",  # VPA 不计
        f"{letter}: 第 3 页文本层缺 1 行来源内容或顺序不对",  # 没有因坏摘录中断
    ]


def test_person_pdf_and_line_order(tmp_path: Path) -> None:
    snapshot = zed_snapshot(tmp_path)
    copy = copy_supplements(tmp_path)
    person = copy / "registry" / "persons" / "MORGAN-ALEX-19790412.json"
    edit_json(person, lambda d: d.update(birth_date="1979-04-13"))  # 改 JSON 不重绘 PDF
    letter = copy / "documents" / "engagement" / "engagement_letter.pdf"
    spec_pages = next(d for d in spec()["documents"] if d["path"].endswith("engagement_letter.pdf"))["pages"]
    last = list(spec_pages[2])
    a, b = last.index("Tessellate Audit Partners"), last.index("Alex Morgan, Director")
    last[a], last[b] = last[b], last[a]  # 两处签名对调
    generator.write_pdf(letter, ["\n".join(spec_pages[0]), "\n".join(spec_pages[1]), "\n".join(last)], "x", HEADER)
    generator.write_manifest(copy, snapshot)
    lines = check_error(copy, snapshot, scan=[]).splitlines()
    assert lines == [
        f"{person}: 文件名或 file_name 不是 MORGAN-ALEX-19790413",
        f"{person}: 职务与持股和公司摘录不一一对应",
        f"{letter}: 第 3 页文本层缺 4 行来源内容或顺序不对",  # 每一行都在, 只是顺序不对
        f"{person.with_suffix('.pdf')}: 第 1 页文本层缺 1 行来源内容或顺序不对",
        f"{person.with_suffix('.pdf')}: 应为 1 页且含姓名, 出生日期与各公司 ACN",
    ]
