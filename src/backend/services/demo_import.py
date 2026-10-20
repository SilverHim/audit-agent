"""演示数据导入 (需求 R269): 按补造清单 (adapters.xero_supplements.plan) 分阶段把 Xero 演示数据集写进项目.

- fieldwork 先写快照的七张表 (XERO_TABLES, 含应付账单 R178; 当成从 Xero 同步一次, source xero, 一个数据版本),
  再逐张写补造账表 (source import, 原文件名为补造目录里的来源文件, 各一个数据版本).
- 各阶段的补造 PDF 经上传交 A3 分类与抽取 (同 "上传文件"); 分类与清单不符的按清单指定类型; 再按清单一批补登记
  可靠性, 文件日期与关联对象 (一个数据版本). statements 的 fs_v1.pdf 在 REQ-financial_statements 为 open 时按请求
  上传 (不调大模型分类).
- 持项目锁一次做完 (DataService.demo_import 起的后台作业), 做完体检一次; 不确认数据已齐.

可重复执行, 以 Store 为准判断做过没有, 不盖掉之后的改动:
- 账表: 该表的版本历史里已有演示导入写过的一版 (快照为 source xero, 补造表为 source import 且原文件名相同) 就不再写.
- 文件: 同内容的文件存档时返回已有登记, upload 留痕同键只一条; 只处理没做完的 (ingest.unfinished).
- 改类型: 只在类型与清单不符, 且最近一次类型决定是 A3 做的 (或还没有) 时改; 审计师改过的不动.
- 补登记: 只给从没补登记过的文件补; 审计师补过的不动.
有写入时记一条 data 留痕 demo_import (payload 为 DemoImportResult); 什么都没写为 already_imported, 不记留痕, 但这一
阶段最近一条 demo_import 是 failed 时 (上次在写完之后才出错) 仍记一条 imported, 表示接着做完了. 作业 (含之前的补写
留痕) 出错时 (DataService.demo_import) 记一条 status failed 的 demo_import, 只有阶段与错误类别 (需求 R270). 阶段算
导入过, 当且仅当它最近一条 demo_import 是 imported (stage_statuses).
"""

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from backend.adapters.xero_supplements import DemoPlan, DemoUpload
from backend.agents.ingest import DETAILS, HELD, DocumentIntake
from backend.contracts.data import DemoImportResult, DemoStage, DocumentDetailsEdit, TableKind
from backend.contracts.models import AgentId, DocumentRef, Engagement, TrailEntry, TrailKind
from backend.services.trail_pages import all_of_kind

if TYPE_CHECKING:
    from backend.services.data_service import DataService

logger = logging.getLogger(__name__)
DEMO_JOB = "demo_import"  # 演示数据导入作业在 DataService 登记的种类
NO_DEMO = "该数据集没有演示资料"
RETYPE_REASON = "演示数据导入: 按补造清单指定类型"
DETAILS_REASON = "演示数据导入: 按补造清单补登记"
STAGE_NAMES: dict[DemoStage, str] = {"acceptance": "承接", "fieldwork": "外勤", "statements": "财报", "manual": "人工"}


def stage_statuses(entries: Iterable[TrailEntry]) -> dict[str, str]:
    """各阶段最近一条 demo_import 留痕的 status (imported 或 failed); entries 按写入顺序, 其余留痕不看."""
    found: dict[str, str] = {}
    for entry in entries:
        if entry.payload.get("action") == DEMO_JOB:
            found[str(entry.payload.get("stage"))] = str(entry.payload.get("status"))
    return found


@dataclass(frozen=True)
class Uploads:
    """一个阶段上传与处理的结果: 清单项与对应的登记 (按清单顺序), 新登记的文件名, 有没有处理文件."""

    pairs: list[tuple[DemoUpload, DocumentRef]]
    fresh_names: list[str]
    processed: bool


