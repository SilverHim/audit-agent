"""需求 R139: A6 GEN-03 关联方图谱研判的判断对象与上下文 (功能 related_parties).

状态里的关系图与 R01 例外取 GEN-08 在 Xero 补造登记上的结果 (tests/test_evidence_gen08.run_gen08), 其余为 Xero 演示的
假存储 (make_xero_deps); 规则引擎为预置结果的 FakeRuleEngine (XERO_RULES 的 GEN-03 取自连库跑一次的结果, 连库一致与
"取整笔凭证" 见 tests/test_gen03_sql.py). 判断引擎缺省为 FakeJudgmentEngine; 渲染出的提示词用真实引擎与 mock 大模型
(GEN-03 还没有 fixture, 每个对象 3 次尝试都失败, 提示词全文在 llm_call 留痕里).
"""

from dataclasses import replace
from typing import Any

import pytest

from backend.agents.base import RunContext
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.registry import build_agents
from backend.contracts.models import (
    AgentId,
    Decision,
    DocumentType,
    EvidencePointer,
    OwnershipGraph,
    PartyLink,
    PartyNode,
    RunStatus,
    TaskKind,
    TrailKind,
)
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState
from backend.orchestration.merge import check_patch
from backend.orchestration.nodes import apply_patch
from tests.external import needs
from tests.fakes import FakeJudgmentEngine, FakeRegistry, FakeRuleEngine, FakeTrail, make_deps
from tests.test_evidence_gen08 import ADVISORY, ALEX, DEMO, SMART, VPA, run_gen08
from tests.test_fieldwork_judging import failed, passed
from tests.test_xero_fixtures import xero_context, xero_state
from tests.xero_fakes import (
    XERO_RULES,
    make_xero_deps,
    manifest_documents,
    xero_contracts,
    xero_financial_store,
    xero_gen03_rows,
    xero_profile,
    xero_registry,
)

FEATURES = frozenset({"critic", "related_parties"})
DANA = "PER-MORGAN-DANA-19811103"
SUBJECTS = [VPA, ADVISORY]  # 关系图中报告主体以外的公司节点, 按 ID
RENT = ("XJ-420", "XJ-518", "XJ-593")  # VPA 的三笔房租 (作废对 XJ-415/417 已由 SQL 剔除)


async def gen03_context(
    deps: Any, graph: OwnershipGraph | None = None, state: dict[str, Any] | None = None
) -> RunContext:
    """GEN-03 第 1 次执行的上下文: Xero 进入 P2 的状态并入 GEN-08 的片段 (关系图, R01 例外, 登记 PDF); graph 给出时
    换掉关系图, state 再覆盖状态字段."""
    gen08, _, _, _ = await run_gen08(xero_registry(), xero_profile(), xero=True)
    merged: EngagementState = apply_patch(xero_state(), gen08)  # type: ignore[arg-type]
    if graph is not None:
        merged = merged | {"ownership_graph": graph}  # type: ignore[assignment]
    return xero_context(deps, merged | (state or {}), "GEN-03", [])  # type: ignore[arg-type]


def gen03_deps(
    outcomes: dict[str, Any] | None = None, rules: RuleResult | None = None, real: bool = False, **kwargs: Any
) -> tuple[Any, FakeJudgmentEngine, FakeRuleEngine]:
    """功能 related_parties 打开的 Xero 依赖: 规则引擎 GEN-03 返回 rules (缺省 XERO_RULES 的预置); 判断引擎缺省为假
    引擎 (outcomes 缺省两个对象都通过), real 为真时用真实引擎 (mock 大模型), 返回的假引擎不被调用."""
    engine = FakeJudgmentEngine(outcomes if outcomes is not None else {s: passed() for s in SUBJECTS})
    preset = XERO_RULES["GEN-03"] if rules is None else (lambda run: rules)
    rule_engine = FakeRuleEngine({"GEN-03": preset})
    deps = make_xero_deps(rules=rule_engine, features=FEATURES, **kwargs)
    if not real:
        deps = replace(deps, judgments=engine, review_llm=None)
    return deps, engine, rule_engine


