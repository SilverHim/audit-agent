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

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "data" / "demo"  # Numbat 合成数据集 (合成数据集设计第 1 节)
UPLOADS = REPO / "tests" / "fixtures" / "llm_variants" / "upload"  # 上传处理的固定输出, 各数据集共用一份
# 上传处理的固定输出中, 演示也用而须同步进 Numbat 运行副本的程序: 文件分类, 回函抽取与账表识别
UPLOAD_RUNTIME = ("DOC-CLASSIFY", "DOC-06", "DOC-SHEET")


@dataclass(frozen=True)
class FixtureSet:
    """一个数据集的大模型 fixture."""

    name: str  # 登记名, 作 pytest 参数 ID
    dataset: str  # 数据集名 (Engagement.dataset)
    fixtures: Path  # 测试 fixture 根: {程序}/{fixture_key}.json
    runtime: Path  # 运行副本根: 生成脚本从 fixtures 同步, 内容须相同; 不存在时守卫报 "运行副本未生成"
    manifest: Path  # 登记 PDF 的 manifest (documents 段), document 指针须指向其中的文件
    # (程序, fixture_key) -> document 指针应指向的 PDF (manifest 中的 path);
    # 为空时只核对指针在 manifest 中且页码不超过页数
    expected_document: Callable[[str, str], str] | None = None
    # 上传文件处理用的固定输出根 ({程序}/{键}.json), 不进主固定输出 (主流程要求主固定输出各用一次); 没有为 None
    uploads: Path | None = None
    uploads_in_runtime: tuple[str, ...] = ()  # uploads 中也须在运行副本里的程序

    def documents(self) -> dict[str, dict[str, Any]]:
        """manifest 登记的 PDF: 文档 ID -> 条目 (path, type, page_count, sha256 等)."""
        documents = json.loads(self.manifest.read_text(encoding="utf-8"))["documents"]
        return {d["document_id"]: d for d in documents}


@cache
def demo_rows(table: str) -> dict[str, dict[str, str]]:
    """data/demo 下某张表的 CSV: id -> 行."""
    with (DEMO / f"{table}.csv").open(encoding="utf-8", newline="") as f:
        return {row["id"]: row for row in csv.DictReader(f)}


def numbat_expected_document(procedure_id: str, fixture_key: str) -> str:
    """Numbat fixture 的 document 指针应指向的 PDF (相对 data/demo): DOC-04, REV-03 为该合同; REV-02 为发票关联的合同;
    DOC-06, CSH-02 为该银行账户的回函, 以账号命名; GEN-09 (A7 复核, 键为 {判断对象}-r1, -r2) 同被复核判断的程序."""
    if procedure_id == "GEN-09":
        subject = fixture_key.rsplit("-r", 1)[0]
        judged_by = {"BA": "CSH-02", "CON": "REV-03", "SI": "REV-02"}[subject.split("-")[0]]
        return numbat_expected_document(judged_by, subject)
    if procedure_id in ("DOC-04", "REV-03"):
        return f"contracts/{fixture_key}.pdf"
    if procedure_id == "REV-02":
        return f"contracts/{demo_rows('sales_invoices')[fixture_key]['contract_id']}.pdf"
    if procedure_id in ("DOC-06", "CSH-02"):
        return f"confirmations/{demo_rows('bank_accounts')[fixture_key]['account_no']}.pdf"
    raise LookupError(f"{procedure_id} 的 fixture 不应有 document 指针")


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
XERO_DEMO = REPO / "data" / "xero_demo" / "20261017-q1"  # Xero 演示的补造目录
XERO = FixtureSet(
    name="xero",
    dataset="xero/20261017-q1",
    fixtures=REPO / "tests" / "fixtures" / "llm_xero",
    runtime=XERO_DEMO / "llm",
    manifest=XERO_DEMO / "manifest.json",
    uploads=UPLOADS,
    uploads_in_runtime=("DOC-SHEET",),  # 账表识别按上传的文件名取, Xero 演示项目里上传网页导出时用 (生成脚本复制)
)
FIXTURE_SETS: list[FixtureSet] = [NUMBAT, XERO]
