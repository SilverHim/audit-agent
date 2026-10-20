"""A7 ReviewAgent: GEN-09 判断独立复核 (规格书 5.2, 5.3, 5.5, 6.3 GEN-09, 8.4).

A6 每生成一条模型类判断即同步调用 review: 受控子图, 不经编排状态, 结果写回判断的 critic 字段后再交 ReviewQueue 分类.
两轮都用复核模型 (AgentDeps.review_llm) 与独立的系统提示词, 避免同源偏差:
- 第一轮只给证据指针解析出的内容, 不给 A6 的结论与推理链, 得出独立结论与推理链 (经 8.6 校验; 推理链无效的,
  争议仲裁时不提供 a7_upheld, 见 judgment_engine.critic_chain_ok);
- 第二轮再给 A6 的结论, 比对得出 consistent 或 inconsistent 与理由.
consistent 且独立推理链有效时置信度计入复核分项 (8.4, 权重见 rules.json); 推理链无效的 consistent 不算复核过,
记 not_applicable 转人工. inconsistent 由 ReviewQueue 建争议任务, 不计分.
"""

from collections.abc import Sequence
from typing import Any, ClassVar, cast

from pydantic_core import to_jsonable_python

from backend.agents.base import ProcedureAgent, RunContext
from backend.agents.ingest import client_profile
from backend.contracts.knowledge import LLMSpec, ProcedureTemplate
from backend.contracts.llm_outputs import CriticIndependent, CriticVerdictOut
from backend.contracts.models import (
    AgentId,
    AuditException,
    AutonomyLevel,
    ClientProfile,
    CriticTrigger,
    CriticVerdict,
    EvidencePointer,
    EvidenceType,
    Judgment,
    ProposedAction,
    Reliability,
    StepKind,
    StepStatus,
)
from backend.contracts.services import ValidationContext
from backend.contracts.state import StatePatch
from backend.services.features import COLLAB
from backend.services.judgment_engine import PointerResolver, critic_chain_ok
from backend.services.sampling import sampled

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


def trigger(
    level: AutonomyLevel,
    judgment: Judgment,
    rate: float,
    given: Sequence[EvidencePointer] = (),
    collab: bool = False,
) -> CriticTrigger | None:
    """命中的触发规则, 按顺序判定, 命中即停; 都不中为 None (规格书 6.3 GEN-09 trigger). 跳过规则 (L1, L2) 在这之前
    由 review 判定.

    - main: 运行时自主度 L3, L4 且建议 accept 或 close (会自动关闭的判断).
    - internal_copy (collab 打开时): given 中非准则指针至少一个, 且可靠性全为 internal_copy (只靠内部复印件).
    - assumption: 推理链有 assumption 步骤.
    - unsupported (collab 打开时): 推理链有未获证据支撑 (status unsupported) 的步骤.
    - sample: 抽中 (services/sampling.sampled, 键为判断 ID, 重放结果不变).
    given: 第一轮会给出的证据 (ReviewAgent.given_evidence: 按来源重新解析, 可靠性取数据行类型或文件登记, 不采信
    判断里填的值). collab 关闭时只判 main, assumption, sample (同 collab 之前).
    本期不做: 提示词或配置刚改过 (procedure.changed_since_commit), 人机一致率低 (需求清单第 13 批定不做).
    """
    if level in REVIEWED_LEVELS and judgment.proposed_action in CLOSING_ACTIONS:
        return CriticTrigger.main
    if collab and internal_copy_only(given):
        return CriticTrigger.internal_copy
    if any(step.kind is StepKind.assumption for step in judgment.reasoning_chain):
        return CriticTrigger.assumption
    if collab and any(step.status is StepStatus.unsupported for step in judgment.reasoning_chain):
        return CriticTrigger.unsupported
    if sampled(judgment.id, rate):
        return CriticTrigger.sample
    return None


