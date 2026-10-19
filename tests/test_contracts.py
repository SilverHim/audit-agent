import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.contracts.errors import VersionConflict
from backend.contracts.models import (
    AuditException,
    AutonomyLevel,
    ClientProfile,
    CriticTrigger,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    GateId,
    Judgment,
    Phase,
    ProcedureStatus,
    ProposedAction,
    Reliability,
    ReviewStatus,
    TaskKind,
    TrailKind,
)
from backend.contracts.state import staged, upsert_by_id
from backend.contracts.views import GateView
from backend.orchestration.checkpoint import checkpoint_serde


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
    assert [k.value for k in TaskKind] == ["normal", "unattributed", "failed", "dispute", "blind", "manual"]
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


def test_new_values_survive_checkpoint_serde() -> None:
    """新枚举在 contracts/models.py 中, 检查点序列化白名单自动登记: 读回仍是枚举, 不退化为字符串."""
    serde = checkpoint_serde()
    judgment = JUDGMENT.model_copy(
        update={"critic_trigger": CriticTrigger.internal_copy, "review_status": ReviewStatus.superseded}
    )
    values = {
        "judgments": [judgment],
        "documents": [DOCUMENT.model_copy(update={"document_date": date(2026, 9, 22), "related_ids": ["XJ-613"]})],
        "kinds": [TaskKind.blind, TaskKind.manual, ProcedureStatus.in_dispute, TrailKind.presentation],
    }
    back = serde.loads_typed(serde.dumps_typed(values))
    assert back == values
    assert back["judgments"][0].critic_trigger is CriticTrigger.internal_copy
    assert back["judgments"][0].review_status is ReviewStatus.superseded
    assert back["documents"][0].type is DocumentType.agreement
    assert back["documents"][0].document_date == date(2026, 9, 22)
    assert [type(k) for k in back["kinds"]] == [TaskKind, TaskKind, ProcedureStatus, TrailKind]


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