def contexts(engine: FakeJudgmentEngine) -> dict[str, Any]:
    return {jctx.subject_id: jctx for _, jctx, _ in engine.calls}


def ids(rows: list[dict[str, Any]]) -> list[str]:
    return [r["id"] for r in rows]


# ── 程序定义与分派 ──


def test_gen03_template_and_dispatch() -> None:
    """验收 1: GEN-03.yaml 的各项; 只在功能 related_parties 打开时启用, 映射到 FieldworkAgent; 基线不启用."""
    deps = make_deps(FakeRegistry(), FakeTrail(), features=FEATURES)
    t = deps.library.load("GEN-03")
    assert (t.owner, t.engine, t.autonomy_level, t.cycle, t.fixed, t.feature, t.manual_result_allowed) == (
        AgentId.A6,
        "llm",
        "L2",
        "Pervasive",
        True,
        "related_parties",
        True,
    )
    assert (t.outputs, t.depends_on) == (["judgments", "exceptions", "review_tasks"], ["DOC-04", "GEN-08"])
    assert t.standards == ["ASA550-P10", "ASA550-P21", "ASA550-P22", "ASA550-P23"]
    assert t.evidence_required == ["external", "contract"]
    assert t.params == {
        "period_start": {"ref": "period_start"},
        "period_end": {"ref": "period_end"},
        "subsequent_days": 30,
        "reversal_reference_pattern": {"ref": "dataset.reversal_reference_pattern"},
        "reversal_entry_no_template": {"ref": "dataset.reversal_entry_no_template"},
        "counterparty_prefixes": {"ref": "dataset.counterparty_prefixes"},
        "entity_suffixes": ["pty ltd", "pty limited", "pty", "ltd", "limited"],
        "equity_categories": ["share_capital", "retained_earnings", "other_equity"],
    }
    assert [(r.id, r.emit) for r in t.rules] == [("C01", "rows"), ("C02", "rows")]
    assert t.llm is not None and (t.llm.prompt, t.llm.output_model) == ("GEN-03.md.j2", "JudgmentDraft")
    assert "GEN-03" in FieldworkAgent.procedures and isinstance(build_agents(deps)["GEN-03"], FieldworkAgent)
    baseline = make_deps(FakeRegistry(), FakeTrail())
    assert not baseline.library.enabled("GEN-03") and "GEN-03" not in build_agents(baseline)


# ── Xero 演示 ──


