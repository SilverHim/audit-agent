import json
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.api.schemas import GateDecisionIn
from backend.contracts.errors import VersionConflict
from backend.contracts.llm_outputs import AuditPlanDraft, EngagementTermsExtraction
from backend.contracts.models import (
    AsicExtract,
    Assertion,
    AuditException,
    AuditPlan,
    AutonomyLevel,
    CheckItem,
    CheckItemStatus,
    ClauseStatus,
    ClientProfile,
    CommunicationForm,
    CommunicationMatter,
    ComplianceCheck,
    Confirmation,
    Contravention,
    CriticTrigger,
    Decision,
    DeclarationForm,
    Direction,
    DocumentRef,
    DocumentType,
    EngagementTerms,
    EvidencePointer,
    EvidenceType,
    FinancialStatementLine,
    FinancialStatements,
    GateDecision,
    GateId,
    GateVerdict,
    GovernanceCommunication,
    IndependenceDeclaration,
    Judgment,
    MisstatementDraft,
    MisstatementType,
    ObjectRef,
    OwnershipGraph,
    PartyLink,
    PartyNode,
    Phase,
    PlanConfirmation,
    PlanStatus,
    Procedure,
    ProcedureStatus,
    ProposedAction,
    Reliability,
    ReportStatus,
    RepresentationClause,
    RepresentationStatus,
    ReviewRecord,
    ReviewStatus,
    TaskKind,
    TermsCheckStatus,
    TermsItem,
    TrailKind,
    WrittenRepresentationLetter,
    company_node_id,
    organisation_node_id,
    person_node_id,
    split_full_name,
)
from backend.contracts.state import staged, upsert_by_id
from backend.contracts.views import GateView
from backend.orchestration.checkpoint import checkpoint_serde
from tests.fakes import (
    PLAN_CONFIRMATION,
    PLAN_EQR_BASIS,
    XERO_FS_V1,
    audit_plan,
    compliance_check,
    engagement_terms,
    governance_communication,
    independence_declaration,
    ownership_graph,
    representation_letter,
)


def _exc(version: int) -> AuditException:
    pointer = EvidencePointer(type=EvidenceType.invoice, source_type="sales_invoice", source_id="SI-1", locator="id")
    return AuditException(
        id="X-1",
        version=version,
        procedure_id="REV-08",
        run_id="RUN-REV-08-1",
        rule_id="R01",
        severity="high",
        source_ids=["SI-1"],
        evidence=[pointer],
    )


def test_enum_values_equal_names() -> None:
    assert Phase.P2_Executing.value == "P2_Executing"
    assert GateId.PLN_01_03.value == "PLN-01/03"
    assert GateId.ACC_08.value == "ACC-08"  # 需求 R198


def test_upsert_by_id() -> None:
    assert upsert_by_id([_exc(1)], [_exc(2)])[0].version == 2
    assert upsert_by_id([_exc(2)], [_exc(2)])[0].version == 2
    with pytest.raises(VersionConflict):
        upsert_by_id([_exc(2)], [_exc(1)])


def test_staged_clears_only_on_none() -> None:
    assert staged([{"a": 1}], []) == [{"a": 1}]
    assert staged([{"a": 1}], None) == []


def test_gate_view_discriminator() -> None:
    view = GateView.model_validate(
        {
            "gate": "FIN-01",
            "target_id": "SCHEDULE",
            "target_version": 1,
            "material": {
                "kind": "FIN-01",
                "misstatements": [],
                "schedule": {
                    "id": "SCHEDULE",
                    "items": [],
                    "excluded_trivial_ids": [],
                    "total_cents": 0,
                    "materiality_cents": 12000000,
                    "candidate_opinion": "unqualified",
                },
            },
        }
    )
    assert view.material.kind == "FIN-01"


# ── 契约一次合并: 新取值与新字段 ─────────────────────────────────────────

NOW = datetime(2026, 10, 18, tzinfo=UTC)
DOCUMENT = DocumentRef(
    id="DOC-0f8e8d98c18b",
    type=DocumentType.agreement,
    file_name="harbourview_lease.pdf",
    sha256="0" * 64,
    page_count=2,
    reliability=Reliability.external_copy,
    status="received",
    uploaded_at=NOW,
)
JUDGMENT = Judgment(
    id="J-REV-02-1-S",
    procedure_id="REV-02",
    run_id="RUN-REV-02-1",
    subject_id="S",
    conclusion="",
    confidence=0.9,
    confidence_parts={},
    reasoning_chain=[],
    evidence=[],
    counter_argument="",
    proposed_action=ProposedAction.accept,
    autonomy_level=AutonomyLevel.L3,
    performed_by="U-1",
    created_at=NOW,
)


def test_new_enum_values() -> None:
    """文件类型 14 个: 上游的前 6 个, 本次 7 个, other 最后; 不加 supplier_invoice."""
    assert [t.value for t in DocumentType] == [
        "contract",
        "invoice",
        "confirmation",
        "bank_statement",
        "minutes",
        "financial_statements",
        "registry_extract",
        "engagement_letter",
        "representation_letter",
        "correspondence",
        "governance_communication",
        "agreement",
        "internal_record",
        "other",
    ]
    assert [k.value for k in TaskKind] == ["normal", "unattributed", "failed", "dispute", "manual"]
    assert [c.value for c in CriticTrigger] == ["main", "internal_copy", "assumption", "unsupported", "sample"]
    assert [k.value for k in TrailKind][-2:] == ["data", "presentation"]
    assert [s.value for s in ProcedureStatus] == [
        "planned",
        "executing",
        "awaiting_human",
        "in_dispute",
        "completed",
        "awaiting_data",  # 需求 R272: 程序等数据
        "no_data",  # 需求 R272, R280: 未执行 (无数据)
    ]
    assert [s.value for s in ReviewStatus] == ["pending", "confirmed", "overridden", "superseded"]


