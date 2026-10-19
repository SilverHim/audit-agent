"""需求 R268 演示导入清单 (xero_supplements.plan): 补造覆盖的账表与按阶段上传的文件, 不连库; 补造目录不匹配或
清单内容不合规时拒绝. 篡改只在 tmp 副本上做 (tests.test_xero_supplements 的 copies 与 rewrite_manifest)."""

import json
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from backend.adapters.xero_supplements import DemoPlan, SupplementMismatch, plan
from backend.contracts.data import TableKind
from tests.external import require_xero_snapshot, require_xero_supplements
from tests.test_employees import EXPECTED as EMPLOYEES
from tests.test_xero_supplements import DATASET, LINKED, copies, loader, rewrite_manifest

pytestmark = pytest.mark.external


@pytest.fixture(scope="module")
def q1() -> DemoPlan:
    require_xero_snapshot("20261017-q1")
    require_xero_supplements("20261017-q1")
    return plan(loader(), DATASET)


def test_tables_are_the_supplement_tables_with_their_sources(q1: DemoPlan) -> None:
    """补造覆盖的账表按 TableKind 顺序, 各带来源文件; 与 XeroDatasetLoader.supplement_tables 相同."""
    assert [(t.kind, t.file_name, len(t.rows)) for t in q1.tables] == [
        (TableKind.sales_invoices, "contract_links.csv", 42),
        (TableKind.bank_accounts, "bank_accounts.csv", 2),
        (TableKind.delivery_notes, "delivery_notes.csv", 24),
        (TableKind.employees, "payroll/employees.csv", 3),
    ]
    invoices, banks, notes, staff = (t.rows for t in q1.tables)
    assert {i.id: i.contract_id for i in invoices if i.contract_id} == LINKED  # type: ignore[attr-defined]
    assert {a.bank_name for a in banks} == {"Wattlebrook Bank Limited"}  # type: ignore[attr-defined]
    note = next(n for n in notes if n.id == "SR-ORC1026")  # type: ignore[attr-defined]
    assert note.invoice_id == "SI-77b338ef-ecc0-4b95-a0d7-2617b0054611"  # type: ignore[attr-defined]
    assert staff == EMPLOYEES
    assert loader().supplement_tables(DATASET) == q1.tables


def test_uploads_follow_stages_and_match_the_manifest(q1: DemoPlan) -> None:
    """23 份按阶段 (承接, 等数据, 完成阶段按补交请求, 人工任务) 再按文件名排序; 登记 mock 与函证 mock 存档的 6 份
    不在其中; 各项的类型, 可靠性, 文件日期, 关联对象与文件 ID 同 manifest.documents."""
    assert Counter(u.stage for u in q1.uploads) == {"acceptance": 2, "fieldwork": 19, "statements": 1, "manual": 1}
    assert [u.file_name for u in q1.uploads[:3]] == ["appointment.pdf", "engagement_letter.pdf", "CON-X01.pdf"]
    assert [u.stage for u in q1.uploads[-2:]] == ["statements", "manual"]
    assert [(u.path, u.request_id) for u in q1.uploads if u.request_id] == [
        ("financial_statements/fs_v1.pdf", "REQ-financial_statements")
    ]
    manifest = json.loads((require_xero_supplements("20261017-q1") / "manifest.json").read_text(encoding="utf-8"))
    keep = ("reliability", "document_date", "related_ids", "document_id")  # 与 manifest 同名的项; type 改名
    recorded = {
        d["path"]: {"document_type": d["type"], **{k: d[k] for k in keep}}
        for d in manifest["documents"]
        if d["registration"] not in ("registry", "confirmation")
    }
    assert {u.path: u.model_dump(mode="json", include={"document_type", *keep}) for u in q1.uploads} == recorded
    smart = next(u for u in q1.uploads if u.file_name == "smart_agency_invoice_SM0195-70135.pdf")
    assert (smart.document_type, smart.related_ids) == ("invoice", ["XJ-548", "BT-090-0097"])


def manifest_documents(edit: Callable[[dict[str, object]], None], path: str) -> Callable[[Path, Path], None]:
    """改副本 manifest.documents 中 path 那一项 (manifest.json 不记哈希, 改后补造核对照常通过)."""

    def change(api: Path, demo: Path) -> None:
        file = demo / "20261017-q1" / "manifest.json"
        manifest = json.loads(file.read_text(encoding="utf-8"))
        edit(next(d for d in manifest["documents"] if d["path"] == path))
        file.write_text(json.dumps(manifest), encoding="utf-8")

    return change


def index_row(api: Path, demo: Path) -> None:
    """index.csv 一行的关联对象与 manifest 不同, 重写副本 manifest 的哈希 (documents 段仍按规格)."""
    index = demo / "20261017-q1" / "documents" / "index.csv"
    index.write_text(index.read_text(encoding="utf-8").replace("XJ-548;BT-090-0097", "XJ-548"), encoding="utf-8")
    rewrite_manifest(api, demo)


def tampered(api: Path, demo: Path) -> None:
    (demo / "20261017-q1" / "delivery_notes.csv").write_text("id\n", encoding="utf-8")  # 不重写哈希


SMART = "documents/evidence/smart_agency_invoice_SM0195-70135.pdf"


@pytest.mark.parametrize(
    ("change", "error", "message"),
    [
        (tampered, SupplementMismatch, "补造文件与快照不匹配: delivery_notes.csv"),
        (manifest_documents(lambda d: d.update(type="supplier_invoice"), SMART), ValueError, f"{SMART}: 类型"),
        (manifest_documents(lambda d: d.update(registration="upload"), SMART), ValueError, f"{SMART}: 登记途径"),
        (index_row, ValueError, f"documents/index.csv 的 {SMART} 与 manifest.documents 不符"),
    ],
    ids=["mismatch", "type", "registration", "index"],
)
def test_plan_rejects_bad_supplements(
    tmp_path: Path, change: Callable[[Path, Path], None], error: type[Exception], message: str
) -> None:
    """补造目录不匹配抛 SupplementMismatch; 清单内容不合规抛 ValueError (不是 SupplementMismatch), 消息含该文件."""
    require_xero_snapshot("20261017-q1")
    api, demo = copies(tmp_path)
    change(api, demo)
    with pytest.raises(error, match=re.escape(message)) as raised:
        plan(loader(api, demo), DATASET)
    assert (type(raised.value) is SupplementMismatch) == (error is SupplementMismatch)


def test_plan_needs_a_supplement_directory() -> None:
    require_xero_snapshot("20261017-mock")
    with pytest.raises(FileNotFoundError, match="xero/20261017-mock 没有补造目录"):
        plan(loader(), "xero/20261017-mock")
