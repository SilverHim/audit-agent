"""演示走通的驱动, 汇总与检查 (需求 R311; 需求 R353 从 tests/test_demo_walkthrough_live 移出): 经 HTTP 的真实模型
变体 (tests/test_demo_walkthrough_live) 与浏览器走查 (tests/e2e/test_browser_walk) 共用.

- 演示决定表 (decision_table) 与一条待决任务的决定 (review_body, dispute_fields): 按决定表期望的结论, 不看模型的
  建议动作; failed 与 unattributed 任务 (判断失败, 后者为带规则例外的对象, 需求 R347) 每个对象至多重跑一次, 计划与
  报告整条失败按修订计 (REVISED_BY); unattributed 重跑后仍失败才按决定表推翻或认定错报. 一轮失败算一组: 对一条
  rerun 时同程序其余待决的可重跑任务随之重跑, 一并计数.
- drive: 经 HTTP 照页面的做法按状态从新建项目走到归档, 进度记在 LiveWalk.
- evaluate: 汇总 (用量, 各判断的建议动作与固定输出的对照, 报告意见, 解析不了的指针, 缺模型结论的对象, 重跑, 项目与
  各执行的语言, 中文执行写出的文字中不是中文的); assert_hard 与 warn_soft 为两级检查.
- 工作语言 (需求 R343): drive 按 language 建项目 (缺省 en). 中文执行 (ProcedureRun.language zh) 写出的判断, 计划叙述与
  报告两段须有中文 (language_problem, 硬性); 疑似整段英文与引号内含中文只告警 (english_run, translated_quote); mock
  下执行都记 en, 这几项为空.
- 用量: FIXED_CALLS (固定输出走一遍) 加 LIVE_MARGIN 为上限 MAX_CALLS.
"""

import json
import re
import warnings
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
import pytest

from backend.contracts.models import (
    AuditPlan,
    AuditReportVersion,
    Confirmation,
    Judgment,
    OpinionType,
    ProcedureRun,
    ReportSection,
    ReportStatus,
    TrailKind,
    WorkingLanguage,
)
from backend.core.config import get_settings
from backend.orchestration.runner import GraphRunner
from backend.services import review_queue
from backend.services.data_service import DataService
from backend.services.judgment_engine import MAX_ATTEMPTS, PointerResolver
from backend.services.procedure_library import ProcedureLibrary
from tests.fakes import evidence_pointers, wait_idle
from tests.fixture_sets import XERO
from tests.tools.live_budget import Budget, summary
from tests.xero_fakes import (
    FIXED_CALLS,
    FIXTURE_READS,
    PLAN_DECISIONS,
    XERO17_ANNOTATION,
    XERO17_BA091_DRAFT,
    XERO17_MATERIALITY,
    xero_journal_reviews,
)
from tests.xero_flow import API, READY_REASON, drain, entered_p2_by

JUDGING = ("REV-03", "REV-02", "CSH-02", "GEN-02")  # 演示配置下出判断的大模型程序, 各对象的固定输出在运行副本里
JUDGED = sum(FIXTURE_READS[pid] for pid in JUDGING)  # 判断对象数 (17)
# 真实模型的用量余量. 重试项按最坏情况算: 四级校验任一级没通过都回传重试 (R34, R348), 每次调用都再重试
# MAX_ATTEMPTS - 1 次. A7 两轮复核从宽, 按全部判断对象计 (演示配置下 L1, L2 跳过 GEN-09, 实际只有
# CSH-02 的 BA-091 一个对象可触发); 多出的部分留给没单列的重跑 (每个对象或修订至多一次, 每次至多 MAX_ATTEMPTS 次)
# 与 A7 自身的重试
LIVE_MARGIN = (MAX_ATTEMPTS - 1) * FIXED_CALLS + 2 * JUDGED
MAX_CALLS = FIXED_CALLS + LIVE_MARGIN  # 用量上限
MAX_STEPS = 200  # 驱动的步数上限 (固定输出走一遍约 40 步)
RERUN_REASON = "The model output did not pass validation; rerun."
CONFIRM_CONCLUSION = "The original conclusion stands: no misstatement is identified for this item."
BA091_REASON = "Accrued interest of AUD 48.53 confirmed by the bank was not recorded."


