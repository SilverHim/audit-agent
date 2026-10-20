"""A8 WorkpaperAgent: 错报的分类 (FIN-03, 需求 R158); FIN-08 (沟通), FIN-09 (更正验证) 由错报闭环各需求加入.

返回字段: judgments, review_tasks, misstatements, procedure_runs (merge.AGENT_FIELDS). FIN-03 不写错报: 分类结果
记在判断的 misstatement_update 上, 判断确认后才写出错报新版本 (需求 R159).
"""

import re
from collections.abc import Sequence
from typing import Any, ClassVar, cast, get_args

from backend.agents.base import AgentDeps, ProcedureAgent, RunContext
from backend.agents.fieldwork import JSON_ROWS, journal_rows
from backend.agents.judging import Judged, JudgedRun, Subject, Subjects, in_scope, judge_all, judgments_patch
from backend.agents.review import ReviewAgent
from backend.contracts.llm_outputs import MisstatementClassificationDraft
from backend.contracts.models import (
    AgentId,
    AuditObjective,
    Judgment,
    Misstatement,
    MisstatementOrigin,
    MisstatementStatus,
    MisstatementUpdate,
    SourceType,
)
from backend.contracts.state import StatePatch
from backend.services.staleness import current_results

FIN03 = "FIN-03"
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


def misstatement_objective(misstatement: Misstatement, objectives: Sequence[AuditObjective]) -> str | None:
    """错报所属的审计目标 (需求 R158 验收 2): 审计目标的科目名作整词出现在错报科目里 (不分大小写, 前后不是字母, 数字或
    下划线; 科目名首尾是标点时也成立) 且认定相同的第一条, 按状态中审计目标的顺序; 没有为空. 如 {Revenue, Cutoff} 的
    错报配 OBJ-REV-CUT, "Interest income (270)" 不配任何目标. 纯函数."""
    for objective in objectives:
        word = rf"(?<!\w){re.escape(objective.account)}(?!\w)"
        if objective.assertion is misstatement.assertion and re.search(word, misstatement.account, re.IGNORECASE):
            return objective.id
    return None


def classification_update(
    misstatement: Misstatement, draft: MisstatementClassificationDraft, objective_id: str | None
) -> MisstatementUpdate:
    """一条错报的分类载荷: 类型与舞弊迹象取大模型给的性质, 审计目标与期间来源取规则 (不取大模型), 依据的版本为判断时
    的错报版本. 纯函数."""
    nature = draft.nature
    return MisstatementUpdate(
        misstatement_id=misstatement.id,
        base_version=misstatement.version,
        kind="classification",
        type=nature.type,
        fraud_indicative=nature.fraud_indicative,
        fraud_reason=nature.fraud_reason,
        origin=ORIGIN,
        objective_id=objective_id,
    )


def with_update(item: Judged, misstatements: dict[str, Misstatement], objectives: Sequence[AuditObjective]) -> Judged:
    """成功的判断写上分类载荷 (同一版本); 失败的原样. 判断的草稿 (JudgmentOutcome.draft) 为分类草稿, 判断对象为
    misstatements 中的错报. 纯函数."""
    judgment = item.outcome.judgment
    if judgment is None:
        return item
    draft = item.outcome.draft
    assert isinstance(draft, MisstatementClassificationDraft)  # FIN-03 的输出模型
    misstatement = misstatements[judgment.subject_id]
    update = classification_update(misstatement, draft, misstatement_objective(misstatement, objectives))
    judgment = judgment.model_copy(update={"misstatement_update": update})
    return Judged(item.ctx, item.outcome.model_copy(update={"judgment": judgment}))


class WorkpaperAgent(ProcedureAgent[JudgedRun]):
    """A8: 错报分类 (FIN-03, 混合程序). 规则部分定审计目标与期间来源, 大模型判类型与舞弊迹象, 走判断公共流程
    (agents/judging.py: judge, A7 复核, 降级, classify, 建任务), 判断带分类载荷 misstatement_update (需求 R158).

    舞弊迹象只记进载荷, 由审计师逐条复核时决定, 确认后带在错报上呈报到 FIN-01 闸门; 规格书 12.2 "为真则进环 3 阻断"
    与 ASA 240 第 37 段的重评舞弊风险本版不做.
    """

    agent_id: ClassVar[AgentId] = AgentId.A8  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({FIN03})  # 负责的程序 (功能 misstatement, 未启用时注册表不映射)

    def __init__(self, deps: AgentDeps) -> None:
        super().__init__(deps)
        self.reviewer = ReviewAgent(deps)  # A7: 判断公共流程中的独立复核 (FIN-03 为 L2, 按跳过规则不复核)

    async def execute(self, ctx: RunContext) -> JudgedRun:
        """FIN-03 的执行: 未分类的错报 (unclassified) 先按 in_scope 筛选 (rerun_subject_ids 与已交人处理的对象, 同 A6
        各程序), 再取数 (fin03_subjects) 交判断公共流程, 成功的判断带上分类载荷 (with_update). 没有判断对象时不调
        大模型."""
        if ctx.procedure.id != FIN03:
            raise NotImplementedError(ctx.procedure.id)
        targets = {m.id: m for m in unclassified(ctx.state) if in_scope(ctx, m.id)}
        subjects = await self.fin03_subjects(ctx, list(targets.values()))
        judged = await judge_all(self.deps, ctx, subjects, self.reviewer)
        objectives = ctx.state["audit_objectives"]
        return JudgedRun([with_update(item, targets, objectives) for item in judged], subjects.outcomes)

    async def interpret(self, ctx: RunContext, findings: JudgedRun) -> StatePatch:
        """判断公共流程的解读 (judgments_patch: 判断, 复核任务, 执行记录终态), 去掉为空的字段: 没有判断对象时片段只有
        procedure_runs. FIN-03 没有规则例外, 不写例外 (A8 也不能写)."""
        patch = judgments_patch(self.deps, ctx, findings.judged, findings.outcomes)
        assert not patch.get("exceptions")  # 判断对象不带规则例外, 归因与标未归因都不会发生
        return {field: value for field, value in patch.items() if value or field == "procedure_runs"}

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
            source = judgments.get(misstatement.judgment_id or "")
            if source is None:
                raise LookupError(f"错报 {misstatement.id} 的来源判断 {misstatement.judgment_id} 不在状态中")
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
