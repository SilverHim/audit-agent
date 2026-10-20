"""A8 WorkpaperAgent: 错报的分类 (FIN-03, 需求 R158), 沟通 (FIN-08, 人工程序, 需求 R161) 与更正验证 (FIN-09, 需求
R165).

返回字段: judgments, review_tasks, exceptions, misstatements, procedure_runs (merge.AGENT_FIELDS). FIN-03, FIN-09 不直接
写错报: 分类与验证结果记在判断的 misstatement_update 上, 判断确认后才写出错报新版本 (需求 R159); FIN-09 对不上时另出
V02 例外. FIN-08 由 A10 派发, A8 为它实现 ManualHandler: 材料 (prepare), 登记的核对 (check), 按管理层的回复写出错报
新版本 (collect).
"""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast, get_args

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext, run_rules
from backend.agents.fieldwork import JSON_ROWS, journal_rows
from backend.agents.judging import (
    JudgedRun,
    Subject,
    Subjects,
    earlier_findings,
    in_scope,
    judge_all,
    judging_steps,
    judgments_patch,
)
from backend.agents.review import ReviewAgent
from backend.contracts.errors import VersionConflict
from backend.contracts.llm_outputs import MisstatementClassificationDraft
from backend.contracts.manual_results import ManagementResponses
from backend.contracts.models import (
    AgentId,
    AuditException,
    AuditObjective,
    Direction,
    EvidencePointer,
    EvidenceType,
    Judgment,
    ManagementResponse,
    Misstatement,
    MisstatementOrigin,
    MisstatementStatus,
    MisstatementUpdate,
    Reliability,
    ReviewRecord,
    ReviewTask,
    SourceType,
)
from backend.contracts.services import JudgmentOutcome
from backend.contracts.state import StatePatch
from backend.services.staleness import current_results

FIN03 = "FIN-03"
FIN08 = "FIN-08"
FIN09 = "FIN-09"
V01, V02 = "V01", "V02"  # FIN-09 的规则: 审计更正分录的全部行 (规则 SQL), 分录与错报对不上 (在代码中生成)
# 借方余额的科目类别: 高估以贷方为更正, 低估以借方为更正; 其余类别 (负债, 权益, 收入) 相反 (需求 R165)
DEBIT_NORMAL = frozenset({"asset", "expense"})
# FIN-08 材料逐条列的错报字段 (需求 R161 验收 2), 另加来源判断的结论 source_conclusion
LISTED_FIELDS = {
    "id",
    "version",
    "account",
    "account_code",
    "assertion",
    "amount_cents",
    "direction",
    "type",
    "fraud_indicative",
}
# 随材料给审计师的更正请求 (ASA 450 第 8, 9 段), 照此与管理层沟通
CORRECTION_REQUEST = "请管理层更正下列错报; 不更正的, 请说明理由 (ASA 450 第 8, 9 段)."
# 期间来源 (需求 R158 验收 2): 一律本期. 首次审计没有上期未更正错报 (ASA 450 第 13 段的结转口径之后另加)
ORIGIN: MisstatementOrigin = "current_period"
ROW_SOURCES = frozenset(get_args(SourceType))  # 经 FinancialDataStore.record 解析的行类型
# 提示词各项取的字段 (FIN-03.md.j2 头注释): 错报, 来源判断, 分录行, 文件登记, 舞弊风险
MISSTATEMENT_FIELDS = {
    "id",
    "version",
    "judgment_id",
    "type",
    "account",
    "account_code",
    "assertion",
    "amount_cents",
    "direction",
    "status",
}
SOURCE_FIELDS = {"id", "procedure_id", "subject_id", "conclusion", "evidence"}
VERIFY_SOURCE_FIELDS = SOURCE_FIELDS - {"evidence"}  # FIN-09.md.j2 的来源判断不带证据
LINE_FIELDS = {
    "source_type",
    "id",
    "entry_no",
    "posting_date",
    "created_by",
    "approved_by",
    "account_code",
    "debit_cents",
    "credit_cents",
    "description",
    "source_doc",
}
DOCUMENT_FIELDS = {"id", "file_name", "type", "page_count", "reliability"}
RISK_FIELDS = {"id", "assertion", "level", "description"}


