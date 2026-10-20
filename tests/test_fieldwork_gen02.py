"""需求 R134, R135: A6 GEN-02 分录例外归因. GEN-05 的例外取 Xero 演示的预置 (XERO_RULES, 7 条, 连库一致见
tests/test_gen05_sql.py), 经 FieldworkAgent 并入状态后执行 GEN-02. R134 (判断对象与上下文) 的判断引擎为按凭证预置
结果的 FakeJudgmentEngine, 渲染提示词用真实判断引擎 (同 JudgmentEngine.judge 的变量); R135 (7 份 fixture) 用真实
判断引擎读 Xero 的 fixture 正本 (tests/fixtures/llm_xero/GEN-02). 设计稿第 6.3 节."""

import asyncio
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import Any

import pytest

from backend.adapters.llm import MockLLMClient
from backend.agents.base import RunContext
from backend.agents.fieldwork import FieldworkAgent, entry_order
from backend.contracts.models import (
    Decision,
    ExceptionDisposition,
    Judgment,
    ProposedAction,
    ReviewStatus,
    RunStatus,
    StepStatus,
    TargetType,
    TaskKind,
    TrailEntry,
    TrailKind,
)
from backend.contracts.services import JudgmentContext
from backend.contracts.state import EngagementState, StatePatch
from backend.orchestration.nodes import apply_patch
from tests.fakes import (
    FakeJudgmentEngine,
    FakeRegistry,
    FakeRuleEngine,
    FakeTrail,
    make_ctx,
    make_deps,
    make_run,
    numbat_gen05_result,
)
from tests.fixture_sets import XERO
from tests.test_fieldwork_judging import failed, passed, pending_task
from tests.test_xero_fixtures import LIBRARY, RecordingJudgments, xero_context, xero_state
from tests.xero_fakes import XERO_RULES, XERO_SAME_AMOUNT_INVOICES, make_xero_deps, supplement

VOUCHERS = ["XJ-397", "XJ-405", "XJ-430", "XJ-449", "XJ-499", "XJ-575", "XJ-601"]  # 凭证号自然序
FINDINGS = ["R09-1", "R12-1", "R10-1", "R10-2", "R09-2", "R11-1", "R11-2"]  # 各凭证的 GEN-05 例外, 依次
ORC1003 = "SI-4fad1af2-b871-4ac5-a15a-3c5e32d2e2c4"
ORC1008 = "SI-1d1ba340-afa2-4f4c-8ff7-a147bda9a47b"


async def gen05_state() -> EngagementState:
    """Xero 项目执行过 GEN-05 (7 条例外并入状态) 之后的状态."""
    deps = make_xero_deps(rules=FakeRuleEngine(XERO_RULES))
    patch = await FieldworkAgent(deps).run(xero_context(deps, xero_state(), "GEN-05", []))
    return apply_patch(xero_state(), patch)


async def run_gen02(
    outcomes: dict[str, Any], state: EngagementState | None = None, rerun: list[str] | None = None
) -> tuple[StatePatch, RunContext, FakeJudgmentEngine]:
    """在 GEN-05 之后的 Xero 状态上执行一次 GEN-02 (固定集, objective_ids 为空); outcomes 为各凭证的预置结果."""
    engine = FakeJudgmentEngine(outcomes)
    deps = replace(make_xero_deps(), judgments=engine)  # type: ignore[arg-type]
    ctx = xero_context(deps, state if state is not None else await gen05_state(), "GEN-02", [])
    ctx = replace(ctx, procedure=ctx.procedure.model_copy(update={"rerun_subject_ids": rerun or []}))
    return await FieldworkAgent(deps).run(ctx), ctx, engine


@dataclass(frozen=True)
class Flow:
    patch: StatePatch  # GEN-02 的状态片段
    ctx: RunContext  # GEN-02 这次执行的上下文
    contexts: dict[str, JudgmentContext]  # 送给判断引擎的上下文, 按凭证号


