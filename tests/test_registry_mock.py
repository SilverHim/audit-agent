"""需求 R129: 登记 mock 的公司摘录与人员检索 (asic_extract, asic_person). 读 Xero 补造目录 registry/ 的 JSON 与同名
PDF; 按项目数据集定位 (DatasetRegistrySource) 同 ABN 与 ASIC 查询, 组装根的组合实现 (CompositeRegistrySource) 对
新建的客户取上传的 ASIC 摘录 (R290), 没有人员检索的来源. 不连库."""

import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from backend.adapters.registry import CompositeRegistrySource
from backend.adapters.registry_mock import DatasetRegistrySource, MockRegistrySource, person_stem
from backend.contracts.errors import RegistryUnavailable
from backend.contracts.models import AsicExtract, PersonSearch
from tests.external import require_xero_supplements
from tests.fakes import ENGAGEMENT, FakeAbr, FakeEngagementStore, FakeRegistry, no_uploads
from tests.xero_fakes import XERO_DATASET

EXTERNAL = pytest.mark.external  # 读补造目录的用例 (经 require_xero_supplements 定位); 纯函数用例不标
DEMO = Path(__file__).resolve().parents[1] / "data" / "demo"
BIRTH = date(1979, 4, 12)
DATASETS = (XERO_DATASET, "demo", "xero/20261017-mock", None)  # 项目 E-0 至 E-3 的数据集 (E-3 为新建的客户)


@pytest.fixture(scope="module")
def root() -> Path:
    return require_xero_supplements("20261017-q1")


@pytest.fixture
def copy(root: Path, tmp_path: Path) -> Path:
    """补造目录 registry/ 的临时副本 (只在副本里改)."""
    shutil.copytree(root / "registry", tmp_path / "registry")
    return tmp_path


def by_dataset(root: Path) -> DatasetRegistrySource:
    """四个项目: Xero q1 (补造目录), Numbat (data/demo, 没有摘录与检索), 20261017-mock (没有补造目录), 没有数据集."""
    engagements = [ENGAGEMENT.model_copy(update={"id": f"E-{n}", "dataset": d}) for n, d in enumerate(DATASETS)]
    directories = {XERO_DATASET: root, "demo": DEMO, "xero/20261017-mock": None}
    return DatasetRegistrySource(FakeEngagementStore([], engagements), lambda dataset: directories[dataset])


@EXTERNAL
async def test_extract_of_the_demo_company(root: Path) -> None:
    """900043021 的摘录 (按 PDF 上的写法 900 043 021 查, 去空白): 登记信息, 高管两项 (同一人任董事与秘书), 股东两名
    (Sam Whitfield 没有出生日期), 原件 PDF."""
    extract = await by_dataset(root).asic_extract("E-0", "900 043 021")
    assert extract is not None
    assert (extract.name, extract.type, extract.company_class, extract.subclass, extract.paid_up_cents) == (
        "Demo Company (AU) Pty Ltd",
        "APTY",
        "LMSH",
        "PROP",
        100,
    )
    assert [(o.name, o.birth_date, o.role, o.appointed) for o in extract.officeholders] == [
        ("Alex Morgan", BIRTH, "director", date(2018, 9, 3)),
        ("Alex Morgan", BIRTH, "secretary", date(2018, 9, 3)),
    ]
    assert [(s.name, s.kind, s.acn, s.share_class, s.shares, s.pct) for s in extract.shareholders] == [
        ("Alex Morgan", "person", None, "ORD", 85, 0.85),
        ("Sam Whitfield", "person", None, "ORD", 15, 0.15),
    ]
    pdf = root / "registry" / "extracts" / "900043021.pdf"
    assert (extract.file_name, extract.file_bytes) == ("900043021.pdf", pdf.read_bytes())
    assert extract.model_dump(by_alias=True)["class"] == "LMSH"  # JSON 键 class 与模型字段 company_class 互通


@EXTERNAL
async def test_person_search_of_the_director(root: Path) -> None:
    """Morgan Alex 1979-04-12: 读 MORGAN-ALEX-19790412.json, 7 项身份按检索结果的顺序 (设计稿第 2.5 节)."""
    person = await by_dataset(root).asic_person("E-0", "Morgan", "Alex", BIRTH)
    assert person is not None
    assert [(r.acn, r.role, r.shares) for r in person.roles] == [
        ("900043021", "director", None),
        ("900043021", "secretary", None),
        ("900043021", "shareholder", 85),
        ("900043030", "director", None),
        ("900043030", "shareholder", 50),
        ("900043049", "director", None),
        ("900043049", "shareholder", 100),
    ]
    pdf = root / "registry" / "persons" / "MORGAN-ALEX-19790412.pdf"
    assert (person.file_name, person.file_bytes) == (pdf.name, pdf.read_bytes())


@EXTERNAL
def test_every_registry_json_reads_into_the_models(root: Path) -> None:
    """补造目录的 4 份 JSON (3 份摘录, 1 份检索) 加同名 PDF 都能按模型读出."""
    mock = MockRegistrySource(root)
    extracts = [mock.extract(p.stem) for p in sorted((root / "registry" / "extracts").glob("*.json"))]
    keys = [json.loads(p.read_text(encoding="utf-8")) for p in (root / "registry" / "persons").glob("*.json")]
    persons = [mock.person(k["family_name"], k["given_name"], date.fromisoformat(k["birth_date"])) for k in keys]
    assert [type(e) for e in extracts] == [AsicExtract] * 3
    assert [type(p) for p in persons] == [PersonSearch]