def test_document_ref_new_fields_after_upstream_ones() -> None:
    assert list(DocumentRef.model_fields)[-6:] == [
        "media_type",
        "channel",
        "uploaded_by",
        "failed_step",
        "document_date",
        "related_ids",
    ]


def test_old_document_ref_json_reads_defaults() -> None:
    old = DOCUMENT.model_dump(mode="json")
    del old["document_date"], old["related_ids"]
    ref = DocumentRef.model_validate_json(json.dumps(old))
    assert (ref.document_date, ref.related_ids) == (None, [])
    dated = DOCUMENT.model_copy(update={"document_date": date(2026, 9, 22), "related_ids": ["ACN-900043030", "XJ-613"]})
    assert DocumentRef.model_validate_json(dated.model_dump_json()) == dated


def test_old_judgment_json_reads_no_critic_trigger() -> None:
    old = JUDGMENT.model_dump(mode="json")
    del old["critic_trigger"]
    assert Judgment.model_validate_json(json.dumps(old)).critic_trigger is None


# 第一个带别名的模型 (JSON 键 class): 检查点按字段名重建, 没有 populate_by_name 时 company_class 悄悄丢失
EXTRACT = AsicExtract(
    acn="900043021",
    name="Demo Company (AU) Pty Ltd",
    type="APTY",
    company_class="LMSH",
    subclass="PROP",
    status="Registered",
    registered_date=date(2018, 9, 3),
    officeholders=[],
    shareholders=[],
    paid_up_cents=100,
    file_name="900043021.pdf",
    file_bytes=b"%PDF-1.4",
)


def test_new_values_survive_checkpoint_serde() -> None:
    """新枚举在 contracts/models.py 中, 检查点序列化白名单自动登记: 读回仍是枚举, 不退化为字符串. 带别名字段的
    AsicExtract 读回相等."""
    serde = checkpoint_serde()
    judgment = JUDGMENT.model_copy(
        update={"critic_trigger": CriticTrigger.internal_copy, "review_status": ReviewStatus.superseded}
    )
    values = {
        "judgments": [judgment],
        "documents": [DOCUMENT.model_copy(update={"document_date": date(2026, 9, 22), "related_ids": ["XJ-613"]})],
        "kinds": [TaskKind.manual, ProcedureStatus.in_dispute, TrailKind.presentation],
        "extracts": [EXTRACT],
    }
    back = serde.loads_typed(serde.dumps_typed(values))
    assert back == values
    assert back["judgments"][0].critic_trigger is CriticTrigger.internal_copy
    assert back["judgments"][0].review_status is ReviewStatus.superseded
    assert back["documents"][0].type is DocumentType.agreement
    assert back["documents"][0].document_date == date(2026, 9, 22)
    assert [type(k) for k in back["kinds"]] == [TaskKind, ProcedureStatus, TrailKind]


def test_procedure_in_dispute_survives_store_json_and_checkpoint() -> None:
    """R151 验收 1: 争议中的程序存回读出 (审计产出以 JSON 存) 与经检查点序列化读回, status 仍是枚举 in_dispute."""
    procedure = Procedure(
        id="CSH-02",
        phase=Phase.P2_Executing,
        template_commit="abc123",
        autonomy_level=AutonomyLevel.L3,
        status=ProcedureStatus.in_dispute,
    )
    assert Procedure.model_validate_json(procedure.model_dump_json()).status is ProcedureStatus.in_dispute
    serde = checkpoint_serde()
    back = serde.loads_typed(serde.dumps_typed({"procedure_pack": [procedure]}))
    assert back["procedure_pack"] == [procedure] and back["procedure_pack"][0].status is ProcedureStatus.in_dispute


def test_old_judgment_and_decision_json_read_without_r150_keys() -> None:
    """R150 验收 3: 旧判断 J-CSH-02-1-BA-01 与旧决定 D-CSH02 的 JSON (没有新键) 读得出, 新字段为缺省; 含新字段的状态
    经检查点存回读出不变."""
    draft = MisstatementDraft(
        type=MisstatementType.factual,
        account="Cash",
        assertion=Assertion.Cutoff,
        amount_cents=5000000,
        direction=Direction.overstated,
    )
    judgment = JUDGMENT.model_copy(update={"id": "J-CSH-02-1-BA-01", "procedure_id": "CSH-02"})
    old = judgment.model_dump(mode="json")
    del old["critic_proposed_action"], old["critic_misstatement_draft"]
    back = Judgment.model_validate_json(json.dumps(old))
    assert (back.critic_proposed_action, back.critic_misstatement_draft) == (None, None)
    decision = ReviewRecord(
        id="D-CSH02",
        task_id="T-J-CSH-02-1-BA-01-v1",
        target_version=1,
        decision=Decision.confirm,
        reviewed_by="U-1",
        decided_at=datetime(2026, 7, 15, tzinfo=UTC),
    )
    stored = decision.model_dump(mode="json")
    del stored["consulted"], stored["consultation_record"]
    read = ReviewRecord.model_validate_json(json.dumps(stored))
    assert (read.consulted, read.consultation_record) == (False, None)
    serde = checkpoint_serde()
    values = {
        "judgments": [
            judgment.model_copy(
                update={"critic_proposed_action": ProposedAction.misstatement, "critic_misstatement_draft": draft}
            )
        ],
        "review_records": [decision.model_copy(update={"consulted": True, "consultation_record": "技术合伙人"})],
    }
    assert serde.loads_typed(serde.dumps_typed(values)) == values


