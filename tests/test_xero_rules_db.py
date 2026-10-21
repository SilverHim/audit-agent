"""需求 R112: Xero 规则 SQL 连库验收 (slow). 测试库中建 Xero 项目, 账表按补造清单写入 (同演示数据导入的账表部分:
快照 XERO_TABLES 七张为数据版本 1, source xero; 补造账表各一版, source import; 不经数据集载入, 不上传文件), 用真实的
PostgresFinancialDataStore 与 SqlRuleEngine 跑现有规则, 结果等于设计稿 v2.1 与离线预置 XERO_RULES (R107).

建项目与各组断言写成以快照目录, 补造目录为参数的模块级函数 (build_xero_project, run_rule_programs, check_*), 供 R121
(注入测试 T0) 在基线副本上复用. 快照与补造经 tests.external 取 (R294: 仓库跟踪了却找不到即失败, 哈希不符跳过);
没有 TEST_DATABASE_URL 时跳过.
"""

import os
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from backend.adapters.registry_mock import DatasetRegistrySource
from backend.adapters.xero_supplements import plan
from backend.agents.base import AgentDeps, RunContext
from backend.agents.client_intel import ClientIntelAgent
from backend.agents.evidence import confirmation_key, reply_reliability
from backend.agents.fieldwork import FieldworkAgent
from backend.contracts.data import XERO_TABLES, TableKind
from backend.contracts.models import (
    ClientProfile,
    Confirmation,
    ConfirmationResult,
    DocumentType,
    Engagement,
    EvidencePointer,
    EvidenceType,
    Phase,
    Procedure,
    ProcedureRun,
    ReceiptChannel,
    TrailKind,
)
from backend.contracts.records import DatasetTraits, DeliveryNote
from backend.contracts.services import RuleResult
from backend.contracts.state import EngagementState, initial_state
from backend.orchestration.nodes import build_run
from backend.persistence import models as m
from backend.persistence.stores import RoutingDatasetLoader, XeroDatasetLoader, now
from tests.external import snapshot_dir, supplements_dir
from tests.fakes import FakeJudgmentEngine, make_ctx, numbat_rev08_populations, passed, rev08_populations
from tests.test_data_store_postgres import table_rows
from tests.xero_fakes import (
    SNAPSHOT_NAME,
    XERO_DATASET,
    XERO_PERIOD,
    XERO_REV08_WINDOW,
    XERO_RULES,
    XERO_S01_ROWS,
    xero_rev08_populations,
    xero_tables,
)
from tests.xero_injection import file_hashes

pytestmark = [pytest.mark.slow, pytest.mark.external]

PERIOD_START, PERIOD_END = date(2026, 7, 1), date(2026, 9, 30)
MATERIALITY = {"materiality_cents": 32431, "performance_materiality_cents": 16216, "clearly_trivial_cents": 1622}
REVENUE = ["OBJ-REV-OCC", "OBJ-REV-CUT"]  # 补造计划预置的收入两条目标, significant
CASH = ["OBJ-CASH-EXI"]  # 现金目标, high
LINKS = {"REV-08": REVENUE, "REV-02": REVENUE, "CSH-05": CASH, "CSH-02": CASH}  # 程序关联的目标; PLN 两条没有
RULE_PROGRAMS = ("PLN-07", "PLN-01", "REV-08", "REV-02", "CSH-05", "CSH-02")  # 有规则 SQL 的现有程序

