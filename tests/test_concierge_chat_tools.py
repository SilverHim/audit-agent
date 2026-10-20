"""审计助理的只读工具 (tools.py) 与引用匹配 (citations.py)."""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from backend.agents.concierge_chat.citations import match_citations
from backend.agents.concierge_chat.tools import (
    TOOLS,
    Found,
    NoArgs,
    Tool,
    ToolBox,
    ToolEnv,
    list_open_items,
    minimize,
    pseudonym,
)
from backend.contracts.conversation import ConciergeCitation, ToolCall
from backend.contracts.data import DataRequest, TableKind, XeroProfileCheck, XeroSource
from backend.contracts.models import (
    AnalyticsResult,
    AuditException,
    AutonomyLevel,
    CriticReasonType,
    CriticVerdict,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    GateId,
    Judgment,
    MaterialityCandidates,
    ProposedAction,
    ReasoningStep,
    Reliability,
    ReviewTask,
    RiskItem,
    StepKind,
    TargetType,
    TaskKind,
    TrailEntry,
    TrailKind,
    mask_person_node_ids,
    mask_person_text,
)
from backend.contracts.views import GateView, PlanningMaterial
from tests.chat_fakes import FixedState, chat_deps, make_overview
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_RECORDS,
    FakeDataStore,
    FakeFinancialStore,
    FakeOutputStore,
    FakeTrail,
    numbat_contracts,
    ownership_graph,
)

EID = ENGAGEMENT.id
INVOICE = EvidencePointer(
    type=EvidenceType.invoice, source_type="sales_invoice", source_id="SI-2026-0612", locator="posting_date"
)
DELIVERY = EvidencePointer(
    type=EvidenceType.delivery, source_type="delivery_note", source_id="DN-2026-0612", locator="signed_date"
)
STANDARD = EvidencePointer(type=EvidenceType.standard, source_type="standard", source_id="AASB15-P38", locator="38")


JUDGMENT = Judgment(
    id="J-REV-02-1-SI-2026-0612",
    procedure_id="REV-02",
    run_id="RUN-REV-02-1",
    subject_id="SI-2026-0612",
    conclusion="SI-2026-0612 的收入记早了: 控制权在 7 月 3 日才转移.",
    confidence=0.82,
    confidence_parts={"evidence": 0.8, "rule": 1.0, "llm": 0.7},
    reasoning_chain=[
        ReasoningStep(kind=StepKind.fact, text="发票 6 月 28 日入账", evidence=[INVOICE]),
        ReasoningStep(kind=StepKind.fact, text="发运单 7 月 3 日签收", evidence=[DELIVERY]),
        ReasoningStep(kind=StepKind.standard, text="AASB 15 第 38 段: 控制权转移时确认", evidence=[STANDARD]),
        ReasoningStep(kind=StepKind.inference, text="收入应计入下期", from_steps=[0, 1, 2]),
    ],
    evidence=[INVOICE, DELIVERY],
    counter_argument="若合同约定出厂即转移控制权, 则 6 月确认正确.",
    proposed_action=ProposedAction.misstatement,
    autonomy_level=AutonomyLevel.L2,
    performed_by="U-1",
    created_at=NOW,
    critic_verdict=CriticVerdict.inconsistent,
    critic_reason_type=CriticReasonType.different_conclusion,
    critic_reason="发运单显示 7 月 3 日才签收",
    critic_conclusion="收入应计入下期",
)
TASK = ReviewTask(
    id="T-J-REV-02-1-SI-2026-0612-v1",
    kind=TaskKind.normal,
    procedure_id="REV-02",
    target_type=TargetType.Judgment,
    target_id=JUDGMENT.id,
    target_version=1,
    run_id="RUN-REV-02-1",
    created_at=NOW,
)
EXCEPTION = AuditException(
    id="X-REV-08-1-R01-1",
    procedure_id="REV-08",
    run_id="RUN-REV-08-1",
    rule_id="R01",
    severity="high",
    source_ids=["SI-2026-0612"],
    amount_cents=14200000,
    evidence=[INVOICE],
)