# ── R127: 客户画像的扩展字段 ──

PROFILE_BASE = {
    "id": "CLIENT-X",
    "legal_name": "X Pty Ltd",
    "abn": "12345678901",
    "entity_type": "small_proprietary",
    "industry_anzsic": "7000",
    "listed": False,
    "directors": ["A"],
}


def test_client_profile_extension_defaults() -> None:
    """扩展字段都有默认值: 只给原有字段的画像 (含 data/demo 与 Xero 快照载入器产出的) 照常 model_validate."""
    profile = ClientProfile.model_validate(PROFILE_BASE)
    assert profile.model_dump(exclude=set(PROFILE_BASE) | {"version", "notes"}) == {
        "financial_year_end": None,
        "accounting_system": None,
        "engagements": [],
        "approval_limit_cents": None,
        "key_personnel": [],
        "related_parties": [],
        "ownership": [],
        "prior_risk_register": {},
        "prior_misstatements": [],
        "independence_flags": [],
        "applicable_framework": None,
        "governance": None,
        "fees": None,
        "service_organisations": [],
        "prior_estimate_outcomes": [],
        "retention": None,
        "prior_period_audited": None,
    }
    numbat = Path(__file__).resolve().parents[1] / "data" / "demo" / "client_profile.json"
    assert ClientProfile.model_validate_json(numbat.read_bytes()).approval_limit_cents is None
    full = ClientProfile.model_validate(
        {
            **PROFILE_BASE,
            "financial_year_end": "02-29",
            "prior_misstatements": [
                {"engagement": "ENG-2025-001", "area": "cutoff", "amount_cents": 1, "corrected": True}
            ],
            "service_organisations": [{"name": "P", "service": "payroll", "report_type": "type2"}],
            "prior_estimate_outcomes": [
                {"engagement": "ENG-2025-001", "estimate": "ecl", "estimated_cents": 1, "actual_cents": 2}
            ],
            "retention": {"created": "2025-07-01", "review_by": "2032-07-01", "basis": "APP 11.2"},
            "prior_risk_register": {"ENG-2025-001": ["revenue_recognition:high"]},
        }
    )
    assert full.retention is not None and full.retention.review_by == date(2032, 7, 1)
    assert ClientProfile.model_validate_json(full.model_dump_json()) == full


@pytest.mark.parametrize(
    "change",
    [
        {"colour": "red"},  # 多字段
        {"financial_year_end": "9-30"},
        {"financial_year_end": "13-01"},
        {"financial_year_end": "02-30"},
        {"approval_limit_cents": -1},
        {"applicable_framework": "ifrs"},
        {"ownership": [{"holder": "A", "kind": "person", "pct": 1.2}]},
        {"ownership": [{"holder": "A", "kind": "person", "shares": -1}]},
        {"ownership": [{"holder": "A", "kind": "trust"}]},
        {"related_parties": [{"name": "B", "kind": "company", "relationship": "x", "abn": "123"}]},
        {"related_parties": [{"name": "B", "kind": "company", "relationship": "x", "acn": "90004304a"}]},
        {
            "related_parties": [{"name": "B", "kind": "company", "relationship": "x", "abn": "١٧٩٠٠٠٠٤٣٠٤"}]
        },  # 只认 ASCII
        {"related_parties": [{"name": "B", "kind": "company", "relationship": "x", "acn": "٩٠٠٠٤٣٠٣٠"}]},
        {"financial_year_end": "٠٩-٣٠"},
        {"fees": {"dependency_pct_by_year": {"2026": 5.0}}},  # 比例 0 至 1
        {"fees": {"referral_source_fees_pct_by_year": {"2026": -1.0}}},
        {"fees": {"audit_fee_cents": 1, "extra": 1}},  # 嵌套模型也不许多字段
        {"governance": {"body": "board", "members": [{"name": "A"}]}},  # 缺 also_management
        {"service_organisations": [{"name": "P", "service": "payroll", "report_type": "type3"}]},
    ],
)
def test_client_profile_extension_rejects_bad_values(change: dict) -> None:
    with pytest.raises(ValidationError):
        ClientProfile.model_validate({**PROFILE_BASE, **change})


def test_fee_shares_accept_the_bounds() -> None:
    fees = {"dependency_pct_by_year": {"2025": 0.0, "2026": 1.0}, "referral_source_fees_pct_by_year": {"2026": 0.25}}
    profile = ClientProfile.model_validate({**PROFILE_BASE, "fees": fees})
    assert profile.fees is not None and profile.fees.dependency_pct_by_year == {"2025": 0.0, "2026": 1.0}