@pytest.fixture(scope="module")
def flow() -> Flow:
    """7 笔凭证都判断成功 (accept, L2) 的一次 GEN-02."""
    patch, ctx, engine = asyncio.run(run_gen02({no: passed() for no in VOUCHERS}))
    return Flow(patch, ctx, {c.subject_id: c for _, c, _ in engine.calls})


def ids(rows: list[dict[str, Any]], key: str = "id") -> list[Any]:
    return [r[key] for r in rows]


@pytest.mark.external
def test_subjects_are_vouchers_in_natural_order(flow: Flow) -> None:
    """验收 2, 6: 7 笔凭证按自然序依次送判断引擎, fixture_key 为凭证号, 规则例外为该凭证上的 GEN-05 例外; 任务说明的
    risks 为空 (固定集没有审计目标), 参数只有 bank_window_days; 舞弊风险放在 data.fraud_risks."""
    assert list(flow.contexts) == VOUCHERS
    for no, finding in zip(VOUCHERS, FINDINGS, strict=True):
        jctx = flow.contexts[no]
        assert (jctx.fixture_key, [x.id for x in jctx.rule_findings]) == (no, [f"X-GEN-05-1-{finding}"])
        assert jctx.task == {"params": {"bank_window_days": 3}, "risks": []}
        assert ids(jctx.data["fraud_risks"]) == ["RISK-REV-OCC", "RISK-REV-CUT"]


@pytest.mark.external
def test_related_entries_invoices_and_findings(flow: Flow) -> None:
    """验收 3, 4: detail 所指的凭证与发票, 以及 detail 指向本凭证的其他例外. XJ-405 (R12) 带配对发票的凭证 XJ-408 与
    全额贷项 XJ-430, 两张发票各带服务完成记录与 CON-X02 (及其文件登记); XJ-430 被 R12-1 指向, 发票为 R10-1 的 5 张;
    R11 的两笔带原凭证与冲回凭证."""
    data = {no: flow.contexts[no].data for no in VOUCHERS}
    entries = {no: ids(d["related_entries"]) for no, d in data.items()}
    assert entries["XJ-405"] == [f"XJ-{no}-{n}" for no in (408, 430) for n in (1, 2, 3)]
    assert entries["XJ-430"] == [f"XJ-405-{n}" for n in (1, 2, 3)]
    assert entries["XJ-575"] == ["XJ-560-1", "XJ-560-2", "XJ-567-1", "XJ-567-2"]
    assert entries["XJ-601"] == ["XJ-599-1", "XJ-599-2", "XJ-602-1", "XJ-602-2"]
    assert {r["source_type"] for d in data.values() for r in [*d["entry_lines"], *d["related_entries"]]} == {
        "journal_entry"
    }
    invoices = {no: [i["invoice"]["id"] for i in d["related_invoices"]] for no, d in data.items()}
    assert invoices["XJ-405"] == sorted([ORC1003, ORC1008])  # 按发票 ID
    assert invoices["XJ-430"] == sorted(XERO_SAME_AMOUNT_INVOICES)
    pair = {i["invoice"]["invoice_no"]: i for i in data["XJ-405"]["related_invoices"]}
    assert [n["id"] for n in pair["ORC1003"]["delivery_notes"]] == []
    assert [n["id"] for n in pair["ORC1008"]["delivery_notes"]] == ["SR-ORC1008"]
    assert {i["invoice"]["contract_id"] for i in pair.values()} == {"CON-X02"}
    contracts = {no: [(c["contract"]["id"], c["document"]["id"]) for c in d["contracts"]] for no, d in data.items()}
    with_contracts = {no: c for no, c in contracts.items() if c}
    # 同一份合同只送一次 (按文件 ID 去重), 发票以 contract_id 引用
    assert with_contracts == {no: [("CON-X02", "DOC-02c5c90d4ffb")] for no in ("XJ-405", "XJ-430", "XJ-449")}
    findings = {no: ids(d["related_findings"]) for no, d in data.items() if d["related_findings"]}
    assert findings == {"XJ-430": ["X-GEN-05-1-R12-1"]}