def env(
    trail: FakeTrail | None = None,
    max_chars: int = 6000,
    state: FixedState | None = None,
    data: FakeDataStore | None = None,
    xero: Any = None,
) -> ToolEnv:
    deps = chat_deps(
        trail,
        output_store=FakeOutputStore([JUDGMENT, TASK, EXCEPTION]),
        financial_store=FakeFinancialStore(records=NUMBAT_RECORDS),
        data=data,
    )
    return ToolEnv(EID, deps, state if state is not None else FixedState(), max_chars, xero)


def data_with_requests() -> FakeDataStore:
    """两个待补交 (审批日志, 总账) 与一个已收到 (科目表) 的补交请求."""
    data = FakeDataStore()
    requests = [
        DataRequest(
            id=f"REQ-{kind.value}",
            kind="table",
            table_kind=kind,
            title=title,
            used_for=used_for,
            reason_code="missing_table",
            status=status,
            created_at=datetime(2026, 9, 30, n, tzinfo=UTC),
        )
        for n, (kind, title, used_for, status) in enumerate(
            [
                (TableKind.accounts, "科目表", ["PLN-07"], "received"),
                (TableKind.journal_entries, "总账分录", ["REV-08"], "open"),
                (TableKind.approval_log, "审批日志", ["REV-08", "CSH-05"], "open"),
            ]
        )
    ]
    data.data_requests = {r.id: r for r in requests}
    return data


async def call(name: str, args: dict[str, Any] | str = "{}", tool_env: ToolEnv | None = None) -> tuple[Any, Any]:
    arguments = args if isinstance(args, str) else json.dumps(args)
    outcome = await ToolBox().call(tool_env or env(), ToolCall(id="c", name=name, arguments=arguments))
    try:
        return outcome, json.loads(outcome.content)
    except ValueError:  # 截断后的结果不是完整 JSON
        return outcome, outcome.content


def test_specs_never_take_engagement_id() -> None:
    for spec in ToolBox().specs:
        assert spec.parameters["type"] == "object"
        assert not {"eid", "engagement_id"} & set(spec.parameters.get("properties", {}))


async def test_overview_gives_progress_amounts_and_procedure_citations() -> None:
    outcome, data = await call("get_overview")
    assert outcome.ok
    assert (data["phase"], data["procedures_completed"], data["procedures_total"]) == ("P2_Executing", 2, 3)
    assert data["materiality"] == "AUD 120,000.00"
    assert set(outcome.cites) == {"REV-08", "CSH-05", "REV-02"}


async def test_judgment_carries_reasoning_counter_argument_and_critic() -> None:
    outcome, data = await call("get_judgment", {"judgment_id": JUDGMENT.id})
    assert outcome.ok
    assert data["counter_argument"].startswith("若合同约定")
    assert [s["kind"] for s in data["reasoning_chain"]] == ["fact", "fact", "standard", "inference"]
    assert data["reasoning_chain"][1]["evidence"][0]["source_id"] == "DN-2026-0612"
    assert data["critic_verdict"] == "inconsistent"
    assert data["critic_reason"] == "发运单显示 7 月 3 日才签收"
    assert set(outcome.cites) == {JUDGMENT.id, "SI-2026-0612", "DN-2026-0612", "AASB15-P38"}
    assert outcome.cites["DN-2026-0612"].pointer == DELIVERY


async def test_missing_judgment_is_an_error_text_not_an_exception() -> None:
    outcome, data = await call("get_judgment", {"judgment_id": "J-NOPE"})
    assert not outcome.ok
    assert "J-NOPE" in data["error"]


async def test_bad_arguments_and_unknown_tool_become_error_text() -> None:
    outcome, data = await call("get_judgment", "{not json")
    assert not outcome.ok and "JSON" in data["error"]
    outcome, data = await call("get_judgment", {"judgment_id": "J-1", "eid": "ENG-OTHER"})
    assert not outcome.ok and "参数不合要求" in data["error"]
    outcome, data = await call("delete_everything")
    assert not outcome.ok and "没有这个工具" in data["error"]


async def test_tool_exception_becomes_error_text() -> None:
    class Broken(FixedState):
        async def overview(self, eid: str) -> Any:
            raise RuntimeError("database unavailable")

    outcome, data = await call("get_overview", tool_env=env(state=Broken()))
    assert not outcome.ok and "RuntimeError" in data["error"]


