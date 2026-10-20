"""R94 人工队列接口 (接口文档第 10 节): GET /manual-tasks, GET /review-tasks 的 kind 过滤, 经 POST /review-records
提交 manual_result. 项目停在 TST-M01 (取得声明书, 结果模型 TstLetter) 的节点内等待, 装配同 test_manual."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute

from backend.api.routes import manual
from backend.contracts.models import DocumentRef, DocumentType, ReviewTask, TaskStatus
from backend.main import operation_id
from backend.orchestration.gates import REPORT_TZ
from backend.services.judgment_engine import DOCUMENT_EVIDENCE
from tests.fakes import ENGAGEMENT, api_client, wait_idle
from tests.test_manual import (  # noqa: F401 (registered 为自动夹具: 登记测试的结果模型与 owner)
    INSTRUCTIONS,
    LETTER,
    MATERIAL,
    TASK,
    TstLetter,
    at_manual,
    letter_result,
    registered,
)

EID = ENGAGEMENT.id
BASE = f"/api/v1/engagements/{EID}"
DRAFT_FS = DocumentRef(  # 待审财报, 不作证据 (DOCUMENT_EVIDENCE 没有它), 人工队列不列
    id="DOC-fs0000000001",
    type=DocumentType.financial_statements,
    file_name="fs_v1.pdf",
    sha256="f" * 64,
    page_count=3,
    reliability=LETTER.reliability,
    status="classified",
    uploaded_at=LETTER.uploaded_at,
)


async def test_manual_tasks_list_request_schema_and_evidence_documents(tmp_path: Path) -> None:
    """验收 1, 3: 只列 manual 任务, 待决在前, 两组各按创建时间升序, 不按写入顺序 (另放四条, 在 TASK 之后依次写入:
    已决定的 T-OLDER, 晚 1 分钟的待决 T-LATER, 早 1 分钟的待决 T-EARLIER 与早 1 分钟的已决定 T-DONE-EARLIER); 每项
    带程序名, owner, 请求, TstLetter 的 JSON schema, 本项目可作证据的文件 (待审财报不列) 与其指针类型. review-tasks
    按 kind 过滤: normal 只有 normal 任务, manual 只有它们, 不带 kind 时同现状含全部. 项目不存在 404."""
    s = await at_manual(tmp_path)
    s.runner.deps.financial_store.document_refs.append(DRAFT_FS)  # type: ignore[attr-defined]
    [task] = [t for t in await s.output_store.latest(EID, ReviewTask) if t.id == TASK]
    minute = timedelta(minutes=1)
    older = task.model_copy(update={"id": "T-OLDER-v1", "status": TaskStatus.decided})
    later = task.model_copy(update={"id": "T-LATER-v1", "created_at": task.created_at + minute})
    earlier = task.model_copy(update={"id": "T-EARLIER-v1", "created_at": task.created_at - minute})
    done = older.model_copy(update={"id": "T-DONE-EARLIER-v1", "created_at": task.created_at - minute})
    async with s.output_store.transaction(EID):
        await s.output_store.put(EID, [older, later, earlier, done], actor="A10")
    evidence = [
        d.id
        for d in await s.runner.deps.financial_store.documents(EID)
        if d.type in DOCUMENT_EVIDENCE  # 判断引擎的文件指针映射, 同提交时的核对
    ]

    async with api_client(s.runner) as client:
        listed = (await client.get(f"{BASE}/manual-tasks")).json()
        normal = (await client.get(f"{BASE}/review-tasks", params={"kind": "normal"})).json()
        manual = (await client.get(f"{BASE}/review-tasks", params={"kind": "manual"})).json()
        everything = (await client.get(f"{BASE}/review-tasks")).json()
        missing = await client.get("/api/v1/engagements/ENG-NOPE/manual-tasks")

    assert [(v["task"]["id"], v["task"]["status"]) for v in listed] == [
        ("T-EARLIER-v1", "pending"),
        (TASK, "pending"),
        ("T-LATER-v1", "pending"),
        ("T-DONE-EARLIER-v1", "decided"),
        ("T-OLDER-v1", "decided"),
    ]
    [view] = [v for v in listed if v["task"]["id"] == TASK]
    assert (view["procedure_name"], view["owner"]) == ("自测人工程序", "A10")
    assert view["request"] == {
        "instructions": INSTRUCTIONS,
        "result_model": "TstLetter",
        "min_evidence": 2,
        "material": MATERIAL,
    }
    assert view["result_schema"] == TstLetter.model_json_schema()
    assert view["result_schema"]["properties"]["signed_on"] == {
        "format": "date",
        "title": "Signed On",
        "type": "string",
    }
    ids = [d["id"] for d in view["documents"]]
    assert ids == evidence and LETTER.id in ids and DRAFT_FS.id not in ids
    assert view["evidence_types"][LETTER.id] == "voucher"  # 声明书作证据为内部凭证 (DOCUMENT_EVIDENCE)
    assert set(view["evidence_types"]) == set(ids)
    assert {t["task"]["kind"] for t in normal} == {"normal"}
    assert sorted(t["task"]["id"] for t in manual) == sorted(v["task"]["id"] for v in listed)
    assert {t["task"]["id"] for t in manual} | {t["task"]["id"] for t in normal} <= {
        t["task"]["id"] for t in everything
    }
    assert missing.status_code == 404


def test_operation_id_gives_the_frontend_function_name() -> None:
    """验收 1: tag manual, 函数名 list_manual_tasks, 生成的前端函数为 manualListManualTasks (main.operation_id 即
    FastAPI 的 generate_unique_id_function, 不必生成整份 OpenAPI)."""
    [route] = manual.router.routes
    assert isinstance(route, APIRoute) and operation_id(route) == "manual-list_manual_tasks"


async def test_submit_manual_result_through_review_records(tmp_path: Path) -> None:
    """验收 4: 经 POST /review-records 提交 manual_result (同 R92 验收 5 的内容, 执行日期取当天的悉尼日期) → 202,
    流程继续到 FIN-01 闸门; 校验失败的错误码与 HTTP 映射同现有 (页码超出 → 422 INVALID_VALUE)."""
    s = await at_manual(tmp_path)
    today = datetime.now(REPORT_TZ).date()
    result: dict[str, Any] = letter_result(performed_on=today).model_dump(mode="json")
    body = {
        "task_id": TASK,
        "target_version": 1,
        "decision": "manual_result",
        "reason": "Letter received from the CFO",
        "manual_result": result,
    }
    body_page, signature_page = result["evidence"]
    bad = {**body, "manual_result": {**result, "evidence": [body_page, {**signature_page, "page": 3}]}}

    async with api_client(s.runner) as client:
        rejected = await client.post(f"{BASE}/review-records", json=bad)
        accepted = await client.post(f"{BASE}/review-records", json=body)
        await wait_idle(s.runner, EID)

    assert (rejected.status_code, rejected.json()["detail"]["code"]) == (422, "INVALID_VALUE")
    assert "页码 3 不在 1-2 之间" in rejected.json()["detail"]["message"]
    assert accepted.status_code == 202
    assert s.runner.run_error(EID) is None
    assert (await s.next_nodes()) == ("gate_fin",)
    [record] = [r for r in s.output_store.rows if getattr(r, "task_id", None) == TASK]
    assert (record.manual_result.performed_on, record.manual_result.details) == (today, result["details"])
    assert {p.type for p in record.manual_result.evidence} == {"voucher"}  # 按声明书的登记改写 (R92)
