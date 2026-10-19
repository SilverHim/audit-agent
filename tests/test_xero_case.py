"""注入测试的机制 (R74): 补丁格式与执行, 联系人过滤, 试算表调整与一致性核对, 副本 manifest, 原目录哈希守护.

各场景 (T1 至 T6) 的补丁与断言由后续需求写; 本文件只测机制与空补丁 T0. 自测用的补丁写在 tmp 下, 不进 fixtures.
联系人过滤与扫描的自测用 tmp 自造快照 (只有合成联系人), 不把真实 contacts.json 的内容写进输出或断言消息.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from backend.adapters.xero_supplements import SupplementMismatch
from backend.persistence.stores import XeroDatasetLoader
from tests import xero_injection as inj

HAS_SNAPSHOT = (inj.SNAPSHOTS / inj.SNAPSHOT).is_dir()
needs_snapshot = pytest.mark.skipif(not HAS_SNAPSHOT, reason=inj.NO_SNAPSHOT)
JOURNAL_637 = {"JournalNumber": 637}  # 10-06 的收款 (借 090, 贷 610 各 550.00), 期后
IN_PERIOD = "/Date(1790726400000+0000)/"  # 2026-09-30


def patch(tmp_path: Path, ops: list[dict[str, Any]], scenario: str = "TX") -> Path:
    path = tmp_path / "patches" / f"{scenario}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"scenario": scenario, "title": "self-test", "ops": ops}), encoding="utf-8")
    return path


def failure(tmp_path: Path, ops: list[dict[str, Any]]) -> str:
    with pytest.raises(inj.InjectionError) as raised:
        inj.xero_case(tmp_path, patch(tmp_path, ops))
    return str(raised.value)


def journal_date(number: int) -> str:
    journals = json.loads((inj.SNAPSHOTS / inj.SNAPSHOT / "journals.json").read_text(encoding="utf-8"))
    return str(next(j for j in journals if j["JournalNumber"] == number)["JournalDate"])


def test_patch_model_is_strict() -> None:
    """补丁模型 extra=forbid; 定位须恰好一个; add 须 key 与 append 二选一; op 只有六种."""
    base = {"scenario": "TX", "title": "t"}
    good = [{"op": "set", "file": "api/journals.json", "match": JOURNAL_637, "field": "a", "before": 1, "after": 2}]
    assert inj.Patch.model_validate({**base, "ops": good}).ops[0].op == "set"
    for ops in (
        [{**good[0], "extra": 1}],
        [{**good[0], "key": "k"}],  # 两种定位
        [{"op": "set", "file": "api/x.json", "field": "a", "before": 1, "after": 2}],  # 没有定位
        [{"op": "add", "file": "api/x.json", "key": "k", "append": True, "after": 1}],
        [{"op": "move", "file": "api/x.json"}],
        [{"op": "tb_shift", "code": "090", "delta_cents": "5"}],  # tb_shift 严格类型
        [{"op": "tb_shift", "code": "090", "delta_cents": True}],
        [{"op": "tb_shift", "code": "090", "delta_cents": 5.0}],
        [{"op": "tb_shift", "code": 90, "delta_cents": 5}],
    ):
        with pytest.raises(ValueError):
            inj.Patch.model_validate({**base, "ops": ops})
    assert inj.read_patch("T0").ops == []


@needs_snapshot
def test_t0_copy_loads_like_the_original(tmp_path: Path) -> None:
    """T0 空补丁: 副本经载入器得到的七张表与原目录逐行相同 (含发票的客户与 ABN); allow_injected 为假时拒绝."""
    case = inj.xero_case(tmp_path, "T0")
    assert (
        case.snapshot == tmp_path / "api" / inj.SNAPSHOT and case.supplements == tmp_path / "xero_demo" / inj.SNAPSHOT
    )
    original = XeroDatasetLoader(inj.SNAPSHOTS, inj.PLANNING, object(), object(), supplements=inj.SUPPLEMENTS)  # type: ignore[arg-type]
    assert case.loader.tables(case.dataset) == original.tables(case.dataset)
    copied, source = case.loader.supplement(case.dataset), original.supplement(case.dataset)
    assert copied is not None and source is not None
    assert [p.read_bytes() for p in copied.contracts()] == [p.read_bytes() for p in source.contracts()]
    manifest = json.loads((case.supplements / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["injected"] == "T0"
    assert "contacts.json" not in {f["path"] for f in manifest["snapshot"]["files"]}
    strict = XeroDatasetLoader(tmp_path / "api", inj.PLANNING, object(), object(), supplements=tmp_path / "xero_demo")  # type: ignore[arg-type]
    with pytest.raises(SupplementMismatch, match="injected=T0"):
        strict.tables(case.dataset)
    # 只取整数与布尔, 测试栈帧里不留真实联系人列表 (断言失败时 pytest 会打印实参与局部变量)
    counts = (
        inj.contact_count(case.snapshot / "contacts.json"),
        inj.contact_count(inj.SNAPSHOTS / inj.SNAPSHOT / "contacts.json"),
    )
    assert counts == (82, 83)
    assert inj.filtered_contacts(case.snapshot / "contacts.json")[1] == 0  # 副本中没有登录账号联系人
    assert not (case.snapshot / "raw").exists()


@needs_snapshot
def test_before_mismatch_and_match_count_fail(tmp_path: Path) -> None:
    message = failure(
        tmp_path / "a",
        [
            {
                "op": "set",
                "file": "api/journals.json",
                "match": JOURNAL_637,
                "field": "JournalDate",
                "before": "x",
                "after": IN_PERIOD,
            }
        ],
    )
    assert message == 'api/journals.json match {"JournalNumber": 637} JournalDate: 当前值与 before 不符'
    message = failure(
        tmp_path / "b",
        [
            {
                "op": "set",
                "file": "api/journals.json",
                "match": {"JournalNumber": 99999},
                "field": "a",
                "before": None,
                "after": 1,
            }
        ],
    )
    assert message.endswith("应恰好命中 1 个, 实有 0 个")
    message = failure(
        tmp_path / "c",
        [{"op": "delete", "file": "api/invoices.json", "match": {"Type": "ACCREC"}, "before": {}}],
    )
    assert "应恰好命中 1 个, 实有" in message and "实有 0 个" not in message


@needs_snapshot
def test_moving_a_journal_needs_tb_shift(tmp_path: Path) -> None:
    """把期后的收款 637 改到 09-30 而不调试算表: 分录推出的余额与试算表不符, 报 090 与 610; 配 tb_shift 后通过."""
    move = {
        "op": "set",
        "file": "api/journals.json",
        "match": JOURNAL_637,
        "field": "JournalDate",
        "before": journal_date(637),
        "after": IN_PERIOD,
    }
    message = failure(tmp_path / "a", [move])
    assert "090" in message and "610" in message
    shifts = [
        {"op": "tb_shift", "code": "090", "delta_cents": 55000},
        {"op": "tb_shift", "code": "610", "delta_cents": -55000},
    ]
    case = inj.xero_case(tmp_path / "b", patch(tmp_path / "b", [move, *shifts], "TM"))
    tables = case.loader.tables(case.dataset)
    current = {r.account_code: r.balance_cents for r in tables["trial_balance"] if r.period == "current"}
    assert (current["090"], current["610"]) == (-1090044 + 55000, 1019950 - 55000)
    assert failure(tmp_path / "c", [{"op": "tb_shift", "code": "999", "delta_cents": 1}]).endswith("试算表没有科目 999")


@needs_snapshot
def test_add_delete_add_file_and_csv_rows(tmp_path: Path) -> None:
    """add 加对象的键, append 追加 CSV 行, delete 删 CSV 行, add_file 加新文件; add_file 的目标已存在时失败."""
    notes = "xero_demo/delivery_notes.csv"
    row = {
        "id": "SR-ORC1026",
        "delivery_no": "SR-ORC1026",
        "invoice_no": "ORC1026",
        "shipped_date": "2026-09-03",
        "signed_date": "2026-09-17",
        "signed_by": "Priya Natarajan",
    }
    ops = [
        {"op": "delete", "file": notes, "row": "SR-ORC1026", "before": row},
        {"op": "add", "file": notes, "append": True, "after": {**row, "signed_date": "2026-10-02"}},
        {
            "op": "add",
            "file": "xero_demo/confirmations/receipts.json",
            "key": "99999999",
            "after": {"receipt_channel": "post"},
        },
        {"op": "add_file", "file": "xero_demo/notes/extra.txt", "content": "synthetic"},
    ]
    case = inj.xero_case(tmp_path / "a", patch(tmp_path / "a", ops, "TA"))
    tables = case.loader.tables(case.dataset)
    moved = next(n for n in tables["delivery_notes"] if n.id == "SR-ORC1026")
    assert str(moved.signed_date) == "2026-10-02"
    assert json.loads((case.supplements / "confirmations" / "receipts.json").read_text())["99999999"]
    assert (case.supplements / "notes" / "extra.txt").read_text() == "synthetic"
    assert failure(
        tmp_path / "b", [{"op": "add_file", "file": "xero_demo/contract_links.csv", "content": "x"}]
    ).endswith("add_file 的目标已存在")
    assert failure(tmp_path / "c", [{"op": "add_file", "file": "other/x.txt", "content": "x"}]).endswith(
        "file 须以 api/ 或 xero_demo/ 开头"
    )
    assert failure(tmp_path / "d", [{"op": "add_file", "file": "api/../../escape.txt", "content": "x"}]).endswith(
        "不能跳出副本目录"
    )


@needs_snapshot
def test_render_registry_pdfs_follows_copy(tmp_path: Path) -> None:
    """改副本的登记 JSON 后 render: 副本 PDF 与副本 JSON 一致, 原目录不变 (哈希守护)."""
    before = inj.originals()
    ops = [
        {
            "op": "set",
            "file": "xero_demo/registry/extracts/900043049.json",
            "key": "name",
            "before": "Morgan Advisory Pty Ltd",
            "after": "Morgan Advisory Services Pty Ltd",
        },
        {"op": "render", "what": "registry_pdfs"},
    ]
    case = inj.xero_case(tmp_path, patch(tmp_path, ops, "TR"))
    import pdfplumber

    with pdfplumber.open(case.supplements / "registry" / "extracts" / "900043049.pdf") as pdf:
        text = " ".join(page.extract_text() or "" for page in pdf.pages)
    assert "Morgan Advisory Services Pty Ltd" in " ".join(text.split())
    assert inj.changed(before, inj.originals()) == []


@needs_snapshot
def test_set_row_changes_one_csv_line(tmp_path: Path) -> None:
    """set + row: 改 CSV 一行的一列, 其余行逐字节不变; 表头没有的列与不全的整行替换都拒绝."""
    notes = "xero_demo/delivery_notes.csv"
    ops = [
        {
            "op": "set",
            "file": notes,
            "row": "SR-ORC1026",
            "field": "signed_date",
            "before": "2026-09-17",
            "after": "2026-10-02",
        }
    ]
    case = inj.xero_case(tmp_path / "a", patch(tmp_path / "a", ops, "TS"))
    tables = case.loader.tables(case.dataset)
    assert str(next(n for n in tables["delivery_notes"] if n.id == "SR-ORC1026").signed_date) == "2026-10-02"
    old = (inj.SUPPLEMENTS / inj.SNAPSHOT / "delivery_notes.csv").read_text(encoding="utf-8").splitlines()
    new = (case.supplements / "delivery_notes.csv").read_text(encoding="utf-8").splitlines()
    changed = [n for n, (a, b) in enumerate(zip(old, new, strict=True)) if a != b]
    assert changed == [next(n for n, line in enumerate(old) if line.startswith("SR-ORC1026,"))]
    column = {**ops[0], "field": "signed_on"}
    assert failure(tmp_path / "b", [column]).endswith("signed_on: CSV 没有这一列")
    whole = {**ops[0], "field": "", "before": None, "after": {"id": "SR-ORC1026"}}
    assert failure(tmp_path / "c", [whole]).endswith("整行替换须按表头给出全部列")


@needs_snapshot
def test_delete_whole_journal_with_tb_shift(tmp_path: Path) -> None:
    """delete + match: 以整笔分录为 before 删去 499 (AJE-X01 要撤销的那笔), 配 tb_shift 后一致; 710 与 711 调到 0 时
    两列都写空; before 与分录有一处不同时拒绝."""
    journals = json.loads((inj.SNAPSHOTS / inj.SNAPSHOT / "journals.json").read_text(encoding="utf-8"))
    entry = next(j for j in journals if j["JournalNumber"] == 499)
    delete = {"op": "delete", "file": "api/journals.json", "match": {"JournalNumber": 499}, "before": entry}
    shifts = [
        {"op": "tb_shift", "code": "710", "delta_cents": -75000},
        {"op": "tb_shift", "code": "711", "delta_cents": 82500},
        {"op": "tb_shift", "code": "820", "delta_cents": -7500},
    ]
    case = inj.xero_case(tmp_path / "a", patch(tmp_path / "a", [delete, *shifts], "TD"))
    assert all(j["JournalNumber"] != 499 for j in json.loads((case.snapshot / "journals.json").read_text()))
    tables = case.loader.tables(case.dataset)
    current = {r.account_code: r.balance_cents for r in tables["trial_balance"] if r.period == "current"}
    assert (current.get("710", 0), current.get("711", 0), current["820"]) == (0, 0, -126697)
    report = json.loads((case.snapshot / "trial_balance_2026-09-30.json").read_text(encoding="utf-8"))
    header = next(r for r in report["Rows"] if r["RowType"] == "Header")
    names = [c["Value"] for c in header["Cells"]]
    rows = [r for section in report["Rows"] for r in section.get("Rows", []) if r.get("RowType") == "Row"]
    row = next(r for r in rows if str(r["Cells"][0]["Value"]).endswith("(710)"))
    assert [row["Cells"][names.index(n)]["Value"] for n in ("YTD Debit", "YTD Credit")] == ["", ""]
    summary = next(r for s in report["Rows"] for r in s.get("Rows", []) if r.get("RowType") == "SummaryRow")
    for name in ("YTD Debit", "YTD Credit"):  # 合计行随之重算
        column = names.index(name)
        assert inj.cents(summary["Cells"][column]["Value"]) == sum(inj.cents(r["Cells"][column]["Value"]) for r in rows)
    stale = {**delete, "before": {**entry, "JournalNumber": 498}}
    assert failure(tmp_path / "b", [stale, *shifts]).endswith("当前值与 before 不符")


@needs_snapshot
def test_before_null_and_strict_types(tmp_path: Path) -> None:
    """before 为 null: 字段原来不存在时可加; 字段有值时拒绝. before 连类型一起比: 布尔字段写成 0 或 1 时拒绝."""
    journal = {"file": "api/journals.json", "match": JOURNAL_637}
    added = {"op": "set", **journal, "field": "SyntheticNote", "before": None, "after": "x"}
    case = inj.xero_case(tmp_path / "a", patch(tmp_path / "a", [added], "TN"))
    journals = json.loads((case.snapshot / "journals.json").read_text(encoding="utf-8"))
    assert next(j for j in journals if j["JournalNumber"] == 637)["SyntheticNote"] == "x"
    present = {"op": "set", **journal, "field": "JournalDate", "before": None, "after": IN_PERIOD}
    assert failure(tmp_path / "b", [present]).endswith("JournalDate: 当前值与 before 不符")
    invoices = json.loads((inj.SNAPSHOTS / inj.SNAPSHOT / "invoices.json").read_text(encoding="utf-8"))
    flag = next(i for i in invoices if i.get("InvoiceNumber") == "ORC1003")["HasAttachments"]
    assert isinstance(flag, bool)
    typed = {
        "op": "set",
        "file": "api/invoices.json",
        "match": {"InvoiceNumber": "ORC1003"},
        "field": "HasAttachments",
        "before": int(flag),
        "after": flag,
    }
    assert failure(tmp_path / "c", [typed]).endswith("HasAttachments: 当前值与 before 不符")


@needs_snapshot
def test_json_object_and_array_ops(tmp_path: Path) -> None:
    """set + key 换掉对象某个键的整个值 (数组), delete + key, append 到 JSON 数组, add_file 写 JSON; render 后副本 PDF
    随 JSON 变, 新文件进副本 manifest. 不合用法的 add 与 delete 都拒绝."""
    person = "xero_demo/registry/persons/MORGAN-ALEX-19790412.json"
    roles = json.loads((inj.SUPPLEMENTS / inj.SNAPSHOT / person.removeprefix("xero_demo/")).read_text())["roles"]
    receipts = "xero_demo/confirmations/receipts.json"
    content = {"rows": [1, 2], "note": "Synthetic – for demonstration only"}
    ops = [
        {"op": "set", "file": person, "key": "roles", "before": roles, "after": roles[:-1]},
        {"op": "delete", "file": receipts, "key": "12345679", "before": {"receipt_channel": "direct_email"}},
        {"op": "add", "file": "xero_demo/planning/risk_register.json", "append": True, "after": {"id": "SYN-1"}},
        {"op": "add_file", "file": "xero_demo/notes/extra.json", "content": content},
        {"op": "render", "what": "registry_pdfs"},
    ]
    case = inj.xero_case(tmp_path / "a", patch(tmp_path / "a", ops, "TJ"))
    copied = case.supplements / "registry" / "persons"
    assert json.loads((copied / "MORGAN-ALEX-19790412.json").read_text())["roles"] == roles[:-1]
    pdf = "registry/persons/MORGAN-ALEX-19790412.pdf"
    assert (case.supplements / pdf).read_bytes() != (inj.SUPPLEMENTS / inj.SNAPSHOT / pdf).read_bytes()
    assert list(json.loads((case.supplements / "confirmations" / "receipts.json").read_text())) == ["12345678"]
    risks = json.loads((case.supplements / "planning" / "risk_register.json").read_text())
    assert len(risks) == 4 and risks[-1] == {"id": "SYN-1"}
    assert json.loads((case.supplements / "notes" / "extra.json").read_text(encoding="utf-8")) == content
    manifest = json.loads((case.supplements / "manifest.json").read_text(encoding="utf-8"))
    hashes = {f["path"]: f["sha256"] for f in manifest["files"]}
    assert "notes/extra.json" in hashes and hashes[pdf] == inj.file_hashes(case.supplements)[pdf]
    case.loader.tables(case.dataset)  # 副本 manifest 与副本内容相符
    for ops, message in (
        ([{"op": "add", "file": receipts, "append": True, "after": {}}], "append 只用于 JSON 数组或 CSV"),
        ([{"op": "add", "file": receipts, "key": "12345678", "after": {}}], "只能加 JSON 对象中原来没有的键"),
        ([{"op": "delete", "file": receipts, "key": "99999999", "before": None}], "当前值与 before 不符"),
        ([{"op": "set", "file": receipts, "row": "x", "before": None, "after": 1}], "row 只用于 CSV"),
    ):
        assert failure(tmp_path / message, ops).endswith(message)


def synthetic_snapshot(root: Path, contacts: list[dict[str, Any]]) -> Path:
    """tmp 自造快照: 只有合成联系人与最少的文件."""
    snapshot = root / "api" / inj.SNAPSHOT
    snapshot.mkdir(parents=True)
    (snapshot / "contacts.json").write_text(json.dumps(contacts), encoding="utf-8")
    (snapshot / "organisation.json").write_text("[]", encoding="utf-8")
    (snapshot / "raw").mkdir()
    (snapshot / "raw" / "export.csv").write_text("x", encoding="utf-8")
    return snapshot


LOGIN = {"FirstName": "Zed", "LastName": "Example", "EmailAddress": "zed@example.invalid"}  # 合成的登录账号联系人
CUSTOMER = {"Name": "Acme", "IsCustomer": True, "FirstName": "Ann", "EmailAddress": "ann@example.invalid"}


def test_contact_filter_and_scan_on_synthetic_snapshot(tmp_path: Path) -> None:
    source = synthetic_snapshot(tmp_path / "src", [LOGIN, CUSTOMER])
    target = tmp_path / "copy"
    inj.copy_snapshot(source, target)
    assert json.loads((target / "contacts.json").read_text()) == [CUSTOMER]
    assert not (target / "raw").exists()
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "note.txt").write_text("Signed by ZED EXAMPLE", encoding="utf-8")
    assert inj.scan_copies(source, [target, planted]) == [f"{planted / 'note.txt'}: 含登录账号联系人的姓名或邮箱"]
    for contacts in ([CUSTOMER], [LOGIN, {**LOGIN, "EmailAddress": "other@example.invalid"}]):
        with pytest.raises(inj.InjectionError, match="登录账号联系人应恰好 1 条"):
            inj.copy_snapshot(
                synthetic_snapshot(tmp_path / f"s{len(contacts)}", contacts), tmp_path / f"c{len(contacts)}"
            )


def test_hash_guard_helpers(tmp_path: Path) -> None:
    """哈希守护: 改动, 新增, 删除都报文件名; 目录不存在时为空 (不核对)."""
    root = tmp_path / "dir"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    (root / "sub" / "b.txt").write_text("b")
    before = {"api": inj.file_hashes(root), "xero_demo": inj.file_hashes(tmp_path / "missing")}
    assert before["xero_demo"] == {} and set(before["api"]) == {"a.txt", "sub/b.txt"}
    (root / "a.txt").write_text("changed")
    (root / "sub" / "b.txt").unlink()
    (root / "c.txt").write_text("c")
    after = {"api": inj.file_hashes(root), "xero_demo": {}}
    assert inj.changed(before, after) == ["api/a.txt", "api/c.txt", "api/sub/b.txt"]


def test_skips_without_snapshot(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception, match=inj.NO_SNAPSHOT):
        inj.xero_case(tmp_path, "T0", snapshots=tmp_path / "missing")


def test_fixture_is_bound_to_tmp_path(xero_case: Any, tmp_path: Path) -> None:
    """conftest 的 xero_case 夹具绑定本用例的 tmp_path; 同一目录再建副本时报出原因, base 换子目录后可再建."""
    if not HAS_SNAPSHOT:
        pytest.skip(inj.NO_SNAPSHOT)
    case = xero_case("T0")
    assert case.snapshot.is_relative_to(tmp_path) and case.loader.allow_injected
    with pytest.raises(inj.InjectionError, match="已有副本"):
        xero_case("T0")
    pair = xero_case("T0", base="pair")
    assert (pair.snapshot, pair.supplements) == (
        tmp_path / "pair" / "api" / inj.SNAPSHOT,
        tmp_path / "pair" / "xero_demo" / inj.SNAPSHOT,
    )
    assert pair.loader.tables(pair.dataset) == case.loader.tables(case.dataset)


def test_match_compares_types(tmp_path: Path) -> None:
    """match 与 before 同口径, 连类型一起比: {"a": 1} 不命中 a 为 true 的元素; 字段不存在不命中."""
    path = tmp_path / "x.json"
    path.write_text(json.dumps([{"a": True, "n": 5}, {"a": 1, "n": 6}]), encoding="utf-8")
    doc = inj.Document(path, "api/x.json")
    locate = lambda match: doc.locate(inj.DeleteOp(op="delete", file="api/x.json", match=match, before=None))  # noqa: E731
    assert locate({"a": 1}) == (doc.data, 1) and locate({"a": True}) == (doc.data, 0)
    with pytest.raises(inj.InjectionError, match="实有 0 个"):
        locate({"n": 5.0})
    with pytest.raises(inj.InjectionError, match="实有 0 个"):
        locate({"missing": None})


def synthetic_originals(root: Path) -> tuple[Path, Path]:
    """tmp 自造的两个 "原目录" (只供哈希比对的自测)."""
    snapshots, supplements = root / "api", root / "xero_demo"
    for directory, name, text in ((snapshots, "a.json", "[]"), (supplements, "b.json", "{}")):
        (directory / inj.SNAPSHOT).mkdir(parents=True)
        (directory / inj.SNAPSHOT / name).write_text(text, encoding="utf-8")
    return snapshots, supplements


def test_originals_compared_on_failure_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """xero_case 收尾的原目录哈希比对在失败路径也做: 构建失败且原目录被改时报改动的文件, 原异常留在 __context__;
    构建成功但原目录被改时也失败; 原目录没改时原异常原样抛出."""
    snapshots, supplements = synthetic_originals(tmp_path / "orig")
    empty = inj.Patch(scenario="TX", title="self-test", ops=[])

    def build(change: bool, error: Exception | None) -> Any:
        def fake(*args: Any) -> Any:
            if change:  # 每次都改出不同的内容
                original = supplements / inj.SNAPSHOT / "b.json"
                original.write_text(original.read_text(encoding="utf-8") + " ", encoding="utf-8")
            if error is not None:
                raise error
            return "case"

        return fake

    monkeypatch.setattr(inj, "build_case", build(True, ValueError("build failed")))
    with pytest.raises(inj.InjectionError, match="原目录被改动") as raised:
        inj.xero_case(tmp_path / "a", empty, snapshots=snapshots, supplements=supplements)
    assert str(raised.value).endswith(f"{inj.SNAPSHOT}/b.json")
    assert isinstance(raised.value.__context__, ValueError) and str(raised.value.__context__) == "build failed"
    monkeypatch.setattr(inj, "build_case", build(True, None))
    with pytest.raises(inj.InjectionError, match="原目录被改动"):
        inj.xero_case(tmp_path / "b", empty, snapshots=snapshots, supplements=supplements)
    monkeypatch.setattr(inj, "build_case", build(False, inj.InjectionError("api/x.json key k: 当前值与 before 不符")))
    with pytest.raises(inj.InjectionError) as raised:
        inj.xero_case(tmp_path / "c", empty, snapshots=snapshots, supplements=supplements)
    assert str(raised.value) == "api/x.json key k: 当前值与 before 不符"
