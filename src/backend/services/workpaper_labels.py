"""底稿固定文字的中英词表; 原始客户资料与生成的判断文字不在这里翻译."""

from backend.contracts.models import LocalizedText


def words(zh: str, en: str) -> LocalizedText:
    return LocalizedText(zh=zh, en=en)


PROCEDURE_NAMES: dict[str, str] = {
    "ACC-01": "Engagement acceptance",
    "ACC-04": "ASIC and ABN check",
    "ACC-08": "Engagement terms and appointment check",
    "DOC-04": "Contract term extraction",
    "PLN-07": "Preliminary analytical procedures",
    "PLN-01": "Materiality",
    "PLN-03": "Risk register sign-off",
    "PLN-15": "Overall audit plan",
    "REV-08": "Three-way match",
    "REV-03": "Revenue contract terms",
    "REV-02": "Revenue cut-off",
    "CSH-04": "Bank confirmation requests",
    "DOC-06": "Confirmation reply extraction",
    "CSH-05": "Bank statement to ledger",
    "CSH-02": "Confirmation differences",
    "FIN-03": "Misstatement classification",
    "FIN-08": "Misstatement communication and management responses",
    "FIN-09": "Correction verification",
    "FIN-04": "Misstatement accumulation",
    "FIN-01": "Uncorrected misstatements",
    "RPT-03": "Report drafting",
    "RPT-01": "Report approval and sign-off",
    "GEN-05": "Full journal entry scan",
    "GEN-02": "Journal entry exception attribution",
    "GEN-03": "Related party analysis",
    "GEN-06": "Subsequent events evaluation",
    "GEN-07": "Subsequent events monitoring",
    "GEN-08": "Ownership and related party map",
    "GEN-09": "Independent review of judgments",
}

AGENTS = {
    "A1": ("编排智能体", "Orchestrator"),
    "A2": ("客户情报智能体", "Client intelligence agent"),
    "A3": ("数据管理智能体", "Ingest agent"),
    "A4": ("风险评估智能体", "Risk assessor"),
    "A5": ("证据智能体", "Evidence agent"),
    "A6": ("实质性测试智能体", "Fieldwork agent"),
    "A7": ("独立复核智能体", "Independent review agent"),
    "A8": ("工作底稿智能体", "Workpaper agent"),
    "A9": ("质量检查智能体", "Quality control agent"),
    "A10": ("审计助理", "Concierge"),
}
SECTIONS = {
    "objectives": words("目标与风险", "Objectives and risks"),
    "basis": words("依据", "Basis"),
    "procedures": words("程序 (性质, 时间与范围)", "Procedures (nature, timing and extent)"),
    "checks": words("检查项定义", "Check definitions"),
    "details": words("测试明细", "Test details"),
    "exceptions": words("例外与处理", "Exceptions and disposition"),
    "conclusion": words("结论", "Conclusion"),
    "cross_references": words("交叉索引", "Cross-references"),
    "review": words("复核意见", "Review comments"),
}
FIELDS = {
    "before": ("修改前", "Before"),
    "after": ("修改后", "After"),
    "withdrawn_at": ("撤回时间", "Withdrawn at"),
    "field": ("字段", "Field"),
    "value": ("取值", "Value"),
    "page": ("页码", "Page"),
    "quote": ("引用原文", "Original text"),
    "term_type": ("条款类型", "Term type"),
    "document_id": ("文件", "Document"),
    "counterparty": ("合同对方", "Counterparty"),
    "sign_date": ("签订日", "Signing date"),
    "changed_by": ("修改人", "Changed by"),
    "changed_at": ("修改时间", "Changed at"),
    "reason": ("理由", "Reason"),
    "decision": ("决定", "Decision"),
    "reviewer": ("复核人", "Reviewer"),
    "reviewed_at": ("复核时间", "Reviewed at"),
    "step": ("步骤", "Step"),
    "from_steps": ("所引步骤", "Referenced steps"),
    "confidence": ("置信度", "Confidence"),
    "conclusion": ("结论", "Conclusion"),
    "recommendation": ("建议", "Recommendation"),
    "balance_date": ("余额截至日", "Balance date"),
    "confirmed_balance_cents": ("回函余额", "Confirmed balance"),
    "accrued_interest_cents": ("应计利息", "Accrued interest"),
    "marker": ("标记", "Mark"),
    "source_id": ("对象", "Item"),
    "invoice_no": ("发票号", "Invoice"),
    "customer": ("客户", "Customer"),
    "contract_id": ("合同", "Contract"),
    "posting_date": ("入账日期", "Posting date"),
    "amount_cents": ("金额", "Amount"),
    "journal_entry_id": ("分录", "Journal line"),
    "delivery_no": ("服务完成记录", "Service record"),
    "signed_date": ("签收日", "Signed date"),
    "signed_by": ("签收人", "Signed by"),
    "bank_transaction_ids": ("收款流水", "Receipt transactions"),
    "received_dates": ("收款日", "Receipt dates"),
    "received_cents": ("收款金额", "Receipts"),
    "remarks": ("备注", "Notes"),
    "kind": ("类型", "Type"),
    "id": ("编号", "Reference"),
    "version": ("版本", "Version"),
    "check": ("检查项", "Check"),
    "scope": ("范围", "Scope"),
    "count": ("数量", "Count"),
    "status": ("状态", "Status"),
    "description": ("说明", "Description"),
    "difference_cents": ("差额", "Difference"),
    "judgment": ("判断", "Judgment"),
    "evidence": ("证据出处", "Evidence"),
    "entry_no": ("凭证号", "Journal entry"),
    "account_code": ("科目", "Account"),
    "source_doc": ("单据号", "Source document"),
    "debit_cents": ("借方", "Debit"),
    "credit_cents": ("贷方", "Credit"),
    "bank_account_id": ("银行账户", "Bank account"),
    "txn_date": ("交易日", "Transaction date"),
    "reference": ("参考号", "Reference"),
    "records": ("服务完成记录", "Service records"),
    "account": ("科目", "Account"),
    "assertion": ("认定", "Assertion"),
    "risk": ("风险", "Risk"),
    "bank_name": ("银行", "Bank"),
    "account_no": ("账户号码", "Account number"),
    "gl_account_code": ("总账科目代码", "General ledger account code"),
    "statement_balance_cents": ("对账单余额", "Statement balance"),
    "book_balance_cents": ("账面余额", "Book balance"),
    "last_bank_transaction_id": ("最后一笔银行流水", "Last bank transaction"),
    "account_name": ("科目名称", "Account name"),
    "current_cents": ("本期末余额", "Current balance"),
    "prior_cents": ("上期末余额", "Prior balance"),
    "opening_cents": ("期初余额", "Opening balance"),
    "basis": ("比较基准", "Comparison basis"),
    "change_pct": ("变动比例", "Movement percentage"),
    "period": ("期间", "Period"),
    "month": ("月份", "Month"),
    "revenue_cents": ("收入", "Revenue"),
    "expense_cents": ("费用", "Expenses"),
    "employee_expense_cents": ("员工费用", "Employee expenses"),
    "cost_of_sales_cents": ("销售成本", "Cost of sales"),
}
PENDING = words("待签署", "Awaiting sign-off")
NOT_RECORDED = words("被测项目未记录", "Items tested not recorded")
NOT_AVAILABLE = words("未记录", "Not recorded")
SCOPES = {
    "full": words("全部明细", "All details"),
    "sample": words("抽查", "Sample"),
    "conclusion_only": words("只看结论", "Conclusion only"),
}
DISPOSITIONS = {
    "open": words("待处理", "Open"),
    "unattributed": words("未归因", "Unattributed"),
    "attributed": words("已由判断解释", "Explained by judgment"),
}
ASSERTIONS = {
    key: words(zh, key)
    for key, zh in {
        "Existence": "存在",
        "Completeness": "完整性",
        "Accuracy": "准确性",
        "Valuation": "计价",
        "Rights": "权利和义务",
        "Occurrence": "发生",
        "Cutoff": "截止",
        "Classification": "分类",
        "Presentation": "列报",
    }.items()
}