def test_confirmation_accrued_interest_reads_old_json_and_survives_checkpoint_serde() -> None:
    """需求 R104: 不含 accrued_interest_cents 的旧函证 JSON (旧存储行) 读回为 None; 检查点序列化往返不变 (0 与
    4853 都原样读回, 0 不退化为空)."""
    sent = Confirmation(
        id="C-BA-01",
        bank_account_id="BA-01",
        confirming_party="Harbourline Bank Limited",
        idempotency_key="E:CSH-04:BA-01:abc",
        sent_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    old = sent.model_dump(mode="json")
    del old["accrued_interest_cents"]
    assert Confirmation.model_validate_json(json.dumps(old)).accrued_interest_cents is None
    serde = checkpoint_serde()
    values = {"confirmations": [sent.model_copy(update={"accrued_interest_cents": v}) for v in (0, 4853, None)]}
    back = serde.loads_typed(serde.dumps_typed(values))
    assert back == values and [c.accrued_interest_cents for c in back["confirmations"]] == [0, 4853, None]


# ── R136: 股权与任职关系图 ──


def test_ownership_graph_is_frozen_and_strict() -> None:
    """验收 1: OwnershipGraph, PartyNode, PartyLink 都不可改 (frozen), 不收多余字段; 关系的比例须在 0 至 1, 股数不为负,
    种类只收三种; 节点的 ACN, ABN 须为 9, 11 位数字; searched 缺省为假; 存储 JSON 往返不变 (检查点往返见
    test_state_compat)."""
    graph = ownership_graph()
    with pytest.raises(ValidationError):
        graph.nodes[0].name = "x"  # type: ignore[misc]
    node = graph.nodes[0].model_dump()
    for change in ({"extra": 1}, {"acn": "900 043 021"}, {"acn": "90004302"}, {"abn": "2590004302"}):
        with pytest.raises(ValidationError):
            PartyNode.model_validate({**node, **change})
    link = graph.links[1].model_dump()
    for change in ({"pct": 1.5}, {"shares": -1}, {"relation": "auditor"}, {"extra": 1}):
        with pytest.raises(ValidationError):
            PartyLink.model_validate({**link, **change})
    with pytest.raises(ValidationError):
        OwnershipGraph.model_validate({**graph.model_dump(), "edges": []})
    sam = graph.nodes[2]
    assert sam.id == "PER-WHITFIELD-SAM-NODOB" and sam.searched is False and "searched" not in sam.model_fields_set
    assert OwnershipGraph.model_validate_json(graph.model_dump_json()) == graph


def test_ownership_graph_is_closed_over_its_nodes() -> None:
    """R136 审核意见 04: 节点 ID 重复, 关系的一端不在 nodes 中, 报告主体不在 nodes 中, 都抛 ValidationError,
    消息写明 ID."""
    graph = ownership_graph().model_dump()
    dangling = {**graph["links"][0], "to_id": "ACN-999999999"}
    cases = [
        ({"nodes": [*graph["nodes"], graph["nodes"][1]]}, "节点 ID 重复: PER-MORGAN-ALEX-19790412"),
        ({"links": [*graph["links"], dangling]}, "不在 nodes 中的节点: ACN-999999999"),
        ({"reporting_entity_id": "ACN-000000000"}, "不在 nodes 中的节点: ACN-000000000"),
    ]
    for change, message in cases:
        with pytest.raises(ValidationError, match=message):
            OwnershipGraph.model_validate({**graph, **change})


@pytest.mark.parametrize(
    ("family", "given", "born", "expected"),
    [
        ("Morgan", "Alex", date(1979, 4, 12), "PER-MORGAN-ALEX-19790412"),
        ("Whitfield", "Sam", None, "PER-WHITFIELD-SAM-NODOB"),  # 只持股不任职, 摘录股东栏没有出生日期
        ("O'Connor", "Mary-Kate", date(1990, 1, 2), "PER-OCONNOR-MARYKATE-19900102"),  # 撇号, 连字符去掉
        ("van der Berg", "Jo Ann", None, "PER-VANDERBERG-JOANN-NODOB"),  # 空格去掉
        ("Smith2", "Zoë", None, "PER-SMITH2-ZOE-NODOB"),  # 数字保留; 先做 Unicode 分解, 附加符去掉而字母留下
        ("Núñez", "José", date(1985, 6, 30), "PER-NUNEZ-JOSE-19850630"),  # 同上, 与不带重音的拼法得同一 ID
        ("Cher", "", None, "PER-CHER--NODOB"),  # 单名: 名为空
    ],
)
def test_person_node_id(family: str, given: str, born: date | None, expected: str) -> None:
    """验收 2: 个人节点 PER-{姓}-{名}-{yyyymmdd 或 NODOB}, 姓与名各自经 Unicode 分解, 转大写并只留 A-Z 与 0-9."""
    assert person_node_id(family, given, born) == expected


def test_person_node_id_rejects_empty_family_name() -> None:
    """R136 审核意见 03: 姓规范化后为空 (只有非拉丁字母, 或空串) 时抛 ValueError, 不同的人不并成 PER---NODOB."""
    for family in ("王", "", " - "):
        with pytest.raises(ValueError, match="姓规范化后为空"):
            person_node_id(family, "小明", None)


def test_full_name_split_and_company_node_id() -> None:
    """验收 2: 摘录中只有全名时姓取最后一个词, 名取其余词; 含逗号时按 "姓, 名" 拆 (R136 审核意见 03); 公司节点
    ACN-{acn}, ACN 先去掉空白, 之后不是 9 位数字抛 ValueError (审核意见 04); 没有 ACN 的公司 ORG-{名称}."""
    assert split_full_name("Sam Whitfield") == ("Whitfield", "Sam")
    assert split_full_name("  Mary Ann  O'Connor ") == ("O'Connor", "Mary Ann")
    assert person_node_id(*split_full_name("Mary Ann O'Connor"), None) == "PER-OCONNOR-MARYANN-NODOB"
    assert split_full_name("Cher") == ("Cher", "")
    assert split_full_name("MORGAN, Alex") == ("MORGAN", "Alex")
    assert split_full_name(" Morgan ,  Alex ") == ("Morgan", "Alex")
    assert person_node_id(*split_full_name("MORGAN, Alex"), None) == "PER-MORGAN-ALEX-NODOB"
    assert split_full_name("van der Berg, Jo Ann") == ("van der Berg", "Jo Ann")
    assert company_node_id("900043021") == company_node_id(" 900 043 021 ") == "ACN-900043021"
    for acn in ("", "90004302", "9000430211", "900-043-021"):
        with pytest.raises(ValueError, match="acn 须为 9 位数字"):
            company_node_id(acn)
    # 没有 ACN 的公司 (需求 R137; 审核意见 R137-02): ORG-{名称}, 名称规范化后为空时抛 ValueError
    assert organisation_node_id("Galah Holdings (NZ) Limited") == "ORG-GALAHHOLDINGSNZLIMITED"
    for name in ("", "  ", "株式会社", "—"):
        with pytest.raises(ValueError, match="名称规范化后为空"):
            organisation_node_id(name)


# ── R216: 待审财报 ──


def lines_of(statement: str) -> dict[str, FinancialStatementLine]:
    return {line.code: line for line in XERO_FS_V1.lines if line.statement == statement}


def test_financial_statements_contract() -> None:
    """验收 1: FS 固定 ID, 版本, 编制日, 董事声明日与比较数重述凭证号; 35 行分属四张报表 (财务状况表 12, 损益表 6,
    权益变动表 5, 现金流量表 12); 行代码重复, sign 不是 ±1, 报表种类不认识或多给字段都被拒; JSON 往返不变."""
    fs = XERO_FS_V1
    assert (fs.id, fs.version, fs.prepared_at, fs.directors_declaration_date, fs.document_id) == (
        "FS",
        1,
        date(2026, 10, 22),
        date(2026, 10, 23),
        None,
    )
    assert fs.comparative_restatement_entry_nos == ["XJ-397"]
    assert Counter(line.statement for line in fs.lines) == {
        "financial_position": 12,
        "profit_or_loss": 6,
        "changes_in_equity": 5,
        "cash_flows": 12,
    }
    with pytest.raises(ValidationError, match="财报行代码重复: SOFP-CASH"):
        FinancialStatements.model_validate(
            {**fs.model_dump(), "lines": [*fs.model_dump()["lines"], fs.lines[0].model_dump()]}
        )
    line = fs.lines[0].model_dump()
    for change in ({"sign": 0}, {"statement": "notes"}, {"extra": 1}):
        with pytest.raises(ValidationError):
            FinancialStatementLine.model_validate({**line, **change})
    assert FinancialStatements.model_validate_json(fs.model_dump_json()) == fs


def test_xero_fs_v1_figures_and_mapping() -> None:
    """验收 2: 设计稿第 4.7 节的三个数 (资产 21,829.26 = 负债 51,878.25 + 权益 (30,048.99)); 比较数的 GST 应收 791.20
    在 SOFP-TAX 中以 -79120 列示; 财务状况表非小计行的科目互不重叠 (820 只映射一次), 共 33 个科目 (09-30 试算表 27 行
    加 804, 805, 826, 478, 620, 270). 表内其余勾稽由 data/check_xero_demo.py 对 fs_v1.json 做 (test_xero_demo_data),
    fs_v1.json 与本表相同由 test_xero_demo_data 守."""
    pos = lines_of("financial_position")
    assets, liabilities, equity = (pos[c].amount_cents for c in ("SOFP-TA", "SOFP-TL", "SOFP-EQ"))
    assert (assets, liabilities, equity) == (2182926, 5187825, -3004899) and assets == liabilities + equity
    assert pos["SOFP-TAX"].prior_amount_cents == -79120
    mapped = [code for line in pos.values() if not line.subtotal for code in line.account_codes]
    assert len(mapped) == len(set(mapped)) == 33


# ── R215: 签字前提的四类审计产出 ──


def test_representation_letter_contract() -> None:
    """验收 1: REPLETTER 的字段与取值; 一项声明为 refused_accepted 而缺 acceptance_reason 或 opinion_effect (空串同),
    kind 不是 core 或 specific, 或多给字段, 抛 ValidationError; 状态只收四种; 存储 JSON 往返不变."""
    letter = representation_letter()
    assert (letter.id, letter.status, letter.period_end, letter.run_id) == (
        "REPLETTER",
        RepresentationStatus.obtained,
        date(2026, 6, 30),
        "RUN-FIN-07-1",
    )
    assert letter.schedule_ref == ObjectRef(kind="UncorrectedSchedule", id="SCHEDULE", version=1)
    accepted = letter.representations[2].model_dump()
    assert accepted["status"] is ClauseStatus.refused_accepted
    for change in (
        {"acceptance_reason": None},
        {"opinion_effect": None},
        {"opinion_effect": ""},
        {"kind": "other"},
        {"extra": 1},
    ):
        with pytest.raises(ValidationError):
            RepresentationClause.model_validate({**accepted, **change})
    present = letter.representations[0].model_dump()
    assert RepresentationClause.model_validate({**present, "status": "missing"}).status is ClauseStatus.missing
    for change in ({"status": "lost"}, {"extra": 1}):
        with pytest.raises(ValidationError):
            WrittenRepresentationLetter.model_validate({**letter.model_dump(), **change})
    with pytest.raises(ValidationError):
        ObjectRef(kind="UncorrectedSchedule", id="SCHEDULE", version=0)
    assert WrittenRepresentationLetter.model_validate_json(letter.model_dump_json()) == letter


def test_compliance_check_contract_and_failed_codes() -> None:
    """验收 2: CHECK-{procedure_id} 的字段; failed_codes 只读, 列出 failed 项的代号 (按首次出现的顺序, 逐行核对的
    同一代号只列一次; 数据里 K03 先于 K01 不通过, 不同于字典序), 不进 JSON; 缺省 exception_ids 为空, must_disclaim
    与 requires_disposition 为假; 多给字段被拒; JSON 往返不变."""
    check = compliance_check(exception_ids=["X-FIN-10-1-E02-1"])
    assert (check.id, check.procedure_id, check.run_id, check.basis) == (
        "CHECK-FIN-10",
        "FIN-10",
        "RUN-FIN-10-1",
        {"client_profile_version": 1},
    )
    assert check.failed_codes == ["E02"] and "failed_codes" not in check.model_dump()
    assert (check.must_disclaim, check.subject_ref, compliance_check().exception_ids) == (False, None, [])
    assert [item.requires_disposition for item in check.items] == [False, True, False]  # 样例只给 E02 显式置真
    line = {"description": "Line agrees to adjusted trial balance", "standard_ref": "ASA 330.20(a)"}
    fs12 = ComplianceCheck(
        id="CHECK-FIN-12",
        procedure_id="FIN-12",
        run_id="RUN-FIN-12-1",
        items=[
            CheckItem(code="K01", status=CheckItemStatus.passed, **line),
            CheckItem(code="K03", status=CheckItemStatus.failed, **line),
            CheckItem(code="K01", status=CheckItemStatus.failed, **line),
            CheckItem(code="K03", status=CheckItemStatus.failed, **line),
            CheckItem(code="K05", status=CheckItemStatus.attention, **line),
        ],
        subject_ref=ObjectRef(kind="FinancialStatements", id="FS", version=1),
        basis={"fs_version": 1, "adjustment_entry_nos": ["AJE-X01", "AJE-X05"]},
    )
    assert fs12.failed_codes == ["K03", "K01"]
    with pytest.raises(ValidationError, match="frozen"):
        fs12.failed_codes = []  # type: ignore[misc]
    for change in ({"extra": 1}, {"items": [{**check.items[0].model_dump(), "status": "skipped"}]}):
        with pytest.raises(ValidationError):
            ComplianceCheck.model_validate({**check.model_dump(), **change})
    assert ComplianceCheck.model_validate_json(fs12.model_dump_json()) == fs12


def test_governance_communication_contract() -> None:
    """验收 3: GC-RPT-07-1 的字段; kind 只有 completion (缺省), form 只收 written, oral; communicated_at 必填; 多给字段
    被拒; JSON 往返不变."""
    communication = governance_communication()
    assert (communication.id, communication.kind, communication.form, communication.run_id) == (
        "GC-RPT-07-1",
        "completion",
        CommunicationForm.written,
        "RUN-RPT-07-1",
    )
    dumped = communication.model_dump()
    for change in ({"kind": "planning_scope"}, {"form": "email"}, {"extra": 1}):
        with pytest.raises(ValidationError):
            GovernanceCommunication.model_validate({**dumped, **change})
    with pytest.raises(ValidationError, match="communicated_at"):
        GovernanceCommunication.model_validate({k: v for k, v in dumped.items() if k != "communicated_at"})
    with pytest.raises(ValidationError):
        CommunicationMatter.model_validate({**communication.matters[0].model_dump(), "extra": 1})
    assert GovernanceCommunication.model_validate_json(communication.model_dump_json()) == communication


def test_independence_declaration_form_matches_contraventions() -> None:
    """验收 4: S307C 的字段与缺省 (draft, 日期, 报告与签字人为空); based_on 必填; form 为 contraventions 而列表为空,
    或 no_contraventions 而列表非空, 抛 ValidationError; 两者一致时通过; JSON 往返不变."""
    declaration = independence_declaration()
    assert (declaration.id, declaration.status, declaration.based_on.id, declaration.based_on.version) == (
        "S307C",
        ReportStatus.draft,
        "CHECK-FIN-10",
        1,
    )
    assert (declaration.declaration_date, declaration.report_id, declaration.signed_by) == (None, None, None)
    dumped = declaration.model_dump()
    listed = [Contravention(item_code="E02", text="A contingent fee was agreed for the audit.")]
    with pytest.raises(ValidationError, match="based_on"):
        IndependenceDeclaration.model_validate({k: v for k, v in dumped.items() if k != "based_on"})
    with pytest.raises(ValidationError, match="form 为 contraventions 时须列明违规"):
        IndependenceDeclaration.model_validate({**dumped, "form": "contraventions"})
    with pytest.raises(ValidationError, match="form 为 no_contraventions 时不能列明违规"):
        IndependenceDeclaration.model_validate({**dumped, "contraventions": listed})
    with_breach = IndependenceDeclaration.model_validate({**dumped, "form": "contraventions", "contraventions": listed})
    assert with_breach.form is DeclarationForm.contraventions and with_breach.contraventions == listed
    signed = declaration.model_copy(
        update={"version": 2, "status": ReportStatus.signed, "declaration_date": date(2026, 8, 25)}
    )
    assert IndependenceDeclaration.model_validate_json(signed.model_dump_json()) == signed


# ── R198: ACC-08 约定条款与委任核验 ──


def test_engagement_terms_contract() -> None:
    """验收 2, 3: EngagementTerms (TERMS) 不可改, 不收多余字段, 框架 (取值同 ReportingFramework, 特殊目的为 spfr, 审核
    R198-04) 与核对结果只收登记的取值, 存储 JSON 往返不变 (检查点往返见 test_state_compat); 要素与确认各只放自己
    那一类的项, 放错抛 ValidationError 并写明项名.
    EngagementTermsExtraction 的字段恰为 EngagementTerms 的 framework 至 year_change, 从 TERMS 的这部分解析回来不变."""
    terms = engagement_terms()
    with pytest.raises(ValidationError):
        terms.consent.given_on = date(2026, 8, 27)  # type: ignore[union-attr, misc]
    data = terms.model_dump()
    for change in (
        {"extra": 1},
        {"framework": "special_purpose"},
        {"checks": [{**data["checks"][0], "status": "warning"}]},
    ):
        with pytest.raises(ValidationError):
            EngagementTerms.model_validate({**data, **change})
    misplaced = TermsItem(item="prepare_fr", present=True).model_dump()
    with pytest.raises(ValidationError, match="放错了类: prepare_fr"):
        EngagementTerms.model_validate({**data, "elements": [*data["elements"][:4], misplaced]})
    assert EngagementTerms.model_validate_json(terms.model_dump_json()) == terms
    assert terms.checks[0].status is TermsCheckStatus.passed

    facts = list(EngagementTermsExtraction.model_fields)
    assert facts == list(EngagementTerms.model_fields)[2:14] and (facts[0], facts[-1]) == ("framework", "year_change")
    extraction = EngagementTermsExtraction.model_validate(terms.model_dump(include=set(facts)))
    assert extraction.model_dump() == terms.model_dump(include=set(facts))


# ── R203: 总体审计计划 ──


def test_audit_plan_fields_and_defaults() -> None:
    """验收 1: AuditPlan 的字段恰为验收所列 (另有 id, version); 草稿的三项确认, 批准人与批准时间为空, 第 1 修订不回应
    决定; 冻结且不收多余键; JSON 往返不变 (materiality_choice 是后文定义的 MaterialityChoice, 经 model_rebuild)."""
    assert list(AuditPlan.model_fields) == [
        "id",
        "version",
        "revision",
        "status",
        "run_id",
        "period_start",
        "period_end",
        "applicable_framework",
        "engagement_letter_document_id",
        "materiality_choice",
        "materiality_cents",
        "performance_materiality_cents",
        "clearly_trivial_cents",
        "significant_risk_objective_ids",
        "items",
        "narrative",
        "procedure_notes",
        "timetable",
        "eqr_recommendation",
        "expert_assessment",
        "standards_cited",
        "eqr_required",
        "eqr_basis",
        "auditor_expert_not_required",
        "addresses_decision_id",
        "revision_reason",
        "approved_by",
        "approved_at",
    ]
    plan = audit_plan()
    assert (plan.id, plan.version, plan.revision, plan.status, plan.run_id) == (
        "PLAN-1",
        1,
        1,
        PlanStatus.draft,
        "RUN-PLN-15-1",
    )
    assert (plan.eqr_required, plan.eqr_basis, plan.auditor_expert_not_required) == (None, None, None)
    assert (plan.addresses_decision_id, plan.revision_reason, plan.approved_by, plan.approved_at) == (None,) * 4
    assert plan.materiality_choice.basis == "revenue" and plan.timetable[0].due == date(2026, 8, 14)
    assert [x.section for x in plan.narrative] == ["overall_strategy", "scope", "significant_risks", "materiality"]
    dumped = plan.model_dump()
    minimal = {
        k: v for k, v in dumped.items() if k not in {"status", "applicable_framework", "engagement_letter_document_id"}
    }
    assert AuditPlan.model_validate(minimal).status is PlanStatus.draft  # 状态缺省 draft, TERMS 两项缺省为空
    with pytest.raises(ValidationError):
        AuditPlan.model_validate({**dumped, "extra": 1})
    with pytest.raises(ValidationError):
        plan.revision = 2  # type: ignore[misc]
    assert AuditPlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": "PLAN-2"}, "须为 PLAN-1"),
        ({"revision": 0, "id": "PLAN-0"}, "greater than or equal to 1"),
        ({"approved_by": "U-1"}, "草稿的批准确认与批准人须为空"),
        ({"eqr_required": False}, "草稿的批准确认与批准人须为空"),
        ({"status": "approved"}, "已批准的计划须有三项确认, 批准人与批准时间"),
        ({"narrative": [{"section": "scope", "text": "a"}, {"section": "scope", "text": "b"}]}, "段名重复"),
        ({"applicable_framework": "unknown"}, "applicable_framework"),
        ({"applicable_framework": "special_purpose"}, "applicable_framework"),  # 特殊目的写作 spfr (ReportingFramework)
    ],
)
def test_audit_plan_rejects_inconsistent_fields(change: dict, message: str) -> None:
    """验收 1, 2: ID 与修订号不符, 修订号小于 1, 草稿带批准的项, 已批准缺批准的项, 叙述段名重复, 框架不是
    ReportingFramework 的取值 (同 TERMS) 时抛错. 改版须经 model_validate (model_copy 不跑这些校验)."""
    with pytest.raises(ValidationError, match=message):
        AuditPlan.model_validate({**audit_plan().model_dump(), **change})