@dataclass(frozen=True)
class Expected:
    """演示决定表中一个对象期望的结论: 认定错报 (带草稿), 推翻 (给出结论) 或确认."""

    decision: str  # misstatement, override 或 confirm
    reason: str | None = None
    draft: dict[str, Any] | None = None  # 认定错报的草稿 (JSON)
    conclusion: str | None = None  # 推翻或 "两者都不采用" 时的结论


def decision_table(owner: str) -> dict[str, Expected]:
    """演示决定表 (同 R287 的离线走法), 按判断对象: BA-091 认定错报; GEN-02 七笔同 R146 (XJ-499 认定错报, XJ-575 推翻
    为接受, 其余确认). 表外的对象一律确认."""
    table = {"BA-091": Expected("misstatement", BA091_REASON, XERO17_BA091_DRAFT.model_dump(exclude_none=True))}
    for record in xero_journal_reviews(owner):
        voucher = record.task_id.removeprefix("T-J-GEN-02-1-").removesuffix("-v1")
        draft = record.misstatement_draft.model_dump(exclude_none=True) if record.misstatement_draft else None
        table[voucher] = Expected(record.decision.value, record.reason, draft, record.override_conclusion)
    return table


FinPolicy = Callable[[dict[str, Any]], str]  # FIN-01 闸门的材料 (GET /gate 的 material) → 选定的意见类型


def candidate_opinion(material: dict[str, Any]) -> str:
    """按汇总表的候选意见决定 (需求 R311)."""
    return cast(str, material["schedule"]["candidate_opinion"])


@dataclass
class LiveWalk:
    """一次走法的记录, 驱动边走边写 (中途失败时汇总照样能写出已走到的部分)."""

    eid: str | None = None
    owner: str | None = None
    gates: list[str] = field(default_factory=list)  # 依次决定过的闸门
    fin: str | None = None  # FIN-01 选定的意见类型
    candidate: str | None = None  # 汇总表的候选意见
    # (程序, 对象) → 重跑过的次数 (failed 与 unattributed 任务; 下 rerun 时同组随之重跑的也计, 第 1 轮审核 R347-03)
    failures: Counter[tuple[str, str]] = field(default_factory=Counter)
    troubled: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # 任务 ID → (程序, 对象, 类型), 非 normal
    decisions: list[tuple[str, str]] = field(default_factory=list)  # (任务 ID, 决定)


def subject_of(view: dict[str, Any]) -> str:
    """任务所指的对象: 判断的 subject_id; 例外取 detail 的凭证号, 否则首个来源 (如回函应计利息的银行账户);
    failed 任务取任务的 subject_id (整条程序失败为空串)."""
    task, target = view["task"], view["target"]
    if task["kind"] == "failed":
        return cast(str, task.get("subject_id") or "")
    if target.get("subject_id"):
        return cast(str, target["subject_id"])
    detail = target.get("detail") or {}
    return cast(str, detail.get("entry_no") or (target.get("source_ids") or [""])[0])


# 按闸门出修订的程序 → 它的闸门: 计划与报告退回一次就出下一修订 (r1, r2). 整条失败的任务没有对象, 按修订计重跑
# 次数 (修订号为该闸门已决定的次数加一), 不然两个修订各失败一次会被误判为 "重跑后又失败" (需求 R317)
REVISED_BY = {"PLN-15": "PLN-15", "RPT-03": "RPT-01"}


RERUNNABLE = frozenset(kind.value for kind in review_queue.RERUNNABLE)  # 可重跑的任务类型 (failed, unattributed)


def rerun_key(view: dict[str, Any], walk: LiveWalk) -> tuple[str, str]:
    """重跑计数的键 (程序, 对象): 对象见 subject_of; 计划与报告整条失败的按修订 (REVISED_BY)."""
    task = view["task"]
    pid, subject = task["procedure_id"], subject_of(view)
    if task["kind"] == "failed" and not subject and pid in REVISED_BY:
        subject = f"r{walk.gates.count(REVISED_BY[pid]) + 1}"
    return pid, subject