@needs("q1_snapshot", "q1_supplements")
async def test_xero_subjects_and_context() -> None:
    """验收 3, 4, 5, 8: 对象为 VPA 与 Morgan Advisory (按 ID, fixture_key 同), 规则参数为参数快照加两家的名称与 ACN
    (快照不变); data 各项如下; 两条判断都建 normal 任务. JudgmentContext 带本项目采用的画像."""
    deps, engine, rules = gen03_deps()
    ctx = await gen03_context(deps)
    patch = await FieldworkAgent(deps).run(ctx)
    check_patch(
        patch,
        writer=AgentId.A6,
        procedure_id="GEN-03",
        run_id=ctx.run.id,
        state=ctx.state,
        outputs=ctx.template.outputs,
    )

    assert [(jctx.subject_id, jctx.fixture_key) for _, jctx, _ in engine.calls] == [(s, s) for s in SUBJECTS]
    snapshot = ctx.run.params_snapshot
    parties = {"party_names": ["Victoria Property Agency Pty Ltd", "Morgan Advisory Pty Ltd"]}
    assert rules.calls == [("GEN-03", ctx.run.id, snapshot | parties | {"party_acns": ["900043030", "900043049"]})]
    assert "party_names" not in snapshot and "party_names" not in patch["procedure_runs"][0].params_snapshot
    found = contexts(engine)
    assert {s: found[s].client_profile for s in SUBJECTS} == {s: xero_profile() for s in SUBJECTS}

    vpa = found[VPA].data
    assert vpa["entity"] == {"name": "Victoria Property Agency Pty Ltd", "acn": "900043030"}
    assert vpa["reporting_entity"]["id"] == DEMO
    assert vpa["client_profile"] == {"id": xero_profile().id, "applicable_framework": "tier2_gpfs"}
    assert [(x["from_id"], x["relation"], x["pct"]) for x in vpa["links"]] == [
        (DANA, "director", None),
        (ALEX, "director", None),
        (DANA, "shareholder", 0.5),
        (ALEX, "shareholder", 0.5),
    ]
    assert all(x["to_id"] == VPA and x["evidence"]["source_type"] == "document" for x in vpa["links"])
    assert vpa["persons"] == [
        {"id": ALEX, "name": "Alex Morgan", "relations_with_entity": ["director", "secretary", "shareholder 0.85"]},
        {"id": DANA, "name": "Dana Morgan", "relations_with_entity": []},
    ]
    assert vpa["declared"] is None
    entries = vpa["transactions"]["entries"]
    assert [x["entry_no"] for x in entries] == [no for no in RENT for _ in range(3)]
    assert [(x["account_code"], x["debit_cents"]) for x in entries if x["account_code"] == "469"] == [
        ("469", 150000)
    ] * 3
    assert {x["source_type"] for x in entries} == {"journal_entry"}
    banks = vpa["transactions"]["bank_lines"]
    assert [(x["id"], x["amount_cents"]) for x in banks] == [(f"BT-090-{n}", -165000) for n in ("0004", "0033", "0058")]
    assert vpa["transactions"]["invoices"] == [] and vpa["equity_entries"] == [] and vpa["contracts"] == []
    paths = {i: d["path"] for i, d in manifest_documents().items()}
    assert sorted(paths[d["document"]["id"]] for d in vpa["documents"]) == [
        "documents/related_party/harbourview_lease.pdf",
        "documents/related_party/vpa_rent_letter.pdf",
        "documents/related_party/vpa_sale_termination_notice.pdf",
    ]
    assert all(len(d["pages"]) == d["document"]["page_count"] and d["pages"][0] for d in vpa["documents"])
    assert [x.id for x in found[VPA].rule_findings] == ["X-GEN-08-1-R01-1"]

    advisory = found[ADVISORY].data
    [party] = [p for p in xero_profile().related_parties if p.name == "Morgan Advisory Pty Ltd"]
    assert advisory["declared"] == party.model_dump(mode="json")
    assert advisory["transactions"] == {"entries": [], "bank_lines": [], "invoices": []}
    assert advisory["documents"] == [] and found[ADVISORY].rule_findings == []
    assert [p["id"] for p in advisory["persons"]] == [ALEX]

    assert [(t.id, t.kind) for t in patch["review_tasks"]] == [
        (f"T-J-GEN-03-1-{s}-v1", TaskKind.normal) for s in SUBJECTS
    ]
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


@needs("q1_snapshot", "q1_supplements")
async def test_failure_task_and_rerun_of_that_subject() -> None:
    """验收 8: VPA 判断失败建 T-RUN-GEN-03-1-ACN-900043030-v1 (failed, 可 rerun 或 manual_result), Morgan Advisory 照常;
    只重判 VPA 时规则参数只带 VPA (先按 in_scope 筛选, 再取数)."""
    deps, _, _ = gen03_deps({VPA: failed("failed"), ADVISORY: passed()})
    ctx = await gen03_context(deps)
    patch = await FieldworkAgent(deps).run(ctx)
    tasks = [(t.id, t.kind, t.subject_id) for t in patch["review_tasks"]]
    assert tasks == [
        ("T-RUN-GEN-03-1-ACN-900043030-v1", TaskKind.failed, VPA),
        ("T-J-GEN-03-1-ACN-900043049-v1", TaskKind.normal, None),
    ]
    assert deps.queue.allowed(patch["review_tasks"][0]) == {Decision.rerun, Decision.manual_result}

    deps, engine, rules = gen03_deps()
    ctx = await gen03_context(deps)
    procedure = ctx.procedure.model_copy(update={"rerun_subject_ids": [VPA]})
    await FieldworkAgent(deps).run(RunContext(ctx.engagement, procedure, ctx.template, ctx.run, ctx.state))
    assert [jctx.subject_id for _, jctx, _ in engine.calls] == [VPA]
    assert (rules.calls[0][2]["party_names"], rules.calls[0][2]["party_acns"]) == (
        ["Victoria Property Agency Pty Ltd"],
        ["900043030"],
    )


