"""智能体协作 (功能 collab) 下的 Numbat 全流程 (真实 Agent 与 mock 输出): 争议变体 (复核模型取 llm_variants/dispute)
中 CSH-02 为争议中, 争议任务在待办里单列在前 (R151); 仲裁补全: 复核方的建议动作与错报草稿, 采纳方建议错报时须带
草稿, 系统外咨询 (R150).

全流程较慢, 同一次运行的结果供多条断言共用. 主流程 (结果同基线, A7 记下命中的触发规则, R149 验收 4) 与跑到归档的
争议变体见 test_collab_flow.py (R155); 争议仲裁的基线行为见 test_dispute.py, 触发规则的单项见 test_review_agent.py.
"""

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.llm import MockLLMClient
from backend.agents.concierge_chat.tools import NoArgs, ToolEnv, list_open_items
from backend.contracts.errors import DecisionRejected
from backend.contracts.models import (
    Assertion,
    AuditException,
    CriticTrigger,
    CriticVerdict,
    Decision,
    Direction,
    DisputeResolution,
    ExceptionDisposition,
    Judgment,
    Misstatement,
    MisstatementDraft,
    MisstatementType,
    NeitherAction,
    Procedure,
    ProcedureStatus,
    ProposedAction,
    ReviewRecord,
    ReviewStatus,
    ReviewTask,
    TaskKind,
    TrailKind,
)
from backend.core.config import get_settings
from backend.orchestration.decisions import dispute_effects
from backend.orchestration.query import GraphStateQuery
from backend.services.features import BASELINE
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import ENGAGEMENT, NUMBAT_CUTOFF_DRAFT, LayeredMockLLM, api_client, wait_idle
from tests.test_decisions import Started
from tests.test_dispute import JID, TASK, VARIANTS, resolve
from tests.test_e2e_mock import start_demo, trail_of
from tests.test_flow import REVIEWS
from tests.test_review_decisions import record

EID = ENGAGEMENT.id
COLLAB = BASELINE | {"collab"}


# ── 争议变体: R151 争议中与争议单列, R150 仲裁补全 (复核方的建议与草稿, 草稿随采纳的一方, 系统外咨询) ──

REV03 = ["T-J-REV-03-1-CON-001-v1", "T-J-REV-03-1-CON-002-v1", "T-J-REV-03-1-CON-003-v1"]
CASH_DRAFT = MisstatementDraft(
    type=MisstatementType.factual,
    account="Cash",
    assertion=Assertion.Cutoff,
    amount_cents=5000000,
    direction=Direction.overstated,
)
CONSULT = "Consulted the firm's technical partner on bank cut-off; agreed the receipt was not banked by 30 June 2026"


def transitions(s: Started, oid: str | None = None) -> list[tuple[str, str]]:
    """程序状态的 transition 留痕 (from, to); oid 为空时取全部程序的."""
    return [
        (e.payload["from"], e.payload["to"])
        for e in trail_of(s).entries
        if e.kind is TrailKind.transition and e.payload["object"] == "procedure" and oid in (None, e.payload["id"])
    ]


async def disputed(features: frozenset[str]) -> tuple[Started, GraphStateQuery]:
    """Numbat 争议变体跑到首次等复核: BA-01 判为不一致, 建争议任务."""
    s = await start_demo(review_llm=MockLLMClient(VARIANTS / "dispute"), features=features)
    await wait_idle(s.runner, EID)
    return s, GraphStateQuery(s.runner.app, s.runner.deps, s.runner)


async def open_review_tasks(s: Started, query: GraphStateQuery) -> list[Any]:
    found = await list_open_items(ToolEnv(EID, s.runner.deps, query, 6000), NoArgs())
    return [item["task_id"] for item in found.data["items"] if item["type"] == "review_task"]


def r1_notes(s: Started, subject: str = "BA-01") -> list[str]:
    """该判断对象独立复核第一轮的 llm_call 留痕记下的草稿问题 (notes, 与推理链的 errors 分开)."""
    [call] = [
        e for e in trail_of(s).entries if e.kind is TrailKind.llm_call and e.payload["fixture_key"] == f"{subject}-r1"
    ]
    notes: list[str] = call.payload["notes"]
    return notes