def review_body(
    view: dict[str, Any], table: dict[str, Expected], walk: LiveWalk, pending: Sequence[dict[str, Any]] = ()
) -> dict[str, Any]:
    """对一条待决任务的决定 (请求体), 按演示决定表期望的结论, 不看模型的建议动作 (见模块说明). pending 为当时全部
    待决任务 (复核队列各项): 下 rerun 时同程序同一次执行其余可重跑的任务随之重跑 (一轮失败算一组, 同
    decisions.rerun_group), 一并计数."""
    task = view["task"]
    kind = task["kind"]
    pid, subject = rerun_key(view, walk)
    body: dict[str, Any] = {"task_id": task["id"], "target_version": task["target_version"]}
    if kind != "normal":
        walk.troubled[task["id"]] = (pid, subject, kind)
    expected = table.get(subject, Expected("confirm"))
    # 判断或执行失败先重跑一次 (每个对象至多一次): failed 重跑后又失败即判失败; unattributed (带规则例外的对象, 需求
    # R347) 重跑后仍失败时按下面的决定表处理
    if kind == "failed" or (kind == "unattributed" and not walk.failures[pid, subject]):
        walk.failures[pid, subject] += 1
        if walk.failures[pid, subject] > 1:
            pytest.fail(f"{pid} {subject or '(整条)'} 重跑后又失败: {task['id']}")
        group = [  # 同组随之重跑的 (DecisionSubmit 一并关闭), 也算重跑过一次
            other
            for other in pending
            if other["task"]["procedure_id"] == pid
            and other["task"]["run_id"] == task["run_id"]
            and other["task"]["kind"] in RERUNNABLE
            and other["task"]["id"] != task["id"]
        ]
        for other in group:
            walk.failures[rerun_key(other, walk)] += 1
        return body | {"decision": "rerun", "reason": RERUN_REASON}
    if kind in ("normal", "unattributed"):  # 无解释的例外: 认定错报或推翻, 不能确认
        if expected.decision == "misstatement":
            return body | {"decision": "misstatement", "reason": expected.reason, "misstatement_draft": expected.draft}
        if expected.decision == "override" or kind == "unattributed":
            conclusion = expected.conclusion or CONFIRM_CONCLUSION
            return body | {
                "decision": "override",
                "reason": expected.reason or conclusion,
                "override_conclusion": conclusion,
            }
        return body | {"decision": "confirm"}
    if kind == "dispute":
        return body | dispute_fields(view, expected)
    pytest.fail(f"没有预期的任务类型 {kind}: {task['id']}")


def dispute_fields(view: dict[str, Any], expected: Expected) -> dict[str, Any]:
    """争议任务的仲裁: 原判断 (A6) 与期望一致时维持原判断 (期望错报时须原判断也建议错报, 带草稿; 期望不认定错报时
    原判断不建议错报); 否则 "两者都不采用" (neither), 给出期望的结论, 期望错报时附草稿. 期望为推翻的对象一律 neither."""
    a6 = view["draft_rules"].get("a6_upheld")
    want = expected.decision == "misstatement"
    upheld = {"decision": "dispute_resolution", "dispute_resolution": "a6_upheld"}
    if "a6_upheld" in view["allowed_resolutions"] and expected.decision != "override":
        if want and a6 == "required":
            return upheld | {"reason": expected.reason, "misstatement_draft": expected.draft}
        if not want and a6 != "required":
            return upheld | {"reason": CONFIRM_CONCLUSION}
    conclusion = expected.conclusion or (expected.reason if want else CONFIRM_CONCLUSION)
    fields: dict[str, Any] = {
        "decision": "dispute_resolution",
        "dispute_resolution": "neither",
        "neither_action": "human_conclusion",
        "override_conclusion": conclusion,
        "reason": expected.reason or conclusion,
    }
    return fields | ({"misstatement_draft": expected.draft} if want else {})


