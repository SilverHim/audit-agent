import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.contracts.errors import VersionConflict
from backend.contracts.models import (
    AsicExtract,
    Assertion,
    AuditException,
    AutonomyLevel,
    ClientProfile,
    Confirmation,
    CriticTrigger,
    Decision,
    Direction,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    GateId,
    Judgment,
    MisstatementDraft,
    MisstatementType,
    OwnershipGraph,
    PartyLink,
    PartyNode,
    Phase,
    Procedure,
    ProcedureStatus,
    ProposedAction,
    Reliability,
    ReviewRecord,
    ReviewStatus,
    TaskKind,
    TrailKind,
    company_node_id,
    person_node_id,
    split_full_name,
)
from backend.contracts.state import staged, upsert_by_id
from backend.contracts.views import GateView
from backend.orchestration.checkpoint import checkpoint_serde
from tests.fakes import ownership_graph


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
    ACN-{acn}, ACN 先去掉空白, 之后不是 9 位数字抛 ValueError (审核意见 04)."""
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
