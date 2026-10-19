"""Xero 注入测试的机制 (设计第 8.1, 8.9 节; 决定 6): 在临时目录复制快照与补造目录, 过滤登录账号联系人, 执行补丁,
调试算表并核对分录与试算表一致, 重写副本 manifest, 交给 allow_injected 的载入器; 原目录逐文件哈希守护.

补丁文件 tests/fixtures/xero_injections/<场景>.json: {scenario, title, ops}. op 六种 (第 9.5 节):
- set (file, 定位, field, before, after): 改字段; field 用点号写嵌套字段, 空为定位到的元素本身; before 为 null
  表示改前不存在.
- delete (file, 定位, before): 删数组元素, 对象的键或 CSV 行; before 为被删的值.
- add (file, key 或 append, after): 加对象的键 (须原来没有), 或在数组 / CSV 末尾追加.
- add_file (file, content): 加新文件 (须原来没有); content 为字符串时原样写, 否则写成 JSON.
- tb_shift (code, delta_cents): 副本期末试算表该科目的 YTD Debit / YTD Credit 按余额变动 delta_cents 改写, 合计行随之
  重算 (本月两列不维护).
- render (what): registry_pdfs 按副本的登记 JSON 重绘登记 PDF.
file 以 "api/" 或 "xero_demo/" 开头 (相对副本快照与补造目录); 定位三种: match (JSON 数组中各字段相等, 连类型
一起比, 须恰好命中 1 个), key (JSON 对象的键), row (CSV 首列主键). 执行前断言当前值等于 before (连类型一起比),
不等时报文件, 定位与字段, 不写当前值. CSV 的 set 只许改表头内的列, 整行替换须给全部列.
不在任何输出与断言消息中写出登录账号联系人的原值 (只在内存中比对).
"""

import csv
import hashlib
import io
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from backend.persistence.stores import XeroDatasetLoader
from tests.external import snapshot_dir, snapshot_location, supplements_dir, supplements_path

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT = "20261017-q1"
# 原目录的上级, 只给路径不核对 (原目录哈希守护与断言里的对照用); xero_case 缺省经 tests.external 定位核对
SNAPSHOTS = snapshot_location(SNAPSHOT).parent  # 快照的上级
SUPPLEMENTS = supplements_path(SNAPSHOT).parent  # 补造目录的上级
PLANNING = REPO / "src" / "backend" / "knowledge" / "planning"
INJECTIONS = REPO / "tests" / "fixtures" / "xero_injections"
NO_SNAPSHOT = "没有本地 Xero 快照"
MISSING = object()  # 字段或元素不存在


class InjectionError(AssertionError):
    """补丁执行或一致性断言失败; 消息只写文件, 定位与字段, 不写联系人原值."""


class Located(BaseModel):
    """带定位的操作: match, key, row 恰好给一个."""

    model_config = ConfigDict(extra="forbid")
    file: str
    match: dict[str, Any] | None = None
    key: str | None = None
    row: str | None = None

    @model_validator(mode="after")
    def one_locator(self) -> "Located":
        if sum(x is not None for x in (self.match, self.key, self.row)) != 1:
            raise ValueError("match, key, row 须恰好给一个")
        return self

    def where(self) -> str:
        if self.match is not None:
            return f"match {json.dumps(self.match, ensure_ascii=False, sort_keys=True)}"
        return f"key {self.key}" if self.key is not None else f"row {self.row}"


class SetOp(Located):
    op: Literal["set"]
    field: str = ""  # 定位到的元素中的字段 (点号写嵌套); 空为元素本身 (如对象某个键的整个值)
    before: Any
    after: Any


class DeleteOp(Located):
    op: Literal["delete"]
    before: Any


class AddOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add"]
    file: str
    key: str | None = None
    append: bool = False
    after: Any

    @model_validator(mode="after")
    def one_target(self) -> "AddOp":
        if (self.key is not None) == self.append:
            raise ValueError("key 与 append 须恰好给一个")
        return self


class AddFileOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["add_file"]
    file: str
    content: Any


class TbShiftOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["tb_shift"]
    code: StrictStr
    delta_cents: StrictInt


