# ruff: noqa: E501  (示例数据与底稿文字多为长字符串)
"""示例工作底稿 (全部 Excel, 一个程序一份, 每份一个文件): 用演示项目 ENG-2026-001 (Xero 演示公司) 10-20 的实际数据.

数据于 2026-10-21 从本地运行的 personal/main 37f7eeb (库 audit_cloud) 取出后写在本文件与 contracts_data.py 里, 不连库.
运行 (仓库根目录): uv run python "docs/云端开发/design/底稿示例/build_sample.py", 输出到同目录的 "ENG-2026-001 审计档案 (示例)/".
"""

from pathlib import Path

from contracts_data import CONTRACTS, KIND
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT_DIR = Path(__file__).with_name("ENG-2026-001 审计档案 (示例)")

CLIENT = "Demo Company (AU) Pty Ltd"
PERIOD = "2026-07-01 至 2026-09-30"
ENGAGEMENT = "ENG-2026-001"
AUDITOR = "Jordan Ellis"

MONEY = '#,##0.00;(#,##0.00);"-"'
THIN = Side(style="thin", color="A6A6A6")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TITLE = Font(bold=True, size=14)
BOLD = Font(bold=True)
GREY = Font(color="7F7F7F")
RED = Font(color="C00000")
GREEN = Font(color="2E7D32", bold=True)
SECTION = PatternFill("solid", fgColor="E7EEF7")
HEAD = PatternFill("solid", fgColor="F2F2F2")
PENDING = PatternFill("solid", fgColor="FFF2CC")
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="top", wrap_text=True)