async def test_evidence_masks_accounts_and_pseudonymises_people() -> None:
    outcome, data = await call("get_evidence", {"source_type": "bank_account", "source_id": "BA-01"})
    assert data["row"]["account_no"] == "***601" and data["row"]["bsb"] == "***101"
    assert "12345601" not in outcome.content
    outcome, data = await call("get_evidence", {"source_type": "journal_entry", "source_id": "CR-SI-2026-0547-1"})
    assert data["row"]["created_by"] == pseudonym("ar.clerk") != data["row"]["approved_by"]
    assert "ar.clerk" not in outcome.content
    assert data["row"]["debit_aud"] == "AUD 50,000.00"
    outcome, data = await call("get_evidence", {"source_type": "delivery_note", "source_id": "DN-2026-0612"})
    assert "Daniel Price" not in outcome.content
    assert outcome.cites["DN-2026-0612"].kind == "evidence"


async def test_evidence_reads_a_supplier_bill_row() -> None:
    """需求 R178: 取证据工具能读应付账单的行, 金额另给 AUD 写法, 引用为发票类的内部原件 (同销售发票)."""
    tool_env = env()
    bill = {"id": "PB-1", "bill_no": "3395", "supplier": "PowerDirect", "outstanding_cents": 33110}
    cast(FakeFinancialStore, tool_env.deps.financial_store).records[("supplier_bill", "PB-1")] = bill
    outcome, data = await call("get_evidence", {"source_type": "supplier_bill", "source_id": "PB-1"}, tool_env)
    assert (data["source_type"], data["row"]["outstanding_aud"]) == ("supplier_bill", "AUD 331.10")
    pointer = outcome.cites["PB-1"].pointer
    assert (pointer.type, pointer.reliability) == (EvidenceType.invoice, Reliability.internal_original)


async def test_open_items_list_gate_then_tasks_with_deferral() -> None:
    trail = FakeTrail()
    await trail.record(
        TrailEntry(
            engagement_id=EID,
            kind=TrailKind.deferral,
            actor="U-1",
            summary="稍后处理",
            payload={"target_type": "review_task", "target_id": TASK.id, "deferred": True},
            created_at=datetime(2026, 9, 30, 1, tzinfo=UTC),
        )
    )
    state = FixedState(view=make_overview(current_gate=GateId.FIN_01))
    outcome, data = await call("list_open_items", tool_env=env(trail, state=state))
    assert [i["type"] for i in data["items"]] == ["gate_decision", "review_task"]
    assert data["items"][0] == {"type": "gate_decision", "gate": "FIN-01", "deferred": False}
    assert data["items"][1]["deferred"] is True
    assert data["items"][1]["subject_id"] == "SI-2026-0612"
    assert JUDGMENT.id in outcome.cites


async def test_open_items_put_open_data_requests_between_gate_and_tasks() -> None:
    trail = FakeTrail()
    await trail.record(
        TrailEntry(
            engagement_id=EID,
            kind=TrailKind.deferral,
            actor="U-1",
            summary="稍后处理",
            payload={"target_type": "data_request", "target_id": "REQ-approval_log", "deferred": True},
            created_at=datetime(2026, 9, 30, 5, tzinfo=UTC),
        )
    )
    state = FixedState(view=make_overview(current_gate=GateId.FIN_01))
    outcome, data = await call("list_open_items", tool_env=env(trail, state=state, data=data_with_requests()))
    assert [i["type"] for i in data["items"]] == ["gate_decision", "data_request", "data_request", "review_task"]
    journals, approvals = data["items"][1], data["items"][2]
    assert (journals["request_id"], journals["deferred"]) == ("REQ-journal_entries", False)
    assert approvals["title"] == "审批日志" and approvals["used_for"] == ["REV-08", "CSH-05"]
    assert approvals["deferred"] is True
    assert "REQ-accounts" not in outcome.content  # 已收到的不列
    assert {"REV-08", "CSH-05"} <= set(outcome.cites)


