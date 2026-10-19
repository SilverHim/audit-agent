"""Xero 演示数据集的补造目录 (R70): 载入与取数前核对 manifest 与快照, 补造画像与计划预置, 测试副本与已载入项目.

用本机的 Xero 快照与入库的补造目录 data/xero_demo/; 没有快照时跳过. 篡改只在 tmp 副本上做, 不改原目录. 不连库.
"""

import hashlib
import json
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.xero_supplements import SupplementMismatch
from backend.contracts.models import ClientProfile
from backend.persistence import stores

REPO = Path(__file__).resolve().parents[1]
SNAPSHOTS = REPO / "data" / "external" / "xero" / "api"
SUPPLEMENTS = REPO / "data" / "xero_demo"
PLANNING = REPO / "src" / "backend" / "knowledge" / "planning"
DATASET = "xero/20261017-q1"
NO_DB: Any = object()  # 本文件不连库
pytestmark = pytest.mark.skipif(not (SNAPSHOTS / "20261017-q1").is_dir(), reason="没有本地 Xero 快照")


def loader(api: Path = SNAPSHOTS, supplements: Path | None = SUPPLEMENTS, **options: Any) -> stores.XeroDatasetLoader:
    return stores.XeroDatasetLoader(api, PLANNING, NO_DB, NO_DB, supplements=supplements, **options)


def copies(tmp_path: Path) -> tuple[Path, Path]:
    """快照与补造目录复制到 tmp, 返回两者的上级目录 (api, xero_demo)."""
    api, demo = tmp_path / "api", tmp_path / "xero_demo"
    shutil.copytree(SNAPSHOTS / "20261017-q1", api / "20261017-q1")
    shutil.copytree(SUPPLEMENTS / "20261017-q1", demo / "20261017-q1")
    return api, demo


def rewrite_manifest(api: Path, demo: Path, injected: str | None = None) -> None:
    """改过补造文件后按副本重写 manifest (同生成脚本), 用来单独测试哈希之外的核对."""
    sys.path.insert(0, str(REPO / "data"))
    try:
        import generate_xero_demo
    finally:
        sys.path.remove(str(REPO / "data"))
    generate_xero_demo.write_manifest(demo / "20261017-q1", api / "20261017-q1", injected=injected)


def mismatch(target: stores.XeroDatasetLoader) -> str:
    with pytest.raises(SupplementMismatch) as raised:
        target.client_profile(DATASET)
    return str(raised.value)


def test_profile_extension_fields_from_supplements() -> None:
    """R127 验收 2: 载入器读出的补造画像带规格书第 4.3 节的扩展字段; 审批限额与申报的关联方不再写在 notes."""
    profile = loader().client_profile(DATASET)
    assert (profile.id, profile.version) == ("CLIENT-XERO-3C1332BB-Q1", 1)
    assert (profile.approval_limit_cents, profile.applicable_framework) == (500000, "tier2_gpfs")
    assert (profile.accounting_system, profile.financial_year_end) == ("Xero", "09-30")
    assert [(p.name, p.role, p.since) for p in profile.key_personnel] == [
        ("Alex Morgan", "Director and Company Secretary", "2018-09-03")
    ]
    assert [(p.name, p.kind, p.abn, p.acn, p.notes) for p in profile.related_parties] == [
        ("Alex Morgan", "person", None, None, "director loan, overdraft guarantee"),
        ("Morgan Advisory Pty Ltd", "company", "17900043049", "900043049", ""),
    ]
    assert [(h.holder, h.kind, h.shares, h.pct) for h in profile.ownership] == [
        ("Alex Morgan", "person", 85, 0.85),
        ("Sam Whitfield", "person", 15, 0.15),
    ]
    assert profile.governance is not None and profile.governance.all_in_management is True
    assert [(m.name, m.also_management) for m in profile.governance.members] == [("Alex Morgan", True)]
    assert profile.governance.body == "board"
    assert profile.fees is not None
    assert (profile.fees.audit_fee_cents, profile.fees.non_audit_fee_cents, profile.fees.firm_total_fee_cents) == (
        480000,
        0,
        240000000,
    )
    assert profile.fees.model_dump(exclude={"audit_fee_cents", "non_audit_fee_cents", "firm_total_fee_cents"}) == {
        "dependency_pct_by_year": {},
        "overdue_cents": 0,
        "overdue_days": 0,
        "contingent": False,
        "referral_source": None,
        "referral_source_fees_pct_by_year": {},
    }
    assert (profile.engagements, profile.prior_period_audited) == ([], False)
    assert profile.notes.startswith("Synthetic – for demonstration only.")
    assert "approval limit" not in profile.notes and "Related parties declared" not in profile.notes


