"""需求 R310: 判断 (A6) 与独立复核 (A7) 发给模型的内容不含出生日期.

Xero 演示的关系图取 GEN-08 在补造登记上的结果 (tests/test_evidence_gen08.run_gen08), 其余为 Xero 演示的假存储; 大模型
为读 Xero fixture 正本的 mock (没有 fixture 的调用失败, 提示词照样记进 llm_call 留痕). 出生日期按关系图里有出生日期的
人员 (Alex, Dana) 查 ISO, yyyymmdd 与摘录上的长日期三种写法, 另查带出生日期的人员节点 ID 原文.
"""

import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from backend.adapters.llm import MockLLMClient
from backend.agents.base import RunContext
from backend.agents.fieldwork import FieldworkAgent
from backend.agents.review import GEN09, ReviewAgent
from backend.contracts.data import REGISTRY_TARGET
from backend.contracts.models import (
    AgentId,
    AutonomyLevel,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    Judgment,
    OwnershipGraph,
    ProposedAction,
    ReasoningStep,
    StepKind,
    TrailKind,
)
from backend.contracts.services import JudgmentContext
from backend.contracts.state import EngagementState
from backend.orchestration.nodes import apply_patch
from tests.external import needs
from tests.fakes import FakeRuleEngine, FakeTrail
from tests.fixture_sets import XERO
from tests.test_evidence_gen08 import VPA, run_gen08
from tests.test_xero_fixtures import LIBRARY, RecordingJudgments, xero_context, xero_state
from tests.xero_fakes import XERO_RULES, make_xero_deps, manifest_documents, xero_profile, xero_registry

FEATURES = frozenset({"critic", "related_parties"})
RAW_PERSON_ID = re.compile(r"PER-[A-Z0-9]+-[A-Z0-9]*-[0-9]{8}")  # 带出生日期的人员节点 ID 原文


def birth_date_forms(graph: OwnershipGraph) -> list[str]:
    """关系图中各人出生日期的三种写法: ISO, yyyymmdd (人员节点 ID 与人员检索的文件名), 摘录上的长日期."""
    born = [n.birth_date for n in graph.nodes if n.birth_date is not None]
    assert len(born) == 2  # Alex 与 Dana
    return [form for d in born for form in (d.isoformat(), d.strftime("%Y%m%d"), f"{d.day} {d:%B} {d.year}")]


def leaks(messages: list[str], graph: OwnershipGraph) -> list[str]:
    """消息里出现的出生日期写法与人员节点 ID 原文."""
    forms = birth_date_forms(graph)
    return sorted({f for text in messages for f in [*forms, *RAW_PERSON_ID.findall(text)] if f in text})


def sent(trail: FakeTrail, actor: AgentId) -> list[str]:
    """该 Agent 调大模型时发出的全部消息 (各次尝试, 含重试时追加的)."""
    return [
        m["content"]
        for e in trail.entries
        if e.kind is TrailKind.llm_call and e.actor == actor
        for m in e.payload["messages"]
    ]


async def graph_state(deps: Any, trail: FakeTrail, related_to_all: bool = False) -> EngagementState:
    """Xero 进入 P2 的状态并入 GEN-08 的片段 (查询留痕写进 trail); GEN-08 存档的登记 PDF 记进财务存储的文件登记 (同
    真实的 EvidenceStore.save). related_to_all 为真时这些登记另带关联对象: 关系图的全部节点与客户 (测试里合成的
    补登记, 让按关联对象取文件的上下文都取得到摘录)."""
    gen08, _, _, _ = await run_gen08(xero_registry(), xero_profile(), xero=True, trail=trail)
    graph = gen08["ownership_graph"]
    related = [*(n.id for n in graph.nodes), xero_profile().id] if related_to_all else []
    extracts = [d for d in gen08["documents"] if d.type is DocumentType.registry_extract]
    deps.financial_store.document_refs += [d.model_copy(update={"related_ids": related}) for d in extracts]
    return apply_patch(xero_state(), gen08)  # type: ignore[arg-type]


def extract_of(acn: str) -> str:
    [found] = [i for i, d in manifest_documents().items() if d["path"] == f"registry/extracts/{acn}.pdf"]
    return found


# ── A7: 登记摘录页与 GEN-08 查询留痕 (验收 1) ──


