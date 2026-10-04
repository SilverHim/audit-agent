"""Agent 抽象基类与依赖."""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, cast

from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import AgentId, Engagement, Procedure, ProcedureRun, RunStatus
from backend.contracts.ports import (
    AuditOutputStore,
    AuditTrail,
    ConfirmationChannel,
    DatasetLoader,
    EngagementStore,
    EvidenceStore,
    FinancialDataStore,
    LLMClient,
    RegistrySource,
    RuleEngine,
)
from backend.contracts.state import EngagementState, StatePatch
from backend.services.judgment_engine import JudgmentEngine
from backend.services.procedure_library import ProcedureLibrary
from backend.services.review_queue import ReviewQueue


@dataclass(frozen=True)
class AgentDeps:
    """Agent 的工具箱: 外部依赖由组装根 main.build_deps 一次注入, 测试时可换成假实现."""

    financial_store: FinancialDataStore  # 财务数据: 试算表, 分录, 发票, 银行流水等
    output_store: AuditOutputStore  # 审计产出的存储; Agent 只读, 写入经 StatePatch
    evidence_store: EvidenceStore  # 证据原件 (PDF) 的存档与读取
    datasets: DatasetLoader  # 合成数据集的载入与预置计划数据
    engagement_store: EngagementStore  # 项目与客户画像; 只读
    llm: LLMClient  # 大语言模型客户端, mock 或真实
    rules: RuleEngine  # 规则引擎: 执行程序 YAML 里的 SQL, 结果包装成例外
    judgments: JudgmentEngine  # 判断引擎: 组装上下文, 调大模型, 校验输出, 算置信度
    queue: ReviewQueue  # 复核队列: 判定哪些结果要人处理并建任务
    library: ProcedureLibrary  # 程序库: 程序模板与准则片段
    trail: AuditTrail  # 审计留痕, 只追加
    registry: RegistrySource  # ABN 与 ASIC 登记查询
    confirmations: ConfirmationChannel  # 函证平台: 发询证函, 收回函


@dataclass(frozen=True)
class RunContext:
    """一次程序执行的上下文: 节点在执行前准备好, 传给 Agent.run.

    procedure.rerun_subject_ids 非空时只重判其中的对象 (上一轮整组失败的对象, 可能多个), 只给本轮再次失败的对象
    建 failed 任务; 为空时整条执行.
    """

    engagement: Engagement  # 所属审计项目
    procedure: Procedure  # 要执行的程序实例, 含参数与状态
    template: ProcedureTemplate  # 该程序的模板, 来自程序 YAML
    run: ProcedureRun  # 本次执行记录, 状态为 running
    state: EngagementState  # 编排状态快照; 只读, 按需取切片, 不整体送 LLM


async def gather_all[T](aws: Iterable[Awaitable[T]]) -> list[T]:
    """并发执行, 全部结束后按输入顺序返回结果; 有出错的, 等其余都结束后抛第一个错误 (按输入顺序).

    asyncio.gather 遇错立即上抛而不取消其余任务, 它们会在 Agent 返回后继续运行, 可能在重跑之后才写入旧结果
    (如 DOC-04 的 save_contract). 这里不留仍在运行的任务. 外层被取消时, 未完成的任务随之取消.
    """
    results = await asyncio.gather(*aws, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return cast(list[T], results)


def finish_run(run: ProcedureRun, ended_at: datetime) -> ProcedureRun:
    """把运行中的执行记录置为成功终态. 版本号不变: 运行中那一版不写入存储, 终态就是第 1 版."""
    return run.model_copy(update={"status": RunStatus.succeeded, "ended_at": ended_at})


class ProcedureAgent[F](ABC):
    """执行审计程序的 Agent 的抽象基类, 对应设计规格 5.3 的三步: 选程序, 执行, 解读.

    子类声明编号与负责的程序, 实现 execute 与 interpret; F 是 execute 产出的原始发现类型.
    run 是模板方法, 子类不覆写.
    """

    agent_id: ClassVar[AgentId]  # 智能体编号, 子类给值
    procedures: ClassVar[frozenset[str]]  # 负责的程序编号, 子类给值

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps  # 外部依赖工具箱

    def select_procedures(self, templates: Iterable[ProcedureTemplate]) -> list[ProcedureTemplate]:
        """从程序模板中选出本 Agent 负责的程序. 按目标与风险装配由 A1 完成, 这里只按归属筛选."""
        return [t for t in templates if t.id in self.procedures]

    @abstractmethod
    async def execute(self, ctx: RunContext) -> F:
        """执行一条程序, 返回原始发现 (规则命中, 外部查询结果或 LLM 输出), 不组装状态片段."""

    @abstractmethod
    async def interpret(self, ctx: RunContext, findings: F) -> StatePatch:
        """把原始发现解读为要并入编排状态的片段. procedure_runs 须含 ctx.run 的终态 (succeeded 或 failed)."""

    async def run(self, ctx: RunContext) -> StatePatch:
        """编排节点调用的唯一入口: 先 execute, 再 interpret."""
        findings = await self.execute(ctx)
        return await self.interpret(ctx, findings)
