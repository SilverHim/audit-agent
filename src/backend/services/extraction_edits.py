"""审计师修改抽取结果 (需求 R14): 合同, 回函, ASIC 摘录 (需求 R290) 与待审财报 (需求 R217) 的可改字段, 值的校验与
新版本的组装. 纯函数, 不读写存储.

只能改系统抽取出的内容; 改出的新版本 version +1 (摘录的版本号由抽取版本表记), 证据指针不变 (仍指向原文中的位置).
"""

import re
import sys
from datetime import date
from typing import Any

from backend.contracts.errors import DataRejected
from backend.contracts.llm_outputs import RegistryExtraction
from backend.contracts.models import Confirmation, Contract, FinancialStatements, TermType

# 合同可改的字段; 条款以下标指定, 从 0 起, 如 terms[0].text
CONTRACT_FIELDS = (
    "counterparty",
    "counterparty_abn",
    "amount_cents",
    "sign_date",
    "terms[n].text",
    "terms[n].term_type",
)
CONFIRMATION_FIELDS = ("confirming_party", "balance_date", "confirmed_balance_cents")  # 回函可改的字段
# ASIC 摘录可改的字段 (需求 R290): 登记信息与高管的姓名, 职务, 任职日; 高管以下标指定, 从 0 起, 只改不增删; 股东只读
REGISTRY_FIELDS = (
    "name",
    "acn",
    "abn",
    "status",
    "type",
    "registered_date",
    "registered_address",
    "officeholders[n].name",
    "officeholders[n].role",
    "officeholders[n].appointed",
)
OFFICEHOLDER_FIELD = re.compile(r"^officeholders\[(\d+)\]\.(name|role|appointed)$")
# 待审财报可改的字段 (需求 R217): 编制日, 董事声明日, 比较数凭证号与各行的名称, 金额和映射科目; 行以行代码指定, 如
# lines[SOFP-OREC].amount_cents, 只改不增删. 报表, 符号, 小计标记是行的结构, 不改. 两个列表 (比较数凭证号, 映射科目)
# 的值写成逗号分隔的文本 (ExtractionEdit 的值只有文本, 整数与空), 如 "620,621", 空串为空列表
STATEMENTS_FIELDS = (
    "prepared_at",
    "directors_declaration_date",
    "comparative_restatement_entry_nos",
    "lines[code].name",
    "lines[code].amount_cents",
    "lines[code].prior_amount_cents",
    "lines[code].account_codes",
)
LINE_FIELD = re.compile(r"^lines\[([^\]]+)\]\.(name|amount_cents|prior_amount_cents|account_codes)$")
REGISTRY_STATUS = ("Registered", "Deregistered")
ROLES = ("director", "secretary")
TERM_FIELD = re.compile(r"^terms\[(\d+)\]\.(text|term_type)$")
TERM_NO = re.compile(r"-T(\d+)$")  # 条款编号 {合同编号}-T{n} 的序号


def term_position(term_id: str) -> tuple[int, str]:
    """条款的排序键: 按编号的序号 n 升序, 即抽取时的顺序 (按文本排序会把 T10 排在 T2 前); 认不出序号的排最后."""
    match = TERM_NO.search(term_id)
    return (int(match.group(1)) if match else sys.maxsize, term_id)


def ordered_terms(contract: Contract) -> Contract:
    """条款按 term_position 排好的合同. 修改用的下标 terms[n] 与幂等比较都以这个顺序为准, 不依赖存储读出的顺序."""
    terms = sorted(contract.terms, key=lambda t: term_position(t.id))
    return contract if terms == contract.terms else contract.model_copy(update={"terms": terms})


def invalid(message: str) -> DataRejected:
    return DataRejected("INVALID_VALUE", message)