class RenderOp(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["render"]
    what: Literal["registry_pdfs"]


Op = Annotated[SetOp | DeleteOp | AddOp | AddFileOp | TbShiftOp | RenderOp, Field(discriminator="op")]


class Patch(BaseModel):
    """一个注入场景的补丁."""

    model_config = ConfigDict(extra="forbid")
    scenario: str
    title: str
    ops: list[Op]


def read_patch(scenario: str | Path | Patch) -> Patch:
    """场景名 (读 INJECTIONS/<名>.json), 补丁文件路径或补丁对象."""
    if isinstance(scenario, Patch):
        return scenario
    path = scenario if isinstance(scenario, Path) else INJECTIONS / f"{scenario}.json"
    return Patch.model_validate_json(path.read_bytes())


def file_hashes(root: Path) -> dict[str, str]:
    """目录下逐文件 (含子目录) 的 sha256; 目录不存在时为空."""
    if not root.is_dir():
        return {}
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def originals() -> dict[str, dict[str, str]]:
    """两个原目录 (快照与补造) 的逐文件哈希; 不存在的目录为空 (没有快照时只核对补造目录)."""
    return {"api": file_hashes(SNAPSHOTS / SNAPSHOT), "xero_demo": file_hashes(SUPPLEMENTS / SNAPSHOT)}


def changed(before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]) -> list[str]:
    """两次哈希之间改动, 新增或删除的文件 (只报文件名)."""
    names: list[str] = []
    for side in before:
        old, new = before[side], after.get(side, {})
        names += [f"{side}/{n}" for n in sorted(old.keys() | new.keys()) if old.get(n) != new.get(n)]
    return names


def is_login_contact(contact: Any) -> bool:
    """登录账号联系人的口径 (同 data/check_xero_demo.login_contact): 双否且有 FirstName 与 EmailAddress."""
    return (
        isinstance(contact, dict)
        and not contact.get("IsCustomer")
        and not contact.get("IsSupplier")
        and bool(contact.get("FirstName"))
        and bool(contact.get("EmailAddress"))
    )


def filtered_contacts(path: Path) -> tuple[list[Any], int]:
    """读 contacts.json, 返回 (去掉登录账号联系人后的列表, 去掉的条数). 原列表只在本函数内, 调用方的栈帧里没有原值
    (断言失败或异常时 pytest 打印的局部变量与实参都不会带出)."""
    contacts = json.loads(path.read_text(encoding="utf-8"))
    kept = [c for c in contacts if not is_login_contact(c)]
    return kept, len(contacts) - len(kept)


def contact_count(path: Path) -> int:
    """contacts.json 的条数 (只返回整数, 测试里不保留真实联系人列表)."""
    return len(json.loads(path.read_text(encoding="utf-8")))