def test_profile_and_planning_from_supplements() -> None:
    profile = loader().client_profile(DATASET)
    supplied = ClientProfile.model_validate_json((SUPPLEMENTS / "20261017-q1" / "client_profile.json").read_bytes())
    assert profile == supplied
    assert (profile.id, profile.version, profile.legal_name, profile.abn) == (
        "CLIENT-XERO-3C1332BB-Q1",
        1,
        "Demo Company (AU) Pty Ltd",
        "25900043021",
    )
    objectives, risks = loader().planning_presets(DATASET)
    assert len(objectives) == 3 and len(risks) == 3
    occurrence = next(r for r in risks if r.id == "RISK-REV-OCC")
    assert occurrence.description.startswith("Presumed fraud risk (ASA 240 para 27). Revenue of AUD 32,431")


def test_without_supplements_falls_back_to_snapshot() -> None:
    """supplements 为空, 或补造目录下没有该快照 (如 20261017-mock) 时照现状: 快照画像与通用模板."""
    generic = stores.read_planning(PLANNING)
    for target, dataset in ((loader(supplements=None), DATASET), (loader(), "xero/20261017-mock")):
        if not (SNAPSHOTS / dataset.removeprefix("xero/")).is_dir():
            continue
        profile = target.client_profile(dataset)
        assert (profile.id, profile.version) == ("CLIENT-XERO-3C1332BB", 1)
        assert target.planning_presets(dataset) == generic


def test_tampered_files_are_rejected(tmp_path: Path) -> None:
    api, demo = copies(tmp_path)
    target = loader(api, demo)
    assert target.client_profile(DATASET).id == "CLIENT-XERO-3C1332BB-Q1"
    profile = demo / "20261017-q1" / "client_profile.json"
    original = profile.read_bytes()
    profile.write_bytes(original + b" ")
    assert mismatch(target) == "补造文件与快照不匹配: client_profile.json"
    profile.write_bytes(original)
    journals = api / "20261017-q1" / "journals.json"
    journals.write_bytes(journals.read_bytes() + b" ")
    assert mismatch(target) == "补造文件与快照不匹配: 快照 journals.json"
    with pytest.raises(SupplementMismatch, match=r"快照 journals\.json"):
        target.planning_presets(DATASET)
    shutil.copy(SNAPSHOTS / "20261017-q1" / "journals.json", journals)
    (demo / "20261017-q1" / "notes.txt").write_text("x", encoding="utf-8")
    assert mismatch(target) == "补造文件与快照不匹配: notes.txt"
    (demo / "20261017-q1" / "notes.txt").unlink()
    (api / "20261017-q1" / "contacts.json").write_bytes(b"[]")  # 不在清单, 只改它照常
    assert target.client_profile(DATASET).id == "CLIENT-XERO-3C1332BB-Q1"
    (demo / "20261017-q1" / "manual" / "rpt07_reply.json").unlink()
    (api / "20261017-q1" / "payments.json").unlink()
    assert mismatch(target) == "补造文件与快照不匹配: manual/rpt07_reply.json, 快照 payments.json"


def test_manifest_must_name_this_dataset(tmp_path: Path) -> None:
    api, demo = copies(tmp_path)
    manifest = demo / "20261017-q1" / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    manifest.write_text(json.dumps({**data, "dataset": "xero/20261017-mock"}), encoding="utf-8")
    assert mismatch(loader(api, demo)) == "补造文件与快照不匹配: manifest.json"
    manifest.write_text("{", encoding="utf-8")
    assert mismatch(loader(api, demo)) == "补造文件与快照不匹配: manifest.json"


