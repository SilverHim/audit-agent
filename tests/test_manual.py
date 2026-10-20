"""人工程序 (engine manual 且不是闸门): R92 契约与提交校验, R93 派发, 等待与结果整理 (接口文档第 2.5, 5, 6, 7, 8.1 节).

两条测试程序放在临时 knowledge 目录, 都在完成阶段, 由 A10 派发, 材料与结果整理由模板 owner 的测试 Agent 实现:
- TST-M01 (owner A10, outputs []): 取得签字的管理层声明书, 结果模型 TstLetter, 至少 2 条证据 (不同于缺省的 1);
  owner LetterOwner 不另查, 不整理对象.
- TST-M02 (owner A6, outputs [exceptions, confirmations]): 登记管理层的回复, 结果模型 TstReply; owner ReplyOwner
  的材料为 {"items": ["M-TEST-1"]}, amount_cents 为负时拒绝, 按结果整理出例外 X-TST-M02-1-MAN-1 (编号按状态中本次
  执行已有的例外数取) 并把状态中第一份函证出新版本 (读状态的纯函数, 重放时看到的状态须同在线).
完成节点改为先执行其中一条再执行 FIN-04, 节点按通用等待 (R87) 停在节点内等人提交 manual_result.
"""

import re
import shutil
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
import pytest_asyncio
from pydantic import ValidationError

from backend.agents import registry
from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.concierge import ConciergeAgent
from backend.agents.qc import QCAgent
from backend.contracts.errors import DecisionRejected
from backend.contracts.manual_results import RESULT_MODELS
from backend.contracts.models import (
    AgentId,
    AuditException,
    Confirmation,
    Decision,
    DocumentRef,
    DocumentType,
    EvidencePointer,
    EvidenceType,
    GateVerdict,
    Judgment,
    ManualRequest,
    ManualResult,
    Model,
    OwnershipGraph,
    PartyNode,
    Phase,
    Procedure,
    ProcedureRun,
    ProcedureStatus,
    Reliability,
    ReviewRecord,
    ReviewTask,
    RunStatus,
    TargetType,
    TaskKind,
    TaskStatus,
    TrailKind,
    UncorrectedSchedule,
    Versioned,
)
from backend.contracts.state import StatePatch, initial_state
from backend.core.config import get_settings
from backend.orchestration.decisions import check_manual_result
from backend.orchestration.merge import MergeRejected
from backend.orchestration.nodes import (
    DISPATCH_FIELDS,
    NODE_PROCEDURES,
    GraphNodes,
    apply_patch,
    check_collected,
    check_dispatch,
    writer_of,
)
from backend.orchestration.query import GraphStateQuery
from backend.orchestration.runner import GraphRunner, waiting_payloads
from backend.services.procedure_library import ProcedureLibrary, manual_program
from backend.services.review_queue import ReviewQueue
from tests.fakes import (
    ENGAGEMENT,
    NOW,
    NUMBAT_PROFILE,
    FakeEngagementStore,
    FakeOutputStore,
    FakeRegistry,
    FakeTrail,
    ScriptAgent,
    finished,
    make_deps,
    make_procedure,
    make_run,
    make_runner,
    numbat_all_p2_agents,
    wait_idle,
)
from tests.test_decisions import Started, acc01_decision, assert_rejected
from tests.test_flow import approve_materiality, review_all
from tests.test_node_wait import delivered, graph_state, store_state, submit_unread
from tests.test_rerun import CrashAfterCommit

EID = ENGAGEMENT.id
CONFIG = GraphRunner.config(EID)
TST = "TST-M01"
RUN = f"RUN-{TST}-1"
TASK = f"T-{RUN}-v1"
REPLY = "TST-M02"
REPLY_RUN = f"RUN-{REPLY}-1"
REPLY_TASK = f"T-{REPLY_RUN}-v1"
REPLY_EXCEPTION = f"X-{REPLY}-1-MAN-1"
INSTRUCTIONS = "Obtain the signed management representation letter"
TST_YAML = (
    f"id: {TST}\nname: 自测人工程序\nphase: P3_Completion\ncycle: Completion\nowner: A10\nengine: manual\n"
    "autonomy_level: L1\noutputs: []\n"
)
MANUAL = f"manual:\n  instructions: {INSTRUCTIONS}\n  result_model: TstLetter\n  min_evidence: 2\n"
LLM = "llm:\n  prompt: DOC-04.md.j2\n  output_model: ContractExtraction\n"  # 借 DOC-04 的, 只为能加载
REPLY_YAML = (
    f"id: {REPLY}\nname: 自测管理层回复\nphase: P3_Completion\ncycle: Completion\nowner: A6\nengine: manual\n"
    "autonomy_level: L1\noutputs: [exceptions, confirmations]\n"
    "manual:\n  instructions: Record management's reply\n  result_model: TstReply\n  min_evidence: 1\n"
)
MATERIAL = {"period_end": "2026-06-30"}  # LetterOwner 派发时给人的材料
REPLY_MATERIAL = {"items": ["M-TEST-1"]}  # ReplyOwner 派发时给人的材料
LETTER = DocumentRef(  # 审计师已上传的声明书 (2 页, 公司出具的原件)
    id="DOC-0a1b2c3d4e5f",
    type=DocumentType.representation_letter,
    file_name="representation_letter.pdf",
    sha256="0a1b2c3d4e5f" + "0" * 52,
    page_count=2,
    reliability=Reliability.internal_original,
    status="classified",
    uploaded_at=NOW,
)
DECIDED_AT = datetime(2026, 10, 23, 5, tzinfo=UTC)  # 悉尼 10-23 16:00
CALLS: list[tuple[str, str]] = []  # 两个 owner 各步的调用 (步骤, 程序), 每个用例前清空
CALLERS: list[object] = []  # 与 CALLS 对应的 owner 实例
CHECKED: list[ManualResult] = []  # ReplyOwner.check 收到的决定中的结果


class TstLetter(Model):
    """测试登记的结果模型: 声明书的签署日与签署人."""

    signed_on: date
    signatory: str


class TstReply(Model):
    """测试登记的结果模型: 管理层回复的金额."""

    amount_cents: int


class Owner(ProcedureAgent[None]):
    """测试 owner 的公共部分: 不负责常规程序 (不经注册表映射), 只为人工程序实现 ManualHandler."""

    procedures: ClassVar[frozenset[str]] = frozenset()

    async def execute(self, ctx: RunContext) -> None:
        raise NotImplementedError("owner 只实现 ManualHandler")

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        raise NotImplementedError("owner 只实现 ManualHandler")

    def called(self, step: str, ctx: RunContext) -> None:
        """记下一步调用 (步骤, 程序) 与调用的实例."""
        CALLS.append((step, ctx.procedure.id))
        CALLERS.append(self)


