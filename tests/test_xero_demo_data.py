"""Xero 演示数据集: 定稿文档 docs/Xero演示数据集设计.md 的内容守卫, 补造文件 (data/xero_demo/) 与生成, 校验脚本."""

import functools
import json
import re
import shutil
import sys
from pathlib import Path

import pdfplumber
import pytest

from tests.test_knowledge import Text, normalise, parent, pdf_text, unit_end

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
        "supplier_invoice",  # 供应商发票用上游的 invoice (第 9 批)
        "load_engagement_documents",  # 承接文件改为上传, 端口作废
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
    assert "共 29 份 57 页" in s95
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
            "露依据改引 AASB 1060 (完整财报的组成 AASB 1060 第 25 段, 现金流量表 AASB 1060 第 64-89 段,"
            " 关联方 AASB 1060 第 189-203 段; 各节脚注写明对应的 AASB 101, 107, 124); AASB 110 只有第 13, 16,"
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
        ('  - 第 5 节规则命中与第 7 节计数不在校验脚本复算 (见 "本版不做"), 分别由各程序的连库测试与合并终验核对.'),
    ),
    (
        "### 9.4 是否入库, 生成与校验脚本",
        (
            '  - 新旧快照比对 (第 9.2 节): 20261017-mock 在快照同级目录时做, 不一致报 "快照与 20261017-mock 不一致 '
            '(Demo 可能已重置)" 并只列文件名; 没有时, 或快照本身就是 20261017-mock 时, 跳过并写说明; 0630 表读不出'
            "或缺 prior_end 也算不一致, 已得出的文件名照常列出."
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
    "| documents/evidence/expense_claim_E003.pdf | internal_record | internal_original | 2026-08-14 | E003 |",
    "| documents/evidence/payroll_register_terms.pdf | internal_record | internal_original "
    "| 2026-09-30 | 804;805;826 |",
    "| documents/evidence/accounting_policies_extract.pdf | internal_record | internal_original "
    "| 2026-07-01 | XJ-499 |",
    "| documents/evidence/diisr_email.pdf | correspondence | external_copy | 2026-10-13 | XJ-720 |",
    "| documents/evidence/smart_agency_invoice_SM0195-70135.pdf | invoice | external_copy | 2026-09-05 "
    "| XJ-548;BT-090-0097 |",
    "| documents/evidence/ridgeway_order_CRM08-12.pdf | agreement | external_copy | 2026-10-02 | XJ-647;BT-090-0092 |",
    "| documents/evidence/jakaranda_quote.pdf | internal_record | internal_copy | 2026-10-08 | BT-090-0099 |",
    "| financial_statements/fs_v1.pdf | financial_statements | internal_original | 2026-10-22 | FS |",
]
# 待审财报 35 行 (与试算表勾稽的 18 行, 权益变动表 5 行, 现金流量表 12 行): (code, amount_cents, sign, account_codes,
# prior_amount_cents)
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
    ("CE-OPEN", "-791200", "-1", "960 与全部损益科目", "0"),
    ("CE-NP", "-2213699", "-1", "全部损益科目", "-791200"),
    ("CE-OCI", "0", "-1", "无", "0"),
    ("CE-TCI (小计)", "-2213699", "-1", "全部损益科目", "-791200"),
    ("CE-CLOSE (小计)", "-3004899", "-1", "960 与全部损益科目", "-791200"),
    ("CF-NP", "-2213699", "+1", "全部损益科目", "-791200"),
    ("CF-REC", "-1019950", "+1", "610", "0"),
    ("CF-OREC", "-4853", "+1", "620", "0"),
    ("CF-PAY", "400507", "+1", "800, 801, 860", "0"),
    ("CF-TAX", "959317", "+1", "820, 825", "-79120"),
    ("CF-EMP", "895856", "+1", "804, 805, 826", "0"),
    ("CF-OP (小计)", "-982822", "+1", "全部损益科目与 610, 620, 800, 801, 860, 820, 825, 804, 805, 826", "-870320"),
    ("CF-INV", "0", "+1", "710, 711", "0"),
    ("CF-FIN", "0", "+1", "840", "0"),
    ("CF-NET (小计)", "-982822", "+1", "CF-OP 的科目与 710, 711, 840", "-870320"),
    ("CF-OPEN", "1050901", "+1", "090, 091", "1921221"),
    ("CF-CLOSE", "68079", "+1", "090, 091", "1050901"),
]