class DemoImport:
    """一个项目的演示数据导入 (清单 plan 已核对). run 是作业本体, 须在持项目锁时调用."""

    def __init__(self, service: "DataService", plan: DemoPlan) -> None:
        self.service = service  # 写入与上传经它的持锁内函数, 同 "账表导入", "上传文件" 与 "补登记"
        self.plan = plan

    def uploads(self, stage: DemoStage) -> list[DemoUpload]:
        return [u for u in self.plan.uploads if u.stage == stage]

    def planned_tables(self, stage: DemoStage) -> list[TableKind]:
        """这一阶段要写的表 (只有 fieldwork 有): 快照七张, 再是补造表 (sales_invoices, bank_accounts 两张各写两次)."""
        if stage != "fieldwork":
            return []
        return [*self.plan.snapshot, *(t.kind for t in self.plan.tables)]

    def labels(self, stage: DemoStage) -> list[str]:
        """作业涉及的账表与文件 (概览的 processing 与各表的 converting 按它)."""
        tables = [k.value for k in dict.fromkeys(self.planned_tables(stage))]
        return tables + [u.document_id for u in self.uploads(stage)]

    def started(self, stage: DemoStage) -> DemoImportResult:
        """发起时返回的结果: 要写的表与要上传的文件."""
        return DemoImportResult(
            stage=stage,
            status="started",
            tables=self.planned_tables(stage),
            uploaded=[u.file_name for u in self.uploads(stage)],
        )

    async def run(self, eid: str, stage: DemoStage, actor: str) -> DemoImportResult:
        """做一个阶段 (见模块说明), 返回结果. 持项目锁时调用 (未持锁抛 RuntimeError); 中途出错照常上抛 (已写的保留,
        再执行时接着做)."""
        self.service.require_lock(eid, "演示数据导入")
        deps = self.service.deps
        engagement = await deps.engagement_store.get(eid)
        if engagement is None:
            raise LookupError(f"Engagement {eid} not found")
        resumed = stage_statuses(await all_of_kind(deps.trail, eid, TrailKind.data)).get(stage) == "failed"
        tables = await self.write_tables(eid, actor) if stage == "fieldwork" else []
        uploads = await self.upload(eid, engagement, self.uploads(stage), actor)
        retyped = await self.retype(eid, engagement, uploads.pairs, actor)
        detailed = await self.details(eid, uploads.pairs, actor)
        current = {d.id: d for d in await deps.financial_store.documents(eid)}
        wrote = bool(tables or uploads.fresh_names or uploads.processed or retyped or detailed)
        result = DemoImportResult(
            stage=stage,
            status="imported" if wrote or resumed else "already_imported",
            tables=tables,
            uploaded=uploads.fresh_names,
            retyped=retyped,
            needs_manual=[u.file_name for u, d in uploads.pairs if current[d.id].status == "needs_manual"],
            data_version=(await deps.data.readiness(eid)).data_version,
        )
        if wrote:
            await self.service.checkup(eid)
        else:
            await self.service.checkup_if_stale(eid)  # 上次在体检之前中断时补做
            if not resumed:
                return result
        summary = f"演示数据导入 ({STAGE_NAMES[stage]}): 上次没做完的已做完, 没有新的写入"
        if wrote:
            written = f"写入 {len(set(tables))} 张表的 {len(tables)} 个版本, " if tables else ""
            summary = f"演示数据导入 ({STAGE_NAMES[stage]}): {written}上传 {len(result.uploaded)} 份文件" + (
                f", 按清单指定类型 {len(retyped)} 份" if retyped else ""
            )
        await deps.trail.record(
            TrailEntry(
                engagement_id=eid,
                kind=TrailKind.data,
                actor=actor,
                summary=summary,
                payload={"action": DEMO_JOB, **result.model_dump(mode="json")},
                created_at=datetime.now(UTC),
            )
        )
        return result

    async def record_failure(self, eid: str, stage: DemoStage, actor: str, error: BaseException) -> None:
        """作业 (含之前的补写留痕) 出错时记一条 data 留痕 demo_import (status failed, 需求 R270): 只写阶段, 错误类别
        (异常类名) 与当前的数据版本, 不写原始错误文本 (可能带文件内容或路径). 已写的保留, 再执行时接着做, 做完记
        imported (什么都没写也记). 留痕写不成只记日志."""
        deps = self.service.deps
        try:
            version = (await deps.data.readiness(eid)).data_version
            result = DemoImportResult(stage=stage, status="failed", error=type(error).__name__, data_version=version)
            await deps.trail.record(
                TrailEntry(
                    engagement_id=eid,
                    kind=TrailKind.data,
                    actor=actor,
                    summary=f"演示数据导入 ({STAGE_NAMES[stage]}) 没做完 ({result.error}), 再导入一次接着做",
                    payload={"action": DEMO_JOB, **result.model_dump(mode="json")},
                    created_at=datetime.now(UTC),
                )
            )
        except Exception:
            logger.exception("演示数据导入失败的留痕没写成: %s %s", eid, stage)

    async def write_tables(self, eid: str, actor: str) -> list[TableKind]:
        """快照七张表一个版本 (source xero), 补造表各一个版本 (source import, 原文件名); 写过的表跳过. 返回写了的表."""
        versions = await self.service.deps.data.versions(eid)
        synced = {v.kind for v in versions if v.source == "xero"}
        snapshot = {kind: rows for kind, rows in self.plan.snapshot.items() if kind not in synced}
        written: list[TableKind] = []
        if snapshot:
            await self.service.write_tables(eid, snapshot, source="xero", actor=actor, checkup=False)
            written += list(snapshot)
        for table in self.plan.tables:
            if any(v.kind is table.kind and v.source == "import" and v.file_name == table.file_name for v in versions):
                continue
            await self.service.write_tables(
                eid, {table.kind: table.rows}, source="import", actor=actor, file_name=table.file_name, checkup=False
            )
            written.append(table.kind)
        return written

    async def upload(self, eid: str, engagement: Engagement, uploads: list[DemoUpload], actor: str) -> Uploads:
        """上传这一阶段的文件并处理没做完的 (同 "上传文件", 不体检). 带补交请求的清单项在请求为 open 时按请求上传,
        否则照常分类. 按请求分组上传, 组内按清单顺序."""
        deps = self.service.deps
        before = {d.id for d in await deps.financial_store.documents(eid)}
        requests = {r.id: r for r in await deps.data.requests(eid)}
        groups: dict[str | None, list[DemoUpload]] = {}
        for upload in uploads:
            found = requests.get(upload.request_id) if upload.request_id is not None else None
            key = upload.request_id if found is not None and found.status == "open" else None
            groups.setdefault(key, []).append(upload)
        registered: dict[str, DocumentRef] = {}
        processed = False
        for request_id, group in groups.items():
            contents = [await asyncio.to_thread((self.plan.root / u.path).read_bytes) for u in group]
            saved = await self.service.register_uploads(eid, [u.file_name for u in group], contents, actor, request_id)
            registered |= {u.path: d for u, d in zip(group, saved.documents, strict=True)}
            processed = processed or bool(saved.fresh) or saved.requested is not None
            await self.service.process_uploads(eid, engagement, saved.fresh, saved.jobs, saved.wanted)
        current = {d.id: d for d in await deps.financial_store.documents(eid)}
        pairs = [(u, current[registered[u.path].id]) for u in uploads]
        return Uploads(pairs, [u.file_name for u, d in pairs if d.id not in before], processed)

    async def retype(
        self, eid: str, engagement: Engagement, pairs: list[tuple[DemoUpload, DocumentRef]], actor: str
    ) -> list[str]:
        """分类与清单不符的按清单指定类型 (同 "人工指定类型", 理由 RETYPE_REASON), 合同随即抽取; 只改最近一次类型决定
        是 A3 做的 (或还没有分类留痕) 的文件. 返回改了的文件名."""
        deps = self.service.deps
        wrong = [(u, d) for u, d in pairs if d.type is not u.document_type and d.status not in HELD]
        if not wrong:
            return []
        decided: dict[str, str] = {}  # 文件 ID → 最近一条 classify 留痕的 actor
        for entry in await all_of_kind(deps.trail, eid, TrailKind.data):
            if entry.payload.get("action") == "classify" and entry.payload.get("document_id"):
                decided[str(entry.payload["document_id"])] = entry.actor
        intake = DocumentIntake(deps)
        retyped: list[str] = []
        for upload, document in wrong:
            if decided.get(document.id, AgentId.A3) != AgentId.A3:
                continue
            kind = upload.document_type
            updated, job = await self.service.assign_type(intake, eid, document, kind, RETYPE_REASON, actor)
            await intake.classified(eid, updated, kind, engagement, job)
            retyped.append(upload.file_name)
        return retyped

    async def details(self, eid: str, pairs: list[tuple[DemoUpload, DocumentRef]], actor: str) -> list[str]:
        """按清单一批补登记可靠性, 文件日期与关联对象 (同 "补登记", 理由 DETAILS_REASON, 一个数据版本); 只给从没补登记
        过且与清单不同的文件. 返回补登记了的文件名."""
        deps = self.service.deps
        current = {d.id: d for d in await deps.financial_store.documents(eid)}
        edits: dict[str, DocumentDetailsEdit] = {}
        names: list[str] = []
        for upload, found in pairs:
            document = current[found.id]
            if document.status in HELD or await deps.data.extraction_versions(eid, DETAILS, document.id):
                continue
            wanted = {
                "reliability": upload.reliability,
                "document_date": upload.document_date,
                "related_ids": upload.related_ids,
            }
            given = {k: v for k, v in wanted.items() if v is not None}  # 补登记不能把文件日期清空
            if all(getattr(document, k) == v for k, v in given.items()):
                continue
            edits[document.id] = DocumentDetailsEdit(
                reliability=upload.reliability,
                document_date=upload.document_date,
                related_ids=upload.related_ids,
                reason=DETAILS_REASON,
            )
            names.append(upload.file_name)
        if edits:
            await self.service.save_details(eid, edits, actor)
        return names