def field_label(key: str) -> LocalizedText:
    return words(*FIELDS[key]) if key in FIELDS else words(key, key.replace("_cents", "").replace("_", " "))


ROW_CHECKS = {
    ("CSH-02", "D02"): "Reconciling candidates: subsequent bank transactions",
    ("CSH-02", "D03"): "Reconciling candidates: pre-year-end bank ledger entries",
    ("CSH-02", "D04"): "Ledger candidates for accrued interest on confirmations",
    ("FIN-09", "V01"): "All lines of audit adjustment entries",
    ("GEN-03", "C01"): "Transactions with related party candidates",
    ("GEN-03", "C02"): "Equity journal entries",
    ("GEN-06", "E01"): "Journal lines from period start to the window end",
    ("GEN-06", "E02"): "Sales invoices posted from period start to the window end",
    ("PLN-01", "M01"): "Materiality benchmark amounts",
    ("PLN-07", "A02"): "Ratio analysis inputs",
    ("PLN-07", "A03"): "Monthly trend inputs",
    ("REV-02", "S01"): "Invoices above performance materiality within the closing window",
}


STATES = {
    key: words(zh, en)
    for key, zh, en in (
        ("pending", "待复核", "Pending review"),
        ("confirmed", "已确认", "Confirmed"),
        ("overridden", "已推翻", "Overridden"),
        ("superseded", "已取代", "Superseded"),
        ("decided", "已决定", "Decided"),
        ("consistent", "一致", "Consistent"),
        ("inconsistent", "不一致", "Inconsistent"),
        ("not_applicable", "未完成复核", "Review not completed"),
        ("skipped", "未触发独立复核", "Independent review not triggered"),
        ("confirm", "确认", "Confirm"),
        ("override", "推翻并另出结论", "Override"),
        ("misstatement", "认定错报", "Recognise misstatement"),
        ("rerun", "重跑", "Rerun"),
        ("manual_result", "人工结果", "Manual result"),
        ("dispute_resolution", "争议裁决", "Resolve dispute"),
    )
}