# 旧 A01 (提交 54a88ed 之前, R102 改为资产负债类对期初之前): 本期对上期, 门槛为变动额 100,000.00. Xero 演示公司规模
# 小, 各科目本期对上期的变动最大是 477 的 3,350,000 分, 也够不上, 一行都出不来 (R102 改口径的理由)
OLD_A01 = """
WITH balances AS (
 SELECT COALESCE(c.account_code,p.account_code) AS code, c.balance_cents AS current_cents,
        p.balance_cents AS prior_cents, COALESCE(c.balance_cents,0)-COALESCE(p.balance_cents,0) AS change_cents
 FROM (SELECT * FROM trial_balance WHERE engagement_id=:eid AND period='current') c
 FULL JOIN (SELECT * FROM trial_balance WHERE engagement_id=:eid AND period='prior') p ON c.account_code=p.account_code)
SELECT 'trial_balance' AS source_type, b.code AS source_id, b.change_cents AS amount_cents,
       a.name AS account_name, b.current_cents, b.prior_cents,
       CAST(b.change_cents AS numeric)/NULLIF(ABS(b.prior_cents),0) AS change_pct
FROM balances b JOIN accounts a ON a.engagement_id=:eid AND a.code=b.code
WHERE ABS(b.change_cents)>=:min_change_cents
 AND (COALESCE(b.prior_cents,0)=0 OR ABS(b.change_cents)>=:change_pct*ABS(b.prior_cents))
ORDER BY source_id
"""
OLD_A01_PARAMS = {"min_change_cents": 10000000, "change_pct": 0.2}  # 当时 PLN-07.yaml 的参数
# 旧 R03 (提交 46e4f69 之前, R101 加数字串整词匹配之前): 流水参考号须包含整个发票号. Xero 的客户付款只写 "1026" 或
# "Invoice 1023", 对不上 ORC1026, 一行都出不来
OLD_R03 = """
SELECT 'sales_invoice' AS source_type, i.id AS source_id,
       i.amount_cents-SUM(t.amount_cents) AS amount_cents, i.amount_cents AS invoice_amount_cents,
       SUM(t.amount_cents) AS received_cents, string_agg(t.id, ',' ORDER BY t.id) AS bank_transaction_ids
FROM sales_invoices i JOIN bank_transactions t ON t.engagement_id=i.engagement_id
  AND t.amount_cents>0 AND position(i.invoice_no IN t.reference)>0
WHERE i.engagement_id=:eid
  AND i.posting_date BETWEEN CAST(:period_end AS date)-CAST(:window_days AS int) AND CAST(:period_end AS date)
GROUP BY i.id, i.amount_cents
HAVING SUM(t.amount_cents)<>i.amount_cents
ORDER BY i.id
"""
# 回函 (补造目录 confirmations/ 下, 文件名为账号) 与 DOC-06 抽出的值 (R111 验收 2): 账户 → (账号, 回函余额, 应计利息)
REPLIES = {"BA-090": ("12345678", -1090044, 0), "BA-091": ("12345679", 1158123, 4853)}


def database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a migrated disposable PostgreSQL database")
    return url


async def new_owner(engine: AsyncEngine) -> str:
    """测试库里新建一个用户 (项目负责人), 返回其 ID."""
    owner = "USR-" + uuid4().hex
    async with engine.begin() as conn:
        await conn.execute(
            insert(m.users).values(
                id=owner, email=owner + "@example.com", full_name="Jordan Ellis", password_hash="x", created_at=now()
            )
        )
    return owner


@dataclass
class XeroProject:
    """测试库中按补造清单写好账表的 Xero 项目."""

    deps: AgentDeps  # 真实存储与规则引擎; 数据集与登记 mock 按给定的快照与补造目录
    engagement: Engagement
    profile: ClientProfile
    traits: DatasetTraits
    state: EngagementState  # 进入 P2 时: 计划预置的目标与风险, 闸门定的重要性
    owner: str
    datasets: RoutingDatasetLoader
    supplement_tables: list[tuple[TableKind, str]]  # 补造清单的账表与来源文件, 按写入顺序


async def build_xero_project(url: str, snapshot: Path, supplements: Path, tmp: Path) -> XeroProject:
    """验收 1: 测试库中建 Xero 项目 (数据集 xero/<快照目录名>, 期间 2026-07-01 至 09-30), 账表按补造清单写入.
    snapshot, supplements 为快照目录与补造目录 (基线或其副本); 依赖为组装根的真实实现, 只把数据集载入与登记 mock 换成
    指向这两个目录的."""
    from backend.core.config import Settings
    from backend.main import build_deps

    settings = Settings(DATABASE_URL=url, EVIDENCE_ROOT=tmp / "evidence", LLM_MODE="mock")
    deps = build_deps(settings)
    engine = deps.data.engine  # type: ignore[attr-defined]  # 组装根的引擎 (应用角色)
    xero = XeroDatasetLoader(
        snapshot.parent,
        settings.KNOWLEDGE_ROOT / "planning",
        engine,
        deps.evidence_store,
        supplements=supplements.parent,
        allow_injected=True,  # 副本 (xero_case) 的 manifest 带 injected; 原目录没有, 照常核对
    )
    datasets = RoutingDatasetLoader(deps.datasets.default, {XeroDatasetLoader.PREFIX: xero})  # type: ignore[attr-defined]  # Numbat 照旧
    dataset = f"{XeroDatasetLoader.PREFIX}{snapshot.name}"
    deps = replace(
        deps,
        datasets=datasets,
        registry=DatasetRegistrySource(deps.engagement_store, datasets.supplement_dir),  # type: ignore[arg-type]
    )
    owner = await new_owner(engine)
    profile = xero.client_profile(dataset).model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagement = await deps.engagement_store.create(  # type: ignore[attr-defined]
        owner_id=owner, period_start=PERIOD_START, period_end=PERIOD_END, dataset=dataset, profile=profile
    )
    demo = plan(xero, dataset)
    await deps.data.write_tables(engagement.id, demo.snapshot, source="xero", actor=owner)
    for table in demo.tables:
        await deps.data.write_tables(
            engagement.id, {table.kind: table.rows}, source="import", actor=owner, file_name=table.file_name
        )
    objectives, risks = xero.planning_presets(dataset)
    state = initial_state(engagement, [], profile.version) | {  # type: ignore[operator]
        "audit_objectives": objectives,
        "risk_register": risks,
        **MATERIALITY,
    }
    tables = [(t.kind, t.file_name) for t in demo.tables]
    return XeroProject(deps, engagement, profile, xero.dataset_traits(dataset), state, owner, datasets, tables)  # type: ignore[arg-type]