async def drive(
    client: httpx.AsyncClient,
    runner: GraphRunner,
    service: DataService,
    walk: LiveWalk,
    *,
    fin: FinPolicy = candidate_opinion,
    budget: Budget | None = None,
    language: WorkingLanguage = "en",
) -> LiveWalk:
    """从新建项目走到归档 (见模块说明), 项目的工作语言为 language, 进度记在 walk. 每步之后等后台作业与图都停下, 后台
    始终没有错误; 每次提交前查预算 (已超限即失败, 不再提交重跑)."""
    walk.owner = (await client.get(f"{API}/users/me")).json()["id"]
    table = decision_table(walk.owner)
    clients = (await client.get(f"{API}/clients")).json()
    [dataset] = [d for d in clients[0]["datasets"] if d["default"]]
    body = {k: dataset[k] for k in ("dataset", "period_start", "period_end")}
    created = await client.post(f"{API}/engagements", json={**body, "working_language": language})
    assert created.status_code == 201, created.text
    eid = walk.eid = created.json()["engagement"]["id"]
    base = f"{API}/engagements/{eid}"
    ready_posted = False

    async def post(path: str, body: dict[str, Any]) -> None:
        if budget is not None:
            budget.check()
        posted = await client.post(f"{base}/{path}", json=body)
        assert posted.status_code in (201, 202), (path, posted.text)

    for _ in range(MAX_STEPS):
        await drain(service)
        await wait_idle(runner, eid)
        overview = (await client.get(base)).json()
        assert overview["run_error"] is None, overview["run_error"]
        if overview["engagement"]["phase"] == "Archived":
            return walk
        pending = (await client.get(f"{base}/review-tasks", params={"status": "pending"})).json()
        if pending:
            body = review_body(pending[0], table, walk, pending)
            walk.decisions.append((body["task_id"], body["decision"]))
            await post("review-records", body)
            continue
        suggested = (await client.get(f"{base}/data/summary")).json()["demo"]["suggested"]
        if suggested is not None:
            await post("data/demo-import", {"stage": suggested})
            continue
        if overview["awaiting_data"]:
            assert not ready_posted, f"确认数据已齐之后仍在等数据: {overview['missing_tables']}"
            ready_posted = True
            await post("data/ready", {"reason": READY_REASON})
            continue
        gate = overview["current_gate"]
        assert gate is not None, f"没有待决任务, 不等数据也不在闸门, 流程停住: {overview['procedures']}"
        got = await client.get(f"{base}/gate")
        assert got.status_code == 200 and got.json() is not None, got.text  # 不在闸门为 200 null (需求 R346)
        view = got.json()
        body = {k: view[k] for k in ("gate", "target_id", "target_version")}
        if gate == "PLN-01/03":
            body |= {"decision": "approve", "materiality": XERO17_MATERIALITY.model_dump()}
        elif gate == "FIN-01":
            walk.candidate = view["material"]["schedule"]["candidate_opinion"]
            walk.fin = fin(view["material"])
            reason = f"Opinion follows the schedule of uncorrected misstatements (candidate {walk.candidate})."
            body |= {"decision": "approve", "disposition": walk.fin, "reason": reason}
        elif gate == "PLN-15":  # 计划先退回一次再批准, 同 R317 的离线走法
            body |= PLAN_DECISIONS[min(walk.gates.count("PLN-15"), 1)]
        elif gate == "RPT-01" and "RPT-01" not in walk.gates:  # 先退回一次, 同 R287
            reason = "The basis for opinion paragraph must name the financial reporting framework."
            body |= {"decision": "reject", "annotations": [XERO17_ANNOTATION.model_dump()], "reason": reason}
        else:
            body |= {"decision": "approve"}
        walk.gates.append(gate)
        await post("gate-decisions", body)
    pytest.fail(f"{MAX_STEPS} 步内未到归档")


def latest_model_judgments(judgments: list[Judgment]) -> dict[tuple[str, str], Judgment]:
    """模型自己给出的判断 (人工判断 -H 不算), 每个 (程序, 对象) 取最后一次执行的."""
    latest: dict[tuple[str, str], Judgment] = {}
    for j in sorted(judgments, key=lambda j: (j.run_id, j.version)):
        if "-H" not in j.id:
            latest[j.procedure_id, j.subject_id] = j
    return latest