def test_audit_plan_status_versions_within_a_revision() -> None:
    """验收 2: 同一修订内只有状态变化出新版本: draft v1 → approved v2 (三项确认, 批准人, 批准时间都填) → superseded v3
    沿用; 驳回后的草稿置 superseded 时确认项都为空也通过. 第 2 修订回应驳回决定."""
    approved = audit_plan(version=2, status=PlanStatus.approved)
    assert (approved.eqr_required, approved.eqr_basis, approved.auditor_expert_not_required) == (
        False,
        PLAN_EQR_BASIS,
        True,
    )
    assert approved.approved_by == "U-1" and approved.approved_at is not None
    for field in ("eqr_basis", "approved_at"):
        with pytest.raises(ValidationError, match="已批准的计划须有"):
            AuditPlan.model_validate({**approved.model_dump(), field: None})
    retired = approved.model_copy(update={"version": 3, "status": PlanStatus.superseded})
    assert AuditPlan.model_validate(retired.model_dump()) == retired
    assert audit_plan(version=2, status=PlanStatus.superseded).approved_by is None
    second = audit_plan(revision=2)
    assert (second.id, second.addresses_decision_id, second.run_id) == ("PLAN-2", "D-P15R", "RUN-PLN-15-2")


def test_plan_confirmation_on_gate_decision_and_request_body() -> None:
    """验收 4: GateId.PLN_15; GateDecision.plan_confirmation 缺省为空, 旧决定的 JSON (没有该键) 读得出; 带确认的往返
    不变; PlanConfirmation 三项都必填. 请求体 GateDecisionIn 的字段与 GateDecision 去掉服务端填的四项相同."""
    assert GateId.PLN_15.value == "PLN-15"
    confirmation = PLAN_CONFIRMATION
    decision = GateDecision(
        id="D-PLAN",
        gate=GateId.PLN_15,
        target_id="PLAN-1",
        target_version=1,
        decision=GateVerdict.approve,
        decided_by="U-1",
        decided_at=datetime(2026, 9, 30, tzinfo=UTC),
    )
    assert decision.plan_confirmation is None
    old = decision.model_dump(mode="json")
    del old["plan_confirmation"]
    assert GateDecision.model_validate_json(json.dumps(old)) == decision
    confirmed = decision.model_copy(update={"plan_confirmation": confirmation})
    assert GateDecision.model_validate_json(confirmed.model_dump_json()) == confirmed
    for field in PlanConfirmation.model_fields:
        with pytest.raises(ValidationError, match=field):
            PlanConfirmation.model_validate({k: v for k, v in confirmation.model_dump().items() if k != field})
    server_side = {"id", "version", "decided_by", "decided_at"}
    assert list(GateDecisionIn.model_fields) == [f for f in GateDecision.model_fields if f not in server_side]
    request = {k: v for k, v in old.items() if k not in server_side}
    body = GateDecisionIn.model_validate({**request, "plan_confirmation": confirmation.model_dump()})
    assert body.plan_confirmation == confirmation