async def assert_not_written(s: Started, decision: Any, code: str, detail: str | None = None) -> None:
    """提交被拒: 错误码对, Store 不写入, 不启动后台."""
    rows = list(s.output_store.rows)
    with pytest.raises(DecisionRejected) as e:
        await s.submit.submit(EID, decision)
    assert e.value.code == code, e.value
    if detail is not None:
        assert e.value.message == detail
    assert s.output_store.rows == rows and not s.runner.is_running(EID)


async def in_dispute_and_listed_first(on: Started, query: GraphStateQuery) -> None:
    """R151 验收 2, 5 (collab 打开, 仲裁前): CSH-02 所在那一层 (第 3 层: CSH-04 → DOC-06 → CSH-02) 汇合后为争议中,
    transition 一行 planned → in_dispute; 总览中 CSH-02 为 in_dispute, 待决任务 1; 对话待办的复核任务依次为争议任务,
    再按创建时间为 REV-03 的三条; 总览与待办按项目冻结的功能集 (进程换成基线功能集读这个项目照样)."""
    assert (await on.output_store.get(EID, Procedure, "CSH-02")).status is ProcedureStatus.in_dispute
    assert transitions(on, "CSH-02") == [("planned", "in_dispute")]
    overview = await query.overview(EID)
    [summary] = [p for p in overview.procedures if p.id == "CSH-02"]
    assert (summary.status, summary.pending_tasks, overview.features) == (
        ProcedureStatus.in_dispute,
        1,
        ["collab", "critic"],
    )
    assert await open_review_tasks(on, query) == [TASK, *REV03]
    baseline = replace(on.runner.deps, features=BASELINE)
    elsewhere = GraphStateQuery(on.runner.app, baseline, on.runner)
    assert (await elsewhere.overview(EID)).features == ["collab", "critic"]
    found = await list_open_items(ToolEnv(EID, baseline, elsewhere, 6000), NoArgs())
    assert [i["task_id"] for i in found.data["items"] if i["type"] == "review_task"] == [TASK, *REV03]