@needs("q1_snapshot", "q1_supplements")
async def test_no_ownership_graph_raises() -> None:
    """验收 3: 状态没有关系图 (GEN-08 未执行) 时抛 LookupError, 不调规则与判断引擎."""
    deps, engine, rules = gen03_deps()
    ctx = await gen03_context(deps, state={"ownership_graph": None})
    with pytest.raises(LookupError, match="关系图"):
        await FieldworkAgent(deps).execute(ctx)
    assert engine.calls == [] and rules.calls == []


@needs("q1_snapshot", "q1_supplements")
async def test_t6_isomorphic_case_takes_rows_by_party_key() -> None:
    """验收 6 (同构于注入场景 T6, 不改任何文件): 关系图加合成的 SMART Agency Pty Ltd 节点 (Alex 任董事), C01 预置为
    连库对 VPA 与 SMART 的结果: SMART 的 entries 为 XJ-548 与 XJ-641 的全部行 (含说明为物品描述的 XJ-548-2, 181818),
    bank_lines 为 BT-090-0097; VPA 的行不变. 行按 party_key 切给节点."""
    gen08, _, _, _ = await run_gen08(xero_registry(), xero_profile(), xero=True)
    graph = gen08["ownership_graph"]
    synthetic = EvidencePointer(  # 合成的摘录行指针 (T6 同构, 不对应任何文件)
        type="external",
        source_type="document",
        source_id="DOC-SYNTHETIC-SMART",
        locator="officeholders[0]",
        page=1,
        reliability="external_original",
    )
    smart = PartyNode(id=SMART, kind="company", name="SMART Agency Pty Ltd", acn="900043076", searched=True)
    graph = graph.model_copy(
        update={
            "nodes": sorted([*graph.nodes, smart], key=lambda n: n.id),
            "links": [*graph.links, PartyLink(from_id=ALEX, to_id=SMART, relation="director", evidence=synthetic)],
        }
    )
    rows = RuleResult(exceptions=[], rows={"C01": xero_gen03_rows(["smart agency", "victoria property agency"])})
    deps, engine, _ = gen03_deps({s: passed() for s in [*SUBJECTS, SMART]}, rules=rows)
    await FieldworkAgent(deps).run(await gen03_context(deps, graph))
    found = contexts(engine)
    assert list(found) == [VPA, ADVISORY, SMART]
    entries = found[SMART].data["transactions"]["entries"]
    assert ids(entries) == [f"{no}-{n}" for no in ("XJ-548", "XJ-641") for n in (1, 2, 3)]
    [design] = [x for x in entries if x["id"] == "XJ-548-2"]
    assert (design["account_code"], design["debit_cents"]) == ("400", 181818)
    assert "smart" not in design["description"].lower()
    assert ids(found[SMART].data["transactions"]["bank_lines"]) == ["BT-090-0097"]
    assert [x["entry_no"] for x in found[VPA].data["transactions"]["entries"]] == [no for no in RENT for _ in range(3)]
    assert found[SMART].data["persons"] == [
        {"id": ALEX, "name": "Alex Morgan", "relations_with_entity": ["director", "secretary", "shareholder 0.85"]}
    ]