def procedure_of(deps: AgentDeps, procedure_id: str) -> Procedure:
    template = deps.library.load(procedure_id)
    return Procedure(
        id=procedure_id,
        phase=Phase.P2_Executing,
        template_commit=deps.library.commit,
        objective_ids=LINKS.get(procedure_id, []),
        autonomy_level=template.autonomy_level,
    )


async def run_rule_programs(project: XeroProject) -> dict[str, tuple[ProcedureRun, RuleResult]]:
    """六条规则程序各跑一次 (第 1 次执行): 参数由程序库按项目, 状态, 数据集声明与关联目标的风险解析 (同
    GraphNodes 的 build_run), 规则引擎为真实的 SqlRuleEngine."""
    deps, found = project.deps, {}
    for procedure_id in RULE_PROGRAMS:
        template = deps.library.load(procedure_id)
        run = build_run(
            deps.library,
            template,
            procedure_of(deps, procedure_id),
            project.engagement,
            project.state,
            datetime.now(UTC),
            traits=project.traits,
            profile=project.profile,
        )
        found[procedure_id] = (run, await deps.rules.run(project.engagement.id, template, run, run.params_snapshot))
    return found


def offline(procedure_id: str, run: ProcedureRun) -> RuleResult:
    """离线预置 XERO_RULES 对同一次执行的结果 (验收 8)."""
    return XERO_RULES[procedure_id](run)


async def check_versions(project: XeroProject) -> None:
    """验收 1: 版本行为快照七张 (数据版本 1, source xero) 与补造账表四版 (source import, 原文件名)."""
    versions = await project.deps.data.versions(project.engagement.id)
    assert sorted((v.data_version, v.kind.value, v.source, v.file_name) for v in versions) == sorted(
        [(1, k.value, "xero", None) for k in XERO_TABLES]
        + [(n, k.value, "import", f) for n, (k, f) in enumerate(project.supplement_tables, 2)]
    )
    assert len(project.supplement_tables) == 4


async def check_planning(project: XeroProject, results: dict[str, tuple[ProcedureRun, RuleResult]]) -> None:
    """验收 2, 3: PLN-07 与 PLN-01 等于离线预置; 820 的期初 79120; 960 (期初 0 加上期损益 791200, 等于本期) 与 860
    (9 分) 在试算表里而不出行; 旧 A01 一行都出不来."""
    eid, financial = project.engagement.id, project.deps.financial_store
    run, pln07 = results["PLN-07"]
    assert pln07 == offline("PLN-07", run)
    flagged = {x.source_ids[0]: x for x in pln07.exceptions}
    assert len(flagged) == 9 and flagged["820"].detail["opening_cents"] == "79120"
    current = {r.account_code: r.balance_cents for r in await financial.trial_balance(eid, "current")}
    assert (current["960"], current["860"]) == (791200, 9) and not {"960", "860"} & set(flagged)
    assert await financial.query(eid, OLD_A01, OLD_A01_PARAMS) == []
    run, pln01 = results["PLN-01"]
    assert pln01 == offline("PLN-01", run)
    assert {r["basis"]: r["base_amount_cents"] for r in pln01.rows["M01"]} == {
        "revenue": 3243100,
        "total_assets": 1080529,
        "profit_before_tax": -1322696,
    }


