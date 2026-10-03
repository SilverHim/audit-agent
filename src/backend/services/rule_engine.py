"""规则引擎 (B 负责): 绑定参数执行程序 YAML 中的 SQL, 结果包装为 AuditException. 约定见 开发接口文档.md 第 5 节."""

from collections.abc import Mapping
from typing import Any

from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import ProcedureRun
from backend.contracts.ports import FinancialDataStore
from backend.contracts.services import RuleResult


class SqlRuleEngine:
    """RuleEngine 的实现: 绑定参数执行 YAML 中的 SQL, 结果包装成例外."""

    def __init__(self, financial_store: FinancialDataStore) -> None:
        self.financial_store = financial_store  # 用来执行 SQL 的财务数据存储

    async def run(
        self, eid: str, template: ProcedureTemplate, run: ProcedureRun, params: Mapping[str, Any]
    ) -> RuleResult:
        raise NotImplementedError
