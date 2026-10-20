"""JudgmentEngine.call_llm: 渲染提示词, 组装请求, 语法与结构失败重试, 每次尝试一行 llm_call 留痕 (接口文档第 5 节)."""

import asyncio
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from jinja2 import UndefinedError

from backend.adapters.llm import DatasetFixtureLLMClient, MockLLMClient, SwitchableLLMClient, make_llm_client
from backend.agents.base import RunContext
from backend.contracts.knowledge import LLMSpec, ProcedureTemplate
from backend.contracts.llm_outputs import ContractExtraction, JudgmentDraft
from backend.contracts.models import AgentId, DocumentRef, Reliability, RunStatus, TaskKind, TrailEntry, TrailKind
from backend.contracts.ports import LLMMessage, LLMRequest
from backend.contracts.services import ValidationContext
from backend.core.config import get_settings
from backend.main import build_deps
from backend.orchestration.nodes import GraphNodes
from backend.services.judgment_engine import (
    MAX_ATTEMPTS,
    JudgmentEngine,
    OutputValidator,
    prompt_env,
    prompt_hash,
)
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CONTRACTS,
    NUMBAT_PROFILE,
    NUMBAT_RECORDS,
    FakeEngagementStore,
    FakeFinancialStore,
    FakeRegistry,
    FakeTrail,
    ScriptA3,
    ScriptedLLM,
    make_ctx,
    make_deps,
    make_run,
)
from tests.test_output_validator import cutoff_draft

EID = ENGAGEMENT.id
SETTINGS = get_settings()
FIXTURES = SETTINGS.LLM_FIXTURES_ROOT / "DOC-04"
CON001, CON002 = NUMBAT_CONTRACTS[0], NUMBAT_CONTRACTS[1]
RUN = make_run("DOC-04")


def raw(contract: str) -> str:
    """DOC-04 某份合同的合法输出 (测试 fixture)."""
    return (FIXTURES / f"{contract}.json").read_text(encoding="utf-8")


def variables(document: DocumentRef = CON001, pages: list[str] | None = None) -> dict[str, Any]:
    return {
        "client_name": NUMBAT_PROFILE.legal_name,
        "document": document.model_dump(mode="json"),
        "pages": pages if pages is not None else ["Page one text.", "Page two text."],
    }


def setup(llm: Any) -> tuple[JudgmentEngine, FakeTrail]:
    trail = FakeTrail()
    deps = make_deps(FakeRegistry(), trail, financial_store=FakeFinancialStore(list(NUMBAT_CONTRACTS)), llm=llm)
    return deps.judgments, trail


def doc04() -> ProcedureTemplate:
    return make_deps(FakeRegistry(), FakeTrail()).library.load("DOC-04")


async def call(engine: JudgmentEngine, contract: DocumentRef = CON001, key: str = "CON-001", **kw: Any) -> Any:
    template = kw.pop("template", doc04())
    return await engine.call_llm(
        EID,
        template,
        RUN,
        kw.pop("variables", variables(contract)),
        ValidationContext(document=contract, standards_of=None),
        AgentId.A3,
        key,
    )


# ── 渲染 ─────────────────────────────────────────


def test_render_doc04_with_two_pages() -> None:
    engine, _ = setup(ScriptedLLM({}))
    text = engine.render(doc04(), variables())
    assert '<page number="1">\nPage one text.\n</page>' in text
    assert '<page number="2">\nPage two text.\n</page>' in text
    assert "{#" not in text and "变量" not in text and "{%" not in text
    assert text.startswith("You extract structured data") and NUMBAT_PROFILE.legal_name in text


async def test_missing_variable_raises_before_calling_llm() -> None:
    llm = ScriptedLLM({"CON-001": [raw("CON-001")]})
    engine, trail = setup(llm)
    incomplete = {k: v for k, v in variables().items() if k != "client_name"}
    with pytest.raises(UndefinedError):
        engine.render(doc04(), incomplete)
    with pytest.raises(UndefinedError):
        await call(engine, variables=incomplete)
    assert llm.requests == [] and trail.entries == []