def paid_by(invoice_no: str, receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """参考号里整词出现发票号数字串的收款 (同 R03 对 Xero 发票号的认法, 测试里独立写一遍核对数据)."""
    digits = re.sub(r"^[A-Za-z]+", "", invoice_no)
    return [r for r in receipts if re.search(rf"(^|[^0-9]){digits}([^0-9]|$)", r["reference"] or "")]


async def check_revenue(project: XeroProject, results: dict[str, tuple[ProcedureRun, RuleResult]]) -> None:
    """验收 4, 5: REV-08 只有 R03 对 ORC1026 一条 (发票 715000, 收到 385000, 差 330000), R01, R02 为空; 旧 R03 为空;
    ORC1020 至 1023 各有一笔全额的收款 (所以不出), ORC1024 的收款没写参考号 (不参与). REV-02 S01 为六张, 同离线预置;
    XJ-580 的三行可按凭证号取到."""
    eid, financial = project.engagement.id, project.deps.financial_store
    run, rev08 = results["REV-08"]
    assert rev08 == offline("REV-08", run)
    [x] = rev08.exceptions
    assert (x.rule_id, x.source_ids, x.amount_cents) == ("R03", ["SI-77b338ef-ecc0-4b95-a0d7-2617b0054611"], 330000)
    assert x.detail == {
        "invoice_amount_cents": 715000,
        "received_cents": "385000",
        "bank_transaction_ids": "BT-090-0124",
    }
    assert await financial.query(eid, OLD_R03, run.params_snapshot) == []
    invoices = {
        r["invoice_no"]: r["amount_cents"]
        for r in await financial.query(eid, "SELECT invoice_no, amount_cents FROM sales_invoices ORDER BY id", {})
    }
    receipts = await financial.query(
        eid, "SELECT id, amount_cents, reference FROM bank_transactions WHERE amount_cents > 0 ORDER BY id", {}
    )
    for number in ("ORC1020", "ORC1021", "ORC1022", "ORC1023"):
        assert [r["amount_cents"] for r in paid_by(number, receipts)] == [invoices[number]], number
    assert paid_by("ORC1024", receipts) == [] and invoices["ORC1024"] == 55000
    run, rev02 = results["REV-02"]
    assert rev02 == offline("REV-02", run) and rev02.rows["S01"] == XERO_S01_ROWS
    lines = await financial.journal_entries(eid, entry_nos=["XJ-580"])
    assert [line.id for line in lines] == ["XJ-580-1", "XJ-580-2", "XJ-580-3"]


async def check_cash(project: XeroProject, results: dict[str, tuple[ProcedureRun, RuleResult]]) -> None:
    """验收 6: CSH-05 没有例外; CSH-02 的三组候选行等于离线预置 (由假存储的表派生): D02 为 BA-090 期后的 21 笔
    (BT-090-0073 至 0093); D03 里 BA-091 只剩 XJ-601-1, 作废对 XJ-599, XJ-602 在窗口内而被剔除; D04 33 行 (窗口内
    37 行减去作废对的 4 行), 没有借或贷 4853 (BA-091 回函的应计利息) 的行."""
    eid, financial = project.engagement.id, project.deps.financial_store
    run, csh05 = results["CSH-05"]
    assert csh05 == offline("CSH-05", run) and csh05.exceptions == []
    run, csh02 = results["CSH-02"]
    assert csh02 == offline("CSH-02", run)
    d02, d03, d04 = (csh02.rows[key] for key in ("D02", "D03", "D04"))
    assert [r["id"] for r in d02] == [f"BT-090-{n:04d}" for n in range(73, 94)]
    assert {r["bank_account_id"] for r in d02} == {"BA-090"}
    assert [r["id"] for r in d03 if r["bank_account_id"] == "BA-091"] == ["XJ-601-1"]
    voided = await financial.journal_entries(eid, entry_nos=["XJ-599", "XJ-602"])
    window = (PERIOD_END - timedelta(days=run.params_snapshot["lookback_days"]), PERIOD_END)
    assert len(voided) == 4 and all(window[0] <= line.posting_date <= window[1] for line in voided)
    assert not {line.entry_no for line in voided} & {r["entry_no"] for r in d03 + d04}
    assert len(d04) == 33 and not [r for r in d04 if 4853 in (r["debit_cents"], r["credit_cents"])]


async def check_csh02_fieldwork(project: XeroProject, supplements: Path) -> None:
    """验收 7: 两份回函 PDF 经真实文件存储登记 (不经 A3), 函证 C-BA-090, C-BA-091 第 2 版取 R111 验收 2 的值;
    FieldworkAgent 用真实存储与规则引擎跑 CSH-02, 判断引擎为假的: 两户 agreed (回函等于账面), 例外只有
    X-CSH-02-1-D04-1 (BA-091 回函写明的应计利息 4853 在期末前 5 天的总账里没有同额的行), 判断对象只有 BA-091."""
    deps, engagement = project.deps, project.engagement
    accounts = {a.id: a for a in await deps.financial_store.bank_accounts(engagement.id)}
    confirmations, documents = [], []
    for account, (account_no, balance, accrued) in REPLIES.items():
        reply = await deps.evidence_store.save(
            engagement.id,
            f"{account_no}.pdf",
            (supplements / "confirmations" / f"{account_no}.pdf").read_bytes(),
            type=DocumentType.confirmation,
            reliability=reply_reliability(ReceiptChannel.direct_email),
            channel="bank_direct",
        )
        documents.append(reply)
        pointers = [
            EvidencePointer(
                type=EvidenceType.external,
                source_type="document",
                source_id=reply.id,
                locator=locator,
                page=1,
                reliability=reply.reliability,
            )
            for locator in ("confirmed_balance_cents", "accrued_interest_cents")
        ]
        confirmations.append(
            Confirmation(
                id=f"C-{account}",
                version=2,
                bank_account_id=account,
                confirming_party="Wattlebrook Bank Limited",
                idempotency_key=confirmation_key(engagement.id, accounts[account]),  # 同 CSH-04 发函时的键
                sent_at=datetime(2026, 10, 17, tzinfo=UTC),
                reply_document_id=reply.id,
                receipt_channel=ReceiptChannel.direct_email,
                balance_date=PERIOD_END,
                confirmed_balance_cents=balance,
                accrued_interest_cents=accrued,
                evidence=pointers,
            )
        )
    judgments = FakeJudgmentEngine({"BA-091": passed()})
    deps = replace(deps, judgments=judgments, review_llm=None)
    procedure = procedure_of(deps, "CSH-02")
    template = deps.library.load("CSH-02")
    state = project.state | {"confirmations": confirmations, "documents": documents, "procedure_pack": [procedure]}
    run = build_run(
        deps.library,
        template,
        procedure,
        engagement,
        state,
        datetime.now(UTC),
        traits=project.traits,
        profile=project.profile,
    )
    patch = await FieldworkAgent(deps).run(RunContext(engagement, procedure, template, run, state))  # type: ignore[arg-type]
    assert {c.id: (c.version, c.result, c.difference_cents) for c in patch["confirmations"]} == {
        "C-BA-090": (3, ConfirmationResult.agreed, 0),
        "C-BA-091": (3, ConfirmationResult.agreed, 0),
    }
    assert [(x.id, x.source_ids, x.amount_cents) for x in patch["exceptions"]] == [
        ("X-CSH-02-1-D04-1", ["BA-091"], 4853)
    ]
    assert [ctx.subject_id for _, ctx, _ in judgments.calls] == ["BA-091"]


async def check_acc04(project: XeroProject) -> None:
    """验收 9: ACC-04 的登记 mock 读补造目录的 registry/ (R71): 有数据集的客户三项检查 (abn_active, name_match,
    company_registered; R290 的 directors_match 只对新建的客户) 都通过, 真实留痕里有 2 行外部调用."""
    deps, engagement = project.deps, project.engagement
    patch = await ClientIntelAgent(deps).run(make_ctx(deps, "ACC-04", engagement=engagement))
    checks = patch["acceptance_scorecard"].registry_checks
    assert [(c.check, c.passed) for c in checks] == [
        ("abn_active", True),
        ("name_match", True),
        ("company_registered", True),
    ]
    assert len(await deps.trail.list(engagement.id, TrailKind.external_call)) == 2


async def test_xero_rules_on_postgres(tmp_path: Path) -> None:
    """验收 1-9, 11: 建一次 Xero 项目跑完全流程; 快照与补造原目录的逐文件哈希前后不变."""
    snapshot, supplements = snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME)
    url = database_url()
    before = {path: file_hashes(path) for path in (snapshot, supplements)}
    project = await build_xero_project(url, snapshot, supplements, tmp_path)
    assert project.engagement.dataset == XERO_DATASET
    results = await run_rule_programs(project)
    await check_versions(project)
    await check_planning(project, results)
    await check_revenue(project, results)
    await check_cash(project, results)
    await check_csh02_fieldwork(project, supplements)
    await check_acc04(project)
    assert {path: file_hashes(path) for path in (snapshot, supplements)} == before


