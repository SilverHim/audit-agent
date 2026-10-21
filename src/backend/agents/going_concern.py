"""A6 的持续经营段 (功能 going_concern): GEN-06 期后事项研判的判断对象与上下文 (需求 R179; GEN-04, FIN-02 由需求
R182, R185 接入). FieldworkAgent 登记取对象 (subject_sources), 之后走判断公共流程 (agents/judging.py, 含 A7 复核).

GEN-06 的判断对象是前置程序 GEN-07 尚未归因的例外所指的行 (银行流水或分录行). 上下文按设计稿第 6.4 节的通用扩展取:
参考号拆成含数字的词, 对方名称按 R11 规范化 (services/party_names), 在总账行的单据号与摘要中整词匹配; 找到的凭证再
带出相关的销售发票与已登记文件. 只用对象自己的参考号与名称, 不写任何公司的科目或单据号写法.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from backend.agents.base import AgentDeps, RunContext, run_rules
from backend.agents.journals import entry_order, journal_rows
from backend.agents.judging import Subject, Subjects, in_scope, open_findings
from backend.contracts.models import AuditException
from backend.contracts.records import BankTransaction, JournalEntry, SalesInvoice
from backend.services.party_names import folded, party_name_key, separated

# 关联对象含给定 ID 的已登记文件: (项目, 关联对象 ID) -> [{document, pages}] (FieldworkAgent.related_documents)
DocumentsOf = Callable[[str, str], Awaitable[list[dict[str, Any]]]]
SubjectType = Literal["bank_transaction", "journal_entry"]  # GEN-07 例外所指的两种行
BANK: SubjectType = "bank_transaction"
JOURNAL: SubjectType = "journal_entry"


def text_words(text: str | None) -> list[str]:
    """原文按非字母数字切分后的词, 顺序不变 (先经 party_names 的第 1 步: 去附加符, 只转 ASCII 字母为小写); 为空
    时为 []."""
    return separated(folded(text)).split() if text else []


def reference_tokens(text: str | None, min_length: int) -> list[str]:
    """参考号 token: text_words 中长度不小于 min_length 且含数字的词, 去重后按出现顺序. 例 (min_length 4):
    "70135 70209" → [70135, 70209], "Sublet RM2031" → [rm2031], "MKT-08123" → [08123], "DEPOSIT ADV" → []."""
    found: list[str] = []
    for word in text_words(text):
        if len(word) >= min_length and any(c.isdigit() for c in word) and word not in found:
            found.append(word)
    return found


def mentions(text_key: str | None, key: str | None) -> bool:
    """key 是否在 text_key 中整词出现 (两边都是空格分隔的词, 同 GEN-03 C01 的 LIKE 写法); 任一为空时为假."""
    return key is not None and text_key is not None and f" {key} " in f" {text_key} "


@dataclass(frozen=True)
class MatchKeys:
    """一个判断对象找总账凭证用的键: 参考号 token 与规范化后的对方名称 (分录对象不按名称找, 为 None)."""

    tokens: list[str]
    name: str | None

    def hits(self, line: Mapping[str, Any], prefixes: Sequence[str]) -> bool:
        """总账行 (E01 的一行) 是否命中: source_doc 或 description 的词中有某个 token, 或这两列的规范化文本 (去说明
        前缀, 不去公司后缀, 同 GEN-03 C01 的总账一侧) 整词含名称."""
        texts = (line.get("source_doc"), line.get("description"))
        if any(word in self.tokens for text in texts for word in text_words(text)):
            return True
        return any(mentions(party_name_key(text, prefixes, ()), self.name) for text in texts)


def subsequent_window(params: Mapping[str, Any]) -> tuple[date, date]:
    """期后窗口 (期末, 上限] 的起止日: 起日为期末次日, 上限为 min(期末 + subsequent_days, data_as_of), data_as_of 为空时
    不截; 同 GEN-07 与 GEN-06 规则 SQL 的 bounds 段. 参数快照经 Store 读回时日期为字符串, 两种都收."""
    period_end = date.fromisoformat(str(params["period_end"]))
    end = period_end + timedelta(days=int(params["subsequent_days"]))
    as_of = params.get("data_as_of")
    if as_of is not None:
        end = min(end, date.fromisoformat(str(as_of)))
    return period_end + timedelta(days=1), end


def subject_type(exception: AuditException) -> SubjectType:
    """例外所指的行类型: 主证据指针 (source_id 等于 source_ids[0] 的第一个指针) 的 source_type; 不是银行流水或分录行
    时抛 ValueError."""
    subject = exception.source_ids[0]
    found = next((p.source_type for p in exception.evidence if p.source_id == subject), None)
    if found in (BANK, JOURNAL):
        return found
    raise ValueError(f"{exception.id} 所指的 {subject} 不是银行流水或分录行 ({found})")


@dataclass(frozen=True)
class Located:
    """一个判断对象的行与匹配结果."""

    row: BankTransaction | JournalEntry  # 例外所指的行
    voucher: list[JournalEntry]  # 分录对象为本凭证的全部行 (按行 ID), 流水对象为 []
    keys: MatchKeys
    related: list[str]  # 命中的凭证号 (按凭证号排序), 不含本凭证; E01 已剔除作废对


async def locate(
    deps: AgentDeps, eid: str, finding: AuditException, lines: Sequence[Mapping[str, Any]], params: Mapping[str, Any]
) -> Located:
    """对象行与它在 E01 中命中的凭证. 流水对象: token 取 reference, 名称取 description (收款人, 经 party_name_key 去
    说明前缀与公司后缀); 分录对象: token 取本行 source_doc, 不按名称找. 对象行或本凭证查不到抛 LookupError."""
    store = deps.financial_store
    prefixes = list(params.get("counterparty_prefixes") or [])
    min_length = int(params["reference_token_min_length"])
    source_type, subject_id = subject_type(finding), finding.source_ids[0]
    record = await store.record(eid, source_type, subject_id)
    if record is None:
        raise LookupError(f"{finding.id} 所指的 {subject_id} 不在财务数据中")
    if source_type == BANK:
        txn = BankTransaction.model_validate(record)
        name = party_name_key(txn.description, prefixes, list(params.get("entity_suffixes") or []))
        keys = MatchKeys(reference_tokens(txn.reference, min_length), name)
        return Located(txn, [], keys, sorted({str(x["entry_no"]) for x in lines if keys.hits(x, prefixes)}))
    entry = JournalEntry.model_validate(record)
    voucher = await store.journal_entries(eid, entry_nos=[entry.entry_no])
    if not voucher:
        raise LookupError(f"凭证 {entry.entry_no} 不在财务数据中")
    keys = MatchKeys(reference_tokens(entry.source_doc, min_length), None)
    related = {str(x["entry_no"]) for x in lines if keys.hits(x, prefixes)} - {entry.entry_no}
    return Located(entry, voucher, keys, sorted(related))


def related_invoices(
    found: Located,
    lines: Sequence[JournalEntry],
    invoices: Sequence[SalesInvoice],
    params: Mapping[str, Any],
) -> list[SalesInvoice]:
    """相关发票 (E02 的顺序). 流水对象: journal_entry_id 在相关凭证行 (lines) 中的发票, 加规范化客户名等于对方名称的
    本期发票; 分录对象: 规范化客户名在本凭证行 description 或 source_doc 的规范化文本中整词出现的本期发票. 本期为
    入账日不晚于期末; 客户名经 party_name_key 去说明前缀与公司后缀, 总账一侧只去说明前缀 (同 MatchKeys.hits)."""
    prefixes = list(params.get("counterparty_prefixes") or [])
    suffixes = list(params.get("entity_suffixes") or [])
    period_end = date.fromisoformat(str(params["period_end"]))

    def customer(invoice: SalesInvoice) -> str | None:
        return party_name_key(invoice.customer, prefixes, suffixes)

    if isinstance(found.row, BankTransaction):
        ids = {line.id for line in lines}
        name = found.keys.name
        return [
            i
            for i in invoices
            if i.journal_entry_id in ids or (name is not None and i.posting_date <= period_end and customer(i) == name)
        ]
    texts = [party_name_key(t, prefixes, ()) for line in found.voucher for t in (line.description, line.source_doc)]
    return [i for i in invoices if i.posting_date <= period_end and any(mentions(t, customer(i)) for t in texts)]


async def documents_by_id(eid: str, related_ids: Sequence[str], documents_of: DocumentsOf) -> list[dict[str, Any]]:
    """对每个关联对象 ID 取已登记文件 (documents_of, 即 FieldworkAgent.related_documents: 不给登记摘录, 带各页文字),
    按文件 ID 去重并排序."""
    found: dict[str, dict[str, Any]] = {}
    for related_id in dict.fromkeys(related_ids):
        for item in await documents_of(eid, related_id):
            found.setdefault(str(item["document"]["id"]), item)
    return [found[i] for i in sorted(found)]


async def gen06_subjects(deps: AgentDeps, ctx: RunContext, documents_of: DocumentsOf) -> Subjects:
    """GEN-06 的判断对象: 前置程序 (GEN-07) 尚未归因的例外 (只看当前结果, services.staleness) 按 source_ids[0] 分组,
    每行一个对象, 按对象 ID 排序; 先按 in_scope 筛选, 再经规则引擎取 E01, E02 的行并组装 data. 规则例外为该对象上的
    这些例外 (按 ID), fixture_key 为对象 ID. 没有对象时不调规则引擎.

    data 字段按 GEN-06.md.j2 头注释 (均为 JSON 形式, 行带 source_type):
    - subject: 例外所指的行; voucher_lines: 分录对象为本凭证全部行 (按行 ID), 流水对象为 [].
    - related_entries: 命中的凭证 (locate) 的全部行, 按 (入账日, 凭证号自然序, 行 ID).
    - related_invoices: 相关发票 (related_invoices) 各带 delivery_notes (按 ID).
    - documents: 对 {对象 ID, 本凭证号, 相关凭证号, 相关发票 ID} 逐个取已登记文件 (documents_by_id).
    - trial_balance: 对象行 (流水为其银行账户的总账科目), 本凭证与相关凭证涉及的科目, 按代码: 科目名称与分类 (科目表
      里没有为 null), 本期余额 (试算表没有该科目为 null).
    - window: 期后窗口 {from, to} (subsequent_window); match_keys: {tokens, name}.
    对象行, 本凭证或相关凭证查不到抛 LookupError; 例外所指的不是流水或分录行抛 ValueError.
    """
    groups: dict[str, list[AuditException]] = {}
    for x in open_findings(ctx.state, ctx.template.depends_on):
        if in_scope(ctx, x.source_ids[0]):
            groups.setdefault(x.source_ids[0], []).append(x)
    if not groups:
        return Subjects()
    eid, store = ctx.engagement.id, deps.financial_store
    params = ctx.run.params_snapshot
    rules = await run_rules(deps, ctx)
    candidates = rules.rows.get("E01", [])
    invoices = [SalesInvoice.model_validate(r) for r in rules.rows.get("E02", [])]
    found = {s: await locate(deps, eid, groups[s][0], candidates, params) for s in sorted(groups)}

    wanted = sorted({no for f in found.values() for no in f.related})
    entries: dict[str, list[JournalEntry]] = {no: [] for no in wanted}
    for line in await store.journal_entries(eid, entry_nos=wanted) if wanted else []:
        entries[line.entry_no].append(line)
    missing = [no for no in wanted if not entries[no]]
    if missing:
        raise LookupError(f"凭证不在财务数据中: {', '.join(missing)}")
    related = {
        s: sorted(
            (line for no in f.related for line in entries[no]),
            key=lambda x: (x.posting_date, entry_order(x.entry_no), x.id),
        )
        for s, f in found.items()
    }
    invoiced = {s: related_invoices(f, related[s], invoices, params) for s, f in found.items()}
    invoice_ids = sorted({i.id for chosen in invoiced.values() for i in chosen})
    notes: dict[str, list[dict[str, Any]]] = {i: [] for i in invoice_ids}
    for note in sorted(await store.delivery_notes(eid, invoice_ids) if invoice_ids else [], key=lambda n: n.id):
        notes[note.invoice_id].append({"source_type": "delivery_note", **note.model_dump(mode="json")})
    banks = (
        {a.id: a.gl_account_code for a in await store.bank_accounts(eid)}
        if any(isinstance(f.row, BankTransaction) for f in found.values())
        else {}
    )
    codes = {
        s: sorted(
            {
                *([banks[f.row.bank_account_id]] if isinstance(f.row, BankTransaction) else []),
                *(line.account_code for line in [*f.voucher, *related[s]]),
            }
        )
        for s, f in found.items()
    }
    chart = {a.code: a for a in await store.accounts(eid, sorted({c for cs in codes.values() for c in cs}))}
    current = {r.account_code: r.balance_cents for r in await store.trial_balance(eid, "current")}
    start, end = subsequent_window(params)

    subjects = []
    for subject_id, f in found.items():
        own = [f.row.entry_no] if isinstance(f.row, JournalEntry) else []
        lookups = [subject_id, *own, *f.related, *(i.id for i in invoiced[subject_id])]
        data: dict[str, Any] = {
            "subject": {
                "source_type": BANK if isinstance(f.row, BankTransaction) else JOURNAL,
                **f.row.model_dump(mode="json"),
            },
            "voucher_lines": journal_rows(f.voucher),
            "related_entries": journal_rows(related[subject_id]),
            "related_invoices": [
                {
                    "invoice": {"source_type": "sales_invoice", **i.model_dump(mode="json")},
                    "delivery_notes": notes[i.id],
                }
                for i in invoiced[subject_id]
            ],
            "documents": await documents_by_id(eid, lookups, documents_of),
            "trial_balance": [
                {
                    "source_type": "trial_balance",
                    "account_code": code,
                    "account_name": chart[code].name if code in chart else None,
                    "account_category": chart[code].category if code in chart else None,
                    "balance_cents": current.get(code),
                }
                for code in codes[subject_id]
            ],
            "window": {"from": start.isoformat(), "to": end.isoformat()},
            "match_keys": {"tokens": f.keys.tokens, "name": f.keys.name},
        }
        subjects.append(Subject(subject_id, data, groups[subject_id]))
    return Subjects(subjects, rules.outcomes)