@pytest.mark.parametrize(
    ("procedure_id", "actor", "variables"),
    [
        (  # data.document 缺失, 模板经 tojson 输出它
            "REV-03",
            AgentId.A6,
            {
                "subject_id": "CON-001",
                "task": {"params": {"period_end": "2026-06-30"}, "risks": []},
                "data": {"contract": {"document_id": CON001.id}},
                "standards": [],
            },
        ),
        (  # qualified 分支缺 misstatements
            "RPT-03",
            AgentId.A10,
            {
                "client_name": NUMBAT_PROFILE.legal_name,
                "task": {"params": {"period_end": "2026-06-30"}},
                "revision": 1,
                "opinion_type": "qualified",
                "fin01_reason": "Uncorrected misstatements",
                "schedule": {"total_cents": 100},
                "judgments": [],
                "previous": None,
                "annotations": [],
                "standards": [],
            },
        ),
    ],
)
async def test_missing_variable_through_tojson_raises_undefined(
    procedure_id: str, actor: AgentId, variables: dict[str, Any]
) -> None:
    llm = ScriptedLLM({})
    engine, trail = setup(llm)
    template = make_deps(FakeRegistry(), FakeTrail()).library.load(procedure_id)
    with pytest.raises(UndefinedError):
        await engine.call_llm(
            EID, template, make_run(procedure_id), variables, ValidationContext(standards_of=None), actor
        )
    assert llm.requests == [] and trail.entries == []


def test_json_default_keeps_type_error_for_other_objects() -> None:
    env = prompt_env(SETTINGS.KNOWLEDGE_ROOT / "prompts")
    with pytest.raises(UndefinedError, match="missing"):
        env.from_string("{{ [1, missing] | tojson }}").render()
    with pytest.raises(TypeError, match="object"):
        env.from_string("{{ value | tojson }}").render(value=object())
    assert (
        env.from_string("{{ value | tojson }}").render(value={"b": 1, "a": "<&>"})
        == '{"a": "\\u003c\\u0026\\u003e", "b": 1}'
    )


def test_render_does_not_escape_html() -> None:
    engine, _ = setup(ScriptedLLM({}))
    text = engine.render(doc04(), variables(pages=["<text>&中文", "second page"]))
    assert "<text>&中文" in text


# ── 请求 ─────────────────────────────────────────


async def test_request_fields() -> None:
    llm = ScriptedLLM({"CON-001": [raw("CON-001")]})
    engine, _ = setup(llm)
    result = await call(engine)
    [req] = llm.requests
    assert (req.procedure_id, req.run_id, req.fixture_key) == ("DOC-04", "RUN-DOC-04-1", "CON-001")
    assert req.eid == EID  # R78: mock 按它选 fixture 目录
    assert req.temperature == 0 and req.max_tokens == 4096
    assert json.dumps(ContractExtraction.model_json_schema(), indent=2) in req.system
    assert req.messages == [LLMMessage(role="user", content=engine.render(doc04(), variables()))]
    assert result.attempts == 1 and result.report.level_failed is None and len(result.trail_ids) == 1


async def test_max_tokens_from_template() -> None:
    assert LLMSpec(prompt="x.md.j2", output_model="JudgmentDraft").max_tokens == 4096
    template = doc04()
    assert template.llm is not None
    custom = template.model_copy(update={"llm": template.llm.model_copy(update={"max_tokens": 1500})})
    llm = ScriptedLLM({"CON-001": [raw("CON-001")]})
    engine, _ = setup(llm)
    await call(engine, template=custom)
    assert llm.requests[0].max_tokens == 1500


def test_max_tokens_read_from_yaml() -> None:
    data = doc04().model_dump(mode="json")
    data["llm"]["max_tokens"] = 2000
    assert ProcedureTemplate.model_validate(data).llm.max_tokens == 2000  # type: ignore[union-attr]