async def test_rev08_populations_on_postgres(tmp_path: Path) -> None:
    """需求 R324 验收 6: 连库的 REV-08 被测总体等于离线预置 (xero_rev08_populations, 其值见 tests/test_e2e_xero17
    的 test_populations): R01, R02 六张 990000, R03 五张 935000 (没有 ORC1024); 每条例外所指的发票都在出它的规则的
    总体里."""
    snapshot, supplements = snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME)
    project = await build_xero_project(database_url(), snapshot, supplements, tmp_path)
    run, rev08 = (await run_rule_programs(project))["REV-08"]
    assert run.params_snapshot["window_days"] == 20
    assert rev08.populations == xero_rev08_populations()
    assert [(p.rule_id, p.count, p.amount_cents) for p in rev08.populations] == [
        ("R01", 6, 990000),
        ("R02", 6, 990000),
        ("R03", 5, 935000),
    ]
    tested = {p.rule_id: {i.source_id for i in p.items} for p in rev08.populations}
    assert rev08.exceptions and all(set(x.source_ids) <= tested[x.rule_id] for x in rev08.exceptions)


async def test_rev08_one_item_per_invoice_on_postgres(tmp_path: Path) -> None:
    """R324-05, R324-13: 一张发票几条服务完成记录时总体仍一张一项. 测试库里本项目的服务完成记录另写一版, 给 ORC1020 加
    一条没签收的与一条签收人写成 "姓, 名" 的 (补造目录不动): R01, R02 仍为 6 张 990000, ORC1020 的 records 为三条记录
    (按记录 ID; 没签收的签收日与签收人为 null, 签收人里的逗号原样); 总体与由同一组表算出的 rev08_populations 相等."""
    snapshot, supplements = snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME)
    project = await build_xero_project(database_url(), snapshot, supplements, tmp_path)
    tables = xero_tables()
    [orc1020] = [i for i in tables["sales_invoices"] if i.invoice_no == "ORC1020"]
    extra = [
        DeliveryNote(
            id=f"SR-ORC1020-{n}",
            delivery_no=f"SR-ORC1020-{n}",
            invoice_id=orc1020.id,
            shipped_date=date(2026, 9, 2),
            signed_date=signed_date,
            signed_by=signed_by,
        )
        for n, signed_date, signed_by in ((2, None, None), (3, date(2026, 9, 29), "Reilly, Tom"))
    ]
    notes = [*tables["delivery_notes"], *extra]
    await project.deps.data.write_tables(
        project.engagement.id,
        {TableKind.delivery_notes: notes},
        source="import",
        actor=project.owner,
        file_name="delivery_notes.csv",
    )
    _, rev08 = (await run_rule_programs(project))["REV-08"]
    assert rev08.populations == rev08_populations(
        tables["sales_invoices"], notes, tables["bank_transactions"], XERO_PERIOD[1], XERO_REV08_WINDOW
    )
    assert [(p.rule_id, p.count, p.amount_cents) for p in rev08.populations] == [
        ("R01", 6, 990000),
        ("R02", 6, 990000),
        ("R03", 5, 935000),
    ]
    for population in rev08.populations[:2]:
        [item] = [i for i in population.items if i.source_id == orc1020.id]
        assert item.fields["records"] == [
            {"delivery_no": "SR-ORC1020", "signed_date": "2026-09-30", "signed_by": "Tom Reilly"},
            {"delivery_no": "SR-ORC1020-2", "signed_date": None, "signed_by": None},
            {"delivery_no": "SR-ORC1020-3", "signed_date": "2026-09-29", "signed_by": "Reilly, Tom"},
        ]