def lines(text, width):
    """估计一段文字在给定列宽里折成几行: 中文按 2 个字符宽计."""
    if not isinstance(text, str) or width <= 0:
        return 1
    weight = sum(2 if ord(ch) > 0x2E80 else 1.1 for ch in text)
    return max(1, -(-int(weight) // int(width)))


def fit(ws, row, height_lines):
    current = ws.row_dimensions[row].height or 15
    ws.row_dimensions[row].height = max(current, 15 * height_lines)


def widths(ws, values):
    for i, w in enumerate(values, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def put(ws, row, col, value, *, font=None, fill=None, fmt=None, align=WRAP, border=True):
    cell = ws.cell(row=row, column=col, value=value)
    cell.alignment = align
    if font:
        cell.font = font
    if fill:
        cell.fill = fill
    if fmt:
        cell.number_format = fmt
    if border:
        cell.border = BOX
    return cell


def span(ws, row, first, last, value, **kw):
    """合并一行里 first..last 列并写值, 边框画到每个格."""
    for c in range(first, last + 1):
        put(ws, row, c, None, fill=kw.get("fill"), border=kw.get("border", True))
    ws.merge_cells(start_row=row, start_column=first, end_row=row, end_column=last)
    total = sum(ws.column_dimensions[get_column_letter(c)].width or 8 for c in range(first, last + 1))
    fit(ws, row, lines(value, total))
    return put(ws, row, first, value, **kw)


def header(ws, ncols, index, title, procedure, preparer, prepared, *, reviewed="待签署", scope="—"):
    """底稿表头: ASA 230 第 9 段要求的编制人与完成日期, 复核人, 复核日期与复核范围."""
    span(ws, 1, 1, ncols, f"{index}  {title}", font=TITLE, border=False)
    # 左右两半按列宽对分, 首列窄的页也不挤
    w = [ws.column_dimensions[get_column_letter(c)].width or 8 for c in range(1, ncols + 1)]
    acc, half = 0, 1
    for i, x in enumerate(w, start=1):
        acc += x
        if acc >= sum(w) / 2:
            half = min(i, ncols - 1)
            break
    span(ws, 2, 1, half, f"客户: {CLIENT}")
    span(ws, 2, half + 1, ncols, f"审计期间: {PERIOD}    项目: {ENGAGEMENT}")
    span(ws, 3, 1, half, f"对应程序: {procedure}")
    span(ws, 3, half + 1, ncols, f"索引: {index}")
    span(ws, 4, 1, half, f"编制人: {preparer}    完成日期: {prepared}")
    fill = PENDING if reviewed == "待签署" else None
    span(ws, 4, half + 1, ncols, f"复核人: {AUDITOR}    复核日期: {reviewed}    复核范围: {scope}", fill=fill)
    return 6


def section(ws, row, ncols, text):
    span(ws, row, 1, ncols, text, font=BOLD, fill=SECTION)
    return row + 1


def kv(ws, row, ncols, key, value, *, font=None, key_to=1):
    """一行 "键: 值"; key_to 为键占到的列 (首列太窄时合并几列放键)."""
    if key_to > 1:
        span(ws, row, 1, key_to, key, font=GREY)
    else:
        put(ws, row, 1, key, font=GREY)
    span(ws, row, key_to + 1, ncols, value, font=font)
    return row + 1


def table(ws, row, heads, rows, *, money_cols=(), center_cols=(), fonts=None):
    for c, h in enumerate(heads, start=1):
        put(ws, row, c, h, font=BOLD, fill=HEAD, align=CENTER)
    fit(
        ws,
        row,
        max(lines(h, ws.column_dimensions[get_column_letter(c)].width or 8) for c, h in enumerate(heads, start=1)),
    )
    row += 1
    for r, values in enumerate(rows):
        need = max(
            lines(v, ws.column_dimensions[get_column_letter(c)].width or 8) for c, v in enumerate(values, start=1)
        )
        fit(ws, row, need)
        for c, v in enumerate(values, start=1):
            font = (fonts or {}).get((r, c))
            if c in money_cols:
                put(ws, row, c, v, fmt=MONEY, font=font, align=Alignment(horizontal="right", vertical="top"))
            elif c in center_cols:
                put(ws, row, c, v, font=font, align=CENTER)
            else:
                put(ws, row, c, v, font=font)
        row += 1
    return row


# ── 数据 (10-20 演示项目实际数据) ──

MATERIALITY = {
    "base": 32431.00,
    "pct": 0.01,
    "overall": 324.31,
    "pm_pct": 0.75,
    "pm": 243.23,
    "ct_pct": 0.05,
    "ct": 16.22,
}

# REV-08 截止窗口 (期末前 20 天) 内入账的发票, 及按规则口径找到的服务完成记录与收款
INVOICES = [
    # 发票号, 客户, 合同, 入账日期, 金额, 分录, 完成记录, 签收日, 签收人, 收款流水, 收款日, 收款额
    (
        "ORC1024",
        "Carruthers & Smale",
        "",
        "2026-09-16",
        550.00,
        "XJ-561",
        "SR-ORC1024",
        "2026-08-19",
        "Ruth Carmody",
        "",
        "",
        None,
    ),
    (
        "ORC1020",
        "Hamilton Smith Pty",
        "CON-X02",
        "2026-09-17",
        550.00,
        "XJ-579",
        "SR-ORC1020",
        "2026-09-30",
        "Tom Reilly",
        "BT-090-0122",
        "2026-10-14",
        550.00,
    ),
    (
        "ORC1021",
        "Yarra Transport",
        "",
        "2026-09-17",
        550.00,
        "XJ-584",
        "SR-ORC1021",
        "2026-09-30",
        "Ava Chen",
        "BT-090-0081",
        "2026-10-06",
        550.00,
    ),
    (
        "ORC1022",
        "Port Phillip Freight",
        "",
        "2026-09-17",
        550.00,
        "XJ-586",
        "SR-ORC1022",
        "2026-09-30",
        "Daniel Kowalski",
        "BT-090-0084",
        "2026-10-06",
        550.00,
    ),
    (
        "ORC1023",
        "Rex Media Group",
        "",
        "2026-09-17",
        550.00,
        "XJ-581",
        "SR-ORC1023",
        "2026-09-30",
        "Isla Brennan",
        "BT-090-0080",
        "2026-10-06",
        550.00,
    ),
    (
        "ORC1026",
        "DIISR - Small Business Services",
        "CON-X01",
        "2026-09-17",
        7150.00,
        "XJ-580",
        "SR-ORC1026",
        "2026-09-17",
        "Priya Natarajan",
        "BT-090-0124",
        "2026-10-14",
        3850.00,
    ),
]


# 一份底稿一个文件: 索引号 → (文件名, 工作表名, 生成函数); 索引单独一个文件
FILES = {
    "A-2": "A-2 重要性 (PLN-01).xlsx",
    "D": "D 收入导引表.xlsx",
    "D-2": "D-2 收入合同条款评价 (REV-03).xlsx",
    "D-3": "D-3 凭证三单匹配 (REV-08).xlsx",
    "X": "X 错报汇总.xlsx",
    "S": "S 准则对照.xlsx",
}


def save(builder, file_name, title):
    wb = Workbook()
    ws = wb.active
    ws.title = title
    builder(ws)
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0  # 宽度缩到一页, 高度按内容分页
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.oddFooter.left.text = f"{CLIENT} · {ENGAGEMENT} · {title}"
    ws.oddFooter.right.text = "第 &P 页, 共 &N 页"
    wb.save(OUT_DIR / file_name)


def build():
    OUT_DIR.mkdir(exist_ok=True)
    for old in OUT_DIR.glob("*.xlsx"):
        old.unlink()
    save(index_sheet, "00 索引.xlsx", "索引")
    save(materiality_sheet, FILES["A-2"], "A-2")
    save(lead_sheet, FILES["D"], "D")
    save(d2_sheet, FILES["D-2"], "D-2")
    save(rev08_sheet, FILES["D-3"], "D-3")
    save(misstatement_sheet, FILES["X"], "X")
    save(standards_sheet, FILES["S"], "S")
    print(OUT_DIR)


def index_sheet(ws):
    widths(ws, [13, 30, 26, 22, 12, 14, 12, 14])
    n = 8
    span(ws, 1, 1, n, "审计档案索引", font=TITLE, border=False)
    row = 3
    for k, v in [
        ("被审计单位", f"{CLIENT} (ABN 25 900 043 021)"),
        ("审计期间", f"{PERIOD} (3 个月)"),
        ("项目编号", ENGAGEMENT),
        ("项目负责人", f"{AUDITOR}, 注册公司审计师"),
        ("工作语言", "中文 (引用的原始单据保留原文)"),
        ("数据来源", "Xero 同步与审计师导入, 数据版本 9, 数据截至 2026-10-17"),
        ("大模型", "gpt-6-luna (真实调用)"),
    ]:
        row = kv(ws, row, n, k, v)
    row += 1
    row = section(ws, row, n, "底稿索引 (一个程序一份底稿, 每份一个文件; 本示例生成的可点击打开)")
    heads = ["索引", "底稿", "对应程序", "编制人", "完成日期", "复核人", "复核日期", "状态"]
    done, wait = "2026-10-20", "待签署"
    rows = [
        ("A-1", "总体审计计划", "PLN-15", "A4 计划智能体", "—", AUDITOR, "—", "功能未开启"),
        ("A-2", "重要性水平", "PLN-01", "A4 计划智能体; 审计师决定", done, AUDITOR, wait, "草稿"),
        ("A-3", "风险清单终审", "PLN-03", "A4 计划智能体; 审计师决定", done, AUDITOR, wait, "本示例未生成"),
        ("A-4", "初步分析程序", "PLN-07", "A4 计划智能体", done, AUDITOR, wait, "本示例未生成"),
        ("B-1", "是否承接", "ACC-01", "审计师决定", done, AUDITOR, wait, "本示例未生成"),
        ("B-2", "ASIC / ABN 核查", "ACC-04", "A2 承接智能体", done, AUDITOR, wait, "本示例未生成"),
        ("C-1", "函证发起", "CSH-04", "A5 函证智能体", done, AUDITOR, wait, "本示例未生成"),
        ("C-2", "函证回函抽取", "DOC-06", "A3 数据管理智能体", done, AUDITOR, wait, "本示例未生成"),
        ("C-3", "流水账面核对", "CSH-05", "A6 实质性测试智能体", done, AUDITOR, wait, "本示例未生成"),
        ("C-4", "函证差异评价", "CSH-02", "A6 实质性测试智能体", done, AUDITOR, wait, "本示例未生成"),
        ("D", "收入导引表", "—", "系统按试算表生成", "2026-10-21", AUDITOR, wait, "草稿"),
        ("D-1", "合同要素抽取", "DOC-04", "A3 数据管理智能体", done, AUDITOR, wait, "本示例未生成"),
        ("D-2", "收入合同条款评价", "REV-03", "A6 实质性测试智能体", done, AUDITOR, wait, "待复核 3 项"),
        ("D-3", "凭证三单匹配", "REV-08", "A6 实质性测试智能体", done, AUDITOR, wait, "草稿"),
        ("D-4", "收入截止性判断", "REV-02", "A6 实质性测试智能体", "—", AUDITOR, "—", "尚未执行"),
        ("G-1", "全量分录扫描", "GEN-05", "A6 实质性测试智能体", done, AUDITOR, wait, "本示例未生成"),
        ("G-2", "分录例外归因", "GEN-02", "A6 实质性测试智能体", done, AUDITOR, wait, "待复核 7 项"),
        ("X", "错报汇总", "FIN-04", "系统", "2026-10-21", AUDITOR, wait, "草稿"),
        ("Z-1", "未更正错报评价", "FIN-01", "审计师决定", "—", AUDITOR, "—", "尚未执行"),
        ("Z-2", "审计报告文本", "RPT-03", "A10 审计助理", "—", AUDITOR, "—", "尚未执行"),
        ("Z-3", "审计报告批准与签字", "RPT-01", "审计师决定", "—", AUDITOR, "—", "尚未执行"),
        ("S", "准则文档要求对照", "—", "系统", "2026-10-21", AUDITOR, wait, "草稿"),
    ]
    start = row + 1
    row = table(ws, row, heads, rows)
    for r in range(start, row):
        ref = ws.cell(row=r, column=1).value
        if ref in FILES:
            cell = ws.cell(row=r, column=1)
            cell.hyperlink = FILES[ref]
            cell.font = Font(color="1F4E9D", underline="single")
        if ws.cell(row=r, column=8).value in ("本示例未生成", "功能未开启", "尚未执行"):
            for c in range(2, 9):
                ws.cell(row=r, column=c).font = GREY
    row += 1
    row = section(ws, row, n, "标记与约定")
    for k, v in [
        ("✓", "已核对, 无误"),
        ("✗", '有例外, 见本页 "例外与处理"'),
        ("—", "不适用, 或本程序不作判断"),
        ("金额", "澳元; 发票与收款含 GST; 贷方余额以括号表示"),
        ("编制人", "执行程序的智能体或系统; 审计师作出的决定另注"),
        ("复核", "审计师逐份签署复核, 记录复核日期与范围 (全部明细 / 抽查 / 只看结论)"),
    ]:
        row = kv(ws, row, n, k, v)


def materiality_sheet(ws):
    n = 4
    widths(ws, [24, 16, 40, 40])
    m = MATERIALITY
    row = header(
        ws,
        n,
        "A-2",
        "重要性",
        "PLN-01 重要性水平 (审计师在风险与重要性决定中选定)",
        "A4 计划智能体 (候选); Jordan Ellis (决定)",
        "2026-10-20",
    )
    row = section(ws, row, n, "依据")
    row = kv(
        ws,
        row,
        n,
        "准则",
        "ASA 320 第 10, 11, 14 段 (重要性与执行重要性的金额及考虑因素); ASA 450 第 15(a) 段 (明显微小临界值)",
    )
    row += 1
    row = section(ws, row, n, "金额与计算")
    rows = [
        ("基准: 收入", m["base"], "科目 200 Sales, 本期试算表", "本期为 3 个月"),
        ("比例", f"{m['pct']:.0%}", "", "候选范围 0.5% - 1%"),
        ("整体重要性", m["overall"], "32,431.00 × 1%", ""),
        ("执行重要性", m["pm"], "324.31 × 75%", "候选范围 50% - 75%"),
        ("明显微小临界值", m["ct"], "324.31 × 5%", "低于此金额的错报不累计"),
        ("特定类别的重要性", "不适用", "", ""),
    ]
    row = table(ws, row, ["项目", "金额 / 比例", "计算", "说明"], rows, money_cols=(2,))
    row += 1
    row = section(ws, row, n, "考虑因素 (ASA 320 第 14 段要求记录)")
    row = kv(ws, row, n, "候选基准", "收入 0.5% - 1%; 总资产 1% - 2%; 税前利润 5% (系统按试算表计算的候选)")
    row = kv(
        ws,
        row,
        n,
        "选择理由",
        "缺: 决定时未填写理由。按 ASA 320 第 14 段应记录选择收入为基准, 以及取 1% 与 75% 的考虑因素。",
        font=RED,
    )
    row = kv(ws, row, n, "决定", "Jordan Ellis 于 2026-10-20 在风险与重要性决定中批准 (决定编号 D-466f8eae)")
    row = kv(ws, row, n, "修订", "无 (ASA 320 第 12, 13 段的修订在完成阶段复核)")
    row += 1
    section(ws, row, n, "复核意见:")


def lead_sheet(ws):
    n = 10
    widths(ws, [10, 18, 12, 15, 13, 15, 15, 15, 15, 16])
    row = header(ws, n, "D", "收入导引表", "系统按试算表与错报汇总生成", "系统", "2026-10-21")
    row = kv(
        ws,
        row,
        n,
        "说明",
        "本期为 3 个月 (至 2026-09-30); 上年数为截至 2026-06-30 的全年数, 期间长度不同, 变动不宜直接比较。",
    )
    row += 1
    heads = [
        "科目代码",
        "科目名称",
        "标准分类",
        "本期未审数",
        "已更正调整",
        "本期审定数",
        "上年数",
        "变动",
        "未更正错报 (见 X)",
        "底稿索引",
    ]
    rows = [
        ("200", "Sales", "收入", -32431.00, 0, -32431.00, -4200.00, -28231.00, 0, "D-1 至 D-4"),
        ("260", "Other Revenue", "其他收入", 0, 0, 0, 0, 0, 0, "—"),
        ("270", "Interest Income", "其他收入", 0, 0, 0, 0, 0, -48.53, "C-4, X-1"),
    ]
    start = row + 1
    row = table(ws, row, heads, rows, money_cols=(4, 5, 6, 7, 8, 9))
    for c in range(1, n + 1):
        put(ws, row, c, None, fill=HEAD)
    put(ws, row, 1, "合计", font=BOLD, fill=HEAD)
    for c in (4, 5, 6, 7, 8, 9):
        col = get_column_letter(c)
        put(
            ws,
            row,
            c,
            f"=SUM({col}{start}:{col}{row - 1})",
            fmt=MONEY,
            font=BOLD,
            fill=HEAD,
            align=Alignment(horizontal="right"),
        )
    row += 2
    row = kv(ws, row, n, "勾稽", "✓ 本期未审数合计与试算表收入类科目合计一致 (32,431.00 贷方)", font=GREEN)
    row = kv(ws, row, n, "错报", "科目 270 利息收入低估 48.53, 未更正, 见 X-1; 审定数未调整。")
    row += 1
    section(ws, row, n, "复核意见:")


def d2_sheet(ws):
    """判断类底稿也用 Excel: 每份合同列关键条款原文, 结论, 逐步推理与出处, 反方论证, 置信度与复核任务."""
    n = 4
    widths(ws, [6, 16, 72, 26])

    def kv2(ws, row, ncols, key, value, **kw):
        return kv(ws, row, ncols, key, value, key_to=2, **kw)

    row = header(
        ws,
        n,
        "D-2",
        "收入合同条款评价",
        "REV-03 收入合同条款评价 (AI 判断, 模型 gpt-6-luna)",
        "A6 实质性测试智能体",
        "2026-10-20",
    )
    row = section(ws, row, n, "1. 目标与风险")
    row = kv2(ws, row, n, "认定", "收入发生, 收入截止 (审计目标 OBJ-REV-OCC, OBJ-REV-CUT)")
    row = kv2(ws, row, n, "应对的风险", "RISK-REV-CUT, RISK-REV-OCC, 均为特别风险 (舞弊相关), 见 A-3")
    row = kv2(
        ws,
        row,
        n,
        "依据",
        "AASB 15 第 31 段 (转移控制权时履行履约义务), 第 35 段 (在某一时段内履行的条件), 第 38 段 (某一时点的控制权转移迹象); "
        "ASA 330 第 21 段 (特别风险须执行专门针对的实质性程序)",
    )
    row += 1
    row = section(ws, row, n, "2. 程序 (性质, 时间, 范围)")
    row = kv2(
        ws,
        row,
        n,
        "性质",
        "AI 判断。A3 先从合同原件抽取付款, 控制权转移, 返利与抵减, 终止等条款 (见 D-1); A6 由大模型逐份对照准则, "
        "判断履约义务的履行方式与控制权转移时点, 给出推理过程, 证据出处与反方论证; 系统按证据完整度与模型自评计算置信度。",
    )
    row = kv2(ws, row, n, "时间", "2026-10-20 执行, 用时约 30 秒; 数据版本 9")
    row = kv2(ws, row, n, "范围", "项目内全部 3 份收入合同 (CON-X01 至 CON-X03), 全量")
    row = kv2(ws, row, n, "独立复核", "三份均未命中独立复核 (A7) 的触发规则, 未经复核模型复核")
    row += 1
    for i, c in enumerate(CONTRACTS, start=1):
        row = section(ws, row, n, f"3.{i}  {c['id']}  {c['title']}")
        for k, v in c["info"]:
            row = kv2(ws, row, n, k, v)
        row = table(
            ws,
            row,
            ["序", "条款与类型", "合同原文", "页"],
            [
                (j, f"{c['id']}-{k} {kind}", text, f"第 {page} 页")
                for j, (k, kind, page, text) in enumerate(c["terms"], start=1)
            ],
            center_cols=(1,),
        )
        row = kv2(ws, row, n, "结论 (智能体草拟)", c["conclusion"])
        row = table(
            ws,
            row,
            ["步", "类别", "推理内容", "出处"],
            [(j, KIND[k], text, ev or "—") for j, (k, text, ev) in enumerate(c["steps"], start=1)],
            center_cols=(1,),
        )
        row = kv2(ws, row, n, "反方论证", c["counter"])
        row = kv2(ws, row, n, "置信度", f"{c['confidence'][0]} ({c['confidence'][1]})")
        row = kv2(ws, row, n, "建议", "接受: 收入按上述转移时点确认")
        row = kv2(ws, row, n, "复核任务", f"{c['task']}, 待审计师决定", font=RED)
        row = kv2(ws, row, n, "关联", c["link"])
        row += 1
    row = section(ws, row, n, "4. 结论汇总")
    row = table(
        ws,
        row,
        ["序", "合同 · 履约方式", "控制权转移时点", "建议 · 置信度 · 复核"],
        [
            (1, "CON-X01 · 某一时点", "完工日: 部署并经项目负责人签署完工证书", "接受 · 0.84 · 待复核"),
            (2, "CON-X02 · 某一时段内", "每个自然月提供服务的过程中", "接受 · 0.87 · 待复核"),
            (3, "CON-X03 · 某一时点 (按日)", "客户签署当天工时单时", "接受 · 0.83 · 待复核"),
        ],
        center_cols=(1,),
    )
    row = kv2(
        ws,
        row,
        n,
        "结论 (智能体草拟)",
        "三份收入合同的控制权转移时点均由合同条款明确约定, 未发现与已入账收入的确认方式相矛盾的条款; "
        "CON-X02, CON-X03 的抵减条款可能影响交易价格, 本期是否发生抵减不在本程序范围内。最终结论待审计师复核三项判断后确定。",
    )
    row += 1
    row = section(ws, row, n, "5. 交叉索引与证据")
    row = kv2(ws, row, n, "交叉索引", "D-1 合同要素抽取; D-3 凭证三单匹配; D-4 收入截止性判断; G-2 分录例外归因")
    row = kv2(
        ws,
        row,
        n,
        "证据",
        "合同原件 CON-X01.pdf, CON-X02.pdf, CON-X03.pdf (审计师上传, 外部副本); AASB 15 原文片段 AASB15-P31, P35, P38",
    )
    row = kv2(
        ws,
        row,
        n,
        "说明",
        '系统目前的判断文字为英文, 本示例按 "工作语言: 中文" 由人工译出; 合同原文保留英文。',
        font=GREY,
    )
    row += 1
    section(ws, row, n, "复核意见:")


def rev08_sheet(ws):
    n = 17

    def kv3(ws, row, ncols, key, value, **kw):
        return kv(ws, row, ncols, key, value, key_to=3, **kw)

    widths(ws, [5, 10, 22, 9, 11, 10, 8, 12, 11, 14, 12, 11, 10, 9, 9, 9, 22])
    row = header(ws, n, "D-3", "凭证三单匹配", "REV-08 凭证三单匹配 (规则核对)", "A6 实质性测试智能体", "2026-10-20")
    row = section(ws, row, n, "目标与风险")
    row = kv3(ws, row, n, "认定", "收入发生 (确有其事), 收入截止 (记入正确期间); 审计目标 OBJ-REV-OCC, OBJ-REV-CUT")
    row = kv3(
        ws,
        row,
        n,
        "应对的风险",
        "RISK-REV-CUT 特别风险 (舞弊相关): 2026-09-16 至 17 日集中开出 6 张发票, 合计 AUD 9,000 (不含 GST), "
        "其中给 DIISR 的开发费发票 ORC1026 服务完成日接近期末。RISK-REV-OCC 特别风险: 推定的收入舞弊风险 (ASA 240)。",
    )
    row = kv3(
        ws,
        row,
        n,
        "准则",
        "ASA 330 第 21 段 (特别风险须执行专门针对的实质性程序); ASA 330 第 28 段与 ASA 230 第 9 段 (记录程序, 被测项目与结果)",
    )
    row += 1
    row = section(ws, row, n, "程序 (性质, 时间, 范围)")
    row = kv3(ws, row, n, "性质", "规则核对: 系统逐张执行下列三项检查; 有例外的发票转 D-4 收入截止性判断处理。")
    row = kv3(ws, row, n, "时间", "2026-10-20 执行; 数据截至 2026-10-17 (Xero 同步), 数据版本 9")
    row = kv3(
        ws,
        row,
        n,
        "范围",
        "期末前 20 天 (2026-09-10 至 2026-09-30) 入账的全部销售发票, 共 6 张, 合计 AUD 9,900.00 (含 GST); 全量, 不抽样。"
        "窗口天数按风险等级取值: 低 5 天, 中 10 天, 高 15 天, 特别 20 天。",
    )
    row += 1
    row = section(ws, row, n, "检查项")
    row = kv3(ws, row, n, "入账期间正确", "服务完成记录的签收日不晚于期末 2026-09-30 (规则 R01)")
    row = kv3(ws, row, n, "有服务完成记录", "发票对应的服务完成记录存在, 且有签收日 (规则 R02)")
    row = kv3(
        ws,
        row,
        n,
        "收款金额相符",
        "按发票号在银行流水参考号中找到的收款合计等于发票金额; 尚未收款的发票本项不作判断 (规则 R03)",
    )
    row += 1
    row = section(ws, row, n, "测试明细 (被测项目的识别特征, ASA 230 第 9(a) 段)")
    heads = [
        "序号",
        "发票号",
        "客户",
        "合同",
        "入账日期",
        "金额",
        "分录",
        "服务完成记录",
        "签收日",
        "签收人",
        "收款流水",
        "收款日",
        "收款金额",
        "入账期间正确",
        "有服务完成记录",
        "收款金额相符",
        "备注",
    ]
    rows, fonts = [], {}
    for i, (no, cust, con, posted, amt, je, sr, signed, by, bt, paid, rec) in enumerate(INVOICES):
        cutoff = "✓" if signed <= "2026-09-30" else "✗"
        has_sr = "✓" if sr and signed else "✗"
        if rec is None:
            match, note = "—", "截至 2026-10-17 尚未收款, 列为关注事项"
        elif abs(rec - amt) < 0.005:
            match, note = "✓", ""
        else:
            match, note = "✗", "例外 a"
        rows.append(
            (
                i + 1,
                no,
                cust,
                con or "—",
                posted,
                amt,
                je,
                sr,
                signed,
                by,
                bt or "—",
                paid or "—",
                rec,
                cutoff,
                has_sr,
                match,
                note,
            )
        )
        for c, v in ((14, cutoff), (15, has_sr), (16, match)):
            fonts[(i, c)] = GREEN if v == "✓" else RED if v == "✗" else GREY
        if note:
            fonts[(i, 17)] = RED if note.startswith("例外") else GREY
    start = row + 1
    row = table(ws, row, heads, rows, money_cols=(6, 13), center_cols=(1, 14, 15, 16), fonts=fonts)
    for c in range(1, n + 1):
        put(ws, row, c, None, fill=HEAD)
    put(ws, row, 2, "合计 6 张", font=BOLD, fill=HEAD)
    put(ws, row, 6, f"=SUM(F{start}:F{row - 1})", fmt=MONEY, font=BOLD, fill=HEAD, align=Alignment(horizontal="right"))
    put(ws, row, 13, f"=SUM(M{start}:M{row - 1})", fmt=MONEY, font=BOLD, fill=HEAD, align=Alignment(horizontal="right"))
    row += 2
    row = section(ws, row, n, "例外与处理")
    put(ws, row, 1, "编号", font=BOLD, fill=HEAD)
    put(ws, row, 2, "系统编号", font=BOLD, fill=HEAD)
    span(ws, row, 3, 9, "说明", font=BOLD, fill=HEAD)
    span(ws, row, 10, 15, "处理", font=BOLD, fill=HEAD)
    span(ws, row, 16, 17, "状态", font=BOLD, fill=HEAD)
    row += 1
    put(ws, row, 1, "a", font=RED)
    put(ws, row, 2, "X-REV-08-1-R03-1")
    span(ws, row, 3, 9, "ORC1026 收款 3,850.00 (BT-090-0124, 2026-10-14), 发票 7,150.00, 差额 3,300.00。严重程度: 中。")
    span(
        ws,
        row,
        10,
        15,
        "转 D-4 收入截止性判断 (REV-02, 尚未执行)。D-2 已评价合同 CON-X01: 控制权在完工日一次性转移, 发票须在 30 天内付款。",
    )
    span(ws, row, 16, 17, "未解决", font=RED)
    row += 2
    row = section(ws, row, n, "关注事项")
    row = kv3(
        ws,
        row,
        n,
        "ORC1024",
        "截至数据日 2026-10-17 尚未收到款项。本程序不对未收款发票作判断, 建议在应收账款期后收款中跟进 (本期计划未覆盖)。",
    )
    row += 1
    row = section(ws, row, n, "结论 (智能体草拟, 待审计师复核)")
    span(
        ws,
        row,
        1,
        n,
        "截止窗口内 6 张发票均有服务完成记录, 签收日均不晚于期末, 未发现跨期入账。1 张发票的收款与发票金额不符 (例外 a), "
        "待 D-4 判断后对收入发生认定下结论; 1 张发票尚未收款, 列为关注事项。",
    )
    row += 2
    row = section(ws, row, n, "交叉索引与证据来源")
    row = kv3(ws, row, n, "交叉索引", "D 收入导引表; D-2 收入合同条款评价; D-4 收入截止性判断; X 错报汇总")
    row = kv3(
        ws,
        row,
        n,
        "证据来源",
        "发票与分录: Xero 同步; 服务完成记录: 审计师导入 delivery_notes.csv; 银行流水: Xero 同步 (账户 090)",
    )
    row += 1
    section(ws, row, n, "复核意见:")


def misstatement_sheet(ws):
    n = 12
    widths(ws, [7, 18, 34, 18, 10, 10, 8, 12, 12, 10, 14, 22])
    m = MATERIALITY
    row = header(ws, n, "X", "错报汇总", "FIN-04 错报累计 (尚未执行, 本页按已认定错报生成)", "系统", "2026-10-21")
    row = section(ws, row, n, "依据")
    row = kv(
        ws,
        row,
        n,
        "准则",
        "ASA 450 第 15 段: (a) 明显微小临界值; (b) 累计的全部错报及是否已更正; (c) 未更正错报是否重大的结论及依据",
    )
    row += 1
    row = section(ws, row, n, "临界值 (见 A-2)")
    for label, amount in (("整体重要性", m["overall"]), ("执行重要性", m["pm"]), ("明显微小临界值", m["ct"])):
        span(ws, row, 1, 2, label, font=GREY)
        put(ws, row, 3, amount, fmt=MONEY, align=Alignment(horizontal="right", vertical="top"))
        row += 1
    row += 1
    row = section(ws, row, n, "累计错报")
    heads = [
        "编号",
        "来源底稿",
        "说明",
        "科目",
        "认定",
        "类型",
        "方向",
        "金额",
        "超过明显微小",
        "状态",
        "对利润的影响",
        "认定人与日期",
    ]
    rows = [
        (
            "X-1",
            "C-4 函证差异评价 (CSH-02)",
            "储蓄账户 091 截至 2026-09-30 的应计利息未入账, 银行回函确认",
            "270 Interest Income",
            "完整性",
            "事实错报",
            "低估",
            48.53,
            "是",
            "未更正",
            "利润低估 48.53",
            "Jordan Ellis, 2026-10-20 (争议裁决后认定)",
        )
    ]
    start = row + 1
    row = table(ws, row, heads, rows, money_cols=(8,), fonts={(0, 10): RED})
    for c in range(1, n + 1):
        put(ws, row, c, None, fill=HEAD)
    put(ws, row, 3, "未更正错报合计", font=BOLD, fill=HEAD)
    put(
        ws,
        row,
        8,
        f'=SUMIF(J{start}:J{row - 1},"未更正",H{start}:H{row - 1})',
        fmt=MONEY,
        font=BOLD,
        fill=HEAD,
        align=Alignment(horizontal="right"),
    )
    total_row = row
    row += 2
    row = section(ws, row, n, "评价")
    span(ws, row, 1, 2, "占执行重要性", font=GREY)
    put(ws, row, 3, f"=H{total_row}/{m['pm']}", fmt="0.0%", align=Alignment(horizontal="right"))
    row += 1
    span(ws, row, 1, 2, "占整体重要性", font=GREY)
    put(ws, row, 3, f"=H{total_row}/{m['overall']}", fmt="0.0%", align=Alignment(horizontal="right"))
    row += 1
    row = kv(
        ws,
        row,
        n,
        "结论",
        "截至目前未更正错报单独及汇总均低于整体重要性; 最终结论在完成阶段 (FIN-01 意见决定) 作出, 届时补充依据。",
    )
    row += 1
    section(ws, row, n, "复核意见:")


def standards_sheet(ws):
    n = 5
    widths(ws, [10, 10, 52, 22, 16])
    row = header(ws, n, "S", "准则文档要求对照", "ASA 230 附录 1 所列各准则的文档要求", "系统", "2026-10-21")
    row = kv(
        ws,
        row,
        n,
        "说明",
        "段落号按仓库中各准则现行文本; ASA 230 附录 1 中 ASA 240, 250, 260 的段落号为修订前编号 (分别为 44-47, 29, 23)。",
    )
    row += 1
    heads = ["准则", "段落", "要求记录的内容", "本档案中的位置", "状态"]
    rows = [
        (
            "ASA 210",
            "10-12",
            "约定条款: 审计目标与范围, 审计师与管理层的责任, 适用的财务报告框架等",
            "B-1, B-2",
            "待补: 承接功能未开",
        ),
        ("ASA 220", "41", "职业道德, 独立性, 承接与保持, 咨询事项的识别, 讨论与结论", "B-1, Z-1", "待补"),
        (
            "ASA 240",
            "45 起",
            "舞弊风险的团队讨论与重大决定, 识别与评估的舞弊风险, 应对与结果",
            "A-3, D-3, G-1, G-2",
            "部分",
        ),
        ("ASA 250", "30", "识别或怀疑的违反法律法规事项, 及与管理层等的讨论", "Z", "未发现, 待完成阶段确认"),
        ("ASA 260", "24", "与治理层口头沟通的事项, 时间与对象; 书面沟通留副本", "Z", "待补"),
        ("ASA 300", "12", "总体审计策略, 审计计划, 及其重大修改与原因", "A-1", "待补: 计划功能未开 (#47)"),
        ("ASA 315", "38", "团队讨论, 了解被审计单位的要点与信息来源, 风险评估程序, 识别与评估的风险", "A-3", "部分"),
        ("ASA 320", "14", "整体重要性, 特定重要性, 执行重要性的金额与考虑因素, 及其修订", "A-2", "缺选择理由"),
        (
            "ASA 330",
            "28-30",
            "总体应对, 进一步程序的性质时间范围, 与认定层次风险的对应, 结果与结论; 报表与会计记录一致",
            "各程序底稿",
            "部分",
        ),
        ("ASA 450", "15", "明显微小临界值, 累计错报及是否更正, 未更正错报是否重大的结论", "X", "结论待完成阶段"),
        ("ASA 540", "39", "会计估计: 了解的要点, 程序与风险的对应, 管理层偏向的迹象等", "—", "待评估是否有重大估计"),
        ("ASA 550", "28", "识别的关联方名称及关系性质", "—", "待补: 关联方程序未开启"),
        ("ASA 600", "59", "集团审计", "—", "不适用: 单一主体"),
        ("ASA 610", "36", "利用内部审计的工作", "—", "不适用: 无内部审计"),
        ("ASA 720", "25", "对其他信息执行的程序, 及所读的其他信息最终版本", "Z", "待补"),
    ]
    fonts = {}
    for i, r in enumerate(rows):
        status = r[4]
        fonts[(i, 5)] = (
            GREEN
            if status == "完成"
            else GREY
            if status.startswith("不适用")
            else RED
            if status.startswith(("待补", "缺"))
            else None
        )
    table(ws, row, heads, rows, fonts=fonts)


if __name__ == "__main__":
    build()
