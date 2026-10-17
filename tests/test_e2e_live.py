"""验收: 真实大模型 (LLM_MODE=live) 与真实存储下跑完同一流程 (R59). 审计师的决定按合成数据集设计第 7 节, 但要应对
真实模型的输出: 失败任务提交人工结果 (允许时) 或重跑, 无判断解释的例外提交错报或推翻, 闸门决定绑定当时材料的版本.

需 TEST_DATABASE_URL (同 R56) 与 .env 中的 LLM_MODEL, LLM_API_KEY (只发合成数据). worktree 里没有 .env 时读主检出的.
"""

import os
import subprocess
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import pytest

from backend.contracts.llm_outputs import ContractExtraction
from backend.contracts.models import (
    AcceptanceScorecard,
    AuditException,
    AuditReportVersion,
    Confirmation,
    Decision,
    Direction,
    DisputeResolution,
    GateDecision,
    GateVerdict,
    Judgment,
    ManualResult,
    MaterialityCandidates,
    Misstatement,
    OpinionType,
    Phase,
    ProposedAction,
    ReportSection,
    ReportStatus,
    ReviewRecord,
    ReviewTask,
    TaskKind,
    TaskStatus,
    TermType,
    TrailEntry,
    TrailKind,
    UncorrectedSchedule,
)
from backend.core.config import Settings
from backend.orchestration.runner import GraphRunner
from tests.fakes import NOW, NUMBAT_CUTOFF_DRAFT, wait_idle
from tests.test_decisions import acc01_decision, pln_decision
from tests.test_e2e_mock import REPO, RealRun, real_storage
from tests.test_flow import FIN_APPROVE, RPT_APPROVE, RPT_REJECT

pytestmark = pytest.mark.slow

CUTOFF = "SI-2026-0612"  # 第 7 节认定错报的发票
RERUN_LIMIT = 2  # 每个程序最多重跑次数, 超过即判测试失败 (防止真实模型反复失败时无限重跑)
MANUAL = {  # 失败任务的人工结论 (第 7 节的预期结论), 按判断对象
    "CON-001": "控制权在客户签收发运单时转移, 按签收时点确认收入",
    "CON-002": "控制权在客户签收发运单时转移, 按签收时点确认收入",
    "CON-003": "控制权在配送中心签收时转移; 退货权与返利按可变对价处理",
    "SI-2026-0618": "发运单签收日在期内, 收入归属本期, 不是跨期",
    "BA-01": "差额 50,000.00 为在途存款 (06-30 入账, 07-02 到账), 属时间性差异, 不认定错报",
}
OVERRIDE = "差额为在途存款形成的时间性差异, 不认定错报"  # 非截止性例外的推翻结论
CUTOFF_REASON = (  # 认定截止性错报的理由, 即审计师的结论: 截止性判断失败而对例外认定时, 报告的事实只能从这里来
    "SI-2026-0612 金额 142,000 于 2026-06-28 入账; 发运单 DN-2026-0612 客户于 2026-07-03 签收; "
    "CON-001 第 5.1 条约定客户签收发运单时所有权与风险转移, 控制权在 2026-07-03 转移, 收入属下一财年, "
    "本年收入高估 142,000"
)


def live_env_file() -> Path | None:
    """真实大模型的配置文件: LLM_ENV_FILE, 仓库根的 .env, 主检出的 .env (worktree 时) 中第一个存在的."""
    candidates = [Path(os.environ["LLM_ENV_FILE"])] if os.getenv("LLM_ENV_FILE") else []
    candidates.append(REPO / ".env")
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        candidates.append(Path(common).parent / ".env")
    except (OSError, subprocess.CalledProcessError):
        pass
    return next((p for p in candidates if p.is_file()), None)


async def gate_decision(run: RealRun, node: str, rejected: bool, n: int) -> GateDecision:
    """停在 node 这道闸门时审计师的决定 (第 7 节), 绑定材料的当前版本. rejected: RPT-01 是否已退回过一次."""
    store, eid = run.deps.output_store, run.eid
    changes = {"id": f"D-LIVE-G{n}", "decided_by": run.owner}
    if node == "gate_acc01":
        card = await store.get(eid, AcceptanceScorecard, "SCORECARD")
        assert card is not None
        return acc01_decision(GateVerdict.approve, target_version=card.version, **changes)
    if node == "gate_p1_risk":
        candidates = await store.get(eid, MaterialityCandidates, "MATERIALITY")
        assert candidates is not None
        return pln_decision(GateVerdict.approve, target_version=candidates.version, **changes)
    if node == "gate_fin":
        schedule = await store.get(eid, UncorrectedSchedule, "SCHEDULE")
        assert schedule is not None
        return FIN_APPROVE.model_copy(update={"target_version": schedule.version, **changes})
    assert node == "gate_rpt01", node
    report = max(await store.latest(eid, AuditReportVersion), key=lambda r: r.revision)
    template = RPT_APPROVE if rejected else RPT_REJECT
    return template.model_copy(update={"target_id": report.id, "target_version": report.version, **changes})


