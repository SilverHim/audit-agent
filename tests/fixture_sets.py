"""大模型 fixture 的数据集登记表 (R77): 每个数据集一行, tests/test_knowledge.py 的两个 fixture 守卫按它参数化.

约定 (接口文档第 4 节 MockLLMClient): 新程序的 fixture 按数据集放, 运行副本由该数据集的生成脚本从测试 fixture 同步.
Numbat 为 tests/fixtures/llm 与 data/demo/llm, 不再往这里加新程序的 fixture; Xero 演示为 tests/fixtures/llm_xero 与
data/xero_demo/20261017-q1/llm (R78, 另有 tests/test_xero_fixtures.py 的守卫).
"""

import csv
import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from tests.external import supplements_dir, supplements_path

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "data" / "demo"  # Numbat 合成数据集 (合成数据集设计第 1 节)
UPLOADS = REPO / "tests" / "fixtures" / "llm_variants" / "upload"  # 上传处理的固定输出, 各数据集共用一份
# 上传处理的固定输出中, 演示也用而须同步进 Numbat 运行副本的程序: 文件分类, 回函抽取, 账表识别与 ASIC 摘录抽取
# (没有数据集的新客户也读 Numbat 的运行副本, 需求 R290)
UPLOAD_RUNTIME = ("DOC-CLASSIFY", "DOC-06", "DOC-SHEET", "DOC-REGISTRY")


# 按程序给出 fixture 的 document 指针应指向的文件: (程序, fixture_key) → 允许的 PDF (manifest 中的 path, 一组); 程序没有
# 登记对应时为 None (只核对指针在 manifest 中且页码不超过页数); 登记了而对象没有对应文件时抛 LookupError (这样的
# fixture 不应有 document 指针)
ExpectedDocuments = Callable[[str, str], frozenset[str] | None]


@dataclass(frozen=True)
class FixtureSet:
    """一个数据集的大模型 fixture."""

    name: str  # 登记名, 作 pytest 参数 ID
    dataset: str  # 数据集名 (Engagement.dataset)
    fixtures: Path  # 测试 fixture 根: {程序}/{fixture_key}.json
    runtime: Path  # 运行副本根: 生成脚本从 fixtures 同步, 内容须相同; 不存在时守卫报 "运行副本未生成"
    manifest: Path  # 登记 PDF 的 manifest (documents 段), document 指针须指向其中的文件
    # document 指针应指向的 PDF (见 ExpectedDocuments); 为空时只核对指针在 manifest 中且页码不超过页数
    expected_document: ExpectedDocuments | None = None
    # 上传文件处理用的固定输出根 ({程序}/{键}.json), 不进主固定输出 (主流程要求主固定输出各用一次); 没有为 None
    uploads: Path | None = None
    uploads_in_runtime: tuple[str, ...] = ()  # uploads 中也须在运行副本里的程序
    # 数据目录在外部数据定位之下的 (Xero 补造): 读 runtime 与 manifest 之前调用, 经 tests.external 核对 (仓库跟踪了却
    # 找不到时失败, 没有时跳过); 随仓库的 data/demo 为 None
    locate: Callable[[], Path] | None = None

    def ready(self) -> None:
        """读 runtime 与 manifest 之前调用: 有 locate 时先定位核对."""
        if self.locate is not None:
            self.locate()

    def documents(self) -> dict[str, dict[str, Any]]:
        """manifest 登记的 PDF: 文档 ID -> 条目 (path, type, page_count, sha256 等)."""
        self.ready()
        documents = json.loads(self.manifest.read_text(encoding="utf-8"))["documents"]
        return {d["document_id"]: d for d in documents}


@cache
def demo_rows(table: str) -> dict[str, dict[str, str]]:
    """data/demo 下某张表的 CSV: id -> 行."""
    with (DEMO / f"{table}.csv").open(encoding="utf-8", newline="") as f:
        return {row["id"]: row for row in csv.DictReader(f)}


# GEN-09 (A7 复核, 键为 {判断对象}-r1, -r2) 按判断对象的前缀找被复核判断的程序; 前缀没登记的不核对
JUDGED_BY = {"BA": "CSH-02", "CON": "REV-03", "SI": "REV-02"}


def expected_documents(
    invoice_contract: Callable[[str], str | None], account_no: Callable[[str], str | None]
) -> ExpectedDocuments:
    """数据集的期望 PDF 函数, 各数据集只在两处查表不同: invoice_contract 为销售发票 ID → 关联的合同编号, account_no
    为银行账户 ID → 账号 (没有时都为 None). 登记的对应: DOC-04, REV-03 为该合同 contracts/{对象}.pdf; REV-02 为发票
    关联的合同; DOC-06, CSH-02 为该账户的回函 confirmations/{账号}.pdf; GEN-09 同被复核判断的程序 (JUDGED_BY).
    新程序的 fixture 要核对应时在这里登记 (可给多份文件)."""

    def expected(procedure_id: str, fixture_key: str) -> frozenset[str] | None:
        if procedure_id == "GEN-09":
            subject = fixture_key.rsplit("-r", 1)[0]
            judged_by = JUDGED_BY.get(subject.split("-")[0])
            return expected(judged_by, subject) if judged_by is not None else None
        if procedure_id in ("DOC-04", "REV-03"):
            return frozenset({f"contracts/{fixture_key}.pdf"})
        if procedure_id == "REV-02":
            contract = invoice_contract(fixture_key)
            if contract is None:
                raise LookupError(f"发票 {fixture_key} 没有关联合同")
            return frozenset({f"contracts/{contract}.pdf"})
        if procedure_id in ("DOC-06", "CSH-02"):
            number = account_no(fixture_key)
            if number is None:
                raise LookupError(f"银行账户 {fixture_key} 不在账户表中")
            return frozenset({f"confirmations/{number}.pdf"})
        return None

    return expected


# Numbat: 发票的合同与账户的账号取 data/demo 的 sales_invoices.csv, bank_accounts.csv
numbat_expected_document = expected_documents(
    lambda key: demo_rows("sales_invoices").get(key, {}).get("contract_id") or None,
    lambda key: demo_rows("bank_accounts").get(key, {}).get("account_no") or None,
)


NUMBAT = FixtureSet(
    name="numbat",
    dataset="demo",
    fixtures=REPO / "tests" / "fixtures" / "llm",
    runtime=DEMO / "llm",
    manifest=DEMO / "manifest.json",
    expected_document=numbat_expected_document,
    uploads=UPLOADS,
    uploads_in_runtime=UPLOAD_RUNTIME,
)
XERO_DEMO = supplements_path("20261017-q1")  # Xero 演示的补造目录 (只拼路径; 用例运行时经 locate 核对)
XERO = FixtureSet(
    name="xero",
    dataset="xero/20261017-q1",
    fixtures=REPO / "tests" / "fixtures" / "llm_xero",
    runtime=XERO_DEMO / "llm",
    manifest=XERO_DEMO / "manifest.json",
    uploads=UPLOADS,
    uploads_in_runtime=("DOC-SHEET",),  # 账表识别按上传的文件名取, Xero 演示项目里上传网页导出时用 (生成脚本复制)
    locate=lambda: supplements_dir("20261017-q1"),
)
FIXTURE_SETS: list[FixtureSet] = [NUMBAT, XERO]
# 按登记表参数化用: 读外部数据定位之下目录的那一行标 external
FIXTURE_PARAMS = [pytest.param(fs, id=fs.name, marks=pytest.mark.external if fs.locate else ()) for fs in FIXTURE_SETS]