async def test_numbat_rules_unchanged_on_the_same_database(tmp_path: Path) -> None:
    """验收 10: 同一测试库上 Numbat (data/demo, CSV 数据集载入) 的同批 SQL 结果不变: PLN-07 A01 (1100, 52500000),
    REV-08 R01 (SI-2026-0612, 14200000), CSH-05 B01 (BA-01, -5000000). REV-08 的被测总体等于离线预置 (需求 R324 审核
    R324-03: 默认测试用的 Numbat 总体出自它)."""
    from backend.core.config import Settings
    from backend.main import build_deps

    url = database_url()
    deps = build_deps(Settings(DATABASE_URL=url, EVIDENCE_ROOT=tmp_path / "evidence", LLM_MODE="mock"))
    owner = await new_owner(deps.data.engine)  # type: ignore[attr-defined]
    profile = deps.datasets.client_profile("demo").model_copy(update={"id": "CLIENT-" + uuid4().hex})
    engagement = await deps.engagement_store.create(  # type: ignore[attr-defined]
        owner_id=owner, period_start=date(2025, 7, 1), period_end=date(2026, 6, 30), dataset="demo", profile=profile
    )
    await deps.datasets.load(engagement.id, "demo")
    objectives, risks = deps.datasets.planning_presets("demo")
    state = initial_state(engagement, [], profile.version) | {"audit_objectives": objectives, "risk_register": risks}
    found = {}
    for procedure_id, rule_id in (("PLN-07", "A01"), ("REV-08", "R01"), ("CSH-05", "B01")):
        template = deps.library.load(procedure_id)
        run = build_run(
            deps.library,
            template,
            procedure_of(deps, procedure_id),
            engagement,
            state,  # type: ignore[arg-type]
            datetime.now(UTC),
            traits=DatasetTraits(),
            profile=profile,
        )
        result = await deps.rules.run(engagement.id, template, run, run.params_snapshot)
        found[procedure_id] = [(x.source_ids[0], x.amount_cents) for x in result.exceptions if x.rule_id == rule_id]
        if procedure_id == "REV-08":
            assert result.populations == numbat_rev08_populations()
    assert found == {
        "PLN-07": [("1100", 52500000)],
        "REV-08": [("SI-2026-0612", 14200000)],
        "CSH-05": [("BA-01", -5000000)],
    }