async def review_decision(run: RealRun, task: ReviewTask, reruns: Counter[str], n: int) -> ReviewRecord:
    """审计师对一条待决任务的决定: 截止性发票认定错报, 其余判断确认; 无判断解释的例外按同样口径认定错报或推翻;
    独立复核 (A7) 与原判断不一致的争议任务维持原结论 (同确认, 截止性发票同时认定错报), 原结论照常由 mismatches
    对照第 7 节; 失败任务优先人工结果 (允许时, 截止性发票除外, 它要模型重判才能认定错报), 否则重跑
    (每程序至多 RERUN_LIMIT 次)."""
    store, eid = run.deps.output_store, run.eid
    base = {"id": f"D-LIVE-R{n}", "task_id": task.id, "target_version": task.target_version}
    common = {**base, "reviewed_by": run.owner, "decided_at": NOW}
    if task.kind is TaskKind.normal:
        judgment = await store.get(eid, Judgment, task.target_id)
        assert judgment is not None
        if judgment.subject_id == CUTOFF:
            return ReviewRecord(
                decision=Decision.misstatement, reason=CUTOFF_REASON, misstatement_draft=NUMBAT_CUTOFF_DRAFT, **common
            )
        return ReviewRecord(decision=Decision.confirm, **common)
    if task.kind is TaskKind.dispute:
        judgment = await store.get(eid, Judgment, task.target_id)
        assert judgment is not None
        upheld = {"decision": Decision.dispute_resolution, "dispute_resolution": DisputeResolution.a6_upheld}
        if judgment.subject_id == CUTOFF:
            return ReviewRecord(**upheld, reason=CUTOFF_REASON, misstatement_draft=NUMBAT_CUTOFF_DRAFT, **common)
        return ReviewRecord(**upheld, reason="仲裁: 维持原结论", **common)
    if task.kind is TaskKind.unattributed:
        exception = await store.get(eid, AuditException, task.target_id)
        assert exception is not None
        if CUTOFF in exception.source_ids:
            return ReviewRecord(
                decision=Decision.misstatement, reason=CUTOFF_REASON, misstatement_draft=NUMBAT_CUTOFF_DRAFT, **common
            )
        return ReviewRecord(decision=Decision.override, reason="复核结论", override_conclusion=OVERRIDE, **common)
    allowed = run.deps.queue.allowed(task)
    if Decision.manual_result in allowed and task.subject_id in MANUAL:
        result = ManualResult(conclusion=MANUAL[task.subject_id])
        return ReviewRecord(decision=Decision.manual_result, reason="人工执行", manual_result=result, **common)
    reruns[task.procedure_id] += 1
    assert reruns[task.procedure_id] <= RERUN_LIMIT, f"{task.procedure_id} 重跑超过 {RERUN_LIMIT} 次"
    return ReviewRecord(decision=Decision.rerun, reason="模型输出未通过校验, 重跑", **common)


async def drive(run: RealRun) -> tuple[list[ReviewTask], Counter[str]]:
    """从启动推进到归档: 每次停下先处理待决任务 (一次一条), 没有待决任务时按所在闸门决定. 返回出现过的全部失败与
    无解释任务, 以及各程序的重跑次数. run_error 始终为空."""
    s, eid = run.s, run.eid
    troubled: dict[str, ReviewTask] = {}
    reruns: Counter[str] = Counter()
    rejected = False
    await s.runner.start(eid)
    for n in range(1, 80):
        await wait_idle(s.runner, eid)
        assert s.runner.run_error(eid) is None, s.runner.run_error(eid)
        engagement = await run.deps.engagement_store.get(eid)
        assert engagement is not None
        if engagement.phase is Phase.Archived:
            return list(troubled.values()), reruns
        tasks = await run.deps.output_store.latest(eid, ReviewTask)
        troubled |= {t.id: t for t in tasks if t.kind in (TaskKind.failed, TaskKind.unattributed)}
        pending = sorted((t for t in tasks if t.status is TaskStatus.pending), key=lambda t: t.id)
        if pending:
            await s.submit.submit(eid, await review_decision(run, pending[0], reruns, n))
            continue
        [node] = (await s.runner.app.aget_state(GraphRunner.config(eid))).next
        decision = await gate_decision(run, node, rejected, n)
        rejected = rejected or decision.decision is GateVerdict.reject
        await s.submit.submit(eid, decision)
    pytest.fail("80 步内未到归档")