@pytest.mark.external
def test_bank_balances_and_documents(flow: Flow) -> None:
    """验收 3, 4: 涉及货币资金科目的凭证带银行账户与凭证日前后 3 天 (含两端) 的对账单行加本期第一行, 不涉及的为
    null; 科目余额带科目名称, 分类与两期试算表 (上期没有为 null); 文件为关联对象含本凭证号的登记, 带各页文字.
    XJ-397 (07-16) 的窗口到 07-19 为止, 恰含 BT-090-0002 (07-19); BT-091-0001 是 091 本期第一行."""
    data = {no: flow.contexts[no].data for no in VOUCHERS}
    assert [no for no, d in data.items() if d["bank"] is not None] == ["XJ-397", "XJ-575", "XJ-601"]
    lines = {
        no: {r["id"]: r["amount_cents"] for r in data[no]["bank"]["lines"]} for no in ("XJ-397", "XJ-575", "XJ-601")
    }
    assert list(lines["XJ-397"]) == ["BT-090-0001", "BT-090-0002", "BT-091-0001"]
    assert lines["XJ-575"]["BT-090-0052"] == -88980 and "BT-090-0001" in lines["XJ-575"]
    assert (lines["XJ-601"]["BT-090-0065"], lines["XJ-601"]["BT-091-0001"]) == (350000, -350000)
    accounts = data["XJ-601"]["bank"]["accounts"]
    assert [(a["id"], a["bank_name"]) for a in accounts] == [
        (f"BA-{gl}", "Wattlebrook Bank Limited") for gl in ("090", "091")
    ]

    def balances(no: str) -> dict[str, tuple[str | None, str | None, int | None, int | None]]:
        rows = data[no]["account_balances"]
        return {b["code"]: (b["name"], b["category"], b["current_cents"], b["prior_cents"]) for b in rows}

    assert balances("XJ-397") == {
        "090": ("Business Bank Account", "cash", -1090044, -870320),
        "091": ("Business Savings Account", "cash", 1158123, None),
        "840": ("Historical Adjustment", "other_current_liabilities", -1921221, None),
    }
    assert balances("XJ-499") == {
        "710": ("Office Equipment", "property_plant_equipment", 75000, None),
        "711": ("Less Accumulated Depreciation on Office Equipment", "property_plant_equipment", -82500, None),
        "820": ("GST", "tax_payable", -119197, 79120),
    }
    documents = {
        no: sorted((d["document"]["id"], d["document"]["file_name"], len(d["pages"])) for d in data[no]["documents"])
        for no in VOUCHERS
    }
    assert {no: d for no, d in documents.items() if d} == {
        "XJ-397": [
            ("DOC-1f4bc9aec6b3", "director_loan_agreement.pdf", 2),
            ("DOC-3738f404ca43", "director_loan_confirmation.pdf", 1),
        ],
        "XJ-499": [
            ("DOC-4873d193e29f", "fixed_asset_register.pdf", 1),
            ("DOC-4fa0cdebd609", "accounting_policies_extract.pdf", 1),
        ],
    }
    assert all(page.strip() for no in ("XJ-397", "XJ-499") for d in data[no]["documents"] for page in d["pages"])


@pytest.mark.external
def test_each_voucher_gets_a_judgment_and_a_review_task(flow: Flow) -> None:
    """验收 6: 7 条判断 (L2, 待复核) 各解释本凭证的例外, 7 条 normal 任务以判断 v1 为目标; 例外不升版; 执行成功."""
    patch = flow.patch
    assert [(j.subject_id, j.exception_ids, j.review_status) for j in patch["judgments"]] == [
        (no, [f"X-GEN-05-1-{finding}"], ReviewStatus.pending) for no, finding in zip(VOUCHERS, FINDINGS, strict=True)
    ]
    assert [(t.id, t.kind) for t in patch["review_tasks"]] == [
        (f"T-J-GEN-02-1-{no}-v1", TaskKind.normal) for no in VOUCHERS
    ]
    assert patch["exceptions"] == []
    [run] = patch["procedure_runs"]
    assert (run.id, run.status) == ("RUN-GEN-02-1", RunStatus.succeeded)