@needs("q1_snapshot", "q1_supplements")
async def test_contracts_and_equity_entries_by_node() -> None:
    """验收 4 的 contracts 与 equity_entries: 合同对方 ABN (带空格) 的末 9 位等于节点 ACN, 或对方名称去后缀后等于节点
    名称的才给该节点 (带合同文件登记); 节点直接持有报告主体股份时才给 C02 的行. 合同的改动与持股关系都是测试里合成的."""
    contracts = xero_contracts()
    by_abn = contracts[0].model_copy(update={"counterparty_abn": "17 900 043 049"})
    by_name = contracts[1].model_copy(update={"counterparty": "Victoria Property Agency", "counterparty_abn": None})
    financial = xero_financial_store()
    financial.saved_contracts = {c.id: c for c in [by_abn, by_name, contracts[2]]}
    equity = [{"source_type": "journal_entry", "id": "SYN-1-1", "posting_date": "2026-09-30", "account_code": "970"}]
    rows = RuleResult(exceptions=[], rows={"C01": [], "C02": equity})
    gen08, _, _, _ = await run_gen08(xero_registry(), xero_profile(), xero=True)
    graph = gen08["ownership_graph"]
    holding = graph.links[3].model_copy(update={"from_id": ADVISORY, "shares": 10, "pct": 0.1})  # 合成: 持股 10%
    graph = graph.model_copy(update={"links": [*graph.links, holding]})
    deps, engine, _ = gen03_deps(rules=rows, financial_store=financial)
    await FieldworkAgent(deps).run(await gen03_context(deps, graph))
    found = contexts(engine)
    assert [c["contract"]["id"] for c in found[ADVISORY].data["contracts"]] == [by_abn.id]
    assert [c["contract"]["id"] for c in found[VPA].data["contracts"]] == [by_name.id]
    documents = {d.id: d for d in financial.document_refs}
    assert found[VPA].data["contracts"][0]["document"] == documents[by_name.document_id].model_dump(mode="json")
    assert (found[ADVISORY].data["equity_entries"], found[VPA].data["equity_entries"]) == (equity, [])


@needs("q1_snapshot", "q1_supplements")
async def test_rendered_prompt_has_no_birth_date() -> None:
    """验收 7: 真实判断引擎渲染出的提示词 (llm_call 留痕) 不含任何人员的出生日期, ISO 与 yyyymmdd 两种写法都查 (关系图
    的端点与 R01 例外的 via 都带人员节点 ID, 渲染前已遮蔽为 PER-MORGAN-ALEX), 另查摘录页上的长日期写法: GEN-08 存档的
    登记摘录补登记了关联对象 VPA (测试里合成的登记) 也不进 documents. Standards 段含 ASA550-P10, ASA550-P22 原文; 写明
    适用框架取画像 (tier2_gpfs) 与 AASB 1060, AASB 124 的用法."""
    trail = FakeTrail()
    deps, _, _ = gen03_deps(real=True, trail=trail)
    ctx = await gen03_context(deps)
    extracts = [d for d in ctx.state["documents"] if d.type is DocumentType.registry_extract]
    assert len(extracts) == 4  # 三份公司摘录与一份人员检索
    deps.financial_store.document_refs += [d.model_copy(update={"related_ids": [VPA]}) for d in extracts]
    await FieldworkAgent(deps).run(ctx)
    calls = [e.payload for e in trail.entries if e.kind is TrailKind.llm_call]
    prompts = {c["fixture_key"]: c["messages"][0]["content"] for c in calls if c["attempt"] == 1}
    assert sorted(prompts) == SUBJECTS
    graph = ctx.state["ownership_graph"]
    assert graph is not None
    born = [n.birth_date for n in graph.nodes if n.birth_date is not None]
    assert len(born) == 2  # Alex 与 Dana
    dates = [form for d in born for form in (d.isoformat(), d.strftime("%Y%m%d"), f"{d.day} {d:%B} {d.year}")]
    sent = [(c["fixture_key"], m["content"]) for c in calls for m in c["messages"]]  # 含重试时追加的各条
    assert [(key, d) for key, text in sent for d in dates if d in text] == []
    vpa = prompts[VPA]
    assert "PER-MORGAN-ALEX" in vpa and "X-GEN-08-1-R01-1" in vpa
    for fragment_id in ("ASA550-P10", "ASA550-P22"):
        fragment = deps.library.fragment(fragment_id)
        assert fragment is not None and fragment.text.strip() in vpa
    assert "tier2_gpfs" in vpa and "AASB 1060" in vpa and "AASB 124" in vpa