def test_formats_section() -> None:
    s95 = section(doc(), "### 9.5 文件格式与载入端口 (实现时以此为准)")
    lines = s95.splitlines()
    assert [ln for ln in lines if re.match(r"^\| (?:documents|financial_statements)/", ln)] == INDEX_ROWS
    fs = [tuple(c.strip() for c in ln.strip("|").split("|")) for ln in lines if re.match(r"^\| (?:SOFP|PL|CE|CF)-", ln)]
    assert fs == FS_ROWS
    assert (
        "E001 Oliver Gray 2026-07-20 full_time 14 240000; E002 Tracy Green 2026-07-20 full_time 14 180000; "
        "E003 Odette Garrison 2026-07-20 full_time 14 250000."
    ) in s95
    for token in (
        "acceptance 2 (appointment.pdf, engagement_letter.pdf, 停在 ACC-01 闸门时由演示数据导入上传)",
        "fieldwork 19 (3 份收入合同加 index.csv 中除承接文件与 fs_v1.pdf 外的 16 份",
        "statements 1 (fs_v1.pdf, 完成阶段按补交请求 REQ-financial_statements 上传)",
        "manual 1 (声明书",
        "registry 4",
        "confirmation 2",
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


# ── AASB 1060 等准则的段号 (R292): 定稿引用的段号须在 docs/参考资料 的正本里, 且该段 (子项) 含登记的一句原文 ──

AASB1060 = "03_会计准则_AASB/AASB_1060.pdf"
AASB1060_PAGES = (5, 52)  # 正文第 1-213 段 (第 5-43 页) 与附录 A 的定义 (第 52 页); 引用其后的段时放宽
# 附录 A 的定义没有段号, 按定义首句定位; 其下的子项按版面逐级取
DEFINITIONS = {
    "related party": "A related party is a person or entity",
    "close members of the family": "Close members of the family of a person are",
}
# 定稿中 "AASB 1060 第 X 段" 的每一项 X (逗号分隔) 与 "AASB 1060 附录 A {用语} 定义 {子项} 项" → 要核对的 (段号或定义加
# 子项, 该段或子项中的一句原文). 范围 a-b 核对两端; 同级子项连写 (25(c)(d)) 逐项核对
AASB1060_CITED: dict[str, list[tuple[str, str]]] = {
    "1": [("1", "elect to apply the Tier 2 reporting requirements under AASB 1053")],
    "4": [("4", "apply this Standard in relation to disclosure requirements only")],
    "10": [("10", "explicit and unreserved statement of such compliance in the notes")],
    "11": [("11", "it is a for-profit or not-for-profit entity")],
    "14": [("14", "at least, but is not limited to, twelve months from the reporting date")],
    "15": [("15", "the entity shall disclose those uncertainties")],
    "16": [("16", "presented for a period longer or shorter than one year")],
    "16(b)": [("16(b)", "the reason for using a longer or shorter period")],
    "16(c)": [("16(c)", "are not entirely comparable")],
    "24": [("24", "shall not offset assets and liabilities or income and expenses, unless required or permitted")],
    "25": [("25", "A complete set of financial statements of an entity shall include all of the following")],
    "25(c)": [("25(c)", "a statement of changes in equity for the reporting period")],
    "25(c)(d)": [
        ("25(c)", "a statement of changes in equity for the reporting period"),
        ("25(d)", "a statement of cash flows for the reporting period"),
    ],
    "26": [
        (
            "26",
            "may present a single statement of income and retained earnings in place of the statement of comprehensive"
            " income and statement of changes in equity",
        )
    ],
    "27": [("27", "it may present only a statement of profit or loss")],
    "28": [("28", "two of each of the required financial statements and related notes")],
    "37-41": [
        ("37", "as separate classifications in its statement of financial position"),
        ("41", "An entity shall classify all other liabilities as non-current."),
    ],
    "40(d)": [("40(d)", "does not have the right at the reporting date to defer settlement of the liability")],
    "20": [("20", "disclose comparative information in respect of the previous comparable period for all amounts")],
    "61": [("61", "for each component of equity, a reconciliation between the carrying amount at the beginning")],
    "61(a)": [("61(a)", "total comprehensive income for the period")],
    "63": [("63", "retained earnings at the beginning of the reporting period")],
    "64-89": [
        ("64", "information about the changes in cash and cash equivalents"),
        ("89", "cash and cash equivalent balances held by the entity that are not available for use"),
    ],
    "65": [("65", "bank overdrafts are a component of cash and cash equivalents")],
    "66": [("66", "classified by operating activities, investing activities and financing activities")],
    "70(a)": [("70(a)", "the indirect method, whereby profit or loss is adjusted for the effects of non-cash")],
    "88": [("88", "a reconciliation of the amounts presented in the statement of cash flows to the equivalent items")],
    "95": [("95", "An entity shall disclose material accounting policy information")],
    "98": [("98", "An entity shall disclose fees to each auditor or reviewer")],
    "110": [("110", "the amount of the correction for each financial statement line item affected")],
    "185": [("185", "to reflect adjusting events after the end of the reporting period")],
    "186": [("186", "the date when the financial statements were authorised for issue and who gave that")],
    "187": [("187", "an estimate of its financial effect or a statement that such an estimate cannot be made")],
    "189-203": [
        ("189", "affected by the existence of related parties"),
        ("203", "may disclose items of a similar nature in the aggregate"),
    ],
    "192": [("192", "the name of its parent and, if different, the ultimate controlling party")],
    "194": [("194", "An entity shall disclose key management personnel compensation in total.")],
    "198": [("198", "the amount of outstanding balances")],
    "199": [("199", "separately for each of the following categories")],
    "206": [("206", "sets out the requirements for which Standard may be applied upon first-time adoption")],
    "206-213": [
        ("206", "applies the requirements of AASB 1 First-time Adoption of Australian Accounting Standards"),
        ("213", "information deficiencies"),
    ],
    "208": [("208", "affected its reported financial position, financial performance and cash flows")],
    "210": [("210", "reconciliations of its equity determined in accordance with its previous financial reporting")],
    "212": [("212", "did not present financial statements for previous periods, it shall disclose that fact")],
    "related party (a)": [("related party (a)", "is related to a reporting entity if that person")],
    "related party (b)": [("related party (b)", "an entity is related to a reporting entity if any of the following")],
    "related party (b)(vi)": [
        ("related party (b)(vi)", "the entity is controlled or jointly controlled by a person identified in (a)")
    ],
    "related party (b)(viii)": [
        (
            "related party (b)(viii)",
            "a person identified in (a)(ii) has significant influence over the entity or is a member of the key"
            " management personnel of the entity",
        )
    ],
    "close members of the family (a)": [("close members of the family (a)", "children and spouse or domestic partner")],
}
# R292 新引的其他准则段: 定稿中的写法 → (PDF, 页范围, 段号, 该段或子项中的一句原文)
OTHER_CITED: dict[str, tuple[str, tuple[int, int], str, str]] = {
    "AASB 1053 第 17 段": (
        "03_会计准则_AASB/AASB_1053.pdf",
        (6, 7),
        "17",
        "Entities adopting Tier 2 reporting requirements for the first time that apply AASB 1",
    ),
    "AASB 1053 第 18A(a) 段": (
        "03_会计准则_AASB/AASB_1053.pdf",
        (6, 7),
        "18A(a)",
        "Tier 2 reporting requirements directly using the requirements in AASB 108",
    ),
    "AASB 1053 第 18A(c) 段": ("03_会计准则_AASB/AASB_1053.pdf", (6, 7), "18A(c)", "shall not apply AASB 1."),
    "AASB 1 第 3(d) 段": (
        "03_会计准则_AASB/AASB_1.pdf",
        (6, 7),
        "3(d)",
        "did not present financial statements for previous periods.",
    ),
    "AASB 1 第 AusF1 段": (
        "03_会计准则_AASB/AASB_1.pdf",
        (31, 31),
        "AusF1",
        "do not apply to entities preparing general purpose financial statements that apply AASB 1060",
    ),
    "AASB 1 第 20-33 段": ("03_会计准则_AASB/AASB_1.pdf", (31, 31), "AusF1", "paragraphs Aus3.2 and 20"),  # AusF1(a)
    "AASB 132 第 42 段": (
        "03_会计准则_AASB/AASB_132.pdf",
        (15, 15),
        "42",
        "currently has a legally enforceable right to set off the recognised amounts",
    ),
    "AASB 128 第 5 段": (
        "03_会计准则_AASB/AASB_128.pdf",
        (6, 6),
        "5",
        "less than 20 per cent of the voting power of the investee, it is presumed that the entity does not have"
        " significant influence",
    ),
}


@functools.cache
def reference_text(path: str, first: int, last: int) -> Text:
    return pdf_text(path, first, last)


def child_line(source: Text, k: int, group: str) -> int | None:
    """第 k 行记号的单元内, 以它为上级的子项 (group) 的记号行."""
    end = unit_end(source, k)
    for j in range(k + 1, len(source.lines)):
        start = source.starts[j]
        if start is None:
            continue
        if start >= end:
            return None
        if source.lines[j].mark == f"({group})" and parent(source, j) == k:
            return j
    return None


def definition_item(source: Text, term: str, group: str) -> list[int]:
    """附录 A 定义 (首句见 DEFINITIONS) 之下第一级子项的记号行: 在定义首句之后, 下一条定义之前, 与首句同 x."""
    found = []
    for k, line in enumerate(source.lines):
        if source.starts[k] is None or not line.text.startswith(DEFINITIONS[term]):
            continue
        for j in range(k + 1, len(source.lines)):
            other = source.lines[j]
            if source.starts[j] is None:
                continue
            if other.mark is None and abs(other.x - line.x) <= 2 and source[: source.starts[j]].rstrip().endswith("."):
                break  # 下一条定义
            if other.mark == f"({group})" and abs(other.x - line.x) <= 2:
                found.append(j)
                break
    return found


def cited_units(source: Text, ref: str) -> list[str]:
    """段号 ("25", "16(b)", "18A(a)", "AusF1") 或附录 A 定义加子项 ("related party (b)(viii)") 在原文中的单元正文
    (从记号起到单元结束, 见 test_knowledge.unit_end). 段号须为顶层的记号行 (没有上级); 版面认不出记号的段
    (如 "AusF1") 退而取以 "段号 " 开头的行. 子项逐级取, 上级须为前一级."""
    term = next((t for t in DEFINITIONS if ref.startswith(f"{t} ")), None)
    head = term or re.match(r"[0-9A-Za-z]+", ref)[0]  # type: ignore[index]
    groups = re.findall(r"\(([0-9a-z]+)\)", ref[len(head) :])
    if term:
        starts = definition_item(source, term, groups.pop(0))
    else:
        starts = [
            k
            for k, line in enumerate(source.lines)
            if source.starts[k] is not None and line.mark in (head, f"{head}.") and parent(source, k) is None
        ] or [
            k
            for k, line in enumerate(source.lines)
            if source.starts[k] is not None and line.mark is None and line.text.startswith(f"{head} ")
        ]
    units = []
    for k in starts:
        j: int | None = k
        for group in groups:
            j = child_line(source, j, group) if j is not None else None
        if j is not None:
            units.append(source[source.starts[j] : unit_end(source, j)])  # type: ignore[misc]
    return units


def aasb1060_citations(text: str) -> set[str]:
    """定稿中 AASB 1060 的段号引用: "AASB 1060 第 X, Y 段" 的各项与 "AASB 1060 附录 A {用语} 定义 {子项} 项"."""
    cited = {item for m in re.finditer(r"AASB 1060 第 ([0-9A-Za-z(), -]+?) 段", text) for item in m[1].split(", ")}
    terms = "|".join(DEFINITIONS)
    cited |= {f"{m[1]} {m[2]}" for m in re.finditer(rf"AASB 1060 附录 A ({terms}) 定义 ((?:\([0-9a-z]+\))+) 项", text)}
    return cited


def test_aasb1060_paragraphs_filled_in() -> None:
    """R292: 段号不再暂缓; 定稿里的 AASB 1060 段号引用都已登记 (登记的都在用), 写法只有两种, 不会漏核."""
    text = doc()
    assert "段号暂缓" not in text and "待 AASB 1060 正本" not in text
    assert "待正本" not in text.replace("## 待核与待正本", "")  # 不许有 "(待正本)" 一类不规范写法
    assert aasb1060_citations(text) == set(AASB1060_CITED)
    assert not re.search(r"AASB 1060 ?(?:para|\d)", text)  # 段号只用上面两种写法
    for citation in OTHER_CITED:
        assert citation in text, citation


@pytest.mark.parametrize("citation", sorted(AASB1060_CITED))
def test_aasb1060_cited_paragraphs_are_in_the_original(citation: str) -> None:
    """定稿引用的 AASB 1060 段号在正本 (第 7 次汇编) 中有, 且含登记原文的该段 (子项) 恰有一处 (第 214 段的表里也有以
    段号开头的行, 如 "213 Transition", 不算)."""
    source = reference_text(AASB1060, *AASB1060_PAGES)
    for ref, phrase in AASB1060_CITED[citation]:
        hits = [unit for unit in cited_units(source, ref) if normalise(phrase) in unit]
        assert len(hits) == 1, f"{ref}: 正本中含 {phrase!r} 的该段有 {len(hits)} 处"


@pytest.mark.parametrize("citation", sorted(OTHER_CITED))
def test_other_cited_paragraphs_are_in_the_original(citation: str) -> None:
    path, (first, last), ref, phrase = OTHER_CITED[citation]
    hits = [unit for unit in cited_units(reference_text(path, first, last), ref) if normalise(phrase) in unit]
    assert len(hits) == 1, f"{citation}: 原文中含 {phrase!r} 的该段有 {len(hits)} 处"


def test_citation_check_rejects_wrong_numbers() -> None:
    """反例: 段号标错, 子项标错上级, 定义子项标错, 都找不到或不含原文."""
    source = reference_text(AASB1060, *AASB1060_PAGES)
    assert normalise("not entirely comparable") not in cited_units(source, "16(b)")[0]
    assert cited_units(source, "17(c)") == []  # 第 17 段没有 (c) 项
    assert "the reason for using" not in cited_units(source, "15")[0]
    iii = cited_units(source, "related party (a)(iii)")
    assert len(iii) == 1 and "significant influence over the reporting entity" in iii[0]
    assert cited_units(source, "related party (a)(iv)") == []
    assert aasb1060_citations("见 AASB 1060 第 25(c)(d), 26 段与 AASB 1060 附录 A related party 定义 (b)(vi) 项") == {
        "25(c)(d)",
        "26",
        "related party (b)(vi)",
    }


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
    paths = [f["path"] for f in manifest["files"]]  # type: ignore[index]
    # llm/ 是大模型 fixture 的运行副本 (R78): 各程序段加 fixture 后随之变, 只与正本推出的集合比 (集合包含口径)
    # 另有上传处理的固定输出中复制进来的账表识别 (DOC-SHEET, 按上传的文件名取, 与数据集无关)
    llm = [p for p in paths if p.startswith("llm/")]
    fixtures = REPO / "tests" / "fixtures" / "llm_xero"
    uploads = REPO / "tests" / "fixtures" / "llm_variants" / "upload"
    assert llm == sorted(
        [
            "llm/README.md",
            *(f"llm/{p.relative_to(fixtures).as_posix()}" for p in fixtures.glob("*/*.json")),
            *(f"llm/{p.relative_to(uploads).as_posix()}" for p in uploads.glob("DOC-SHEET/*.json")),
        ]
    )
    assert [p for p in paths if not p.startswith("llm/")] == [
        "README.md",  # 生成方式与合同底稿的署名, 许可 (R286)
        "adjustments/journal_entries.csv",
        "bank_accounts.csv",
        "client_profile.json",
        "confirmations/12345678.pdf",
        "confirmations/12345679.pdf",
        "confirmations/receipts.json",
        "contract_links.csv",
        "contracts/CON-X01.pdf",
        "contracts/CON-X02.pdf",
        "contracts/CON-X03.pdf",
        "dataset_traits.json",
        "delivery_notes.csv",
        "documents/engagement/appointment.pdf",
        "documents/engagement/engagement_letter.pdf",
        "documents/evidence/accounting_policies_extract.pdf",
        "documents/evidence/diisr_email.pdf",
        "documents/evidence/director_loan_agreement.pdf",
        "documents/evidence/director_loan_confirmation.pdf",
        "documents/evidence/director_support_letter.pdf",
        "documents/evidence/expense_claim_E003.pdf",
        "documents/evidence/fixed_asset_register.pdf",
        "documents/evidence/going_concern_memo_forecast.pdf",
        "documents/evidence/jakaranda_quote.pdf",
        "documents/evidence/payroll_register_terms.pdf",
        "documents/evidence/ridgeway_order_CRM08-12.pdf",
        "documents/evidence/smart_agency_invoice_SM0195-70135.pdf",
        "documents/index.csv",
        "documents/related_party/harbourview_lease.pdf",
        "documents/related_party/rm2031_sublease.pdf",
        "documents/related_party/vpa_rent_letter.pdf",
        "documents/related_party/vpa_sale_termination_notice.pdf",
        "financial_statements/fs_v1.json",
        "financial_statements/fs_v1.pdf",
        "manual/fin08_round1.json",
        "manual/fin08_round2.json",
        "manual/representation.json",
        "manual/rpt07_reply.json",
        "payroll/employees.csv",
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
        "representation/representation_letter.pdf",
    ]
    contracts = {f"contracts/CON-X0{n}.pdf" for n in (1, 2, 3)}
    for item in manifest["files"]:  # type: ignore[index]
        extra = ["base"] if item["path"] in contracts else []  # 只有收入合同记底稿来源 (R286)
        assert list(item) == ["path", "sha256", "synthetic", "generator", "xero_refs", *extra]
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
        mock = SNAPSHOT.parent / "20261017-mock"
        assert notes == ([] if mock.is_dir() else [f"没有 20261017-mock ({checker.rel(mock)}): 跳过新旧快照比对"])
    else:
        assert len(notes) == 1 and "没有本地 Xero 快照" in notes[0]


def test_check_without_snapshot_still_checks_files(tmp_path: Path) -> None:
    notes = checker.check(SUPPLEMENTS, tmp_path / "absent")
    assert len(notes) == 1 and "没有本地 Xero 快照" in notes[0] and "补造表与快照的核对" in notes[0]
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
    next(f for f in manifest["files"] if f["path"] == "client_profile.json")["synthetic"] = False
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
    assert next(f for f in manifest["files"] if f["path"] == "client_profile.json")["xero_refs"] == good
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
    assert checker.check(copy, snapshot, determinism=False, scan=[]) == [
        f"不是正式补造目录 ({copy}): 跳过 documents 段份数, 页数与登记途径的计数",
        "快照缺 dataset.json, invoices.json, accounts.json, bank_statements.json, journals.json: "
        "跳过补造表与快照的核对, 快照关键数复算与新旧快照比对",
    ]
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
    # 上期没有编制财报 (定稿第 2.4 节), 与财报附注 1 一致; 不说 "上期财报未经审计" (R293 第 1 轮审核 R293-02)
    assert "No financial report was prepared for the year ended 30 June 2026." in first
    assert "Because the comparative information will be unaudited" in first and "not audited" not in first
    assert "4. Responsibilities of the directors" in second and "5. Applicable financial reporting framework" in second
    for item in ("(i) for the preparation", "(ii) for such internal control", "(iii) to provide us with"):
        assert item in second, item  # 第 6(b)(i)-(iii) 段
    assert "Australian Accounting Standards - Simplified Disclosures." in second and "not entirely comparable" in second
    assert "6. Form and content of our report" in third and "differs from its expected form and content" in third
    assert "AUD 4,800, excluding GST" in third and "Date: 2 September 2026" in third
    text = " ".join(pages)
    assert "AASB 1060" not in text and "paragraph" not in second  # 委托书只写简化披露框架的名称, 不写准则段号


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


def manifest_documents() -> dict[str, tuple[object, ...]]:
    """manifest documents 段: 路径 → (类型, 可靠性, 文件日期, 登记途径, 页数, 关联对象)."""
    return {
        d["path"]: (
            d["type"],
            d["reliability"],
            d["document_date"],
            d["registration"],
            d["page_count"],
            d["related_ids"],
        )
        for d in read("manifest.json")["documents"]  # type: ignore[index]
    }


def test_documents_in_manifest_and_index() -> None:
    docs = manifest_documents()
    expected = {
        "documents/engagement/appointment.pdf": ("minutes", "internal_copy", "2026-08-27", "acceptance", 4, []),
        "documents/engagement/engagement_letter.pdf": (
            "engagement_letter",
            "external_copy",
            "2026-08-28",
            "acceptance",
            3,
            [],
        ),
        **{
            f"registry/extracts/{acn}.pdf": ("registry_extract", "external_original", "2026-10-17", "registry", 2, [])
            for acn in ("900043021", "900043030", "900043049")
        },
        "registry/persons/MORGAN-ALEX-19790412.pdf": (
            "registry_extract",
            "external_original",
            "2026-10-17",
            "registry",
            1,
            [],
        ),
        **{
            f"confirmations/{n}.pdf": ("confirmation", "external_original", "2026-09-30", "confirmation", 2, [])
            for n in ("12345678", "12345679")
        },
        "contracts/CON-X01.pdf": ("contract", "external_copy", "2026-09-01", "fieldwork", 3, []),
        "contracts/CON-X02.pdf": ("contract", "external_copy", "2026-06-24", "fieldwork", 3, []),
        "contracts/CON-X03.pdf": ("contract", "external_copy", "2026-08-10", "fieldwork", 3, []),
    }
    assert {path: docs[path] for path in expected} == expected
    pages = [v[4] for v in expected.values()]
    assert sum(pages[:6]) == 14 and sum(pages[6:]) == 13  # 承接与登记 14 页, 回函与合同 13 页
    for item in read("manifest.json")["documents"]:  # type: ignore[index]
        assert item["document_id"] == "DOC-" + item["sha256"][:12] and item["synthetic"]
    assert (SUPPLEMENTS / "documents" / "index.csv").read_text(encoding="utf-8").splitlines()[:3] == [
        "path,document_type,reliability,document_date,related_ids",
        "documents/engagement/appointment.pdf,minutes,internal_copy,2026-08-27,",
        "documents/engagement/engagement_letter.pdf,engagement_letter,external_copy,2026-08-28,",
    ]


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
    assert [d["page_count"] for d in manifest["documents"] if d["path"].endswith("engagement_letter.pdf")] == [2]
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


# ── 收入合同, 服务完成记录, 合同关联与银行回函 ───────────────


def test_contract_pages() -> None:
    x01 = pdf_texts(SUPPLEMENTS / "contracts" / "CON-X01.pdf")
    assert len(x01) == 3 and not any("ABN" in page for page in x01)  # 决定 11: DIISR 保留原名, 不写 ABN
    assert "Customer: DIISR - Small Business Services" in x01[0] and "Date: 1 September 2026" in x01[0]
    assert "2.1 The Contract Price is $6,500.00 excluding GST (10 developer days at $650.00)." in x01[0]
    assert "from 3 September 2026 to 16 September 2026" in x01[0]
    assert (
        "4.1 The Services are complete when the Supplier has deployed the Module to the Customer's environment and "
        "the Customer's Project Sponsor has signed the Completion Certificate (the Completion Date). Title to and "
        "control of the Module pass to the Customer on the Completion Date."
    ) in x01[1]
    assert "3.1 The Supplier will carry out the development in its own development environment." in x01[1]
    assert "5.1 The Supplier may issue a tax invoice on or after the Completion Date." in x01[1]
    assert "no later than 30 days after the date of the invoice" in x01[1]
    assert "6.1 Any change to the Contract Price must be agreed by both parties in writing." in x01[2]
    assert "giving 10 Business Days' written notice" in x01[2]
    assert (
        "7.2 If this Statement of Work is terminated before the Completion Date, the Supplier is not entitled to "
        "payment for the Module."
    ) in x01[2]  # 终止时无权收款: 第 35(c) 段不满足
    x02 = pdf_texts(SUPPLEMENTS / "contracts" / "CON-X02.pdf")
    assert len(x02) == 3 and "Customer: Hamilton Smith Pty (ABN 19 900 043 058)" in x02[0]
    assert "Date: 24 June 2026" in x02[0] and "starts on 1 July 2026 and ends on 30 June 2027" in x02[0]
    assert "Monthly Fee of $500.00 excluding GST" in x02[0]
    assert (
        "4.1 The Support Services are provided throughout each calendar month, and the Customer simultaneously "
        "receives and consumes the benefit of the Support Services as the Supplier provides them."
    ) in x02[1]  # 第 35(a) 段
    assert "on or about the 17th day of that month" in x02[1] and "within 14 days after the date" in x02[1]
    assert "Priority 1 request within 4 business hours" in x02[2] and "10% of the Monthly Fee" in x02[2]
    assert "7.1 Either party may terminate this Agreement by giving 30 days' written notice" in x02[2]
    x03 = pdf_texts(SUPPLEMENTS / "contracts" / "CON-X03.pdf")
    assert len(x03) == 3 and "Customer: Maddox Publishing Group (ABN 21 900 043 067)" in x03[0]
    assert "for the Customer's CRM integration project" in x03[0]  # 同 ORC1014 的发票行描述
    assert "Date: 10 August 2026 Customer purchase order: H008-346" in x03[0]
    assert "6 On-site Days at $700.00 per day, a total of $4,200.00 excluding GST" in x03[0]
    assert "4.1 Each On-site Day is accepted when the Customer signs the timesheet" in x03[1]
    assert "within 10 days after the date of the invoice" in x03[1]
    assert "credit $350.00 for each half day of absence" in x03[2] and "5 Business Days' written" in x03[2]


# ── 收入合同的 CUAD 底稿 (R286) ───────────────

CUAD = generator.CUAD_DIR
needs_cuad = pytest.mark.skipif(not CUAD.is_dir(), reason=f"本机没有 CUAD ({CUAD}); 取法: {generator.CUAD_FETCH}")
CONTRACTS = ("CON-X01", "CON-X02", "CON-X03")
CUAD_NOTE = (
    "General terms adapted from a contract in CUAD v1 (The Atticus Project, CC BY 4.0); modified for this "
    "synthetic document. Licence: https://creativecommons.org/licenses/by/4.0/"
)
KEY_LINES = {  # 第 4-7 节逐行冻结 (页 → 行, 同改写前): DOC-04 的条款 text 是子条款连号照抄 (设计稿 6.1), 改写不得增删
    "CON-X01": {
        2: [
            "4. Completion",
            "4.1 The Services are complete when the Supplier has deployed the Module to the",
            "Customer's environment and the Customer's Project Sponsor has signed the Completion",
            "Certificate (the Completion Date). Title to and control of the Module pass to the",
            "Customer on the Completion Date.",
            "",
            "5. Invoicing and Payment",
            "5.1 The Supplier may issue a tax invoice on or after the Completion Date.",
            "5.2 The Customer must pay each tax invoice by its due date, which is no later than",
            "30 days after the date of the invoice.",
        ],
        3: [
            "6. Variations",
            "6.1 Any change to the Contract Price must be agreed by both parties in writing.",
            "",
            "7. Termination",
            "7.1 Either party may terminate this Statement of Work by giving 10 Business Days'",
            "written notice to the other party.",
            "7.2 If this Statement of Work is terminated before the Completion Date, the Supplier is",
            "not entitled to payment for the Module.",
        ],
    },
    "CON-X02": {
        2: [
            "4. Provision of the Support Services",
            "4.1 The Support Services are provided throughout each calendar month, and the Customer",
            "simultaneously receives and consumes the benefit of the Support Services as the Supplier",
            "provides them.",
            "",
            "5. Invoicing and Payment",
            "5.1 The Supplier will issue a tax invoice for the Monthly Fee of each calendar month on",
            "or about the 17th day of that month.",
            "5.2 The Customer must pay each tax invoice within 14 days after the date of the",
            "invoice.",
        ],
        3: [
            "6. Service Levels",
            "6.1 If the Supplier does not respond to a Priority 1 request within 4 business hours,",
            "the Customer may claim a credit of 10% of the Monthly Fee for that month.",
            "",
            "7. Termination",
            "7.1 Either party may terminate this Agreement by giving 30 days' written notice to the",
            "other party.",
        ],
    },
    "CON-X03": {
        2: [
            "4. Acceptance",
            "4.1 Each On-site Day is accepted when the Customer signs the timesheet for that day.",
            "",
            "5. Invoicing and Payment",
            "5.1 The Supplier will issue a tax invoice after the sixth On-site Day. The Customer",
            "must pay the invoice within 10 days after the date of the invoice.",
        ],
        3: [
            "6. Absence",
            "6.1 If the project manager is absent for part of an On-site Day, the Supplier will",
            "credit $350.00 for each half day of absence.",
            "",
            "7. Termination",
            "7.1 Either party may terminate this Agreement by giving 5 Business Days' written",
            "notice to the other party.",
        ],
    },
}
AMOUNTS = {"CON-X01": ["$6,500.00", "$650.00"], "CON-X02": ["$500.00"], "CON-X03": ["$350.00", "$4,200.00", "$700.00"]}
DATES = {
    "CON-X01": ["1 September 2026", "16 September 2026", "3 September 2026"],
    "CON-X02": ["1 July 2026", "24 June 2026", "30 June 2027"],
    "CON-X03": ["10 August 2026", "11 August 2026", "26 August 2026"],
}
# 第 4-7 节以外不得出现会动收入结论的措辞 (付款, 控制权与所有权, 退货, 贷项与折扣, 终止, 担保, 关联方)
REVENUE_WORDS = re.compile(
    r"warrant|guarantee|refund|return|rebate|discount|credit|title|owner|intellectual property|related|partner"
    r"|terminat|invoice|payment|\bpaid\b|\bpay\b|interest|deposit|bonus|penalt|liquidated"
    r"|risk|accept|deliver|reimburs|expense|overdue|\blate\b|indemn",
    re.I,
)
ORIGINAL_HITS = {  # 改写前就有的行 (X02 的 2.1 期限与 3.1 月费)
    "CON-X02": [
        "is terminated earlier under clause 7.",
        "3.1 The Customer will pay a Monthly Fee of $500.00 excluding GST for each calendar month",
    ]
}
BASE_COMPANIES = (
    "Clickstream",
    "InfinixSoft",
    "TriZetto",
    "TRIZETTO",
    "Medical Manager",
    "MMMW",
    "RadialSpark",
    "Radial Spark",
    "Clear Capital",
    "MERITLIFE",
    "MeritLife",
)


def contract_spec(name: str) -> dict:
    return next(d for d in spec()["documents"] if d["path"] == f"contracts/{name}.pdf")


def test_contract_rewrite_keeps_key_terms() -> None:
    """改写只加一般条款: 第 4-7 节的子条款, 金额与日期同设计稿 4.3; 其余各节没有收入条款的措辞; 每份仍 3 页."""
    for name in CONTRACTS:
        pages = contract_spec(name)["pages"]
        assert len(pages) == 3 and all(len(page) <= generator.PAGE_LINES for page in pages), name
        lines = [line for page in pages for line in page]
        section, frozen = 0, dict[int, list[str]]()
        for n, page in enumerate(pages, 1):
            for line in page:
                if m := re.match(r"(\d+)\. [A-Z]", line):
                    section = int(m.group(1))
                if 4 <= section <= 7:
                    frozen.setdefault(n, []).append(line)
        assert {n: "\n".join(v).rstrip("\n").split("\n") for n, v in frozen.items()} == KEY_LINES[name], name
        text = " ".join(lines)
        assert sorted(set(re.findall(r"\$[\d,]+\.\d{2}", text))) == AMOUNTS[name], name
        months = "January|February|March|April|May|June|July|August|September|October|November|December"
        assert sorted(set(re.findall(rf"\b\d{{1,2}} (?:{months}) \d{{4}}\b", text))) == DATES[name], name
        section, outside = 0, []
        for line in lines:
            if m := re.match(r"(\d+)\. [A-Z]", line):
                section = int(m.group(1))
            if not 4 <= section <= 7 and REVENUE_WORDS.search(line):
                outside.append(line)
        assert outside == ORIGINAL_HITS.get(name, []), name
    assert not any("ABN" in line for page in contract_spec("CON-X01")["pages"] for line in page)  # 决定 11


def test_contract_pdfs_credit_cuad_without_naming_the_base() -> None:
    for name in CONTRACTS:
        pages = pdf_texts(SUPPLEMENTS / "contracts" / f"{name}.pdf")
        assert pages[2].rstrip().endswith(f"{CUAD_NOTE} Contract No. {name} | Page 3 of 3"), name  # 签字之后, 页脚之前
        assert not [c for c in BASE_COMPANIES if any(c in page for page in pages)], name


def test_contract_bases_in_spec_manifest_and_readme() -> None:
    from tests.tools import cuad_fetch

    files = {f["path"]: f for f in read("manifest.json")["files"]}  # type: ignore[index]
    expected = {
        "CON-X01": "ClickstreamCorp_20200330_1-A_EX1A-6 MAT CTRCT_12089935_EX1A-6 MAT CTRCT_Development Agreement.pdf",
        "CON-X02": "TRIZETTOGROUPINC_08_18_1999-EX-10.17-TECHNICAL INFRASTRUCTURE MAINTENANCE AGREEMENT.PDF",
        "CON-X03": "MERITLIFEINSURANCECO_06_19_2020-EX-10.(XIV)-MASTER SERVICES AGREEMENT.PDF",
    }
    readme = (SUPPLEMENTS / "README.md").read_text(encoding="utf-8")
    s43 = section(doc(), "### 4.3 收入合同")
    assert "第 4-7 节一字不加" in s43 and "(不变)" not in s43
    assert "Contract Understanding Atticus Dataset (CUAD) v1, The Atticus Project, CC BY 4.0" in section(
        doc(), "## 1 文件布局与来源"
    )
    for name, file in expected.items():
        assert f"| {name} | {file} (" in s43  # 设计稿记同一份底稿
        base = contract_spec(name)["base"]
        assert list(base) == ["dataset", "file", "txt", "sha256", "excerpts"] and base["file"] == file
        assert base["txt"] == file.rsplit(".", 1)[0] + ".txt" and re.fullmatch(r"[0-9a-f]{64}", base["sha256"])
        assert all(not re.search(r"\.\s+[A-Z]", e["text"]) for e in base["excerpts"])  # 每条至多一句
        assert files[f"contracts/{name}.pdf"]["base"] == {"dataset": "CUAD v1", "file": file}
        assert f"contracts/{name}.pdf: {file}" in readme
    assert [p for p, f in files.items() if "base" in f] == [f"contracts/{n}.pdf" for n in CONTRACTS]
    assert "Contract Understanding Atticus Dataset (CUAD) v1, The Atticus Project, CC BY 4.0." in readme
    assert "https://zenodo.org/records/4595826" in readme and "https://creativecommons.org/licenses/by/4.0/" in readme
    assert (
        "已改写" in readme and cuad_fetch.DISCLAIMER in readme and "uv run python -m tests.tools.cuad_fetch" in readme
    )
    assert generator.CUAD_TERMS in readme and "https://www.atticusprojectai.org/disclaimer" in readme
    assert files["README.md"]["synthetic"] is True
    for name in ("ATTRIBUTION", "RECORD_URL", "LICENSE_URL", "DISCLAIMER"):  # 生成脚本与下载脚本的常量一致
        assert getattr(generator, f"CUAD_{name}") == getattr(cuad_fetch, name)
    assert generator.CUAD_DIR == cuad_fetch.DEFAULT_DEST / cuad_fetch.TARGET


def test_contract_base_records_are_checked(tmp_path: Path) -> None:
    copy = copy_supplements(tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    assert checker.check_contract_bases(copy, manifest) == []
    item = next(f for f in manifest["files"] if f["path"] == "contracts/CON-X02.pdf")
    item["base"]["file"] = "other.pdf"
    next(f for f in manifest["files"] if f["path"] == "bank_accounts.csv")["base"] = {"dataset": "CUAD v1", "file": "x"}
    (copy / "README.md").write_text("edited", encoding="utf-8")
    assert checker.check_contract_bases(copy, manifest) == [
        f"{copy / 'README.md'}: 与规格推出的说明不符, 需重新生成",
        f"{copy / 'bank_accounts.csv'}: manifest 的 base 与规格不符",
        f"{copy / 'contracts' / 'CON-X02.pdf'}: manifest 的 base 与规格不符",
    ]


def test_spec_base_records_are_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = spec()
    docs = {d["path"]: d for d in data["documents"]}
    docs["contracts/CON-X01.pdf"]["base"]["excerpts"].append({"clause": "4.2", "text": "Not in the contract"})
    docs["contracts/CON-X02.pdf"]["base"]["excerpts"][0]["text"] = "One sentence. Another sentence."
    del docs["contracts/CON-X03.pdf"]["base"]["txt"]
    docs["contracts/CON-X01.pdf"]["pages"][2] = docs["contracts/CON-X01.pdf"]["pages"][2][:-1]  # 去掉许可链接
    patched = tmp_path / "spec.json"
    patched.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(generator, "SPEC", patched)
    manifest = read("manifest.json")
    problems = checker.check_contract_bases(SUPPLEMENTS, manifest)  # type: ignore[arg-type]
    where = f"{patched}"
    assert [p for p in problems if p.startswith(where)] == [
        f"{where} contracts/CON-X01.pdf: 取用原句对应的条款 4.2 不在正文里",
        f"{where} contracts/CON-X01.pdf: 末页最后没有 CUAD 出处说明与许可链接",
        f"{where} contracts/CON-X02.pdf: 取用原句 1.2 应为 clause 与 text, text 至多一句",
        f"{where} contracts/CON-X03.pdf: base 应有 dataset, file, txt, sha256, excerpts, dataset 为 CUAD v1, "
        "且至少一条取用原句",
    ]


def test_check_without_cuad_notes_the_fetch_command(tmp_path: Path) -> None:
    notes = checker.check(SUPPLEMENTS, tmp_path / "absent", cuad=tmp_path / "no-cuad")
    assert (
        f"没有 CUAD ({tmp_path / 'no-cuad'}): 跳过合同底稿的核对 (底稿文件, sha256 与取用原句); 合同 PDF 按规格生成, "
        "不受影响. 取 CUAD: uv run python -m tests.tools.cuad_fetch"
    ) in notes
    assert not [n for n in checker.check(SUPPLEMENTS, tmp_path / "absent") if "CUAD" in n]  # 不给 cuad 时不核对


@needs_snapshot
def test_generator_without_cuad_prints_the_hint_and_same_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(generator, "CUAD_DIR", tmp_path / "no-cuad")
    out = tmp_path / "out"
    assert generator.main(["--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert f"没有 CUAD ({tmp_path / 'no-cuad'})" in printed and "uv run python -m tests.tools.cuad_fetch" in printed
    produced = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    expected = {p.relative_to(SUPPLEMENTS).as_posix(): p.read_bytes() for p in SUPPLEMENTS.rglob("*") if p.is_file()}
    assert produced == expected  # 已入库的合同 PDF 与有无 CUAD 无关


def test_cuad_source_problems_are_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = spec()
    docs = {d["path"]: d for d in data["documents"]}
    fake = tmp_path / "CUAD_v1"
    (fake / "full_contract_txt").mkdir(parents=True)
    (fake / "full_contract_pdf" / "Part_I").mkdir(parents=True)
    x01, x03 = docs["contracts/CON-X01.pdf"]["base"], docs["contracts/CON-X03.pdf"]["base"]
    (fake / "full_contract_txt" / x01["txt"]).write_text("not the base contract", encoding="utf-8")
    kept = x03["excerpts"][0]["text"]
    (fake / "full_contract_txt" / x03["txt"]).write_text(f"Preamble.  {kept}\n", encoding="utf-8")
    x03["sha256"] = generator.sha256(fake / "full_contract_txt" / x03["txt"])
    (fake / "full_contract_pdf" / "Part_I" / x01["file"]).write_bytes(b"%PDF")
    (fake / "CUAD_v1_README.txt").write_text(generator.CUAD_DISCLAIMER, encoding="utf-8")  # 缺使用条款一句
    patched = tmp_path / "spec.json"
    patched.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(generator, "SPEC", patched)
    lacking = ", ".join(e["clause"] for e in x03["excerpts"][1:])
    x02 = docs["contracts/CON-X02.pdf"]["base"]
    assert checker.check_cuad_sources(fake) == [
        f"{fake / 'CUAD_v1_README.txt'}: 没有补造 README 引用的免责句或使用条款原句",
        f"{fake / 'full_contract_txt' / x01['txt']}: sha256 与规格记录的不符",
        f"{fake / 'full_contract_txt' / x02['txt']}: contracts/CON-X02.pdf 的底稿不存在",
        f"{fake / 'full_contract_txt' / x03['txt']}: 找不到 contracts/CON-X03.pdf 第 {lacking} 条的取用原句",
        f"{fake / 'full_contract_pdf'}: 没有 contracts/CON-X03.pdf 的底稿 PDF",
    ]


@needs_cuad
def test_cuad_sources_match() -> None:
    assert checker.check_cuad_sources(CUAD) == []


def table(name: str) -> list[dict[str, str]]:
    import csv

    with (SUPPLEMENTS / name).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_contract_links_and_delivery_notes() -> None:
    assert [(r["invoice_no"], r["contract_id"]) for r in table("contract_links.csv")] == [
        ("ORC1026", "CON-X01"),
        ("ORC1003", "CON-X02"),
        ("ORC1008", "CON-X02"),
        ("ORC1016", "CON-X02"),
        ("ORC1020", "CON-X02"),
        ("ORC1014", "CON-X03"),
    ]
    rows = table("delivery_notes.csv")
    assert list(rows[0]) == ["id", "delivery_no", "invoice_no", "shipped_date", "signed_date", "signed_by"]
    expected = {
        **{f"ORC{n}": ("2026-07-01", "2026-07-31") for n in range(1008, 1012)},
        **{f"ORC{n}": ("2026-08-01", "2026-08-31") for n in range(1016, 1020)},
        **{f"ORC{n}": ("2026-09-01", "2026-09-30") for n in range(1020, 1024)},
        "ORC1006": ("2026-07-21", "2026-07-23"),
        "ORC1002": ("2026-07-20", "2026-07-24"),
        "ORC1004": ("2026-07-29", "2026-07-29"),
        "ORC1000": ("2026-07-27", "2026-07-31"),
        "ORC1001": ("2026-07-20", "2026-07-31"),
        "ORC1007": ("2026-07-06", "2026-07-31"),
        "ORC1012": ("2026-08-13", "2026-08-14"),
        "ORC1013": ("2026-08-14", "2026-08-20"),
        "ORC1014": ("2026-08-11", "2026-08-26"),
        "ORC1015": ("2026-09-01", "2026-09-04"),
        "ORC1024": ("2026-08-19", "2026-08-19"),
        "ORC1026": ("2026-09-03", "2026-09-17"),
    }
    assert {r["invoice_no"]: (r["shipped_date"], r["signed_date"]) for r in rows} == expected and len(rows) == 24
    assert all(r["id"] == r["delivery_no"] == f"SR-{r['invoice_no']}" and r["signed_by"] for r in rows)
    sponsor = next(r["signed_by"] for r in rows if r["invoice_no"] == "ORC1026")
    x01 = pdf_texts(SUPPLEMENTS / "contracts" / "CON-X01.pdf")[2]
    assert f"{sponsor}, Project Sponsor" in x01  # 完工证书由 DIISR Project Sponsor 签


def test_bank_accounts_and_receipts() -> None:
    demo_header = (REPO / "data" / "demo" / "bank_accounts.csv").read_text(encoding="utf-8").splitlines()[0]
    assert (SUPPLEMENTS / "bank_accounts.csv").read_text(encoding="utf-8") == (
        f"{demo_header}\n"
        "BA-090,Wattlebrook Bank Limited,306234,12345678,090\n"
        "BA-091,Wattlebrook Bank Limited,306234,12345679,091\n"
    )
    assert read("confirmations/receipts.json") == {
        "12345678": {"receipt_channel": "direct_email"},
        "12345679": {"receipt_channel": "direct_email"},
    }


def test_confirmation_pages() -> None:
    overdraft = pdf_texts(SUPPLEMENTS / "confirmations" / "12345678.pdf")
    assert len(overdraft) == 2
    first, second = overdraft
    assert "WATTLEBROOK BANK LIMITED ABN 82 938 074 116" in first and "Tessellate Audit Partners" in first
    assert "Customer: Demo Company (AU) Pty Ltd (ABN 25 900 043 021)" in first
    assert "Balance at 30 September 2026: AUD 10,900.44 (debit - account overdrawn)" in first
    assert "accrued interest and fees not yet debited: nil" in first.lower()
    for words in (
        "Limit: AUD 15,000.00",
        "Repayable on demand",
        "Annual review due 31 March 2027",
        "Personal guarantee given by Alex Morgan",
        "There are no set-off arrangements",
        "Account 12345679 (Business Savings Account): AUD 11,581.23 (credit)",
        "This reply is sent by email directly to the auditor",
    ):
        assert words in second, words
    assert all("Wattlebrook Bank Limited | Ref WBL-AC-2026-093001" in page for page in overdraft)
    savings = pdf_texts(SUPPLEMENTS / "confirmations" / "12345679.pdf")
    assert len(savings) == 2
    assert "Balance at 30 September 2026: AUD 11,581.23 (credit)" in savings[0]
    assert "No interest was credited to this account between 1 July 2026 and 30 September 2026" in savings[0]
    assert (
        "Accrued interest at 30 September 2026, not yet credited to the account: AUD 48.53 (in the customer's "
        "favour; interest period 16 September to 15 October 2026, payable on 16 October 2026)"
    ) in savings[0]
    assert "Facilities: none" in savings[1] and "This reply is sent by email directly to the auditor" in savings[1]
    assert all("Wattlebrook Bank Limited | Ref WBL-AC-2026-093002" in page for page in savings)


def test_table_problems_are_reported(tmp_path: Path) -> None:
    copy = copy_supplements(tmp_path)
    notes = (copy / "delivery_notes.csv").read_text(encoding="utf-8").splitlines()
    notes[1] = notes[1].replace("2026-07-27", "2026-08-02")  # SR-ORC1000 开始日晚于完成日
    notes.append(notes[2].replace("SR-ORC1001,SR-ORC1001", "SR-X,SR-ORC1001"))  # 重复且 id 不对
    (copy / "delivery_notes.csv").write_text("\n".join(notes) + "\n", encoding="utf-8")
    (copy / "contract_links.csv").write_text(
        "invoice_no,contract_id\nORC1026,CON-X01\nORC1026,CON-X09\n", encoding="utf-8"
    )
    edit_json(copy / "confirmations" / "receipts.json", lambda d: d["12345679"].update(receipt_channel="fax"))
    reply = copy / "confirmations" / "12345678.pdf"
    source = next(d for d in spec()["documents"] if d["path"] == "confirmations/12345678.pdf")["pages"]
    pages = ["\n".join(p).replace("10,900.44", "10,900.40") for p in source]
    generator.write_pdf(reply, pages, "Wattlebrook Bank Limited | Ref WBL-AC-2026-093001", HEADER)
    generator.write_manifest(copy, zed_snapshot(tmp_path))
    lines = check_error(copy, tmp_path / "absent").splitlines()
    assert [line for line in lines if "文本层" not in line] == [
        f"{copy / 'delivery_notes.csv'}: 同一发票有多条记录: ORC1001",
        f"{copy / 'delivery_notes.csv'}: id 与 delivery_no 应为 SR-{{发票号}}: SR-X",
        f"{copy / 'delivery_notes.csv'}: 开始日晚于完成日: SR-ORC1000",
        f"{copy / 'contract_links.csv'}: 发票号重复: ORC1026",
        f"{copy / 'contract_links.csv'}: 合同 PDF 不存在: CON-X09",
        f"{copy / 'confirmations' / 'receipts.json'}: receipt_channel 不是有效的回函渠道",
        f"{reply}: 第 1 页没有写出规格中的余额或应计利息",
    ]


@needs_snapshot
def test_tables_agree_with_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert checker.check_against_snapshot(SUPPLEMENTS, SNAPSHOT) == []
    copy = copy_supplements(tmp_path)
    notes = (copy / "delivery_notes.csv").read_text(encoding="utf-8").splitlines()
    notes = [n for n in notes if ",ORC1015," not in n]  # 少一张本期发票
    notes.append("SR-ORC1027,SR-ORC1027,ORC1027,2026-10-01,2026-10-16,Priya Natarajan")  # 期后且未生效
    (copy / "delivery_notes.csv").write_text("\n".join(notes) + "\n", encoding="utf-8")
    links = (copy / "contract_links.csv").read_text(encoding="utf-8")
    (copy / "contract_links.csv").write_text(links + "ORC1028,CON-X02\n", encoding="utf-8")  # 草稿发票
    accounts = (copy / "bank_accounts.csv").read_text(encoding="utf-8").replace("12345679,091", "12345670,091")
    (copy / "bank_accounts.csv").write_text(accounts, encoding="utf-8")
    data = spec()
    for doc in data["documents"]:
        if doc.get("confirmation", {}).get("bank_account_id") == "BA-091":
            doc["confirmation"].update(balance_cents=1158124, accrued_interest_cents=9705)
    patched = tmp_path / "spec.json"
    patched.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(generator, "SPEC", patched)
    assert checker.check_against_snapshot(copy, SNAPSHOT) == [
        f"{copy / 'delivery_notes.csv'}: 不是本期生效的销售发票: ORC1027",
        f"{copy / 'delivery_notes.csv'}: 没有记录的本期发票应恰好是 ORC1003, ORC1005",
        f"{copy / 'contract_links.csv'}: 快照中没有这些生效发票: ORC1028",
        f"{copy / 'bank_accounts.csv'}: BA-091 与快照银行科目的账号不一致",
        f"{patched}: confirmations/12345679.pdf 的余额与快照对账单期末余额不符",
        f"{patched}: confirmations/12345679.pdf 的应计利息不等于 BT-091-0002 按 15/30 折算",
    ]


def test_more_table_problems(tmp_path: Path) -> None:
    """坏日期不拖累别的表; 回函渠道少一个账号; 应计利息写成 10 月全额; 其他账户一节的余额与该账户回函不符."""
    copy = copy_supplements(tmp_path)
    notes = (copy / "delivery_notes.csv").read_text(encoding="utf-8").replace("2026-09-17,Priya", "2026-09-31,Priya")
    (copy / "delivery_notes.csv").write_text(notes, encoding="utf-8")
    links = (copy / "contract_links.csv").read_text(encoding="utf-8")
    (copy / "contract_links.csv").write_text(links + "ORC1014,CON-X03\n", encoding="utf-8")
    edit_json(copy / "confirmations" / "receipts.json", lambda d: d.pop("12345678"))
    documents = {d["path"]: d for d in spec()["documents"]}
    for path, old, new in (
        ("confirmations/12345679.pdf", "AUD 48.53", "AUD 97.05"),  # 第 6.1 节指出的抽取风险
        ("confirmations/12345678.pdf", "11,581.23", "11,581.24"),
    ):
        pages = ["\n".join(p).replace(old, new) for p in documents[path]["pages"]]
        generator.write_pdf(copy / path, pages, documents[path]["footer"], HEADER)
    generator.write_manifest(copy, zed_snapshot(tmp_path))
    lines = [line for line in check_error(copy, tmp_path / "absent").splitlines() if "文本层" not in line]
    assert lines == [
        f"{copy / 'delivery_notes.csv'}: 日期格式不对: SR-ORC1026",
        f"{copy / 'contract_links.csv'}: 发票号重复: ORC1014",
        f"{copy / 'confirmations' / 'receipts.json'}: 账号应与回函 PDF 一一对应",
        f"{copy / 'confirmations' / '12345678.pdf'}: 其他账户 12345679 的余额与该账户回函不符",
        f"{copy / 'confirmations' / '12345679.pdf'}: 第 1 页没有写出规格中的余额或应计利息",
    ]


@needs_snapshot
def test_bank_accounts_and_refs_against_snapshot(tmp_path: Path) -> None:
    copy = copy_supplements(tmp_path)
    accounts = (copy / "bank_accounts.csv").read_text(encoding="utf-8").splitlines()
    (copy / "bank_accounts.csv").write_text("\n".join(accounts[:2]) + "\n", encoding="utf-8")  # 少了 BA-091
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    next(f for f in manifest["files"] if f["path"] == "contracts/CON-X03.pdf")["xero_refs"].append("XJ-99999")
    (copy / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")  # --inject 副本不在生成时核对
    assert checker.check_against_snapshot(copy, SNAPSHOT) == [
        f"{copy / 'bank_accounts.csv'}: 行应与快照银行科目 BA-090, BA-091 一一对应",
        f"{copy / 'manifest.json'}: xero_refs 在快照中找不到: XJ-99999",
    ]


# ── 关联方, 董事借款与持续经营文件 ──────────────────────────


def test_related_party_documents() -> None:
    rent = pdf_texts(SUPPLEMENTS / "documents" / "related_party" / "vpa_rent_letter.pdf")
    assert len(rent) == 1 and "25 June 2026" in rent[0] and "ACN 900 043 030" in rent[0]
    assert "Rent: AUD 1,500.00 per calendar month plus GST." in rent[0]
    assert "payable on the 21st day of that month" in rent[0] and "No written lease will be entered into" in rent[0]
    assert "at least one month's written notice" in rent[0]
    assert "Dana Morgan Director, Victoria Property Agency Pty Ltd" in rent[0] and "Alex Morgan, Director" in rent[0]
    notice = pdf_texts(SUPPLEMENTS / "documents" / "related_party" / "vpa_sale_termination_notice.pdf")
    assert len(notice) == 1 and "28 August 2026" in notice[0]
    assert "to Harbourview Nominees Pty Ltd. Settlement is due on 30 September 2026" in notice[0]
    assert "will end on 30 September 2026" in notice[0] and "Truxton Property Management" in notice[0]
    lease = pdf_texts(SUPPLEMENTS / "documents" / "related_party" / "harbourview_lease.pdf")
    assert len(lease) == 2 and "Date: 22 September 2026" in lease[0]
    assert "starts on 1 October 2026 and ends on 30 September 2028" in lease[0]
    assert "The annual rent is AUD 12,886.32 plus GST" in lease[0] and "AUD 1,073.86 plus GST" in lease[0]
    assert "may sublet part of the Premises with the Landlord's prior written consent" in lease[0]
    sublease = pdf_texts(SUPPLEMENTS / "documents" / "related_party" / "rm2031_sublease.pdf")
    assert len(sublease) == 1 and "Date: 15 October 2026" in sublease[0] and "Majestic Contracting" in sublease[0]
    assert "Rent: AUD 450.00 per month" in sublease[0] and "room RM2031" in sublease[0]
    assert round(1073.86 * 12, 2) == 12886.32  # 年租与月租对得上


def test_director_loan_and_support_documents() -> None:
    agreement = pdf_texts(SUPPLEMENTS / "documents" / "evidence" / "director_loan_agreement.pdf")
    assert len(agreement) == 2 and "Date: 10 September 2018" in agreement[0]
    assert "working capital" in agreement[0] and "The loan is interest-free." in agreement[0]
    assert "The loan is unsecured." in agreement[0] and "repayable on demand" in agreement[0]
    confirmation = pdf_texts(SUPPLEMENTS / "documents" / "evidence" / "director_loan_confirmation.pdf")
    assert len(confirmation) == 1 and "16 October 2026" in confirmation[0]
    assert "AUD 19,212.21 recorded in account 840" in confirmation[0] and "includes the AUD 1.00" in confirmation[0]
    assert "opening bank balances were brought in against account 840" in confirmation[0]
    assert '"Loan from director" within current liabilities' in confirmation[0]
    support = pdf_texts(SUPPLEMENTS / "documents" / "evidence" / "director_support_letter.pdf")
    assert len(support) == 1 and "16 October 2026" in support[0]
    assert "for 12 months from the date of the auditor's report" in support[0]
    assert "AUD 19,212.21" in support[0] and "up to AUD 40,000" in support[0]
    assert "AUD 1,000.00 on 12 October 2026 and AUD 1,500.00 on 15 October 2026" in support[0]
    assert "asset" not in support[0].lower()  # 不附个人资产证明 (ASA 570 A13, A19)


def forecast_rows() -> list[list[str]]:
    page = pdf_texts(SUPPLEMENTS / "documents" / "evidence" / "going_concern_memo_forecast.pdf")[1]
    return re.findall(
        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) 20\d\d) ((?:\(?[\d,]+\.\d\d\)? ?){6})(\*)?", page
    )


def amount(text: str) -> int:
    value = int(text.strip("()").replace(",", "").replace(".", ""))
    return -value if text.startswith("(") else value


def test_going_concern_memo_and_forecast() -> None:
    pages = pdf_texts(SUPPLEMENTS / "documents" / "evidence" / "going_concern_memo_forecast.pdf")
    assert len(pages) == 3 and "Date: 16 October 2026" in pages[0]
    assert "13 months from October 2026 to October 2027" in pages[0] and "late October 2027" in pages[0]
    assert "Overdraft limit 15,000.00" in pages[0] and "Unused overdraft at 30 September 2026 4,099.56" in pages[0]
    assert "Total receipts 1-16 October 26,991.65" in pages[0] and "Net cash at 16 October 2026 13,353.72" in pages[0]
    assert "PAYG withholding of AUD 7,535.00" in pages[2] and "superannuation guarantee of AUD 4,261.20" in pages[2]
    assert "PC Complete, AUD 2,166.99" in pages[2] and "about AUD 14,500 per month" in pages[2]
    assert "funding gaps in November 2026 and January 2027" in pages[2] and "AUD 6,000 and AUD 14,000" in pages[2]
    assert "AUD 3,014.00 of PAYG withheld from the 11 and 25 October pay runs" in pages[2]
    customers = re.findall(r"\d\d Oct ([A-Z][^\d]+?) ([\d,]+\.\d\d) ", pages[0].split("Customer receipts")[0])
    assert len(customers) == 13 and sum(amount(a) for _, a in customers) == 2170510
    assert "Customer receipts 21,705.10" in pages[0] and "Payments 1-16 October 14,318.72" in pages[0]
    rows = forecast_rows()
    months = [r[0] for r in rows]
    assert months == [f"{m} 2026" for m in ("Oct", "Nov", "Dec")] + [
        f"{m} 2027" for m in ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct")
    ]
    cash, funding = 68079, 0  # 09-30 两户净额 11,581.23 - 10,900.44
    for month, numbers, star in rows:
        receipts, payments, before, director, closing, available = (amount(x) for x in numbers.split())
        assert before == cash + receipts - payments and closing == before + director, month
        assert available == closing + 1500000 and bool(star) == (before + 1500000 < 0), month
        cash, funding = closing, funding + director
    assert [r[0] for r in rows if r[2]] == ["Nov 2026", "Jan 2027"]  # 缺口只在这两个月
    assert funding - 250000 <= 4000000  # 10 月已转入的 2,500 之外, 追加不超过支持函的 40,000


@needs_snapshot
def test_memo_october_receipts_match_bank_statements() -> None:
    from datetime import date

    from backend.adapters.xero_dataset import map_bank_transactions

    lines = map_bank_transactions(generator.Snapshot(SNAPSHOT))  # type: ignore[arg-type]
    window = [t for t in lines if date(2026, 10, 1) <= t.txn_date <= date(2026, 10, 16)]
    assert sum(t.amount_cents for t in window if t.amount_cents > 0) == 2699165
    assert -sum(t.amount_cents for t in window if t.amount_cents < 0) == 1431872
    last = {t.bank_account_id: t.balance_after_cents for t in window}
    assert last["BA-090"] + last["BA-091"] == 1335372  # 备忘录第 1 页的 16 日净额
    page = pdf_texts(SUPPLEMENTS / "documents" / "evidence" / "going_concern_memo_forecast.pdf")[0]
    listed = sorted(
        (int(day), amount(value))
        for day, value in re.findall(r"(\d\d) Oct [A-Z][^\d]+? ([\d,]+\.\d\d) ", page.split("Interest and other")[0])
    )
    others = {"BT-090-0107", "BT-090-0130", "BT-090-0121", "BT-090-0138", "BT-091-0002"}  # 董事转入与 336.55 的三笔
    statement = sorted((t.txn_date.day, t.amount_cents) for t in window if t.amount_cents > 0 and t.id not in others)
    assert listed == statement  # 逐笔对上对账单 (客户回款 13 笔, 订金与分租各 1 笔)
    assert sum(t.amount_cents for t in window if t.id in others - {"BT-090-0107", "BT-090-0130"}) == 33655


def test_r66_index_rows_and_manifest() -> None:
    rows = (SUPPLEMENTS / "documents" / "index.csv").read_text(encoding="utf-8").splitlines()
    assert rows[3:11] == [
        "documents/related_party/vpa_rent_letter.pdf,agreement,external_copy,2026-06-25,ACN-900043030;XJ-420;XJ-518;XJ-593",
        "documents/related_party/vpa_sale_termination_notice.pdf,correspondence,external_copy,2026-08-28,ACN-900043030",
        "documents/related_party/harbourview_lease.pdf,agreement,external_copy,2026-09-22,ACN-900043030;XJ-613",
        "documents/related_party/rm2031_sublease.pdf,agreement,external_copy,2026-10-15,BT-090-0131",
        "documents/evidence/director_loan_agreement.pdf,agreement,internal_original,2018-09-10,XJ-397",
        "documents/evidence/director_loan_confirmation.pdf,correspondence,internal_original,2026-10-16,"
        "XJ-397;CLIENT-XERO-3C1332BB-Q1",
        "documents/evidence/going_concern_memo_forecast.pdf,internal_record,internal_original,2026-10-16,"
        "CLIENT-XERO-3C1332BB-Q1",
        "documents/evidence/director_support_letter.pdf,correspondence,internal_original,2026-10-16,"
        "CLIENT-XERO-3C1332BB-Q1;BT-090-0107;BT-090-0130",
    ]
    docs = manifest_documents()
    new = [r.split(",")[0] for r in rows[3:11]]
    assert {docs[p][3] for p in new} == {"fieldwork"} and sum(int(docs[p][4]) for p in new) == 12  # type: ignore[call-overload]


def test_index_problems_are_reported(tmp_path: Path) -> None:
    copy = copy_supplements(tmp_path)
    index = (copy / "documents" / "index.csv").read_text(encoding="utf-8")
    index = index.replace("ACN-900043030;XJ-613", "ACN-900043099;XJ-613").replace(
        "2026-10-16,CLIENT-XERO-3C1332BB-Q1\n", "2026-10-16,CLIENT-XERO-OTHER\n"
    )
    (copy / "documents" / "index.csv").write_text(index, encoding="utf-8")
    support = copy / "documents" / "evidence" / "director_support_letter.pdf"
    source = next(d for d in spec()["documents"] if d["path"].endswith("director_support_letter.pdf"))
    generator.write_pdf(support, ["\n".join(source["pages"][0]).replace("40,000", "4,000")], source["footer"], HEADER)
    generator.write_manifest(copy, zed_snapshot(tmp_path))
    lines = [line for line in check_error(copy, tmp_path / "absent").splitlines() if "文本层缺 1 行" not in line]
    where = copy / "documents" / "index.csv"
    assert lines == [
        f"{where}: documents/related_party/harbourview_lease.pdf 与 manifest 的登记信息不一致",
        f"{where}: documents/evidence/going_concern_memo_forecast.pdf 与 manifest 的登记信息不一致",
        f"{where}: documents/related_party/harbourview_lease.pdf 的关联对象 ACN-900043099 不在 registry/asic.json",
        f"{where}: documents/evidence/going_concern_memo_forecast.pdf 的关联对象 CLIENT-XERO-OTHER 不是画像 ID",
        f"{support}: 文本层缺关键词 40,000",
    ]


@needs_snapshot
def test_index_xero_objects_must_exist(tmp_path: Path) -> None:
    copy = copy_supplements(tmp_path)
    index = (copy / "documents" / "index.csv").read_text(encoding="utf-8").replace(";XJ-613", ";XJ-99999")
    (copy / "documents" / "index.csv").write_text(index, encoding="utf-8")
    assert checker.snapshot_related(copy, generator.Snapshot(SNAPSHOT)) == [
        f"{copy / 'documents' / 'index.csv'}: 关联对象在快照中找不到: XJ-99999"
    ]


def test_more_index_problems(tmp_path: Path) -> None:
    """index.csv 换序, 缺列, 未知类型的关联对象; 删掉 index.csv 也要报."""
    copy = copy_supplements(tmp_path)
    rows = (copy / "documents" / "index.csv").read_text(encoding="utf-8").splitlines()
    rows[3], rows[4] = rows[4], rows[3]  # 换序
    rows[5] = rows[5].replace("ACN-900043030;XJ-613", "ZZ-1")  # 未知类型
    rows[6] = rows[6].split(",")[0]  # 只剩一列
    (copy / "documents" / "index.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    generator.write_manifest(copy, zed_snapshot(tmp_path))
    where = copy / "documents" / "index.csv"
    count = sum(1 for d in spec()["documents"] if d.get("index"))
    lines = check_error(copy, tmp_path / "absent").splitlines()
    assert lines == [
        f"{where}: 行应为规格中 index 为真的 {count} 份文件, 顺序同规格",
        f"{where}: documents/related_party/harbourview_lease.pdf 与 manifest 的登记信息不一致",
        f"{where}: documents/related_party/rm2031_sublease.pdf 与 manifest 的登记信息不一致",
        f"{where}: documents/related_party/harbourview_lease.pdf 的关联对象 ZZ-1 类型未知",
    ]
    (copy / "documents" / "index.csv").unlink()
    generator.write_manifest(copy, zed_snapshot(tmp_path / "again"))
    assert check_error(copy, tmp_path / "absent") == f"{where}: 缺 documents/index.csv (规格有 {count} 份 index 文件)"


# ── 其他证据文件, 工资册与更正分录 ──────────────────────────


def evidence(name: str) -> list[str]:
    return pdf_texts(SUPPLEMENTS / "documents" / "evidence" / name)


def test_other_evidence_documents() -> None:
    register = evidence("fixed_asset_register.pdf")
    assert len(register) == 1 and "As at 30 September 2026" in register[0]
    assert "Total cost at 30 September 2026: Nil" in register[0]
    assert "Additions after the reporting date (for information)" in register[0]
    assert "04 Oct 2026 ABC Furniture Desks, chairs, reception furniture 710 6,300.00" in register[0]
    assert "11 Oct 2026 PC Complete Laptop for Tracy Green 720 1,969.99" in register[0]
    claim = evidence("expense_claim_E003.pdf")
    assert len(claim) == 2 and "Employee name: Odette Garrison" in claim[0] and "Employee number: E003" in claim[0]
    assert "Date of claim: 14 August 2026" in claim[0] and "Total claimed 296.95" in claim[0]
    assert (
        "GST included 26.31" in claim[0]
        and "Approved for payment: Alex Morgan, Director Date: 16 August 2026" in claim[0]
    )
    assert claim[1].count("Receipt ") == 4 and "Total of receipts AUD 296.95" in claim[1]  # 4 张收据
    assert "Grainger" not in " ".join(claim)  # 系统录入的误写只在 Xero 里
    payroll = evidence("payroll_register_terms.pdf")
    assert len(payroll) == 2
    for row in (
        "E001 Oliver Gray 20 Jul 2026 Full-time Fortnightly 2,400.00",
        "E002 Tracy Green 20 Jul 2026 Full-time Fortnightly 1,800.00",
        "E003 Odette Garrison 20 Jul 2026 Full-time Fortnightly 2,500.00",
        "Total 33,500.00 7,535.00 25,965.00",
    ):
        assert row in payroll[0], row
    assert "ends on the pay day, a Sunday" in payroll[0]
    assert "4 weeks of paid annual leave" in payroll[1] and "National Employment Standards" in payroll[1]
    policies = evidence("accounting_policies_extract.pdf")
    assert len(policies) == 1 and "1 July 2026" in policies[0]
    assert (
        "their cost, net of any trade discount or supplier credit, is AUD 1,000 or more, excluding GST" in policies[0]
    )
    assert "office equipment over 5 years" in policies[0] and "for each on-site day as it is provided" in policies[0]
    email = evidence("diisr_email.pdf")
    assert len(email) == 1 and "Date: 13 October 2026" in email[0]
    assert "price concession of AUD 1,050.00 plus GST on invoice ORC1026" in email[0]
    assert "commercial arrangement agreed today" in email[0] and "credit note for AUD 1,155.00" in email[0]
    smart = evidence("smart_agency_invoice_SM0195-70135.pdf")
    assert len(smart) == 1 and "Invoice date: 5 September 2026" in smart[0]
    assert "Design concepts for advertising material 4 September 2026 1,818.18" in smart[0]
    assert "Total (GST inclusive) 2,000.00" in smart[0] and "Due date: 19 September 2026" in smart[0]
    order = evidence("ridgeway_order_CRM08-12.pdf")
    assert len(order) == 1 and "PURCHASE ORDER CRM08-12" in order[0] and "Date: 2 October 2026" in order[0]
    assert "3 days per week on site" in order[0] and "starting on 5 October 2026" in order[0]
    assert "invoice each month in advance at the start of the month" in order[0]
    quote = evidence("jakaranda_quote.pdf")
    assert len(quote) == 1 and "Date: 8 October 2026" in quote[0] and "(new project)" in quote[0]
    assert "A deposit of AUD 2,000.00 is payable on acceptance" in quote[0]


def test_r67_index_rows_and_manifest() -> None:
    rows = (SUPPLEMENTS / "documents" / "index.csv").read_text(encoding="utf-8").splitlines()
    ev = "documents/evidence/"
    assert len(rows) >= 19 and rows[11:19] == [  # 表头加 R64 至 R67 的 18 行
        f"{ev}fixed_asset_register.pdf,internal_record,internal_original,2026-09-30,XJ-499;XJ-626;XJ-676",
        f"{ev}expense_claim_E003.pdf,internal_record,internal_original,2026-08-14,E003",
        f"{ev}payroll_register_terms.pdf,internal_record,internal_original,2026-09-30,804;805;826",
        f"{ev}accounting_policies_extract.pdf,internal_record,internal_original,2026-07-01,XJ-499",
        f"{ev}diisr_email.pdf,correspondence,external_copy,2026-10-13,XJ-720",
        f"{ev}smart_agency_invoice_SM0195-70135.pdf,invoice,external_copy,2026-09-05,XJ-548;BT-090-0097",
        f"{ev}ridgeway_order_CRM08-12.pdf,agreement,external_copy,2026-10-02,XJ-647;BT-090-0092",
        f"{ev}jakaranda_quote.pdf,internal_record,internal_copy,2026-10-08,BT-090-0099",
    ]
    docs = manifest_documents()
    new = [r.split(",")[0] for r in rows[11:19]]
    assert {docs[p][3] for p in new} == {"fieldwork"} and sum(int(docs[p][4]) for p in new) == 10  # type: ignore[call-overload]


def test_payroll_and_adjustments_tables() -> None:
    from backend.contracts.records import JournalEntry

    assert (SUPPLEMENTS / "payroll" / "employees.csv").read_text(encoding="utf-8") == (
        "employee_id,name,start_date,employment_type,pay_period_days,gross_per_period_cents\n"
        "E001,Oliver Gray,2026-07-20,full_time,14,240000\n"
        "E002,Tracy Green,2026-07-20,full_time,14,180000\n"
        "E003,Odette Garrison,2026-07-20,full_time,14,250000\n"
    )
    demo_header = (REPO / "data" / "demo" / "journal_entries.csv").read_text(encoding="utf-8").splitlines()[0]
    rows = table("adjustments/journal_entries.csv")
    assert (SUPPLEMENTS / "adjustments" / "journal_entries.csv").read_text(encoding="utf-8").splitlines()[
        0
    ] == demo_header
    assert [
        (r["id"], r["account_code"], int(r["debit_cents"]), int(r["credit_cents"]), r["source_doc"]) for r in rows
    ] == [
        ("AJE-X01-1", "711", 82500, 0, "XJ-499"),
        ("AJE-X01-2", "710", 0, 75000, "XJ-499"),
        ("AJE-X01-3", "820", 0, 7500, "XJ-499"),
        ("AJE-X02-1", "478", 426120, 0, "826"),
        ("AJE-X02-2", "826", 0, 426120, "826"),
        ("AJE-X03-1", "477", 201000, 0, "804"),
        ("AJE-X03-2", "804", 0, 201000, "804"),
        ("AJE-X04-1", "477", 268736, 0, "805"),
        ("AJE-X04-2", "805", 0, 268736, "805"),
        ("AJE-X05-1", "620", 4853, 0, "BA-091"),
        ("AJE-X05-2", "270", 0, 4853, "BA-091"),
    ]
    created = {r["entry_no"]: r["created_at"] for r in rows}
    assert created == {
        "AJE-X01": "2026-10-20T03:00:00Z",
        "AJE-X02": "2026-10-21T23:30:00Z",
        "AJE-X03": "2026-10-21T23:30:00Z",
        "AJE-X04": "2026-10-21T23:30:00Z",
        "AJE-X05": "2026-10-20T03:00:00Z",
    }
    for r in rows:
        entry = JournalEntry.model_validate({k: (v if v != "" else None) for k, v in r.items()})
        assert (str(entry.posting_date), entry.created_by, entry.approved_by, entry.is_adjustment) == (
            "2026-09-30",
            "xero",
            None,
            True,
        )
        assert entry.description and entry.description.startswith(f"Audit adjustment {entry.entry_no}")
        assert "revers" not in entry.description.lower()
    s28 = section(doc(), "### 2.8 更正分录 (adjustments/journal_entries.csv, 全部合成)")
    for r in rows:
        assert f'"{r["description"]}"' in s28, r["id"]  # 每一行都是第 2.8 节写出的原句


def test_payroll_self_check() -> None:
    staff = table("payroll/employees.csv")
    gross = sum(int(r["gross_per_period_cents"]) for r in staff)
    assert gross == 670000 and gross * 3 // 10 == 201000
    assert round(73 / 7 * 4 / 52 * (gross / 2)) == 268736  # 07-20 至 09-30 共 73 天, 周工资 3,350
    assert round((3350000 + 201000) * 0.12) == 426120


def test_adjustment_problems_are_reported(tmp_path: Path) -> None:
    copy = copy_supplements(tmp_path)
    path = copy / "adjustments" / "journal_entries.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[3] = lines[3].replace(",0,7500,", ",0,7600,")  # AJE-X01 借贷不平衡
    lines[6] = lines[6].replace("wages accrued for", "reversal of wages accrued for")
    lines[7] = lines[7].replace(",0,201000,", ",0,200000,").replace(",201000,0,", ",200000,0,")
    lines[6] = lines[6].replace(",201000,0,", ",200000,0,")  # AJE-X03 改成 2,000.00, 仍平衡
    (copy / "payroll" / "employees.csv").write_text(
        (copy / "payroll" / "employees.csv")
        .read_text(encoding="utf-8")
        .replace("2026-07-20,full_time,14,250000", "2026-07-13,full_time,14,250000"),
        encoding="utf-8",
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    generator.write_manifest(copy, zed_snapshot(tmp_path))
    where = copy / "adjustments" / "journal_entries.csv"
    staff = copy / "payroll" / "employees.csv"
    assert [line for line in check_error(copy, tmp_path / "absent").splitlines() if "index.csv" not in line] == [
        f"{where}: AJE-X01 借贷不平衡",
        f"{where}: AJE-X03 各行摘要应相同",
        f"{where}: AJE-X03 的摘要应以 Audit adjustment AJE-X03 开头且不含 revers",
        f"{staff}: 应计工资更正额不等于每期毛工资合计 × 3 / 10",
        f"{staff}: 年假更正额不等于按工资册推算的金额",
    ]
    lines[1] = lines[1].replace(",xero,,711,", ",xero,,711,x")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    generator.write_manifest(copy, zed_snapshot(tmp_path / "again"))
    assert f"{where}: AJE-X01-1 不符合 JournalEntry (1 处)" in check_error(copy, tmp_path / "absent").splitlines()


@needs_snapshot
def test_adjusted_trial_balance(tmp_path: Path) -> None:
    snapshot = generator.Snapshot(SNAPSHOT)
    assert checker.snapshot_adjustments(SUPPLEMENTS, snapshot) == []
    copy = copy_supplements(tmp_path)
    path = copy / "adjustments" / "journal_entries.csv"
    text = (
        path.read_text(encoding="utf-8")
        .replace(",478,426120,0,", ",478,426000,0,")
        .replace(",826,0,426120,", ",826,0,426000,")
    )
    path.write_text(
        text.replace(
            "AJE-X05-1,AJE-X05,2026-09-30,2026-10-20T03:00:00Z,xero,,620,",
            "AJE-X05-1,AJE-X05,2026-09-30,2026-10-20T03:00:00Z,xero,,999,",
        ),
        encoding="utf-8",
    )
    where = copy / "adjustments" / "journal_entries.csv"
    assert checker.snapshot_adjustments(copy, snapshot) == [
        f"{where}: 科目不在快照科目表: 999",
        f"{where}: 更正后试算表与设计不符: 478, 620, 826",
        f"{where}: 更正后本期损益 -2213579 不等于 -2213699",
        f"{where}: 养老金更正额不等于 (更正前工资费用 + 应计工资) × 0.12",
    ]


@needs_snapshot
def test_adjustments_must_post_at_period_end(tmp_path: Path) -> None:
    """全部更正分录的入账日一致但不是期末时报出."""
    copy = copy_supplements(tmp_path)
    path = copy / "adjustments" / "journal_entries.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    moved = [lines[0], *(line.replace(",2026-09-30,", ",2026-09-29,", 1) for line in lines[1:])]
    assert len(moved) == 12 and all(",2026-09-29," in line for line in moved[1:])
    path.write_text("\n".join(moved) + "\n", encoding="utf-8")
    assert checker.snapshot_adjustments(copy, generator.Snapshot(SNAPSHOT)) == [
        f"{path}: 更正分录的入账日应为期末 2026-09-30",
    ]


def test_more_adjustment_problems(tmp_path: Path) -> None:
    """前缀, 行 ID, is_adjustment, 录入人与审批人, 入账日不一; 某行不合模型时只报一次."""
    copy = copy_supplements(tmp_path)
    path = copy / "adjustments" / "journal_entries.csv"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[4] = lines[4].replace("AJE-X02-1,AJE-X02", "AJE-X02-3,AJE-X02")  # 行 ID 不连续
    lines[6] = lines[6].replace(",xero,,477,", ",alex,cfo,477,")  # 录入人与审批人
    lines[8] = lines[8].replace(",true", ",false").replace("2026-09-30", "2026-09-29")  # 非调整分录, 入账日不同
    lines[10] = lines[10].replace("AJE-X05-1,AJE-X05,", "ADJ-X05-1,ADJ-X05,")
    lines[11] = lines[11].replace("AJE-X05-2,AJE-X05,", "ADJ-X05-2,ADJ-X05,")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    generator.write_manifest(copy, zed_snapshot(tmp_path))
    where = copy / "adjustments" / "journal_entries.csv"
    got = [line for line in check_error(copy, tmp_path / "absent").splitlines() if str(where) in line]
    assert got == [
        f"{where}: AJE-X02 的行 ID 应为 AJE-X02-1 起连续编号",
        f"{where}: AJE-X03 的录入人应为 xero, 审批人应为空",
        f"{where}: AJE-X04 的 is_adjustment 应为 true",
        f"{where}: ADJ-X05 不以 AJE- 开头",
        f"{where}: ADJ-X05 的摘要应以 Audit adjustment ADJ-X05 开头且不含 revers",
        f"{where}: 更正分录的入账日应全部相同 (期末)",
    ]
    lines[1] = lines[1].replace(",xero,,711,", ",xero,,711,x")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    generator.write_manifest(copy, zed_snapshot(tmp_path / "again"))
    message = check_error(copy, tmp_path / "absent")
    assert message.count("AJE-X01-1 不符合 JournalEntry") == 1  # 不在工资自检下重复报


@needs_snapshot
def test_expense_claim_matches_xero_entry() -> None:
    """报销单 E003 的收据与 Xero 中这笔报销的入账分录 XJ-493 逐张对得上, 日期不晚于入账日."""
    from datetime import date

    from backend.adapters.xero_dataset import journal_entries_from_api, journal_rows

    entries = journal_entries_from_api(generator.Snapshot(SNAPSHOT))  # type: ignore[arg-type]
    claim = next(e for e in entries if e.entry_no == "XJ-493")
    lines = [r for r in journal_rows([claim]) if r.entry_no == "XJ-493"]
    gst = sum(r.debit_cents for r in lines if r.account_code == "820")
    nets = sorted(r.debit_cents for r in lines if r.account_code in ("493", "420"))
    receipts = evidence("expense_claim_E003.pdf")[1]
    amounts = [amount(a) for a in re.findall(r"AUD ([\d,]+\.\d\d) \(", receipts)][:4]
    gst_parts = [amount(a) for a in re.findall(r"GST AUD ([\d,]+\.\d\d) included", receipts)]
    assert sum(gst_parts) == gst == 2631 and sum(amounts) == 29695
    assert sorted(a - g for a, g in zip(amounts, [*gst_parts, 0], strict=True)) == nets  # 不含税额逐张相等
    days = [int(d) for d in re.findall(r"(\d\d) Aug 2026", receipts)]
    assert len(days) == 4 and max(date(2026, 8, d) for d in days) <= claim.posting_date == date(2026, 8, 16)


PL_CODES = ["200", "270", "400", "404", "408", "412", "420", "429", "445", "449", "453", "461", "469", "477", "478"]
PL_CODES += ["489", "493"]  # 17 个损益科目
EQ_CODES = ["960", *PL_CODES]  # 累计亏损行的科目
OP_CODES = [*PL_CODES, "610", "620", "800", "801", "860", "820", "825", "804", "805", "826"]  # 经营活动各行的科目
FS_LINES = [  # 需求清单 R216 的 XERO_FS_V1 (待审财报唯一正本): 代码, 表, 名称, 科目, sign, 小计, 本期, 比较数
    ("SOFP-CASH", "financial_position", "Cash and cash equivalents", ["091"], 1, False, 1158123, 1508123),
    ("SOFP-REC", "financial_position", "Trade receivables", ["610"], 1, False, 1019950, 0),
    ("SOFP-OREC", "financial_position", "Other receivables (accrued interest)", ["620"], 1, False, 4853, 0),
    ("SOFP-PPE", "financial_position", "Office equipment", ["710", "711"], 1, False, 0, 0),
    ("SOFP-TA", "financial_position", "Total assets", ["091", "610", "620", "710", "711"], 1, True, 2182926, 1508123),
    ("SOFP-OD", "financial_position", "Bank overdraft", ["090"], -1, False, 1090044, 457222),
    ("SOFP-PAY", "financial_position", "Trade and other payables", ["800", "801", "860"], -1, False, 400507, 0),
    ("SOFP-TAX", "financial_position", "GST and PAYG withholding", ["820", "825"], -1, False, 880197, -79120),
    ("SOFP-EMP", "financial_position", "Employee benefit liabilities", ["804", "805", "826"], -1, False, 895856, 0),
    ("SOFP-DLOAN", "financial_position", "Loan from director", ["840"], -1, False, 1921221, 1921221),
    (
        "SOFP-TL",
        "financial_position",
        "Total liabilities",
        ["090", "800", "801", "860", "820", "825", "804", "805", "826", "840"],
        -1,
        True,
        5187825,
        2299323,
    ),
    (
        "SOFP-EQ",
        "financial_position",
        "Accumulated losses",
        ["960", *PL_CODES],
        -1,
        False,
        -3004899,
        -791200,
    ),
    ("PL-REV", "profit_or_loss", "Revenue", ["200"], -1, False, 3243100, 420000),
    ("PL-OI", "profit_or_loss", "Interest income", ["270"], -1, False, 4853, 0),
    ("PL-EMP", "profit_or_loss", "Employee benefits expense", ["477", "478"], 1, False, 4245856, 0),
    ("PL-OCC", "profit_or_loss", "Occupancy expenses", ["408", "445", "469"], 1, False, 635600, 0),
    (
        "PL-OTH",
        "profit_or_loss",
        "Other expenses",
        ["400", "404", "412", "420", "429", "449", "453", "461", "489", "493"],
        1,
        False,
        580196,
        1211200,
    ),
    (
        "PL-NP",
        "profit_or_loss",
        "Loss for the period",
        PL_CODES,
        -1,
        True,
        -2213699,
        -791200,
    ),
    # 权益变动表 (R293): 只有累计亏损一项, 2025-07-01 (过渡日) 为 0
    ("CE-OPEN", "changes_in_equity", "Balance at the beginning of the period", EQ_CODES, -1, False, -791200, 0),
    ("CE-NP", "changes_in_equity", "Loss for the period", PL_CODES, -1, False, -2213699, -791200),
    ("CE-OCI", "changes_in_equity", "Other comprehensive income", [], -1, False, 0, 0),  # 第 61(a) 段, 两期都为 0
    ("CE-TCI", "changes_in_equity", "Total comprehensive loss for the period", PL_CODES, -1, True, -2213699, -791200),
    ("CE-CLOSE", "changes_in_equity", "Balance at the end of the period", EQ_CODES, -1, True, -3004899, -791200),
    # 现金流量表 (R293, 间接法): 各变动行为财务状况表对应行的变动对现金的影响; 透支计入现金及现金等价物
    ("CF-NP", "cash_flows", "Loss for the period", PL_CODES, 1, False, -2213699, -791200),
    ("CF-REC", "cash_flows", "Increase in trade receivables", ["610"], 1, False, -1019950, 0),
    ("CF-OREC", "cash_flows", "Increase in other receivables", ["620"], 1, False, -4853, 0),
    ("CF-PAY", "cash_flows", "Increase in trade and other payables", ["800", "801", "860"], 1, False, 400507, 0),
    ("CF-TAX", "cash_flows", "Change in GST and PAYG withholding", ["820", "825"], 1, False, 959317, -79120),
    ("CF-EMP", "cash_flows", "Increase in employee benefit liabilities", ["804", "805", "826"], 1, False, 895856, 0),
    ("CF-OP", "cash_flows", "Net cash used in operating activities", OP_CODES, 1, True, -982822, -870320),
    ("CF-INV", "cash_flows", "Net cash from investing activities", ["710", "711"], 1, False, 0, 0),
    ("CF-FIN", "cash_flows", "Net cash from financing activities", ["840"], 1, False, 0, 0),
    (
        "CF-NET",
        "cash_flows",
        "Net decrease in cash and cash equivalents",
        [*OP_CODES, "710", "711", "840"],
        1,
        True,
        -982822,
        -870320,
    ),
    ("CF-OPEN", "cash_flows", "Cash and cash equivalents at 1 July", ["090", "091"], 1, False, 1050901, 1921221),
    ("CF-CLOSE", "cash_flows", "Cash and cash equivalents at end of period", ["090", "091"], 1, False, 68079, 1050901),
]
TB_LINES = [x for x in FS_LINES if x[1] in ("financial_position", "profit_or_loss")]  # 与试算表勾稽的 18 行


def test_financial_statements_json() -> None:
    fs = read(generator.FS_JSON)
    assert list(fs) == [  # type: ignore[arg-type]
        "id",
        "version",
        "prepared_at",
        "directors_declaration_date",
        "document_id",
        "comparative_restatement_entry_nos",
        "lines",
    ]
    assert (fs["id"], fs["version"], fs["prepared_at"], fs["directors_declaration_date"]) == (  # type: ignore[index]
        "FS",
        1,
        "2026-10-22",
        "2026-10-23",
    )
    assert fs["comparative_restatement_entry_nos"] == ["XJ-397"]  # type: ignore[index]
    keys = ["code", "statement", "name", "account_codes", "sign", "subtotal", "amount_cents", "prior_amount_cents"]
    assert [tuple(line[k] for k in keys) for line in fs["lines"]] == [  # type: ignore[index]
        (c, st, n, codes, sg, sub, a, p) for c, st, n, codes, sg, sub, a, p in FS_LINES
    ]
    assert all(
        list(line)
        == ["code", "statement", "name", "amount_cents", "sign", "account_codes", "subtotal", "prior_amount_cents"]
        for line in fs["lines"]  # type: ignore[index]
    )
    docs = {d["path"]: d for d in read("manifest.json")["documents"]}  # type: ignore[index]
    assert fs["document_id"] == docs[generator.FS_PDF]["document_id"]  # type: ignore[index]
    by = {c: (a, p) for c, *_, a, p in FS_LINES}
    assert by["SOFP-TA"][0] == by["SOFP-TL"][0] + by["SOFP-EQ"][0]  # 21,829.26 = 51,878.25 + (30,048.99)
    assert by["SOFP-EQ"][1] + by["PL-NP"][0] == by["SOFP-EQ"][0]  # 期初 (7,912.00) 加本期 (22,136.99)
    manifest = read("manifest.json")
    item = next(f for f in manifest["files"] if f["path"] == generator.FS_JSON)  # type: ignore[index]
    assert item["xero_refs"] == ["XJ-397"]


def test_financial_statements_pdf() -> None:
    pages = pdf_texts(SUPPLEMENTS / generator.FS_PDF)
    assert len(pages) == 6
    first, second, equity, cash, third, fourth = pages
    assert "FINANCIAL REPORT FOR THE FINANCIAL YEAR ENDED 30 SEPTEMBER 2026" in first  # 第 6 批的期间写法
    assert "STATEMENT OF FINANCIAL POSITION AS AT 30 SEPTEMBER 2026" in first
    for row in (
        "Cash and cash equivalents 11,581.23 15,081.23",
        "Office equipment - -",
        "Total assets 21,829.26 15,081.23",
        "Bank overdraft 3 10,900.44 4,572.22",
        "GST and PAYG withholding 8,801.97 (791.20)",
        "Employee benefit liabilities 8,958.56 -",
        "Total liabilities 51,878.25 22,993.23",
        "Accumulated losses (30,048.99) (7,912.00)",
    ):
        assert row in first
    assert "STATEMENT OF PROFIT OR LOSS AND OTHER COMPREHENSIVE INCOME" in second
    assert "FOR THE FINANCIAL YEAR ENDED 30 SEPTEMBER 2026" in second
    for row in (
        "Revenue 32,431.00 4,200.00",
        "Interest income 48.53 -",
        "Employee benefits expense (42,458.56) -",
        "Occupancy expenses (6,356.00) -",
        "Other expenses (5,801.96) (12,112.00)",
        "Loss for the period (22,136.99) (7,912.00)",
    ):
        assert row in second
    assert "restated" not in " ".join(pages)  # 上期没有编制财报, 比较数是首次按准则编制, 不是重述 (R293)
    assert "The comparative figures are unaudited and include the opening bank balances" in first
    assert "STATEMENT OF CHANGES IN EQUITY" in equity and "FOR THE FINANCIAL YEAR ENDED 30 SEPTEMBER 2026" in equity
    for row in (
        "Balance at the beginning of the period (7,912.00) -",
        "Loss for the period (22,136.99) (7,912.00)",
        "Other comprehensive income - -",  # 第 61(a) 段
        "Total comprehensive loss for the period (22,136.99) (7,912.00)",
        "Balance at the end of the period (30,048.99) (7,912.00)",
    ):
        assert row in equity, row
    assert "no transactions with owners in their capacity as owners in either period" in equity
    assert "STATEMENT OF CASH FLOWS" in cash and "FOR THE FINANCIAL YEAR ENDED 30 SEPTEMBER 2026" in cash
    for heading in (  # 第 66 段的三类活动 (第 1 轮审核 R293-04)
        "Cash flows from operating activities Loss for the period",
        "Changes in operating assets and liabilities: Increase in trade receivables",
        "Cash flows from investing activities Net cash from investing activities",
        "Cash flows from financing activities Net cash from financing activities",
    ):
        assert heading in cash, heading
    for row in (
        "Loss for the period (22,136.99) (7,912.00)",
        "Increase in trade receivables (10,199.50) -",
        "Change in GST and PAYG withholding 9,593.17 (791.20)",
        "Net cash used in operating activities (9,828.22) (8,703.20)",
        "Net cash from investing activities - -",
        "Net cash from financing activities - -",
        "Net decrease in cash and cash equivalents (9,828.22) (8,703.20)",
        "Cash and cash equivalents at 1 July 10,509.01 19,212.21",
        "Cash and cash equivalents at end of period 680.79 10,509.01",
        "Cash at bank 11,581.23 15,081.23",  # 第 88 段的调节
        "Bank overdraft 3 (10,900.44) (4,572.22)",
        "Total 680.79 10,509.01",
    ):
        assert row in cash, row
    assert "repayable on demand and forms part of the Company's cash management" in cash  # 第 65 段
    assert "indirect method" in cash
    assert "1. Basis of preparation" in third and "the 3 months from 1 July 2026 to 30 September 2026" in third
    assert "did not present financial statements for previous periods" in third  # 第 212 段
    assert "the date of transition is 1 July 2025" in third
    assert "to meet the shareholders agreement of 15 June 2026" in third  # 第 16(b) 段: 缩短的原因
    assert "Material accounting policies:" in third and "for each on-site day as it is provided" in third  # 第 95 段
    assert "section 323D(2A)" in third and "have not been audited" in third and "(journal #397)" in third
    assert "2. Going concern" in third and "loss of AUD 22,136.99 for the financial year ended" in third
    assert "current liabilities exceeded its current assets by AUD 30,048.99" in third
    assert (  # ASA 570 第 19(b) 段的句式
        "there is a material uncertainty related to events or conditions that may cast significant doubt on the "
        "Company's ability to continue as a going concern and, therefore, that it may be unable to realise its "
        "assets and discharge its liabilities in the normal course of business"
    ) in third
    assert "up to AUD 40,000" in third and "overdraft facility of AUD 15,000" in third
    assert "3. Related parties" in fourth and "a further AUD 2,500.00 in October 2026" in fourth
    assert "personally guaranteed the Company's bank overdraft" in fourth
    assert "Victoria Property Agency Pty Ltd" in fourth and "rent of AUD 4,500.00 (excluding GST)" in fourth
    assert "AUD 1,500.00 per month on a month-to-month arrangement without a written lease" in fourth
    assert "4. Events after the reporting period" in fourth and "Harbourview Nominees Pty Ltd" in fourth
    assert "office furniture for AUD 6,300.00" in fourth and "a laptop for AUD 1,969.99" in fourth
    assert "the director lent the Company a further AUD 2,500.00" in fourth
    assert "authorised for issue by the sole director, Alex Morgan, on 23 October 2026" in fourth  # 第 186 段
    assert "5. Auditor's remuneration" in fourth and "AUD 4,800 (excluding GST)" in fourth  # 第 98 段
    assert "(year ended 30 June 2026: nil, as no financial report was prepared for that year)" in fourth  # 第 20 段
    assert "The auditor provided no other services in either period." in fourth
    assert "DIRECTOR'S DECLARATION" in fourth and "Alex Morgan, Director 23 October 2026" in fourth
    assert "pay its debts as and when they become due and payable" in fourth  # s295(4)(c)
    assert "in accordance with a resolution of the director" in fourth  # s295(5)(a)
    assert not any("AASB 1060" in p for p in pages)  # 财报正文不引准则编号与段号 (所依段号见定稿第 4.7 节)


def test_representation_letter() -> None:
    pages = pdf_texts(SUPPLEMENTS / "representation" / "representation_letter.pdf")
    assert len(pages) == 2
    text = " ".join(pages)
    assert pages[0].count("23 October 2026") >= 1 and "Tessellate Audit Partners" in pages[0]
    assert "for the financial year ended 30 September 2026" in text
    assert "terms of the audit engagement dated 28 August 2026" in text  # 委托书日期 (engagement_letter.pdf)
    for phrase in (  # 第 4.8 节列出的准则内容, 措辞取 ASA 580 Appendix 2 (主体名与编制框架按本公司替换)
        "in particular the financial report gives a true and fair view in accordance therewith",  # ASA 580 第 10, 12 段
        "(a) access to all information of which we are aware that is relevant to the preparation of the financial "
        "report such as records, documentation and other matters",  # ASA 580 第 11(a) 段, 委托书 (iii) a
        "(b) additional information that you have requested from us for the purpose of the audit",  # (iii) b
        "(c) unrestricted access to persons within the Company from whom you determined it necessary to obtain "
        "audit evidence",  # (iii) c
        "All transactions have been recorded in the accounting records and are reflected in the financial report",
        "We acknowledge our responsibility for the design, implementation and maintenance of internal control to "
        "prevent and detect fraud",  # ASA 240 第 40(a) 段
        "the results of our assessment of the risk that the financial report may be materially misstated as a "
        "result of fraud",  # 40(b)
        "all information in relation to fraud or suspected fraud that we are aware of and that affects the Company "
        "and involves: (a) management; (b) employees who have significant roles in internal control; or (c) others "
        "where the fraud could have a material effect on the financial report",  # 40(c)
        "allegations of fraud, or suspected fraud, affecting the Company's financial report communicated by "
        "employees, former employees, analysts, regulators or others",  # 40(d)
        "all known instances of non-compliance or suspected non-compliance with laws and regulations whose "
        "effects should be considered when preparing the financial report",  # ASA 250 第 17 段
        "the identity of the Company's related parties and all the related party relationships and transactions "
        "of which we are aware",  # ASA 550 第 26(a) 段
        "Related party relationships and transactions have been appropriately accounted for and disclosed",  # 26(b)
        "All events subsequent to the date of the financial report and for which Australian Accounting Standards "
        "- Simplified Disclosures require adjustment or disclosure have been adjusted or disclosed",  # ASA 560 第 9 段
        "plans for future actions",  # ASA 570 第 16(e) 段
    ):
        assert phrase in text, phrase
    assert "All misstatements communicated have been corrected; there are no uncorrected misstatements" in text
    assert "Schedule of uncorrected misstatements: none." in text and "Alex Morgan, Director" in pages[1]
    docs = manifest_documents()
    assert docs["representation/representation_letter.pdf"][:5] == (
        "representation_letter",
        "internal_original",
        "2026-10-23",
        "manual",
        2,
    )


def test_r68_index_rows_and_counts() -> None:
    rows = (SUPPLEMENTS / "documents" / "index.csv").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 20 and rows[19] == (
        "financial_statements/fs_v1.pdf,financial_statements,internal_original,2026-10-22,FS"
    )
    assert not any(r.startswith("representation/") for r in rows)  # 声明书经上传接口登记, 不进 index.csv
    docs = manifest_documents()
    assert docs[generator.FS_PDF][:5] == ("financial_statements", "internal_original", "2026-10-22", "statements", 6)
    manifest = read("manifest.json")
    assert checker.check_document_counts(SUPPLEMENTS, manifest) == []  # type: ignore[arg-type]
    registrations = [d[3] for d in docs.values()]
    assert len(docs) == 29 and sum(int(d[4]) for d in docs.values()) == 57  # type: ignore[call-overload]
    assert {r: registrations.count(r) for r in set(registrations)} == {
        "acceptance": 2,
        "fieldwork": 19,
        "statements": 1,
        "manual": 1,
        "registry": 4,
        "confirmation": 2,
    }
    fewer = {**manifest, "documents": manifest["documents"][1:]}  # type: ignore[index]
    assert len(checker.check_document_counts(SUPPLEMENTS, fewer)) == 1
    assert "documents 段的份数, 页数或登记途径与第 9.5 节不符" in checker.check_document_counts(SUPPLEMENTS, fewer)[0]


def test_manual_submissions() -> None:
    first, second = read("manual/fin08_round1.json"), read("manual/fin08_round2.json")
    assert (
        list(first)
        == list(second)
        == [  # type: ignore[arg-type]
            "procedure_id",
            "round",
            "communicated_at",
            "recipient",
            "form",
            "misstatement_sources",
            "responded_at",
            "response",
            "adjustment_entry_nos",
        ]
    )
    assert {k: v for k, v in first.items() if k != "response"} == {  # type: ignore[union-attr]
        "procedure_id": "FIN-08",
        "round": 1,
        "communicated_at": "2026-10-19T04:00:00Z",
        "recipient": "Alex Morgan, Director",
        "form": "written",
        "misstatement_sources": ["J-GEN-02-1-XJ-499", "J-CSH-02-1-BA-091"],
        "responded_at": "2026-10-19T22:00:00Z",
        "adjustment_entry_nos": ["AJE-X01", "AJE-X05"],
    }
    assert {k: v for k, v in second.items() if k != "response"} == {  # type: ignore[union-attr]
        "procedure_id": "FIN-08",
        "round": 2,
        "communicated_at": "2026-10-21T04:00:00Z",
        "recipient": "Alex Morgan, Director",
        "form": "written",
        "misstatement_sources": ["J-FIN-15-1-826", "J-FIN-15-1-804", "J-FIN-15-1-805"],
        "responded_at": "2026-10-21T22:00:00Z",
        "adjustment_entry_nos": ["AJE-X02", "AJE-X03", "AJE-X04"],
    }
    quoted = re.findall(r'第 \d 轮回复 \(10-\d\d\)\*\*: "([^"]+)"', section(doc(), "### 4.8"))
    assert [first["response"], second["response"]] == quoted  # type: ignore[index]  # 第 4.8 节原文
    assert "AJE-X05" in first["response"] and "48.53" in first["response"]  # type: ignore[index]
    assert read("manual/representation.json") == {
        "procedure_id": "FIN-07",
        "status": "obtained",
        "dated_at": "2026-10-23",
        "signed_by": ["Alex Morgan"],
        "period_end": "2026-09-30",
        "document": "representation/representation_letter.pdf",
        "standards": [
            "ASA580-P10",
            "ASA580-P11",
            "ASA560-P09",
            "ASA570-P16",
            "ASA550-P26",
            "ASA240-P40",
            "ASA250-P17",
        ],
        "uncorrected_schedule": [],
    }
    reply = read("manual/rpt07_reply.json")
    assert {k: v for k, v in reply.items() if k != "response"} == {  # type: ignore[union-attr]
        "procedure_id": "RPT-07",
        "communicated_on": "2026-10-22",
        "form": "written",
        "recipients": ["Alex Morgan"],
        "matters": ["SIG_PRACTICES", "DIFFICULTIES", "REPORT_FORM", "INDEPENDENCE"],
        "corrected_misstatements": 5,
        "uncorrected_misstatements": 0,
        "responded_on": "2026-10-22",
    }
    assert reply["response"] == spec()["manual"]["rpt07_reply.json"]["response"]  # type: ignore[index]
    assert "material uncertainty related to going concern" in reply["response"]  # type: ignore[index]


def fs_copy(tmp_path: Path) -> tuple[Path, Path, dict]:
    copy = copy_supplements(tmp_path)
    path = copy / generator.FS_JSON
    return copy, path, json.loads(path.read_text(encoding="utf-8"))


def test_financial_statement_problems(tmp_path: Path) -> None:
    copy, path, fs = fs_copy(tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    assert checker.check_financial_statements(copy, manifest) == []
    where, pdf = path, copy / generator.FS_PDF
    fs["lines"][0]["amount_cents"] += 1  # SOFP-CASH; 资产合计未改, 所以平衡式仍成立, 由小计报出
    path.write_text(json.dumps(fs), encoding="utf-8")
    assert checker.check_financial_statements(copy, manifest) == [
        f"{where}: 与规格 financial_statements 段不一致",
        f"{where}: 小计 SOFP-TA 的 amount_cents 不等于所含各行之和",
        f"{where}: CF-CLOSE 的 amount_cents 不等于 SOFP-CASH 减 SOFP-OD",  # 现金流量表期末与财务状况表对不上
        f"{pdf}: 第 1 页文本层没有 SOFP-CASH 的名称与本期, 比较数",
        f"{pdf}: 第 4 页文本层没有调节行 Cash at bank",
    ]
    fs["lines"][0]["amount_cents"] -= 1
    fs["lines"][6]["account_codes"].append("091")  # SOFP-PAY 也映射 091
    fs["lines"][17]["code"] = "PL-REV"  # 行代码重复
    fs["document_id"] = "DOC-000000000000"
    path.write_text(json.dumps(fs), encoding="utf-8")
    assert checker.check_financial_statements(copy, manifest) == [
        f"{where}: 与规格 financial_statements 段不一致",
        f"{where}: 行代码重复",
        f"{where}: document_id 不是 {generator.FS_PDF} 的文件 ID",
        f"{where}: financial_position 的非小计行之间科目重复",
        f"{where}: 小计 SOFP-TL 的科目不等于所含各行的科目",  # SOFP-PAY 的科目不再是负债合计的子集
        f"{where}: 小计 SOFP-TL 的 amount_cents 不等于所含各行之和",
        f"{where}: 缺行 PL-NP",
    ]
    fs["lines"] = fs["lines"][:5]
    path.write_text(json.dumps({**fs, "extra": 1}), encoding="utf-8")
    assert checker.check_financial_statements(copy, manifest) == [f"{where}: 不符合 FinancialStatements (1 处)"]
    path.unlink()
    assert checker.check_financial_statements(copy, manifest) == [f"{where}: 文件不存在"]


def test_financial_statement_pages_swapped(tmp_path: Path) -> None:
    """财务状况表与损益表互换页码, 或权益变动表与现金流量表互换页码时报出 (各行只在该表所在页找)."""
    copy, _, fs = fs_copy(tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    pages = ["\n".join(p) for p in next(d for d in spec()["documents"] if d["path"] == generator.FS_PDF)["pages"]]
    pdf = copy / generator.FS_PDF
    generator.write_pdf(pdf, [pages[1], pages[0], *pages[2:]], "x", HEADER)
    got = checker.check_financial_statements(copy, manifest)
    assert got == [  # 权益变动表与现金流量表在第 3, 4 页, 不受影响
        f"{pdf}: 第 {checker.FS_PAGES[x['statement']]} 页文本层没有 {x['code']} 的名称与本期, 比较数"
        for x in fs["lines"]
        if x["statement"] in checker.TB_STATEMENTS
    ]
    generator.write_pdf(pdf, [*pages[:2], pages[3], pages[2], *pages[4:]], "x", HEADER)
    got = checker.check_financial_statements(copy, manifest)
    assert got == [
        *(
            f"{pdf}: 第 {checker.FS_PAGES[x['statement']]} 页文本层没有 {x['code']} 的名称与本期, 比较数"
            for x in fs["lines"]  # 两张表都有同名同数的 "Loss for the period" 行 (CE-NP, CF-NP), 换页也找得到
            if x["statement"] not in checker.TB_STATEMENTS and x["code"] not in ("CE-NP", "CF-NP")
        ),
        f"{pdf}: 第 4 页文本层没有调节行 Cash at bank",
        f"{pdf}: 第 4 页文本层没有调节行 Bank overdraft",
        f"{pdf}: 第 4 页文本层没有调节行 Total",
    ]
    generator.write_pdf(pdf, pages[:3], "x", HEADER)
    assert checker.check_financial_statements(copy, manifest) == [f"{pdf}: 文件不存在, 无法解析或不足 4 页"]


def test_financial_statement_totals_problems(tmp_path: Path) -> None:
    """规格本身算错时 (小计, 资产负债平衡, 累计亏损结转) 也能报出: 用改过的规格比对."""
    copy, path, fs = fs_copy(tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    lines = {x["code"]: x for x in fs["lines"]}
    lines["SOFP-EQ"]["prior_amount_cents"] = -791100  # 期初累计亏损错 1 分
    lines["SOFP-TL"]["account_codes"].remove("801")  # 小计少一个科目
    lines["PL-NP"]["account_codes"].append("960")
    path.write_text(json.dumps(fs), encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        changed = {**generator.load_spec(), "financial_statements": {k: v for k, v in fs.items() if k != "document_id"}}
        patch.setattr(generator, "load_spec", lambda: changed)
        got = checker.check_financial_statements(copy, manifest)
    where = path
    assert got == [
        f"{where}: 小计 SOFP-TL 的科目不等于所含各行的科目",
        f"{where}: 小计 SOFP-TL 的 amount_cents 不等于所含各行之和",  # SOFP-PAY 不再计入 (其比较数为 0)
        f"{where}: 小计 PL-NP 的科目不等于所含各行的科目",
        f"{where}: prior_amount_cents 资产 1508123 不等于负债 2299323 加权益 -791100",
        f"{where}: 期初 SOFP-EQ 加本期 PL-NP 不等于期末 SOFP-EQ",
        f"{where}: CE-CLOSE 的 prior_amount_cents 不等于 SOFP-EQ",  # 权益变动表的上期末随之对不上
        f"{copy / generator.FS_PDF}: 第 1 页文本层没有 SOFP-EQ 的名称与本期, 比较数",
    ]


def fs_items(**changes: dict) -> list:
    """fs_v1.json 的各行 (FinancialStatementLine), 按行代码改字段; 值为 None 的行代码删去该行."""
    items = []
    for line in read(generator.FS_JSON)["lines"]:  # type: ignore[index]
        update = changes.get(line["code"].replace("-", "_"), {})
        if update is not None:
            items.append(checker.FinancialStatementLine.model_validate({**line, **update}))
    return items


def test_equity_and_cash_flow_problems() -> None:
    """权益变动表与现金流量表 (R293) 与财务状况表, 损益表对不上时报出; 正式目录的行全部一致 (10 组变异)."""
    check = checker.equity_and_cash_flow_problems
    assert check(fs_items()) == []
    assert check(fs_items(CF_REC={"amount_cents": -1019951})) == [
        "CF-REC 不等于 SOFP-REC 的变动对现金的影响",
        "CF-OP 的 amount_cents 不等于经营各行之和",
    ]
    assert check(fs_items(CF_EMP=None)) == [  # 漏了员工薪酬负债的变动
        "现金流量表没有 SOFP-EMP 的变动",
        "CF-OP 的 amount_cents 不等于经营各行之和",
    ]
    assert check(fs_items(CF_PAY={"account_codes": ["800", "860"]})) == [
        "CF-PAY 在财务状况表中没有科目相同的一行",
        "现金流量表没有 SOFP-PAY 的变动",
    ]
    assert check(fs_items(CE_NP={"prior_amount_cents": -791100})) == [
        "CE-TCI 的 prior_amount_cents 不等于 CE-NP 加 CE-OCI",
        "CE-NP 或 CF-NP 的 prior_amount_cents 不等于 PL-NP",
    ]
    assert check(fs_items(CE_OCI={"amount_cents": 100})) == [  # 损益表没有其他综合收益, 权益变动表也只能为 0
        "CE-TCI 的 amount_cents 不等于 CE-NP 加 CE-OCI",
        "CE-OCI 的 amount_cents 应为 0 (损益表没有其他综合收益)",
    ]
    assert check(fs_items(CE_TCI={"amount_cents": -2213698})) == [
        "CE-TCI 的 amount_cents 不等于 CE-NP 加 CE-OCI",
        "CE-CLOSE 的 amount_cents 不等于 CE-OPEN 加 CE-TCI",
    ]
    assert check(fs_items(CE_OPEN={"amount_cents": 0}, CF_OPEN={"amount_cents": 1050900})) == [
        "CE-CLOSE 的 amount_cents 不等于 CE-OPEN 加 CE-TCI",
        "CE-OPEN 的本期数不等于上期的 CE-CLOSE",
        "CF-CLOSE 的 amount_cents 不等于 CF-OPEN 加 CF-NET",
        "CF-OPEN 的本期数不等于上期的 CF-CLOSE",
    ]
    assert check(fs_items(CF_NET={"prior_amount_cents": 0}, CF_CLOSE={"amount_cents": 68078})) == [
        "CF-CLOSE 的 amount_cents 不等于 CF-OPEN 加 CF-NET",
        "CF-CLOSE 的 amount_cents 不等于 SOFP-CASH 减 SOFP-OD",
        "CF-NET 的 prior_amount_cents 不等于经营, 投资与筹资之和",
        "CF-CLOSE 的 prior_amount_cents 不等于 CF-OPEN 加 CF-NET",
    ]
    assert check(fs_items(CE_OPEN=None, CF_FIN=None)) == ["缺行 CE-OPEN, CF-FIN"]
    assert check(fs_items(PL_NP=None)) == []  # 缺合计行由 check_financial_statements 报出, 不重复


@needs_snapshot
def test_financial_statements_tie_to_trial_balance(tmp_path: Path) -> None:
    from collections import Counter

    from backend.adapters.xero_dataset import trial_balance_cents

    snapshot = generator.Snapshot(SNAPSHOT)
    assert checker.snapshot_financial_statements(SUPPLEMENTS, snapshot) == []
    tb = Counter(trial_balance_cents(snapshot.load("trial_balance_2026-09-30")))
    assert len(tb) == 27 and tb["801"] == 0
    for row in checker.adjustment_entries(SUPPLEMENTS) or []:
        tb[row.account_code] += row.debit_cents - row.credit_cents
    assert len(tb) == 33  # 27 行加 804, 805, 826, 478, 620, 270
    for code, _, _, codes, sign, _, cents, _ in TB_LINES:
        assert cents == sign * sum(tb[c] for c in codes), code
    copy, path, fs = fs_copy(tmp_path)
    where = path
    lines = {x["code"]: x for x in fs["lines"]}
    lines["SOFP-PAY"]["account_codes"].remove("801")  # 801 余额为 0, 金额不变, 只少了映射
    lines["PL-OTH"]["account_codes"].append("960")
    lines["SOFP-EQ"]["account_codes"].remove("960")
    fs["comparative_restatement_entry_nos"] = []
    path.write_text(json.dumps(fs), encoding="utf-8")
    assert checker.snapshot_financial_statements(copy, snapshot) == [
        f"{where}: SOFP-CASH 的比较数不等于上期末试算表加重述分录",
        f"{where}: SOFP-TA 的比较数不等于上期末试算表加重述分录",
        f"{where}: SOFP-OD 的比较数不等于上期末试算表加重述分录",
        f"{where}: SOFP-DLOAN 的比较数不等于上期末试算表加重述分录",
        f"{where}: SOFP-TL 的比较数不等于上期末试算表加重述分录",
        f"{where}: SOFP-EQ 不等于期末试算表加更正分录",
        f"{where}: PL-OTH 不等于期末试算表加更正分录",
        f"{where}: 映射的科目应恰为期末试算表与更正分录的 33 个 (缺 ['801'], 多 [])",
        f"{where}: PL-OTH 的科目 960 类型与所在报表不符",
        # 没有重述分录时过渡日余额全为 0: 董事借款与现金及现金等价物在比较期内 "凭空" 出现
        f"{where}: CF-FIN 的比较数不等于 SOFP-DLOAN 自过渡日起的变动对现金的影响",
        f"{where}: CF-OPEN 的比较数不等于过渡日的 SOFP-CASH 减 SOFP-OD",
    ]
    fs["comparative_restatement_entry_nos"] = ["XJ-9999"]
    path.write_text(json.dumps(fs), encoding="utf-8")
    assert f"{where}: comparative_restatement_entry_nos 在快照中找不到" in checker.snapshot_financial_statements(
        copy, snapshot
    )


def manual_copy(tmp_path: Path) -> tuple[Path, dict]:
    copy = copy_supplements(tmp_path)
    data = {n: json.loads((copy / "manual" / n).read_text(encoding="utf-8")) for n in checker.MANUAL_FILES}
    return copy, data


def write_manual(copy: Path, data: dict) -> None:
    for name, value in data.items():
        (copy / "manual" / name).write_text(json.dumps(value), encoding="utf-8")


def test_manual_problems(tmp_path: Path) -> None:
    copy, data = manual_copy(tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    assert checker.check_manual(copy, manifest) == []
    folder = copy / "manual"
    data["fin08_round1.json"]["note"] = "x"  # 两轮键不同
    data["fin08_round2.json"]["responded_at"] = "2026-10-22T00:00:00Z"  # 晚于 AJE-X02 至 X04 的录入时间
    data["fin08_round2.json"]["adjustment_entry_nos"] = ["AJE-X02", "AJE-X03", "AJE-X09"]  # 对象也随之对不上
    data["representation.json"]["signed_by"] = ["Someone Else"]
    data["representation.json"]["standards"] = ["ASA 580 para 10"]
    data["rpt07_reply.json"]["corrected_misstatements"] = 4
    write_manual(copy, data)
    assert checker.check_manual(copy, manifest) == [
        *(f"{folder / n}: 与规格 manual 段不一致" for n in checker.MANUAL_FILES),
        f"{folder}: fin08_round1.json 与 fin08_round2.json 的键不同",
        f"{folder / 'fin08_round2.json'}: misstatement_sources 的对象应与所涉更正分录的 source_doc 一一对应",
        f"{folder / 'fin08_round2.json'}: responded_at 应早于所涉更正分录的录入时间",
        f"{folder / 'fin08_round2.json'}: adjustment_entry_nos 不在更正分录中: AJE-X09",
        f"{folder}: 两轮的 adjustment_entry_nos 应恰好覆盖全部 5 笔更正分录",
        f"{folder / 'representation.json'}: signed_by 不在声明书文本层",
        f"{folder / 'representation.json'}: standards 应为非空的准则段落 ID 列表 (如 ASA580-P10)",
        f"{folder / 'rpt07_reply.json'}: corrected_misstatements 应等于更正分录笔数 5",
    ]
    copy, data = manual_copy(tmp_path / "again")
    folder = copy / "manual"
    data["fin08_round1.json"]["communicated_at"] = "2026-10-19 04:00"  # 不是 UTC 写法
    data["fin08_round1.json"]["misstatement_sources"].reverse()  # 顺序不同仍一一对应, 不报
    data["fin08_round2.json"]["round"] = 3
    data["fin08_round2.json"]["communicated_at"] = "2026-10-22T04:00:00Z"  # 晚于回复
    data["representation.json"]["dated_at"] = "2026-10-24"
    data["representation.json"]["uncorrected_schedule"] = [{"id": "M-9"}]
    data["rpt07_reply.json"]["responded_on"] = "2026-10-21"
    write_manual(copy, data)
    assert checker.check_manual(copy, manifest)[len(checker.MANUAL_FILES) :] == [
        f"{folder / 'fin08_round1.json'}: procedure_id 应为 FIN-08, communicated_at 与 responded_at 应为 UTC 时刻",
        f"{folder / 'fin08_round2.json'}: round 应为 2",
        f"{folder / 'fin08_round2.json'}: responded_at 应晚于 communicated_at",
        f"{folder / 'representation.json'}: dated_at 与声明书的 document_date 不一致",
        f"{folder / 'representation.json'}: uncorrected_schedule 应为空 (全部错报已更正)",
        f"{folder / 'rpt07_reply.json'}: uncorrected_misstatements 应等于声明书附表行数",
        f"{folder / 'rpt07_reply.json'}: responded_on 不应早于 communicated_on",
    ]
    (folder / "extra.json").write_text("{}", encoding="utf-8")
    assert checker.check_manual(copy, manifest) == [f"{folder}: 应恰为 {', '.join(checker.MANUAL_FILES)}"]
    other = {**manifest, "documents": [d for d in manifest["documents"] if not d["path"].startswith("representation/")]}
    (folder / "extra.json").unlink()
    assert f"{folder / 'representation.json'}: document 不是 manifest 中类型 representation_letter 的文件" in (
        checker.check_manual(copy, other)
    )


def test_index_fs_reference(tmp_path: Path) -> None:
    copy, path, fs = fs_copy(tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    assert checker.check_index(copy, manifest) == []
    path.write_text(json.dumps({**fs, "id": "FS-2"}), encoding="utf-8")
    where = copy / "documents" / "index.csv"
    assert checker.check_index(copy, manifest) == [
        f"{where}: {generator.FS_PDF} 的关联对象 FS 不是 {generator.FS_JSON} 的 id"
    ]


def test_manual_rounds_order_and_sources(tmp_path: Path) -> None:
    copy, data = manual_copy(tmp_path)
    manifest = json.loads((copy / "manifest.json").read_text(encoding="utf-8"))
    folder = copy / "manual"
    first, second = data["fin08_round1.json"], data["fin08_round2.json"]
    first["misstatement_sources"], second["misstatement_sources"] = (
        second["misstatement_sources"],
        first["misstatement_sources"],
    )  # 两轮的错报来源互换
    first["responded_at"] = "2026-10-21T05:00:00Z"  # 晚于第 2 轮沟通 (仍早于第 2 轮的更正录入)
    first["adjustment_entry_nos"], second["adjustment_entry_nos"] = (
        ["AJE-X02"],
        ["AJE-X01", "AJE-X03", "AJE-X04", "AJE-X05"],
    )
    write_manual(copy, data)
    assert checker.check_manual(copy, manifest)[2:] == [
        f"{folder / 'fin08_round1.json'}: misstatement_sources 的对象应与所涉更正分录的 source_doc 一一对应",
        f"{folder / 'fin08_round2.json'}: misstatement_sources 的对象应与所涉更正分录的 source_doc 一一对应",
        f"{folder / 'fin08_round2.json'}: responded_at 应早于所涉更正分录的录入时间",
        f"{folder}: 第 1 轮的回复应早于第 2 轮的沟通",
    ]


MOCK = SNAPSHOT.parent / "20261017-mock"


def test_snapshot_key_constants() -> None:
    """快照关键数的常量照需求清单 R69 验收 1 (复算由下面需要快照的用例做)."""
    assert checker.JOURNAL_COUNTS == {
        "分录笔数与行数": (368, 1017),
        "本期分录笔数与行数": (215, 582),
        "作废对 (全部, 本期)": (65, 33),
    }
    assert checker.TRIAL_BALANCE_ROWS == 27 and checker.TRIAL_BALANCE_BALANCES["801"] == 0
    assert checker.PROFIT_BEFORE_ADJUSTMENTS == -1322696 and checker.CURRENT_TOTALS == (2178073, 4284469)
    assert checker.STATEMENT_KEYS == {
        "BA-090": (142, 72, "BT-090-0072", -1090044),
        "BA-091": (4, 1, "BT-091-0001", 1158123),
    }
    assert checker.MONTHLY_REVENUE == [460000, 1803600, 979500] and checker.MATERIALITY == (32431, 16216, 1622)
    assert (
        len(checker.LATE_ENTRIES) == 15 and checker.LATE_ENTRIES[0] == "XJ-434" and checker.LATE_ENTRIES[-1] == "XJ-755"
    )


@needs_snapshot
def test_snapshot_key_numbers() -> None:
    assert checker.check_snapshot_keys(SNAPSHOT, SUPPLEMENTS) == []
    if not MOCK.is_dir():
        pytest.skip("没有 20261017-mock")
    assert checker.compare_with_mock(SNAPSHOT, MOCK) == []


def snapshot_copies(tmp_path: Path) -> tuple[Path, Path]:
    """快照与 20261017-mock 复制到 tmp (篡改只在副本上做, 不动原目录)."""
    api = tmp_path / "api"
    shutil.copytree(SNAPSHOT, api / SNAPSHOT.name)
    if MOCK.is_dir():
        shutil.copytree(MOCK, api / MOCK.name)
    return api / SNAPSHOT.name, api / MOCK.name


@needs_snapshot
def test_tampered_journal_is_reported(tmp_path: Path) -> None:
    """journals.json 中一行 NetAmount 改 1 分: 关键数复算, 快照哈希与新旧快照比对都报出 journals.json."""
    before = generator.sha256(SNAPSHOT / "journals.json")
    snapshot, mock = snapshot_copies(tmp_path)
    journals = json.loads((snapshot / "journals.json").read_text(encoding="utf-8"))
    line = journals[0]["JournalLines"][0]
    assert (journals[0]["JournalNumber"], line["AccountCode"]) == (389, "800")
    line["NetAmount"] = round(line["NetAmount"] + 0.01, 2)
    (snapshot / "journals.json").write_text(json.dumps(journals), encoding="utf-8")
    assert checker.check_snapshot_keys(snapshot, SUPPLEMENTS) == [
        f"{snapshot / 'journals.json'}: 快照关键数与设计不符: 每笔借贷平衡"
    ]
    manifest = read("manifest.json")
    assert any("journals.json" in x for x in checker.check_snapshot(SUPPLEMENTS, manifest, snapshot))  # type: ignore[arg-type]
    message = check_error(SUPPLEMENTS, snapshot, scan=[])
    assert f"{snapshot / 'journals.json'}: 快照关键数与设计不符: 每笔借贷平衡" in message.splitlines()
    if mock.is_dir():
        assert f"{snapshot}: 快照与 20261017-mock 不一致 (Demo 可能已重置): journals.json" in message.splitlines()
    assert generator.sha256(SNAPSHOT / "journals.json") == before  # 原目录未动


@needs_snapshot
def test_more_snapshot_key_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot, _ = snapshot_copies(tmp_path)
    path = snapshot / "trial_balance_2026-09-30.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"11581.23"', '"11581.24"'), encoding="utf-8")
    copy = copy_supplements(tmp_path)
    edit_json(copy / "dataset_traits.json", lambda t: t.update(entry_timestamps_note="16 journals carry created times"))
    assert checker.check_snapshot_keys(snapshot, copy) == [
        f"{path}: 快照关键数与设计不符: 试算表合计, 主要余额 091, 流动资产与流动负债",
        f"{copy / 'dataset_traits.json'}: entry_timestamps_note 与快照的录入时间不符",
    ]
    monkeypatch.setattr(checker, "MONTHLY_REVENUE", [460000, 1803600, 979501])  # 其余分支: 期望值改掉一项即报出
    monkeypatch.setattr(checker, "LATE_ENTRIES", checker.LATE_ENTRIES[1:])
    monkeypatch.setattr(checker, "STATEMENT_KEYS", {**checker.STATEMENT_KEYS, "BA-091": (4, 2, "BT-091-0001", 1158123)})
    monkeypatch.setattr(checker, "JOURNAL_COUNTS", {**checker.JOURNAL_COUNTS, "作废对 (全部, 本期)": (65, 32)})
    monkeypatch.setattr(checker, "MATERIALITY", (32431, 16215, 1622))
    assert checker.check_snapshot_keys(SNAPSHOT, SUPPLEMENTS) == [
        f"{checker.rel(SNAPSHOT / 'journals.json')}: 快照关键数与设计不符: 作废对 (全部, 本期), 月度收入, "
        "录入日晚于 2026-10-19 的分录",
        f"{checker.rel(SNAPSHOT / 'trial_balance_2026-09-30.json')}: 快照关键数与设计不符: 重要性",
        f"{checker.rel(SNAPSHOT / 'bank_statements.json')}: 快照关键数与设计不符: 对账单 BA-091",
    ]


@needs_snapshot
def test_mock_comparison(tmp_path: Path) -> None:
    snapshot, mock = snapshot_copies(tmp_path)
    if not mock.is_dir():
        pytest.skip("没有 20261017-mock")
    prior = mock / "trial_balance_2026-06-30.json"
    prior.write_text(json.dumps(json.loads(prior.read_text(encoding="utf-8")), indent=4), encoding="utf-8")
    (mock / "dataset.json").write_text("{}", encoding="utf-8")  # 期间声明不比
    assert checker.compare_with_mock(snapshot, mock) == []  # 试算表按规范化比较, 排版不同不报
    prior.write_text(prior.read_text(encoding="utf-8").replace('"4200.00"', '"4200.01"'), encoding="utf-8")
    (mock / "contacts.json").write_bytes((mock / "contacts.json").read_bytes() + b" ")  # 只比哈希
    (mock / "payments.json").unlink()
    assert checker.compare_with_mock(snapshot, mock) == [
        f"{snapshot}: 快照与 20261017-mock 不一致 (Demo 可能已重置): contacts.json, payments.json, "
        "trial_balance_2026-06-30.json"
    ]
    shutil.rmtree(mock)
    notes = checker.check(copy_supplements(tmp_path), snapshot, determinism=False, scan=[])
    assert f"没有 20261017-mock ({mock}): 跳过新旧快照比对" in notes


@needs_snapshot
def test_snapshot_key_steps_are_independent(tmp_path: Path) -> None:
    """某个文件出问题或某步意外中断, 只报该文件, 其余各项照常报出."""
    snapshot, mock = snapshot_copies(tmp_path)
    journals = json.loads((snapshot / "journals.json").read_text(encoding="utf-8"))
    del journals[0]["JournalLines"][0]["NetAmount"]  # 关键数这一步中断
    (snapshot / "journals.json").write_text(json.dumps(journals), encoding="utf-8")
    path = snapshot / "trial_balance_2026-09-30.json"
    path.write_text(path.read_text(encoding="utf-8").replace('"11581.23"', '"11581.24"'), encoding="utf-8")
    assert checker.check_snapshot_keys(snapshot, SUPPLEMENTS) == [
        f"{snapshot / 'journals.json'}: 校验中断 (KeyError)",
        f"{path}: 快照关键数与设计不符: 试算表合计, 主要余额 091, 流动资产与流动负债",
    ]
    path.unlink()
    assert f"{path}: 快照缺期末试算表" in checker.check_snapshot_keys(snapshot, SUPPLEMENTS)
    if mock.is_dir():
        (mock / "payments.json").unlink()
        (snapshot / "dataset.json").write_text('{"period_start": "2026-07-01", "period_end": "2026-09-30"}')
        assert checker.compare_with_mock(snapshot, mock) == [  # 缺 prior_end 也不丢已得出的文件名
            f"{snapshot}: 快照与 20261017-mock 不一致 (Demo 可能已重置): journals.json, payments.json, 上期末试算表"
        ]


@needs_snapshot
def test_snapshot_is_the_mock(tmp_path: Path) -> None:
    snapshot, mock = snapshot_copies(tmp_path)
    if mock.is_dir():
        shutil.rmtree(mock)
    mock.symlink_to(snapshot, target_is_directory=True)
    copy = copy_supplements(tmp_path)
    assert checker.check(copy, snapshot, determinism=False, scan=[]) == [
        f"不是正式补造目录 ({copy}): 跳过 documents 段份数, 页数与登记途径的计数",
        "快照即 20261017-mock: 跳过新旧快照比对",
    ]


def test_write_llm_fixtures_copies_clears_and_rejects_links(tmp_path: Path) -> None:
    """R78: 运行副本 llm/ 先清空再原样复制正本的 <程序>/<对象>.json, 另写 README; 正本删掉的文件不留副本; 正本根目录,
    程序目录或文件是符号链接时拒绝, 且在清空之前拒绝 (llm/ 保持原样); 正本不存在时只写 README."""
    source, out, uploads = tmp_path / "llm_xero", tmp_path / "out", tmp_path / "upload"
    (source / "CSH-02").mkdir(parents=True)
    (source / "CSH-02" / "BA-01.json").write_bytes(b'{"a": 1}\n')
    (source / "notes.md").write_text("not copied", encoding="utf-8")
    (out / "llm" / "OLD").mkdir(parents=True)
    (out / "llm" / "OLD" / "stale.json").write_text("{}", encoding="utf-8")
    for program in ("DOC-SHEET", "DOC-CLASSIFY"):  # 上传固定输出只复制 programs 列出的
        (uploads / program).mkdir(parents=True)
        (uploads / program / "tb.json").write_bytes(b'{"b": 2}\n')
    generator.write_llm_fixtures(out, source, uploads)
    copied = sorted(p.relative_to(out / "llm").as_posix() for p in (out / "llm").rglob("*") if p.is_file())
    assert copied == ["CSH-02/BA-01.json", "DOC-SHEET/tb.json", "README.md"]
    assert (out / "llm" / "DOC-SHEET" / "tb.json").read_bytes() == b'{"b": 2}\n'
    missing = tmp_path / "missing"
    generator.write_llm_fixtures(out, source, missing)
    copied = sorted(p.relative_to(out / "llm").as_posix() for p in (out / "llm").rglob("*") if p.is_file())
    assert copied == ["CSH-02/BA-01.json", "README.md"]
    assert (out / "llm" / "CSH-02" / "BA-01.json").read_bytes() == b'{"a": 1}\n'
    assert "Synthetic – for demonstration only" in (out / "llm" / "README.md").read_text(encoding="utf-8")

    (source / "CSH-02" / "LINK.json").symlink_to(source / "CSH-02" / "BA-01.json")
    with pytest.raises(SystemExit, match=r"CSH-02/LINK\.json"):
        generator.write_llm_fixtures(out, source, missing)
    (source / "CSH-02" / "LINK.json").unlink()
    (source / "REV-02").symlink_to(source / "CSH-02", target_is_directory=True)  # 程序目录是符号链接
    with pytest.raises(SystemExit, match=r"REV-02/BA-01\.json"):
        generator.write_llm_fixtures(out, source)
    (source / "REV-02").unlink()
    linked = tmp_path / "linked"
    linked.symlink_to(source)
    with pytest.raises(SystemExit, match="符号链接"):
        generator.write_llm_fixtures(out, linked)
    assert (out / "llm" / "CSH-02" / "BA-01.json").exists()  # 被拒时 llm/ 保持原样

    generator.write_llm_fixtures(out, tmp_path / "missing", tmp_path / "missing")
    assert [p.name for p in (out / "llm").rglob("*") if p.is_file()] == ["README.md"]