class LetterOwner(Owner):
    """TST-M01 的 owner (A10): 材料为 MATERIAL, 按程序不另查, 不整理出对象."""

    agent_id: ClassVar[AgentId] = AgentId.A10
    manual_procedures: ClassVar[frozenset[str]] = frozenset({TST})

    def prepare(self, ctx: RunContext) -> dict[str, Any]:
        self.called("prepare", ctx)
        return dict(MATERIAL)

    def check(self, ctx: RunContext, task: ReviewTask, record: ReviewRecord) -> list[str]:
        self.called("check", ctx)
        return []

    def collect(self, ctx: RunContext, record: ReviewRecord) -> StatePatch:
        self.called("collect", ctx)
        return {}


class ReplyOwner(Owner):
    """TST-M02 的 owner (A6): 材料为 REPLY_MATERIAL; amount_cents 为负时拒绝; 按回复的金额整理出一条例外, 编号取状态中
    本次执行已有的例外数加 1 (同 RPT-03 按状态取修订号), 并把状态中 ID 最小的函证按常规写法出新版本 (version + 1)
    记下回复金额: 都读 ctx.state, 重放时看到自己上次写进 Store 的对象就会多出一条例外, 一个版本. extra 为真时另带
    一个没声明的字段, 测合并校验."""

    agent_id: ClassVar[AgentId] = AgentId.A6
    manual_procedures: ClassVar[frozenset[str]] = frozenset({REPLY})
    extra: ClassVar[bool] = False

    def prepare(self, ctx: RunContext) -> dict[str, Any]:
        self.called("prepare", ctx)
        return dict(REPLY_MATERIAL)

    def check(self, ctx: RunContext, task: ReviewTask, record: ReviewRecord) -> list[str]:
        self.called("check", ctx)
        assert record.manual_result is not None
        CHECKED.append(record.manual_result)
        return ["amount_cents 不得为负"] if record.manual_result.details.get("amount_cents", 0) < 0 else []

    def collect(self, ctx: RunContext, record: ReviewRecord) -> StatePatch:
        self.called("collect", ctx)
        assert record.manual_result is not None
        mine = [x for x in ctx.state["exceptions"] if x.run_id == ctx.run.id]
        exception = AuditException(
            id=f"X-{REPLY}-{ctx.run.run_no}-MAN-{len(mine) + 1}",
            procedure_id=REPLY,
            run_id=ctx.run.id,
            rule_id="MAN",
            severity="medium",
            source_ids=REPLY_MATERIAL["items"],
            evidence=record.manual_result.evidence,
            amount_cents=record.manual_result.details["amount_cents"],
        )
        amount = record.manual_result.details["amount_cents"]
        confirmations = [
            c.model_copy(update={"version": c.version + 1, "confirmed_balance_cents": amount})
            for c in sorted(ctx.state["confirmations"], key=lambda c: c.id)[:1]
        ]
        patch: StatePatch = {"exceptions": [exception], "confirmations": confirmations}
        return {**patch, "judgments": []} if self.extra else patch


def register(monkeypatch: pytest.MonkeyPatch, program: str = TST) -> None:
    """登记两个结果模型与两个测试 owner; 完成节点先执行 program 再执行 FIN-04 (recovery 与 nodes 共用这张表)."""
    monkeypatch.setitem(RESULT_MODELS, "TstLetter", TstLetter)
    monkeypatch.setitem(RESULT_MODELS, "TstReply", TstReply)
    monkeypatch.setattr(registry, "AGENT_CLASSES", (*registry.AGENT_CLASSES, LetterOwner, ReplyOwner))
    monkeypatch.setitem(NODE_PROCEDURES, "completion", (program, "FIN-04"))