# ── 重试 ─────────────────────────────────────────


async def test_retry_after_syntax_error_appends_raw_and_errors() -> None:
    llm = ScriptedLLM({"CON-001": ["not json", raw("CON-001")]})
    engine, _ = setup(llm)
    result = await call(engine)
    assert result.attempts == 2 and result.report.level_failed is None and len(result.trail_ids) == 2
    first, second = llm.requests
    assert second.messages[0] == first.messages[0] and len(second.messages) == 3
    assistant, retry = second.messages[1:]
    assert assistant == LLMMessage(role="assistant", content="not json")
    assert retry.role == "user" and "Output is not valid JSON" in retry.content
    assert second.system == first.system


async def test_three_syntax_or_structure_failures_stop() -> None:
    llm = ScriptedLLM({"CON-001": ["oops", "{}", "still bad", raw("CON-001")]})
    engine, _ = setup(llm)
    result = await call(engine)
    assert result.attempts == MAX_ATTEMPTS == 3 and len(llm.requests) == 3 and len(result.trail_ids) == 3
    assert result.report.level_failed == "syntax" and result.report.retryable
    assert "still bad" not in result.report.errors[0] and "not valid JSON" in result.report.errors[0]
    assert len(llm.requests[2].messages) == 5  # user, 第 1 次原文与错误, 第 2 次原文与错误
    assert "does not match ContractExtraction" in llm.requests[2].messages[4].content


async def test_fixture_not_found_retries_three_times(tmp_path: Path) -> None:
    engine, trail = setup(MockLLMClient(tmp_path))
    result = await call(engine)
    assert result.attempts == 3 and result.report.level_failed == "syntax"
    assert result.report.errors[-1] == "fixture_not_found"
    assert len(trail.entries) == 3


async def test_reference_failure_is_not_retried() -> None:
    bad = json.loads(raw("CON-001"))
    bad["evidence"][0]["page"] = 9
    llm = ScriptedLLM({"CON-001": [json.dumps(bad), raw("CON-001")]})
    engine, _ = setup(llm)
    result = await call(engine)
    assert result.attempts == 1 and len(llm.requests) == 1
    assert result.report.level_failed == "reference" and not result.report.retryable


async def test_semantic_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = cutoff_draft().model_copy(update={"reasoning_chain": []})
    llm = ScriptedLLM({"SI-2026-0612": [draft.model_dump_json(), cutoff_draft().model_dump_json()]})
    trail = FakeTrail()
    deps = make_deps(
        FakeRegistry(), trail, financial_store=FakeFinancialStore(list(NUMBAT_CONTRACTS), NUMBAT_RECORDS), llm=llm
    )
    engine = deps.judgments
    monkeypatch.setattr(engine, "render", lambda template, variables: "REV-02 prompt")
    result = await engine.call_llm(
        EID,
        deps.library.load("REV-02"),
        make_run("REV-02"),
        {},
        ValidationContext(standards_of=None),
        AgentId.A6,
        "SI-2026-0612",
    )
    assert result.attempts == 1 and result.report.level_failed == "semantic"
    assert isinstance(result.report.output, JudgmentDraft)


# ── 留痕 ─────────────────────────────────────────


