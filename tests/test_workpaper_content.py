"""底稿内容快照的真实数据与版本行为 (R331), 不新增完整走通."""

import pytest

from backend.contracts.models import LocalizedText, RunPopulation, Workpaper, WorkpaperStatus
from backend.services.workpaper_content import build_content
from backend.services.workpapers import load, refresh, view_of
from tests.fakes import api_client
from tests.workpaper_cases import d3_case

pytestmark = pytest.mark.external


async def test_three_way_snapshot_records_the_six_invoices() -> None:
    _, _, paper = await d3_case()
    content = paper.content
    assert content is not None
    assert (content.header.client_name, str(content.header.period_start), str(content.header.period_end)) == (
        "Demo Company (AU) Pty Ltd",
        "2026-07-01",
        "2026-09-30",
    )
    assert [section.id for section in content.sections] == [
        "objectives",
        "basis",
        "procedures",
        "checks",
        "details",
        "exceptions",
        "conclusion",
        "cross_references",
        "review",
    ]
    details = next(section for section in content.sections if section.id == "details").tables[0]
    assert [row["invoice_no"] for row in details.rows] == [
        "ORC1024",
        "ORC1020",
        "ORC1021",
        "ORC1022",
        "ORC1023",
        "ORC1026",
    ]
    assert sum(row["amount_cents"] for row in details.rows) == 990000
    assert sum(row.get("received_cents", 0) for row in details.rows) == 605000
    checks = [col for col in details.columns if col.key.startswith("check_")]
    assert [col.label.en for col in checks] == [
        "Recorded in the correct period",
        "Service completion record exists",
        "Receipts agree to the invoice",
    ]
    receipt = checks[2].key
    assert [row[receipt] for row in details.rows] == ["—", "✓", "✓", "✓", "✓", "✗"]
    assert details.rows[-1]["journal_entry_id"] == "XJ-580-2"
    assert details.rows[-1]["remarks"] == LocalizedText(zh="例外 a", en="Exception a")
    attention = details.rows[0]["remarks"]
    assert isinstance(attention, LocalizedText) and "not assessed" in attention.en
    exception = next(section for section in content.sections if section.id == "exceptions").tables[0].rows[0]
    assert (exception["id"], exception["difference_cents"], exception["marker"]) == ("X-REV-08-1-R03-1", 330000, "a")
    assert "description" not in exception  # 说明由双语检查项、对象与差额呈现, 不搬中文例外描述.


async def test_content_is_pure_and_old_versions_stay_independent() -> None:
    runner, store, first = await d3_case()
    eid = first.content.header.engagement_id
    snapshot = await load(runner.deps, eid)
    template = snapshot.templates["REV-08"]
    assert build_content(view_of(template, snapshot.sources), snapshot.sources, first) == first.content
    frozen = first.model_dump(mode="json")
    async with api_client(runner) as client:
        signed = await client.post(f"/api/v1/engagements/{eid}/workpapers/D-3/signoff", json={"scope": "sample"})
        assert signed.status_code == 200
    [population] = await store.latest(eid, RunPopulation)
    rules = list(population.rules)
    rules = [
        rule.model_copy(
            update={
                "items": [
                    item.model_copy(update={"fields": {**item.fields, "customer": "Synthetic revised customer"}})
                    for item in rule.items
                ]
            }
        )
        for rule in rules
    ]
    await store.put(eid, [population.model_copy(update={"version": 2, "rules": rules})], actor="A6")
    [revised] = [p for p in await refresh(runner.deps, eid) if p.ref == "D-3"]
    assert revised.status is WorkpaperStatus.needs_rereview and revised.signoff is None
    assert revised.content != first.content
    details = next(section for section in revised.content.sections if section.id == "details").tables[0]
    assert {row["customer"] for row in details.rows} == {"Synthetic revised customer"}
    assert (await store.get(eid, Workpaper, first.id, 1)).model_dump(mode="json") == frozen
    assert await refresh(runner.deps, eid) == []


async def test_missing_population_is_visible_and_marks_required_item() -> None:
    runner, store, first = await d3_case()
    eid = first.content.header.engagement_id
    [population] = await store.latest(eid, RunPopulation)
    missing = population.rules[0].model_copy(update={"count": None, "amount_cents": None, "items": []})
    await store.put(
        eid, [population.model_copy(update={"version": 2, "rules": [missing, *population.rules[1:]]})], actor="A6"
    )
    [revised] = [p for p in await refresh(runner.deps, eid) if p.ref == "D-3"]
    assert "tested_items" in revised.missing
    details = next(section for section in revised.content.sections if section.id == "details")
    assert any(
        "not recorded" in paragraph.en for paragraph in details.paragraphs if isinstance(paragraph, LocalizedText)
    )


async def test_multiple_check_exceptions_keep_every_footnote() -> None:
    """同一张发票在两项检查出例外时, 备注保留两条引用, 不由最后一项覆盖."""
    from backend.contracts.models import AuditException

    runner, store, first = await d3_case()
    eid = first.content.header.engagement_id
    [original] = await store.latest(eid, AuditException)
    additional = original.model_copy(update={"id": "X-REV-08-1-R02-1", "rule_id": "R02"})
    await store.put(eid, [additional], actor="A6")
    [changed] = [p for p in await refresh(runner.deps, eid) if p.ref == "D-3"]
    data = next(s for s in changed.content.sections if s.id == "details").tables[0]
    row = next(r for r in data.rows if r["invoice_no"] == "ORC1026")
    assert row["remarks"] == LocalizedText(zh="例外 a, b", en="Exception a, b")
    assert row["check_RUN-REV-08-1_R02"] == row["check_RUN-REV-08-1_R03"] == "✗"


def test_legacy_account_resolution_requires_an_unambiguous_match() -> None:
    """旧错报无独立代码时能读括号代码, 同名冲突或明确的无效代码不分摊到猜测科目."""
    from backend.contracts.records import Account
    from backend.services.workpaper_leads import account_code

    accounts = {
        code: Account(code=code, name="Interest income", type="revenue", category="other_income")
        for code in ("010", "020")
    }
    assert account_code("Interest income", None, accounts) is None
    assert account_code("Interest income (010)", None, accounts) == "010"
    assert account_code("Interest income", "020", accounts) == "020"
    assert account_code("Interest income (010)", "999", accounts) is None


def test_classification_acceptance_does_not_mean_no_misstatement() -> None:
    """接受已认定错报的分类与判为不是错报含义不同, 底稿不能用同一句话表示."""
    from backend.contracts.models import Judgment, ProposedAction, Verdict
    from backend.services.workpaper_judgments import action_label

    classification = Judgment.model_construct(verdict=None, proposed_action=ProposedAction.accept)
    assert action_label(classification).en == "Accept"
    clear = classification.model_copy(update={"verdict": Verdict.not_misstatement})
    assert action_label(clear).en == "Not a misstatement"