@needs("q1_snapshot", "q1_supplements")
async def test_critic_gets_registry_content_without_birth_dates() -> None:
    """验收 1: A7 复核一条引用 VPA 摘录第 1, 2 页与 Alex 人员检索留痕的 GEN-03 判断: 发给复核模型的消息不含出生日期
    (三种写法) 与人员节点 ID 原文; 仍有判断要用的内容: 高管的姓名, 职务与任职日, 股东的持股, 人员检索到的任职与持股.
    摘录给的是 GEN-08 查询记下的结构化结果, 不是页面文字 (页上印有长日期的出生日期); 留痕的明细去掉了出生日期与
    存档文件名 (人员检索的文件名带 yyyymmdd)."""
    trail = FakeTrail()
    client = MockLLMClient(XERO.fixtures)  # GEN-09 没有这条判断的 fixture: 两轮都失败, 消息照样记下
    deps = make_xero_deps(trail=trail, llm=client, review_llm=client, features=FEATURES)
    state = await graph_state(deps, trail)
    ctx = xero_context(deps, state, "GEN-03", [])
    [search] = [e for e in trail.entries if e.kind is TrailKind.external_call and "family_name" in e.payload]
    vpa = extract_of("900043030")
    pointers = [
        EvidencePointer(
            type="external", source_type="document", source_id=vpa, locator=locator, page=page, reliability=None
        )
        for locator, page in (("officeholders[1]", 1), ("shareholders[1]", 2))
    ]
    pointers.append(
        EvidencePointer(type="external", source_type="trail", source_id=search.id, locator="result", reliability=None)
    )
    judgment = Judgment(
        id=f"J-GEN-03-1-{VPA}",
        procedure_id="GEN-03",
        run_id=ctx.run.id,
        subject_id=VPA,
        conclusion="Victoria Property Agency Pty Ltd is a related party that management has not declared.",
        confidence=0.88,
        confidence_parts={"llm": 0.85},
        reasoning_chain=[
            ReasoningStep(kind=StepKind.fact, text="Alex Morgan is a director of VPA.", evidence=pointers)
        ],
        evidence=pointers,
        counter_argument="none",
        proposed_action=ProposedAction.more_evidence,
        autonomy_level=AutonomyLevel.L3,
        performed_by="U-1",
        created_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    reviewed = await ReviewAgent(deps).cross_check(ctx, deps.library.load(GEN09), judgment)
    assert reviewed.critic_verdict == "not_applicable"  # 没有 fixture, 复核没做成; 只看发出的消息
    messages = sent(trail, AgentId.A7)
    assert messages
    graph = state["ownership_graph"]
    assert graph is not None
    assert leaks(messages, graph) == []
    first = messages[0]
    for text in (
        '"name": "Alex Morgan"',
        '"role": "director"',
        '"appointed": "2014-03-03"',  # VPA 摘录: Alex 任董事
        '"shares": 50',  # VPA 摘录: Alex 持 50 股
        '"company_name": "Morgan Advisory Pty Ltd"',  # 人员检索
        '"family_name": "Morgan"',
    ):
        assert text in first, text


@needs("q1_supplements")
async def test_uploaded_extract_content_is_its_latest_extraction() -> None:
    """登记摘录是审计师上传而由 A3 抽取的 (需求 R290) 时, 复核拿到的是抽取版本表里最新一版的结构化结果, 去掉出生日期与
    文件名; 不读页面文字, 也不找查询留痕. 两版抽取与抽取结果都是测试里合成的."""
    deps = make_xero_deps(features=FEATURES)
    document = DocumentRef(
        id="DOC-SYNTHETIC-ASIC",
        type=DocumentType.registry_extract,
        file_name="MORGAN-ALEX-19790412.pdf",
        sha256="0" * 64,
        page_count=2,
        reliability="external_copy",
        status="extracted",
        uploaded_at=datetime(2026, 10, 20, tzinfo=UTC),
    )
    deps.financial_store.document_refs.append(document)
    officer = {"name": "Alex Morgan", "birth_date": "1979-04-12", "role": "director", "appointed": "2018-09-03"}
    for version, name in ((1, "Old Name Pty Ltd"), (2, "Demo Company (AU) Pty Ltd")):
        await deps.data.save_extraction(
            "ENG-X",
            REGISTRY_TARGET,
            document.id,
            version,
            {"acn": "900043021", "name": name, "officeholders": [officer], "file_name": document.file_name},
            changed_by=AgentId.A3,
        )
    pointer = EvidencePointer(type="external", source_type="document", source_id=document.id, locator="", page=1)
    content = await ReviewAgent(deps).evidence_content("ENG-X", pointer)
    assert content == {
        "acn": "900043021",
        "name": "Demo Company (AU) Pty Ltd",
        "officeholders": [{"name": "Alex Morgan", "role": "director", "appointed": "2018-09-03"}],
    }


@needs("q1_snapshot", "q1_supplements")
async def test_engine_drops_birth_date_fields_before_rendering() -> None:
    """判断引擎渲染提示词前去掉 birth_date 字段, 防程序把关系图节点或摘录高管整体 dump 进上下文时带出出生日期: GEN-03
    对 VPA 的上下文里 persons 换成关系图人员节点的整体 dump (带 birth_date 与人员节点 ID 原文, 测试里改的), 再交真实
    判断引擎, 发出的消息不含出生日期与 ID 原文, 仍有人员的姓名."""
    trail = FakeTrail()
    deps = make_xero_deps(trail=trail, features=FEATURES, rules=FakeRuleEngine(XERO_RULES), review_llm=None)
    recording = RecordingJudgments(deps.judgments)
    deps = replace(deps, judgments=recording)
    state = await graph_state(deps, trail)
    ctx = xero_context(deps, state, "GEN-03", [])
    await FieldworkAgent(deps).run(ctx)
    graph = state["ownership_graph"]
    assert graph is not None
    [vpa] = [c for c in recording.contexts if c.subject_id == VPA]
    nodes = [n.model_dump(mode="json") for n in graph.nodes if n.kind == "person"]
    assert any(n["birth_date"] for n in nodes)
    before = len(trail.entries)
    await recording.inner.judge(ctx.engagement.id, ctx.template, ctx.run, replace_data(vpa, persons=nodes), "U-1")
    messages = [
        m["content"] for e in trail.entries[before:] if e.kind is TrailKind.llm_call for m in e.payload["messages"]
    ]
    assert messages and leaks(messages, graph) == []
    assert '"name": "Dana Morgan"' in messages[0]


def replace_data(jctx: JudgmentContext, **fields: Any) -> JudgmentContext:
    return jctx.model_copy(update={"data": {**jctx.data, **fields}})


# ── A6 与 A7: 读关系图的判断程序 (验收 2) ──


def needs_graph(procedure_id: str) -> bool:
    """该程序的依赖闭包含 GEN-08 (读关系图, 上下文会有人员)."""
    depends_on = LIBRARY.load(procedure_id).depends_on
    return "GEN-08" in depends_on or any(needs_graph(p) for p in depends_on)


GRAPH_PROGRAMS = sorted(t.id for t in LIBRARY.all(include_disabled=True) if t.llm is not None and needs_graph(t.id))


def test_graph_programs_are_listed() -> None:
    """守卫自身: 读关系图的判断程序按依赖闭包自动列出, 现为 GEN-03; 之后的 GEN-01 等加进程序库即进下一例受查."""
    assert "GEN-03" in GRAPH_PROGRAMS and "GEN-08" not in GRAPH_PROGRAMS and "REV-02" not in GRAPH_PROGRAMS


@needs("q1_snapshot", "q1_supplements")
@pytest.mark.parametrize("procedure_id", GRAPH_PROGRAMS)
async def test_graph_programs_send_no_birth_dates(procedure_id: str) -> None:
    """验收 2 (守卫): 读关系图的每个判断程序 (GRAPH_PROGRAMS) 在 Xero 演示上执行一次, 防新程序把出生日期发给模型:
    先按依赖顺序执行它依赖的读关系图的程序, 运行时自主度都提到 L3 (A7 按主规则复核建议 accept 的判断); 四份登记摘录
    补登记关联到关系图的全部节点与客户 (合成的登记, 让按关联对象取文件的上下文都取得到). A6 与 A7 发给模型的消息都
    不含出生日期 (三种写法) 与人员节点 ID 原文. 用的是正本 fixture, 没有 fixture 的调用失败也照样查消息."""
    trail = FakeTrail()
    client = MockLLMClient(XERO.fixtures)
    deps = make_xero_deps(
        trail=trail, llm=client, review_llm=client, features=FEATURES, rules=FakeRuleEngine(XERO_RULES)
    )
    state = await graph_state(deps, trail, related_to_all=True)
    graph = state["ownership_graph"]
    assert graph is not None
    order = [p for p in GRAPH_PROGRAMS if p != procedure_id and p in closure(procedure_id)] + [procedure_id]
    for pid in order:
        ctx = xero_context(deps, state, pid, [])
        procedure = ctx.procedure.model_copy(update={"autonomy_level": AutonomyLevel.L3})
        ctx = RunContext(ctx.engagement, procedure, ctx.template, ctx.run, state | {"procedure_pack": [procedure]})
        state = apply_patch(state, await FieldworkAgent(deps).run(ctx))  # type: ignore[arg-type]
    a6, a7 = sent(trail, AgentId.A6), sent(trail, AgentId.A7)
    assert a6 and a7  # 两边都发了消息, 守卫查得到东西
    assert leaks([*a6, *a7], graph) == []


def closure(procedure_id: str) -> set[str]:
    """依赖闭包 (不含自身)."""
    found: set[str] = set()
    for p in LIBRARY.load(procedure_id).depends_on:
        found |= {p, *closure(p)}
    return found