@pytest.mark.parametrize(
    ("features", "expected"),
    [(["collab", "critic"], ["T-Z", "T-A", TASK.id]), (["critic"], [TASK.id, "T-Z", "T-A"])],
    ids=["collab", "baseline"],
)
async def test_open_items_disputes_first_by_created_at(features: list[str], expected: list[str]) -> None:
    """R151 验收 5 (第 1 轮审核 R151-01): 两条待决争议, 晚建的 T-A 任务 ID 在前, 早建的 T-Z 在后, 普通任务最早.
    项目开了 collab 时争议排在其余复核任务之前, 争议之间按创建时间 (防按任务 ID 或存储顺序); 没开时全按创建时间."""
    late = TASK.model_copy(update={"id": "T-A", "kind": TaskKind.dispute, "created_at": TASK.created_at + timedelta(2)})
    early = TASK.model_copy(
        update={"id": "T-Z", "kind": TaskKind.dispute, "created_at": TASK.created_at + timedelta(1)}
    )
    deps = chat_deps(output_store=FakeOutputStore([JUDGMENT, late, TASK, early]))
    found = await list_open_items(ToolEnv(EID, deps, FixedState(view=make_overview(features=features)), 6000), NoArgs())
    assert [i["task_id"] for i in found.data["items"] if i["type"] == "review_task"] == expected


class PendingXero:
    """Xero 连接记录的替身: 一次待处理的组织信息核对 (名称为合成值)."""

    CHECK = XeroProfileCheck(
        key="XP-0123456789ab",
        xero_name="Numbat Electronics",
        xero_legal_name="Numbat Electronics Ltd",
        xero_abn=None,
        profile_version=1,
        profile_legal_name="Numbat Electronic Components Pty Ltd",
        profile_abn="",
        differences=["legal_name"],
        checked_at=NOW,
        next_version=2,
        stage="after",
    )

    async def source(self, eid: str) -> XeroSource:
        return XeroSource(status="connected", profile_check=self.CHECK)


async def test_open_items_put_xero_check_after_requests_before_tasks() -> None:
    """R291 Q7 a: Xero 组织信息核对排在补交资料之后, 复核任务之前 (同工作台), 带两边的名称与 ABN."""
    state = FixedState(view=make_overview(current_gate=GateId.FIN_01))
    tool_env = env(state=state, data=data_with_requests(), xero=PendingXero())
    _, data = await call("list_open_items", tool_env=tool_env)
    types = [i["type"] for i in data["items"]]
    assert types == ["gate_decision", "data_request", "data_request", "xero_profile", "review_task"]
    assert data["items"][3] == {
        "type": "xero_profile",
        "key": "XP-0123456789ab",
        "differences": ["legal_name"],
        "xero_legal_name": "Numbat Electronics Ltd",
        "xero_abn": None,
        "profile_version": 1,
        "profile_legal_name": "Numbat Electronic Components Pty Ltd",
        "profile_abn": "",
        "deferred": False,
    }


async def test_contracts_list_and_terms_with_pages() -> None:
    deps = chat_deps(financial_store=FakeFinancialStore(records=NUMBAT_RECORDS, contracts=numbat_contracts()))
    contract_env = ToolEnv(EID, deps, FixedState(), 6000)
    outcome, data = await call("get_contract", tool_env=contract_env)
    assert [c["id"] for c in data["contracts"]] == ["CON-001", "CON-002", "CON-003"]
    outcome, data = await call("get_contract", {"contract_id": "CON-001"}, tool_env=contract_env)
    assert outcome.ok and data["counterparty"] == "Bilby Robotics Pty Ltd"
    first = data["terms"][0]
    assert first["id"] == "CON-001-T1" and first["page"] >= 1 and first["text"]
    citation = outcome.cites["CON-001-T1"]
    assert citation.pointer is not None and citation.pointer.page == first["page"]
    assert citation.pointer.source_id == next(c.document_id for c in numbat_contracts() if c.id == "CON-001")
    outcome, data = await call("get_contract", {"contract_id": "CON-404"}, tool_env=contract_env)
    assert not outcome.ok and "CON-404" in data["error"]


async def test_gate_material_pseudonymises_nested_people() -> None:
    material = PlanningMaterial(
        analytics=AnalyticsResult(id="ANALYTICS", ratios={}, exception_ids=[]),
        analytics_exceptions=[],
        candidates=MaterialityCandidates(id="MATERIALITY", candidates=[]),
        objectives=[],
        risks=[
            RiskItem(
                id="R-1",
                objective_id="O-1",
                assertion="Occurrence",
                level="low",
                fraud_related=False,
                description="risk",
                approved_by="raw.person",
            )
        ],
        uncovered_objective_ids=[],
    )
    view = GateView(gate=GateId.PLN_01_03, target_id="MATERIALITY", target_version=1, material=material)
    outcome, data = await call("get_gate", tool_env=env(state=FixedState(gate_view=view)))
    assert outcome.ok and "raw.person" not in outcome.content
    assert data["material"]["risks"][0]["approved_by"] == pseudonym("raw.person")


