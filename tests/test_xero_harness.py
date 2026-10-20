"""Xero 程序级测试台的自测 (需求 R118 验收 5): T0 副本上离线跑收入一串 (REV-08 → REV-03 → REV-02), 分层 mock 与按键
分流, 账表差异; 连库一例 (slow) 跑 db_deps. 副本要本地快照 (没有时跳过), 都标 external."""

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from backend.contracts.data import TableKind
from backend.contracts.models import ProcedureRun, ProposedAction, ReviewStatus, RunStatus, TrailKind
from tests.fixture_sets import XERO
from tests.tools.live_budget import Budget
from tests.xero_fakes import ORC1026
from tests.xero_harness import (
    LayeredMockLLMClient,
    Ran,
    RoutedLLMClient,
    TableDiff,
    db_deps,
    imported_tables,
    offline_deps,
    request_text,
    run_programs,
    table_diff,
)
from tests.xero_injection import Patch, XeroCase

pytestmark = pytest.mark.external

REVENUE = ["REV-02"]  # 依赖闭包为 REV-08, REV-03 (与 DOC-04), REV-02
CONTRACTS = ["CON-X01", "CON-X02", "CON-X03"]
INVOICES = [  # S01 选中的六张发票, 按发票 ID (REV-02 判断对象的次序)
    "SI-3b28bf11-ed2f-4cf4-8e9e-fcae730cc292",
    "SI-5aadcd34-01a9-4b8d-a2bb-d7cc1de9fa45",
    "SI-5f6deadf-36a2-495a-9980-ceb11e8af9a9",
    ORC1026,
    "SI-a9f765e6-b9bc-4505-a47b-fb3ecb327e7b",
    "SI-bcd8a71f-aa31-4d0f-8a01-13ea26363ddf",
]
CALLS = [("REV-03", c) for c in CONTRACTS] + [("REV-02", i) for i in INVOICES]  # 收入一串的大模型调用, 按次序
LANDLORD = "27900043030"  # 补造登记里的出租方 Victoria Property Agency Pty Ltd
PROBE = Patch.model_validate(  # 自测用的补丁: 账表与补造登记各改一处, 看假存储是否取副本
    {
        "scenario": "R118-probe",
        "title": "服务完成日改到期后, 出租方登记名改名",
        "ops": [
            {
                "op": "set",
                "file": "xero_demo/delivery_notes.csv",
                "row": "SR-ORC1026",
                "field": "signed_date",
                "before": "2026-09-17",
                "after": "2026-10-02",
            },
            {
                "op": "set",
                "file": "xero_demo/registry/abn.json",
                "key": LANDLORD,
                "field": "entity_name",
                "before": "Victoria Property Agency Pty Ltd",
                "after": "Renamed Property Agency Pty Ltd",
            },
        ],
    }
)


@pytest.fixture
def t0(xero_case: Any) -> XeroCase:
    built: XeroCase = xero_case("T0")
    return built


def fixture(procedure_id: str, key: str) -> dict[str, Any]:
    """llm_xero 正本中的一份 (R109 REV-03, R110 REV-02)."""
    value: dict[str, Any] = json.loads((XERO.fixtures / procedure_id / f"{key}.json").read_text(encoding="utf-8"))
    return value


def llm_calls(ran: Ran) -> list[tuple[str, str, int, str | None]]:
    """大模型调用的留痕: (程序, fixture_key, attempt, 失败级别), 按调用次序."""
    entries = [e for e in ran.deps.trail.entries if e.kind is TrailKind.llm_call]  # type: ignore[attr-defined]
    return [
        (e.procedure_id, e.payload["fixture_key"], e.payload["attempt"], e.payload["level_failed"]) for e in entries
    ]