@pytest.fixture(autouse=True)
def registered(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """每个用例: register (完成节点为 TST-M01, R93 的用例经 reply_node 改为 TST-M02); 清空 owner 的调用记录."""
    register(monkeypatch)
    CALLS.clear()
    CALLERS.clear()
    CHECKED.clear()
    yield


def manual_library(root: Path, yamls: Sequence[str] = (TST_YAML + MANUAL,)) -> ProcedureLibrary:
    """knowledge 目录的副本另加测试程序 (每段 YAML 一个文件, 文件名取其 id)."""
    shutil.copytree(get_settings().KNOWLEDGE_ROOT, root)
    for yaml in yamls:
        pid = yaml.split("\n", 1)[0].removeprefix("id: ")
        (root / "procedures" / f"{pid}.yaml").write_text(yaml, encoding="utf-8")
    return ProcedureLibrary(root)


def pointer(
    source_type: str = "document", source_id: str = LETTER.id, page: int | None = 2, locator: str = "signature"
) -> EvidencePointer:
    """人填的证据指针: 种类填成 external, 不填可靠性 (写入时按文件登记改写为 voucher, internal_original)."""
    return EvidencePointer(
        type=EvidenceType.external, source_type=source_type, source_id=source_id, locator=locator, page=page
    )


BODY = pointer(page=1, locator="representations")  # 声明书正文页


def letter_result(**changes: Any) -> ManualResult:
    """取得声明书的结果: 结论, 10-23 执行, 证据为声明书第 1 页 (正文) 与第 2 页 (签字页), 结构化结果合 TstLetter."""
    base = ManualResult(
        conclusion="Signed letter obtained, dated 23 October 2026",
        performed_on=date(2026, 10, 23),
        evidence=[BODY, pointer()],
        details={"signed_on": "2026-10-23", "signatory": "Alex Morgan"},
    )
    return base.model_copy(update=changes)


def as_registered(result: ManualResult) -> ManualResult:
    """写入的结果: 证据指针按 LETTER 的登记改写种类与可靠性 (同判断的文件指针), 其余原样."""
    evidence = [
        p.model_copy(update={"type": EvidenceType.voucher, "reliability": Reliability.internal_original})
        for p in result.evidence
    ]
    return result.model_copy(update={"evidence": evidence})


def submitted(decision: Decision = Decision.manual_result, **changes: Any) -> ReviewRecord:
    """对 TST-M01 的 manual 任务的决定: 绑定执行记录第 1 版, 带理由与 letter_result; changes 覆盖任意字段."""
    base = ReviewRecord(
        id="D-M01",
        task_id=TASK,
        target_version=1,
        decision=decision,
        reason="Letter received from the CFO",
        manual_result=letter_result() if decision is Decision.manual_result else None,
        reviewed_by="U-1",
        decided_at=DECIDED_AT,
    )
    return base.model_copy(update=changes)


def replied(amount_cents: int, record_id: str = "D-M02") -> ReviewRecord:
    """对 TST-M02 的 manual 任务的决定: 回复金额 amount_cents, 证据为声明书第 2 页."""
    result = letter_result(conclusion="Management replied", details={"amount_cents": amount_cents})
    return submitted(id=record_id, task_id=REPLY_TASK, manual_result=result)


# ── R92 验收 1: 加载校验 ──


@pytest.mark.parametrize(
    ("yaml", "problem"),
    [
        (TST_YAML, f"{TST}: engine manual 且不是闸门程序, 须有 manual 段"),
        (TST_YAML + MANUAL.replace("TstLetter", "TstUnknown"), f"{TST}: 结果模型 TstUnknown 没有登记"),
        (TST_YAML.replace("P3_Completion", "P2_Executing") + MANUAL, f"{TST}: 本级不做 P2 的人工程序"),
        (
            TST_YAML.replace("engine: manual", "engine: llm") + LLM + MANUAL,
            f"{TST}: 不是人工程序 (engine manual 且不是闸门), 不能写 manual 段",
        ),
        (
            TST_YAML + MANUAL.replace("min_evidence: 2", "min_evidence: -1"),
            "manual.min_evidence\n  Input should be greater than or equal to 0",
        ),
    ],
    ids=["no_manual_section", "unregistered_model", "in_p2", "manual_on_llm_program", "negative_min_evidence"],
)
def test_library_rejects_malformed_manual_programs(tmp_path: Path, yaml: str, problem: str) -> None:
    """人工程序没写 manual 段, 结果模型没登记, 或放在 P2; 别的程序写了 manual 段; 证据下限为负: 程序库构造即报错,
    消息含程序编号或字段 (防派出没有说明或无从校验结果的任务, 提交时才发现)."""
    with pytest.raises(ValueError, match=re.escape(problem)):
        manual_library(tmp_path / "knowledge", [yaml])


def test_gate_programs_are_not_manual_programs() -> None:
    """现有五个闸门程序都是 engine manual, 不算人工程序, 没有 manual 段照常加载; engine manual 的程序里只有 FIN-08
    (功能 misstatement, 需求 R161) 是人工程序."""
    library = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
    engine_manual = [t for t in library.all(include_disabled=True) if t.engine == "manual"]
    assert [t.id for t in engine_manual if manual_program(t)] == ["FIN-08"]
    gates = [t for t in engine_manual if not manual_program(t)]
    assert sorted(t.id for t in gates) == ["ACC-01", "FIN-01", "PLN-01", "PLN-03", "RPT-01"]
    assert not any(t.manual is not None for t in gates)


# ── R92 验收 2: manual 任务 ──


def test_manual_task_carries_the_request(tmp_path: Path) -> None:
    """make_task(manual): 目标为本次执行记录第 1 版, ID T-RUN-TST-M01-1-v1, subject_id 取模板的 RESULT, 请求取模板
    manual 段与给定材料; 只允许 manual_result. 没有 manual 段的程序不能建 manual 任务."""
    queue = ReviewQueue(manual_library(tmp_path / "knowledge"))
    run = finished(make_run(TST))
    task = queue.make_task(TaskKind.manual, run, TST, run.id, NOW, material=MATERIAL)
    assert (task.id, task.target_type, task.target_id, task.target_version, task.subject_id) == (
        TASK,
        TargetType.ProcedureRun,
        RUN,
        1,
        "RESULT",
    )
    assert task.manual_request == ManualRequest(
        instructions=INSTRUCTIONS, result_model="TstLetter", min_evidence=2, material=MATERIAL
    )
    assert queue.allowed(task) == {Decision.manual_result}
    with pytest.raises(ValueError, match="FIN-04 没有 manual 段"):
        queue.make_task(TaskKind.manual, finished(make_run("FIN-04")), "FIN-04", "RUN-FIN-04-1", NOW)


# ── R93 验收 1: 映射到 A10, owner 须实现 ManualHandler ──


class Undeclared(ReplyOwner):
    """agent_id 为 A6, 实现了 ManualHandler, 但 manual_procedures 不含 TST-M02."""

    manual_procedures: ClassVar[frozenset[str]] = frozenset()


class Unimplemented(Owner):
    """agent_id 为 A6, manual_procedures 含 TST-M02, 但没实现 ManualHandler."""

    agent_id: ClassVar[AgentId] = AgentId.A6
    manual_procedures: ClassVar[frozenset[str]] = frozenset({REPLY})


def test_manual_programs_map_to_a10_and_need_an_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """组装时 (registry.with_manual_owners, make_deps 同组装根) 按模板 owner 找好两条人工程序的 owner 放进 deps;
    build_agents 把它们都映射到 RPT-03 的那个 ConciergeAgent (A10 代码不按程序改). owner 须 agent_id 为模板 owner,
    manual_procedures 含该程序且实现了 ManualHandler, 缺一项 (不在 AGENT_CLASSES, 没声明, 没实现) 都在组装时报错,
    消息含程序编号与 owner; 换了程序库而没重新组装时 build_agents 报错."""
    library = manual_library(tmp_path / "knowledge", [TST_YAML + MANUAL, REPLY_YAML])
    deps = make_deps(FakeRegistry(), FakeTrail(), library=library)
    assert {pid: type(owner) for pid, owner in deps.manual_owners.items()} == {TST: LetterOwner, REPLY: ReplyOwner}
    agents = registry.build_agents(deps)
    concierge = agents["RPT-03"]
    assert isinstance(concierge, ConciergeAgent)
    assert agents[TST] is agents[REPLY] is concierge
    assert all(owner.deps is deps for owner in deps.manual_owners.values())  # type: ignore[attr-defined]  # 同一个 deps
    unassembled = replace(make_deps(FakeRegistry(), FakeTrail()), library=library)
    with pytest.raises(LookupError, match=f"人工程序 {TST}, {REPLY} 的 owner 没有组装"):
        registry.build_agents(unassembled)

    others = registry.AGENT_CLASSES[:-1]  # 去掉 ReplyOwner
    for extra in [(), (Undeclared,), (Unimplemented,)]:
        monkeypatch.setattr(registry, "AGENT_CLASSES", (*others, *extra))
        with pytest.raises(LookupError, match=f"{REPLY} 的 owner A6 没有为它实现 ManualHandler"):
            registry.with_manual_owners(deps)


# ── 走到完成阶段的人工程序等待 ──


async def at_manual(
    tmp_path: Path, program: str = TST, output_store: FakeOutputStore | None = None, agents: Any = None
) -> Started:
    """Numbat 走完 P2 与全部复核后进入完成阶段, A10 派发 program 后停在它的节点内等待 (完成节点先执行它再执行
    FIN-04); 之后登记审计师上传的声明书 LETTER. output_store 可换成会在某次提交后出错的存储, agents 另给覆盖的
    Agent (如 FIN-04)."""

    def mapping(deps: AgentDeps) -> dict[str, Any]:
        return {**numbat_all_p2_agents(deps), **(agents(deps) if agents is not None else {})}

    yamls = [TST_YAML + MANUAL] if program == TST else [REPLY_YAML]
    engagement_store = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    runner, store = make_runner(
        engagement_store=engagement_store,
        output_store=output_store,
        agents=mapping,
        library=manual_library(tmp_path / "knowledge", yamls),
    )
    s = Started(runner, store, engagement_store)
    await runner.start(EID)
    await wait_idle(runner, EID)
    await s.submit.submit(EID, acc01_decision(GateVerdict.approve, id="D-ACC"))
    await wait_idle(runner, EID)
    await approve_materiality(s)
    await wait_idle(runner, EID)
    await review_all(s)
    runner.deps.financial_store.document_refs.append(LETTER)  # type: ignore[attr-defined]
    return s


async def submit(s: Started, decision: ReviewRecord) -> None:
    await s.submit.submit(EID, decision)
    await wait_idle(s.runner, EID)
    assert s.runner.run_error(EID) is None


def latest(s: Started, model: type[Any], oid: str) -> Any:
    return max((r for r in s.output_store.rows if isinstance(r, model) and r.id == oid), key=lambda r: r.version)


def entries(s: Started, kind: TrailKind) -> list[Any]:
    trail = s.runner.deps.trail
    assert isinstance(trail, FakeTrail)
    return [e for e in trail.entries if e.kind is kind]


def transitions(s: Started, pid: str = TST) -> list[tuple[str, str]]:
    return [(e.payload["from"], e.payload["to"]) for e in entries(s, TrailKind.transition) if e.payload["id"] == pid]


def written(s: Started, oid: str) -> list[tuple[str, str, int]]:
    """写入 oid 的那次提交里的对象 (类型, ID, 版本)."""
    [commit] = [c for c in s.output_store.commits if any(getattr(o, "id", None) == oid for o in c)]
    return sorted((type(o).__name__, o.id, o.version) for o in commit if isinstance(o, Versioned))


# ── R92 验收 3 至 5: 节点内等待, 拒绝与通过 ──

REJECTED = [  # (说明, 决定, 错误码, 消息含)
    ("缺结果", submitted(manual_result=None), "MISSING_FIELD", "manual_result"),
    ("缺理由", submitted(reason=None), "MISSING_FIELD", "reason"),
    ("缺执行日期", submitted(manual_result=letter_result(performed_on=None)), "MISSING_FIELD", "performed_on"),
    ("confirm", submitted(Decision.confirm, reason=None), "DECISION_NOT_ALLOWED", "confirm"),
    ("rerun", submitted(Decision.rerun), "DECISION_NOT_ALLOWED", "rerun"),
    ("没有证据", submitted(manual_result=letter_result(evidence=[])), "INVALID_VALUE", "至少 2 条, 只有 0 条"),
    ("证据不够", submitted(manual_result=letter_result(evidence=[pointer()])), "INVALID_VALUE", "至少 2 条, 只有 1 条"),
    (  # ID 是已登记的声明书, 来源却写成留痕: 按来源类型拒, 不只按 ID 查登记
        "证据不是文件",
        submitted(manual_result=letter_result(evidence=[BODY, pointer("trail", LETTER.id)])),
        "INVALID_VALUE",
        f"证据须为本项目已登记的文件, trail {LETTER.id} 不是",
    ),
    (
        "文件不在本项目",
        submitted(manual_result=letter_result(evidence=[BODY, pointer(source_id="DOC-ffffffffffff")])),
        "INVALID_VALUE",
        "DOC-ffffffffffff",
    ),
    (
        "页码超出",
        submitted(manual_result=letter_result(evidence=[BODY, pointer(page=3)])),
        "INVALID_VALUE",
        "页码 3 不在 1-2 之间",
    ),
    (
        "执行日期晚于决定当天",
        submitted(manual_result=letter_result(performed_on=date(2026, 10, 24))),
        "INVALID_VALUE",
        "晚于决定当天 2026-10-23",
    ),
    (
        "结构化结果缺字段",
        submitted(manual_result=letter_result(details={"signed_on": "2026-10-23"})),
        "INVALID_VALUE",
        "TstLetter: signatory",
    ),
    ("版本不符", submitted(target_version=2), "VERSION_MISMATCH", RUN),
]


async def test_manual_task_waits_rejects_and_accepts_the_result(tmp_path: Path) -> None:
    """R92 验收 3 至 5, 一次运行:
    - 派发: A10 调 owner.prepare 一次, 同一事务写入 RUN-TST-M01-1 succeeded, manual 任务 (带请求与材料) 与 TST-M01
      awaiting_human; 节点内等待, 载荷同其他人工等待;
    - 拒绝: 逐条提交 REJECTED, 错误码与消息对, Store, 留痕与检查点都不变;
    - 通过: 一个事务写入决定 (证据指针按文件登记改写种类与可靠性, 其余原样), 任务 v2 decided 与 TST-M01
      completed; 不建判断; 1 行 decision 留痕, 1 行 transition awaiting_human → completed; 节点读回后继续执行
      FIN-04, 停在 FIN-01 闸门."""
    s = await at_manual(tmp_path)

    assert waiting_payloads(await s.runner.app.aget_state(CONFIG)) == [
        {"awaiting": "human", "procedure_id": TST, "run_id": RUN, "records": []}
    ]
    assert written(s, RUN) == [("Procedure", TST, 2), ("ProcedureRun", RUN, 1), ("ReviewTask", TASK, 1)]
    task: ReviewTask = latest(s, ReviewTask, TASK)
    assert (task.kind, task.status, task.manual_request.material if task.manual_request else None) == (
        TaskKind.manual,
        TaskStatus.pending,
        MATERIAL,
    )
    assert (latest(s, ProcedureRun, RUN).status, latest(s, Procedure, TST).status) == (
        RunStatus.succeeded,
        ProcedureStatus.awaiting_human,
    )

    for name, decision, code, detail in REJECTED:
        try:
            await assert_rejected(s, decision, code, detail)
        except (AssertionError, DecisionRejected) as e:
            raise AssertionError(f"{name}: {e}") from e

    decision = submitted()
    await submit(s, decision)
    assert written(s, "D-M01") == [("Procedure", TST, 3), ("ReviewRecord", "D-M01", 1), ("ReviewTask", TASK, 2)]
    stored = latest(s, ReviewRecord, "D-M01")
    assert stored == decision.model_copy(update={"manual_result": as_registered(letter_result())})
    assert [(p.type, p.reliability) for p in stored.manual_result.evidence] == [
        (EvidenceType.voucher, Reliability.internal_original)
    ] * 2  # 人填的 external 与空可靠性不留
    assert (latest(s, ReviewTask, TASK).status, latest(s, Procedure, TST).status) == (
        TaskStatus.decided,
        ProcedureStatus.completed,
    )
    assert [j.id for j in s.output_store.rows if isinstance(j, Judgment) and j.procedure_id == TST] == []
    assert [e.idempotency_key for e in entries(s, TrailKind.decision)].count("D-M01") == 1
    assert transitions(s) == [("planned", "awaiting_human"), ("awaiting_human", "completed")]
    assert (await s.next_nodes()) == ("gate_fin",)
    assert [c for c in CALLS if c[0] != "check"] == [("prepare", TST), ("collect", TST)]


# ── R93 验收 2, 3, 7: 派发, 按程序核对与整理结果 ──


@pytest.fixture
def reply_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """完成节点先执行 TST-M02 再执行 FIN-04."""
    monkeypatch.setitem(NODE_PROCEDURES, "completion", (REPLY, "FIN-04"))


@pytest.mark.usefixtures("reply_node")
async def test_reply_dispatched_checked_and_collected(tmp_path: Path) -> None:
    """R93 验收 2, 3, 7, 一次运行:
    - 派发: A10 调 owner.prepare 一次, 不调大模型; 同一事务写入 RUN-TST-M02-1 succeeded, manual 任务 (材料为 owner 给的)
      与 TST-M02 v2 awaiting_human; 载荷同其他人工等待, 总览为等复核, 1 条待决任务;
    - 按程序核对: amount_cents -1 被 owner.check 拒绝 (INVALID_VALUE, 消息为 owner 给的原文), 什么都不写; 同时缺
      理由时报通用核对的 MISSING_FIELD, owner.check 不调 (在通用核对之后);
    - 整理: 提交 4853 后节点读回决定, 调 owner.collect 一次, 以 A6 写入 X-TST-M02-1-MAN-1 (4853, 派发的那次执行),
      程序取决定写出的 completed 版本, 不再执行一次, 继续执行 FIN-04 停在 FIN-01 闸门;
    - A10 派发, DecisionSubmit 核对与节点整理调的是同一个 owner 实例 (组装时找好的 deps.manual_owners)."""
    s = await at_manual(tmp_path, REPLY)

    assert CALLS == [("prepare", REPLY)]
    assert [e for e in entries(s, TrailKind.llm_call) if e.procedure_id == REPLY] == []
    assert written(s, REPLY_RUN) == [
        ("Procedure", REPLY, 2),
        ("ProcedureRun", REPLY_RUN, 1),
        ("ReviewTask", REPLY_TASK, 1),
    ]
    task: ReviewTask = latest(s, ReviewTask, REPLY_TASK)
    assert (task.kind, task.manual_request.material if task.manual_request else None) == (
        TaskKind.manual,
        REPLY_MATERIAL,
    )
    assert waiting_payloads(await s.runner.app.aget_state(CONFIG)) == [
        {"awaiting": "human", "procedure_id": REPLY, "run_id": REPLY_RUN, "records": []}
    ]
    overview = await GraphStateQuery(s.runner.app, s.runner.deps, s.runner).overview(EID)
    assert (overview.awaiting_review, overview.pending_tasks) == (True, 1)

    await assert_rejected(s, replied(-1).model_copy(update={"reason": None}), "MISSING_FIELD", "reason")
    assert CALLS == [("prepare", REPLY)]
    await assert_rejected(s, replied(-1), "INVALID_VALUE", "amount_cents 不得为负")
    assert CALLS[-1] == ("check", REPLY)
    # owner 收到的是通用核对改写后的结果: 人填的 external 与空可靠性已按声明书的登记改为 voucher, internal_original
    assert {(p.type, p.reliability) for p in CHECKED[-1].evidence} == {
        (EvidenceType.voucher, Reliability.internal_original)
    }

    actors: dict[str, str] = {}
    put = s.output_store.put

    async def recording(eid: str, objs: Sequence[Versioned], *, actor: str) -> list[Versioned]:
        actors.update({o.id: actor for o in objs})
        return await put(eid, objs, actor=actor)

    s.output_store.put = recording  # type: ignore[method-assign]
    await submit(s, replied(4853))

    assert [c for c in CALLS if c[0] == "collect"] == [("collect", REPLY)]
    exception: AuditException = latest(s, AuditException, REPLY_EXCEPTION)
    assert (exception.version, exception.amount_cents, exception.run_id, actors[REPLY_EXCEPTION]) == (
        1,
        4853,
        REPLY_RUN,
        AgentId.A6,
    )
    assert latest(s, Procedure, REPLY).status is ProcedureStatus.completed
    assert [r.id for r in s.output_store.rows if isinstance(r, ProcedureRun) and r.procedure_id == REPLY] == [REPLY_RUN]
    snapshot = await s.runner.app.aget_state(CONFIG)
    assert snapshot.next == ("gate_fin",)
    assert REPLY_EXCEPTION in {x.id for x in snapshot.values["exceptions"]}
    assert next(p for p in snapshot.values["procedure_pack"] if p.id == REPLY) == latest(s, Procedure, REPLY)
    assert [step for step, _ in CALLS] == ["prepare", "check", "check", "collect"]
    assert all(owner is s.runner.deps.manual_owners[REPLY] for owner in CALLERS)


async def test_collect_with_an_undeclared_field_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R93 验收 3 (小夹具, 不走全流程): owner.collect 的片段多带了模板没声明的 judgments, GraphNodes.collect 的合并校验
    不过抛 MergeRejected (节点里即节点错误, 记入 run_error 由 R87 的通用路径负责), 整理出的例外与函证都不写入 Store.
    不多带时同样的调用写入例外."""
    deps = make_deps(FakeRegistry(), FakeTrail(), library=manual_library(tmp_path / "knowledge", [REPLY_YAML]))
    nodes = GraphNodes(deps, 1)
    run = finished(make_run(REPLY))
    task = deps.queue.make_task(TaskKind.manual, run, REPLY, run.id, NOW, material=REPLY_MATERIAL)
    state = apply_patch(
        initial_state(ENGAGEMENT, [make_procedure(REPLY, Phase.P3_Completion)], 1),
        {"procedure_runs": [run], "review_tasks": [task]},
    )
    monkeypatch.setattr(ReplyOwner, "extra", True)

    with pytest.raises(MergeRejected, match=f"{REPLY} 未声明产出 judgments"):
        await nodes.collect(ENGAGEMENT, state, REPLY, replied(4853), task)
    assert deps.output_store.rows == []  # type: ignore[attr-defined]

    monkeypatch.setattr(ReplyOwner, "extra", False)
    written = await nodes.collect(ENGAGEMENT, state, REPLY, replied(4853), task)
    assert [x.id for x in written["exceptions"]] == [REPLY_EXCEPTION]


@pytest.mark.usefixtures("reply_node")
async def test_collect_clash_with_a_store_edit_is_a_node_error(tmp_path: Path) -> None:
    """停在 TST-M02 的等待时审计师改了回函抽取结果 (同 DataService.edit_confirmation, 只写 Store, 函证出 v+1). 提交
    回复后 owner 按状态里的 v 也出 v+1, 与 Store 中那一版撞号而内容不同: 节点报错 (记入 run_error), 整理出的例外与
    函证都不写, 审计师那一版留着, 不静默丢掉回复金额."""
    s = await at_manual(tmp_path, REPLY)
    first = min((await s.runner.app.aget_state(CONFIG)).values["confirmations"], key=lambda c: c.id)
    edited = first.model_copy(update={"version": first.version + 1, "confirmed_balance_cents": 111})
    async with s.output_store.transaction(EID):
        await s.output_store.put(EID, [edited], actor="U-1")

    await s.submit.submit(EID, replied(4853))
    await wait_idle(s.runner, EID)

    error = s.runner.run_error(EID) or ""
    assert f"{REPLY} 整理出的对象与 Store 中已有的同版本内容不同: {first.id} v{first.version + 1}" in error
    assert [r for r in s.output_store.rows if isinstance(r, AuditException) and r.id == REPLY_EXCEPTION] == []
    assert latest(s, Confirmation, first.id) == edited


def test_collected_patch_checks_its_own_run() -> None:
    """整理结果的片段 (check_collected) 照 check_patch 的 run_id 为 None 查, 另按派发的那次执行查只能由本次执行写的对象
    (check_own_run): 关系图的 run_id 为别的执行被拒, 为派发的那次照收."""
    state = initial_state(ENGAGEMENT, [make_procedure(REPLY, Phase.P3_Completion)], 1)
    graph = OwnershipGraph(
        id="OWNERSHIP",
        version=1,
        run_id="RUN-GEN-08-1",
        reporting_entity_id="ACN-1",
        nodes=[PartyNode.model_validate({"id": "ACN-1", "kind": "company", "name": "Numbat Pty Ltd"})],
        links=[],
    )
    where: dict[str, Any] = {
        "writer": AgentId.A5,
        "procedure_id": REPLY,
        "state": state,
        "outputs": ["ownership_graph"],
    }
    with pytest.raises(
        MergeRejected, match=f"ownership_graph 中 OWNERSHIP 的 run_id RUN-GEN-08-1 不是本次执行 {REPLY_RUN}"
    ):
        check_collected({"ownership_graph": graph}, run_id=REPLY_RUN, **where)
    check_collected({"ownership_graph": graph.model_copy(update={"run_id": REPLY_RUN})}, run_id=REPLY_RUN, **where)


# ── R93 验收 4, 5: 重放与恢复补做 ──

shared_loop = pytest.mark.asyncio(loop_scope="module")  # 用模块共用的在线基线 (reply_online) 的用例与它同一个事件循环


@dataclass(frozen=True)
class Baseline:
    """不中断的一次的快照: Store 全部对象, 检查点状态, 决定留痕的幂等键."""

    store: list[Any]
    graph: Any
    decisions: list[str]


def decision_keys(s: Started) -> list[str]:
    return sorted(e.idempotency_key or "" for e in entries(s, TrailKind.decision))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def reply_online(tmp_path_factory: pytest.TempPathFactory) -> Baseline:
    """TST-M02 不中断的一次: 派发, 提交 4853, 停在 FIN-01 闸门. 重放与恢复补做的两例都与它比对, 模块内只跑一遍
    (模块级夹具用不了用例的 monkeypatch, 登记在这里另做, 跑完即撤)."""
    with pytest.MonkeyPatch.context() as mp:
        register(mp, REPLY)
        online = await at_manual(tmp_path_factory.mktemp("reply-online"), REPLY)
        await submit(online, replied(4853))
        return Baseline(store_state(online.output_store), await graph_state(online), decision_keys(online))  # type: ignore[arg-type]


@shared_loop
@pytest.mark.usefixtures("reply_node")
async def test_collect_replays_after_crash(tmp_path: Path, reply_online: Baseline) -> None:
    """R93 验收 4: collect 的结果写进 Store 后, 写检查点前被杀; 再继续时节点从头执行: 不再调 prepare (执行已是终态,
    取 Store), collect 重算出同一对象, Store 与留痕与不中断时相同. ReplyOwner 按 ctx.state 取例外编号并给函证出新
    版本: 重放时它看到的状态须同在线 (不含自己上次写进 Store 的例外与函证新版本), 否则多出 MAN-2 与又一版函证."""
    store = CrashAfterCommit(REPLY_EXCEPTION)
    s = await at_manual(tmp_path / "crash", REPLY, output_store=store)
    await s.submit.submit(EID, replied(4853))
    await wait_idle(s.runner, EID)
    assert "被杀" in (s.runner.run_error(EID) or "")

    await s.runner.app.ainvoke(None, CONFIG)

    assert CALLS == [("prepare", REPLY), ("check", REPLY), ("collect", REPLY), ("collect", REPLY)]
    assert (await s.next_nodes()) == ("gate_fin",)
    assert sorted({x.id for x in store.rows if isinstance(x, AuditException) and x.procedure_id == REPLY}) == [
        REPLY_EXCEPTION
    ]
    assert store_state(store) == reply_online.store  # 函证的版本也同在线
    assert await graph_state(s) == reply_online.graph  # type: ignore[arg-type]
    assert decision_keys(s) == reply_online.decisions


class FlakyQC(QCAgent):
    """FIN-04 第 1 次执行失败 (A1 转成失败执行与 failed 任务), 重跑时照常."""

    async def execute(self, ctx: RunContext) -> UncorrectedSchedule:
        if ctx.run.run_no == 1:
            raise RuntimeError("FIN-04 第 1 次失败")
        return await super().execute(ctx)


def flaky(deps: AgentDeps) -> dict[str, Any]:
    return {"FIN-04": FlakyQC(deps)}


def rerun_fin04(s: Started) -> ReviewRecord:
    task = latest(s, ReviewTask, "T-RUN-FIN-04-1-v1")
    return submitted(Decision.rerun, id="D-R", task_id=task.id, manual_result=None)


@shared_loop
@pytest.mark.usefixtures("reply_node")
async def test_unread_result_and_rerun_recovered_in_node_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reply_online: Baseline
) -> None:
    """R93 验收 5:
    - 决定 D-M02 写了 Store (留痕没写成) 而检查点停在 TST-M02 的等待: recover 补写留痕, 交回 D-M02, 结果与在线相同;
    - 同一节点 TST-M02 之后 FIN-04 失败再 rerun: 在线依次提交 D-M02, D-R. 读回 D-M02 后 FIN-04 的失败执行写 Store 后
      被杀, D-R 也写进 Store 而检查点仍停在 TST-M02 的等待: recover 按节点内程序次序交回 D-M02, D-R, 结果与在线相同."""
    lost = await at_manual(tmp_path / "lost", REPLY)
    await submit_unread(lost, replied(4853), monkeypatch, lose_trail=True)  # type: ignore[arg-type]
    await lost.runner.recover()
    await wait_idle(lost.runner, EID)
    assert lost.runner.run_error(EID) is None
    assert [e.idempotency_key for e in entries(lost, TrailKind.decision)].count("D-M02") == 1
    assert store_state(lost.output_store) == reply_online.store
    assert await graph_state(lost) == reply_online.graph  # type: ignore[arg-type]

    online = await at_manual(tmp_path / "online-rerun", REPLY, agents=flaky)
    await submit(online, replied(4853))
    assert waiting_payloads(await online.runner.app.aget_state(CONFIG))[0]["procedure_id"] == "FIN-04"
    await submit(online, rerun_fin04(online))
    assert (await online.next_nodes()) == ("gate_fin",)
    store = CrashAfterCommit("RUN-FIN-04-1")
    crashed = await at_manual(tmp_path / "crash-rerun", REPLY, output_store=store, agents=flaky)
    await crashed.submit.submit(EID, replied(4853))
    await wait_idle(crashed.runner, EID)
    assert "被杀" in (crashed.runner.run_error(EID) or "")
    snapshot = await crashed.runner.app.aget_state(CONFIG)
    decision = rerun_fin04(crashed)
    await crashed.submit._apply_review(EID, decision, latest(crashed, ReviewTask, decision.task_id), snapshot)
    order = delivered(crashed, monkeypatch)  # type: ignore[arg-type]

    await crashed.runner.recover()
    await wait_idle(crashed.runner, EID)

    assert crashed.runner.run_error(EID) is None
    assert order == ["D-M02", "D-R"]
    assert (await crashed.next_nodes()) == ("gate_fin",)
    assert store_state(store) == store_state(online.output_store)
    assert await graph_state(crashed) == await graph_state(online)  # type: ignore[arg-type]


class FlakyReplyOwner(ReplyOwner):
    """第 1 次执行的 prepare 抛错 (派发失败, A1 建 failed 任务), rerun 后照常."""

    def prepare(self, ctx: RunContext) -> dict[str, Any]:
        if ctx.run.run_no == 1:
            raise RuntimeError("材料取不到")
        return super().prepare(ctx)


async def rerun_then_reply(s: Started) -> None:
    """对派发失败的 failed 任务 rerun (D-R0), 再对第 2 次派发的 manual 任务提交回复 4853 (D-M2), 等后台停下."""
    failed: ReviewTask = latest(s, ReviewTask, f"T-RUN-{REPLY}-1-v1")
    assert failed.kind is TaskKind.failed
    await submit(s, submitted(Decision.rerun, id="D-R0", task_id=failed.id, manual_result=None))
    task: ReviewTask = latest(s, ReviewTask, f"T-RUN-{REPLY}-2-v1")
    assert (task.kind, task.status) == (TaskKind.manual, TaskStatus.pending)
    await s.submit.submit(EID, replied(4853, "D-M2").model_copy(update={"task_id": task.id}))
    await wait_idle(s.runner, EID)


@pytest.mark.usefixtures("reply_node")
async def test_dispatch_rerun_then_result_recovered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """派发失败 → rerun D-R0 → 第 2 次派发 → manual_result D-M2:
    - 在线: collect 的 ctx.run 为最新一次 (第 2 次) 执行, 例外 X-TST-M02-2-MAN-1 挂在 RUN-TST-M02-2 下, 停在 FIN-01;
    - collect 写 Store 后被杀: 等待中的节点带着错误, recover 复制检查点后按 D-R0, D-M2 交回 (manual 任务的决定排在
      触发该次派发的 rerun 之后; 先交 D-M2 时它落到第 1 次执行的等待上, 找不到所属执行), 结果同在线."""
    monkeypatch.setattr(registry, "AGENT_CLASSES", (*registry.AGENT_CLASSES[:-1], FlakyReplyOwner))
    online = await at_manual(tmp_path / "online", REPLY)
    await rerun_then_reply(online)
    assert online.runner.run_error(EID) is None
    exception: AuditException = latest(online, AuditException, f"X-{REPLY}-2-MAN-1")
    assert exception.run_id == f"RUN-{REPLY}-2"
    assert (await online.next_nodes()) == ("gate_fin",)

    store = CrashAfterCommit(f"X-{REPLY}-2-MAN-1")
    crashed = await at_manual(tmp_path / "crash", REPLY, output_store=store)
    await rerun_then_reply(crashed)
    assert "被杀" in (crashed.runner.run_error(EID) or "")
    order = delivered(crashed, monkeypatch)  # type: ignore[arg-type]

    await crashed.runner.recover()
    await wait_idle(crashed.runner, EID)

    assert crashed.runner.run_error(EID) is None
    assert order == ["D-R0", "D-M2"]
    assert (await crashed.next_nodes()) == ("gate_fin",)
    assert store_state(store) == store_state(online.output_store)
    assert await graph_state(crashed) == await graph_state(online)  # type: ignore[arg-type]


# ── R92 按请求核对的边界 (纯函数) ──


@pytest.mark.parametrize(
    ("result_model", "decided_at", "performed_on", "details", "problem"),
    [
        (None, DECIDED_AT, date(2026, 10, 23), {}, None),  # 没有结果模型, details 为空
        (
            None,
            DECIDED_AT,
            date(2026, 10, 23),
            {"signatory": "A"},
            "TST-M01 没有结果模型, manual_result.details 须为空",
        ),
        # 10-23 14:00 UTC 是悉尼 10-24 01:00: 10-24 执行不算晚 (按 UTC 日期会误拒)
        (
            "TstLetter",
            datetime(2026, 10, 23, 14, tzinfo=UTC),
            date(2026, 10, 24),
            {"signed_on": "2026-10-24", "signatory": "A"},
            None,
        ),
        (
            "TstLetter",
            DECIDED_AT,
            date(2026, 10, 23),
            {"signed_on": "2026-10-23", "signatory": "A", "extra": 1},
            "TstLetter: extra",
        ),
        # 派发之后结果模型名从登记里拿掉了 (或模板改了名): 旧任务的请求取不到模型, 拒绝而不是 500
        (
            "TstGone",
            DECIDED_AT,
            date(2026, 10, 23),
            {"signed_on": "2026-10-23", "signatory": "A"},
            "结果模型 TstGone 未登记 (contracts/manual_results), 无从核对 manual_result.details",
        ),
    ],
    ids=["no_model_empty_details", "no_model_with_details", "sydney_date", "extra_field", "unregistered_model"],
)
def test_result_checked_against_the_request(
    result_model: str | None, decided_at: datetime, performed_on: date, details: dict[str, Any], problem: str | None
) -> None:
    """check_manual_result 按任务请求核对: 没有结果模型时 details 须为空; 执行日期按决定时刻的悉尼日期比; 结果模型
    多出的字段也报 (TstLetter 继承 Model, extra forbid); 请求里的模型没登记报 INVALID_VALUE. 通过时返回的结果证据指针
    按文件登记改写, 其余原样."""
    request = ManualRequest(instructions=INSTRUCTIONS, result_model=result_model, min_evidence=2, material={})
    task = ReviewTask(
        id=TASK,
        kind=TaskKind.manual,
        procedure_id=TST,
        target_type=TargetType.ProcedureRun,
        target_id=RUN,
        target_version=1,
        run_id=RUN,
        subject_id="RESULT",
        created_at=NOW,
        manual_request=request,
    )
    record = submitted(decided_at=decided_at, manual_result=letter_result(performed_on=performed_on, details=details))
    if problem is None:
        assert record.manual_result is not None
        assert check_manual_result(record, task, [LETTER]) == as_registered(record.manual_result)
        return
    with pytest.raises(DecisionRejected) as e:
        check_manual_result(record, task, [LETTER])
    assert e.value.code == "INVALID_VALUE" and problem in str(e.value)


def test_manual_request_goes_with_manual_tasks_only() -> None:
    """ReviewTask 的模型校验: manual 任务须带请求, 其余类型不能带 (不靠核对时的 assert)."""
    request = ManualRequest(instructions=INSTRUCTIONS, result_model=None, min_evidence=0, material={})
    fields: dict[str, Any] = {
        "id": TASK,
        "procedure_id": TST,
        "target_type": TargetType.ProcedureRun,
        "target_id": RUN,
        "target_version": 1,
        "run_id": RUN,
        "created_at": NOW,
    }
    with pytest.raises(ValidationError, match="manual 任务须带 manual_request"):
        ReviewTask(kind=TaskKind.manual, **fields)
    with pytest.raises(ValidationError, match="其余类型不能带"):
        ReviewTask(kind=TaskKind.failed, manual_request=request, **fields)
    assert ReviewTask(kind=TaskKind.manual, manual_request=request, **fields).manual_request == request
    with pytest.raises(ValidationError, match="min_evidence"):
        ManualRequest(instructions=INSTRUCTIONS, result_model=None, min_evidence=-1, material={})


# ── 派发片段 (纯函数) ──


def dispatch(*tasks: ReviewTask, **fields: Any) -> tuple[StatePatch, dict[str, Any]]:
    """TST-M01 的派发片段 (本次执行 succeeded 与给定任务, 另加 fields) 与 check_dispatch 的其余参数."""
    run = finished(make_run(TST))
    patch: StatePatch = {"procedure_runs": [run], "review_tasks": list(tasks), **fields}
    state = initial_state(ENGAGEMENT, [make_procedure(TST, Phase.P3_Completion)], 1)
    return patch, {"procedure_id": TST, "run_id": run.id, "state": state}


def manual_task(kind: TaskKind = TaskKind.manual) -> ReviewTask:
    request = ManualRequest(instructions=INSTRUCTIONS, result_model=None, min_evidence=0, material={})
    return ReviewTask(
        id=TASK,
        kind=kind,
        procedure_id=TST,
        target_type=TargetType.ProcedureRun,
        target_id=RUN,
        target_version=1,
        run_id=RUN,
        subject_id="RESULT",
        created_at=NOW,
        manual_request=request if kind is TaskKind.manual else None,
    )


def test_dispatch_patch_holds_the_run_and_manual_tasks_only(tmp_path: Path) -> None:
    """派发片段 (check_dispatch) 由 A10 写, 只能含本次执行记录与 manual 任务: 另带 A10 写入范围内的 report_versions
    (模板 outputs 不列, 按 DISPATCH_FIELDS 查) 被拒, 建 failed 任务被拒. 写入者取 writer_of: 人工程序不论由哪个
    Agent 执行都是 A10; 按执行它的非 A10 Agent (脚本 Agent 为 A2, 不能写 review_tasks) 查则被拒."""
    patch, where = dispatch(manual_task())
    check_dispatch(patch, writer=AgentId.A10, **where)
    assert DISPATCH_FIELDS == ("review_tasks",)

    with pytest.raises(MergeRejected, match="未声明产出 report_versions"):
        check_dispatch(dispatch(manual_task(), report_versions=[])[0], writer=AgentId.A10, **where)
    with pytest.raises(MergeRejected, match=f"派发片段只能建 manual 任务, {TASK} 不是"):
        check_dispatch(dispatch(manual_task(TaskKind.failed))[0], writer=AgentId.A10, **where)

    template = manual_library(tmp_path / "knowledge").load(TST)
    agent = ScriptAgent(make_deps(FakeRegistry(), FakeTrail()), lambda ctx: {})
    assert (agent.agent_id, writer_of(template, agent)) == (AgentId.A2, AgentId.A10)
    with pytest.raises(MergeRejected, match="review_tasks"):
        check_dispatch(patch, writer=agent.agent_id, **where)