@pytest.mark.external
async def test_failed_voucher_leaves_its_finding_unattributed() -> None:
    """验收 7: XJ-499 判断失败 (unattributed): R09-2 出 v2 unattributed, 以它为目标建带对象的任务, 可 override, 认定
    错报或重跑 (需求 R347); 其余 6 笔照常, 执行成功."""
    outcomes = {no: passed() for no in VOUCHERS} | {"XJ-499": failed("unattributed")}
    patch, _, _ = await run_gen02(outcomes)
    [marked] = patch["exceptions"]
    assert (marked.id, marked.version, marked.disposition) == ("X-GEN-05-1-R09-2", 2, ExceptionDisposition.unattributed)
    task = next(t for t in patch["review_tasks"] if t.kind is TaskKind.unattributed)
    assert (task.id, task.subject_id, task.target_type) == (
        "T-X-GEN-05-1-R09-2-v2",
        "XJ-499",
        TargetType.AuditException,
    )
    assert make_xero_deps().queue.allowed(task) == {Decision.override, Decision.misstatement, Decision.rerun}
    assert [j.subject_id for j in patch["judgments"]] == [no for no in VOUCHERS if no != "XJ-499"]
    assert patch["procedure_runs"][0].status is RunStatus.succeeded


@pytest.mark.external
async def test_rerun_and_vouchers_awaiting_review_are_filtered() -> None:
    """验收 2: rerun_subject_ids 只留其中的凭证, 已交人处理 (待决的 unattributed 任务) 的凭证不再判断."""
    state = await gen05_state()
    waiting = pending_task(  # 上一轮 XJ-575 判断失败留下的任务, 仍待决
        id="T-X-GEN-05-1-R11-1-v2",
        procedure_id="GEN-02",
        target_id="X-GEN-05-1-R11-1",
        run_id="RUN-GEN-02-1",
        subject_id="XJ-575",
    )
    state = state | {"review_tasks": [waiting]}
    _, _, engine = await run_gen02({no: passed() for no in VOUCHERS}, state, rerun=["XJ-499", "XJ-575"])
    assert [c.subject_id for _, c, _ in engine.calls] == ["XJ-499"]


@pytest.mark.external
async def test_attributed_findings_are_context_but_not_subjects() -> None:
    """验收 2, 3 (审核 R134-01): R09-1 (XJ-397) 与 R12-1 (XJ-405) 已归因 (v2 attributed) 时, 这两笔凭证不再是判断对象;
    XJ-430 的 related_findings 仍列已归因的 R12-1, related_entries 仍含 XJ-405 的行 (只作上下文)."""
    state = await gen05_state()
    attributed = {"X-GEN-05-1-R09-1", "X-GEN-05-1-R12-1"}
    exceptions = [
        x.model_copy(update={"version": x.version + 1, "disposition": ExceptionDisposition.attributed})
        if x.id in attributed
        else x
        for x in state["exceptions"]
    ]
    _, _, engine = await run_gen02({no: passed() for no in VOUCHERS}, state | {"exceptions": exceptions})
    contexts = {c.subject_id: c for _, c, _ in engine.calls}
    assert list(contexts) == ["XJ-430", "XJ-449", "XJ-499", "XJ-575", "XJ-601"]
    data = contexts["XJ-430"].data
    assert [x["id"] for x in data["related_findings"]] == ["X-GEN-05-1-R12-1"]
    assert {r["entry_no"] for r in data["related_entries"]} == {"XJ-405"}


@pytest.mark.parametrize(
    ("entries", "expected"),
    [
        (["XJ-100", "XJ-10", "XJ-9"], ["XJ-9", "XJ-10", "XJ-100"]),  # 数字段按数值, 不按字典序
        (["XJ-10", "XJ-9A", "XJ-9"], ["XJ-9", "XJ-9A", "XJ-10"]),  # 同一数字段后还有字符的排在后
        (["XJ-9", "XJ-09"], ["XJ-09", "XJ-9"]),  # 自然序相等时按原文
        (["XJ-1", "AJE-X01"], ["AJE-X01", "XJ-1"]),  # 前缀不同按前缀
    ],
)
def test_vouchers_sort_in_natural_order(entries: list[str], expected: list[str]) -> None:
    """验收 2 (审核 R134-02): 凭证号的自然序 (entry_order); Xero 的 7 个凭证号都是 3 位数, 区分不了自然序与字典序."""
    assert sorted(entries, key=entry_order) == expected