def test_audit_plan_draft_contract() -> None:
    """验收 3: AuditPlanDraft 的字段恰为验收所列; 从模型输出的 JSON 读出, 时间表日期为 date; 不收多余键, 缺键报错."""
    assert list(AuditPlanDraft.model_fields) == [
        "overall_strategy",
        "sections",
        "procedure_notes",
        "timetable",
        "eqr_recommendation",
        "expert_assessment",
        "standards_cited",
    ]
    text = json.dumps(
        {
            "overall_strategy": "Substantive audit.",
            "sections": {"scope": "Year ended 30 June 2026.", "timing": "Fieldwork in August."},
            "procedure_notes": [
                {"procedure_id": "REV-08", "nature_timing_extent": "Match all invoices in the last 20 days."}
            ],
            "timetable": [{"milestone": "Report signed", "due": "2026-08-28"}],
            "eqr_recommendation": {"required": False, "basis": PLAN_EQR_BASIS},
            "expert_assessment": "Not needed.",
            "standards_cited": ["ASA300-P07"],
        }
    )
    draft = AuditPlanDraft.model_validate_json(text)
    assert draft.timetable[0].due == date(2026, 8, 28) and draft.eqr_recommendation.required is False
    assert draft.procedure_notes == audit_plan().procedure_notes[1:]  # 与合成计划的 REV-08 一项相同
    with pytest.raises(ValidationError):
        AuditPlanDraft.model_validate({**json.loads(text), "extra": 1})
    with pytest.raises(ValidationError, match="expert_assessment"):
        AuditPlanDraft.model_validate({k: v for k, v in json.loads(text).items() if k != "expert_assessment"})