def name_key(name: str | None) -> str | None:
    """公司名的比较键: 只忽略大小写与多余空白 (模型照抄时的格式差异), 其余逐字比较."""
    return None if name is None else " ".join(name.split()).casefold()


def fixture_header(contract_id: str) -> tuple[object, ...]:
    """DOC-04 fixture (第 6.6 节) 中合同的四个要素, 作为预期值; 公司名取比较键."""
    path = REPO / "tests" / "fixtures" / "llm" / "DOC-04" / f"{contract_id}.json"
    e = ContractExtraction.model_validate_json(path.read_text(encoding="utf-8"))
    return (name_key(e.counterparty), e.counterparty_abn, e.amount_cents, e.sign_date)


def report_problems(report: AuditReportVersion, control: str, others: set[str]) -> list[str]:
    """报告基础段与第 7 节第 16, 17 行的事实不符之处: 错报金额与方向, 发运单签收日, 引用的是截止性判断;
    第 2 稿须回应退回批注, 引控制权转移条款 (CON-001 第 5.1 条, 条款编号为 control), 不引该合同的其他条款
    (others). 条款编号按 DOC-04 本次抽取的页内顺序给出, 真实模型的顺序可能与 fixture 不同, 故由调用方从
    已存合同取. 措辞与格式不限, 只查这些事实是否出现."""
    basis = report.sections[ReportSection.basis_for_opinion]
    problems = [f"缺 {fact}" for fact in ("142,000", "3 July 2026") if fact not in basis]
    if "overstat" not in basis.casefold() or "understat" in basis.casefold():  # 高估的常见措辞都算, 低估不算
        problems.append("未写明高估")
    if not any(CUTOFF in ref.id for ref in report.judgment_refs):
        problems.append("未引用截止性判断")
    if report.revision >= 2:
        if "5.1" not in basis and control not in basis:
            problems.append("未引控制权转移条款")
        problems += [f"引错条款 {term}" for term in sorted(others) if term in basis]
    return problems


async def mismatches(run: RealRun) -> dict[tuple[str, str], str]:
    """各大模型程序的结果中与第 7 节不一致的, (程序, 对象) -> 实际结果. 只看模型自己的输出 (人工判断 -H 不算),
    同一对象取最后一次执行; 报告按修订号 (r1, r2) 计."""
    store, eid = run.deps.output_store, run.eid
    found: dict[tuple[str, str], str] = {}
    contracts = await run.deps.financial_store.contracts(eid)
    for contract in contracts:
        actual = (contract.counterparty, contract.counterparty_abn, contract.amount_cents, contract.sign_date)
        if (name_key(contract.counterparty), *actual[1:]) != fixture_header(contract.id):
            found["DOC-04", contract.id] = repr(actual)
    con001 = next((c for c in contracts if c.id == "CON-001"), None)
    terms = con001.terms if con001 is not None else []
    term = next((t for t in terms if t.term_type is TermType.control_transfer), None)
    control = term.id if term is not None else "CON-001-?"
    others = {t.id for t in terms} - {control}
    clause = (con001.document_id, control, term.page) if con001 is not None and term is not None else None
    for c in await store.latest(eid, Confirmation):
        expected = {"BA-01": 120840000, "BA-02": 40000000}[c.bank_account_id]
        if (c.confirmed_balance_cents, c.balance_date) != (expected, date(2026, 6, 30)):
            found["DOC-06", c.bank_account_id] = repr((c.confirmed_balance_cents, c.balance_date))
    latest: dict[tuple[str, str], Judgment] = {}
    for j in sorted(await store.latest(eid, Judgment), key=lambda j: j.run_id):
        if "-H" not in j.id and j.subject_id is not None:
            latest[j.procedure_id, j.subject_id] = j
    for (pid, subject), j in latest.items():
        if pid == "REV-02" and subject == CUTOFF:
            draft = j.misstatement_draft
            ok = j.proposed_action is ProposedAction.misstatement and draft is not None
            ok = ok and (draft.type, draft.assertion, draft.amount_cents, draft.direction) == (
                NUMBAT_CUTOFF_DRAFT.type,
                NUMBAT_CUTOFF_DRAFT.assertion,
                14200000,
                Direction.overstated,
            )
            # 合同证据须是 CON-001 本身的文件, 控制权转移条款的编号与页码 (不只比 locator 文本)
            ok = ok and any(
                p.source_type == "document" and (p.source_id, p.locator, p.page) == clause for p in j.evidence
            )
        else:
            expected = ProposedAction.close if pid == "CSH-02" else ProposedAction.accept
            ok = j.proposed_action is expected and j.misstatement_draft is None
        if not ok:
            found[pid, subject] = f"{j.proposed_action} {j.misstatement_draft}"
    for report in await store.latest(eid, AuditReportVersion):
        if problems := report_problems(report, control, others):
            found["RPT-03", f"r{report.revision}"] = "; ".join(problems)
    return found


