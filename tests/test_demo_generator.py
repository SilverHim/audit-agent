"""Numbat 合成数据集的生成脚本 (data/generate_demo.py, 需求 R284): 各表的列写成常量, 与 data/demo 的表头逐字相同,
不随契约给记录模型新加的可选字段变化. 只比对表头, 不重生成 (完整的确定性检查见 uv run python data/check_demo.py)."""

import csv
import importlib.util
from pathlib import Path
from types import ModuleType

from backend.contracts import records
from tests.fixture_sets import UPLOAD_RUNTIME

DATA = Path(__file__).resolve().parents[1] / "data"
MODELS = {
    "accounts": records.Account,
    "trial_balance": records.TrialBalanceRow,
    "journal_entries": records.JournalEntry,
    "sales_invoices": records.SalesInvoice,
    "delivery_notes": records.DeliveryNote,
    "bank_accounts": records.BankAccount,
    "bank_transactions": records.BankTransaction,
}


def generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("generate_demo", DATA / "generate_demo.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def header(name: str) -> tuple[str, ...]:
    with (DATA / "demo" / f"{name}.csv").open(newline="", encoding="utf-8") as stream:
        return tuple(next(csv.reader(stream)))


def test_generator_columns_match_demo_headers() -> None:
    """每张表的列与 data/demo 的表头逐字相同 (漏写, 多写或换了顺序都失败); 列都是记录模型的字段, 模型多出的可选字段
    (如 Account.category, JournalEntry.approved_at) 不进原始数据."""
    columns = generator().COLUMNS
    assert sorted(columns) == sorted(p.stem for p in (DATA / "demo").glob("*.csv"))
    for name, expected in columns.items():
        assert expected == header(name), name
        assert set(expected) <= set(MODELS[name].model_fields), name


def test_generator_copies_the_same_upload_fixtures_as_the_guard() -> None:
    """运行副本 data/demo/llm 里同步的上传处理固定输出, 生成脚本与 test_knowledge 的守卫用同一份清单."""
    assert generator().UPLOAD_RUNTIME == UPLOAD_RUNTIME
