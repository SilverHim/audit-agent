# ruff: noqa: E501  (示例数据与底稿文字多为长字符串)
"""D-2 收入合同条款评价的示例数据: 演示项目 ENG-2026-001 10-20 的三条判断, 按中文工作语言人工译出, 合同原文保留英文."""

KIND = {"fact": "事实", "standard": "准则", "limitation": "局限", "inference": "推断"}

CONTRACTS = [
    {
        "id": "CON-X01",
        "title": "开发服务 (模块交付)",
        "info": [
            ("客户", "DIISR - Small Business Services"),
            ("签订日期", "2026-09-01"),
            ("合同金额", "AUD 6,500.00 (不含 GST)"),
            ("原件", "CON-X01.pdf (文件 DOC-7795e9312e9f, 3 页)"),
            ("对应发票", "ORC1026, AUD 7,150.00 (含 GST), 2026-09-17 入账"),
        ],
        "terms": [
            (
                "T1",
                "付款",
                2,
                "5.1 The Supplier may issue a tax invoice on or after the Completion Date. 5.2 The Customer must pay each tax invoice by its due date, which is no later than 30 days after the date of the invoice.",
            ),
            (
                "T2",
                "控制权转移",
                2,
                "4.1 The Services are complete when the Supplier has deployed the Module to the Customer's environment and the Customer's Project Sponsor has signed the Completion Certificate (the Completion Date). Title to and control of the Module pass to the Customer on the Completion Date.",
            ),
            (
                "T3",
                "终止",
                3,
                "7.1 Either party may terminate this Statement of Work by giving 10 Business Days' written notice to the other party. 7.2 If this Statement of Work is terminated before the Completion Date, the Supplier is not entitled to payment for the Module.",
            ),
        ],
        "conclusion": "控制权在完工日这一时点转移: 供应方把模块部署到客户环境, 并由客户的项目负责人签署完工证书 (T2)。"
        "T1 规定只能在完工日当天或之后开票, 并须在 30 天内付款; T3 允许提前 10 个工作日通知终止, 完工前终止的不付款。"
        "所提供的条款未涉及退货, 返利, 保证, 开票持有, 寄售或关联方条款。",
        "steps": [
            (
                "fact",
                "T2 把完工定义为模块部署到客户环境并由项目负责人签署完工证书, 并规定所有权与控制权在完工日转移。",
                "CON-X01-T2, 第 2 页",
            ),
            ("fact", "T1 允许在完工日当天或之后开具税务发票, 付款期限不晚于开票后 30 天。", "CON-X01-T1, 第 2 页"),
            (
                "fact",
                "T3 允许任一方提前 10 个工作日书面通知终止; 完工日前终止的, 供应方无权就模块收款。",
                "CON-X01-T3, 第 3 页",
            ),
            (
                "standard",
                "履约义务通过向客户转移控制权而履行时确认收入; 不满足在某一时段内履行的条件时, 按迹象判断某一时点的控制权转移。",
                "AASB 15 第 31, 38 段",
            ),
            (
                "standard",
                "在某一时段内确认收入须至少满足一项条件: 客户同时取得并消耗利益; 客户控制在建资产; 或资产无替代用途且就迄今已完成的履约有可执行的收款权。",
                "AASB 15 第 35 段",
            ),
            (
                "limitation",
                "所提供的条款没有说明部署与签证之前的履约是否满足第 35 段的任一条件, 也未涉及退货, 返利, 保证, 开票持有, 寄售或关联方条款。",
                "",
            ),
            (
                "inference",
                "合同在 T2 中明确把转移事件定在完工日, 没有与之矛盾的条款; 条款支持部署并经项目负责人签字时一次性转移, 不能证明在某一时段内转移。T1, T3 是付款与终止条款, 没有规定不同的转移事件。",
                "由第 1 至 6 步推出",
            ),
        ],
        "counter": '如有已签署的补充协议或其他条款规定了不同的控制权转移事件, 或证明本项履约满足第 35 段的某项条件, 可能推翻 "某一时点" 的结论。',
        "confidence": ("0.84", "证据 0.80, 模型 0.91"),
        "task": "T-J-REV-03-1-CON-X01-v1",
        "link": "ORC1026 的收款与发票差额见 D-3 例外 a, 已转 D-4 收入截止性判断。",
    },
    {
        "id": "CON-X02",
        "title": "支持服务 (按月)",
        "info": [
            ("客户", "Hamilton Smith Pty (ABN 19 900 043 058)"),
            ("签订日期", "2026-06-24"),
            ("合同金额", "按月收费, 合同未列总额"),
            ("原件", "CON-X02.pdf (文件 DOC-02c5c90d4ffb, 3 页)"),
            ("对应发票", "ORC1003, ORC1008 (均 2026-07-17), ORC1016, ORC1020, 每张 AUD 550.00 (含 GST)"),
        ],
        "terms": [
            (
                "T1",
                "付款",
                2,
                "5.1 The Supplier will issue a tax invoice for the Monthly Fee of each calendar month on or about the 17th day of that month. 5.2 The Customer must pay each tax invoice within 14 days after the date of the invoice.",
            ),
            (
                "T2",
                "控制权转移",
                2,
                "4.1 The Support Services are provided throughout each calendar month, and the Customer simultaneously receives and consumes the benefit of the Support Services as the Supplier provides them.",
            ),
            (
                "T3",
                "返利 / 抵减",
                3,
                "6.1 If the Supplier does not respond to a Priority 1 request within 4 business hours, the Customer may claim a credit of 10% of the Monthly Fee for that month.",
            ),
            (
                "T4",
                "终止",
                3,
                "7.1 Either party may terminate this Agreement by giving 30 days' written notice to the other party.",
            ),
        ],
        "conclusion": "支持服务在每个自然月内持续转移, 因为客户在供应方提供服务的同时取得并消耗其利益 (T2), 属于在某一时段内履行。"
        "其他相关条款为按月开票与付款时间 (T1), 有条件的 10% 月费抵减 (T3), 以及任一方提前 30 天通知终止的权利 (T4)。",
        "steps": [
            (
                "fact",
                "T2 规定支持服务在每个自然月内持续提供, 客户在供应方提供服务的同时取得并消耗其利益。",
                "CON-X02-T2, 第 2 页",
            ),
            ("standard", "客户在主体履约的同时取得并消耗利益的, 履约义务在某一时段内履行。", "AASB 15 第 35(a) 段"),
            ("fact", "T1 规定每月约 17 日就当月月费开票, 开票后 14 天内付款。", "CON-X02-T1, 第 2 页"),
            (
                "fact",
                "T3 规定供应方未在 4 个工作小时内响应一级请求的, 客户可要求抵减当月月费的 10%。",
                "CON-X02-T3, 第 3 页",
            ),
            ("fact", "T4 允许任一方提前 30 天书面通知终止协议。", "CON-X02-T4, 第 3 页"),
            (
                "inference",
                "T2 的服务转移方式符合第 35(a) 段的条件, 支持服务收入应在每个自然月提供服务的过程中确认, 而不是仅在开票时确认。T3 是有条件的抵减条款, T1 规定开票与付款时间, T4 规定终止权利。",
                "由第 1 至 5 步推出",
            ),
        ],
        "counter": '如有补充协议或其他适用条款表明客户并非在服务提供时同时取得并消耗利益, 或要求服务验收后控制权才转移, 可能推翻 "在某一时段内履行" 的结论。',
        "confidence": ("0.87", "证据 0.80, 模型 0.98"),
        "task": "T-J-REV-03-1-CON-X02-v1",
        "link": "同一天开出的 ORC1003 与 ORC1008 金额相同, 是否重复开票由 G-2 分录例外归因判断。",
    },
    {
        "id": "CON-X03",
        "title": "驻场咨询 (按日)",
        "info": [
            ("客户", "Maddox Publishing Group (ABN 21 900 043 067)"),
            ("签订日期", "2026-08-10"),
            ("合同金额", "AUD 4,200.00 (不含 GST)"),
            ("原件", "CON-X03.pdf (文件 DOC-c90b3e398274, 3 页)"),
            ("对应发票", "ORC1014, AUD 4,620.00 (含 GST), 2026-08-26 入账"),
        ],
        "terms": [
            (
                "T1",
                "控制权转移",
                2,
                "4.1 Each On-site Day is accepted when the Customer signs the timesheet for that day.",
            ),
            (
                "T2",
                "付款",
                2,
                "5.1 The Supplier will issue a tax invoice after the sixth On-site Day. The Customer must pay the invoice within 10 days after the date of the invoice.",
            ),
            (
                "T3",
                "返利 / 抵减",
                3,
                "6.1 If the project manager is absent for part of an On-site Day, the Supplier will credit $350.00 for each half day of absence.",
            ),
            (
                "T4",
                "终止",
                3,
                "7.1 Either party may terminate this Agreement by giving 5 Business Days' written notice to the other party.",
            ),
        ],
        "conclusion": "每个驻场日的服务控制权在客户签署当天工时单时转移 (T1), 合同约定的转移时点即这一验收事件。"
        "开票时间 (T2), 缺勤抵减 (T3) 与终止权 (T4) 也相关, 其中 T3 可能影响交易价格。"
        "所提供的条款未涉及退货权, 保证, 开票持有, 寄售或关联方条款, 无法据此评估。",
        "steps": [
            ("fact", "T1 规定每个驻场日在客户签署当天工时单时验收。", "CON-X03-T1, 第 2 页"),
            ("standard", "履约义务通过向客户转移所承诺服务的控制权而履行时确认收入。", "AASB 15 第 31 段"),
            ("standard", "不在某一时段内履行的, 在某一时点履行; 客户验收是控制权转移的迹象之一。", "AASB 15 第 38 段"),
            ("standard", "在某一时段内确认收入须至少满足第 35 段的一项条件。", "AASB 15 第 35 段"),
            ("fact", "T2 规定第六个驻场日之后开票, 开票后 10 天内付款。", "CON-X03-T2, 第 2 页"),
            ("fact", "T3 规定项目经理每缺勤半天, 抵减 AUD 350.00。", "CON-X03-T3, 第 3 页"),
            ("fact", "T4 允许任一方提前 5 个工作日书面通知终止。", "CON-X03-T4, 第 3 页"),
            (
                "limitation",
                "所提供的条款不能确定是否满足第 35 段的某项条件, 也未涉及退货, 保证, 开票持有, 寄售或关联方安排; 但约定的每日验收事件已确定了每个驻场日的转移时点。",
                "",
            ),
            (
                "inference",
                "把控制权与验收的要求适用于 T1, 每个驻场日的转移事件是客户签署当天的工时单。T2 规定开票与付款时间, T3 的缺勤抵减可能影响对价, T4 规定终止权。",
                "由第 1 至 8 步推出",
            ),
        ],
        "counter": "如有已签署的补充协议或其他约束性条款规定控制权在其他事件转移, 而非客户签署每日工时单时, 可能推翻所认定的转移时点。",
        "confidence": ("0.83", "证据 0.80, 模型 0.88"),
        "task": "T-J-REV-03-1-CON-X03-v1",
        "link": "合同金额 4,200.00 加 GST 等于发票 ORC1014 的 4,620.00。",
    },
]
