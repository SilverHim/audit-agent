"""Agent 抽象基类与依赖."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import AgentId, Engagement, Procedure, ProcedureRun
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

    fin: FinancialDataStore  # 财务数据: 试算表, 分录, 发票, 银行流水等
    outputs: AuditOutputStore  # 审计产出的存储; Agent 只读, 写入经 StatePatch
    evidence: EvidenceStore  # 证据原件 (PDF) 的存档与读取
    datasets: DatasetLoader  # 合成数据集的载入与预置计划数据
    engagements: EngagementStore  # 项目与客户画像; 只读
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
    """一次程序执行的上下文: 节点在执行前准备好, 传给 Agent.run."""

    engagement: Engagement  # 所属审计项目
    procedure: Procedure  # 要执行的程序实例, 含参数与状态
    template: ProcedureTemplate  # 该程序的模板, 来自程序 YAML
    run: ProcedureRun  # 本次执行记录, 状态为 running
    state: EngagementState  # 编排状态快照; 只读, 按需取切片, 不整体送 LLM


class ProcedureAgent(ABC):
    """执行审计程序的 Agent 的抽象基类: 子类声明编号与负责的程序, 并实现 run."""

    agent_id: ClassVar[AgentId]  # 智能体编号, 子类给值
    procedures: ClassVar[frozenset[str]]  # 负责的程序编号, 子类给值

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps  # 外部依赖工具箱

    @abstractmethod
    async def run(self, ctx: RunContext) -> StatePatch:
        """执行一条程序, 返回要并入编排状态的片段. procedure_runs 须含 ctx.run 的终态 (succeeded 或 failed)."""