@EXTERNAL
@pytest.mark.parametrize(
    ("eid", "query"),
    [
        ("E-0", ("extract", "900049999")),  # 没有这家的摘录
        ("E-0", ("extract", "../asic")),  # 不是 ACN, 不拼文件名 (否则读到 registry/asic.json)
        ("E-0", ("person", "Morgan", "Alexander", BIRTH)),  # 名不符, 没有这个文件
        ("E-0", ("person", "Morgan", "Alex", date(1979, 4, 13))),  # 出生日期不符
        ("E-1", ("extract", "900042015")),  # Numbat 的 registry/ 没有摘录
        ("E-1", ("person", "Morgan", "Alex", BIRTH)),
        ("E-2", ("extract", "900043021")),  # 20261017-mock 没有补造目录
        ("E-2", ("person", "Morgan", "Alex", BIRTH)),
    ],
)
async def test_not_found_is_none(root: Path, eid: str, query: tuple[object, ...]) -> None:
    source = by_dataset(root)
    kind, *args = query
    found = await (source.asic_extract(eid, *args) if kind == "extract" else source.asic_person(eid, *args))  # type: ignore[arg-type]
    assert found is None


@EXTERNAL
def test_missing_pdf_is_an_error(copy: Path) -> None:
    """有 JSON 而缺同名 PDF 是数据不全, 抛 FileNotFoundError (不当作查不到)."""
    (copy / "registry" / "extracts" / "900043021.pdf").unlink()
    with pytest.raises(FileNotFoundError):
        MockRegistrySource(copy).extract("900043021")


@EXTERNAL
def test_person_search_checks_the_names_in_the_file(copy: Path) -> None:
    """文件名以连字符分隔, (Smith, Jones-Ann) 与 (Smith-Jones, Ann) 拼出同一文件名: 读到后核对 JSON 里的姓名, 只有
    (Smith-Jones, Ann) 查得到."""
    source = copy / "registry" / "persons" / "MORGAN-ALEX-19790412"
    target = source.with_name("SMITH-JONES-ANN-19790412")
    data = json.loads(source.with_suffix(".json").read_text(encoding="utf-8"))
    data.update(family_name="Smith-Jones", given_name="Ann", file_name=f"{target.name}.pdf")
    target.with_suffix(".json").write_text(json.dumps(data), encoding="utf-8")
    shutil.copyfile(source.with_suffix(".pdf"), target.with_suffix(".pdf"))
    mock = MockRegistrySource(copy)
    assert mock.person("Smith", "Jones-Ann", BIRTH) is None
    found = mock.person("Smith-Jones", "Ann", BIRTH)
    assert found is not None and (found.family_name, found.given_name) == ("Smith-Jones", "Ann")


def test_person_file_name() -> None:
    """人员检索的文件名: 姓与名去空白转大写, 出生日期 yyyymmdd; 姓名里有路径字符 (或别的字符) 时不拼文件名."""
    assert person_stem(" Morgan", "alex ", BIRTH) == "MORGAN-ALEX-19790412"
    assert person_stem("O'Neil-Smith", "Mary Ann", BIRTH) == "O'NEIL-SMITH-MARY ANN-19790412"
    assert [person_stem(f, "Alex", BIRTH) for f in ("../Morgan", "Morgan/x", "", "'Morgan", "Zoë")] == [None] * 5


@pytest.mark.parametrize("composite", [False, True], ids=["dataset", "composite"])
@EXTERNAL
async def test_unknown_engagement_and_new_client(root: Path, composite: bool) -> None:
    """项目不存在抛 LookupError; 没有数据集 (新建的客户, E-3) 抛 RegistryUnavailable: 登记 mock 一侧都是 no_source;
    组合实现的摘录取上传的 (R290), 一份都没有为 no_extract, 人员检索仍 no_source. 组合实现对有数据集的项目转给数据集
    一侧, 都不发 ABR 请求."""
    abr = FakeAbr()
    datasets = by_dataset(root)
    source = (
        CompositeRegistrySource(datasets.engagements, datasets, abr.client(), no_uploads()) if composite else datasets
    )
    assert (await source.asic_extract("E-0", "900043021")) is not None
    assert (await source.asic_person("E-0", "Morgan", "Alex", BIRTH)) is not None
    for eid, error in (("E-9", LookupError), ("E-3", RegistryUnavailable)):
        with pytest.raises(error) as extract_error:
            await source.asic_extract(eid, "900043021")
        with pytest.raises(error) as person_error:
            await source.asic_person(eid, "Morgan", "Alex", BIRTH)
        if error is RegistryUnavailable:
            reasons = ["no_extract" if composite else "no_source", "no_source"]
            assert [getattr(e.value, "reason", None) for e in (extract_error, person_error)] == reasons
    assert abr.requests == []


@EXTERNAL
async def test_fake_registry_records_query_order(root: Path) -> None:
    """FakeRegistry 按顺序记下 abn, extract, person 三类查询; 人员检索同登记 mock 去首尾空白, 不分大小写, 出生日期
    不符查不到."""
    mock = MockRegistrySource(root)
    extract, person = mock.extract("900043021"), mock.person("Morgan", "Alex", BIRTH)
    assert extract is not None and person is not None
    registry = FakeRegistry(extracts={"900043021": extract}, persons=[person])
    await registry.abn("E", "25900043021")
    assert await registry.asic_extract("E", "900043021") == extract
    assert await registry.asic_person("E", "MORGAN ", "alex", BIRTH) == person
    assert await registry.asic_person("E", "Morgan", "Alex", date(1979, 4, 13)) is None
    assert registry.queries == [
        ("abn", "25900043021"),
        ("extract", "900043021"),
        ("person", "MORGAN  alex 1979-04-12"),
        ("person", "Morgan Alex 1979-04-13"),
    ]