def unclassified(state: Any) -> list[Misstatement]:
    """FIN-03 的判断对象: 当前结果中 (过期重跑取代的执行引出的不算, 同 FIN-04) status 为 identified 且还没有分类判断
    (classification_judgment_id 为空) 的错报, 按状态中的顺序 (即认定的先后)."""
    return [
        m
        for m in current_results(state).misstatements
        if m.status is MisstatementStatus.identified and m.classification_judgment_id is None
    ]


def to_communicate(state: Any) -> list[Misstatement]:
    """FIN-08 的沟通对象 (需求 R161): 当前结果中已分类的 identified 错报, 与须重新沟通 (分录对不上) 的 communicated
    错报, 按状态中的顺序. 纯函数."""
    return [
        m
        for m in current_results(state).misstatements
        if (m.status is MisstatementStatus.identified and m.classification_judgment_id is not None)
        or (m.status is MisstatementStatus.communicated and m.needs_recommunication)
    ]


def after_response(misstatement: Misstatement, response: ManagementResponse) -> Misstatement:
    """按管理层的回复写出的错报新版本 (接口文档第 2.5 节迁移表): 同意更正为 communicated, 拒绝为 uncorrected
    (refused); 都记沟通与回复的四项, 清掉重新沟通的标记. 经 model_validate 重建. 回复依据的不是该错报的当前版本抛
    VersionConflict. 纯函数."""
    if response.base_version != misstatement.version:
        raise VersionConflict(
            f"{misstatement.id} 的登记依据第 {response.base_version} 版, 当前为第 {misstatement.version} 版"
        )
    agrees = response.agrees_to_correct
    changes = {
        "status": MisstatementStatus.communicated if agrees else MisstatementStatus.uncorrected,
        "uncorrected_basis": None if agrees else "refused",
        "communicated_at": response.communicated_at,
        "communicated_to": response.communicated_to,
        "responded_at": response.responded_at,
        "management_response": response.response,
        "needs_recommunication": False,
        "version": misstatement.version + 1,
    }
    return Misstatement.model_validate({**misstatement.model_dump(), **changes})


def responses_of(record: ReviewRecord) -> list[ManagementResponse]:
    """FIN-08 决定中登记的回复 (通用核对已按结果模型 ManagementResponses 校验 details)."""
    assert record.manual_result is not None  # manual 任务的决定都带结果
    return ManagementResponses.model_validate(record.manual_result.details).responses


def misstatement_objective(misstatement: Misstatement, objectives: Sequence[AuditObjective]) -> str | None:
    """错报所属的审计目标 (需求 R158 验收 2): 审计目标的科目名作整词出现在错报科目里 (不分大小写, 前后不是字母, 数字或
    下划线; 科目名首尾是标点时也成立) 且认定相同的第一条, 按状态中审计目标的顺序; 没有为空. 如 {Revenue, Cutoff} 的
    错报配 OBJ-REV-CUT, "Interest income (270)" 不配任何目标. 纯函数."""
    for objective in objectives:
        word = rf"(?<!\w){re.escape(objective.account)}(?!\w)"
        if objective.assertion is misstatement.assertion and re.search(word, misstatement.account, re.IGNORECASE):
            return objective.id
    return None


def rule_fields(misstatement: Misstatement, objectives: Sequence[AuditObjective]) -> dict[str, Any]:
    """分类载荷中按规则定的两项 (不取大模型, 也不靠人填): 期间来源 (ORIGIN) 与所属审计目标 (misstatement_objective).
    AI 的载荷与人给的载荷 (DecisionSubmit 写入前补上, 需求 R159 第 1 轮审核) 同用. 纯函数."""
    return {"origin": ORIGIN, "objective_id": misstatement_objective(misstatement, objectives)}


def classification_update(
    misstatement: Misstatement, draft: MisstatementClassificationDraft, objectives: Sequence[AuditObjective]
) -> MisstatementUpdate:
    """一条错报的分类载荷: 类型与舞弊迹象取大模型给的性质, 审计目标与期间来源取规则 (rule_fields), 依据的版本为判断时
    的错报版本. 纯函数."""
    nature = draft.nature
    return MisstatementUpdate(
        misstatement_id=misstatement.id,
        base_version=misstatement.version,
        kind="classification",
        type=nature.type,
        fraud_indicative=nature.fraud_indicative,
        fraud_reason=nature.fraud_reason,
        **rule_fields(misstatement, objectives),
    )