def fixture_action(procedure_id: str, subject_id: str) -> str | None:
    """演示运行副本里该对象固定输出的建议动作; 没有固定输出为空."""
    path = XERO.runtime / procedure_id / f"{subject_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))["proposed_action"] if path.is_file() else None


CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")  # 中日韩统一表意文字
QUOTED = re.compile(r"“[^”]*”|\"[^\"]*\"|「[^」]*」|『[^』]*』|‘[^’]*’")  # 引号里的原文
# 引号外 8 个以上相连的英文词 (词间只有空白与常见标点): 疑似整段英文正文, 只告警 (照原文保留的名称与准则原文
# 也可能这样长)
ENGLISH_RUN = re.compile(r"[A-Za-z][A-Za-z'’]*(?:[\s,;:()&/-]+[A-Za-z][A-Za-z'’]*){7,}")


def language_problem(text: str) -> str | None:
    """中文执行写出的一段文字的硬性问题 (需求 R343 验收 2, 审核 R343-01): 没有中日韩字符为 "没有中文", 合格为 None.
    照原文保留的英文名称 (如 APES 110 的全名) 与准则原文不论多长都不算; 整段英文与引文被译另见 english_run,
    translated_quote (只告警)."""
    return None if CJK.search(text) else "没有中文"


def english_run(text: str) -> str | None:
    """引号外疑似整段英文正文的一段 (ENGLISH_RUN, 前 60 字), 没有为 None; 只告警. 已知漏报: 英文正文被数字, 金额或
    句点切成不足 8 个词的几段时不报 (如 "was overstated by $75,000 because ...")."""
    found = ENGLISH_RUN.search(QUOTED.sub("", text))
    return found.group(0)[:60] if found else None


def translated_quote(text: str) -> str | None:
    """引号里含中日韩字符的第一段引文 (前 60 字), 没有为 None; 只告警: Xero 演示的合同与回函原文都是英文, 引号里出现中文
    多半是把原文译掉了 (R340 要求引文照原文)."""
    found = next((q.group(0) for q in QUOTED.finditer(text) if CJK.search(q.group(0))), None)
    return found[:60] if found else None


def chinese_texts(
    judged: dict[tuple[str, str], Judgment],
    plans: Sequence[AuditPlan],
    reports: Sequence[AuditReportVersion],
    runs: dict[str, ProcedureRun],
) -> list[tuple[str, str, str]]:
    """中文执行 (language zh) 写出的各段文字 (来源, 字段, 文字): 模型的判断 (结论, 推理各步, 反方论证), 计划 (叙述各段,
    各程序的性质时间与范围, 质量复核与专家的说明), 报告 (意见段与基础段)."""

    def in_chinese(run_id: str | None) -> bool:
        run = runs.get(run_id or "")
        return run is not None and run.language == "zh"

    texts: list[tuple[str, str, str]] = []
    for j in (j for j in judged.values() if in_chinese(j.run_id)):
        texts += [(j.id, "conclusion", j.conclusion), (j.id, "counter_argument", j.counter_argument)]
        texts += [(j.id, f"reasoning_chain[{n}]", step.text) for n, step in enumerate(j.reasoning_chain)]
    for plan in (p for p in plans if in_chinese(p.run_id)):
        texts += [(plan.id, f"narrative.{s.section}", s.text) for s in plan.narrative]
        texts += [(plan.id, f"procedure_notes.{n.procedure_id}", n.nature_timing_extent) for n in plan.procedure_notes]
        texts += [(plan.id, "eqr_recommendation", plan.eqr_recommendation.basis)]
        texts += [(plan.id, "expert_assessment", plan.expert_assessment)]
    for report in (r for r in reports if in_chinese(r.run_id)):
        texts += [
            (report.id, s.value, report.sections[s]) for s in (ReportSection.opinion, ReportSection.basis_for_opinion)
        ]
    return texts