async def test_one_llm_call_trail_per_attempt() -> None:
    llm = ScriptedLLM({"CON-001": ["not json", raw("CON-001")]})
    engine, trail = setup(llm)
    result = await call(engine)
    assert result.trail_ids == ["TR-1", "TR-2"]
    for attempt, (entry, req) in enumerate(zip(trail.entries, llm.requests, strict=True), start=1):
        assert entry.kind is TrailKind.llm_call and entry.actor == AgentId.A3 and entry.engagement_id == EID
        assert (entry.procedure_id, entry.run_id, entry.model) == ("DOC-04", "RUN-DOC-04-1", "scripted")
        assert (entry.input_tokens, entry.output_tokens) == (100 * len(req.messages), len(entry.payload["raw"]))
        assert entry.prompt_hash == prompt_hash(req.system, req.messages)
        assert re.fullmatch(r"[0-9a-f]{64}", entry.prompt_hash or "")
        assert entry.payload["attempt"] == attempt and entry.payload["fixture_key"] == "CON-001"
        assert entry.payload["system"] == req.system
        assert entry.payload["messages"] == [m.model_dump() for m in req.messages]
    first, second = trail.entries
    assert first.payload["raw"] == "not json" and first.payload["level_failed"] == "syntax"
    assert first.payload["errors"] and first.summary == "DOC-04 CON-001 第 1 次调用大模型: syntax"
    assert second.payload["level_failed"] is None and second.payload["errors"] == []
    assert second.summary == "DOC-04 CON-001 第 2 次调用大模型: passed"
    assert first.prompt_hash != second.prompt_hash


def test_prompt_hash_matches_independent_canonical_json() -> None:
    canonical = '{"messages":[{"content":"中文\\n<raw>&","role":"user"}],"system":"系统"}'
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert prompt_hash("系统", [LLMMessage(role="user", content="中文\n<raw>&")]) == expected


class FailingTrail(FakeTrail):
    """第 fail_on 次写入抛错, 之前的写入照常."""

    def __init__(self, fail_on: int) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.calls = 0
        self.failure = RuntimeError("trail unavailable")

    async def record(self, entry: TrailEntry) -> str:
        self.calls += 1
        if self.calls == self.fail_on:
            raise self.failure
        return await super().record(entry)


@pytest.mark.parametrize(
    ("replies", "fail_on"),
    [(["not json", "{}"], 1), (["valid", "{}"], 1), (["not json", "valid", "{}"], 2)],
)
async def test_trail_failure_propagates_and_stops_calling(replies: list[str], fail_on: int) -> None:
    llm = ScriptedLLM({"CON-001": [raw("CON-001") if r == "valid" else r for r in replies]})
    trail = FailingTrail(fail_on)
    deps = make_deps(FakeRegistry(), trail, financial_store=FakeFinancialStore(list(NUMBAT_CONTRACTS)), llm=llm)
    engine = deps.judgments
    with pytest.raises(RuntimeError) as caught:
        await call(engine)
    assert caught.value is trail.failure
    assert len(llm.requests) == fail_on and len(trail.entries) == fail_on - 1


async def test_trail_failure_becomes_failed_run() -> None:
    """留痕写失败按 Agent 异常处理: A1 写失败的 Run 与 failed 任务."""
    trail = FailingTrail(1)
    deps = make_deps(FakeRegistry(), trail, llm=ScriptedLLM({"CON-001": [raw("CON-001")]}))

    class CallingAgent(ScriptA3):
        async def execute(self, ctx: RunContext) -> None:
            await self.deps.judgments.call_llm(
                EID,
                ctx.template,
                ctx.run,
                variables(),
                ValidationContext(document=CON001, standards_of=None),
                self.agent_id,
                "CON-001",
            )

    patch, writer = await GraphNodes(deps, parallel_cap=2).run_agent(
        CallingAgent(deps, lambda ctx: {}), make_ctx(deps, "DOC-04")
    )
    assert writer is AgentId.A1
    [run] = patch["procedure_runs"]
    assert run.status is RunStatus.failed and run.failure_reason == "RuntimeError: trail unavailable"
    assert patch["review_tasks"][0].kind is TaskKind.failed


async def test_same_input_same_hash() -> None:
    hashes = []
    for _ in range(2):
        engine, trail = setup(ScriptedLLM({"CON-001": [raw("CON-001")]}))
        await call(engine)
        hashes.append(trail.entries[0].prompt_hash)
    assert hashes[0] == hashes[1]
    assert prompt_hash("s", [LLMMessage(role="user", content="中文")]) != prompt_hash("s", [])