def text_value(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise invalid(f"{field} 须为非空文本")
    return value.strip()


def cents_value(value: Any, field: str, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid(f"{field} 须为整数分")
    return value


def date_value(value: Any, field: str, optional: bool) -> date | None:
    if value is None and optional:
        return None
    try:
        return date.fromisoformat(value) if isinstance(value, str) else _bad()
    except ValueError as exc:
        raise invalid(f"{field} 须为 YYYY-MM-DD 日期") from exc


def _bad() -> date:
    raise ValueError


def abn_value(value: Any) -> str | None:
    if value is None:
        return None
    digits = re.sub(r"\s", "", value) if isinstance(value, str) else ""
    if not re.fullmatch(r"\d{11}", digits):
        raise invalid("counterparty_abn 须为 11 位数字")
    return digits


def apply_contract_edit(contract: Contract, field: str, value: Any) -> Contract:
    """合同改一个字段后的新版本 (条款按 ordered_terms 排好). 条款下标从 0 起, 按 ordered_terms 的顺序.
    字段不认识, 条款下标越界, 值不合法或与当前相同抛 DataRejected(INVALID_VALUE)."""
    update: dict[str, Any]
    contract = ordered_terms(contract)
    term = TERM_FIELD.match(field)
    if term is not None:
        index, part = int(term.group(1)), term.group(2)
        if index >= len(contract.terms):
            raise invalid(f"合同 {contract.id} 没有第 {index} 条 (条款下标从 0 起, 共 {len(contract.terms)} 条)")
        if part == "text":
            new_term = contract.terms[index].model_copy(update={"text": text_value(value, field)})
        else:
            try:
                new_term = contract.terms[index].model_copy(update={"term_type": TermType(value)})
            except ValueError as exc:
                raise invalid(f"{field} 不是条款类型: {value}") from exc
        if new_term == contract.terms[index]:
            raise invalid(f"{field} 与当前值相同")
        terms = [new_term if i == index else t for i, t in enumerate(contract.terms)]
        return contract.model_copy(update={"terms": terms, "version": contract.version + 1})
    if field == "counterparty":
        update = {field: text_value(value, field)}
    elif field == "counterparty_abn":
        update = {field: abn_value(value)}
    elif field == "amount_cents":
        update = {field: cents_value(value, field, optional=True)}
    elif field == "sign_date":
        update = {field: date_value(value, field, optional=True)}
    else:
        raise invalid(f"合同不能改 {field}; 可改: {', '.join(CONTRACT_FIELDS)}")
    if getattr(contract, field) == update[field]:
        raise invalid(f"{field} 与当前值相同")
    return contract.model_copy(update={**update, "version": contract.version + 1})


def apply_confirmation_edit(confirmation: Confirmation, field: str, value: Any) -> Confirmation:
    """回函改一个字段后的函证新版本. 字段不认识, 值不合法或与当前相同抛 DataRejected(INVALID_VALUE)."""
    if field == "confirming_party":
        new: Any = text_value(value, field)
    elif field == "balance_date":
        new = date_value(value, field, optional=False)
    elif field == "confirmed_balance_cents":
        new = cents_value(value, field, optional=False)
    else:
        raise invalid(f"回函不能改 {field}; 可改: {', '.join(CONFIRMATION_FIELDS)}")
    if getattr(confirmation, field) == new:
        raise invalid(f"{field} 与当前值相同")
    return confirmation.model_copy(update={field: new, "version": confirmation.version + 1})


def digits_value(value: Any, field: str, length: int, optional: bool) -> str | None:
    if value is None and optional:
        return None
    digits = re.sub(r"\s", "", value) if isinstance(value, str) else ""
    if not re.fullmatch(rf"\d{{{length}}}", digits):
        raise invalid(f"{field} 须为 {length} 位数字")
    return digits


def choice_value(value: Any, field: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise invalid(f"{field} 须为 {', '.join(choices)} 之一")
    return str(value)


def optional_text(value: Any, field: str) -> str | None:
    return None if value is None else text_value(value, field)


def apply_registry_edit(extraction: RegistryExtraction, field: str, value: Any) -> RegistryExtraction:
    """ASIC 摘录的抽取结果改一个字段后的新内容 (需求 R290). 高管下标从 0 起, 按抽取的顺序. 字段不认识, 下标越界,
    值不合法或与当前相同抛 DataRejected(INVALID_VALUE)."""
    holder = OFFICEHOLDER_FIELD.match(field)
    if holder is not None:
        index, part = int(holder.group(1)), holder.group(2)
        people = extraction.officeholders
        if index >= len(people):
            raise invalid(f"摘录没有第 {index} 名高管 (下标从 0 起, 共 {len(people)} 名)")
        new: Any
        if part == "name":
            new = text_value(value, field)
        elif part == "role":
            new = choice_value(value, field, ROLES)
        else:
            new = date_value(value, field, optional=False)
        if getattr(people[index], part) == new:
            raise invalid(f"{field} 与当前值相同")
        changed = people[index].model_copy(update={part: new})
        return extraction.model_copy(
            update={"officeholders": [changed if i == index else p for i, p in enumerate(people)]}
        )
    if field in ("name", "type"):
        new = text_value(value, field)
    elif field == "acn":
        new = digits_value(value, field, 9, optional=False)
    elif field == "abn":
        new = digits_value(value, field, 11, optional=True)
    elif field == "status":
        new = choice_value(value, field, REGISTRY_STATUS)
    elif field == "registered_date":
        new = date_value(value, field, optional=False)
    elif field == "registered_address":
        new = optional_text(value, field)
    else:
        raise invalid(f"ASIC 摘录不能改 {field}; 可改: {', '.join(REGISTRY_FIELDS)}")
    if getattr(extraction, field) == new:
        raise invalid(f"{field} 与当前值相同")
    return extraction.model_copy(update={field: new})


def list_value(value: Any, field: str) -> list[str]:
    """逗号分隔的文本转成列表: 各项去首尾空白, 整个为空串 (或只有空白) 为空列表; 有空项或重复 INVALID_VALUE."""
    if not isinstance(value, str):
        raise invalid(f"{field} 须为逗号分隔的文本, 空串为空列表")
    if not value.strip():
        return []
    items = [item.strip() for item in value.split(",")]
    if any(not item for item in items):
        raise invalid(f"{field} 有空的一项")
    repeated = sorted({item for item in items if items.count(item) > 1})
    if repeated:
        raise invalid(f"{field} 重复: {', '.join(repeated)}")
    return items


def apply_statements_edit(statements: FinancialStatements, field: str, value: Any) -> FinancialStatements:
    """待审财报改一个字段后的内容 (需求 R217; 版本号由调用方定, 接在已有版本之后). 字段不认识, 没有该行代码, 值不合法
    或与当前相同抛 DataRejected(INVALID_VALUE)."""
    line_field = LINE_FIELD.match(field)
    if line_field is not None:
        code, part = line_field.group(1), line_field.group(2)
        index = next((i for i, x in enumerate(statements.lines) if x.code == code), None)
        if index is None:
            raise invalid(f"待审财报没有行 {code}")
        if part == "name":
            new: Any = text_value(value, field)
        elif part == "account_codes":
            new = list_value(value, field)
        else:
            new = cents_value(value, field, optional=part == "prior_amount_cents")
        line = statements.lines[index]
        if getattr(line, part) == new:
            raise invalid(f"{field} 与当前值相同")
        changed = line.model_copy(update={part: new})
        return statements.model_copy(
            update={"lines": [changed if i == index else x for i, x in enumerate(statements.lines)]}
        )
    if field == "prepared_at":
        new = date_value(value, field, optional=False)
    elif field == "directors_declaration_date":
        new = date_value(value, field, optional=True)
    elif field == "comparative_restatement_entry_nos":
        new = list_value(value, field)
    else:
        raise invalid(f"待审财报不能改 {field}; 可改: {', '.join(STATEMENTS_FIELDS)}")
    if getattr(statements, field) == new:
        raise invalid(f"{field} 与当前值相同")
    return statements.model_copy(update={field: new})