async def test_same_person_gets_the_same_pseudonym_across_tools() -> None:
    _, row = await call("get_evidence", {"source_type": "journal_entry", "source_id": "CR-SI-2026-0547-1"})
    assert row["row"]["created_by"] == pseudonym("ar.clerk")  # 只化名一次, 不会被二次化名


def test_generated_amount_wins_regardless_of_key_order() -> None:
    first = minimize({"amount_cents": 12345, "amount_aud": "AUD 999.00"})
    second = minimize({"amount_aud": "AUD 999.00", "amount_cents": 12345})
    assert first["amount_aud"] == second["amount_aud"] == "AUD 123.45"


async def test_long_results_are_truncated() -> None:
    outcome, _ = await call("get_judgment", {"judgment_id": JUDGMENT.id}, tool_env=env(max_chars=200))
    assert outcome.ok
    assert len(outcome.content) < 300 and outcome.content.endswith("换更具体的查询]")


async def test_trail_search_excludes_conversation_and_newest_first() -> None:
    trail = FakeTrail()
    for kind, summary in [
        (TrailKind.llm_call, "REV-02 调用大模型"),
        (TrailKind.conversation, "审计师的问题"),
        (TrailKind.decision, "确认 REV-02"),
    ]:
        await trail.record(
            TrailEntry(engagement_id=EID, kind=kind, actor="U-1", procedure_id="REV-02", summary=summary)
        )
    outcome, data = await call("search_trail", {"procedure_id": "REV-02"}, tool_env=env(trail))
    assert [e["summary"] for e in data["entries"]] == ["确认 REV-02", "REV-02 调用大模型"]
    assert set(outcome.cites) == {"TR-3", "TR-1"}


def cite(cid: str, kind: Any = "judgment") -> ConciergeCitation:
    return ConciergeCitation(kind=kind, id=cid, label=cid)


def test_citations_need_boundaries_and_prefer_long_ids() -> None:
    cites = [cite("REV-02", "procedure"), cite("J-REV-02-1-SI-2026-0612"), cite("CON-001", "evidence")]
    pool = {c.id: c for c in cites}
    answer = "判断J-REV-02-1-SI-2026-0612认为收入记早了；合同条款见 CON-001-T1。程序REV-02还有 1 项待复核。"
    found = match_citations(answer, pool)
    assert [c.id for c in found] == ["J-REV-02-1-SI-2026-0612", "REV-02"]


def test_citations_only_from_pool_and_deduplicated() -> None:
    pool = {"SI-2026-0612": cite("SI-2026-0612", "evidence")}
    answer = "SI-2026-0612 与 SI-2026-0618 都在期末前后；SI-2026-0612 的发运单晚于期末。"
    assert [c.id for c in match_citations(answer, pool)] == ["SI-2026-0612"]
    assert match_citations("", pool) == [] and match_citations(answer, {}) == []


# ── 个人节点 ID 不带出生日期发给模型 (需求 R305) ──
# 合成的个人节点 ID (tests.fakes.ownership_graph: 姓名与出生日期都是编的); 守卫同时查两种日期写法

MORGAN, WHITFIELD = "PER-MORGAN-ALEX-19790412", "PER-WHITFIELD-SAM-NODOB"
BIRTH_FORMS = ("19790412", "1979-04-12")


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        (MORGAN, "PER-MORGAN-ALEX"),
        (WHITFIELD, WHITFIELD),  # 没有出生日期的不变
        ("PER-MADONNA--19580816", "PER-MADONNA-"),  # 单名
        (f"via {MORGAN}→ACN-900043021; key:{MORGAN}:2", "via PER-MORGAN-ALEX→ACN-900043021; key:PER-MORGAN-ALEX:2"),
        (f"node_{MORGAN}", "node_PER-MORGAN-ALEX"),  # 前面是下划线也算
        ("INV-19790412, XPER-A-B-19790412, PER-A-B-197904121", "INV-19790412, XPER-A-B-19790412, PER-A-B-197904121"),
    ],
    ids=["dated", "nodob", "single_name", "embedded", "underscore", "not_person_ids"],
)
def test_mask_person_node_ids(text: str, masked: str) -> None:
    assert mask_person_node_ids(text) == masked == mask_person_text(text)