async def test_concurrent_calls_keep_separate_conversations() -> None:
    llm = ScriptedLLM({"CON-001": ["{}", raw("CON-001")], "CON-002": [raw("CON-002")]})
    engine, trail = setup(llm)
    a, b = await asyncio.gather(
        call(engine, CON001, "CON-001"),
        call(engine, CON002, "CON-002"),
    )
    assert (a.attempts, b.attempts) == (2, 1)
    assert a.report.level_failed is None and b.report.level_failed is None
    a_reqs = [r for r in llm.requests if r.fixture_key == "CON-001"]
    [b_req] = [r for r in llm.requests if r.fixture_key == "CON-002"]
    assert [m.role for m in a_reqs[1].messages] == ["user", "assistant", "user"]
    assert a_reqs[1].messages[0].content == engine.render(doc04(), variables(CON001))
    assert a_reqs[1].messages[1].content == "{}"
    assert b_req.messages == [LLMMessage(role="user", content=engine.render(doc04(), variables(CON002)))]
    assert len(trail.entries) == 3
    assert Counter(e.payload["fixture_key"] for e in trail.entries) == {"CON-001": 2, "CON-002": 1}
    attempts = {
        k: [e.payload["attempt"] for e in trail.entries if e.payload["fixture_key"] == k]
        for k in ("CON-001", "CON-002")
    }
    assert attempts == {"CON-001": [1, 2], "CON-002": [1]}
    assert set(a.trail_ids).isdisjoint(b.trail_ids)


# ── mock 客户端与组装 ─────────────────────────────────────────


async def test_mock_client_with_doc04_fixture_passes() -> None:
    # 组装根的客户端绑着 Postgres 项目存储 (按项目数据集选目录要查项目), 这里用不带项目存储的同一种客户端, 不连库
    engine, _ = setup(make_llm_client(SETTINGS))
    result = await call(engine)
    assert result.attempts == 1 and result.report.level_failed is None
    assert {p.reliability for p in result.report.output.evidence} == {Reliability.external_copy}


def test_build_deps_and_make_deps_wire_engine() -> None:
    real = build_deps(SETTINGS).judgments
    assert isinstance(real, JudgmentEngine) and isinstance(real.validator, OutputValidator)
    trail = FakeTrail()
    financial = FakeFinancialStore()
    deps = make_deps(FakeRegistry(), trail, financial_store=financial)
    assert deps.judgments.trail is trail and deps.judgments.llm is deps.llm
    assert deps.judgments.validator.financial is financial and deps.judgments.validator.trail is trail


# ── R78: mock 按项目数据集选 fixture 目录 ──


def request(eid: str, key: str = "CON-001") -> LLMRequest:
    return LLMRequest(
        procedure_id="DOC-04", run_id="RUN-DOC-04-1", system="s", messages=[], max_tokens=10, fixture_key=key, eid=eid
    )


