"""一次程序执行的结果摘要 (需求 R325, 纯函数): 结束留痕 (orchestration/trail.run_finished) 的 result, 工作区时间线与
审计助理据此说 "测了多少, 发现什么". 按程序种类只给有的项: 规则程序给被测总体与例外, 判断程序给判断与待复核, 抽取
程序给文件."""

from typing import Any

from backend.contracts.knowledge import ProcedureTemplate
from backend.contracts.models import ProcedureRun, RunPopulation, RunStatus, TaskStatus
from backend.contracts.state import StatePatch
from backend.services.populations import distinct_items, total_cents


def run_result(
    template: ProcedureTemplate, run: ProcedureRun, patch: StatePatch, population: RunPopulation | None
) -> dict[str, Any] | None:
    """run 的结果摘要; 执行失败时为空 (没有结果, 原因见执行记录的 failure_reason). patch 为写入 Store 后的片段 (重放时为
    从 Store 取回的), 只数 run_id 为本次执行的对象 (取回的片段带着别的执行的例外与任务). 各项:
    - populations, items, amount_cents: 有被测总体 (规则程序) 时. populations 为各规则的 {rule_id, count, amount_cents};
      items 与 amount_cents 为去重后的项目数与合计 (services.populations.distinct_items), 有规则未记录时都为空;
    - exceptions: 模板 outputs 含 exceptions 时, 本次执行出的例外数;
    - judgments: outputs 含 judgments 时, 本次执行的判断数;
    - pending_reviews: outputs 含 review_tasks 时, 本次执行建而待决的任务数;
    - documents: outputs 含 documents 时, 片段交回的文件数."""
    if run.status is not RunStatus.succeeded:
        return None
    result: dict[str, Any] = {}
    if population is not None:
        items = distinct_items(population)
        result["populations"] = [
            {"rule_id": r.rule_id, "count": r.count, "amount_cents": r.amount_cents} for r in population.rules
        ]
        result["items"] = len(items) if items is not None else None
        result["amount_cents"] = total_cents(i.amount_cents for i in items) if items is not None else None

    def own(field: str) -> list[Any]:
        return [x for x in patch.get(field, []) if x.run_id == run.id]

    outputs = set(template.outputs)
    if "exceptions" in outputs:
        result["exceptions"] = len(own("exceptions"))
    if "judgments" in outputs:
        result["judgments"] = len(own("judgments"))
    if "review_tasks" in outputs:
        result["pending_reviews"] = sum(1 for t in own("review_tasks") if t.status is TaskStatus.pending)
    if "documents" in outputs:
        result["documents"] = len(patch.get("documents", []))
    return result