async def test_snapshot_only_load_differs_only_in_the_supplements(tmp_path: Path) -> None:
    """验收 12: 另一项目用 XeroDatasetLoader.load 只载快照, 与按补造清单写入的项目逐表比较 (全部账表与工资册):
    应付账单两边各 6 行相同; 差异只有发运单 (0 与 24 行), 销售发票的合同关联 (6 张), 银行账户的开户行 (2 户) 与工资册
    (0 与 3 行)."""
    snapshot, supplements = snapshot_dir(SNAPSHOT_NAME), supplements_dir(SNAPSHOT_NAME)
    url = database_url()
    written = await build_xero_project(url, snapshot, supplements, tmp_path)
    loaded = await written.deps.engagement_store.create(  # type: ignore[attr-defined]
        owner_id=written.owner,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        dataset=written.engagement.dataset,
        profile=written.profile.model_copy(update={"id": "CLIENT-" + uuid4().hex}),
    )
    await written.datasets.load(loaded.id, XERO_DATASET)
    engine = written.deps.data.engine  # type: ignore[attr-defined]
    tables = {
        name: (await table_rows(engine, written.engagement.id, name), await table_rows(engine, loaded.id, name))
        for name in m.FINANCIAL
    }
    differing = {name for name, (a, b) in tables.items() if a != b}
    assert differing == {"delivery_notes", "sales_invoices", "bank_accounts", "employees"}
    assert len(tables["supplier_bills"][0]) == 6
    assert [len(tables[name][0]) - len(tables[name][1]) for name in ("delivery_notes", "employees")] == [24, 3]
    assert not tables["delivery_notes"][1] and not tables["employees"][1]

    def changed_columns(name: str) -> dict[str, set[str]]:
        """两项目同 ID 的行里值不同的列 → 行 ID."""
        a, b = ({r["id"]: r for r in rows} for rows in tables[name])
        assert a.keys() == b.keys(), name
        found: dict[str, set[str]] = {}
        for key in a:
            for column in a[key]:
                if a[key][column] != b[key][column]:
                    found.setdefault(column, set()).add(key)
        return found

    invoices = changed_columns("sales_invoices")
    assert list(invoices) == ["contract_id"] and len(invoices["contract_id"]) == 6
    banks = changed_columns("bank_accounts")
    assert list(banks) == ["bank_name"] and banks["bank_name"] == {"BA-090", "BA-091"}