async def chinese_checks(runner: GraphRunner, eid: str, runs: dict[str, ProcedureRun]) -> dict[str, Any]:
    """中文执行写出的文字的检查 (需求 R343): chinese_checked 为查了多少段 (chinese_texts), not_in_language 为硬性问题
    (language_problem), english_runs 与 translated_quotes 为告警 (english_run, translated_quote); 每条 "来源 字段:
    原因". runs 为执行 ID → 执行记录 (取其 language)."""
    store = runner.deps.output_store
    judged = latest_model_judgments(await store.latest(eid, Judgment))
    signed = [r for r in await store.latest(eid, AuditReportVersion) if r.status is ReportStatus.signed]
    texts = chinese_texts(judged, await store.latest(eid, AuditPlan), signed, runs)

    def listed(check: Callable[[str], str | None]) -> list[str]:
        return [f"{source} {name}: {found}" for source, name, text in texts if (found := check(text))]

    return {
        "chinese_checked": len(texts),
        "not_in_language": listed(language_problem),
        "english_runs": listed(english_run),
        "translated_quotes": listed(translated_quote),
    }


async def evaluate(runner: GraphRunner, walk: LiveWalk, budget: Budget) -> dict[str, Any]:
    """汇总 (需求 R311 验收 4) 与两级检查的材料: 用量 (调用与 token 取预算, 尝试与失败级别取留痕), 各判断的建议动作与
    固定输出的对照, 签字报告的意见类型与语言, 解析不了的证据指针, 缺模型结论的判断对象, 重跑与非 normal 的任务; 项目
    的工作语言, 各执行的语言 (语言 → 次数), 中文执行写出的文字的检查 (chinese_checks, 需求 R343)."""
    deps, eid = runner.deps, walk.eid
    out: dict[str, Any] = {"engagement_id": eid, "gates": walk.gates, "fin_disposition": walk.fin}
    out["candidate_opinion"] = walk.candidate
    out["reruns"] = {f"{pid} {subject}": n for (pid, subject), n in sorted(walk.failures.items())}
    out["troubled_tasks"] = [list(v) for _, v in sorted(walk.troubled.items())]
    out["decisions"] = dict(sorted(Counter(decision for _, decision in walk.decisions).items()))
    entries = await deps.trail.list(eid, TrailKind.llm_call, limit=10_000) if eid else []
    out["usage"] = summary(entries, budget)
    if eid is None:
        return out
    store = deps.output_store
    judged = latest_model_judgments(await store.latest(eid, Judgment))
    rows = []
    for (pid, subject), j in sorted(judged.items()):
        expected = fixture_action(pid, subject)
        rows.append({"procedure_id": pid, "subject_id": subject, "proposed_action": j.proposed_action.value})
        rows[-1] |= {"fixture": expected, "same": expected == j.proposed_action.value}
    out["judgments"] = rows
    out["differences"] = [r for r in rows if r["fixture"] is not None and not r["same"]]
    expected_subjects = {(pid, p.stem) for pid in JUDGING for p in (XERO.runtime / pid).glob("*.json")}
    out["missing_subjects"] = [f"{pid} {subject}" for pid, subject in sorted(expected_subjects - set(judged))]
    runs = {r.id: r for r in await store.latest(eid, ProcedureRun)}
    engagement = await deps.engagement_store.get(eid)
    out["language"] = engagement.working_language if engagement is not None else None
    out["run_languages"] = dict(sorted(Counter(r.language for r in runs.values()).items()))
    signed = [r for r in await store.latest(eid, AuditReportVersion) if r.status is ReportStatus.signed]
    out["report_opinion"] = [r.opinion_type.value for r in signed]
    # 报告的语言为起草它的执行记录的 (需求 R341), 没有 run_id 的旧稿为 en
    out["report_language"] = [runs[r.run_id].language if r.run_id in runs else "en" for r in signed]
    out["report_heading"] = [r.sections["opinion"].strip().splitlines()[:1] for r in signed]
    out["contracts"] = len(await deps.financial_store.contracts(eid))
    out["confirmations"] = len(await store.latest(eid, Confirmation))
    out["pointers"], out["unresolved_pointers"] = await unresolved_pointers(runner, eid)
    plans = await store.latest(eid, AuditPlan)
    out["plans"] = sorted([p.id, p.status.value] for p in plans)
    out |= await chinese_checks(runner, eid, runs)
    out["entered_p2_by"] = entered_p2_by(await deps.trail.list(eid, TrailKind.transition, limit=10_000))
    return out


