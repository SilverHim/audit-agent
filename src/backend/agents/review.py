"""A7 ReviewAgent: GEN-09 判断独立复核 (规格书 5.2, 5.3, 5.5, 6.3 GEN-09, 8.4).

A6 每生成一条模型类判断即同步调用 review: 受控子图, 不经编排状态, 结果写回判断的 critic 字段后再交 ReviewQueue 分类.
两轮都用复核模型 (AgentDeps.review_llm) 与独立的系统提示词, 避免同源偏差:
- 第一轮只给证据指针解析出的内容, 不给 A6 的结论与推理链, 得出独立结论与推理链 (经 8.6 校验; 推理链无效的,
  争议仲裁时不提供 a7_upheld, 见 judgment_engine.critic_chain_ok);
- 第二轮再给 A6 的结论, 比对得出 consistent 或 inconsistent 与理由.
consistent 且独立推理链有效时置信度计入复核分项 (8.4, 权重见 rules.json); 推理链无效的 consistent 不算复核过,
记 not_applicable 转人工. inconsistent 由 ReviewQueue 建争议任务, 不计分.
"""

import hashlib
from collections.abc import Sequence
from typing import Any, ClassVar, cast

from pydantic_core import to_jsonable_python

from backend.agents.base import ProcedureAgent, RunContext
from backend.contracts.knowledge import LLMSpec, ProcedureTemplate
from backend.contracts.llm_outputs import CriticIndependent, CriticVerdictOut
from backend.contracts.models import (
    AgentId,
    AutonomyLevel,
    CriticVerdict,
    EvidencePointer,
    EvidenceType,
    Judgment,
    ProposedAction,
    StepKind,
    StepStatus,
)
from backend.contracts.services import ValidationContext
from backend.contracts.state import StatePatch
from backend.services.judgment_engine import PointerResolver, critic_chain_ok

GEN09 = "GEN-09"
CRITIC = "critic"  # 功能名 (services/features.FEATURES), 关闭时不做独立复核
# 独立的系统提示词 (5.5: 与生成者至少不同的系统提示词); {schema} 为输出模型的 JSON schema, JSON 模式要求出现 "JSON"
CRITIC_SYSTEM = """You are an independent reviewer. Judge only from the inputs given in this conversation, \
not from any earlier analysis you may have seen. Reply with a single JSON object that conforms to the JSON schema \
below. Write nothing before or after the object.

{schema}"""
# 第二轮换用的提示词与输出模型 (程序 YAML 的 llm 是第一轮的)
VERDICT_SPEC = LLMSpec(prompt="GEN-09-verdict.md.j2", output_model="CriticVerdictOut", max_tokens=1024)
REVIEWED_LEVELS = (AutonomyLevel.L3, AutonomyLevel.L4)  # 主规则: 人只看异常或事后抽查, 会自动关闭的判断必复核
CLOSING_ACTIONS = (ProposedAction.accept, ProposedAction.close)  # 主规则: 会关闭事项的建议动作
SKIPPED_LEVELS = (AutonomyLevel.L1, AutonomyLevel.L2)  # 跳过规则: 审计师逐条复核


def sampled(judgment_id: str, rate: float) -> bool:
    """确定性抽样: 判断 ID 的 sha256 前 8 位折成 [0, 1), 小于 rate 即抽中. 判断 ID 确定, 重放结果不变."""
    return int(hashlib.sha256(judgment_id.encode()).hexdigest()[:8], 16) / 0x1_0000_0000 < rate


def trigger(level: AutonomyLevel, judgment: Judgment, rate: float) -> str | None:
    """命中的触发规则 (任一即复核), 没命中为 None: main 主规则; assumption 推理链含假设步骤; sample 抽样."""
    if level in REVIEWED_LEVELS and judgment.proposed_action in CLOSING_ACTIONS:
        return "main"
    if any(step.kind is StepKind.assumption for step in judgment.reasoning_chain):
        return "assumption"
    if sampled(judgment.id, rate):
        return "sample"
    return None


def evidence_pointers(judgment: Judgment) -> list[EvidencePointer]:
    """判断引用的全部证据指针 (结论引用的与推理链各步的), 按出现顺序去重. 只取指针, 不取推理链的文字."""
    seen: set[tuple[str, str, str, int | None]] = set()
    pointers: list[EvidencePointer] = []
    for p in [*judgment.evidence, *(p for step in judgment.reasoning_chain for p in step.evidence)]:
        key = (p.source_type, p.source_id, p.locator, p.page)
        if key not in seen:
            seen.add(key)
            pointers.append(p)
    return pointers