async def proposal_and_checks(on: Started, before: Judgment) -> None:
    """R150 (collab 打开, 仲裁前, 不改 Store).

    验收 2: 争议变体第一轮建议 misstatement, 草稿 {factual, Cash, Cutoff, 5000000, overstated} (等于 CR-SI-2026-0547-1
    的借方, 也等于 X-CSH-05-1-B01-1 的绝对值) 写回判断, 第一轮留痕没有草稿的问题.
    验收 3, 4 经接口: review-tasks 的争议目标带这两项 (前端争议卡与预填用), 争议任务带各仲裁的草稿规则 draft_rules;
    POST review-records 咨询过而没写记录报 422
    MISSING_FIELD. 验收 4 (a)(b) 用写回的真实数据: A6 建议 close 时 a6_upheld 带草稿被拒, A7 建议错报时 a7_upheld
    不带草稿被拒. 各条的取值组合见 test_review_decisions 的纯函数测试.
    验收 6, 9 的效果 (纯函数 dispute_effects, 带草稿的 a7_upheld 由仲裁实际走): a7_upheld 不带草稿时 -H1 的建议动作取
    critic_proposed_action (改为 close 时为 close); neither 带草稿时人工判断建议 misstatement 并带该草稿, 不带为 accept;
    collab 关闭时都为 accept, 无草稿 (同 collab 之前), 附了草稿的错报照建."""
    assert (before.critic_proposed_action, before.critic_misstatement_draft) == (
        ProposedAction.misstatement,
        CASH_DRAFT,
    )
    assert r1_notes(on) == []

    draft = CASH_DRAFT.model_dump(mode="json")
    url = f"/api/v1/engagements/{EID}"
    rows = list(on.output_store.rows)
    async with api_client(on.runner) as client:
        tasks = (await client.get(f"{url}/review-tasks", params={"status": "pending"})).json()
        [dispute] = [view for view in tasks if view["task"]["id"] == TASK]
        target = dispute["target"]
        assert (target["critic_proposed_action"], target["critic_misstatement_draft"]) == ("misstatement", draft)
        # 前端按它显示 "同时登记错报" 一项 (与校验同一规则; 第 1 轮审核 R150-01)
        assert dispute["draft_rules"] == {"a6_upheld": "hidden", "a7_upheld": "required", "neither": "optional"}
        # 只有争议任务给 (第 2 轮审核 R150-06): REV-03 的普通复核任务为空, 防对全部仲裁取规则
        assert {view["task"]["id"]: view["draft_rules"] for view in tasks if view["task"]["id"] != TASK} == {
            task_id: {} for task_id in REV03
        }
        body = {
            "task_id": TASK,
            "target_version": 1,
            "decision": "dispute_resolution",
            "reason": "仲裁意见",
            "dispute_resolution": "a7_upheld",
            "misstatement_draft": draft,
            "consulted": True,
        }
        missing = await client.post(f"{url}/review-records", json=body)
        assert (missing.status_code, missing.json()["detail"]["code"]) == (422, "MISSING_FIELD")
    assert on.output_store.rows == rows
    await assert_not_written(
        on, resolve(DisputeResolution.a6_upheld, misstatement_draft=CASH_DRAFT), "FIELD_NOT_ALLOWED"
    )
    await assert_not_written(on, resolve(DisputeResolution.a7_upheld), "MISSING_FIELD", "misstatement_draft")

    task = await on.output_store.get(EID, ReviewTask, TASK)
    explained = [await on.output_store.get(EID, AuditException, x) for x in before.exception_ids]

    def adopted(decision: Any, collab: bool, judgment: Judgment = before) -> tuple[Any, ...]:
        effects = dispute_effects(decision, task, judgment, explained, set(), collab)
        [human] = [e for e in effects if isinstance(e, Judgment) and e.id == f"{JID}-H1"]
        drafted = [e.judgment_id for e in effects if isinstance(e, Misstatement)]
        return human.proposed_action, human.misstatement_draft, drafted

    plain = resolve(DisputeResolution.a7_upheld)
    closing = before.model_copy(update={"critic_proposed_action": ProposedAction.close})
    neither = resolve(
        DisputeResolution.neither,
        neither_action=NeitherAction.human_conclusion,
        override_conclusion="The receipt was banked after year end (synthetic).",
        misstatement_draft=CASH_DRAFT,
    )
    accepted = (ProposedAction.accept, None, [f"{JID}-H1"])  # 照建错报, 判断本身照旧
    assert adopted(plain, True, closing) == (ProposedAction.close, None, [])
    assert adopted(plain, False, closing) == (ProposedAction.accept, None, [])
    assert adopted(resolve(DisputeResolution.a7_upheld, misstatement_draft=CASH_DRAFT), False) == accepted
    assert adopted(neither, True) == (ProposedAction.misstatement, CASH_DRAFT, [f"{JID}-H1"])
    assert adopted(neither, False) == accepted
    assert adopted(neither.model_copy(update={"misstatement_draft": None}), True) == (ProposedAction.accept, None, [])


async def a7_adopted(on: Started, before: Judgment) -> None:
    """R150 验收 6 (a7_upheld 带草稿与咨询 D-ARB 之后): 建 -H1 (建议 misstatement, 草稿为记录的, 结论, 推理链与留痕
    取复核的, critic_verdict not_applicable, confirmed), M-D-ARB 记 -H1, 原判断 v2 overridden, decision 留痕与存下的
    记录带咨询两项 (记录去首尾空白)."""
    new = await on.output_store.get(EID, Judgment, f"{JID}-H1")
    assert (new.proposed_action, new.misstatement_draft) == (ProposedAction.misstatement, CASH_DRAFT)
    assert (new.conclusion, new.reasoning_chain, new.trail_ref) == (
        before.critic_conclusion,
        before.critic_reasoning_chain,
        before.critic_trail_ref,
    )
    assert (new.critic_verdict, new.review_status) == (CriticVerdict.not_applicable, ReviewStatus.confirmed)
    misstatement = await on.output_store.get(EID, Misstatement, "M-D-ARB")
    assert (misstatement.judgment_id, misstatement.amount_cents, misstatement.direction) == (
        new.id,
        5000000,
        Direction.overstated,
    )
    old = await on.output_store.get(EID, Judgment, JID)
    assert (old.version, old.review_status, old.superseded_by) == (2, ReviewStatus.overridden, new.id)
    [trail] = [e for e in trail_of(on).entries if e.kind is TrailKind.decision and e.idempotency_key == "D-ARB"]
    assert (trail.payload["dispute_resolution"], trail.payload["consulted"], trail.payload["consultation_record"]) == (
        "a7_upheld",
        True,
        CONSULT,
    )
    stored = await on.output_store.get(EID, ReviewRecord, "D-ARB")
    assert (stored.consulted, stored.consultation_record) == (True, CONSULT)


