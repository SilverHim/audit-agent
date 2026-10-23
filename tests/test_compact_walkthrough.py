"""独立精简演示经真实存储、SQL 规则和专属 mock 模型输出完成闭环, 不连接真实模型."""

from collections import defaultdict
from pathlib import Path

import pytest

from backend.contracts.models import AuditReportVersion, Misstatement, MisstatementStatus, Phase, ReportStatus
from tests.fakes import api_client, wait_idle
from tests.test_e2e_mock import real_storage
from tests.xero_flow import drain, xero_link

DATA = Path(__file__).resolve().parents[1] / "data/demo-compact"


@pytest.mark.slow
@pytest.mark.parametrize("scenario", ["normal", "evidence", "returns"])
async def test_compact_demo_authorized_batches_to_signed_report(tmp_path: Path, scenario: str) -> None:
    async with real_storage(
        tmp_path,
        dataset="demo-compact",
        load_dataset=False,
        fresh_profile=False,
        FEATURES="critic,collab,journals,misstatement,planning,more_evidence",
    ) as run:
        service = run.data_service()
        runner = run.s.runner
        await runner.start(run.eid)
        counts: dict[str, set[str]] = defaultdict(set)
        uploaded = False
        returned: set[str] = set()
        evidence_processed = False
        async with api_client(runner, run.owner, xero=xero_link(run.deps, service)) as client:
            base = f"/api/v1/engagements/{run.eid}"

            async def post(path: str, body: dict) -> dict:
                response = await client.post(base + path, json=body)
                assert response.status_code in (200, 201, 202), response.text
                return response.json()

            for _ in range(45):
                await drain(service)
                await wait_idle(runner, run.eid)
                overview = (await client.get(base)).json()
                assert overview["run_error"] is None, overview["run_error"]
                if overview["engagement"]["phase"] == "Archived":
                    break
                pending = (await client.get(base + "/review-tasks?status=pending")).json()
                for view in pending:
                    counts[view["task"]["procedure_id"]].add(view["task"]["id"])
                if any(v["task"]["kind"] == "manual" for v in pending):
                    if not uploaded:
                        for name in ("management_response.pdf", "representation_letter.pdf"):
                            response = await client.post(
                                base + "/documents",
                                files=[("files", (name, (DATA / "support" / name).read_bytes(), "application/pdf"))],
                            )
                            assert response.status_code == 200, response.text
                            await drain(service)
                            await wait_idle(runner, run.eid)
                        uploaded = True
                    views = (await client.get(base + "/manual-tasks")).json()
                    view = next(v for v in views if v["task"]["status"] == "pending")
                    responses = [
                        {
                            "misstatement_id": m["id"],
                            "base_version": m["version"],
                            "communicated_at": "2026-07-20T08:00:00+10:00",
                            "communicated_to": "Synthetic directors",
                            "responded_at": "2026-07-20T09:00:00+10:00",
                            "response": "Management declines both corrections."
                            if scenario == "evidence"
                            else "Management agrees to both corrections.",
                            "agrees_to_correct": scenario != "evidence",
                        }
                        for m in view["request"]["material"]["misstatements"]
                    ]
                    await post(
                        "/review-records",
                        {
                            "task_id": view["task"]["id"],
                            "target_version": view["task"]["target_version"],
                            "decision": "manual_result",
                            "manual_result": {
                                "conclusion": "Both proposed corrections communicated and agreed.",
                                "performed_on": "2026-07-20",
                                "details": {"responses": responses},
                            },
                        },
                    )
                    continue
                if pending:
                    if (
                        scenario == "evidence"
                        and not evidence_processed
                        and all(v["task"]["procedure_id"] == "FIN-03" for v in pending)
                    ):
                        for view in pending:
                            await post(
                                "/review-records",
                                {
                                    "task_id": view["task"]["id"],
                                    "target_version": view["task"]["target_version"],
                                    "decision": "more_evidence",
                                    "reason": "Isolated supplementary evidence scenario.",
                                },
                            )
                            await wait_idle(runner, run.eid)
                        plan = await post("/assistant-actions", {"action": "evidence_unavailable"})
                        assert len(plan["targets"]) == 2
                        await post(f"/assistant-actions/{plan['id']}/execute", {})
                        await wait_idle(runner, run.eid)
                        reopened = (await client.get(base + "/review-tasks?status=pending")).json()
                        assert len(reopened) == 2
                        assert (await client.get(base)).json()["awaiting_review"] is True
                        plan = await post(
                            "/assistant-actions",
                            {
                                "action": "confirm_reviews",
                                "target_kind": "review_task",
                                "target_ids": [v["task"]["id"] for v in reopened],
                            },
                        )
                        await post(f"/assistant-actions/{plan['id']}/execute", {})
                        await wait_idle(runner, run.eid)
                        assert runner.run_error(run.eid) is None
                        tasks = (await client.get(base + "/review-tasks?status=pending")).json()
                        assert [v["task"]["procedure_id"] for v in tasks] == ["FIN-08"]
                        evidence_processed = True
                        continue
                    assert all(v["task"]["kind"] == "normal" for v in pending)
                    assert all(v["target"]["proposed_action"] in ("accept", "misstatement") for v in pending)
                    accept = any(v["target"]["proposed_action"] == "accept" for v in pending)
                    ids = [
                        v["task"]["id"]
                        for v in pending
                        if v["target"]["proposed_action"] == ("accept" if accept else "misstatement")
                    ]
                    plan = await post(
                        "/assistant-actions",
                        {
                            "action": "confirm_reviews" if accept else "recognize_misstatements",
                            "target_kind": "review_task",
                            "target_ids": ids,
                        },
                    )
                    path = f"/assistant-actions/{plan['id']}/execute"
                    receipt = await post(path, {})
                    assert await post(path, {}) == receipt
                    continue
                if overview["awaiting_correction"]:
                    await post(
                        "/correction-notices",
                        {
                            "kind": "adjustments_posted",
                            "misstatement_ids": overview["awaiting_correction_ids"],
                            "adjustment_entry_nos": ["AJE-C01", "AJE-C02"],
                        },
                    )
                    continue
                demo = (await client.get(base + "/data/summary")).json()["demo"]
                if demo["suggested"]:
                    await post("/data/demo-import", {"stage": demo["suggested"]})
                    continue
                if overview["awaiting_data"]:
                    await post("/data/ready", {"reason": "Verified synthetic data quality checks."})
                    continue
                gate = (await client.get(base + "/gate")).json()
                assert gate is not None, overview
                body = {k: gate[k] for k in ("gate", "target_id", "target_version")}
                body["decision"] = "approve"
                if scenario == "returns" and gate["gate"] in {"PLN-15", "RPT-01"} and gate["gate"] not in returned:
                    rejection = {
                        **body,
                        "decision": "reject",
                        "reason": "Redraft for clarity, retaining the approved scope and opinion.",
                    }
                    if gate["gate"] == "RPT-01":
                        rejection["annotations"] = [
                            {
                                "section": "basis_for_opinion",
                                "comment": "Please express the existing basis more concisely.",
                            }
                        ]
                    await post("/gate-decisions", rejection)
                    returned.add(gate["gate"])
                    continue
                if gate["gate"] == "PLN-01/03":
                    body["materiality"] = {
                        "basis": "revenue",
                        "pct": 0.01,
                        "performance_pct": 0.5,
                        "clearly_trivial_pct": 0.05,
                    }
                if gate["gate"] == "PLN-15":
                    body["plan_confirmation"] = {
                        "eqr_required": False,
                        "eqr_basis": "Synthetic small private entity.",
                        "auditor_expert_not_required": True,
                    }
                if gate["gate"] == "FIN-01":
                    body["disposition"] = gate["material"]["schedule"]["candidate_opinion"]
                await post("/gate-decisions", body)
            else:
                pytest.fail("精简演示未在预期步数内完成")
        assert (await run.deps.engagement_store.get(run.eid)).phase is Phase.Archived
        statements = await run.deps.output_store.latest(run.eid, Misstatement)
        expected_status = MisstatementStatus.uncorrected if scenario == "evidence" else MisstatementStatus.corrected
        assert len(statements) == 2 and all(m.status is expected_status for m in statements)
        reports = await run.deps.output_store.latest(run.eid, AuditReportVersion)
        assert any(r.status is ReportStatus.signed for r in reports)
        if scenario == "returns":
            assert returned == {"PLN-15", "RPT-01"}
            assert max(r.revision for r in reports) == 2
        expected_counts = {
            "REV-03": 2,
            "GEN-02": 1,
            "CSH-02": 1,
            "REV-02": 2,
            "FIN-03": 2,
            "FIN-08": 1,
            "FIN-09": 2,
        }
        if scenario == "evidence":
            expected_counts.pop("FIN-09")
            assert evidence_processed
            assert any(r.opinion_type.value == "qualified" for r in reports)
        assert {pid: len(ids) for pid, ids in counts.items()} == expected_counts
        await service.shutdown()
        await run.deps.activities.settle()