def with_update(
    outcome: JudgmentOutcome, misstatements: dict[str, Misstatement], objectives: Sequence[AuditObjective]
) -> Judgment:
    """成功的判断写上分类载荷 (同一版本, 只加 misstatement_update). 判断的草稿 (JudgmentOutcome.draft) 为分类草稿,
    判断对象为 misstatements 中的错报. 纯函数; 作 judge_all 的 annotate, 只对成功的判断调用."""
    judgment = outcome.judgment
    draft = outcome.draft
    assert judgment is not None and isinstance(draft, MisstatementClassificationDraft)  # FIN-03 的输出模型
    update = classification_update(misstatements[judgment.subject_id], draft, objectives)
    return judgment.model_copy(update={"misstatement_update": update})


def source_judgment(misstatement: Misstatement, judgments: dict[str, Judgment]) -> Judgment:
    """错报的来源判断 (judgments 为状态中各判断的当前版本, 按 ID); 不在其中 (如来源是例外) 抛 LookupError."""
    source = judgments.get(misstatement.judgment_id or "")
    if source is None:
        raise LookupError(f"错报 {misstatement.id} 的来源判断 {misstatement.judgment_id} 不在状态中")
    return source


# ── FIN-09 更正验证 (需求 R165): 规则部分在代码中做, 口径见接口文档第 6 节 FIN-09 段 ──


def to_verify(state: Any) -> list[Misstatement]:
    """FIN-09 的判断对象: 当前结果中 (同 FIN-04) verifying 的错报, 按状态中的顺序. 纯函数."""
    return [m for m in current_results(state).misstatements if m.status is MisstatementStatus.verifying]


