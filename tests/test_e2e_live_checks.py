"""真实大模型验收 (test_e2e_live) 自身的离线检查 (Codex R59 探针): 存储换成假实现, 大模型换成在 fixture 上注入
指定错误的客户端, 把整条验收测试离线跑一遍, 确认它对合理的格式差异放行, 对错误结果拒绝, 失败后恢复的路径不崩."""

import json
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from backend.adapters.llm import LLMTransportError, MockLLMClient
from backend.agents.base import AgentDeps, ProcedureAgent
from backend.contracts.models import (
    AuditReportVersion,
    JudgmentRef,
    OpinionType,
    ReportSection,
    ReviewTask,
    TaskKind,
    TaskStatus,
)
from backend.contracts.ports import LLMRequest, LLMResponse, T_out
from tests import test_e2e_live as live
from tests.fakes import (
    ENGAGEMENT,
    NUMBAT_CONTRACTS,
    NUMBAT_RECORDS,
    NUMBAT_TRIAL_BALANCE,
    FakeFinancialStore,
    make_runner,
)
from tests.test_decisions import Started
from tests.test_e2e_mock import REPO, RealRun, real_agents
from tests.test_p2_agents_demo import replies


def facts_in_request(req: LLMRequest) -> bool:
    """报告请求 (提示词与对话) 是否给出了截止性错报的签收日与控制权转移条款: 模型只能写请求里有的事实."""
    text = req.system + "".join(m.content for m in req.messages)
    return "2026-07-03" in text and "5.1" in text


class InjectingLLM:
    """读 tests/fixtures/llm 的输出, 按 mode 在指定调用上改动后返回 (模型名 probe-model-2026, token 各 100)."""

    def __init__(self, mode: str) -> None:
        self.mode = mode  # 注入哪种错误
        self.fixtures = MockLLMClient(REPO / "tests" / "fixtures" / "llm")
        self.calls: Counter[tuple[str, str]] = Counter()  # (程序, fixture_key) -> 第几次调用

    async def complete(self, req: LLMRequest, output_model: type[T_out]) -> LLMResponse[T_out]:
        key = (req.procedure_id, req.fixture_key)
        self.calls[key] += 1
        n = self.calls[key]
        data = json.loads((await self.fixtures.complete(req, output_model)).raw)
        if self.mode == "name_case" and req.procedure_id == "DOC-04":
            data["counterparty"] = data["counterparty"].upper()
        if self.mode == "run_then_subject" and key == ("DOC-04", "CON-003"):
            if n == 1:
                raise LLMTransportError("模拟整次执行失败")
            if n == 2:
                data["evidence"][0]["source_id"] = "DOC-NOT-REGISTERED"  # 单对象校验失败
        if self.mode == "wrong_after_failed" and key == ("DOC-04", "CON-003"):
            if n == 1:
                data["evidence"][0]["source_id"] = "DOC-NOT-REGISTERED"
            else:
                data["counterparty"] = "Definitely Wrong Counterparty Pty Ltd"  # 重跑后校验通过但结果错
        if self.mode == "report_without_facts" and req.procedure_id == "RPT-03":
            data["basis_for_opinion"] = "Basis for Qualified Opinion\n\nThere is an unspecified issue."
        if self.mode == "report_wrong_clause" and key == ("RPT-03", "r2"):  # 第 2 次真实联调中出现过
            basis = data["basis_for_opinion"].replace("Clause 5.1 of contract CON-001", "Clause CON-001-T2")
            data["basis_for_opinion"] = basis
        if self.mode in ("overstatement_wording", "understatement_wording") and req.procedure_id == "RPT-03":
            word = "an overstatement" if self.mode == "overstatement_wording" else "an understatement"
            data["basis_for_opinion"] = data["basis_for_opinion"].replace(
                "is overstated by $142,000", f"includes {word} of $142,000"
            )
        # R59-06: 截止性判断失败, 审计师对例外认定错报, 报告引人工判断
        if self.mode in ("cutoff_unattributed", "cutoff_unattributed_bare"):
            if key == ("REV-02", "SI-2026-0612"):
                del data["conclusion"]  # 每次都缺必填字段, 结构级失败重试耗尽, hybrid 有例外即 unattributed
            if req.procedure_id == "RPT-03":  # (Codex R59-09) 只写请求里给了的事实, 不沿用 fixture 自带的
                data["judgment_refs"] = [{"id": "J-REV-02-1-SI-2026-0612-H1", "version": 1}]
                if not facts_in_request(req):
                    data["basis_for_opinion"] = "Basis for Qualified Opinion\n\nRevenue is overstated by $142,000."
        if self.mode in ("cutoff_wrong_document", "cutoff_wrong_page") and key == ("REV-02", "SI-2026-0612"):
            for pointer in [*data["evidence"], *(p for step in data["reasoning_chain"] for p in step["evidence"])]:
                if pointer["source_type"] == "document":  # R59-07: 指针仍能解析, 但不是 CON-001 第 5.1 条
                    if self.mode == "cutoff_wrong_document":
                        pointer["source_id"] = NUMBAT_CONTRACTS[1].id
                    else:
                        pointer["page"] = 3
        if self.mode == "critic_disagrees" and req.procedure_id == "GEN-09" and req.fixture_key.endswith("-r2"):
            data.update(critic_verdict="inconsistent", critic_reason_type="different_conclusion")  # 独立复核不同意
        try:
            output = output_model.model_validate(data)
            error = None
        except ValidationError as e:
            output, error = None, str(e)
        return LLMResponse[output_model](  # type: ignore[valid-type]
            output=output,
            raw=json.dumps(data),
            model="probe-model-2026",
            input_tokens=100,
            output_tokens=100,
            error=error,
        )