class ReviewAgent(ProcedureAgent[None]):
    """A7: 执行 GEN-09. 不单独派发 (GEN-09 不进程序包), 由 FieldworkAgent 对每条判断调用 review."""

    agent_id: ClassVar[AgentId] = AgentId.A7  # 智能体编号
    procedures: ClassVar[frozenset[str]] = frozenset({GEN09})  # 负责的程序

    async def execute(self, ctx: RunContext) -> None:
        raise NotImplementedError("GEN-09 不单独派发, 由 A6 对每条判断调用 ReviewAgent.review")

    async def interpret(self, ctx: RunContext, findings: None) -> StatePatch:
        raise NotImplementedError("GEN-09 不单独派发, 由 A6 对每条判断调用 ReviewAgent.review")

    async def review(self, ctx: RunContext, judgment: Judgment) -> Judgment:
        """复核 A6 在 ctx 这次执行中生成的一条判断, 返回写好 critic 字段 (consistent 时含新置信度) 的同一版本.

        跳过 (skipped): 功能开关 critic 关闭 (AgentDeps.features), 没有复核模型 (只在测试), 程序运行时自主度 L1, L2,
        未命中触发规则; critic_reason 写明原因.
        复核没做成 (not_applicable): 判断没有能解析的证据; 任一轮校验重试后仍失败, 或调用出错 (CancelledError 除外);
        判为 consistent 而独立推理链无效. ReviewQueue 据此转人工.
        """
        level = ctx.procedure.autonomy_level
        if CRITIC not in self.deps.features:
            return skip(judgment, "功能开关 critic 关闭, 不做独立复核")
        if self.deps.review_llm is None:
            return skip(judgment, "未配置复核模型, 不做独立复核")
        if level in SKIPPED_LEVELS:
            return skip(judgment, f"{level.value}: 审计师逐条复核, 不经独立复核 (GEN-09 跳过规则)")
        template = self.deps.library.load(GEN09)
        if trigger(level, judgment, float(template.params.get("sample_rate", 0))) is None:
            return skip(judgment, "未命中独立复核的触发规则")
        try:
            return await self.cross_check(ctx, template, judgment)
        except Exception as e:
            return incomplete(judgment, str(e))

    async def cross_check(self, ctx: RunContext, template: ProcedureTemplate, judgment: Judgment) -> Judgment:
        """两轮调用复核模型 (见模块说明). 没有能给的证据, 或任一轮未通过校验时记 not_applicable."""
        eid, subject = ctx.engagement.id, judgment.subject_id
        engine, llm = self.deps.judgments, self.deps.review_llm
        procedure = {
            "id": ctx.template.id,
            "name": ctx.template.name,
            "assertions": [a.value for a in ctx.template.assertions],
        }
        pointers = await self.given_evidence(eid, judgment)
        if not pointers:
            return incomplete(judgment, "判断没有能解析的证据指针")
        # 推理链可引用的: 判断引用的证据, 加上该程序的准则片段 (提示词里另列, standard 步骤指向它们)
        allowed = [
            *pointers,
            *(
                EvidencePointer(type=EvidenceType.standard, source_type="standard", source_id=fid, locator="")
                for fid in ctx.template.standards
            ),
        ]
        items = [
            {"pointer": p.model_dump(mode="json"), "content": await self.evidence_content(eid, p)} for p in pointers
        ]
        first = await engine.call_llm(
            eid,
            template,
            ctx.run,
            {
                "subject_id": subject,
                "procedure": procedure,
                "items": items,
                "standards": engine.standards(ctx.template),
            },
            ValidationContext(allowed_evidence=allowed),
            self.agent_id,
            f"{subject}-r1",
            llm=llm,
            system=CRITIC_SYSTEM,
        )
        if first.report.level_failed is not None:
            return incomplete(
                judgment, f"第一轮输出未通过 {first.report.level_failed} 校验 ({brief(first.report.errors)})"
            )
        independent = cast(CriticIndependent, first.report.output)
        unsupported = set(first.report.unsupported_steps)
        steps = [
            step.model_copy(update={"status": StepStatus.unsupported if i in unsupported else StepStatus.supported})
            for i, step in enumerate(independent.reasoning_chain)
        ]
        second = await engine.call_llm(
            eid,
            template.model_copy(update={"llm": VERDICT_SPEC}),
            ctx.run,
            {
                "subject_id": subject,
                "procedure": procedure,
                "independent": {
                    "conclusion": independent.conclusion,
                    "reasoning_chain": [s.model_dump(mode="json") for s in steps],
                },
                "generator": {"conclusion": judgment.conclusion, "proposed_action": judgment.proposed_action.value},
            },
            ValidationContext(),
            self.agent_id,
            f"{subject}-r2",
            llm=llm,
            system=CRITIC_SYSTEM,
        )
        if second.report.level_failed is not None:
            return incomplete(
                judgment, f"第二轮输出未通过 {second.report.level_failed} 校验 ({brief(second.report.errors)})"
            )
        verdict = cast(CriticVerdictOut, second.report.output)
        update: dict[str, Any] = {
            "critic_verdict": CriticVerdict(verdict.critic_verdict),
            "critic_reason_type": verdict.critic_reason_type,
            "critic_reason": verdict.critic_reason,
            "critic_conclusion": independent.conclusion,
            "critic_reasoning_chain": steps,
            "critic_model": second.model,
            "critic_trail_ref": second.trail_ids[-1],
        }
        if verdict.critic_verdict == "consistent":
            if not critic_chain_ok(steps):  # 结论相同而依据站不住, 不算复核过; 推理链照存, 供人复核时看
                detail = f"判为一致, 但独立推理链没有通过校验, 不计复核分项 ({brief(first.report.errors)})"
                return incomplete(judgment, detail, **update)
            parts = {**judgment.confidence_parts, "critic": 1.0}
            update |= {"confidence_parts": parts, "confidence": engine.confidence(parts)}
        return judgment.model_copy(update=update)

    async def given_evidence(self, eid: str, judgment: Judgment) -> list[EvidencePointer]:
        """第一轮给出的证据: 判断引用的指针 (evidence_pointers) 中按 OutputValidator 引用级解析得了的, 改写后的指针.

        A6 校验时解析不了的指针原样留在判断里 (只要还有别的有效指针), 如指向 llm_call 的留痕, 其明细含旧输出的结论;
        这些既不给模型, 也不进推理链可引用的证据.
        """
        resolver = PointerResolver(self.deps.judgments.validator, eid)
        given = []
        for p in evidence_pointers(judgment):
            resolved, _ = await resolver.resolve(p)
            if resolved is not None:
                given.append(resolved)
        return given

    async def evidence_content(self, eid: str, pointer: EvidencePointer) -> Any:
        """证据指针所指的内容: 数据行 (JSON), 文件该页的文本, 准则片段原文, 外部调用留痕的摘要与明细
        (只有这类留痕能通过 given_evidence). 查不到给说明文字."""
        source_type, source_id = pointer.source_type, pointer.source_id
        if source_type == "standard":
            fragment = self.deps.library.fragment(source_id)
            return fragment.text if fragment is not None else "(standard fragment not found)"
        if source_type == "document":
            pages = await self.deps.evidence_store.pages(eid, source_id)
            page = pointer.page or 0
            return pages[page - 1] if 1 <= page <= len(pages) else "(page not available)"
        if source_type == "trail":
            entry = await self.deps.trail.get(eid, source_id)
            if entry is None:
                return "(trail not found)"
            return to_jsonable_python({"summary": entry.summary, "payload": entry.payload})
        row = await self.deps.financial_store.record(eid, cast(Any, source_type), source_id)
        return to_jsonable_python(row) if row is not None else "(row not found)"  # 日期, 时间转成字符串


def skip(judgment: Judgment, reason: str) -> Judgment:
    return judgment.model_copy(update={"critic_verdict": CriticVerdict.skipped, "critic_reason": reason})


def incomplete(judgment: Judgment, detail: str, **update: Any) -> Judgment:
    """复核没做成 (not_applicable), critic_reason 写明原因; update 为同时写回的其他 critic 字段."""
    reason = f"独立复核没有完成: {detail}"
    return judgment.model_copy(
        update={**update, "critic_verdict": CriticVerdict.not_applicable, "critic_reason": reason}
    )


def brief(errors: Sequence[str]) -> str:
    return "; ".join(errors)[:300]