def matched_lines(
    misstatement: Misstatement, source_object: str, rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """规则部分匹配的更正分录行: 候选为 source_doc 等于来源对象 (来源判断的对象) 的 V01 行; 错报有 account_code 时取
    候选中科目相同的各行 (V01 的顺序), 没有时取借方或贷方等于错报金额的第一行 (按行 ID). 纯函数."""
    candidates = [r for r in rows if r["source_doc"] == source_object]
    if misstatement.account_code is not None:
        return [r for r in candidates if r["account_code"] == misstatement.account_code]
    amount = misstatement.amount_cents
    return sorted((r for r in candidates if amount in (r["debit_cents"], r["credit_cents"])), key=lambda r: r["id"])[:1]


def corrected_cents(misstatement: Misstatement, lines: Sequence[dict[str, Any]]) -> int:
    """已更正额: 错报有 account_code 时为匹配行更正方向的金额合计减反方向的合计 (DEBIT_NORMAL 的科目高估以贷方为
    更正, 低估以借方; 其余类别相反); 没有时匹配到即为错报金额, 否则为 0. 匹配行的科目不在科目表 (account_type 为空)
    抛 LookupError. 纯函数."""
    if misstatement.account_code is None:
        return misstatement.amount_cents if lines else 0
    total = 0
    for row in lines:
        if row["account_type"] is None:
            raise LookupError(f"更正分录行 {row['id']} 的科目 {row['account_code']} 不在科目表中")
        by_credit = (row["account_type"] in DEBIT_NORMAL) == (misstatement.direction is Direction.overstated)
        debit, credit = row["debit_cents"], row["credit_cents"]
        total += credit - debit if by_credit else debit - credit
    return total


def verification_update(
    misstatement: Misstatement, lines: Sequence[dict[str, Any]], corrected: int
) -> MisstatementUpdate:
    """验证载荷 (kind verification, 依据判断时的错报版本): 已更正额在 (0, 金额] 内时为 corrected (等于金额) 或
    partially_corrected, 带匹配行所在的凭证号 (去重排序), 已更正额与剩余额; 否则对不上 (没有匹配行时已更正额为 0, 或
    方向相反, 超额): communicated 且须重新沟通. 纯函数."""
    amount = misstatement.amount_cents
    base: dict[str, Any] = {"misstatement_id": misstatement.id, "base_version": misstatement.version}
    if not 0 < corrected <= amount:
        return MisstatementUpdate(
            **base, kind="verification", status=MisstatementStatus.communicated, needs_recommunication=True
        )
    return MisstatementUpdate(
        **base,
        kind="verification",
        status=MisstatementStatus.corrected if corrected == amount else MisstatementStatus.partially_corrected,
        adjustment_entry_nos=sorted({r["entry_no"] for r in lines}),
        corrected_cents=corrected,
        residual_cents=amount - corrected,
    )


def mismatch_exception(
    ctx: RunContext,
    misstatement: Misstatement,
    source_object: str,
    lines: Sequence[dict[str, Any]],
    corrected: int,
    n: int,
) -> AuditException:
    """分录与错报对不上的 V02 例外 X-FIN-09-{run_no}-V02-{n} (n 在本次新建 V02 的错报中按判断对象的顺序从 1 编号,
    沿用的不计): severity medium, 涉及匹配行 (没有时为来源对象), 金额为错报金额, 证据为匹配行的指针."""
    line_ids = [r["id"] for r in lines]
    return AuditException(
        id=f"X-{FIN09}-{ctx.run.run_no}-{V02}-{n}",
        procedure_id=FIN09,
        run_id=ctx.run.id,
        rule_id=V02,
        severity="medium",
        source_ids=line_ids or [source_object],
        amount_cents=misstatement.amount_cents,
        detail={
            "misstatement_id": misstatement.id,
            "expected_cents": misstatement.amount_cents,
            "corrected_cents": corrected,
            "matched_line_ids": line_ids,
        },
        evidence=[
            EvidencePointer(
                type=EvidenceType.voucher,
                source_type="journal_entry",
                source_id=line_id,
                locator="debit_cents, credit_cents",
                reliability=Reliability.internal_original,
            )
            for line_id in line_ids
        ],
    )


def verification_subject(
    misstatement: Misstatement,
    source: Judgment,
    lines: Sequence[dict[str, Any]],
    rows: Sequence[dict[str, Any]],
    corrected: int,
    findings: Sequence[AuditException],
) -> Subject:
    """一条 verifying 错报的判断对象: data 按 FIN-09.md.j2 头注释 (均为 JSON 形式): misstatement, source_judgment
    (结论, 不带证据), matched_line_ids, entry_lines (匹配行所在凭证的全部 V01 行, 按行 ID), corrected_cents,
    residual_cents; 规则例外为 findings (对不上时的 V02); 校验上下文给剩余额与 entry_lines 的科目中错报科目以外的
    (去重排序; 错报没有 account_code 时为全部). fixture_key 为来源对象."""
    entries = {r["entry_no"] for r in lines}
    entry_lines = sorted((r for r in rows if r["entry_no"] in entries), key=lambda r: r["id"])
    residual = misstatement.amount_cents - corrected
    data = {
        "misstatement": misstatement.model_dump(mode="json", include=MISSTATEMENT_FIELDS),
        "source_judgment": source.model_dump(mode="json", include=VERIFY_SOURCE_FIELDS),
        "matched_line_ids": [r["id"] for r in lines],
        "entry_lines": JSON_ROWS.dump_python(entry_lines, mode="json"),
        "corrected_cents": corrected,
        "residual_cents": residual,
    }
    return Subject(
        misstatement.id,
        data,
        list(findings),
        fixture_key=source.subject_id,
        expected_residual_cents=residual,
        entry_account_codes=sorted({r["account_code"] for r in entry_lines} - {misstatement.account_code}),
    )


def with_verification(outcome: JudgmentOutcome, updates: dict[str, MisstatementUpdate]) -> Judgment:
    """成功的判断写上验证载荷 (同一版本, 只加 misstatement_update; 载荷由规则部分定, 按判断对象取 updates). 纯函数;
    作 judge_all 的 annotate, 只对成功的判断调用."""
    judgment = outcome.judgment
    assert judgment is not None
    return judgment.model_copy(update={"misstatement_update": updates[judgment.subject_id]})


@dataclass(frozen=True)
class VerifiedRun(JudgedRun):
    """FIN-09 一次执行的原始发现: 判断结果与规则引擎没有执行的规则 (同 JudgedRun), 加本次刚建的 V02 例外 (按编号)."""

    exceptions: list[AuditException] = field(default_factory=list)


class WorkpaperAgent(ProcedureAgent[JudgedRun]):
    """A8: 错报分类 (FIN-03, 混合程序). 规则部分定审计目标与期间来源, 大模型判类型与舞弊迹象, 走判断公共流程
    (agents/judging.py: judge, 写上分类载荷 misstatement_update, A7 复核, 降级, classify, 建任务; 需求 R158, R170).
    更正验证 (FIN-09, 混合程序, 需求 R165): 规则部分匹配更正分录, 定验证载荷, 对不上时出 V02 例外; 大模型判是否完整
    更正与影响的其他科目, 同走判断公共流程.
    错报沟通 (FIN-08, 人工程序, 需求 R161): 由 A10 派发, 本类实现 ManualHandler 的 prepare, check 与 collect.

    舞弊迹象只记进载荷, 由审计师逐条复核时决定, 确认后带在错报上呈报到 FIN-01 闸门; 规格书 12.2 "为真则进环 3 阻断"
    与 ASA 240 第 37 段的重评舞弊风险本版不做.
    """

    agent_id: ClassVar[AgentId] = AgentId.A8  # 智能体编号
    # 负责的程序 (功能 misstatement, 未启用时注册表不映射)
    procedures: ClassVar[frozenset[str]] = frozenset({FIN03, FIN09})
    manual_procedures: ClassVar[frozenset[str]] = frozenset({FIN08})  # 作为模板 owner 的人工程序 (需求 R161)

    def __init__(self, deps: AgentDeps) -> None:
        super().__init__(deps)
        # A7: 判断公共流程中的独立复核 (FIN-03, FIN-09 为 L2, 按跳过规则不复核)
        self.reviewer = ReviewAgent(deps)

    async def execute(self, ctx: RunContext) -> JudgedRun:
        """按程序编号分派: FIN-03 为 classify, FIN-09 为 verify."""
        if ctx.procedure.id == FIN09:
            return await self.verify(ctx)
        if ctx.procedure.id != FIN03:
            raise NotImplementedError(ctx.procedure.id)
        return await self.classify(ctx)

    async def classify(self, ctx: RunContext) -> JudgedRun:
        """FIN-03 的执行: 未分类的错报 (unclassified) 先按 in_scope 筛选 (rerun_subject_ids 与已交人处理的对象, 同 A6
        各程序), 再取数 (fin03_subjects) 交判断公共流程 (步骤按功能, judging_steps). 成功的判断在判断引擎之后即写上
        分类载荷 (with_update, 经 judge_all 的 annotate), 功能 autonomy 打开时降级按所指错报的金额与舞弊迹象判. 没有
        判断对象时不调大模型."""
        targets = {m.id: m for m in unclassified(ctx.state) if in_scope(ctx, m.id)}
        subjects = await self.fin03_subjects(ctx, list(targets.values()))
        objectives = ctx.state["audit_objectives"]
        judged = await judge_all(
            self.deps,
            ctx,
            subjects,
            self.reviewer,
            judging_steps(self.deps),
            annotate=lambda outcome: with_update(outcome, targets, objectives),
        )
        return JudgedRun(judged, subjects.outcomes)

    async def verify(self, ctx: RunContext) -> VerifiedRun:
        """FIN-09 的执行: verifying 的错报 (to_verify) 先按 in_scope 筛选, 没有时不执行规则, 不调大模型. 规则 V01 取审计
        更正分录的全部行; 逐条错报在代码中匹配 (matched_lines), 算已更正额 (corrected_cents) 与验证载荷
        (verification_update), 对不上时建 V02 例外 (mismatch_exception) 作该对象的规则例外; 当前结果里该错报已有本程序
        尚未归因的 V02 (judging.earlier_findings, 同 CSH-02 的 D01, D04, 需求 R347) 时沿用, 不看本次比对, 不再新建 (对
        unattributed 任务 rerun 后旧 V02 才有任务指向; 本次对得上也沿用, 同 CSH-02 数据变了不另处理). 再交判断公共流程,
        成功的判断经 annotate 写上验证载荷. 来源判断不在状态中抛 LookupError."""
        targets = [m for m in to_verify(ctx.state) if in_scope(ctx, m.id)]
        if not targets:
            return VerifiedRun([])
        rules = await run_rules(self.deps, ctx)
        rows = rules.rows.get(V01, [])
        judgments = {j.id: j for j in ctx.state["judgments"]}
        subjects: list[Subject] = []
        exceptions: list[AuditException] = []
        updates: dict[str, MisstatementUpdate] = {}
        for m in targets:
            source = source_judgment(m, judgments)
            lines = matched_lines(m, source.subject_id, rows)
            corrected = corrected_cents(m, lines)
            updates[m.id] = verification_update(m, lines, corrected)
            # 该错报有此前未归因的 V02 就沿用 (不看本次比对), 没有才在对不上时新建 (谓词在调用内用完, B023 不适用)
            findings = earlier_findings(
                ctx.state,
                ctx.procedure.id,
                (V02,),
                lambda x: x.detail.get("misstatement_id") == m.id,  # noqa: B023
            )
            if not findings and updates[m.id].status is MisstatementStatus.communicated:
                exceptions.append(mismatch_exception(ctx, m, source.subject_id, lines, corrected, len(exceptions) + 1))
                findings.append(exceptions[-1])
            subjects.append(verification_subject(m, source, lines, rows, corrected, findings))
        judged = await judge_all(
            self.deps,
            ctx,
            subjects,
            self.reviewer,
            judging_steps(self.deps),
            annotate=lambda outcome: with_verification(outcome, updates),
        )
        return VerifiedRun(judged, rules.outcomes, exceptions)

    async def interpret(self, ctx: RunContext, findings: JudgedRun) -> StatePatch:
        """判断公共流程的解读 (judgments_patch: 判断, 复核任务, 例外, 执行记录终态), 去掉为空的字段: 没有判断对象时片段
        只有 procedure_runs. FIN-09 的 V02 是本次刚建的 (judgments_patch 的 created); FIN-03 没有规则例外."""
        created = findings.exceptions if isinstance(findings, VerifiedRun) else []
        patch = judgments_patch(self.deps, ctx, findings.judged, findings.outcomes, created=created)
        return {key: value for key, value in patch.items() if value or key == "procedure_runs"}

    def prepare(self, ctx: RunContext) -> dict[str, Any] | None:
        """FIN-08 派发的材料 (需求 R161 验收 2): 逐条列沟通对象 (to_communicate) 的 LISTED_FIELDS 与来源判断 (状态中的
        当前版本) 的结论 source_conclusion (来源是例外或判断不在状态中时为空), 加更正请求 request; 均为 JSON 形式.
        没有沟通对象时为 None (空执行, 不建任务)."""
        misstatements = to_communicate(ctx.state)
        if not misstatements:
            return None
        conclusions = {j.id: j.conclusion for j in ctx.state["judgments"]}
        return {
            "request": CORRECTION_REQUEST,
            "misstatements": [
                {
                    **m.model_dump(mode="json", include=LISTED_FIELDS),
                    "source_conclusion": conclusions.get(m.judgment_id or ""),
                }
                for m in misstatements
            ],
        }

    def check(self, ctx: RunContext, task: ReviewTask, record: ReviewRecord) -> list[str]:
        """FIN-08 登记的核对 (需求 R161 验收 7, 在通用核对之后): 每条材料中的错报恰好一行, 不多不少; 依据的版本须为
        材料中的 (不是抛 VersionConflict, 即 VERSION_MISMATCH); 沟通与回复的时间都不能晚于决定时刻. 返回问题原文."""
        assert task.manual_request is not None  # manual 任务都带请求
        listed = {row["id"]: row["version"] for row in task.manual_request.material["misstatements"]}
        responses = responses_of(record)
        counts = Counter(r.misstatement_id for r in responses)
        problems = [f"{mid} 登记了 {n} 行" for mid, n in counts.items() if n > 1]
        missing = [mid for mid in listed if mid not in counts]
        if missing:
            problems.append(f"登记漏了材料中的错报 {', '.join(missing)}")
        extra = [mid for mid in counts if mid not in listed]
        if extra:
            problems.append(f"登记了材料中没有的错报 {', '.join(extra)}")
        if problems:
            return problems
        for r in responses:
            if r.base_version != listed[r.misstatement_id]:
                raise VersionConflict(
                    f"{r.misstatement_id} 的登记依据第 {r.base_version} 版, 材料中为第 {listed[r.misstatement_id]} 版"
                )
        return [
            f"{r.misstatement_id} 的{label} {at.isoformat()} 晚于决定时刻 {record.decided_at.isoformat()}"
            for r in responses
            for label, at in (("沟通时间", r.communicated_at), ("回复时间", r.responded_at))
            if at > record.decided_at
        ]

    def collect(self, ctx: RunContext, record: ReviewRecord) -> StatePatch:
        """FIN-08 的结果整理 (需求 R161 验收 3 至 6): 登记的每条回复写出所指错报的新版本 (after_response), 按登记的
        顺序; 错报取 ctx.state 中的版本 (派发之前的), 重放时重算得同一版本. 登记的错报不在状态中抛 KeyError (check
        已核对登记覆盖材料, 正常走不到)."""
        by_id = {m.id: m for m in ctx.state["misstatements"]}
        return {"misstatements": [after_response(by_id[r.misstatement_id], r) for r in responses_of(record)]}

    async def fin03_subjects(self, ctx: RunContext, misstatements: Sequence[Misstatement]) -> Subjects:
        """FIN-03 的判断对象: 给定的错报 (已筛选的未分类错报, 见 execute), 对象 ID 为错报 ID, mock 的 fixture_key 为
        来源判断的对象. data 按 FIN-03.md.j2 头注释 (均为 JSON 形式):
        - misstatement: 错报; source_judgment: 来源判断 (状态中的当前版本) 的结论与证据;
        - journal_lines: 来源判断的对象为凭证号时该凭证的全部行 (journal_entries(entry_nos=[对象])), 加以该对象为
          原始单据的行 (journal_entries(source_docs=[对象])); 两次调用分开, 按行 ID 去重并排序; 都取不到为空;
        - cited_rows: 来源判断其余证据指针所指的行 (FinancialDataStore.record, 带 source_type; 已在 journal_lines 的
          分录行不重复, 取不到的不列), 按指针顺序去重;
        - documents: 来源判断证据中引用的文件的登记, 按指针顺序去重 (没登记的不列);
        - fraud_risks: 风险清单中 fraud_related 为真的风险.
        不给别的程序的判断 (规格书 8.5). 来源判断不在状态中抛 LookupError.
        """
        eid = ctx.engagement.id
        store = self.deps.financial_store
        judgments = {j.id: j for j in ctx.state["judgments"]}
        registered = {d.id: d for d in await store.documents(eid)}
        risks = ctx.state["risk_register"]
        fraud_risks = [r.model_dump(mode="json", include=RISK_FIELDS) for r in risks if r.fraud_related]
        subjects = []
        for misstatement in misstatements:
            source = source_judgment(misstatement, judgments)
            lines = {
                line.id: line
                for found in (
                    await store.journal_entries(eid, entry_nos=[source.subject_id]),
                    await store.journal_entries(eid, source_docs=[source.subject_id]),
                )
                for line in found
            }
            data = {
                "misstatement": misstatement.model_dump(mode="json", include=MISSTATEMENT_FIELDS),
                "source_judgment": source.model_dump(mode="json", include=SOURCE_FIELDS),
                "journal_lines": [
                    {k: v for k, v in row.items() if k in LINE_FIELDS}
                    for row in journal_rows([lines[i] for i in sorted(lines)])
                ],
                "cited_rows": await self.cited_rows(eid, source, set(lines)),
                "documents": self.cited_documents(source, registered),
                "fraud_risks": fraud_risks,
            }
            subjects.append(Subject(misstatement.id, data, fixture_key=source.subject_id))
        return Subjects(subjects)

    async def cited_rows(self, eid: str, source: Judgment, listed: set[str]) -> list[dict[str, Any]]:
        """来源判断证据指针所指的行 (JSON 形式, 带 source_type): 已列进分录行的不重复, 取不到的不列, 同一行只取一次."""
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for pointer in source.evidence:
            key = (pointer.source_type, pointer.source_id)
            if pointer.source_type not in ROW_SOURCES or key in seen:
                continue
            seen.add(key)
            if pointer.source_type == "journal_entry" and pointer.source_id in listed:
                continue
            row = await self.deps.financial_store.record(eid, cast(SourceType, pointer.source_type), pointer.source_id)
            if row is not None:
                rows.append({"source_type": pointer.source_type, **row})
        return cast(list[dict[str, Any]], JSON_ROWS.dump_python(rows, mode="json"))

    @staticmethod
    def cited_documents(source: Judgment, registered: dict[str, Any]) -> list[dict[str, Any]]:
        """来源判断证据中引用的文件的登记 (JSON 形式), 按指针顺序去重; 没登记的不列."""
        ids = dict.fromkeys(p.source_id for p in source.evidence if p.source_type == "document")
        return [registered[i].model_dump(mode="json", include=DOCUMENT_FIELDS) for i in ids if i in registered]