async def test_finding_without_entry_no_raises() -> None:
    """验收 2: GEN-05 的例外 detail 缺 entry_no 时抛 ValueError, 不调判断引擎 (Numbat 的 GEN-05 例外, 去掉一条的
    entry_no)."""
    engine = FakeJudgmentEngine({})
    deps = replace(make_deps(FakeRegistry(), FakeTrail()), judgments=engine)  # type: ignore[arg-type]
    ctx = make_ctx(deps, "GEN-02")
    found = numbat_gen05_result(make_run("GEN-05")).exceptions
    broken = found[0].model_copy(update={"detail": {k: v for k, v in found[0].detail.items() if k != "entry_no"}})
    ctx = replace(ctx, state=ctx.state | {"exceptions": [broken, *found[1:]]})
    with pytest.raises(ValueError, match=f"{broken.id} 的 detail 没有 entry_no"):
        await FieldworkAgent(deps).run(ctx)
    assert engine.calls == []


# ── 提示词 (验收 8) ─────────────────────────────────────────


def rendered(flow: Flow, no: str) -> str:
    """GEN-02 对该凭证渲染出的提示词 (变量同 JudgmentEngine.judge)."""
    jctx = flow.contexts[no]
    real = make_deps(FakeRegistry(), FakeTrail()).judgments
    variables = {
        "subject_id": jctx.subject_id,
        "task": jctx.task,
        "data": jctx.data,
        "rule_findings": [x.model_dump(mode="json") for x in jctx.rule_findings],
        "standards": real.standards(flow.ctx.template),
    }
    return real.render(flow.ctx.template, variables)


def json_objects(prompt: str) -> Iterator[dict[str, Any]]:
    """提示词里各 json 代码块中的全部对象 (含嵌套)."""

    def walk(value: Any) -> Iterator[dict[str, Any]]:
        if isinstance(value, dict):
            yield value
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)

    for block in re.findall(r"```json\n(.*?)\n```", prompt, flags=re.S):
        yield from walk(json.loads(block))