async def test_t0_revenue_matches_the_fixtures(t0: XeroCase) -> None:
    """T0 副本上 offline_deps 带 21 份文件登记; run_programs(["REV-02"]): 先跑的 DOC-04 沿用导入时抽出的合同 (不调
    大模型), 重要性按副本的收入为 32431 / 16216 / 1622; REV-08 只有 R03-1 (330000); REV-03 三条与 REV-02 六条判断的
    结论与建议同 R109, R110 的 fixture, 每次大模型调用 attempt 1 且都读副本的运行副本; REV-03 的判断在 REV-02 之前
    自动确认 (v2 confirmed), REV-02 的仍待复核; 执行都成功."""
    client = LayeredMockLLMClient([t0.supplements / "llm"])
    h = await offline_deps(t0, llm=client)
    assert len(h.state["documents"]) == 21  # acceptance 2 与 fieldwork 19 份上传
    ran = await run_programs(h, REVENUE)
    state = ran.state
    assert (state["materiality_cents"], state["performance_materiality_cents"], state["clearly_trivial_cents"]) == (
        32431,
        16216,
        1622,
    )
    assert sorted((r.procedure_id, r.status) for r in state["procedure_runs"]) == [
        (pid, RunStatus.succeeded) for pid in ("DOC-04", "PLN-01", "PLN-07", "REV-02", "REV-03", "REV-08")
    ]
    assert [(x.id, x.amount_cents) for x in state["exceptions"] if x.procedure_id == "REV-08"] == [
        ("X-REV-08-1-R03-1", 330000)
    ]
    assert llm_calls(ran) == [(pid, key, 1, None) for pid, key in CALLS]
    assert [(r.procedure_id, r.fixture_key, r.root, r.file) for r in client.reads] == [
        (pid, key, t0.supplements / "llm", f"{pid}/{key}.json") for pid, key in CALLS
    ]
    judgments = {(j.procedure_id, j.subject_id): j for j in state["judgments"]}
    assert sorted(judgments) == sorted(CALLS)
    for (pid, key), j in judgments.items():
        expected = fixture(pid, key)
        assert (j.conclusion, j.proposed_action) == (expected["conclusion"], expected["proposed_action"]), key
        assert (j.version, j.review_status) == (
            (2, ReviewStatus.confirmed) if pid == "REV-03" else (1, ReviewStatus.pending)
        ), key


async def test_overlay_root_comes_first(t0: XeroCase, tmp_path: Path) -> None:
    """overlay 根里放一份 ORC1026 的 REV-02 输出 (结论另加一句): 该判断取 overlay 的, 记为读自 overlay; 其余八次读
    副本的运行副本."""
    overlay = tmp_path / "overlay"
    changed = fixture("REV-02", ORC1026) | {"conclusion": fixture("REV-02", ORC1026)["conclusion"] + " (overlay)"}
    (overlay / "REV-02").mkdir(parents=True)
    (overlay / "REV-02" / f"{ORC1026}.json").write_text(json.dumps(changed), encoding="utf-8")
    client = LayeredMockLLMClient([overlay, t0.supplements / "llm"])
    ran = await run_programs(await offline_deps(t0, llm=client), REVENUE)
    [j] = [j for j in ran.state["judgments"] if j.subject_id == ORC1026]
    assert j.conclusion == changed["conclusion"] and j.proposed_action is ProposedAction.accept
    assert [(r.fixture_key, r.root) for r in client.reads] == [
        (key, overlay if key == ORC1026 else t0.supplements / "llm") for _, key in CALLS
    ]


async def test_routed_client_sends_only_the_live_keys(t0: XeroCase) -> None:
    """RoutedLLMClient 只把 (REV-02, ORC1026) 交给假 live 客户端 (读正本 llm_xero), 计入预算 (上限 1 次, 多发即超限),
    其余八次走 mock; 记下的请求全文含该发票的分录行 XJ-580-1 (R100 的上下文), 别的发票的请求不含."""
    mock = LayeredMockLLMClient([t0.supplements / "llm"])
    live = LayeredMockLLMClient([XERO.fixtures])
    budget = Budget(max_calls=1)
    client = RoutedLLMClient(mock, live, {("REV-02", ORC1026)}, budget)
    ran = await run_programs(await offline_deps(t0, llm=client), REVENUE)
    assert [(route, r.procedure_id, r.fixture_key) for route, r in client.requests] == [
        ("live" if key == ORC1026 else "mock", pid, key) for pid, key in CALLS
    ]
    assert [(r.procedure_id, r.fixture_key) for r in live.requests] == [("REV-02", ORC1026)]
    assert [(r.procedure_id, r.fixture_key) for r in mock.requests] == [c for c in CALLS if c[1] != ORC1026]
    assert (budget.used.calls, budget.exceeded) == (1, False)
    assert llm_calls(ran) == [(pid, key, 1, None) for pid, key in CALLS]
    texts = {r.fixture_key: request_text(r) for _, r in client.requests}
    assert "XJ-580-1" in texts[ORC1026]
    assert not any("XJ-580-1" in text for key, text in texts.items() if key != ORC1026)