async def test_dispute_on_and_off() -> None:
    """争议变体在 collab 打开与关闭时各跑一次 (到首次等复核, 仲裁后继续), R151 与 R150 的断言共用这两次运行.

    打开: 仲裁前见 in_dispute_and_listed_first (R151) 与 proposal_and_checks (R150); a7_upheld 带草稿与咨询 (D-ARB)
    后 CSH-02 completed, transition in_dispute → completed (R151 验收 3), 结果见 a7_adopted (R150 验收 6).
    关闭 (R151 验收 7, R150 验收 9): CSH-02 为 awaiting_human, 待办按创建时间 (REV-03 三条在前); 复核方的建议与
    草稿不写回; a6_upheld 带草稿且 consulted 而无记录也收 (草稿随哪一方, 咨询都不校验), 照建 M-D-ARB 记原判断;
    两边 transition 总行数相同."""
    on, query = await disputed(COLLAB)
    await in_dispute_and_listed_first(on, query)
    before = await on.output_store.get(EID, Judgment, JID)
    await proposal_and_checks(on, before)
    # 记录带首尾空白: 存与留痕时去掉 (a7_adopted 按 CONSULT 核对)
    record = f"  {CONSULT}\n"
    a7 = resolve(DisputeResolution.a7_upheld, misstatement_draft=CASH_DRAFT, consulted=True, consultation_record=record)
    await on.submit.submit(EID, a7)
    await wait_idle(on.runner, EID)
    assert on.runner.run_error(EID) is None
    assert (await on.output_store.get(EID, Procedure, "CSH-02")).status is ProcedureStatus.completed
    assert transitions(on, "CSH-02") == [("planned", "in_dispute"), ("in_dispute", "completed")]
    await a7_adopted(on, before)

    off, query = await disputed(BASELINE)
    assert (await off.output_store.get(EID, Procedure, "CSH-02")).status is ProcedureStatus.awaiting_human
    assert (await query.overview(EID)).features == ["critic"]
    assert await open_review_tasks(off, query) == [*REV03, TASK]
    j = await off.output_store.get(EID, Judgment, JID)
    assert (j.critic_proposed_action, j.critic_misstatement_draft) == (None, None)
    async with api_client(off.runner) as client:
        tasks = (await client.get(f"/api/v1/engagements/{EID}/review-tasks", params={"status": "pending"})).json()
    assert [view["draft_rules"] for view in tasks if view["task"]["id"] == TASK] == [{}]  # 关闭时不给, 草稿两可
    await off.submit.submit(EID, resolve(DisputeResolution.a6_upheld, misstatement_draft=CASH_DRAFT, consulted=True))
    await wait_idle(off.runner, EID)
    assert off.runner.run_error(EID) is None
    assert transitions(off, "CSH-02") == [("planned", "awaiting_human"), ("awaiting_human", "completed")]
    stored = await off.output_store.get(EID, ReviewRecord, "D-ARB")
    assert (stored.consulted, stored.consultation_record) == (True, None)
    assert (await off.output_store.get(EID, Misstatement, "M-D-ARB")).judgment_id == JID
    assert len(transitions(on)) == len(transitions(off))