def test_mask_person_node_ids_walks_keys_and_nested_values() -> None:
    """结构化版本遮字典的键与值, 列表与元组里的字符串, 其余原样, 不改入参."""
    data = {MORGAN: [MORGAN, (MORGAN, 3)], "n": 1.5, "nodob": WHITFIELD}
    assert mask_person_node_ids(data) == {
        "PER-MORGAN-ALEX": ["PER-MORGAN-ALEX", ("PER-MORGAN-ALEX", 3)],
        "n": 1.5,
        "nodob": WHITFIELD,
    }
    assert MORGAN in data


ESCAPED = ["关联方:\n", "列表\t", "a\r", "\u001f", "→", "董事", "'"]  # 序列化时会转义, 或紧邻 ID 的字符


@pytest.mark.parametrize("prefix", ESCAPED, ids=["newline", "tab", "cr", "control", "arrow", "hanzi", "quote"])
async def test_ids_after_escaped_characters_are_masked(prefix: str) -> None:
    """自由文本 (判断结论, 退回理由等) 里 ID 紧跟在换行, 制表符, 控制字符, 箭头, 汉字或单引号之后: 工具出口在序列化
    之前遮蔽, 发给模型的文本不含两种日期写法. 文本版对已序列化的文本 (ensure_ascii, 同 tojson) 也不漏, 防误用. 防
    按原文的边界去遮已序列化的文本: 转义 (\\n, \\uXXXX) 的末字符是字母数字, 边界不成立 (第 1 轮审核 R305-01)."""

    async def get_text(env: ToolEnv, args: NoArgs) -> Found:
        return Found({"conclusion": f"{prefix}{MORGAN} 任董事"})

    tools = ToolBox([Tool("get_text", "文本", NoArgs, get_text, {"zh": "查看", "en": "Text"})])
    outcome = await tools.call(env(), ToolCall(id="c", name="get_text", arguments="{}"))
    assert json.loads(outcome.content) == {"conclusion": f"{prefix}PER-MORGAN-ALEX 任董事"}
    for escaped in (json.dumps(f"{prefix}{MORGAN}"), json.dumps(f"{prefix}{MORGAN}", ensure_ascii=False)):
        assert not [form for form in BIRTH_FORMS if form in mask_person_text(escaped)], escaped


async def test_person_ids_reach_the_model_without_birth_dates() -> None:
    """关系图 (整体 model_dump 外发, 节点带 birth_date), 例外 detail 与 source_ids, 留痕摘要, 以及错误文本里的个人
    节点 ID, 经 ToolBox.call 出口统一处理: 发给模型的文本两种日期写法都没有, 带出生日期的 ID 换成 PER-MORGAN-ALEX,
    NODOB 的不变; 给前端的引用 (cites) 原样. 防哪个工具漏了遮蔽, 或只遮了 ID 而节点的 birth_date 照发."""
    graph = ownership_graph()

    async def get_graph(env: ToolEnv, args: NoArgs) -> Found:
        return Found(graph.model_dump(mode="json"))

    tools = ToolBox([*TOOLS, Tool("get_graph", "关系图", NoArgs, get_graph, {"zh": "查看关系图", "en": "Graph"})])
    exception = EXCEPTION.model_copy(
        update={
            "id": "X-GEN-08-1-R01-1",
            "procedure_id": "GEN-08",
            "source_ids": [MORGAN],
            "detail": {"via": [MORGAN, WHITFIELD]},
        }
    )
    trail = FakeTrail()
    await trail.record(
        TrailEntry(
            engagement_id=EID, kind=TrailKind.transition, actor="A5", procedure_id="GEN-08", summary=f"{MORGAN} 任董事"
        )
    )
    tool_env = env(trail)
    await tool_env.deps.output_store.put(EID, [exception], actor="A5")
    calls = [
        ("get_graph", {}),
        ("list_exceptions", {"procedure_id": "GEN-08"}),
        ("search_trail", {"procedure_id": "GEN-08"}),
        ("get_judgment", {"judgment_id": f"J-GEN-03-1-{MORGAN}"}),  # 查不到: 错误文本回显了编号
    ]
    outcomes = [
        await tools.call(tool_env, ToolCall(id="c", name=name, arguments=json.dumps(args))) for name, args in calls
    ]
    for (name, _), outcome in zip(calls, outcomes, strict=True):
        assert not [form for form in BIRTH_FORMS if form in outcome.content], name
        assert "PER-MORGAN-ALEX" in outcome.content, name
    graph_out, exceptions_out, trail_out, _ = (json.loads(o.content) for o in outcomes)
    assert [n["id"] for n in graph_out["nodes"]] == ["ACN-900043021", "PER-MORGAN-ALEX", WHITFIELD]
    assert all("birth_date" not in n for n in graph_out["nodes"])
    assert exceptions_out["exceptions"][0]["detail"] == {"via": ["PER-MORGAN-ALEX", WHITFIELD]}
    assert trail_out["entries"][0]["summary"] == "PER-MORGAN-ALEX 任董事"
    assert [c.label for c in outcomes[2].cites.values()] == [f"{MORGAN} 任董事"]  # 前端的引用原样