def test_injected_copy_needs_allow_injected(tmp_path: Path) -> None:
    api, demo = copies(tmp_path)
    rewrite_manifest(api, demo, injected="T1")
    assert mismatch(loader(api, demo)) == "测试副本 (injected=T1) 只能在 allow_injected 下载入"
    assert loader(api, demo, allow_injected=True).client_profile(DATASET).id == "CLIENT-XERO-3C1332BB-Q1"


@pytest.mark.parametrize("bad_id", ["CLIENT-XERO-3C1332BB", "CLIENT-001"])
def test_profile_id_must_carry_snapshot_prefix(tmp_path: Path, bad_id: str) -> None:
    """补造画像 ID 须以快照组织 ID 推出的前缀开头, 免得与快照画像 CLIENT-XERO-3C1332BB v1 同 ID 而内容不同."""
    api, demo = copies(tmp_path)
    path = demo / "20261017-q1" / "client_profile.json"
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "id": bad_id}), encoding="utf-8")
    rewrite_manifest(api, demo)
    message = mismatch(loader(api, demo))
    assert message.startswith("补造文件与快照不匹配") and "CLIENT-XERO-3C1332BB-" in message


@asynccontextmanager
async def no_transaction(*args: Any, **kwargs: Any) -> Any:
    yield None


