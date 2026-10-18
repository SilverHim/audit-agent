"""科目映射: 把各公司, 各 ERP 的科目归入标准分类 (records.AccountCategory).

映射分三层, 前一层给出结果就不再往下:
1. 连接器用 ERP 自带的结构化信息 (如 Xero 的科目类型, 系统科目, 报告代码), 见各连接器;
2. 本模块按科目类别与名称关键词归类, 适用于英文科目表, 任何连接器都可兜底;
3. 仍认不出的为 None (未映射), 留给后续的 LLM 建议与审计师确认.
"""

from typing import get_args

from backend.contracts.records import Account, AccountCategory

CATEGORIES: tuple[str, ...] = get_args(AccountCategory)  # 全部标准分类
# 按科目类别的关键词规则, 自上而下取第一个命中的; 关键词与小写名称做子串匹配
NAME_RULES: dict[str, list[tuple[tuple[str, ...], AccountCategory]]] = {
    "asset": [
        (("cash", "bank", "petty", "term deposit"), "cash"),
        (("receivable", "debtor", "credit loss", "doubtful"), "receivables"),
        (("inventory", "stock on hand", "finished goods", "raw material", "work in progress"), "inventory"),
        (("prepay", "prepaid"), "prepayments"),
        (("goodwill", "intangible", "software", "patent", "trademark", "licence"), "intangible_assets"),
        (
            ("plant", "equipment", "vehicle", "building", "land", "furniture", "fixture", "computer", "leasehold"),
            "property_plant_equipment",
        ),
    ],
    "liability": [
        (("tax", "gst", "payg", "bas "), "tax_payable"),
        (("employee", "wage", "salar", "leave", "superannuation", "payroll"), "employee_provisions"),
        (("loan", "borrowing", "overdraft", "lease liabilit", "hire purchase", "credit card"), "borrowings"),
        (("accru",), "accruals"),
        (("payable", "creditor"), "payables"),
    ],
    "equity": [
        (("capital", "share"), "share_capital"),
        (("retained", "accumulated profit", "accumulated loss", "current year earnings"), "retained_earnings"),
        (("",), "other_equity"),
    ],
    "revenue": [
        (("interest", "dividend", "gain", "rental income", "sundry", "other"), "other_income"),
        (("",), "revenue"),
    ],
    "expense": [
        (("income tax", "tax expense"), "income_tax"),
        (("cost of goods", "cost of sales", "cogs", "purchases", "direct cost"), "cost_of_sales"),
        (("depreciation", "amortisation", "amortization"), "depreciation_amortisation"),
        (("interest expense", "finance cost", "borrowing cost", "loan interest"), "finance_costs"),
        (("salar", "wage", "superannuation", "payroll", "employee"), "employee_expenses"),
        (("",), "operating_expenses"),
    ],
}


def classify_by_name(name: str, account_type: str) -> AccountCategory | None:
    """按科目类别与名称关键词归类; 资产, 负债认不出时为 None, 权益, 收入, 费用有兜底分类."""
    lowered = f" {name.lower()} "
    for keywords, category in NAME_RULES.get(account_type, []):
        if any(k in lowered for k in keywords):
            return category
    return None


def with_category(account: Account) -> Account:
    """科目没有分类时按名称补上, 依据记 name (认不出的分类与依据都为空); 已有分类的不改."""
    if account.category is not None:
        return account
    category = classify_by_name(account.name, account.type)
    return account.model_copy(update={"category": category, "category_basis": "name" if category else None})