def copy_snapshot(source: Path, target: Path) -> None:
    """复制快照顶层文件 (raw/ 等子目录不复制); contacts.json 在内存中去掉登录账号联系人 (须恰好 1 条)."""
    target.mkdir(parents=True)
    for path in sorted(source.iterdir()):
        if not path.is_file():
            continue
        if path.name == "contacts.json":
            kept, removed = filtered_contacts(path)
            if removed != 1:
                raise InjectionError(f"{path.name}: 登录账号联系人应恰好 1 条, 实有 {removed} 条")
            (target / path.name).write_text(json.dumps(kept, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            shutil.copy2(path, target / path.name)


@dataclass(frozen=True)
class XeroCase:
    """注入场景的副本: 快照副本, 补造副本与 allow_injected 的载入器 (数据集 xero/20261017-q1)."""

    scenario: str
    snapshot: Path  # tmp_path/api/20261017-q1
    supplements: Path  # tmp_path/xero_demo/20261017-q1
    loader: XeroDatasetLoader
    dataset: str = f"xero/{SNAPSHOT}"


def target(case_roots: dict[str, Path], file: str) -> Path:
    """补丁中的 file ("api/..." 或 "xero_demo/...") → 副本中的路径; 不能跳出副本目录."""
    side, _, rest = file.partition("/")
    if side not in case_roots or not rest:
        raise InjectionError(f"{file}: file 须以 api/ 或 xero_demo/ 开头")
    root = case_roots[side]
    path = (root / rest).resolve()
    if not path.is_relative_to(root.resolve()):
        raise InjectionError(f"{file}: 不能跳出副本目录")
    return path


def same(a: Any, b: Any) -> bool:
    """连类型一起比 (0 与 false, 1 与 1.0 不算相等): 比较规范化的 JSON."""
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


def matches(item: Any, match: dict[str, Any]) -> bool:
    """match 定位: 各字段都存在且与给定值相等 (同 before, 连类型一起比)."""
    return all((value := get_field(item, k)) is not MISSING and same(value, v) for k, v in match.items())


def get_field(item: Any, field: str) -> Any:
    for part in field.split("."):
        if isinstance(item, dict) and part in item:
            item = item[part]
        elif isinstance(item, list) and part.isdigit() and int(part) < len(item):
            item = item[int(part)]
        else:
            return MISSING
    return item


def set_field(item: Any, field: str, value: Any) -> None:
    *parents, last = field.split(".")
    for part in parents:
        item = item[int(part)] if isinstance(item, list) else item.setdefault(part, {})
    if isinstance(item, list):
        item[int(last)] = value
    else:
        item[last] = value


class Document:
    """补丁目标文件: JSON (数组或对象) 或 CSV (首列为主键); 改完 save 写回."""

    def __init__(self, path: Path, file: str) -> None:
        self.path, self.file = path, file
        if not path.is_file():
            raise InjectionError(f"{file}: 文件不存在")
        self.csv = path.suffix == ".csv"
        if self.csv:
            with path.open(newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                self.columns = list(reader.fieldnames or [])
                self.data: Any = list(reader)
        else:
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def locate(self, op: Located) -> tuple[Any, Any]:
        """返回 (容器, 下标或键); 定位不到或 match 不唯一时抛 InjectionError."""
        where = f"{self.file} {op.where()}"
        if op.row is not None:
            if not self.csv:
                raise InjectionError(f"{where}: row 只用于 CSV")
            hits = [n for n, r in enumerate(self.data) if r[self.columns[0]] == op.row]
        elif op.key is not None:
            if not isinstance(self.data, dict):
                raise InjectionError(f"{where}: key 只用于 JSON 对象")
            return self.data, op.key
        else:
            if not isinstance(self.data, list):
                raise InjectionError(f"{where}: match 只用于 JSON 数组")
            hits = [n for n, item in enumerate(self.data) if matches(item, op.match or {})]
        if len(hits) != 1:
            raise InjectionError(f"{where}: 应恰好命中 1 个, 实有 {len(hits)} 个")
        return self.data, hits[0]

    def save(self) -> None:
        if self.csv:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=self.columns, lineterminator="\n")
            writer.writeheader()
            writer.writerows(self.data)
            self.path.write_text(buffer.getvalue(), encoding="utf-8")
        else:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def apply_op(op: Any, roots: dict[str, Path]) -> None:
    """执行一条补丁 (tb_shift 与 render 由 xero_case 处理)."""
    if isinstance(op, AddFileOp):
        path = target(roots, op.file)
        if path.exists():
            raise InjectionError(f"{op.file}: add_file 的目标已存在")
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (
            op.content if isinstance(op.content, str) else json.dumps(op.content, ensure_ascii=False, indent=2) + "\n"
        )
        path.write_text(text, encoding="utf-8")
        return
    doc = Document(target(roots, op.file), op.file)
    if isinstance(op, SetOp):
        container, index = doc.locate(op)
        item = container.get(index, MISSING) if isinstance(container, dict) else container[index]
        if doc.csv:
            if op.field and op.field not in doc.columns:
                raise InjectionError(f"{op.file} {op.where()} {op.field}: CSV 没有这一列")
            if not op.field and (not isinstance(op.after, dict) or list(op.after) != doc.columns):
                raise InjectionError(f"{op.file} {op.where()}: 整行替换须按表头给出全部列")
        current = item if not op.field else get_field(item, op.field) if item is not MISSING else MISSING
        if not same(None if current is MISSING else current, op.before):
            raise InjectionError(f"{op.file} {op.where()} {op.field or '(整个值)'}: 当前值与 before 不符")
        if not op.field:
            container[index] = op.after
        else:
            if item is MISSING:
                container[index] = item = {}
            set_field(item, op.field, op.after)
    elif isinstance(op, DeleteOp):
        container, index = doc.locate(op)
        current = container.get(index, MISSING) if isinstance(container, dict) else container[index]
        if current is MISSING or not same(current, op.before):
            raise InjectionError(f"{op.file} {op.where()}: 当前值与 before 不符")
        del container[index]
    elif isinstance(op, AddOp):
        if op.append:
            if not isinstance(doc.data, list):
                raise InjectionError(f"{op.file}: append 只用于 JSON 数组或 CSV")
            if doc.csv and (not isinstance(op.after, dict) or list(op.after) != doc.columns):
                raise InjectionError(f"{op.file}: 追加的 CSV 行须按表头给出全部列")
            doc.data.append(op.after)
        else:
            if not isinstance(doc.data, dict) or op.key in doc.data:
                raise InjectionError(f"{op.file} key {op.key}: 只能加 JSON 对象中原来没有的键")
            doc.data[op.key] = op.after
    doc.save()


def shift_tb(report: dict[str, Any], code: str, delta_cents: int) -> None:
    """试算表报表 (Xero 结构) 中该科目的余额 (YTD Debit - YTD Credit) 加 delta_cents, 按余额方向改写两列 (原地改);
    找不到科目时报出 (设计第 8.1 节第 5 步)."""
    header = next(r for r in report["Rows"] if r["RowType"] == "Header")
    names = [c["Value"] for c in header["Cells"]]
    debit, credit = names.index("YTD Debit"), names.index("YTD Credit")
    for section in report["Rows"]:
        for row in section.get("Rows", []):
            cells = row.get("Cells", [])
            if row.get("RowType") == "Row" and cells and str(cells[0]["Value"]).endswith(f"({code})"):
                balance = cents(cells[debit]["Value"]) - cents(cells[credit]["Value"]) + delta_cents
                cells[debit]["Value"] = money(balance) if balance > 0 else ""
                cells[credit]["Value"] = money(-balance) if balance < 0 else ""
                refresh_totals(report, debit, credit)
                return
    raise InjectionError(f"试算表没有科目 {code}")


def refresh_totals(report: dict[str, Any], debit: int, credit: int) -> None:
    """按各科目行重算合计行 (SummaryRow) 的 YTD Debit 与 YTD Credit. 本月的 Debit 与 Credit 两列不维护
    (载入器与各程序只读 YTD 两列)."""
    rows = [r for section in report["Rows"] for r in section.get("Rows", []) if r.get("RowType") == "Row"]
    totals = [sum(cents(r["Cells"][column]["Value"]) for r in rows) for column in (debit, credit)]
    for section in report["Rows"]:
        for row in section.get("Rows", []):
            if row.get("RowType") == "SummaryRow":
                row["Cells"][debit]["Value"], row["Cells"][credit]["Value"] = (money(t) for t in totals)


def cents(value: Any) -> int:
    from backend.adapters.xero_dataset import cents as to_cents

    return to_cents(value or 0)


def money(value: int) -> str:
    return f"{value // 100}.{value % 100:02d}"


def check_consistency(snapshot_dir: Path) -> None:
    """副本期末试算表借贷合计为 0, 且副本分录推出的期末余额等于试算表 (plug_entry 为 None); 不一致报科目代码."""
    from backend.adapters.xero_dataset import Snapshot, journal_entries_from_api, plug_entry, trial_balance_cents

    snapshot = Snapshot(snapshot_dir)
    dataset = snapshot.load("dataset")
    start, end = date.fromisoformat(dataset["period_start"]), date.fromisoformat(dataset["period_end"])
    current = trial_balance_cents(snapshot.trial_balance(end))
    if sum(current.values()) != 0:
        raise InjectionError(f"trial_balance_{end.isoformat()}.json: 借贷合计不为 0")
    prior = trial_balance_cents(snapshot.trial_balance(date.fromisoformat(dataset["prior_end"])))
    plug = plug_entry(journal_entries_from_api(snapshot), snapshot.load("accounts"), current, prior, start, end)
    if plug is not None:
        codes = sorted({line.account_code for line in plug.lines})
        raise InjectionError(f"分录推出的 {end.isoformat()} 余额与试算表不符: {', '.join(codes)}")


def generator() -> Any:
    """data/generate_xero_demo.py (以脚本方式导入)."""
    data = str(REPO / "data")
    if data not in sys.path:
        sys.path.insert(0, data)
    import generate_xero_demo

    return generate_xero_demo


def xero_case(
    tmp_path: Path,
    scenario: str | Path | Patch,
    *,
    engine: Any = None,
    evidence_store: Any = None,
    snapshots: Path | None = None,
    supplements: Path | None = None,
) -> XeroCase:
    """按第 8.1 节生成注入副本:
    (1) 记两个原目录的逐文件哈希; (2) 复制到 tmp_path/api/<快照> 与 tmp_path/xero_demo/<快照>; (3) contacts.json 在
    内存中去掉登录账号联系人, raw/ 不复制, 断言副本不含其原值; (4) 逐条执行补丁; (5) tb_shift 调副本期末试算表,
    全部执行后核对借贷合计与分录推出的余额; (6) 重写副本 manifest (injected 为场景名); 收尾再比一次原目录哈希.
    原目录缺省经 tests.external 定位核对: 没有快照时跳过, 仓库跟踪了却找不到时失败. engine 与 evidence_store 传给
    载入器 (连库的场景用; 不传时只用不连库的 tables 等). snapshots 与 supplements 为原目录的上级, 只供本机制的自测
    换成自造目录 (没有快照时同样跳过)."""
    import pytest

    if snapshots is None:
        snapshots = snapshot_dir(SNAPSHOT).parent
    elif not (snapshots / SNAPSHOT).is_dir():
        pytest.skip(NO_SNAPSHOT)
    if supplements is None:
        supplements = supplements_dir(SNAPSHOT).parent
    patch = read_patch(scenario)
    watched = [snapshots / SNAPSHOT, supplements / SNAPSHOT]
    before = {str(d): file_hashes(d) for d in watched}
    api, demo = tmp_path / "api" / SNAPSHOT, tmp_path / "xero_demo" / SNAPSHOT
    if api.exists() or demo.exists():
        raise InjectionError(f"{tmp_path} 下已有副本: 一个目录只能建一个副本 (夹具用 base 参数换子目录)")
    try:
        return build_case(patch, snapshots, supplements, tmp_path, api, demo, engine, evidence_store)
    finally:
        moved = changed(before, {str(d): file_hashes(d) for d in watched})
        if moved:
            raise InjectionError("原目录被改动: " + ", ".join(moved))


def build_case(
    patch: Patch,
    snapshots: Path,
    supplements: Path,
    tmp_path: Path,
    api: Path,
    demo: Path,
    engine: Any,
    evidence_store: Any,
) -> XeroCase:
    """xero_case 的第 2 至 6 步 (原目录哈希的前后比对在 xero_case 里, 失败路径也比)."""
    copy_snapshot(snapshots / SNAPSHOT, api)
    shutil.copytree(supplements / SNAPSHOT, demo)
    roots = {"api": api, "xero_demo": demo}
    period_end = json.loads((api / "dataset.json").read_text(encoding="utf-8"))["period_end"]
    for op in patch.ops:
        if isinstance(op, TbShiftOp):
            path = api / f"trial_balance_{period_end}.json"
            report = json.loads(path.read_text(encoding="utf-8"))
            shift_tb(report, op.code, op.delta_cents)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        elif isinstance(op, RenderOp):
            generator().render_registry_pdfs(demo)
        else:
            apply_op(op, roots)
    check_consistency(api)
    generator().write_manifest(demo, api, injected=patch.scenario)
    leaked = scan_copies(snapshots / SNAPSHOT, [api, demo])
    if leaked:
        raise InjectionError("副本含登录账号联系人的原值: " + ", ".join(leaked))
    loader = XeroDatasetLoader(
        tmp_path / "api",
        PLANNING,
        engine if engine is not None else object(),
        evidence_store if evidence_store is not None else object(),
        supplements=tmp_path / "xero_demo",
        allow_injected=True,
    )
    return XeroCase(patch.scenario, api, demo, loader)


def scan_copies(source: Path, paths: list[Path]) -> list[str]:
    """副本中出现登录账号联系人原值 (取自 source 快照, 只在内存比对) 的文件名."""
    data = str(REPO / "data")
    if data not in sys.path:
        sys.path.insert(0, data)
    import check_xero_demo

    return check_xero_demo.scan_login_contact(source, paths)
