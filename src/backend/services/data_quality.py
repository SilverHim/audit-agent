"""数据管理智能体 (A3) 的数据规则: 就绪条件 (需求 R10), 数据体检与补交请求 (需求 R15), 程序要的资料与补交请求
(需求 R272). 纯函数, 不读写存储.

体检各项的口径见 开发接口文档.md 第 6 节 "数据体检"; 汇总 (CheckInputs) 由 DataStore.check_inputs 按 SQL 算,
summarize 是同一口径的 Python 版本 (假存储与对照测试用).
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from backend.adapters.xero_dataset import CREATED_BY as XERO_PREPARER
from backend.contracts.data import (
    CheckId,
    CheckInputs,
    CheckStatus,
    DataCheck,
    DataRequest,
    RequestReason,
    TableKind,
    TableStats,
    TableVersion,
)
from backend.contracts.knowledge import ProcedureInputs
from backend.contracts.models import DocumentType
from backend.contracts.records import Account, JournalEntry, TrialBalanceRow
from backend.services.ledger import PROFIT_AND_LOSS, opening_balances

# P1 开始前必须接入的表: 科目表, 本期与上期试算表, 总账分录
REQUIRED_TABLES: tuple[TableKind, ...] = (TableKind.accounts, TableKind.trial_balance, TableKind.journal_entries)
# 不算录入人的值: 空串, 连接器的占位值 (Xero API 不提供录入人, 适配器记 "xero")
PLACEHOLDER_PREPARERS = frozenset({"", XERO_PREPARER})


def missing_tables(stats: Mapping[TableKind, TableStats]) -> list[TableKind]:
    """就绪必需而还缺的表, 按 REQUIRED_TABLES 顺序. 试算表本期与上期任一期没有行都算缺."""
    missing = []
    for kind in REQUIRED_TABLES:
        item = stats.get(kind)
        if kind is TableKind.trial_balance:
            absent = item is None or item.current == 0 or item.prior == 0
        else:
            absent = item is None or item.records == 0
        if absent:
            missing.append(kind)
    return missing


def missing_in(inputs: CheckInputs) -> list[TableKind]:
    """同 missing_tables, 按体检汇总判断 (科目表没有行, 试算表任一期没有行, 总账没有行)."""
    absent = {
        TableKind.accounts: not inputs.account_types,
        TableKind.trial_balance: not inputs.tb_current or not inputs.tb_prior,
        TableKind.journal_entries: inputs.journal_rows == 0,
    }
    return [kind for kind in REQUIRED_TABLES if absent[kind]]


def summarize(
    accounts: Iterable[Account],
    trial_balance: Iterable[TrialBalanceRow],
    journals: Iterable[JournalEntry],
    period_start: date,
    period_end: date,
    dataset: str | None = None,
    adjustment_prefix: str | None = None,
) -> CheckInputs:
    """由各表的行算体检汇总, 口径同 PostgresDataStore.check_inputs (发生额不算凭证号以 adjustment_prefix 开头的审计
    更正分录)."""
    rows = list(journals)
    movements: dict[str, int] = {}
    for row in rows:
        adjustment = bool(adjustment_prefix) and row.entry_no.startswith(adjustment_prefix or "")
        if period_start <= row.posting_date <= period_end and not adjustment:
            movements[row.account_code] = movements.get(row.account_code, 0) + row.debit_cents - row.credit_cents
    balances: dict[str, dict[str, int]] = {"current": {}, "prior": {}}
    for line in trial_balance:
        period = balances[line.period]
        period[line.account_code] = period.get(line.account_code, 0) + line.balance_cents
    chart = list(accounts)
    return CheckInputs(
        journal_rows=len(rows),
        debit_cents=sum(r.debit_cents for r in rows),
        credit_cents=sum(r.credit_cents for r in rows),
        movements=movements,
        preparer_rows=sum(r.created_by is not None and r.created_by not in PLACEHOLDER_PREPARERS for r in rows),
        approver_rows=sum(bool(r.approved_by) for r in rows),
        tb_current=balances["current"],
        tb_prior=balances["prior"],
        account_types={a.code: a.type for a in chart},
        account_categories={a.code: a.category for a in chart},
        dataset=dataset,
    )


def source_label(version: TableVersion | None, dataset: str | None) -> str | None:
    """体检与请求文案里总账的来源名: Xero 同步与 Xero 快照载入为 Xero; 导入为原文件名; 其他数据集载入为
    journal_entries.csv (数据集的总账文件). version 为总账最新一版, 数据集载入的没有版本. 没有数据集 (新建的客户,
    需求 R288) 而总账还没接入的为 None, 前端显示 "尚未导入"."""
    if version is not None:
        if version.source == "xero":
            return "Xero"
        if version.source == "import":
            return version.file_name or "import"
    if dataset is None:
        return None
    if dataset.startswith("xero/"):
        return "Xero"
    return "journal_entries.csv"


# ── 补交请求 ──

TABLE_TITLES = {
    TableKind.accounts: "科目表",
    TableKind.trial_balance: "试算表 (本期与上期)",
    TableKind.journal_entries: "总账分录",
    TableKind.sales_invoices: "销售发票",
    TableKind.supplier_bills: "应付账单",
    TableKind.bank_accounts: "银行账户",
    TableKind.bank_transactions: "银行流水",
    TableKind.delivery_notes: "发运单",
    TableKind.approval_log: "分录审批日志",
    TableKind.employees: "工资册",
}
DOCUMENT_TITLES = {
    DocumentType.contract: "合同",
    DocumentType.invoice: "发票",
    DocumentType.confirmation: "回函",
    DocumentType.bank_statement: "银行对账单",
    DocumentType.minutes: "会议纪要",
    DocumentType.financial_statements: "财务报表",
    DocumentType.registry_extract: "ASIC 摘录",
    DocumentType.engagement_letter: "审计业务约定书",
    DocumentType.representation_letter: "管理层声明书",
    DocumentType.correspondence: "往来函件",
    DocumentType.governance_communication: "与治理层的沟通",
    DocumentType.agreement: "协议",
    DocumentType.internal_record: "内部记录",
}  # other 以外的文件类型都有标题 (模板 inputs 可写任一种, 见 test_procedure_inputs)
# 体检请求 (就绪必需的表, 审批日志) 的用途 (程序库与设计规格说明书第 10 节): 审批日志供全量分录扫描 (GEN-05) 判断
# 录入与审批. 程序要的资料的用途取模板 inputs (program_requests); inputs 不能写就绪必需的表, 两类请求的资料不重叠.
# GEN-05 不写 inputs (缺审批人时 R04 记 not_executable, 不等数据, 需求 R131), 这里它的两项保留
USED_FOR: dict[TableKind, list[str]] = {
    TableKind.accounts: ["PLN-07", "PLN-01"],
    TableKind.trial_balance: ["PLN-07", "PLN-01", "CSH-05", "CSH-02"],
    TableKind.journal_entries: ["PLN-07", "CSH-02", "GEN-05"],
    TableKind.approval_log: ["GEN-05"],
}
TABLE_FORMATS = "csv, xlsx, xls"  # 账表请求可接受的格式 (走导入)
DOCUMENT_FORMATS = "pdf, docx, xlsx, png, jpg"  # 文件请求可接受的格式 (走上传)
# 各体检项要用的表: 缺数据时 request_id 指向其中第一张缺的表的请求
CHECK_TABLES: dict[CheckId, tuple[TableKind, ...]] = {
    CheckId.debits_equal_credits: (TableKind.journal_entries,),
    CheckId.journals_agree_tb: (TableKind.journal_entries, TableKind.trial_balance, TableKind.accounts),
    CheckId.count_agrees_source: (TableKind.journal_entries,),
    CheckId.mapping_complete: (TableKind.accounts,),
    CheckId.preparer_approver_available: (TableKind.journal_entries,),
}


def request_id(subject: TableKind | DocumentType) -> str:
    """补交请求的固定 ID: REQ-{表种类或文件类型}."""
    return f"REQ-{subject.value}"


# 体检不再提出的请求 → 撤回的理由 (写进 request_withdrawn 留痕): 已有项目里仍待补交的, 下一次体检转 withdrawn
# (reconcile; DataCheckup.run_if_stale 见到它们即重跑). Issue #46: 财务报表以账套为准, 不再要报表草稿. 前提: 没有程序
# 的模板 inputs 要这些资料 (test_no_procedure_wants_a_retired_request 守住), 否则请求一直 open, 每次都重跑体检
RETIRED_REQUESTS: dict[str, str] = {request_id(DocumentType.financial_statements): "报表以账套为准"}


# 同一时间提出的请求的先后: 账表按 TableKind 顺序, 再是文件按 DocumentType 顺序 (认不出的排最后, 按 ID)
REQUEST_ORDER = [request_id(subject) for subject in [*TableKind, *DocumentType]]


def request_order(request: DataRequest) -> tuple[datetime, int, str]:
    """补交请求的排序键: 先按提出时间, 同时提出的按 REQUEST_ORDER."""
    rank = REQUEST_ORDER.index(request.id) if request.id in REQUEST_ORDER else len(REQUEST_ORDER)
    return (request.created_at, rank, request.id)


def table_request(
    kind: TableKind,
    reason: RequestReason,
    params: Mapping[str, Any],
    now: datetime,
    used_for: Sequence[str] | None = None,
) -> DataRequest:
    """账表的补交请求; used_for 缺省取 USED_FOR (体检请求)."""
    return DataRequest(
        id=request_id(kind),
        kind="table",
        table_kind=kind,
        title=TABLE_TITLES[kind],
        used_for=list(USED_FOR[kind] if used_for is None else used_for),
        reason_code=reason,
        reason_params=dict(params),
        format_hint=TABLE_FORMATS,
        created_at=now,
    )


def document_request(kind: DocumentType, reason: RequestReason, now: datetime, used_for: Sequence[str]) -> DataRequest:
    """文件的补交请求 (只有程序要的, 体检不要文件), used_for 为要它的程序."""
    return DataRequest(
        id=request_id(kind),
        kind="document",
        document_type=kind,
        title=DOCUMENT_TITLES[kind],
        used_for=list(used_for),
        reason_code=reason,
        format_hint=DOCUMENT_FORMATS,
        created_at=now,
    )


# ── 程序要的资料 (需求 R272) ──

Subject = TableKind | DocumentType  # 一项资料: 账表或文件类型


@dataclass(frozen=True)
class OnHand:
    """项目现有的资料: 有行的账表与有文件的类型 (口径同就绪判断: 账表有任意一行即算有, 含审计期间外的), 以及
    审计师标了客户无此类资料的 (需求 R280)."""

    tables: frozenset[TableKind] = frozenset()
    documents: frozenset[DocumentType] = frozenset()
    unavailable: frozenset[Subject] = frozenset()

    def has(self, subject: Subject) -> bool:
        return subject in self.tables if isinstance(subject, TableKind) else subject in self.documents


def subject_of(request: DataRequest) -> Subject | None:
    """请求要的资料: 账表种类或文件类型."""
    return request.table_kind if request.table_kind is not None else request.document_type


def on_hand(
    stats: Mapping[TableKind, TableStats], documents: Mapping[DocumentType, int], requests: Iterable[DataRequest] = ()
) -> OnHand:
    """由 DataStore.stats, DataStore.document_counts 与补交请求 (标了 unavailable 的) 算现有的资料."""
    return OnHand(
        tables=frozenset(kind for kind, item in stats.items() if item.records > 0),
        documents=frozenset(kind for kind, n in documents.items() if n > 0),
        unavailable=frozenset(
            subject for r in requests if r.status == "unavailable" and (subject := subject_of(r)) is not None
        ),
    )


def missing_inputs(inputs: ProcedureInputs, have: OnHand) -> list[Subject]:
    """模板 inputs 中现在还没有的资料, 按声明顺序 (先账表后文件)."""
    tables: list[Subject] = [kind for kind in inputs.tables if kind not in have.tables]
    return tables + [kind for kind in inputs.documents if kind not in have.documents]


# 请求 ID → 资料
SUBJECTS: dict[str, Subject] = {request_id(subject): subject for subject in [*TableKind, *DocumentType]}


def with_requested(inputs: ProcedureInputs, request_ids: Iterable[str]) -> ProcedureInputs:
    """模板 inputs 并上程序等数据时记下的请求 (Procedure.data_request_ids) 所要的资料, 已有的不重复, 认不出的请求 ID
    略去. ACC-04 等 ASIC 摘录不写模板 inputs (只有新建的公司客户要), 由主图 p0_inputs 判定后记在 data_request_ids
    (需求 R290)."""
    subjects = [SUBJECTS[rid] for rid in request_ids if rid in SUBJECTS]
    tables = [*inputs.tables, *(s for s in subjects if isinstance(s, TableKind) and s not in inputs.tables)]
    documents = [*inputs.documents, *(s for s in subjects if isinstance(s, DocumentType) and s not in inputs.documents)]
    return ProcedureInputs(tables=list(dict.fromkeys(tables)), documents=list(dict.fromkeys(documents)))


def program_requests(needs: Mapping[str, ProcedureInputs], have: OnHand, now: datetime) -> list[DataRequest]:
    """程序要的补交请求: needs 为还没执行完的程序 → 其 inputs; 现在还没有的资料每项一个请求 (标了客户无此类资料的
    不算), used_for 为要它的程序 (按编号). 账表的原因为 missing_table, 文件为 not_provided. 按 REQUEST_ORDER 排."""
    users: dict[Subject, list[str]] = {}
    for procedure_id in sorted(needs):
        for subject in missing_inputs(needs[procedure_id], have):
            if subject not in have.unavailable:
                users.setdefault(subject, []).append(procedure_id)
    requests = [
        table_request(subject, "missing_table", {}, now, used_for)
        if isinstance(subject, TableKind)
        else document_request(subject, "not_provided", now, used_for)
        for subject, used_for in users.items()
    ]
    return sorted(requests, key=request_order)


def merge_requests(*groups: Iterable[DataRequest]) -> list[DataRequest]:
    """几组请求合成一组: 同 ID 的取先出现的, used_for 取并集 (保持先后). 按 REQUEST_ORDER 排."""
    merged: dict[str, DataRequest] = {}
    for request in (r for group in groups for r in group):
        first = merged.get(request.id)
        if first is None:
            merged[request.id] = request
        else:
            used_for = list(dict.fromkeys([*first.used_for, *request.used_for]))
            merged[request.id] = first.model_copy(update={"used_for": used_for})
    return sorted(merged.values(), key=request_order)


# ── 体检 ──


@dataclass(frozen=True)
class Checkup:
    """一次体检的结论: 各项结果, 与这次认为需要补交的请求 (status open, created_at 为体检时间)."""

    checks: list[DataCheck]
    requests: list[DataRequest] = field(default_factory=list)


def opening_of(inputs: CheckInputs) -> dict[str, int]:
    """各科目期初余额 (见 journals_differences)."""
    try:
        return opening_balances(inputs.tb_prior, inputs.account_types, inputs.account_categories)
    except ValueError:
        return {c: v for c, v in inputs.tb_prior.items() if inputs.account_types.get(c) not in PROFIT_AND_LOSS}


def compared_accounts(inputs: CheckInputs, opening: Mapping[str, int]) -> list[str]:
    """勾稽比较的科目: 期间内有发生额, 在两期试算表中, 或有期初余额 (含两期试算表与分录都没有, 却承接了上期损益的
    留存收益科目) 的, 按代码排序."""
    return sorted(set(inputs.movements) | set(inputs.tb_current) | set(inputs.tb_prior) | set(opening))


def journals_differences(inputs: CheckInputs) -> list[dict[str, Any]]:
    """期间内各科目分录发生额与试算表变动 (本期余额 - 期初余额) 的差异, 按科目代码, 只列不为 0 的.
    期初余额见 services.ledger.opening_balances (损益类归零, 上期损益转入留存收益); 留存收益科目不唯一或没有时
    不结转, 差异会落在留存收益上. 比较的科目见 compared_accounts."""
    opening = opening_of(inputs)
    codes = compared_accounts(inputs, opening)
    differences = []
    for code in codes:
        change = inputs.tb_current.get(code, 0) - opening.get(code, 0)
        difference = inputs.movements.get(code, 0) - change
        if difference:
            differences.append({"account_code": code, "difference_cents": difference})
    return differences


def run_checks(inputs: CheckInputs, *, journal_version: TableVersion | None, now: datetime) -> Checkup:
    """五项体检与补交请求.

    - debits_equal_credits: 全部分录借贷合计相等; 没有分录为 missing_data.
    - journals_agree_tb: 期间内各科目发生额与试算表变动一致 (journals_differences); 缺总账, 试算表任一期或科目表为
      missing_data.
    - count_agrees_source: 总账行数与来源交付的行数 (最新一版记下的 records; 数据集载入以表中行数为准) 一致.
    - mapping_complete: 科目都已归入标准分类; 没有科目为 missing_data.
    - preparer_approver_available: 分录里有录入人与审批人 (至少一行有真实值); 缺的字段记 missing_fields, 不通过时
      请求审批日志 (field_unavailable, {fields, source}).
    就绪必需的表缺失时各请求一份 (missing_table). 缺数据的项 request_id 指向它要用的表中第一张缺的表的请求.
    财务报表以账套为准 (Issue #46), 不查试算表与报表草稿, 也不要报表草稿.
    """
    missing = missing_in(inputs)
    label = source_label(journal_version, inputs.dataset)
    requests = [table_request(kind, "missing_table", {}, now) for kind in missing]

    def check(cid: CheckId, status: CheckStatus, params: Mapping[str, Any], request: str | None = None) -> DataCheck:
        if request is None and status == "missing_data":
            request = next((request_id(k) for k in CHECK_TABLES[cid] if k in missing), None)
        return DataCheck(id=cid, status=status, params=dict(params), request_id=request)

    has_journals = inputs.journal_rows > 0
    checks = []
    balanced = inputs.debit_cents == inputs.credit_cents
    checks.append(
        check(
            CheckId.debits_equal_credits,
            ("passed" if balanced else "failed") if has_journals else "missing_data",
            {"debit_cents": inputs.debit_cents, "credit_cents": inputs.credit_cents},
        )
    )
    if has_journals and not missing:
        differences = journals_differences(inputs)
        accounts = len(compared_accounts(inputs, opening_of(inputs)))
        status: CheckStatus = "failed" if differences else "passed"
        checks.append(check(CheckId.journals_agree_tb, status, {"accounts": accounts, "differences": differences}))
    else:
        checks.append(check(CheckId.journals_agree_tb, "missing_data", {"accounts": 0, "differences": []}))
    expected = journal_version.records if journal_version is not None else inputs.journal_rows
    checks.append(
        check(
            CheckId.count_agrees_source,
            ("passed" if expected == inputs.journal_rows else "failed") if has_journals else "missing_data",
            {"source": label, "expected": expected if has_journals else 0, "received": inputs.journal_rows},
        )
    )
    total = len(inputs.account_types)
    mapped = sum(inputs.account_categories.get(code) is not None for code in inputs.account_types)
    checks.append(
        check(
            CheckId.mapping_complete,
            ("passed" if mapped == total else "failed") if total else "missing_data",
            {"mapped": mapped, "total": total},
        )
    )
    absent = [
        name for name, n in (("created_by", inputs.preparer_rows), ("approved_by", inputs.approver_rows)) if not n
    ]
    if not has_journals:
        checks.append(
            check(CheckId.preparer_approver_available, "missing_data", {"source": label, "missing_fields": []})
        )
    elif absent:
        approval = table_request(TableKind.approval_log, "field_unavailable", {"fields": absent, "source": label}, now)
        requests.append(approval)
        params = {"source": label, "missing_fields": absent}
        checks.append(check(CheckId.preparer_approver_available, "failed", params, approval.id))
    else:
        checks.append(check(CheckId.preparer_approver_available, "passed", {"source": label, "missing_fields": []}))
    return Checkup(checks, requests)


@dataclass(frozen=True)
class RequestChanges:
    """补交请求的变化: saved 为要写入的 (新提出, 重新打开, 参数变了, 转 received 或 withdrawn 的), raised, received
    与 withdrawn 为要留痕的."""

    saved: list[DataRequest] = field(default_factory=list)
    raised: list[DataRequest] = field(default_factory=list)
    received: list[DataRequest] = field(default_factory=list)
    withdrawn: list[DataRequest] = field(default_factory=list)


def reconcile(
    existing: Sequence[DataRequest], wanted: Sequence[DataRequest], now: datetime, have: OnHand | None = None
) -> RequestChanges:
    """对照已有请求与这次体检需要的请求: 没有的新提出; 已收到 (或撤回) 的重新打开 (created_at 为 now, resolved_at
    清空); 标了客户无此类资料的是定论, 不重开也不改; 仍待补交的保留原提出时间, 只更新原因与参数等; 待补交而这次不再
    需要的转 received (资料已补齐), 但程序要的资料现在仍没有 (have 中没有, 要它的程序都不再执行, 需求 R280) 时转
    withdrawn. 体检请求的资料 (USED_FOR 的) 不再需要即算收到 (如审批人随总账一并补上); have 为空时一律按收到.
    体检不再提出的请求 (RETIRED_REQUESTS) 一律转 withdrawn, 不论资料到没到."""
    changes = RequestChanges()
    current = {r.id: r for r in existing}
    for request in wanted:
        old = current.get(request.id)
        if old is not None and old.status == "unavailable":
            continue
        if old is None or old.status != "open":
            changes.saved.append(request)
            changes.raised.append(request)
        elif old != request.model_copy(update={"created_at": old.created_at}):
            changes.saved.append(request.model_copy(update={"created_at": old.created_at}))
    needed = {r.id for r in wanted}
    for old in existing:
        if old.status == "open" and old.id not in needed:
            subject = subject_of(old)
            missing = have is not None and subject is not None and subject not in USED_FOR and not have.has(subject)
            gone = missing or old.id in RETIRED_REQUESTS
            resolved = old.model_copy(update={"status": "withdrawn" if gone else "received", "resolved_at": now})
            changes.saved.append(resolved)
            (changes.withdrawn if gone else changes.received).append(resolved)
    return changes