async def test_masking_comes_before_truncation() -> None:
    """先遮蔽再截断: 截断处落在出生日期中间时也不会留下半截日期 (先截断时 "...-1979" 会留下, 再遮蔽也认不出)."""

    async def get_person(env: ToolEnv, args: NoArgs) -> Found:
        return Found({"id": MORGAN})

    tools = ToolBox([Tool("get_person", "一个人", NoArgs, get_person, {"zh": "查看", "en": "Person"})])
    cut = len('{"id": "PER-MORGAN-ALEX-1979')  # 未遮蔽的全文在这里截断会留下 1979
    outcome = await tools.call(env(max_chars=cut), ToolCall(id="c", name="get_person", arguments="{}"))
    assert outcome.content == '{"id": "PER-MORGAN-ALEX"}'


async def test_registry_extract_file_name_is_not_sent() -> None:
    """GEN-08 存档的人员检索 PDF 文件名为 {姓}-{名}-{yyyymmdd}.pdf (Xero 补造的 MORGAN-ALEX-19790412.pdf), 不带 PER-
    前缀, 遮蔽认不出: get_evidence 对登记摘录类文件不发文件名. 模型照 X-GEN-08-1-R01-1 的证据 (list_exceptions) 再调
    get_evidence, 两次发给模型的文本都不含两种日期写法; 别的类型的文件照发文件名. R137 未收进来, 例外与文件是合成的."""
    person = DocumentRef(
        id="DOC-0123456789ab",
        type=DocumentType.registry_extract,
        file_name="MORGAN-ALEX-19790412.pdf",
        sha256="0" * 64,
        page_count=1,
        reliability=Reliability.external_original,
        status="extracted",
        uploaded_at=NOW,
    )
    letter = person.model_copy(
        update={"id": "DOC-ba9876543210", "type": DocumentType.engagement_letter, "file_name": "engagement_letter.pdf"}
    )
    pointer = EvidencePointer(
        type=EvidenceType.external,
        source_type="document",
        source_id=person.id,
        locator="document",
        page=1,
        reliability=Reliability.external_original,
    )
    exception = EXCEPTION.model_copy(
        update={"id": "X-GEN-08-1-R01-1", "procedure_id": "GEN-08", "source_ids": [MORGAN], "evidence": [pointer]}
    )
    deps = chat_deps(
        output_store=FakeOutputStore([exception]),
        financial_store=FakeFinancialStore(documents=[person, letter], records=NUMBAT_RECORDS),
    )
    tool_env = ToolEnv(EID, deps, FixedState(), 6000)
    _, listed = await call("list_exceptions", {"procedure_id": "GEN-08"}, tool_env)
    [evidence] = listed["exceptions"][0]["evidence"]
    outcome, found = await call(
        "get_evidence", {"source_type": "document", "source_id": evidence["source_id"]}, tool_env
    )
    _, other = await call("get_evidence", {"source_type": "document", "source_id": letter.id}, tool_env)
    assert outcome.ok and not [form for form in BIRTH_FORMS if form in outcome.content + json.dumps(listed)]
    assert (found["id"], found["type"], "file_name" in found) == (person.id, "registry_extract", False)
    assert other["file_name"] == "engagement_letter.pdf"