async def test_load_verifies_before_building(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """未载入时先核对再读快照建表; 不匹配时抛 SupplementMismatch, 不写任何表."""
    api, demo = copies(tmp_path)
    target = loader(api, demo)
    (demo / "20261017-q1" / "client_profile.json").write_bytes(b"{}")
    built: list[str] = []

    async def writable(conn: Any, eid: str) -> None:
        return None

    async def rows(eid: str, statement: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(stores, "writable", writable)
    monkeypatch.setattr(target, "rows", rows)
    monkeypatch.setattr(stores, "build_tables", lambda snapshot: built.append("built"))
    with pytest.raises(SupplementMismatch, match=r"client_profile\.json"):
        await target.load("E", DATASET)
    assert built == []


async def test_already_loaded_does_not_read_supplements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已载入的项目再 load 直接返回: 补造目录与快照被移走也不报错."""
    target = loader(tmp_path / "api", tmp_path / "xero_demo")  # 两个目录都不存在

    async def writable(conn: Any, eid: str) -> None:
        return None

    async def rows(eid: str, statement: Any) -> list[dict[str, Any]]:
        return [{"dataset": DATASET, "data_version": 1}]

    monkeypatch.setattr(stores, "transaction", no_transaction)
    monkeypatch.setattr(stores, "writable", writable)
    monkeypatch.setattr(target, "rows", rows)
    await target.load("E", DATASET)


def test_supplements_without_profile_or_planning(tmp_path: Path) -> None:
    """补造目录没有 client_profile.json 或 planning/ 时, 分别取快照画像与通用模板 (其余补造文件照常核对)."""
    api, demo = copies(tmp_path)
    (demo / "20261017-q1" / "client_profile.json").unlink()
    shutil.rmtree(demo / "20261017-q1" / "planning")
    rewrite_manifest(api, demo)
    target = loader(api, demo)
    assert target.client_profile(DATASET).id == "CLIENT-XERO-3C1332BB"
    assert target.planning_presets(DATASET) == stores.read_planning(PLANNING)


def test_tampered_organisation_is_a_mismatch(tmp_path: Path) -> None:
    """快照 organisation.json 被改坏时先报不匹配 (不报数据集不存在或 JSON 解析错误)."""
    api, demo = copies(tmp_path)
    organisation = api / "20261017-q1" / "organisation.json"
    organisation.write_text("[{}]", encoding="utf-8")  # 去掉 OrganisationID
    assert mismatch(loader(api, demo)) == "补造文件与快照不匹配: 快照 organisation.json"
    organisation.write_text("{", encoding="utf-8")
    assert mismatch(loader(api, demo)) == "补造文件与快照不匹配: 快照 organisation.json"


def test_invalid_supplied_models_do_not_echo_content(tmp_path: Path) -> None:
    """补造画像或计划哈希相符而不合模型时, 消息只列文件名, 不带出文件里的值."""
    api, demo = copies(tmp_path)
    path = demo / "20261017-q1" / "client_profile.json"
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), "extra": "MARKER-VALUE"}))
    risks = demo / "20261017-q1" / "planning" / "risk_register.json"
    risks.write_text(json.dumps([{"id": "MARKER-VALUE"}]), encoding="utf-8")
    rewrite_manifest(api, demo)
    message = mismatch(loader(api, demo))
    assert message == "补造文件与快照不匹配: client_profile.json 不合模型" and "MARKER" not in message
    with pytest.raises(SupplementMismatch) as raised:
        loader(api, demo).planning_presets(DATASET)
    assert str(raised.value) == "补造文件与快照不匹配: planning/ 缺文件或不合模型"


LINKED = {
    "SI-77b338ef-ecc0-4b95-a0d7-2617b0054611": "CON-X01",  # ORC1026
    "SI-4fad1af2-b871-4ac5-a15a-3c5e32d2e2c4": "CON-X02",  # ORC1003
    "SI-1d1ba340-afa2-4f4c-8ff7-a147bda9a47b": "CON-X02",  # ORC1008
    "SI-b2c02d0b-41a8-4d4d-97d7-014c78b3547d": "CON-X02",  # ORC1016
    "SI-5f6deadf-36a2-495a-9980-ceb11e8af9a9": "CON-X02",  # ORC1020
    "SI-aa0173af-8707-4e7f-8dde-4c7a357bd312": "CON-X03",  # ORC1014
}  # 需求清单 R72 验收 4


def test_tables_merge_supplements() -> None:
    """R72: 行数, 服务完成记录, 合同关联, 开户行与合同 PDF; 其余各表与纯快照逐行相同."""
    from datetime import date

    from backend.adapters.xero_dataset import Snapshot, build_tables

    tables, contracts = loader().tables(DATASET)
    assert {name: len(rows) for name, rows in tables.items()} == {
        "accounts": 62,
        "trial_balance": 33,
        "journal_entries": 1017,
        "sales_invoices": 42,
        "delivery_notes": 24,
        "bank_accounts": 2,
        "bank_transactions": 146,
    }
    periods = [r.period for r in tables["trial_balance"]]
    assert (periods.count("current"), periods.count("prior")) == (27, 6)
    assert sum(date(2026, 7, 1) <= i.invoice_date <= date(2026, 9, 30) for i in tables["sales_invoices"]) == 26
    banks = [t.bank_account_id for t in tables["bank_transactions"]]
    assert (banks.count("BA-090"), banks.count("BA-091")) == (142, 4)
    notes = {n.id: n for n in tables["delivery_notes"]}
    assert all(n.id == n.delivery_no and n.id.startswith("SR-ORC") for n in notes.values())
    assert (notes["SR-ORC1026"].invoice_id, notes["SR-ORC1026"].shipped_date, notes["SR-ORC1026"].signed_date) == (
        "SI-77b338ef-ecc0-4b95-a0d7-2617b0054611",
        date(2026, 9, 3),
        date(2026, 9, 17),
    )
    assert (notes["SR-ORC1024"].invoice_id, notes["SR-ORC1024"].shipped_date, notes["SR-ORC1024"].signed_date) == (
        "SI-5aadcd34-01a9-4b8d-a2bb-d7cc1de9fa45",
        date(2026, 8, 19),
        date(2026, 8, 19),
    )
    assert "SR-ORC1003" not in notes and "SR-ORC1005" not in notes
    invoices = {i.id: i for i in tables["sales_invoices"]}
    assert {i.id: i.contract_id for i in invoices.values() if i.contract_id} == LINKED
    assert sum(i.contract_id is None for i in invoices.values()) == 36
    assert [(a.id, a.bank_name, a.bsb, a.account_no) for a in tables["bank_accounts"]] == [
        ("BA-090", "Wattlebrook Bank Limited", "306234", "12345678"),
        ("BA-091", "Wattlebrook Bank Limited", "306234", "12345679"),
    ]
    assert [p.stem for p in contracts] == ["CON-X01", "CON-X02", "CON-X03"]
    documents = {
        d["path"]: d["document_id"]
        for d in json.loads((SUPPLEMENTS / "20261017-q1" / "manifest.json").read_text())["documents"]
    }
    for path in contracts:  # 载入时登记的文件 ID (DOC- 加 sha256 前 12 位) 等于 manifest 所记
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert documents[f"contracts/{path.name}"] == f"DOC-{digest[:12]}"
    plain = build_tables(Snapshot(SNAPSHOTS / "20261017-q1"))
    for name in ("accounts", "trial_balance", "journal_entries", "bank_transactions"):
        assert tables[name] == plain[name]
    assert [i.model_copy(update={"contract_id": None}) for i in tables["sales_invoices"]] == plain["sales_invoices"]


def edit_csv(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def tables_error(api: Path, demo: Path) -> str:
    rewrite_manifest(api, demo)
    with pytest.raises(SupplementMismatch) as raised:
        loader(api, demo).tables(DATASET)
    return str(raised.value)


@pytest.mark.parametrize(
    ("name", "old", "new", "expected"),
    [
        ("delivery_notes.csv", ",ORC1026,", ",ORC9999,", "delivery_notes.csv 的发票号 ORC9999 在快照中找不到或不唯一"),
        (
            "contract_links.csv",
            "ORC1026,CON-X01",
            "ORC9998,CON-X01",
            "contract_links.csv 的发票号 ORC9998 在快照中找不到或不唯一",
        ),
        ("contract_links.csv", "ORC1003,CON-X02", "ORC1026,CON-X02", "contract_links.csv 的发票号 ORC1026 重复"),
        ("bank_accounts.csv", "306234,12345678", "306235,12345678", "bank_accounts.csv 的 BA-090 与快照的银行账户不符"),
        ("bank_accounts.csv", "BA-091,", "BA-099,", "bank_accounts.csv 的 BA-099 与快照的银行账户不符"),
        ("contract_links.csv", "ORC1026,CON-X01", "ORC1026,CON-X09", "contract_links.csv 的合同 CON-X09 没有 PDF"),
        ("delivery_notes.csv", "signed_date", "signed", "delivery_notes.csv 缺列或格式不对"),
        ("delivery_notes.csv", "2026-09-17", "17/09/2026", "delivery_notes.csv 缺列或格式不对"),
        ("bank_accounts.csv", "gl_account_code", "code", "bank_accounts.csv 缺列或格式不对"),
    ],
)
def test_tables_reject_inconsistent_supplements(tmp_path: Path, name: str, old: str, new: str, expected: str) -> None:
    """补造表与快照对不上时不载入 (哈希按副本重写, 只看内容上的不一致)."""
    api, demo = copies(tmp_path)
    edit_csv(demo / "20261017-q1" / name, old, new)
    assert tables_error(api, demo) == f"补造文件与快照不匹配: {expected}"


@pytest.mark.parametrize(
    ("name", "row", "expected"),
    [
        (
            "delivery_notes.csv",
            "SR-ORC1026,",
            "delivery_notes.csv 的 SR-ORC1026, SI-77b338ef-ecc0-4b95-a0d7-2617b0054611 重复",
        ),
        ("bank_accounts.csv", "BA-090,", "bank_accounts.csv 的 BA-090 重复"),
    ],
)
def test_tables_reject_repeated_rows(tmp_path: Path, name: str, row: str, expected: str) -> None:
    """补造 CSV 同一行出现两次时报重复 (不留到写库时撞主键)."""
    api, demo = copies(tmp_path)
    path = demo / "20261017-q1" / name
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([*lines, next(x for x in lines if x.startswith(row))]) + "\n", encoding="utf-8")
    assert tables_error(api, demo) == f"补造文件与快照不匹配: {expected}"


def test_tables_reject_repeated_invoice_number_in_snapshot(tmp_path: Path) -> None:
    """快照中两张已生效的销售发票同号时, 引用该号的补造行报不唯一."""
    api, demo = copies(tmp_path)
    path = api / "20261017-q1" / "invoices.json"
    invoices = json.loads(path.read_text(encoding="utf-8"))
    live = [i for i in invoices if i.get("Type") == "ACCREC" and i.get("Status") in ("AUTHORISED", "PAID")]
    other = next(i for i in live if i["InvoiceNumber"] not in ("ORC1024", "ORC1026"))
    other["InvoiceNumber"] = "ORC1024"
    path.write_text(json.dumps(invoices), encoding="utf-8")
    assert tables_error(api, demo) == "补造文件与快照不匹配: delivery_notes.csv 的发票号 ORC1024 在快照中找不到或不唯一"