async def test_dataset_fixture_client_picks_directory_by_engagement(tmp_path: Path) -> None:
    """验收 2: demo 项目读 LLM_FIXTURES_ROOT; xero/20261017-q1 且补造目录有 llm/ 时读它 (同一客户端两个项目各调一次,
    各读到自己目录的文件); 没有补造 llm/ 的 Xero 快照, eid 为空, 查不到项目都读 LLM_FIXTURES_ROOT. 目录内读法同
    MockLLMClient: 退回 default.json, 都没有为 fixture_not_found."""
    root, datasets = tmp_path / "llm", tmp_path / "data"
    xero = datasets / "xero_demo" / "20261017-q1" / "llm" / "DOC-04"
    for folder, contract in ((root / "DOC-04", "CON-001"), (xero, "CON-002")):
        folder.mkdir(parents=True)
        (folder / "CON-001.json").write_text(raw(contract), encoding="utf-8")
    (xero / "default.json").write_text(raw("CON-003"), encoding="utf-8")
    engagements = FakeEngagementStore(
        [NUMBAT_PROFILE],
        [
            ENGAGEMENT,
            ENGAGEMENT.model_copy(update={"id": "ENG-X", "dataset": "xero/20261017-q1"}),
            ENGAGEMENT.model_copy(update={"id": "ENG-M", "dataset": "xero/20261017-mock"}),
        ],
    )
    client = DatasetFixtureLLMClient(root, datasets, engagements)

    async def read(eid: str, key: str = "CON-001") -> str | None:
        response = await client.complete(request(eid, key), ContractExtraction)
        return response.raw if response.error is None else response.error

    assert (await read(EID), await read("ENG-X")) == (raw("CON-001"), raw("CON-002"))
    assert await read("ENG-X", "CON-999") == raw("CON-003")  # 退回 default.json
    for eid in ("ENG-M", "", "ENG-NONE"):
        assert await read(eid) == raw("CON-001"), eid
    assert await read(EID, "CON-999") == "fixture_not_found"
    assert await DatasetFixtureLLMClient(root, datasets).directory("ENG-X") == root  # 没有项目存储时总读根目录
    # 快照名只能是一段目录名: 不校验时会指向的目录都先建好, 校验去掉任一条这里就会读到它们
    for dataset, would_be in (
        ("xero/..", datasets / "llm"),  # xero_demo/../llm, 跳出补造目录
        ("xero/.", datasets / "xero_demo" / "llm"),  # xero_demo/./llm
        ("xero/20261017-q1/llm", xero.parent / "llm"),  # 多段
        ("xero/", datasets / "xero_demo" / "llm"),  # 空快照名
    ):
        would_be.mkdir(parents=True, exist_ok=True)
        eid = f"ENG-{len(engagements.engagements)}"
        engagements.engagements.append(ENGAGEMENT.model_copy(update={"id": eid, "dataset": dataset}))
        assert await client.directory(eid) == root, dataset
    assert await client.directory("ENG-X") == xero.parent


def test_mock_mode_builds_dataset_fixture_client() -> None:
    """验收 2: mock 模式下 make_llm_client 构造 DatasetFixtureLLMClient (根目录为 LLM_FIXTURES_ROOT); 组装根与
    make_deps 的主模型与复核模型都包着它, make_deps 的绑定在假项目存储上."""
    client = make_llm_client(SETTINGS)
    assert isinstance(client, DatasetFixtureLLMClient) and client.root == SETTINGS.LLM_FIXTURES_ROOT
    assert client.dataset_root == SETTINGS.DATASET_ROOT and client.engagements is None
    assembled = build_deps(SETTINGS)  # 只构造, 不连库
    for switchable in (assembled.llm, assembled.review_llm):
        assert isinstance(switchable, SwitchableLLMClient) and isinstance(switchable.inner, DatasetFixtureLLMClient)
        assert switchable.inner.engagements is assembled.engagement_store  # 组装根绑定项目存储, Xero 项目才读补造目录
    engagements = FakeEngagementStore([NUMBAT_PROFILE], [ENGAGEMENT])
    deps = make_deps(FakeRegistry(), FakeTrail(), engagement_store=engagements)
    for switchable in (deps.llm, deps.review_llm):
        assert isinstance(switchable, SwitchableLLMClient)
        assert isinstance(switchable.inner, DatasetFixtureLLMClient) and switchable.inner.engagements is engagements


async def test_prompt_hash_ignores_eid() -> None:
    """验收 1: prompt_hash 只算 system 与 messages, 加了 eid 后留痕的 prompt_hash 不变."""
    llm = ScriptedLLM({"CON-001": [raw("CON-001")]})
    engine, trail = setup(llm)
    await call(engine)
    [req], [entry] = llm.requests, trail.entries
    assert entry.prompt_hash == prompt_hash(req.system, req.messages)
    assert "eid" not in entry.payload