def internal_copy_only(given: Sequence[EvidencePointer]) -> bool:
    """非准则指针至少一个, 且可靠性全为 internal_copy."""
    rows = [p for p in given if p.source_type != "standard"]
    return bool(rows) and all(p.reliability is Reliability.internal_copy for p in rows)


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

    async def review(self, ctx: RunContext, judgment: Judgment, findings: Sequence[AuditException] = ()) -> Judgment:
        """复核 A6 在 ctx 这次执行中生成的一条判断, 返回写好 critic 字段 (consistent 时含新置信度) 的同一版本.
        findings: 该判断解释的例外 (A6 判断时同一组规则例外, 含本次执行刚建的), 第一轮校验错报草稿的金额用.

        跳过 (skipped): 功能开关 critic 关闭 (AgentDeps.features), 没有复核模型 (只在测试), 程序运行时自主度 L1, L2,
        未命中触发规则 (见 trigger); critic_reason 写明原因.
        功能 collab 打开时触发规则另判 internal_copy 与 unsupported, 命中的那条写进 critic_trigger (之后复核没做成也
        保留; 取第一轮证据本身出错, 如存储读不了时, 记 not_applicable, critic_trigger 为空); 关闭时 critic_trigger 为空.
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
        rate = float(template.params.get("sample_rate", 0))
        collab = COLLAB in self.deps.features
        given: list[EvidencePointer] | None = None
        try:
            profile = await self.cited_profile(ctx, judgment)
            # 第一轮要给的证据: collab 打开时 internal_copy 要用, 先取, 复核时沿用 (主规则命中时复核本来也要取)
            if collab:
                given = await self.given_evidence(ctx.engagement.id, judgment, profile)
            hit = trigger(level, judgment, rate, given or (), collab)
            if hit is None:
                return skip(judgment, "未命中独立复核的触发规则")
            if collab:
                judgment = judgment.model_copy(update={"critic_trigger": hit})
            return await self.cross_check(ctx, template, judgment, given, findings, profile)
        except Exception as e:
            return incomplete(judgment, str(e))

    async def cross_check(
        self,
        ctx: RunContext,
        template: ProcedureTemplate,
        judgment: Judgment,
        given: list[EvidencePointer] | None = None,
        findings: Sequence[AuditException] = (),
        profile: ClientProfile | None = None,
    ) -> Judgment:
        """两轮调用复核模型 (见模块说明). 没有能给的证据, 或任一轮未通过校验时记 not_applicable.
        given: review 判定触发规则时已取得的第一轮证据 (given_evidence), 为空时在这里取.
        findings: 该判断解释的例外, 第一轮校验错报草稿的金额用 (口径同 A6 的错报草稿).
        profile: 判断引用的客户画像 (cited_profile), review 已取得时传入; 为空时在这里取 (没引用时不读存储).
        功能 collab 打开时另写回第一轮的建议动作与 (通过校验的) 错报草稿 (需求 R150)."""
        eid, subject = ctx.engagement.id, judgment.subject_id
        engine, llm = self.deps.judgments, self.deps.review_llm
        procedure = {
            "id": ctx.template.id,
            "name": ctx.template.name,
            "assertions": [a.value for a in ctx.template.assertions],
        }
        profile = profile if profile is not None else await self.cited_profile(ctx, judgment)
        pointers = given if given is not None else await self.given_evidence(eid, judgment, profile)
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
            {"pointer": p.model_dump(mode="json"), "content": await self.evidence_content(eid, p, profile)}
            for p in pointers
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
            ValidationContext(
                allowed_evidence=allowed,
                rule_findings=list(findings),
                critic_drafts=COLLAB in self.deps.features,
                client_profile=profile,
            ),
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
        if COLLAB in self.deps.features:
            update |= {
                "critic_proposed_action": independent.proposed_action,
                "critic_misstatement_draft": independent.misstatement_draft,
            }
        if verdict.critic_verdict == "consistent":
            if not critic_chain_ok(steps):  # 结论相同而依据站不住, 不算复核过; 推理链照存, 供人复核时看
                detail = f"判为一致, 但独立推理链没有通过校验, 不计复核分项 ({brief(first.report.errors)})"
                return incomplete(judgment, detail, **update)
            parts = {**judgment.confidence_parts, "critic": 1.0}
            update |= {"confidence_parts": parts, "confidence": engine.confidence(parts)}
        return judgment.model_copy(update=update)

    async def cited_profile(self, ctx: RunContext, judgment: Judgment) -> ClientProfile | None:
        """判断引用了客户画像 (client_profile 指针, 需求 R128) 时, 本项目采用的那一版 (状态中的 client_profile_version);
        没引用时为空, 不读项目存储."""
        if not any(p.source_type == "client_profile" for p in evidence_pointers(judgment)):
            return None
        return await client_profile(self.deps, ctx.engagement, ctx.state["client_profile_version"])

    async def given_evidence(
        self, eid: str, judgment: Judgment, profile: ClientProfile | None = None
    ) -> list[EvidencePointer]:
        """第一轮给出的证据: 判断引用的指针 (evidence_pointers) 中按 OutputValidator 引用级解析得了的, 改写后的指针.
        profile: 本项目采用的客户画像 (cited_profile), client_profile 指针按它解析, 规则同 OutputValidator.

        A6 校验时解析不了的指针原样留在判断里 (只要还有别的有效指针), 如指向 llm_call 的留痕, 其明细含旧输出的结论;
        这些既不给模型, 也不进推理链可引用的证据.
        """
        resolver = PointerResolver(self.deps.judgments.validator, eid, profile)
        given = []
        for p in evidence_pointers(judgment):
            resolved, _ = await resolver.resolve(p)
            if resolved is not None:
                given.append(resolved)
        return given

    async def evidence_content(self, eid: str, pointer: EvidencePointer, profile: ClientProfile | None = None) -> Any:
        """证据指针所指的内容: 数据行 (JSON), 文件该页的文本, 准则片段原文, 外部调用留痕的摘要与明细
        (只有这类留痕能通过 given_evidence), 客户画像 (profile) 中 locator 那个字段的 JSON. 查不到给说明文字."""
        source_type, source_id = pointer.source_type, pointer.source_id
        if source_type == "client_profile":
            if profile is None or profile.id != source_id or pointer.locator not in ClientProfile.model_fields:
                return "(client profile field not available)"
            return to_jsonable_python(getattr(profile, pointer.locator))
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