def birth_dates() -> set[str]:
    """补造目录 (登记摘录与人员检索) 中出现的全部出生日期, ISO 形式."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            found.update(str(v) for k, v in value.items() if k == "birth_date" and v)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for path in sorted((supplement().root / "registry").rglob("*.json")):
        walk(json.loads(path.read_text(encoding="utf-8")))
    return found


@pytest.mark.external
def test_prompt_holds_only_cited_journal_lines_and_no_birth_dates(flow: Flow) -> None:
    """验收 8: 各凭证的提示词里, 分录行 (source_type journal_entry 的对象) 只有本凭证与 related_entries 的凭证, 且都在;
    补造目录里的出生日期 (守卫有据: 登记摘录与人员检索带董事的出生日期) 不以 ISO 或 yyyymmdd 形式出现."""
    dates = birth_dates()
    assert dates  # 补造目录确有出生日期, 守卫才有意义
    forms = {form for d in dates for form in (d, d.replace("-", ""))}
    for no in VOUCHERS:
        prompt = rendered(flow, no)
        expected = {no, *(r["entry_no"] for r in flow.contexts[no].data["related_entries"])}
        found = {o["entry_no"] for o in json_objects(prompt) if o.get("source_type") == "journal_entry"}
        assert found == expected, no
        assert not [form for form in forms if form in prompt], no


# ── 7 份 fixture (需求 R135): 真实判断引擎, mock 大模型读 Xero 的 fixture 正本 ─────────────────────────


@dataclass(frozen=True)
class Fixed:
    patch: StatePatch  # GEN-02 的状态片段
    calls: list[TrailEntry]  # 大模型调用的留痕, 按调用顺序
    contexts: dict[str, JudgmentContext]  # 送给判断引擎的上下文, 按凭证号
    judgments: dict[str, Judgment]  # 判断, 按凭证号


async def run_fixed() -> Fixed:
    """GEN-05 之后用真实判断引擎执行一次 GEN-02: 主模型与复核模型都读 Xero 的 fixture 正本 (同 test_xero_fixtures 的
    收入一串)."""
    trail = FakeTrail()
    client = MockLLMClient(XERO.fixtures)
    deps = make_xero_deps(trail=trail, llm=client, review_llm=client)
    recording = RecordingJudgments(deps.judgments)
    deps = replace(deps, judgments=recording)  # type: ignore[arg-type]
    patch = await FieldworkAgent(deps).run(xero_context(deps, await gen05_state(), "GEN-02", []))
    calls = [e for e in trail.entries if e.kind is TrailKind.llm_call]
    return Fixed(
        patch,
        calls,
        {c.subject_id: c for c in recording.contexts},
        {j.subject_id: j for j in patch["judgments"]},
    )


@pytest.fixture(scope="module")
def fixed() -> Fixed:
    return asyncio.run(run_fixed())


def context_problems(name: str, value: dict[str, Any], jctx: JudgmentContext, standards: list[str]) -> list[str]:
    """GEN-02 fixture 中引了上下文以外来源的指针 (只写 fixture 与指针): 允许的来源为本凭证与相关凭证的分录行, 科目
    余额, 银行账户与对账单行, 相关发票及其服务完成记录, data.contracts 中合同的条款 (locator 为条款 ID, page 为该
    条款的页), 关联文件的页 (1 至 page_count), 以及模板 standards 的片段."""
    data = jctx.data
    bank = data["bank"] or {"accounts": [], "lines": []}
    invoices = data["related_invoices"]
    allowed: dict[str, set[Any]] = {
        "journal_entry": {r["id"] for r in [*data["entry_lines"], *data["related_entries"]]},
        "trial_balance": {b["code"] for b in data["account_balances"]},
        "bank_account": {a["id"] for a in bank["accounts"]},
        "bank_transaction": {t["id"] for t in bank["lines"]},
        "sales_invoice": {i["invoice"]["id"] for i in invoices},
        "delivery_note": {n["id"] for i in invoices for n in i["delivery_notes"]},
        "standard": set(standards),
        "document": {
            (c["document"]["id"], t["id"], t["page"]) for c in data["contracts"] for t in c["contract"]["terms"]
        },
    }
    pages = {d["document"]["id"]: d["document"]["page_count"] for d in data["documents"]}
    problems = []
    for p in [*value["evidence"], *(q for s in value["reasoning_chain"] for q in s["evidence"])]:
        if p["source_type"] == "document" and 1 <= (p.get("page") or 0) <= pages.get(p["source_id"], 0):
            continue
        key = (p["source_id"], p.get("locator"), p.get("page")) if p["source_type"] == "document" else p["source_id"]
        if key not in allowed.get(p["source_type"], set()):
            problems.append(f"{name}: {p['source_type']} {p['source_id']} {p.get('locator')} 不在该凭证的上下文中")
    return sorted(set(problems))


def fixture(voucher: str) -> dict[str, Any]:
    """该凭证的 fixture 正本."""
    return json.loads((XERO.fixtures / "GEN-02" / f"{voucher}.json").read_text(encoding="utf-8"))


@pytest.mark.external
def test_fixtures_pass_the_real_engine_and_cite_only_the_context(fixed: Fixed) -> None:
    """R135 验收 1: 每笔凭证调一次大模型, 第 1 次就通过 (没有失败级别, 没有错误), 推理链每步都受支持 (指针都可解析);
    7 条判断各一条 normal 任务, 例外不升版, 执行成功; 每份 fixture 的指针都在该凭证的上下文内 (context_problems)."""
    assert [e.payload["fixture_key"] for e in fixed.calls] == VOUCHERS
    assert {(e.payload["attempt"], e.payload["level_failed"]) for e in fixed.calls} == {(1, None)}
    assert [e.payload["errors"] for e in fixed.calls] == [[]] * len(VOUCHERS)
    for j in fixed.judgments.values():
        assert {s.status for s in j.reasoning_chain} == {StepStatus.supported}, j.id
    assert [(t.id, t.kind) for t in fixed.patch["review_tasks"]] == [
        (f"T-J-GEN-02-1-{no}-v1", TaskKind.normal) for no in VOUCHERS
    ]
    assert fixed.patch["exceptions"] == [] and fixed.patch["procedure_runs"][0].status is RunStatus.succeeded
    standards = LIBRARY.load("GEN-02").standards
    for no in VOUCHERS:
        assert context_problems(no, fixture(no), fixed.contexts[no], standards) == []


@pytest.mark.external
@pytest.mark.parametrize(
    "pointer",
    [
        {"source_type": "journal_entry", "source_id": "XJ-500-1", "locator": "debit_cents"},  # 上下文外的凭证
        {"source_type": "trial_balance", "source_id": "200", "locator": "current_cents"},  # 别的科目
        {"source_type": "bank_transaction", "source_id": "BT-090-0040", "locator": "amount_cents"},  # 窗口外的流水
        {"source_type": "document", "source_id": "DOC-4873d193e29f", "locator": "Nil", "page": 2},  # 页码超出
        {"source_type": "standard", "source_id": "ASA240-P27", "locator": "para 27"},  # 模板 standards 以外
    ],
)
def test_context_guard_reports_pointers_outside_the_context(fixed: Fixed, pointer: dict[str, Any]) -> None:
    """自测: XJ-499 的 fixture 副本另引一个上下文以外的来源, 守卫报且只报这一条."""
    value = fixture("XJ-499")
    value["evidence"].append({"type": "voucher", "page": None, "reliability": None, **pointer})
    problems = context_problems("XJ-499", value, fixed.contexts["XJ-499"], LIBRARY.load("GEN-02").standards)
    assert problems == [
        f"XJ-499: {pointer['source_type']} {pointer['source_id']} {pointer['locator']} 不在该凭证的上下文中"
    ]


def evidence_of(j: Judgment) -> set[tuple[str, str, int | None]]:
    return {(p.source_type, p.source_id, p.page) for p in j.evidence}


@pytest.mark.external
def test_xj499_is_a_misstatement_of_office_equipment(fixed: Fixed) -> None:
    """R135 验收 2: XJ-499 认定错报, 草稿为 710 多记 750.00 (金额即所引 XJ-499-1 的借方); 结论写明 710 上期无余额而
    本期 750.00 全来自 XJ-499-1, 固定资产登记表为 Nil, 分录没有依据且对折旧抵扣进项税, 711 多记 825.00, 820 少记
    75.00, 不影响损益与净资产 (只查关键词); 证据含三行分录, 710 的试算表, 登记表第 1 页与两个准则片段; 置信度按
    0.3 / 0.3 / 0.2 加权为 0.85."""
    j = fixed.judgments["XJ-499"]
    assert j.proposed_action is ProposedAction.misstatement
    assert j.misstatement_draft is not None
    assert j.misstatement_draft.model_dump(mode="json") == {
        "type": "factual",
        "account": "Office equipment (710)",
        "assertion": "Existence",
        "amount_cents": 75000,
        "direction": "overstated",
        "account_code": "710",  # 需求 R156 (第 7 批): AI 草稿从上下文得到科目代码时填
    }
    for text in (
        "no prior period trial balance",
        "AUD 750.00 comes entirely from XJ-499-1",
        "shows Nil",
        "input tax on depreciation",
        "(711) is overstated by AUD 825.00",
        "(820) is understated by AUD 75.00",
        "no effect on profit or loss or net assets",
    ):
        assert text in j.conclusion, text
    assert {
        ("journal_entry", "XJ-499-1", None),
        ("journal_entry", "XJ-499-2", None),
        ("journal_entry", "XJ-499-3", None),
        ("trial_balance", "710", None),
        ("document", "DOC-4873d193e29f", 1),
        ("standard", "ASA240-P33", None),
        ("standard", "ASA240-A44", None),
    } <= evidence_of(j)
    assert (j.confidence_parts, j.confidence, j.exception_ids) == (
        {"evidence": 0.7, "rule": 1.0, "llm": 0.85},
        0.85,
        ["X-GEN-05-1-R09-2"],
    )


@pytest.mark.external
def test_xj575_needs_the_expense_claim(fixed: Fixed) -> None:
    """R135 验收 3: XJ-575 要更多证据, 没有错报草稿; 结论写明原凭证 XJ-560 收款人 Grainger, 同日由 XJ-567 作废后
    改记为 Garrison, 银行只有当日的批量付款 BT-090-0052 (-889.80), 看不出收款人, 改名没有记录, 须取得报销单与审批
    (ASA 240 第 33(a)(i) 段); 置信度 0.8125."""
    j = fixed.judgments["XJ-575"]
    assert (j.proposed_action, j.misstatement_draft) == (ProposedAction.more_evidence, None)
    for text in (
        "XJ-560",
        "to Odette Grainger and voided by XJ-567",
        "to Odette Garrison",
        "BT-090-0052",
        "AUD -889.80",
        "does not identify the payee",
        "why the payee's name was changed",
        "expense claim and its approval",
        "33(a)(i)",
    ):
        assert text in j.conclusion, text
    assert {("journal_entry", "XJ-560-1", None), ("bank_transaction", "BT-090-0052", None)} <= evidence_of(j)
    assert (j.confidence_parts, j.confidence) == ({"evidence": 0.7, "rule": 1.0, "llm": 0.7}, 0.8125)


@pytest.mark.external
@pytest.mark.parametrize(
    ("voucher", "texts"),
    [
        (  # 银行两侧与对账单期初一致, 840 为董事借款; 0630 表缺的余额属未审比较数, 不是本期错报 (Issue #46)
            "XJ-397",
            (
                "-8,703.20 + 4,130.98 = -4,572.22",
                "091 opens at AUD 15,081.23",
                "loan agreement",
                "confirmation",
                "unaudited comparative information, not a misstatement of this period",
            ),
        ),
        (  # 同日同额重复开票, 没有服务记录的已全额冲销
            "XJ-405",
            (
                "ORC1003 duplicates ORC1008",
                "ORC1003 has no service-completion record",
                "SR-ORC1008",
                "cancelled in full by credit note CR1003 (XJ-430)",
                "CON-X02 provides for one invoice",
            ),
        ),
        (  # 冲销的是重复开票 ORC1003, 依据 R12-1
            "XJ-430",
            ("X-GEN-05-1-R12-1", "paired invoices", "full credit", "cancels the duplicate invoice ORC1003 in full"),
        ),
        (  # 同日, 同一客户, 同额, 收入净额不受影响
            "XJ-449",
            (
                "ORC1005 is the only one to Bank West",
                "same day",
                "no service-completion record",
                "revenue is not affected",
            ),
        ),
        (  # 091 转 090, 只换了名称, 两边对账单对应, 两户同一银行
            "XJ-601",
            (
                "AUD 3,500.00 from the Business Savings Account (091) to the Business Bank Account (090)",
                "only the description now uses the account names",
                "BT-090-0065",
                "BT-091-0001",
                "Wattlebrook Bank Limited",
            ),
        ),
    ],
)
def test_other_vouchers_are_accepted(fixed: Fixed, voucher: str, texts: tuple[str, ...]) -> None:
    """R135 验收 4: 其余 5 笔 accept, 没有错报草稿, 结论含设计稿第 6.3 节的要点 (只查关键词); 有例外而建议 accept,
    rule 分项为 0 (只断言分项值, 不比较各判断置信度的高低). 结论与推理链不引已取消的 FIN-12 (Issue #46)."""
    j = fixed.judgments[voucher]
    assert (j.proposed_action, j.misstatement_draft, j.confidence_parts["rule"]) == (ProposedAction.accept, None, 0.0)
    for text in texts:
        assert text in j.conclusion, text
    assert "FIN-12" not in " ".join([j.conclusion, *(step.text for step in j.reasoning_chain)])
