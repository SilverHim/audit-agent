"""被测总体与对象清单 (需求 R324, 纯函数): 规则程序的总体由规则引擎记下 (RunPopulation, 只在 Store), 这里去重与合计;
判断与抽取程序的对象清单不另存, 由 Store 中本次执行的判断, failed 任务与文件登记推出."""

from collections.abc import Collection, Iterable

from backend.contracts.models import (
    DocumentRef,
    DocumentType,
    Judgment,
    PopulationItem,
    ReviewTask,
    RunPopulation,
    TaskKind,
)

EXTRACTED = frozenset({"extracted", "needs_manual"})  # 抽取过的文件状态: 抽取成功, 抽取失败转人工处理


def total_cents(amounts: Iterable[int | None]) -> int | None:
    """金额合计: 都有金额时为其和 (一个也没有时为 0), 有一个没有金额即为空 (合计不全, 不给)."""
    found = list(amounts)
    return None if None in found else sum(a for a in found if a is not None)


def distinct_items(population: RunPopulation) -> list[PopulationItem] | None:
    """一次执行去重后的被测对象: 各规则总体的对象按 (对象类型, 行 ID) 去重, 留先出现的 (规则按 YAML 顺序); 没有
    行 ID 的对象 (汇总行) 各算一个. 有规则的总体未记录 (count 为空) 时为空: 缺了那条规则的对象, 去重数与合计都不全."""
    if any(rule.count is None for rule in population.rules):
        return None
    seen: set[tuple[str | None, str]] = set()
    items = []
    for rule in population.rules:
        for item in rule.items:
            if item.source_id is not None:
                key = (rule.source_type, item.source_id)
                if key in seen:
                    continue
                seen.add(key)
            items.append(item)
    return items


def judged_subjects(run_id: str, judgments: Iterable[Judgment], tasks: Iterable[ReviewTask]) -> list[str]:
    """判断程序一次执行的对象清单: 该执行的判断的 subject_id, 加该执行 failed 任务的 subject_id (单个对象失败, 没有
    判断; 整次失败的任务没有 subject_id, 不算), 去重按 ID 排序. 判断与任务可传本程序的全部, 按 run_id 筛选."""
    subjects = {j.subject_id for j in judgments if j.run_id == run_id}
    subjects |= {
        t.subject_id for t in tasks if t.run_id == run_id and t.kind is TaskKind.failed and t.subject_id is not None
    }
    return sorted(subjects)


def extracted_documents(types: Collection[DocumentType], documents: Iterable[DocumentRef]) -> list[str]:
    """抽取程序 (DOC-04, DOC-06) 的对象清单: 程序文件类型 (模板 document_types) 中抽取过的文件 (extracted, 与抽取
    失败的 needs_manual) 的 ID, 按 ID 排序. 文件登记不记是哪次执行处理的, 取的是程序当前的抽取对象; 失败的另有 failed
    任务 (subject_id 为合同编号或账户), 不并进来, 免得同一份文件记两次."""
    return sorted(d.id for d in documents if d.type in types and d.status in EXTRACTED)