def test_t0_tables_equal_the_original(t0: XeroCase) -> None:
    """T0 副本导入后的各表 (八张与工资册) 与原目录的逐行相同: table_diff 为空."""
    assert table_diff(imported_tables(), imported_tables(t0.loader)) == {}


async def test_offline_deps_read_the_copy(xero_case: Any) -> None:
    """offline_deps 的数据取副本而不是原目录: 补丁改了服务完成记录与出租方的 ABN 登记各一处; table_diff 只列出服务完成
    记录那一行 (登记不是账表), 假财务存储与假登记取到的是改后的值."""
    case: XeroCase = xero_case(PROBE)
    assert table_diff(imported_tables(), imported_tables(case.loader)) == {
        TableKind.delivery_notes: TableDiff(
            added=[], deleted=[], changed={"SR-ORC1026": {"signed_date": (date(2026, 9, 17), date(2026, 10, 2))}}
        )
    }
    h = await offline_deps(case)
    note = await h.deps.financial_store.record(h.engagement.id, "delivery_note", "SR-ORC1026")
    record = await h.deps.registry.abn(h.engagement.id, LANDLORD)
    assert note is not None and note["signed_date"] == date(2026, 10, 2)
    assert record is not None and record.entity_name == "Renamed Property Agency Pty Ltd"


def test_table_diff_lists_rows_and_fields() -> None:
    """自测: 改一行的一个字段 (SR-ORC1026 的 signed_date), 删一行科目 (按 code), 改一行试算表 (按期间与科目),
    加一位员工, 各表只列出这些; 行 ID 重复时报错."""
    original = imported_tables()
    copy = {kind: list(rows) for kind, rows in original.items()}
    notes = copy[TableKind.delivery_notes]
    n = next(i for i, r in enumerate(notes) if r.id == "SR-ORC1026")
    notes[n] = notes[n].model_copy(update={"signed_date": date(2026, 10, 2)})
    copy[TableKind.accounts] = [a for a in copy[TableKind.accounts] if a.code != "260"]
    balances = copy[TableKind.trial_balance]
    m = next(i for i, r in enumerate(balances) if (r.period, r.account_code) == ("current", "090"))
    balances[m] = balances[m].model_copy(update={"balance_cents": balances[m].balance_cents + 650000})
    staff = copy[TableKind.employees]
    staff.append(staff[0].model_copy(update={"employee_id": "E999"}))
    assert table_diff(original, copy) == {
        TableKind.accounts: TableDiff(added=[], deleted=["260"], changed={}),
        TableKind.delivery_notes: TableDiff(
            added=[], deleted=[], changed={"SR-ORC1026": {"signed_date": (date(2026, 9, 17), date(2026, 10, 2))}}
        ),
        TableKind.employees: TableDiff(added=["E999"], deleted=[], changed={}),
        TableKind.trial_balance: TableDiff(
            added=[],
            deleted=[],
            changed={"current:090": {"balance_cents": (-1090044, -440044)}},
        ),
    }
    with pytest.raises(ValueError, match="E001 重复"):
        table_diff(original, {TableKind.employees: [staff[0], staff[0]]})


@pytest.mark.slow
async def test_db_harness_on_t0(t0: XeroCase) -> None:
    """连库 (需 TEST_DATABASE_URL): db_deps 在 T0 副本上新建负责人 Jordan Ellis 与项目, 演示数据导入两阶段后 21 份
    文件已登记, 数据已确认 (确认人为负责人); run_programs(["REV-08"]) 用真实 SQL: 重要性 32431 / 16216 / 1622,
    REV-08 只有 R03-1 (330000); 执行都已写入存储."""
    h = await db_deps(t0)
    assert len(h.state["documents"]) == 21
    owner = await h.deps.users.get(h.actor)
    assert owner is not None and owner.full_name == "Jordan Ellis"
    readiness = await h.deps.data.readiness(h.engagement.id)
    assert readiness.ready_at is not None and readiness.ready_by == h.actor
    ran = await run_programs(h, ["REV-08"])
    state = ran.state
    assert (state["materiality_cents"], state["performance_materiality_cents"], state["clearly_trivial_cents"]) == (
        32431,
        16216,
        1622,
    )
    assert [(x.id, x.amount_cents) for x in state["exceptions"] if x.procedure_id == "REV-08"] == [
        ("X-REV-08-1-R03-1", 330000)
    ]
    stored = await h.deps.output_store.latest(h.engagement.id, ProcedureRun)
    assert sorted((r.id, r.status) for r in stored) == sorted((r.id, r.status) for r in state["procedure_runs"])