async def unresolved_pointers(runner: GraphRunner, eid: str) -> tuple[int, list[str]]:
    """项目的全部证据指针 (范围同 R56 的 everything: 判断, 合同, 函证, 例外, 评分卡, 人工结果) 经存储解析, 客户画像
    指针按项目采用的画像解析; 返回指针数与解析不了的 (来源, 定位, 页与原因). R250 的程序级评测也用."""
    deps = runner.deps
    engagement = await deps.engagement_store.get(eid)
    profile = await deps.engagement_store.client_profile(engagement.client_id) if engagement else None
    resolver = PointerResolver(deps.judgments.validator, eid, None, profile)
    unresolved = []
    pointers = await evidence_pointers(deps, eid, everything=True)
    for p in pointers:
        resolved, reason = await resolver.resolve(p)
        if resolved is None:
            unresolved.append(f"{p.source_type} {p.source_id} {p.locator} p{p.page}: {reason}")
    return len(pointers), unresolved


def assert_hard(data: dict[str, Any]) -> None:
    """硬性检查 (需求 R311 验收 2): 走到归档 (调用方已由驱动保证, run_error 始终为空); 每个判断对象有模型结论, 重跑至多
    一次 (驱动保证); 证据指针全部解析通过; 签字报告一份, 意见类型与 FIN-01 决定一致, 意见段首行为该意见在报告语言
    (起草它的执行记录的语言) 下的标题; 合同 3 份与函证 2 户都已抽取; 总体审计计划两个修订都出了 (每个修订一次通过, 或
    failed 后重跑一次), PLAN-1 已取代, PLAN-2 批准后由它推进到执行阶段 (需求 R317); 中文执行写出的文字都是中文
    (需求 R343, mock 下没有中文执行). 软性的差异只告警 (warn_soft)."""
    assert data["missing_subjects"] == [], data["missing_subjects"]
    assert data["unresolved_pointers"] == [], data["unresolved_pointers"]
    assert data["pointers"] > 30
    library = ProcedureLibrary(get_settings().KNOWLEDGE_ROOT)
    opinion = OpinionType(data["fin_disposition"])
    headings = [[library.report_headings(language)[opinion][0]] for language in data["report_language"]]
    assert (data["report_opinion"], data["report_heading"]) == ([data["fin_disposition"]], headings)
    assert data["not_in_language"] == [], data["not_in_language"]
    assert (data["contracts"], data["confirmations"]) == (3, 2)
    assert all(n <= 1 for n in data["reruns"].values()), data["reruns"]
    assert data["plans"] == [["PLAN-1", "superseded"], ["PLAN-2", "approved"]], data["plans"]
    assert data["entered_p2_by"] == ["PLN-15"], data["entered_p2_by"]


def warn_soft(data: dict[str, Any]) -> None:
    """软性检查 (需求 R311 验收 3): 判断的建议动作与固定输出不同的逐条告警 (如 BA-091, XJ-499 是否为 misstatement);
    中文执行写出的文字里疑似整段英文正文与引号内含中文 (疑似把原文译掉) 的段落逐条告警 (需求 R343, 审核 R343-01, 03)."""
    for row in data["differences"]:
        warnings.warn(
            f"{row['procedure_id']} {row['subject_id']}: 建议动作 {row['proposed_action']}, 固定输出 {row['fixture']}",
            stacklevel=1,
        )
    for line in data["english_runs"]:
        warnings.warn(f"疑似整段英文: {line}", stacklevel=1)
    for line in data["translated_quotes"]:
        warnings.warn(f"引号内含中文 (疑似原文被译): {line}", stacklevel=1)