async def human_outcomes(run: RealRun) -> set[tuple[str, str]]:
    """最终结论由人给出的 (程序, 对象): 存在人工判断 -H (失败任务的人工结果, 或推翻). 这些对象的模型输出
    不再要求与第 7 节一致; 其余对象 (含重跑后又由模型给出的结果) 都要一致."""
    judgments = await run.deps.output_store.latest(run.eid, Judgment)
    return {(j.procedure_id, j.subject_id) for j in judgments if "-H" in j.id and j.subject_id is not None}


def llm_stats(entries: list[TrailEntry]) -> dict[str, dict[str, object]]:
    """各程序的大模型调用统计: 调用次数, 各尝试序号的次数, 各校验失败级别的次数, token 合计."""
    stats: dict[str, dict[str, object]] = defaultdict(dict)
    for pid in sorted({e.procedure_id or "" for e in entries}):
        rows = [e for e in entries if e.procedure_id == pid]
        stats[pid] = {
            "calls": len(rows),
            "attempts": dict(Counter(e.payload["attempt"] for e in rows)),
            "level_failed": dict(Counter(e.payload["level_failed"] or "passed" for e in rows)),
            "tokens": sum((e.input_tokens or 0) + (e.output_tokens or 0) for e in rows),
        }
    return dict(stats)


async def test_live_llm_full_flow(tmp_path: Path) -> None:
    """验收 1-4: 真实大模型跑到归档, run_error 始终为空; 每个大模型程序的结果同第 7 节, 或建了 failed / unattributed
    任务并经人工结果, 重跑, 错报或推翻后继续; 最终错报 14200000 高估, 意见保留, 报告签字; llm_call 留痕的模型为
    配置的真实模型, token 大于 0; 打印各程序尝试次数与校验失败级别."""
    env_file = live_env_file()
    configured = Settings(_env_file=env_file) if env_file is not None else None  # type: ignore[call-arg]
    if configured is None or not (configured.LLM_MODEL and configured.LLM_API_KEY):
        pytest.skip(".env 中没有 LLM_MODEL 或 LLM_API_KEY")
    model = configured.LLM_MODEL
    async with real_storage(tmp_path, LLM_MODE="live", _env_file=env_file) as run:
        troubled, reruns = await drive(run)

        store, eid = run.deps.output_store, run.eid
        entries = await run.deps.trail.list(eid, TrailKind.llm_call, limit=10_000)
        stats = llm_stats(entries)
        found = await mismatches(run)
        handled = await human_outcomes(run)
        print("\n大模型调用统计:", stats)
        print("失败或无解释任务:", sorted((t.procedure_id, t.subject_id or "", t.kind.value) for t in troubled))
        print("重跑次数:", dict(reruns), "与第 7 节不一致:", found)

        engagement = await run.deps.engagement_store.get(eid)
        assert engagement is not None and (engagement.phase, engagement.locked) == (Phase.Archived, True)
        assert {k: v for k, v in found.items() if k not in handled} == {}  # 验收 2: 不一致的只能是人给了结论的
        [misstatement] = await store.latest(eid, Misstatement)  # 验收 3
        assert (misstatement.amount_cents, misstatement.direction) == (14200000, Direction.overstated)
        schedule = await store.get(eid, UncorrectedSchedule, "SCHEDULE")
        assert schedule is not None and schedule.candidate_opinion is OpinionType.qualified
        signed = [r for r in await store.latest(eid, AuditReportVersion) if r.status is ReportStatus.signed]
        assert [(r.opinion_type, r.approved_by) for r in signed] == [(OpinionType.qualified, run.owner)]
        assert entries and all(e.model and e.model.startswith(model) for e in entries)  # 验收 4
        assert all((e.input_tokens or 0) > 0 and (e.output_tokens or 0) > 0 for e in entries)
        assert set(stats) - {"GEN-09"} == {"DOC-04", "DOC-06", "REV-03", "REV-02", "CSH-02", "RPT-03"}
        assert all(e.actor == "A7" for e in entries if e.procedure_id == "GEN-09")  # 独立复核按触发规则, 次数随模型输出