def offline(monkeypatch: pytest.MonkeyPatch, mode: str) -> dict[str, RealRun]:
    """把验收测试的配置读取与真实存储组装换成假实现; 返回的字典在运行后装着那次的 RealRun."""
    observed: dict[str, RealRun] = {}

    def agents(deps: AgentDeps) -> dict[str, ProcedureAgent[Any]]:
        deps.judgments.llm = InjectingLLM(mode)  # type: ignore[misc]
        return real_agents(deps)

    @asynccontextmanager
    async def storage(tmp_path: Path, **overrides: Any) -> AsyncIterator[RealRun]:
        financial = FakeFinancialStore(replies(), dict(NUMBAT_RECORDS), trial_balance=NUMBAT_TRIAL_BALANCE)
        # 独立复核 (A7, GEN-09) 的复核模型也换成同样的假模型: 留痕同样记假模型名与 token
        runner, store = make_runner(financial_store=financial, agents=agents, review_llm=InjectingLLM(mode))
        engagement_store = runner.deps.engagement_store
        run = RealRun(Started(runner, store, engagement_store), runner.deps, ENGAGEMENT.id, ENGAGEMENT.owner_id)  # type: ignore[arg-type]
        observed["run"] = run
        try:
            yield run
        finally:
            await runner.shutdown()

    monkeypatch.setattr(live, "live_env_file", lambda: Path("/nonexistent/probe.env"))
    monkeypatch.setattr(live, "Settings", lambda **_: SimpleNamespace(LLM_MODEL="probe-model", LLM_API_KEY="probe"))
    monkeypatch.setattr(live, "real_storage", storage)
    if mode == "cutoff_unattributed_bare":
        monkeypatch.setattr(live, "CUTOFF_REASON", "跨期")  # 审计师认定错报时理由不写事实
    return observed


@pytest.mark.parametrize(
    "mode", ["fixtures", "name_case", "run_then_subject", "overstatement_wording", "cutoff_unattributed"]
)
async def test_acceptance_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str) -> None:
    """fixture 原样通过; 公司名只差大小写也通过 (R59-05); 同一程序先整次失败, 再单对象失败, 重跑后成功,
    验收照常通过, 统计打印不因对象为空而崩 (R59-01); 高估换成名词说法也通过 (R59-08); 截止性判断失败后审计师对
    例外认定错报, 报告引人工判断, 照样签字归档 (R59-06)."""
    offline(monkeypatch, mode)
    await live.test_live_llm_full_flow(tmp_path)


@pytest.mark.parametrize(
    ("mode", "problem"),
    [
        ("wrong_after_failed", ("DOC-04", "CON-003")),  # R59-03: 早先的失败不豁免重跑后的错误结果
        ("report_without_facts", ("RPT-03", "r1")),  # R59-04: 报告缺错报事实
        ("report_wrong_clause", ("RPT-03", "r2")),  # 第 2 稿引错条款
        ("understatement_wording", ("RPT-03", "r1")),  # R59-08: 写成低估
        ("cutoff_wrong_document", ("REV-02", "SI-2026-0612")),  # R59-07: 合同证据换成 CON-002 的文件
        ("cutoff_wrong_page", ("REV-02", "SI-2026-0612")),  # R59-07: 合同证据页码不对
        ("cutoff_unattributed_bare", ("RPT-03", "r1")),  # R59-09: 认定理由不带事实, 报告写不出签收日与条款
    ],
)
async def test_acceptance_rejects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str, problem: tuple[str, str]
) -> None:
    """模型结果与第 7 节不一致且不是人给的结论: 验收 2 不通过, 不一致处含注入的那一项."""
    observed = offline(monkeypatch, mode)
    with pytest.raises(AssertionError):
        await live.test_live_llm_full_flow(tmp_path)
    assert problem in await live.mismatches(observed["run"])


async def test_acceptance_arbitrates_dispute(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """独立复核 (A7) 判 BA-01 与原判断不一致: 建争议任务, 审计师仲裁维持原结论 (原结论同第 7 节), 照样签字归档."""
    observed = offline(monkeypatch, "critic_disagrees")
    await live.test_live_llm_full_flow(tmp_path)
    run = observed["run"]
    tasks = await run.deps.output_store.latest(run.eid, ReviewTask)
    disputes = [(t.target_id, t.status) for t in tasks if t.kind is TaskKind.dispute]
    assert disputes == [("J-CSH-02-1-BA-01", TaskStatus.decided)]


def test_report_clause_check_follows_extracted_numbering() -> None:
    """条款编号随 DOC-04 本次抽取的页内顺序而定 (第 4 次真实运行中付款条款排在前, 控制权转移为 CON-001-T2):
    引 T2 在 T2 是控制权转移条款时正确, 在 T2 是付款条款时判为引错."""
    basis = (
        "Basis for Qualified Opinion\n\nRevenue is overstated by $142,000. Under clause CON-001-T2, control passed "
        "when the customer signed the delivery note on 3 July 2026."
    )
    report = AuditReportVersion(
        id="REPORT-2",
        revision=2,
        opinion_type=OpinionType.qualified,
        sections={ReportSection.opinion: "Qualified Opinion", ReportSection.basis_for_opinion: basis},
        judgment_refs=[JudgmentRef(id="J-REV-02-1-SI-2026-0612", version=2)],
    )
    assert live.report_problems(report, "CON-001-T2", {"CON-001-T1", "CON-001-T3"}) == []
    assert live.report_problems(report, "CON-001-T1", {"CON-001-T2", "CON-001-T3"}) == [
        "未引控制权转移条款",
        "引错条款 CON-001-T2",
    ]