async def test_drafts_with_rev02_at_l3_and_untraceable_critic_amount(tmp_path: Path) -> None:
    """一次运行 (collab 打开; REV-02 在 L3 下运行, 测试改的程序库副本; 复核模型的 BA-01 第一轮草稿金额改为 4000000)
    覆盖 R150 两个要另造数据的场景.

    验收 2: BA-01 复核方的草稿金额对不上 (不是给出的行, 两行之差或该判断解释的例外的金额): critic_misstatement_draft
    为空, critic_proposed_action 仍为 misstatement, 问题写进第一轮 llm_call 留痕的 notes (与推理链的 errors 分开);
    不判失败, 仍为争议.
    验收 4 (a), 5: A6 对 SI-2026-0612 建议 misstatement, 草稿 {factual, Revenue, Cutoff, 14200000, overstated}, 推理链
    另有一步 assumption 而命中复核, A7 判 inconsistent, 建争议任务 T-J-REV-02-1-SI-2026-0612-v1. a6_upheld 不带草稿被拒
    (MISSING_FIELD); 带该草稿 (D-ARB-A6): 原判断 v2 confirmed, 建 M-D-ARB-A6 (judgment_id 原判断, review_record_id
    D-ARB-A6, 14200000 overstated), X-REV-08-1-R01-1 出新版本 attributed."""
    root = tmp_path / "knowledge"
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    yaml = root / "procedures" / "REV-02.yaml"
    text = yaml.read_text(encoding="utf-8")
    assert text.count("autonomy_level: L2\n") == 1
    yaml.write_text(text.replace("autonomy_level: L2\n", "autonomy_level: L3\n"), encoding="utf-8")
    variant = json.loads((VARIANTS / "dispute" / "GEN-09" / "BA-01-r1.json").read_text(encoding="utf-8"))
    variant["misstatement_draft"]["amount_cents"] = 4000000
    (tmp_path / "untraceable" / "GEN-09").mkdir(parents=True)
    (tmp_path / "untraceable" / "GEN-09" / "BA-01-r1.json").write_text(json.dumps(variant), encoding="utf-8")
    fixtures = get_settings().LLM_FIXTURES_ROOT
    llm = LayeredMockLLM(VARIANTS / "dispute_a6", fixtures)
    review = LayeredMockLLM(tmp_path / "untraceable", VARIANTS / "dispute_a6", VARIANTS / "dispute")
    s = await start_demo(llm=llm, review_llm=review, features=COLLAB, library=ProcedureLibrary(root, COLLAB))
    await wait_idle(s.runner, EID)

    j = await s.output_store.get(EID, Judgment, JID)
    assert (j.critic_verdict, j.critic_proposed_action, j.critic_misstatement_draft) == (
        CriticVerdict.inconsistent,
        ProposedAction.misstatement,
        None,
    )
    assert [e for e in r1_notes(s) if e.startswith("独立复核的错报金额 4000000")]

    for decision in REVIEWS[:3]:  # REV-03 确认后 REV-02 才派发
        await s.submit.submit(EID, decision)
        await wait_idle(s.runner, EID)
    task_id = "T-J-REV-02-1-SI-2026-0612-v1"
    task = await s.output_store.get(EID, ReviewTask, task_id)
    assert task is not None and task.kind is TaskKind.dispute, s.runner.run_error(EID)
    judgment = await s.output_store.get(EID, Judgment, "J-REV-02-1-SI-2026-0612")
    assert (judgment.critic_trigger, judgment.critic_verdict, judgment.proposed_action) == (
        CriticTrigger.assumption,
        CriticVerdict.inconsistent,
        ProposedAction.misstatement,
    )
    plain = record(task_id, Decision.dispute_resolution, id="D-ARB-A6", dispute_resolution=DisputeResolution.a6_upheld)
    await assert_not_written(s, plain, "MISSING_FIELD", "misstatement_draft")
    await s.submit.submit(EID, plain.model_copy(update={"misstatement_draft": NUMBAT_CUTOFF_DRAFT}))
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None
    confirmed = await s.output_store.get(EID, Judgment, judgment.id)
    assert (confirmed.version, confirmed.review_status) == (2, ReviewStatus.confirmed)
    m = await s.output_store.get(EID, Misstatement, "M-D-ARB-A6")
    assert (m.judgment_id, m.review_record_id, m.amount_cents, m.direction) == (
        judgment.id,
        "D-ARB-A6",
        14200000,
        Direction.overstated,
    )
    exception = await s.output_store.get(EID, AuditException, "X-REV-08-1-R01-1")
    assert (exception.disposition, exception.attributed_by) == (ExceptionDisposition.attributed, judgment.id)
