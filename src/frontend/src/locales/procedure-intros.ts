import type { Lang } from '@/lib/i18n'
import { formatMoney, formatPct } from '@/lib/money'

// 程序介绍: 做什么, 什么时候执行, 需要的资料, 怎么判断, 结果与审计师的工作; 程序页侧栏与程序详情页用.
// 内容按程序库 (knowledge/procedures/*.yaml) 与提示词写, 改程序时同步改这里. project 按本项目最近一次执行的参数
// 快照 (ProcedureRun.params_snapshot) 写出实际取值, 没有执行或快照里没有相关参数时返回 null, 不显示
export type ProcedureIntro = {
  purpose: string // 做什么: 两三句白话, 含针对的认定
  when: string // 什么时候执行: 触发条件, 参数怎么随风险变, 受哪个功能开关控制
  inputs?: string // 需要的资料: 缺了会停下来等数据的那些
  logic: { name: string; detail: string }[] // 怎么判断: 规则程序逐条检查, 模型程序逐步, 人工程序要决定的事
  result: string // 结果与你的工作: 产出什么, 什么情况交审计师
  project?: (params: Params, currency?: string) => string | null // 本项目的取值
}

type Params = Record<string, unknown>

const num = (params: Params, key: string): number | null => {
  const value = params[key]
  return typeof value === 'number' ? value : null
}

const str = (params: Params, key: string): string | null => {
  const value = params[key]
  return typeof value === 'string' ? value : null
}

// 某日前后 n 天的日期 (YYYY-MM-DD), daysBefore 同后端 days_before 过滤器
const daysBefore = (date: string, days: number): string => {
  const value = new Date(`${date}T00:00:00Z`)
  value.setUTCDate(value.getUTCDate() - days)
  return value.toISOString().slice(0, 10)
}

const daysAfter = (date: string, days: number): string => daysBefore(date, -days)

const money = formatMoney
const pct = (value: number) => formatPct(value, value * 100 === Math.round(value * 100) ? 0 : 1)

export const PROCEDURE_INTROS: Record<string, Record<Lang, ProcedureIntro>> = {
  'ACC-01': {
    zh: {
      purpose:
        '由你根据 ASIC / ABN 核查（ACC-04）汇成的承接评分卡和客户资料，决定是否承接本项目。系统只整理材料，结论由你作出。',
      when: '每个项目都执行，在承接阶段（P0）ASIC / ABN 核查（ACC-04）完成之后，作为“业务承接”阶段决定交给你。不随风险变化，没有功能开关。',
      logic: [
        {
          name: '查看核查结果',
          detail:
            '逐项查看 ABN 状态、登记名称、ASIC 登记（新建的客户另有董事名单）是通过、不通过还是待你人工确认。不通过的附例外，每项都可以打开对应的查询记录或摘录页核对。',
        },
        {
          name: '更正客户资料',
          detail:
            '发现客户资料有误时，可以在这一步修改并保存。保存后系统重新做 ASIC / ABN 核查（新建的公司客户先看是否要补 ASIC 摘录），再按新结果请你决定。',
        },
        {
          name: '作出决定',
          detail:
            '三个选项：承接，进入审计计划阶段；不承接，项目以“不承接”关闭并锁定；信息不足，只记下决定，本项继续等你决定。理由选填。',
        },
      ],
      result:
        '你的决定记入审计留痕。选“承接”后项目进入审计计划阶段（P1），财务数据接入并确认已齐后开始计划工作；选“不承接”后项目锁定。',
    },
    en: {
      purpose:
        'You decide whether to accept the engagement, based on the acceptance scorecard produced by ASIC and ABN check (ACC-04) and on the client profile. The system assembles the material; the conclusion is yours.',
      when: 'Runs on every engagement in the acceptance stage (P0), after ASIC and ABN check (ACC-04), as the Engagement acceptance stage decision. It does not vary with risk and has no feature switch.',
      logic: [
        {
          name: 'Review the checks',
          detail:
            'For each check (ABN status, registered name, ASIC registration, and for a new client the directors), see whether it passed, failed or is left for you to confirm manually. Failed checks come with an exception, and each check links to the registry query or extract page behind it.',
        },
        {
          name: 'Correct the client profile',
          detail:
            'If the profile is wrong, edit and save it here. ASIC and ABN check (ACC-04) then runs again, first checking whether a new company client needs an ASIC extract, and you decide on the new results.',
        },
        {
          name: 'Decide',
          detail:
            'Three options: accept, which moves the engagement to planning; decline, which closes the engagement as declined and locks it; or need more information, which records the decision and keeps this item open for you. A reason is optional.',
        },
      ],
      result:
        'Your decision is recorded in the audit trail. After you accept, the engagement moves to planning (P1), where work starts once the financial data is loaded and confirmed complete. After you decline, the engagement is locked.',
    },
  },
  'ACC-04': {
    zh: {
      purpose:
        '核对客户资料中的 ABN 和法定名称在 ABN 登记（ABN Lookup）与 ASIC 公司登记中的记录，确认 ABN 有效、名称一致、公司已登记；新建的客户另核对董事名单。结果汇成承接评分卡，供业务承接（ACC-01）决定。',
      when: '每个项目都执行，在承接阶段（P0）开始时执行。不随风险变化，没有功能开关。你在“业务承接”阶段决定处修改客户资料后会重新执行。',
      inputs:
        '使用演示数据集的项目按数据集中的登记记录核对，不需要另交资料；新建的客户在线查询 ABN Lookup。新建的客户如果是公司（ABN 登记上有 ACN），还需要上传 ASIC 公司摘录，摘录到之前停下来等数据；客户确实没有时可标“客户无此类资料”，本程序照常执行，ASIC 登记与董事两项留给你人工确认。',
      logic: [
        {
          name: 'ABN 处于有效状态',
          detail: 'ABN 登记查得到这个 ABN，且状态为有效（Active）；查不到或不是有效状态即不通过。严重程度高。',
        },
        {
          name: '登记名称与客户资料一致',
          detail:
            '登记名称与客户资料中的法定名称在忽略大小写、空格和标点后相同。ABN 查不到即不通过；登记名称不公开时留给你人工确认。严重程度高。',
        },
        {
          name: '公司已在 ASIC 登记',
          detail:
            '按 ABN 登记上的 ACN 查 ASIC，状态为已登记（Registered）；ABN 查不到或没有 ACN 即不通过。新建的客户改为核对上传的 ASIC 摘录：状态为已登记，且公司名称与法定名称一致；已上传的摘录都对不上这个 ACN 即不通过；不是公司或没有可用的摘录时留给你人工确认。严重程度高。',
        },
        {
          name: '董事名单与 ASIC 摘录一致（新建的客户）',
          detail: '客户资料中的董事与摘录上职务为董事的姓名逐一对应，多出或缺少都不通过；公司秘书不比。严重程度高。',
        },
        {
          name: '查不了登记时',
          detail:
            'ABN Lookup 没有配置、连不上或返回错误时，各项都留给你人工确认，不记例外。每次查询（包括没查成的）都记入审计留痕，作为对应检查的证据。',
        },
      ],
      result:
        '不通过的检查各记一条例外（严重程度高），待人工确认的不记例外。各项结果汇成承接评分卡，连同例外在“业务承接”阶段决定（ACC-01）时交给你，不单独进入复核队列；本程序执行完即完成。执行出错时在复核队列交给你重跑。',
    },
    en: {
      purpose:
        'Checks the ABN and legal name in the client profile against the ABN register (ABN Lookup) and the ASIC company register, to confirm that the ABN is active, the name matches and the company is registered; for a new client it also checks the directors. The results form the acceptance scorecard for Engagement acceptance (ACC-01).',
      when: 'Runs on every engagement at the start of the acceptance stage (P0). It does not vary with risk and has no feature switch. It runs again after you edit the client profile at the Engagement acceptance stage decision.',
      inputs:
        'Engagements on a demo dataset are checked against the registry records in the dataset and need nothing more; a new client is looked up live on ABN Lookup. A new client that is a company (its ABN record has an ACN) also needs an uploaded ASIC company extract, and the procedure waits for data until it arrives. If the client has none, mark it as such: the procedure then runs and leaves ASIC registration and the directors for you to confirm manually.',
      logic: [
        {
          name: 'ABN is active',
          detail:
            'The ABN is found on the ABN register with status Active. Not found, or any other status, fails. High severity.',
        },
        {
          name: 'Registered name matches the client profile',
          detail:
            'The registered name equals the legal name in the client profile, ignoring case, spaces and punctuation. It fails if the ABN is not found, and is left for you to confirm manually when the registered name is not public. High severity.',
        },
        {
          name: 'Company is registered with ASIC',
          detail:
            'The ACN on the ABN record is looked up on the ASIC register and must show Registered; it fails if the ABN is not found or has no ACN. For a new client the uploaded ASIC extract is used instead: it must show Registered and a company name matching the legal name. It fails when no uploaded extract matches the ACN, and is left for you to confirm manually when the client is not a company or no usable extract exists. High severity.',
        },
        {
          name: 'Directors match the ASIC extract (new clients)',
          detail:
            'The directors in the client profile match, name for name, those shown as directors on the extract; any extra or missing name fails. The company secretary is not compared. High severity.',
        },
        {
          name: 'When a register cannot be queried',
          detail:
            'If ABN Lookup is not configured, cannot be reached or returns an error, every check is left for you to confirm manually and no exception is raised. Every query, including one that could not be made, is recorded in the audit trail as the evidence for its check.',
        },
      ],
      result:
        'Each failed check is recorded as a high-severity exception; checks left for manual confirmation raise none. The results form the acceptance scorecard, which comes to you with the exceptions at the Engagement acceptance (ACC-01) stage decision rather than through the review queue; the procedure completes once it has run. If it fails to run, the review queue asks you to rerun it.',
    },
  },
  'ACC-08': {
    zh: {
      purpose:
        '阅读客户签回的约定书和委任文件（董事决议、股东指示、审计师同意书），核对约定书是否写明准则要求的事项、董事是否确认了自己的责任、审计是否有依据、审计师的委任是否有效，并给出建议，供你决定约定条款与委任是否齐备。不起草约定书。',
      when: '这条程序本版尚未实现，下面是设计中的做法。属于“承接”功能，打开时才执行；在业务承接（ACC-01）之后、进入审计计划阶段之前执行。不随风险变化。',
      inputs: '客户签回的约定书和委任文件（董事决议、股东指示、审计师同意书）。',
      logic: [
        {
          name: '摘出事实（模型）',
          detail:
            '模型逐页阅读全部约定文件，摘出约定书写明的财务报告框架、约定书的五项要素和董事确认的五项责任是否写明、约定书日期与签回人、股东指示、委任审计师的决议、审计师同意书、缩短财年的决议。每项标出所在文件和页码；文件没写的留空，不推测。',
        },
        {
          name: '核对要求（规则）',
          detail:
            '按摘出的事实逐项核对：框架可以接受（特殊目的框架也可以，但报告须加强调事项段）；五项要素齐全；五项责任都已确认；小型私人公司须有持至少 5% 表决权的股东签署、要求审计、并在财年结束后 12 个月内作出的指示；委任之前已取得审计师以事务所和本人名义签署的书面同意；约定书由董事签回；本期不足 12 个月时，缩短财年的决议合规。不通过的各记一条例外。',
        },
        {
          name: '给出建议（模型）',
          detail:
            '模型根据事实和核对结果，建议“具备承接前提”或“需要补充证据”，说明每项不通过意味着什么，客户能否补正（如在重新委任之前取得签署的同意书），还是应当不承接。建议不替代核对结果。',
        },
      ],
      result:
        '摘出的事实、核对结果、例外和模型建议一起在“约定条款与委任核验”阶段决定时交给你，不进入复核队列。你可以选通过（进入审计计划阶段）、不承接（关闭本项目）或信息不足（客户补交文件后重新阅读）。核对得到的适用框架和约定书会写进总体审计计划（PLN-15）。',
    },
    en: {
      purpose:
        'Reads the engagement letter and appointment documents the client has signed and returned (directors’ resolution, members’ direction, auditor’s consent), checks that the letter records the matters the standards require, that the directors have acknowledged their responsibilities, that the audit has a basis and that the auditor’s appointment is valid, and gives a recommendation for your decision on whether the terms and appointment are in order. It does not draft the engagement letter.',
      when: 'Not yet implemented in this version; what follows is the intended design. Part of the "Acceptance" feature and runs only when it is on, after Engagement acceptance (ACC-01) and before planning starts. It does not vary with risk.',
      inputs:
        'The signed engagement letter and the appointment documents (directors’ resolution, members’ direction, auditor’s consent).',
      logic: [
        {
          name: 'Extract the facts (model)',
          detail:
            'The model reads every page of the documents and records the financial reporting framework the letter identifies, whether the letter covers the five required matters and the directors’ five acknowledgements, the letter’s date and signatory, the members’ direction, the resolution appointing the auditor, the auditor’s consent and any resolution shortening the financial year. Each fact points to its document and page; facts the documents do not state are left blank, never inferred.',
        },
        {
          name: 'Check the requirements (rules)',
          detail:
            'The facts are checked one by one: the framework is acceptable (a special purpose framework is too, but the report then needs an emphasis of matter paragraph); all five matters are in the letter; all five responsibilities are acknowledged; for a small proprietary company, a direction signed by members with at least 5% of the votes, asking for an audit and made within 12 months after year end; the auditor’s written consent, signed in the firm name and in the auditor’s own name, given before the appointment; the letter signed back by a director; and, where the period is shorter than 12 months, a compliant resolution changing the financial year. Each failed check is recorded as an exception.',
        },
        {
          name: 'Recommend (model)',
          detail:
            'From the facts and checks the model recommends either that the preconditions are present or that more evidence is needed, explaining what each failed check means and whether the client can cure it (for example, a consent signed before a fresh appointment) or the engagement should not be accepted. The recommendation does not replace the checks.',
        },
      ],
      result:
        'The facts, checks, exceptions and the model’s recommendation come to you together at the Engagement terms and appointment stage decision, not through the review queue. You can approve (planning starts), decline (the engagement is closed) or ask for more information (the documents are read again once the client adds to them). The framework and engagement letter confirmed here are carried into Overall audit plan (PLN-15).',
    },
  },
  'DOC-04': {
    zh: {
      purpose:
        '从上传的收入合同中摘出客户名称与 ABN、合同总价、签订日期，以及付款、控制权转移、退货、返利与折扣、终止、额外保证、关联方等条款的原文和页码，供收入合同条款评价（REV-03）等程序使用。',
      when: '每个项目都执行，在审计计划阶段（P1）你确认财务数据已齐之后、初步分析之前。不随风险变化，没有功能开关。合同到得晚的，在执行阶段补做。',
      inputs:
        '收入合同（PDF）。缺了本程序先停下来等数据，其余计划工作照常进行；客户确实没有时可标“客户无此类资料”，本程序记为未执行。',
      logic: [
        {
          name: '确定要读的合同',
          detail:
            '每个合同编号（取文件名）读一份，同名文件重新上传过的读最新一份。上传时已经抽取过的合同直接沿用，不重读，也不覆盖你的修改。',
        },
        {
          name: '摘出合同要素（模型）',
          detail:
            '模型逐页阅读合同，摘出对方（客户）名称、ABN、合同总价（同时列出含税与不含税金额时取不含 GST 的；只有单价时留空）和签订日期（双方签署日期不同时取较晚者），每项标出页码。合同没写的留空，不推算。',
        },
        {
          name: '摘录条款（模型）',
          detail:
            '按付款、控制权转移、退货权、返利与折扣、终止、超出常规质保的保证、关联方声明七类，摘出条款原文（含条款编号）和起始页；只说明某项权利不适用的条款跳过。',
        },
        {
          name: '校验与重做',
          detail:
            '每个摘出的值都要有出处，出处只能指向这份合同，页码不能超出页数。不通过时把问题反馈给模型重做，最多共三次。',
        },
      ],
      result:
        '通过的合同写入合同记录，文件状态记为“已完成”，不逐份交给你复核；你可以在数据管理的文件详情里查看并修改摘出的要素。三次仍不通过的合同记为“待人工处理”，在复核队列交给你，只能选择重跑。收入合同条款评价（REV-03）和关联方图谱研判（GEN-03）读取这些合同要素。',
    },
    en: {
      purpose:
        'Extracts from each uploaded revenue contract the customer’s name and ABN, the total price and the signing date, plus the wording and page of clauses on payment, transfer of control, returns, rebates and discounts, termination, extra guarantees and related parties, for Revenue contract terms (REV-03) and other procedures.',
      when: 'Runs on every engagement in planning (P1), after you confirm the financial data is complete and before the preliminary analysis. It does not vary with risk and has no feature switch. Contracts that arrive late are processed during fieldwork.',
      inputs:
        'Revenue contracts (PDF). Without them this procedure waits for data while the rest of planning carries on. If the client has none, mark it as such and the procedure is recorded as not performed.',
      logic: [
        {
          name: 'Choose the contracts to read',
          detail:
            'One file per contract number (taken from the file name); where a file of the same name was uploaded again, the latest one. Contracts already extracted on upload are reused as they are, without reading them again or overwriting your edits.',
        },
        {
          name: 'Extract the contract details (model)',
          detail:
            'The model reads the contract page by page and records the counterparty (the customer), its ABN, the total price (the GST-exclusive amount when both are shown; blank when the contract sets only unit prices) and the signing date (the later date when the parties signed on different days), each with its page. Anything the contract does not state is left blank, never calculated.',
        },
        {
          name: 'Extract the clauses (model)',
          detail:
            'Clauses of seven types are copied word for word, with their numbers and starting page: payment, transfer of control, return rights, rebates and discounts, termination, guarantees beyond standard warranty, and related party statements. A clause that only says a right does not apply is skipped.',
        },
        {
          name: 'Validate and retry',
          detail:
            'Every extracted value must point to a page of this contract within its page count. If not, the problems go back to the model to try again, up to three attempts in all.',
        },
      ],
      result:
        'Contracts that pass are saved and their files marked done; they are not sent to you one by one, and you can view and edit the extracted details in the file view under Data management. A contract that still fails after three attempts is marked as pending auditor action and comes to you in the review queue, where the only option is to rerun. Revenue contract terms (REV-03) and Related party analysis (GEN-03) use these details.',
    },
  },
  'DOC-06': {
    zh: {
      purpose:
        '从收到的银行函证回函中读出银行确认的余额、余额截至日和截至该日已计提未入账的利息，更新对应账户的函证记录，供函证差异评价（CSH-02）与账面核对。',
      when: '在程序执行阶段（P2）函证发起（CSH-04）之后执行。现金的“存在”风险为中、高或特别风险时，作为函证差异评价（CSH-02）的前置程序纳入。没有功能开关，只读已收到回函的账户。',
      logic: [
        {
          name: '确定要读的回函',
          detail: '取银行账户表中仍在、账户信息没有变过的账户的函证，有回函的才读。',
        },
        {
          name: '读出回函内容（模型）',
          detail:
            '模型逐页阅读回函，读出出具回函的银行、账号（不含 BSB）、确认余额（透支为负数）、余额截至日、回函方式（直接寄给或发邮件给审计师、经客户或客户能访问的门户、口头），以及截至余额日已计提未入账的利息（银行欠客户为正，客户欠银行为负；写明为零记 0，没提到或只给整期利息时留空，不推算）。每项标出页码。',
        },
        {
          name: '核对账号',
          detail:
            '读出的账号与被函证账户的账号不符，说明回函不是这个账户的，本次读取不通过。回函写的方式与发函回执记录的不同时只作提示，以回执为准。',
        },
        {
          name: '校验与重做',
          detail:
            '出处只能指向这份回函且页码在页数内，读出应计利息时必须有出处。不通过时把问题反馈给模型重做，最多共三次。',
        },
        {
          name: '沿用你的修改',
          detail: '你改过某份回函的读取结果、而回函原件没有换过时，不再重读，沿用你的修改并记入审计留痕。',
        },
      ],
      result:
        '通过的回函更新函证记录（确认余额、截至日、应计利息），文件状态记为“已完成”，不逐份交给你复核；你可以在数据管理的文件详情里修改银行名称、截至日和确认余额。三次仍不通过的回函记为“待人工处理”，在复核队列交给你，只能选择重跑。函证差异评价（CSH-02）用这些结果与账面和银行流水比较。',
    },
    en: {
      purpose:
        'Reads each bank confirmation reply for the confirmed balance, the date it is confirmed as at and any interest accrued to that date but not yet posted, and updates the confirmation record for the account, for Confirmation differences (CSH-02) to compare with the books.',
      when: 'Runs during fieldwork (P2), after Bank confirmation requests (CSH-04). It is brought in as a prerequisite of Confirmation differences (CSH-02) when cash existence carries a medium, high or significant risk. It has no feature switch and reads only accounts with a reply on file.',
      logic: [
        {
          name: 'Choose the replies to read',
          detail:
            'Confirmations for accounts still in the bank accounts table whose details have not changed, where a reply has been received.',
        },
        {
          name: 'Read the reply (model)',
          detail:
            'The model reads the reply page by page and records the bank that issued it, the account number (without the BSB), the confirmed balance (negative if overdrawn), the date it is confirmed as at, how the reply was sent (posted or emailed directly to the auditor, through the client or a portal the client can access, or orally) and the interest accrued to that date but not yet posted (positive if the bank owes it, negative if the customer does; 0 when stated as nil; blank when not mentioned or when only a whole period’s interest is given, never calculated). Each value points to its page.',
        },
        {
          name: 'Match the account number',
          detail:
            'If the account number read differs from that of the confirmed account, the reply is not for that account and the reading fails. If the delivery method stated differs from the dispatch receipt, this is only noted; the receipt prevails.',
        },
        {
          name: 'Validate and retry',
          detail:
            'Every value must point to a page of this reply within its page count, and accrued interest must have its own pointer. If not, the problems go back to the model to try again, up to three attempts in all.',
        },
        {
          name: 'Keep your edits',
          detail:
            'Where you have edited the details read from a reply and the reply itself has not been replaced, it is not read again: your edit is kept and this is noted in the audit trail.',
        },
      ],
      result:
        'Replies that pass update the confirmation (confirmed balance, date and accrued interest) and their files are marked done; they are not sent to you one by one, and you can edit the bank name, date and confirmed balance in the file view under Data management. A reply that still fails after three attempts is marked as pending auditor action and comes to you in the review queue, where the only option is to rerun. Confirmation differences (CSH-02) compares these results with the books and bank transactions.',
    },
  },
  'PLN-01': {
    zh: {
      purpose:
        '从本期试算表取营业收入、资产总额和税前利润三个基准，按各自的比例区间算出重要性水平的候选范围，供你在“重要性与风险清单”阶段决定时选定重要性水平、实际执行的重要性和明显微小错报临界值。',
      when: '每个项目都执行，在审计计划阶段（P1）紧接初步分析程序（PLN-07）之后。不随风险变化，没有功能开关。你在“重要性与风险清单”阶段决定处退回时重新执行。',
      inputs: '科目表和本期试算表。它们是确认财务数据已齐的前提，缺了整个计划阶段停下来等数据。',
      logic: [
        {
          name: '取基准金额（规则）',
          detail:
            '营业收入取收入类科目余额（贷方为正）；资产总额取全部资产类科目余额；税前利润取全部收入与费用类科目余额相抵，所得税费用除外。',
        },
        {
          name: '算候选范围',
          detail:
            '每个基准乘以它的比例区间上下限，得出重要性水平的候选金额。基准不为正的（如税前亏损）照样列出，标为本期不适用，不能选。',
        },
        {
          name: '你的选择（人工）',
          detail:
            '选定基准，并在它的区间内定重要性水平的比例；实际执行的重要性取重要性水平的一定比例，须在规定区间内；明显微小错报临界值取重要性水平的一定比例，大于 0 且不超过上限。系统据此算出三个金额。',
        },
      ],
      result:
        '候选范围在“重要性与风险清单”阶段决定（PLN-01/03）时交给你，与风险清单终审（PLN-03）一起决定。批准时必须选定重要性，三个金额随即确定，供后续程序作门槛（如收入截止性判断（REV-02）、全量分录扫描（GEN-05））、复核分档和错报累计使用。首次执行时还带出本项目的审计目标与风险清单，交风险清单终审。',
      project: (p) => {
        const names: Record<string, string> = {
          revenue: '营业收入',
          total_assets: '资产总额',
          profit_before_tax: '税前利润',
        }
        const list = Array.isArray(p.candidates) ? p.candidates : []
        const parts = list.flatMap((c) => {
          if (typeof c?.basis !== 'string' || typeof c.pct_low !== 'number' || typeof c.pct_high !== 'number') return []
          const share = c.pct_low === c.pct_high ? pct(c.pct_low) : `${pct(c.pct_low)}–${pct(c.pct_high)}`
          return [`${names[c.basis] ?? c.basis} ${share}`]
        })
        const range = Array.isArray(p.performance_pct_range) ? p.performance_pct_range : []
        const trivial = num(p, 'clearly_trivial_pct')
        if (parts.length === 0 || typeof range[0] !== 'number' || typeof range[1] !== 'number' || trivial == null)
          return null
        return `重要性水平占基准的比例：${parts.join('，')}。实际执行的重要性为重要性水平的 ${pct(range[0])}–${pct(range[1])}，明显微小错报临界值不超过重要性水平的 ${pct(trivial)}。`
      },
    },
    en: {
      purpose:
        'Takes three benchmarks from the current trial balance (revenue, total assets and profit before tax) and applies each one’s percentage range to give candidate ranges for overall materiality, from which you set overall materiality, performance materiality and the clearly trivial threshold at the Materiality and risk register stage decision.',
      when: 'Runs on every engagement in planning (P1), straight after Preliminary analytical procedures (PLN-07). It does not vary with risk and has no feature switch. It runs again if you send the Materiality and risk register stage decision back.',
      inputs:
        'The chart of accounts and the current trial balance. Both are required before the financial data can be confirmed complete, so without them planning waits for data.',
      logic: [
        {
          name: 'Take the benchmarks (rules)',
          detail:
            'Revenue is the balance of the revenue accounts (credit positive); total assets is the balance of all asset accounts; profit before tax is the net of all revenue and expense accounts, excluding income tax expense.',
        },
        {
          name: 'Work out the candidate ranges',
          detail:
            'Each benchmark is multiplied by the low and high ends of its percentage range to give candidate amounts for overall materiality. A benchmark that is not positive (such as a loss before tax) is still listed but marked not applicable this period and cannot be chosen.',
        },
        {
          name: 'Your choice (auditor)',
          detail:
            'Choose a benchmark and an overall percentage within its range; performance materiality as a share of overall materiality, within the set range; and the clearly trivial threshold as a share of overall materiality, above zero and no more than the upper limit. The three amounts are calculated from your choice.',
        },
      ],
      result:
        'The candidate ranges come to you at the Materiality and risk register stage decision (PLN-01/03), decided together with Risk register sign-off (PLN-03). Approving requires a materiality choice, which fixes the three amounts used as thresholds by later procedures (such as Revenue cut-off (REV-02) and Full journal entry scan (GEN-05)), for review tiers and for misstatement accumulation. On its first run it also brings in the engagement’s audit objectives and risk register for that sign-off.',
      project: (p) => {
        const names: Record<string, string> = {
          revenue: 'revenue',
          total_assets: 'total assets',
          profit_before_tax: 'profit before tax',
        }
        const list = Array.isArray(p.candidates) ? p.candidates : []
        const parts = list.flatMap((c) => {
          if (typeof c?.basis !== 'string' || typeof c.pct_low !== 'number' || typeof c.pct_high !== 'number') return []
          const share = c.pct_low === c.pct_high ? pct(c.pct_low) : `${pct(c.pct_low)}–${pct(c.pct_high)}`
          return [`${names[c.basis] ?? c.basis} ${share}`]
        })
        const range = Array.isArray(p.performance_pct_range) ? p.performance_pct_range : []
        const trivial = num(p, 'clearly_trivial_pct')
        if (parts.length === 0 || typeof range[0] !== 'number' || typeof range[1] !== 'number' || trivial == null)
          return null
        return `Overall materiality as a share of the benchmark: ${parts.join(', ')}. Performance materiality is ${pct(range[0])}–${pct(range[1])} of overall materiality; the clearly trivial threshold is at most ${pct(trivial)} of it.`
      },
    },
  },
  'PLN-03': {
    zh: {
      purpose:
        '由你终审风险清单与审计目标，确认每项风险的等级（低、中、高、特别风险），必要时调整。定稿的等级决定执行阶段纳入哪些审计程序，以及各程序按哪一档风险取参数。',
      when: '每个项目都执行，在审计计划阶段（P1），与重要性水平（PLN-01）在同一个“重要性与风险清单”阶段决定中完成。没有功能开关。',
      logic: [
        {
          name: '风险清单从哪里来',
          detail:
            '重要性水平（PLN-01）首次执行时带出本项目的审计目标与风险清单：使用演示数据集的项目取数据集预置的清单，新建的客户取系统的通用模板。',
        },
        {
          name: '调整风险等级',
          detail: '可以逐项改等级，理由选填。改过的风险记你为确认人；所属审计目标的风险等级取其下各项风险的最高等级。',
        },
        {
          name: '检查程序覆盖',
          detail:
            '按调整后的等级，每个审计目标都要至少触发一条程序。有目标没有程序覆盖时不能批准，只能退回或再调整等级。',
        },
        {
          name: '作出决定',
          detail:
            '批准（同时须选定重要性）、退回（重新做初步分析和重要性计算）或信息不足（只记下决定，本项继续等你决定）。理由选填。',
        },
      ],
      result:
        '批准后风险清单与审计目标定稿，系统据此装配执行阶段的程序包：按审计目标匹配的程序，加上每个项目都做的程序，并补齐它们的前置程序。“总体审计计划”功能打开时，先起草总体审计计划（PLN-15）交你批准；关闭时直接开始执行。',
    },
    en: {
      purpose:
        'You sign off the risk register and audit objectives, confirming the level of each risk (low, medium, high or significant) and changing it where needed. The final levels decide which audit procedures are brought into fieldwork and which risk tier each procedure takes its parameters from.',
      when: 'Runs on every engagement in planning (P1) and is completed in the same Materiality and risk register stage decision as Materiality (PLN-01). It has no feature switch.',
      logic: [
        {
          name: 'Where the risk register comes from',
          detail:
            'The first run of Materiality (PLN-01) brings in the engagement’s audit objectives and risk register: the preset list in the demo dataset, or the system’s general template for a new client.',
        },
        {
          name: 'Change risk levels',
          detail:
            'Any risk’s level can be changed; a reason is optional. You are recorded as the approver of each risk you change, and each audit objective takes the highest level among its risks.',
        },
        {
          name: 'Check procedure coverage',
          detail:
            'At the revised levels every audit objective must trigger at least one procedure. If any objective is not covered you cannot approve; send it back or change a level.',
        },
        {
          name: 'Decide',
          detail:
            'Approve (with a materiality choice), send back (the preliminary analysis and materiality are redone) or need more information (the decision is recorded and this item stays open for you). A reason is optional.',
        },
      ],
      result:
        'Once approved, the risk register and audit objectives are final and the fieldwork pack is assembled from them: the procedures matched to the audit objectives, the procedures performed on every audit, and their prerequisites. With the "Overall audit plan" feature on, Overall audit plan (PLN-15) first drafts the plan for your approval; with it off, fieldwork starts straight away.',
    },
  },
  'PLN-07': {
    zh: {
      purpose:
        '比较本期与上期试算表，找出变动异常大的科目，计算毛利率和收入增长率，并按月汇总本期收入与费用，帮你识别可能存在重大错报风险的领域，为重要性与风险清单的决定提供参考。',
      when: '每个项目都执行，在审计计划阶段（P1）你确认财务数据已齐之后执行，紧接着计算重要性水平（PLN-01）。不随风险变化，没有功能开关。你在“重要性与风险清单”阶段决定处退回时重新执行。',
      inputs: '科目表、本期与上期试算表、本期总账分录。它们是确认财务数据已齐的前提，缺了整个计划阶段停下来等数据。',
      logic: [
        {
          name: '科目变动超阈值',
          detail:
            '资产负债类科目比较本期末与期初余额（期初取上期末余额，上期损益结转进留存收益）；损益类科目只在本期与上期等长时比较两期金额。变动同时达到比较基数绝对值的一定比例和本期收入的一定比例时记为例外。严重程度中。',
        },
        {
          name: '比率',
          detail:
            '计算本期和上期的毛利率（收入减销售成本后除以收入）以及收入增长率；某期缺数据或收入为 0 时不算对应的比率。',
        },
        {
          name: '月度趋势',
          detail: '按月汇总本期总账分录中的收入、费用和其中的员工费用；没有分录的月份记 0，整月缺分录一眼可见。',
        },
      ],
      result:
        '每个超阈值的科目记一条例外，连同比率和月度趋势在“重要性与风险清单”阶段决定（PLN-01/03）时交给你，不单独进入复核队列；本程序执行完即完成。总体审计计划（PLN-15）起草时也参考这些结果。之后数据有变动时只标记为过期，不能单独重跑；需要重做时，在“重要性与风险清单”阶段决定处退回。',
      project: (p) => {
        const ratio = num(p, 'change_pct')
        const share = num(p, 'min_change_revenue_pct')
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        if (ratio == null || share == null || start == null || end == null) return null
        const text = `本期 ${start} 至 ${end}。变动同时达到比较基数的 ${pct(ratio)} 和本期收入的 ${pct(share)} 才记为例外。`
        const prior = str(p, 'prior_period_start')
        if (prior == null) return `${text}上期起始日不明，按与本期等长处理，损益类科目也比较。`
        // 整月数, 同后端 age() 取月: 止日的日小于起日的日时少算一个月
        const months = (from: string, to: string) =>
          (Number(to.slice(0, 4)) - Number(from.slice(0, 4))) * 12 +
          Number(to.slice(5, 7)) -
          Number(from.slice(5, 7)) -
          (Number(to.slice(8)) < Number(from.slice(8)) ? 1 : 0)
        const same = prior.slice(8) === start.slice(8) && months(prior, start) === months(start, daysAfter(end, 1))
        return `${text}上期 ${prior} 至 ${daysBefore(start, 1)}，${same ? '与本期等长，损益类科目也比较。' : '与本期不等长，损益类科目不比较。'}`
      },
    },
    en: {
      purpose:
        'Compares the current and prior trial balances to find accounts that moved unusually, works out gross margin and revenue growth, and totals current-period revenue and expenses by month, to help you identify areas that may carry a risk of material misstatement before you decide on materiality and the risk register.',
      when: 'Runs on every engagement in planning (P1) once you confirm the financial data is complete, just before Materiality (PLN-01). It does not vary with risk and has no feature switch. It runs again if you send the Materiality and risk register stage decision back.',
      inputs:
        'The chart of accounts, the current and prior trial balances and the current journal entries. They are required before the financial data can be confirmed complete, so without them planning waits for data.',
      logic: [
        {
          name: 'Account movement above threshold',
          detail:
            'Balance sheet accounts compare the period-end balance with the opening balance (the prior period-end balance, with the prior profit or loss carried into retained earnings); profit or loss accounts compare the two periods only when they are of equal length. A movement is an exception when it reaches both a set share of the comparison base (in absolute terms) and a set share of current revenue. Medium severity.',
        },
        {
          name: 'Ratios',
          detail:
            'Gross margin (revenue less cost of sales, over revenue) for both periods, and revenue growth. A ratio is left out when a period has no data or its revenue is zero.',
        },
        {
          name: 'Monthly trend',
          detail:
            'Current-period journal entries are totalled by month into revenue, expenses and, within expenses, employee costs. Months without entries show zero, so a missing month stands out.',
        },
      ],
      result:
        'Each account above the threshold is recorded as an exception and comes to you, with the ratios and the monthly trend, at the Materiality and risk register stage decision (PLN-01/03) rather than through the review queue; the procedure completes once it has run. Overall audit plan (PLN-15) also draws on these results. If the data changes later, the procedure is only marked out of date and cannot be rerun on its own; to redo it, send the Materiality and risk register stage decision back.',
      project: (p) => {
        const ratio = num(p, 'change_pct')
        const share = num(p, 'min_change_revenue_pct')
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        if (ratio == null || share == null || start == null || end == null) return null
        const text = `Current period ${start} to ${end}. A movement is an exception only when it reaches both ${pct(ratio)} of the comparison base and ${pct(share)} of current revenue.`
        const prior = str(p, 'prior_period_start')
        if (prior == null)
          return `${text} The prior period start is not known, so the periods are treated as equal in length and profit or loss accounts are compared too.`
        // 整月数, 同后端 age() 取月: 止日的日小于起日的日时少算一个月
        const months = (from: string, to: string) =>
          (Number(to.slice(0, 4)) - Number(from.slice(0, 4))) * 12 +
          Number(to.slice(5, 7)) -
          Number(from.slice(5, 7)) -
          (Number(to.slice(8)) < Number(from.slice(8)) ? 1 : 0)
        const same = prior.slice(8) === start.slice(8) && months(prior, start) === months(start, daysAfter(end, 1))
        return `${text} Prior period ${prior} to ${daysBefore(start, 1)}, ${same ? 'the same length as the current period, so profit or loss accounts are compared too.' : 'a different length from the current period, so profit or loss accounts are not compared.'}`
      },
    },
  },
  'PLN-15': {
    zh: {
      purpose:
        '起草总体审计策略与审计计划，交你批准。计划的范围、程序与参数、重要性和特别风险由系统按已定的结果组装，模型只写叙述：总体策略、各段说明、每条程序的性质、时间与范围、时间表，以及项目质量复核和审计师的专家的建议。',
      when: '属于“总体审计计划”功能，打开时才执行；在“重要性与风险清单”批准、执行阶段程序包装配之后，开始执行之前。不随风险变化。你退回计划后，按你的理由起草下一修订。',
      logic: [
        {
          name: '组装计划骨架（规则）',
          detail:
            '取审计期间，约定条款与委任核验（ACC-08）确认的财务报告框架与约定书（没有核验时留空），你选定的重要性与三个金额，风险等级为特别风险的审计目标，以及执行阶段的每条程序与参数。模型不能增删或改动这些内容。',
        },
        {
          name: '写计划叙述（模型）',
          detail:
            '模型读客户资料、审计目标与风险清单、计划的程序与参数、本期试算表、初步分析结果和重要性，写总体策略和各段：范围、特别风险及应对、重要性、时间安排必写；首次承接时的期初余额、持续经营迹象、没有审计目标覆盖的重大科目、报告及报送期限视资料而写。另为每条程序写性质、时间与范围，给出时间表、是否需要项目质量复核及依据、是否需要审计师的专家。退回重拟时针对你的理由修改相关部分。',
        },
        {
          name: '校验与重做',
          detail:
            '引用的准则只能是本程序列出的；每条计划的程序都要有说明且不重复；时间表不早于期末；必写的段齐全；项目质量复核建议须写依据。不通过时把问题反馈给模型重做，最多共三次。',
        },
        {
          name: '你的决定（人工）',
          detail:
            '在“总体审计计划”阶段决定处查看计划（程序表、时间表与修订历史在审计计划页）。批准时须确认是否需要项目质量复核并写明依据，并确认是否不需要审计师的专家（不确认只作记录）；退回时按你写的理由（选填）起草下一修订；信息不足只记下决定，本项继续等你决定。',
        },
      ],
      result:
        '批准后计划定稿，记下批准人、时间和你的确认，执行阶段（P2）随即开始；退回后旧修订标为已被取代。模型三次仍写不出合格的计划时，在复核队列交给你重跑。功能关闭时不起草计划，装配完程序包直接开始执行。',
      project: (p) => {
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        if (start == null || end == null) return null
        return `计划覆盖审计期间 ${start} 至 ${end}。`
      },
    },
    en: {
      purpose:
        'Drafts the overall audit strategy and audit plan for your approval. The scope, procedures and parameters, materiality and significant risks are assembled by the system from what has already been decided; the model writes only the narrative: the overall strategy, the plan sections, the nature, timing and extent of each procedure, the timetable, and recommendations on engagement quality review and an auditor’s expert.',
      when: 'Part of the "Overall audit plan" feature and runs only when it is on, after the Materiality and risk register stage decision is approved and the fieldwork pack is assembled, before fieldwork starts. It does not vary with risk. If you send the plan back, the next revision is drafted against your reason.',
      logic: [
        {
          name: 'Assemble the plan skeleton (rules)',
          detail:
            'The audit period; the financial reporting framework and engagement letter confirmed by Engagement terms and appointment check (ACC-08), left blank if that check was not made; your materiality choice and the three amounts; the audit objectives with significant risk; and each fieldwork procedure with its parameters. The model cannot add, drop or change any of these.',
        },
        {
          name: 'Write the narrative (model)',
          detail:
            'The model reads the client profile, the audit objectives and risk register, the planned procedures and parameters, the current trial balance, the preliminary analysis and materiality, and writes the overall strategy and the sections. Scope, significant risks and responses, materiality and timing are always written; opening balances for a first engagement, going concern indicators, material accounts no objective covers, and the report and its deadline are added where the inputs support them. It also writes the nature, timing and extent of each procedure, a timetable, whether an engagement quality review is needed and why, and whether an auditor’s expert is needed. A redraft addresses your reason in the affected parts.',
        },
        {
          name: 'Validate and retry',
          detail:
            'Cited standards must be among those listed for this procedure; every planned procedure needs exactly one note; no milestone may fall before period end; the required sections must be present; and the engagement quality review recommendation must state its basis. If not, the problems go back to the model to try again, up to three attempts in all.',
        },
        {
          name: 'Your decision (auditor)',
          detail:
            'Review the plan at the Overall audit plan stage decision (the procedure table, timetable and revision history are on the Audit plan page). To approve, confirm whether an engagement quality review is needed and give the basis, and confirm whether an auditor’s expert is not needed (leaving it unconfirmed is only recorded). Sending back has the plan redrafted as the next revision, addressing your reason if you give one; need more information records the decision and keeps this item open.',
        },
      ],
      result:
        'On approval the plan is final, with the approver, the time and your confirmations recorded, and fieldwork (P2) starts. A plan sent back is marked superseded by the next revision. If the model still cannot produce a valid plan after three attempts, the review queue asks you to rerun. With the feature off no plan is drafted, and fieldwork starts once the pack is assembled.',
      project: (p) => {
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        if (start == null || end == null) return null
        return `The plan covers the audit period ${start} to ${end}.`
      },
    },
  },
  'REV-08': {
    zh: {
      purpose:
        '检查期末前一段时间内入账的全部销售发票，逐张核对发票、服务完成记录（发运单签收）和银行收款三者是否对得上，确认这些收入确实发生、并记在了正确的期间。针对的认定：发生、截止。',
      when: '收入有“发生”或“截止”风险时执行，任何风险等级都会执行。风险越高，检查的窗口越长：低 5 天，中 10 天，高 15 天，特别风险 20 天。',
      inputs: '销售发票、发运单、银行流水。缺任何一项都会停下来等数据。',
      logic: [
        {
          name: '入账期间正确',
          detail: '发票在期末前入账，对应发运单的签收日却在期末之后，可能提前确认了收入。严重程度高。',
        },
        { name: '有服务完成记录', detail: '发票找不到对应的发运单，或发运单没有签收日。严重程度高。' },
        {
          name: '收款金额相符',
          detail: '按发票号在银行流水里找到的收款合计不等于发票金额。严重程度中。截至数据日还没收款的发票不作判断。',
        },
      ],
      result:
        '每处不符记为一条例外，本程序随即完成，例外不直接交给你。收入截止风险为中、高或特别时，由收入截止性判断（REV-02）逐张发票判断这些例外的原因；风险为低或只有发生风险时不执行 REV-02，例外留在本程序的记录里供你查看。',
      project: (p) => {
        const days = num(p, 'window_days')
        const end = str(p, 'period_end')
        if (days == null || end == null) return null
        return `检查期末前 ${days} 天，即 ${daysBefore(end, days)} 至 ${end} 入账的销售发票。`
      },
    },
    en: {
      purpose:
        'Checks every sales invoice posted shortly before period end by matching each invoice to its service completion record (signed delivery note) and to the bank receipt, to confirm the revenue occurred and was recorded in the right period. Assertions: occurrence and cut-off.',
      when: 'Runs whenever revenue carries an occurrence or cut-off risk, at any risk level. The higher the risk, the longer the window: 5 days for low, 10 for medium, 15 for high and 20 for significant risk.',
      inputs:
        'Sales invoices, delivery notes and bank transactions. If any of them is missing, the procedure waits for data.',
      logic: [
        {
          name: 'Recorded in the correct period',
          detail:
            'The invoice is posted before period end but its delivery note was signed after period end, so revenue may have been recognised early. High severity.',
        },
        {
          name: 'Service completion record exists',
          detail: 'The invoice has no delivery note, or the delivery note has no signed date. High severity.',
        },
        {
          name: 'Receipts agree to the invoice',
          detail:
            'The receipts found in the bank transactions by invoice number do not add up to the invoice amount. Medium severity. Invoices not yet paid at the data date are not assessed.',
        },
      ],
      result:
        'Each mismatch is recorded as an exception and the procedure completes; exceptions do not come to you directly. When revenue cut-off risk is medium, high or significant, Revenue cut-off (REV-02) assesses their cause invoice by invoice. Otherwise REV-02 does not run and the exceptions stay in this procedure’s record for you to read.',
      project: (p) => {
        const days = num(p, 'window_days')
        const end = str(p, 'period_end')
        if (days == null || end == null) return null
        return `Tests sales invoices posted in the ${days} days before period end, from ${daysBefore(end, days)} to ${end}.`
      },
    },
  },
  'CSH-04': {
    zh: {
      purpose:
        '向本项目每个银行账户的开户行发出询证函，询证期末余额，并存档银行回函，作为后续核对银行存款的外部证据。针对的认定：存在。',
      when: '货币资金有“存在”风险且为中、高或特别风险时执行。',
      inputs: '银行账户清单。没有时会停下来等数据。',
      logic: [
        { name: '发函范围', detail: '银行账户清单中的全部账户，每户一封询证函，询证期末余额。' },
        {
          name: '不重复发函',
          detail: '已经发过函的账户不再重发。账户的开户行、BSB 或账号变了，视为新账户重新发函，原来的函证留作历史。',
        },
        {
          name: '存档回函',
          detail:
            '回函存入证据库。银行直接寄给或发邮件给审计师的回函记为外部原件，经客户转交等其他渠道收到的记为外部副本。',
        },
        {
          name: '没有回函',
          detail: '目前只处理银行已回函的账户：任一账户还没有回函时，整次执行失败，交给你重新执行。',
        },
      ],
      result:
        '产出每个账户的函证记录和存档的回函，不作判断，也不产生例外，执行完即完成。之后由函证回函抽取（DOC-06）从回函中读出回函余额和应计利息，再由函证差异评价（CSH-02）与账面核对。',
      project: (p) => {
        const asOf = str(p, 'as_of')
        if (asOf == null) return null
        return `询证各账户 ${asOf} 的余额。`
      },
    },
    en: {
      purpose:
        "Sends a confirmation request to the bank of every bank account to confirm the period-end balance, and files the bank's reply as external evidence for the later cash procedures. Assertion: existence.",
      when: 'Runs when cash carries an existence risk at medium, high or significant level.',
      inputs: 'The list of bank accounts. If it is missing, the procedure waits for data.',
      logic: [
        {
          name: 'Accounts confirmed',
          detail: 'Every account on the bank account list gets one request, asking for the balance at period end.',
        },
        {
          name: 'No duplicate requests',
          detail:
            "An account already confirmed is not sent another request. If an account's bank, BSB or account number changes, it is treated as a new account and confirmed again; the old confirmation is kept as history.",
        },
        {
          name: 'Reply filed',
          detail:
            'The reply is filed with the evidence. A reply the bank posted or emailed directly to the auditor is treated as an external original; one received any other way, such as through the client, as an external copy.',
        },
        {
          name: 'No reply',
          detail:
            'For now only accounts the bank has replied for are handled. If any account has no reply yet, the whole run fails and comes to you to run again.',
        },
      ],
      result:
        'Produces a confirmation record for each account and the filed replies. No judgment is made and no exceptions are raised, so the procedure completes once it runs. Confirmation reply extraction (DOC-06) then reads the confirmed balance and accrued interest from each reply, and Confirmation differences (CSH-02) compares them with the books.',
      project: (p) => {
        const asOf = str(p, 'as_of')
        if (asOf == null) return null
        return `Confirms each account's balance at ${asOf}.`
      },
    },
  },
  'CSH-05': {
    zh: {
      purpose: '逐个银行账户核对期末对账单余额与总账余额是否一致，确认账面记录的银行存款确实存在。针对的认定：存在。',
      when: '货币资金有“存在”风险时执行，任何风险等级都会执行。',
      inputs: '银行账户清单、银行流水。缺任何一项都会停下来等数据。',
      logic: [
        {
          name: '对账单期末余额与总账一致',
          detail:
            '以每个账户期末当天或之前最后一笔银行流水后的余额作为对账单余额，与该账户总账科目的本期期末余额比较。两者不等，或期末前没有流水、试算表里没有该科目时，记为例外，金额为对账单减账面。严重程度中。',
        },
      ],
      result:
        '每个不符的账户记为一条例外。例外不直接交给你：函证差异评价（CSH-02）判断某个账户的回函差异时，会把该账户上的这类例外一并纳入判断；没有例外时本程序自动通过。',
      project: (p) => {
        const end = str(p, 'period_end')
        if (end == null) return null
        return `核对各账户 ${end} 的对账单余额与总账余额。`
      },
    },
    en: {
      purpose:
        "Agrees each bank account's period-end statement balance to its general ledger balance, to confirm that the cash recorded in the books exists. Assertion: existence.",
      when: 'Runs whenever cash carries an existence risk, at any risk level.',
      inputs:
        'The list of bank accounts and the bank transactions. If either is missing, the procedure waits for data.',
      logic: [
        {
          name: 'Statement balance agrees to the general ledger',
          detail:
            "The statement balance is the balance after the last bank transaction on or before period end, compared with the period-end balance of the account's general ledger account. A difference, no transactions up to period end, or no trial balance line for the ledger account is an exception, measured as statement minus book. Medium severity.",
        },
      ],
      result:
        'Each account that does not agree is recorded as an exception. Exceptions do not come to you directly: when Confirmation differences (CSH-02) assesses an account with a confirmation difference, it takes the exceptions on that account into the same judgment. With no exceptions the procedure closes automatically.',
      project: (p) => {
        const end = str(p, 'period_end')
        if (end == null) return null
        return `Compares each account's statement and general ledger balances at ${end}.`
      },
    },
  },
  'CSH-02': {
    zh: {
      purpose:
        '比较银行回函余额与总账余额，并检查回函列明的应计利息是否入账；对有问题的账户逐户判断差异能否由在途存款、未达付款等调节项解释，还是构成错报。针对的认定：存在。',
      when: '货币资金有“存在”风险且为中、高或特别风险时执行，在流水账面核对（CSH-05）和函证回函抽取（DOC-06）完成后进行。',
      inputs: '银行账户清单、银行流水。缺任何一项都会停下来等数据。',
      logic: [
        {
          name: '回函余额与账面一致',
          detail:
            '逐个账户比较回函余额与该账户总账科目的本期期末余额，不相等时记一条例外，金额为回函减账面，严重程度中。函证记录同时写上账面余额、差额以及是否相符。',
        },
        {
          name: '回函应计利息已入账',
          detail:
            '回函列明的应计利息绝对值超过明显微小错报临界值，而期末前窗口内全部科目的分录中找不到借方或贷方等于该金额的行时，记一条例外，严重程度中。互相冲回的凭证及其原凭证不计入。',
        },
        {
          name: '选出要判断的账户',
          detail:
            '有上述任一例外的账户每户判断一次，并带上流水账面核对（CSH-05）在该账户上的例外；回函与账面一致、应计利息也已入账的账户不作判断。',
        },
        {
          name: '模型读取的资料',
          detail:
            '回函（余额、截至日、应计利息、回函渠道及可靠性）、账面余额与差额、该账户期后的银行流水，以及期末前该银行科目的分录（不含互相冲回的凭证）。',
        },
        {
          name: '解释差异',
          detail:
            '回函余额须截至期末。先按金额、再按日期和参考号，把期末前已记账、期后才过银行的收款和付款匹配为在途存款或未达付款，看能否把差额解释到分；期后才发生、期末前没有记账的流水不算调节项。',
        },
        {
          name: '给出建议',
          detail:
            '截至日正确、差额被调节项完全解释且没有应计利息问题，建议关闭；账面有记账差错（如同一参考号的分录与银行流水金额不同、重复记账）或应计利息未入账，建议认定为错报；截至日不对，或差额解释不了又看不出差错，建议补充证据。',
        },
      ],
      result:
        '每个有问题的账户一条判断。函证差异须由人评价，所以每条判断都交给你复核；部分判断（如建议关闭的）会先由另一个模型独立复核，两者结论不一致时改由你裁决分歧。建议认定错报的附错报草稿：余额记账差错按货币资金高估或低估列示，应计利息未入账按利息收入或利息支出低估列示，同一账户只出一份；是否登记为错报由你决定。你确认判断后，该账户上的回函差异、应计利息和流水账面核对（CSH-05）的例外即视为已解释。模型没能给出有效判断时，该账户单独交给你：可以重新执行，也可以自己下结论或直接认定错报。',
      project: (p, currency = 'AUD') => {
        const end = str(p, 'period_end')
        const look = num(p, 'lookback_days')
        const after = num(p, 'subsequent_days')
        const trivial = num(p, 'clearly_trivial_cents')
        if (end == null || look == null || after == null || trivial == null) return null
        return `回函余额与 ${end} 的总账余额比较。候选调节项为期后 ${after} 天（${daysAfter(end, 1)} 至 ${daysAfter(end, after)}）的银行流水和期末前 ${look} 天（${daysBefore(end, look)} 至 ${end}）的银行科目分录。回函应计利息超过 ${money(trivial, currency)}（明显微小错报临界值）才检查是否入账。`
      },
    },
    en: {
      purpose:
        'Compares each confirmed bank balance with the general ledger and checks whether accrued interest stated on the reply has been booked. For each account with an issue, it assesses whether reconciling items such as deposits in transit and unpresented payments explain the difference, or whether there is a misstatement. Assertion: existence.',
      when: 'Runs when cash carries an existence risk at medium, high or significant level, after Bank statement to ledger (CSH-05) and Confirmation reply extraction (DOC-06) are complete.',
      inputs:
        'The list of bank accounts and the bank transactions. If either is missing, the procedure waits for data.',
      logic: [
        {
          name: 'Confirmed balance agrees to the books',
          detail:
            "For each account the confirmed balance is compared with the period-end balance of the account's general ledger account; a difference is an exception, measured as confirmed minus book. Medium severity. The confirmation record is updated with the book balance, the difference and whether they agree.",
        },
        {
          name: 'Accrued interest on the reply is booked',
          detail:
            'If the reply states accrued interest above the clearly trivial threshold and no journal line on any account in the window before period end has a debit or credit of that amount, it is an exception. Medium severity. Reversed entries and the entries reversing them are left out.',
        },
        {
          name: 'Accounts judged',
          detail:
            'Each account with either exception is judged once, together with the Bank statement to ledger (CSH-05) exceptions on that account. Accounts whose reply agrees and whose accrued interest is booked are not judged.',
        },
        {
          name: 'What the model reads',
          detail:
            "The reply (balance, balance date, accrued interest, how it was received and its reliability), the book balance and difference, the account's bank transactions after period end, and the entries on its bank ledger account just before period end, excluding reversed entries.",
        },
        {
          name: 'Explaining the difference',
          detail:
            'The confirmed balance must be at period end. Receipts and payments booked on or before period end that cleared the bank after it are matched as deposits in transit or unpresented payments, on amount first and then date and reference, to see whether they explain the difference to the cent. A bank transaction after period end with no entry on or before it is not a reconciling item.',
        },
        {
          name: 'Proposed action',
          detail:
            'Close when the balance date is right, reconciling items fully explain the difference and there is no accrued interest issue; misstatement when the books contain a recording error (such as an entry whose amount differs from the bank transaction with the same reference, or a duplicated entry) or the accrued interest is not booked; more evidence when the balance date is wrong, or part of the difference is unexplained and no error is shown.',
        },
      ],
      result:
        'Each account with an issue gets one judgment. Confirmation differences must be evaluated by a person, so every judgment comes to you for review; some, such as those proposing to close, are first reviewed independently by another model, and if the two disagree you resolve the disagreement. A proposed misstatement comes with a draft: a recording error as cash overstated or understated, unbooked accrued interest as interest income or interest expense understated, one draft per account. You decide whether to record it. Once you confirm a judgment, the confirmation difference, accrued interest and Bank statement to ledger (CSH-05) exceptions on that account count as explained. If the model cannot reach a valid judgment, the account comes to you separately: run it again, give your own conclusion or record a misstatement directly.',
      project: (p, currency = 'AUD') => {
        const end = str(p, 'period_end')
        const look = num(p, 'lookback_days')
        const after = num(p, 'subsequent_days')
        const trivial = num(p, 'clearly_trivial_cents')
        if (end == null || look == null || after == null || trivial == null) return null
        return `Compares confirmed balances with the general ledger at ${end}. Candidate reconciling items are bank transactions in the ${after} days after period end (${daysAfter(end, 1)} to ${daysAfter(end, after)}) and bank ledger entries in the ${look} days before it (${daysBefore(end, look)} to ${end}). Accrued interest on a reply is checked only above ${money(trivial, currency)}, the clearly trivial threshold.`
      },
    },
  },
  'REV-03': {
    zh: {
      purpose:
        '逐份阅读收入合同的条款，确定商品或服务的控制权何时转移给客户、由哪一条款决定，并找出退货权、返利等影响收入确认的条款，供收入截止性判断（REV-02）判断发票所属期间。针对的认定：发生、截止。',
      when: '收入有“发生”或“截止”风险且为中、高或特别风险时执行，在合同要素抽取（DOC-04）完成后进行。',
      logic: [
        { name: '选出合同', detail: '本项目的全部收入合同，每份判断一次。' },
        {
          name: '模型读取的资料',
          detail:
            '合同要素抽取（DOC-04）抽出的合同对方、金额、签订日和各条款（带所在页），以及合同文件的可靠性。只依据抽出的条款，需要而找不到的条款记为局限。',
        },
        {
          name: '控制权转移时点',
          detail:
            '确定控制权是在某一时点转移（如客户签收、验收、服务完成），还是随履约在一段时间内转移，并指出决定它的条款。明确写出控制权、所有权或风险转移事件的条款即为转移时点，除非有其他条款与之矛盾。',
        },
        {
          name: '其他影响收入确认的条款',
          detail: '找出退货权、返利、验收条件、开票持有安排、寄售、担保和关联方等条款，在结论中列明，不改变建议。',
        },
        {
          name: '给出建议',
          detail: '能从条款确定转移时点或随时间转移的方式，建议接受；条款定不了或互相矛盾，建议补充证据。',
        },
      ],
      result:
        '每份合同一条判断，全部交给你逐条复核；模型只会建议接受或补充证据，不会建议认定错报。你处理完全部合同后，收入截止性判断（REV-02）才开始执行，并以每份合同未被推翻的最新结论作参考，你推翻的以你的结论为准。模型没能对某份合同给出有效判断时，该合同单独交给你：可以重新执行，也可以自己给出结果。',
    },
    en: {
      purpose:
        'Reads the terms of each revenue contract to establish when control of the goods or services passes to the customer and which term sets it, and identifies other terms that affect revenue recognition, such as return rights and rebates. Revenue cut-off (REV-02) relies on this to decide the period of each invoice. Assertions: occurrence and cut-off.',
      when: 'Runs when revenue carries an occurrence or cut-off risk at medium, high or significant level, after Contract term extraction (DOC-04) is complete.',
      logic: [
        { name: 'Contracts assessed', detail: 'Every revenue contract of the engagement, one judgment each.' },
        {
          name: 'What the model reads',
          detail:
            'The counterparty, amount, signing date and terms (with page numbers) extracted by Contract term extraction (DOC-04), and the reliability of the contract document. Only the extracted terms are used; a term that is needed but missing is recorded as a limitation.',
        },
        {
          name: 'Point of control transfer',
          detail:
            'Determines whether control passes at a point in time (such as the customer signing for delivery, acceptance or completion of the service) or over time as the company performs, and names the term that sets it. A term naming the event on which control, title or risk passes sets the point unless another term contradicts it.',
        },
        {
          name: 'Other relevant terms',
          detail:
            'Identifies return rights, rebates, acceptance conditions, bill-and-hold, consignment, guarantees and related-party terms and names them in the conclusion; they do not change the proposed action.',
        },
        {
          name: 'Proposed action',
          detail:
            'Accept when the terms determine the transfer point or how control transfers over time; more evidence when they do not, or when terms conflict.',
        },
      ],
      result:
        'Each contract gets one judgment, and every judgment comes to you for review. The model only proposes accept or more evidence, never a misstatement. Revenue cut-off (REV-02) starts only after you have dealt with all contracts, and uses the latest conclusion on each contract that has not been overridden; where you override, your conclusion is used. If the model cannot reach a valid judgment on a contract, it comes to you separately: run it again or enter the result yourself.',
    },
  },
  'REV-02': {
    zh: {
      purpose:
        '对期末前入账的大额销售发票，以及凭证三单匹配（REV-08）发现问题的发票，逐张判断收入是否记在控制权转移给客户的期间，识别提前确认的收入。针对的认定：发生、截止。',
      when: '收入有“截止”风险且为中、高或特别风险时执行，在收入合同条款评价（REV-03）和凭证三单匹配（REV-08）完成后进行。风险越高，检查的窗口越长：中 10 天，高 15 天，特别风险 20 天。金额门槛为实际执行的重要性。',
      inputs: '销售发票。没有时会停下来等数据。',
      logic: [
        {
          name: '选出要判断的发票',
          detail:
            '规则选出期末前窗口内入账、金额不低于实际执行的重要性的销售发票，再加上凭证三单匹配（REV-08）发现例外而尚未解释的发票，每张发票判断一次。',
        },
        {
          name: '模型读取的资料',
          detail:
            '发票、发运单的发货日与签收日、该发票入账分录的全部行（带科目名称与类别）、所属合同及其条款、收入合同条款评价（REV-03）对该合同的结论，以及该发票上的三单匹配例外。合同评价结论只作参考，引用时以合同条款原文为准。',
        },
        {
          name: '确定控制权转移日',
          detail:
            '按合同的控制权转移条款确定转移事件，没有合同时按发运或服务完成记录确定；以签收、验收或服务完成为准的取签收日，以发货为准的取发货日。条款与合同评价结论不一致时以条款原文为准，并记为局限。',
        },
        {
          name: '判断归属期间',
          detail:
            '收入属于转移日所在的期间；随时间转移的服务按服务期间落在各期的部分判断。每条三单匹配例外都要在推理中回应，收款不符本身不构成截止错报。',
        },
        {
          name: '给出建议',
          detail:
            '转移日在期末或之前，建议接受；期末前入账而转移日在期末之后，建议认定为错报；缺发运或服务完成记录、缺转移日期、条款定不了转移时点，或一张发票涉及跨期的交付或服务，建议补充证据。',
        },
      ],
      result:
        '每张发票一条判断，全部交给你逐条复核。建议认定错报的附错报草稿：收入高估，金额取该发票分录中收入科目的金额（不含 GST），没有收入行时取发票金额；是否登记为错报由你决定，登记的错报由错报累计（FIN-04）汇总。你确认判断后，该发票上的三单匹配例外即视为已解释。模型没能对某张发票给出有效判断时，该发票单独交给你：可以重新执行，也可以自己给出结论。',
      project: (p, currency = 'AUD') => {
        const days = num(p, 'window_days')
        const end = str(p, 'period_end')
        const threshold = num(p, 'threshold_cents')
        if (days == null || end == null || threshold == null) return null
        return `检查期末前 ${days} 天，即 ${daysBefore(end, days)} 至 ${end} 入账、金额不低于 ${money(threshold, currency)}（实际执行的重要性）的销售发票，另加三单匹配例外所指的发票。`
      },
    },
    en: {
      purpose:
        'Assesses, invoice by invoice, whether large sales invoices posted shortly before period end, and invoices flagged by Three-way match (REV-08), are recorded in the period in which control passed to the customer, to detect revenue recognised early. Assertions: occurrence and cut-off.',
      when: 'Runs when revenue carries a cut-off risk at medium, high or significant level, after Revenue contract terms (REV-03) and Three-way match (REV-08) are complete. The higher the risk, the longer the window: 10 days for medium, 15 for high and 20 for significant risk. The amount threshold is performance materiality.',
      inputs: 'Sales invoices. If they are missing, the procedure waits for data.',
      logic: [
        {
          name: 'Invoices selected',
          detail:
            'A rule selects sales invoices posted in the window before period end at or above performance materiality, and adds invoices behind Three-way match (REV-08) exceptions not yet explained. Each invoice is judged once.',
        },
        {
          name: 'What the model reads',
          detail:
            "The invoice, the shipped and signed dates on its delivery notes, every line of the invoice's journal entry with account names and categories, the contract and its terms, the conclusion of Revenue contract terms (REV-03) on that contract, and the three-way match exceptions on the invoice. The contract conclusion is guidance only; the contract term itself is cited.",
        },
        {
          name: 'Transfer date',
          detail:
            "The transfer event comes from the contract's control transfer term, or from the delivery or service completion record when there is no contract. Transfer on signature, acceptance or completion takes the signed date; transfer on shipment takes the shipped date. Where the term and the contract conclusion differ, the term prevails and a limitation is recorded.",
        },
        {
          name: 'Period the revenue belongs to',
          detail:
            'Revenue belongs to the period in which the transfer date falls; for a service transferred over time, the part of the service period in each period is assessed. Every three-way match exception must be addressed, but a receipt mismatch is not a cut-off misstatement on its own.',
        },
        {
          name: 'Proposed action',
          detail:
            'Accept when the transfer date is on or before period end; misstatement when the invoice is posted on or before period end but the transfer date is after it; more evidence when there is no delivery or service completion record, the transfer date is missing, the terms do not set the transfer point, or the invoice covers deliveries or services in different periods.',
        },
      ],
      result:
        "Each invoice gets one judgment, and every judgment comes to you for review. A proposed misstatement comes with a draft: revenue overstated, for the amount of the revenue line in the invoice's journal entry excluding GST, or the invoice amount when there is no such line. You decide whether to record it; recorded misstatements are totalled by Misstatement accumulation (FIN-04). Once you confirm a judgment, the three-way match exceptions on that invoice count as explained. If the model cannot reach a valid judgment on an invoice, it comes to you separately: run it again or give your own conclusion.",
      project: (p, currency = 'AUD') => {
        const days = num(p, 'window_days')
        const end = str(p, 'period_end')
        const threshold = num(p, 'threshold_cents')
        if (days == null || end == null || threshold == null) return null
        return `Tests sales invoices posted in the ${days} days before period end, from ${daysBefore(end, days)} to ${end}, at or above ${money(threshold, currency)} (performance materiality), plus invoices behind three-way match exceptions.`
      },
    },
  },
  'GEN-05': {
    zh: {
      purpose:
        '对本期入账的全部分录逐笔按规则扫描，找出不寻常、可能被用来凌驾于控制之上的分录，例如周末录入的大额收入、整数金额的手工调整、恰低于审批限额的付款、缺少独立审批的分录和疑似重复开票。规则只标出分录，不作判断。针对的风险：管理层凌驾于控制之上；认定：发生、完整性、准确性。',
      when: '属于“分录测试”功能，打开时每个项目都执行，扫描业务日期在本期内的全部分录。客户按错报沟通载入更正分录后，会再单独扫描一次这些更正凭证。看录入时间的四项检查（周末录入的重大收入、审计调整期间的其他分录、非工作时间录入、期末后补记）在数据集声明录入时间不可靠时不执行。',
      logic: [
        {
          name: '周末录入的重大收入',
          detail: '收入科目的贷方金额超过实际执行的重要性，且录入时间（当地时间）在周六或周日。严重程度高。',
        },
        {
          name: '整数金额的调整分录',
          detail:
            '手工调整分录中，借方或贷方金额是整数（10,000.00 的倍数）且不低于实际执行的重要性；作废并冲回的分录不算。严重程度中。',
        },
        {
          name: '恰低于审批限额的付款',
          detail:
            '贷记货币资金的付款凭证中，对方科目的借方金额不低于审批限额的 95%、但低于限额。客户画像没有审批限额时无法执行。严重程度高。',
        },
        {
          name: '审批缺失或自审自批',
          detail: '分录没有审批人，或审批人就是录入人。本期分录都没有审批人记录时无法执行。严重程度高。',
        },
        {
          name: '期末前三日冲销',
          detail: '业务日期在期末前三天至期末之间，摘要或单据号含“冲销”或 reverse、reversal 等字样。严重程度高。',
        },
        {
          name: '审计调整期间的其他分录',
          detail:
            '首次向管理层发出错报沟通之后录入、业务日期在本期内的分录，审计更正分录除外。发出错报沟通之前不执行，所以实际只在再次扫描更正凭证时执行，并查这段时间录入的全部其他分录。严重程度高。',
        },
        {
          name: '非常规录入人',
          detail: '本期录入凭证不超过 2 笔的录入人，其录入的凭证全部标出。本期录入人少于两个时无法执行。严重程度中。',
        },
        { name: '非工作时间录入', detail: '录入时间（当地时间）在早 7 点之前或晚 8 点及以后。严重程度中。' },
        {
          name: '非标准分录',
          detail: '手工录入的调整分录，作废并冲回的分录和审计更正分录除外，金额超过明显微小错报临界值。严重程度高。',
        },
        {
          name: '收入全额冲减',
          detail:
            '借记收入、贷记应收且不涉及货币资金的凭证，冲减的应收金额正好等于本期此前开具的某张发票的全额。只能按金额匹配，同额发票全部列出，由分录例外归因（GEN-02）判断冲的是哪张。金额超过明显微小错报临界值。严重程度中。',
        },
        {
          name: '作废后重记且对方名称改变',
          detail:
            '期内一笔凭证被作废冲回后，同一天有一笔科目和金额完全相同的重记，但对方名称不同（有同名重记的视为正常更正）。数据集没有声明作废冲回的写法时不执行。金额超过明显微小错报临界值。严重程度中。',
        },
        {
          name: '疑似重复开票',
          detail:
            '同一客户、同一开票日、同一金额的本期发票有两张以上，其中没有发运或服务完成记录的每张标出。本期发票都没有发运或服务完成记录时无法执行。金额超过明显微小错报临界值。严重程度高。',
        },
        {
          name: '期末后补记',
          detail: '业务日期在本期内，录入日（当地时间）却在期末之后，审计更正分录除外。严重程度中。',
        },
      ],
      result:
        '每处命中记为一条例外，同一检查每笔凭证只记一条（疑似重复开票按发票记）。例外不直接交给你，先由分录例外归因（GEN-02）按凭证逐笔判断原因，本程序本身不建复核任务。无法执行或不执行的检查会在执行记录里写明原因。',
      project: (p, currency = 'AUD') => {
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        if (start == null || end == null) return null
        const entries = Array.isArray(p.entry_nos) ? p.entry_nos.length : 0
        const parts = [entries > 0 ? `只扫描更正通知载入的 ${entries} 笔凭证` : `扫描 ${start} 至 ${end} 的分录`]
        const performance = num(p, 'performance_materiality_cents')
        if (performance != null) parts.push(`实际执行的重要性 ${money(performance, currency)}`)
        const trivial = num(p, 'clearly_trivial_cents')
        if (trivial != null) parts.push(`明显微小错报临界值 ${money(trivial, currency)}`)
        const limit = num(p, 'approval_limit_cents')
        if (limit != null) parts.push(`审批限额 ${money(limit, currency)}`)
        else if (p.approval_limit_cents === null) parts.push('客户画像没有审批限额')
        const unit = num(p, 'round_unit_cents')
        if (unit != null) parts.push(`整数金额单位 ${money(unit, currency)}`)
        const hours = p.business_hours
        if (Array.isArray(hours) && typeof hours[0] === 'number' && typeof hours[1] === 'number') {
          const zone = str(p, 'timezone')
          parts.push(`工作时间 ${hours[0]}:00 至 ${hours[1]}:00${zone ? `（${zone}）` : ''}`)
        }
        const rare = num(p, 'rare_user_max')
        if (rare != null) parts.push(`录入不超过 ${rare} 笔凭证的录入人视为非常规`)
        const note =
          p.entry_timestamps_reliable === false ? '数据集声明分录录入时间不可靠，看录入时间的四项检查不执行。' : ''
        return `${parts.join('；')}。${note}`
      },
    },
    en: {
      purpose:
        'Scans every journal entry posted in the period against a set of rules to find unusual entries that could be used to override controls, such as large revenue entered at a weekend, round-sum manual adjustments, payments just below the approval limit, entries without independent approval and possible duplicate invoices. The rules only flag entries and make no judgment. Risk addressed: management override of controls. Assertions: occurrence, completeness and accuracy.',
      when: 'Part of the "Journal entry testing" feature; when the feature is on, it runs on every engagement and scans all entries dated in the period. After the client posts correcting entries in response to the misstatement communication, those correcting entries are scanned again on their own. The four checks that rely on entry times (large revenue entered at a weekend, other entries during the audit adjustment period, out-of-hours entries and entries made after period end) do not run when the dataset declares entry times unreliable.',
      logic: [
        {
          name: 'Large revenue entered at a weekend',
          detail:
            'A credit to a revenue account above performance materiality, entered on a Saturday or Sunday (local time). High severity.',
        },
        {
          name: 'Round-sum adjusting entries',
          detail:
            'A manual adjusting entry with a debit or credit that is a round sum (a multiple of 10,000.00) and not below performance materiality; reversed and reversing pairs are excluded. Medium severity.',
        },
        {
          name: 'Payments just below the approval limit',
          detail:
            'A payment crediting cash whose other side is a debit of at least 95% of the approval limit but below it. Not executable when the client profile has no approval limit. High severity.',
        },
        {
          name: 'Missing or self approval',
          detail:
            'The entry has no approver, or was approved by the person who entered it. Not executable when no entry in the period records an approver. High severity.',
        },
        {
          name: 'Reversals in the last three days',
          detail:
            'An entry dated from three days before period end to period end whose description or source document mentions a reversal. High severity.',
        },
        {
          name: 'Other entries during the audit adjustment period',
          detail:
            'An entry dated in the period but entered after the first misstatement communication to management, other than the auditor’s correcting entries. It does not run before that communication, so in practice it runs only when the correcting entries are rescanned, and then covers every other entry made in that time. High severity.',
        },
        {
          name: 'Infrequent preparers',
          detail:
            'All entries of a preparer who entered no more than 2 entries in the period. Not executable when fewer than two people entered entries. Medium severity.',
        },
        {
          name: 'Out-of-hours entries',
          detail: 'Entered before 7 am or from 8 pm onwards (local time). Medium severity.',
        },
        {
          name: 'Non-standard entries',
          detail:
            'A manual adjusting entry above the clearly trivial threshold, excluding reversed and reversing pairs and the auditor’s correcting entries. High severity.',
        },
        {
          name: 'Revenue credited in full',
          detail:
            'An entry that debits revenue and credits receivables without touching cash, where the receivable credited equals the full amount of an invoice issued earlier in the period. Only amounts can be matched, so every invoice of that amount is listed for journal entry exception attribution (GEN-02) to identify. Above the clearly trivial threshold. Medium severity.',
        },
        {
          name: 'Voided and reposted under a different name',
          detail:
            'An entry voided by reversal in the period is reposted on the same date with identical accounts and amounts but a different counterparty name (a repost under the same name is treated as a normal correction). Does not run when the dataset does not declare how reversals are referenced. Above the clearly trivial threshold. Medium severity.',
        },
        {
          name: 'Possible duplicate invoices',
          detail:
            'Two or more invoices in the period to the same customer, on the same date, for the same amount; each one without a delivery or service completion record is flagged. Not executable when no invoice in the period has such a record. Above the clearly trivial threshold. High severity.',
        },
        {
          name: 'Entries made after period end',
          detail:
            'Dated in the period but entered after period end (local date), other than the auditor’s correcting entries. Medium severity.',
        },
      ],
      result:
        'Each hit is recorded as an exception, at most one per check per entry (possible duplicate invoices are recorded per invoice). Exceptions do not come to you directly: journal entry exception attribution (GEN-02) first assesses the cause entry by entry, and this procedure creates no review tasks itself. Checks that could not or did not run are recorded in the run record with the reason.',
      project: (p, currency = 'AUD') => {
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        if (start == null || end == null) return null
        const entries = Array.isArray(p.entry_nos) ? p.entry_nos.length : 0
        const parts = [
          entries > 0
            ? `Scans only the ${entries} entries loaded with the correction notice`
            : `Scans entries dated ${start} to ${end}`,
        ]
        const performance = num(p, 'performance_materiality_cents')
        if (performance != null) parts.push(`performance materiality ${money(performance, currency)}`)
        const trivial = num(p, 'clearly_trivial_cents')
        if (trivial != null) parts.push(`clearly trivial threshold ${money(trivial, currency)}`)
        const limit = num(p, 'approval_limit_cents')
        if (limit != null) parts.push(`approval limit ${money(limit, currency)}`)
        else if (p.approval_limit_cents === null) parts.push('no approval limit in the client profile')
        const unit = num(p, 'round_unit_cents')
        if (unit != null) parts.push(`round-sum unit ${money(unit, currency)}`)
        const hours = p.business_hours
        if (Array.isArray(hours) && typeof hours[0] === 'number' && typeof hours[1] === 'number') {
          const zone = str(p, 'timezone')
          parts.push(`business hours ${hours[0]}:00 to ${hours[1]}:00${zone ? ` (${zone})` : ''}`)
        }
        const rare = num(p, 'rare_user_max')
        if (rare != null) parts.push(`preparers with no more than ${rare} entries count as infrequent`)
        const note =
          p.entry_timestamps_reliable === false
            ? ' The dataset declares entry times unreliable, so the four checks that rely on them do not run.'
            : ''
        return `${parts.join('; ')}.${note}`
      },
    },
  },
  'GEN-02': {
    zh: {
      purpose:
        '对全量分录扫描（GEN-05）标出的分录，逐笔凭证查明原因：分录是否恰当、有没有依据，是否造成错报，还是需要更多证据。针对的风险：管理层凌驾于控制之上；认定：发生、完整性、准确性。',
      when: '属于“分录测试”功能，打开时每个项目都执行，在全量分录扫描（GEN-05）之后执行。只研判 GEN-05 尚未归因的例外，没有这样的例外时不调用模型。',
      logic: [
        {
          name: '选判断对象',
          detail: '把全量分录扫描（GEN-05）尚未归因的例外按凭证归组，每笔凭证判断一次。',
        },
        {
          name: '模型读的资料',
          detail:
            '本凭证的全部分录行、涉及科目的本期和上期余额；凭证涉及货币资金时另给凭证日前后 3 天的银行对账单行；例外指向的其他凭证（原凭证、冲回、重记、贷项凭证）、发票及其发运或服务完成记录和合同、与本凭证关联的文件，以及计划阶段识别的舞弊风险。',
        },
        {
          name: '判断原因与依据',
          detail:
            '逐条说明规则标出的问题能否解释；看分录的性质（是否手工或非标准，是否冲回、重记或冲减另一笔），以及有没有金额、日期、对方都一致的原始依据。重记时对方名称改变要有解释，只按金额匹配的冲减要认出冲的是哪张发票、冲得是否恰当。',
        },
        {
          name: '考虑舞弊风险',
          detail: '考虑这笔分录能否被用来凌驾于控制之上，或错报舞弊风险涉及的科目。缺少解释只算局限，不等于舞弊。',
        },
        {
          name: '提出建议',
          detail:
            '建议接受（问题已解释且有依据，或已被有依据的分录全额冲回）、认定为错报（附错报草稿：科目、认定、方向和金额，记在无依据确认的收入、费用或资产科目上，不含 GST），或补充证据（写明缺哪一项资料）。',
        },
      ],
      result:
        '每笔凭证一条判断，全部交给你逐条复核，不经独立复核。你可以确认、推翻或认定为错报；确认后，这条判断解释的例外标为已归因。模型没能给出有效判断时，这笔凭证的例外标为未归因交给你，可以重新执行，也可以由你直接给出结论或认定为错报。',
      project: (p) => {
        const days = num(p, 'bank_window_days')
        if (days == null) return null
        return `凭证涉及货币资金时，取凭证日前后各 ${days} 天（含两端）的银行对账单行。`
      },
    },
    en: {
      purpose:
        'Investigates the entries flagged by the full journal entry scan (GEN-05), one entry at a time: whether each entry is appropriate and supported, whether it misstates the financial report, or whether more evidence is needed. Risk addressed: management override of controls. Assertions: occurrence, completeness and accuracy.',
      when: 'Part of the "Journal entry testing" feature; when the feature is on, it runs on every engagement after the full journal entry scan (GEN-05). It assesses only GEN-05 exceptions not yet explained; with none, no model is called.',
      logic: [
        {
          name: 'Choose what to assess',
          detail:
            'Groups the unexplained exceptions from the full journal entry scan (GEN-05) by entry and assesses each entry once.',
        },
        {
          name: 'What the model reads',
          detail:
            'All lines of the entry and the current and prior period balances of its accounts; for an entry that touches cash, the bank statement lines from 3 days before to 3 days after its date; the other entries the exceptions point to (originals, reversals, reposts and credits), the invoices with their delivery or service completion records and contracts, documents linked to the entry, and the fraud risks identified at planning.',
        },
        {
          name: 'Cause and support',
          detail:
            'Says whether each flagged issue is explained, considers the nature of the entry (manual or non-standard, or a reversal, repost or credit of another entry), and looks for a source record that agrees on amount, date and counterparty. A repost under a changed counterparty name needs an explanation, and a credit matched only by amount must be tied to the invoice it cancels and judged appropriate or not.',
        },
        {
          name: 'Fraud risks',
          detail:
            'Considers whether the entry could be used to override controls or to misstate an account a fraud risk concerns. A missing explanation is treated as a limitation, not as proof of fraud.',
        },
        {
          name: 'Proposed outcome',
          detail:
            'Accept (explained and supported, or reversed in full by a supported entry), misstatement (with a draft giving account, assertion, direction and amount, recorded on the revenue, expense or asset recognised without basis and excluding GST), or more evidence (naming the missing item).',
        },
      ],
      result:
        'One judgment per entry, and every judgment comes to you for review; they skip independent review. You can confirm, override or record a misstatement; on confirmation, the exceptions the judgment explains are marked as attributed. If the model cannot produce a valid judgment, the entry’s exceptions are marked unattributed and come to you: run it again, give your own conclusion or record a misstatement.',
      project: (p) => {
        const days = num(p, 'bank_window_days')
        if (days == null) return null
        return `For entries that touch cash, bank statement lines from ${days} days before to ${days} days after the entry date (inclusive) are included.`
      },
    },
  },
  'GEN-03': {
    zh: {
      purpose:
        '对股权穿透发现的每一家公司，判断它是不是被审计单位的关联方、管理层是否已申报，以及本期和期后与它的交易及条款，以识别未申报的关联方关系和交易。针对的认定：完整性。',
      when: '属于“关联方”功能，打开时每个项目都执行，在股权穿透与关联方关系图（GEN-08）和合同要素抽取（DOC-04）之后执行。往来看本期及期末后 30 天。',
      logic: [
        { name: '选判断对象', detail: '关系图中被审计单位以外的每一家公司，各判断一次。' },
        {
          name: '找出往来',
          detail:
            '按公司名称（去掉 Pty Ltd 等后缀）在本期及期后的分录、银行流水和销售发票中找与它的往来，发票另按 ABN 匹配，作废冲回的分录不算；这家公司直接持有被审计单位股份时，另取同期权益科目的分录。',
        },
        {
          name: '模型读的资料',
          detail:
            '登记摘录中这家公司的持股和任职关系、相关人员与被审计单位的关系（不含出生日期）、管理层申报清单中对应的一项、找到的往来、与它的合同和关联文件，以及关系图对它标出的例外。',
        },
        {
          name: '判断关联关系与交易',
          detail:
            '按客户适用的财务报告框架对关联方的定义，看是否存在控制、共同控制、重大影响或共同的关键管理人员；与管理层申报比对，未申报的作为新识别的关联方；说明期内和期后交易的金额、日期和条款，是否属于正常经营。',
        },
        {
          name: '提出建议',
          detail:
            '建议接受（不是关联方，或已申报且交易与申报相符），或补充证据（未申报的关联方、关系无法确定，或交易及条款需要进一步取证）。本程序不提出错报，关联方的会计处理和披露在之后评价。',
        },
      ],
      result:
        '每家公司一条判断，全部交给你逐条复核，不经独立复核。你可以确认、推翻或认定为错报；确认后，关系图对这家公司标出的例外标为已归因。模型没能完成某家公司的判断时，建一条执行失败任务，可以重新执行，也可以由你人工给出结果。',
      project: (p) => {
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        const days = num(p, 'subsequent_days')
        if (start == null || end == null || days == null) return null
        return `往来与权益分录取 ${start} 至 ${daysAfter(end, days)}，即本期加期末后 ${days} 天。`
      },
    },
    en: {
      purpose:
        'For each company found through the ownership search, decides whether it is a related party of the audited entity, whether management declared it, and what transactions with it occurred in the period and afterwards and on what terms, so that undisclosed related party relationships and transactions are identified. Assertion: completeness.',
      when: 'Part of the "Related parties" feature; when the feature is on, it runs on every engagement after the ownership and related party map (GEN-08) and contract term extraction (DOC-04). Dealings are taken for the period and the 30 days after period end.',
      logic: [
        {
          name: 'Choose what to assess',
          detail: 'Every company on the map other than the audited entity, assessed one at a time.',
        },
        {
          name: 'Find dealings',
          detail:
            'Searches journal entries, bank transactions and sales invoices in and after the period for the company’s name, ignoring suffixes such as Pty Ltd; invoices also match on ABN, and reversed pairs are excluded. If the company holds shares in the audited entity directly, equity entries for the same window are added.',
        },
        {
          name: 'What the model reads',
          detail:
            'The company’s ownership and office links from the registry extracts, the linked people’s relationships with the audited entity (without dates of birth), the matching item in management’s declaration, the dealings found, contracts and linked documents, and the exceptions the map raised on the company.',
        },
        {
          name: 'Relationship and transactions',
          detail:
            'Applies the related party definition of the client’s financial reporting framework: control, joint control, significant influence or key management personnel in common. Compares with management’s declaration, treating an undeclared related party as newly identified, and describes the amounts, dates and terms of transactions in and after the period and whether they are in the normal course of business.',
        },
        {
          name: 'Proposed outcome',
          detail:
            'Accept (not a related party, or declared with transactions consistent with the declaration) or more evidence (an undeclared related party, a relationship that cannot be established, or transactions or terms that need further evidence). It never proposes a misstatement; accounting and disclosure for related parties are evaluated later.',
        },
      ],
      result:
        'One judgment per company, and every judgment comes to you for review; they skip independent review. You can confirm, override or record a misstatement; on confirmation, the exceptions the map raised on that company are marked as attributed. If the model fails on a company, a failed task is created: run it again or enter the result yourself.',
      project: (p) => {
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        const days = num(p, 'subsequent_days')
        if (start == null || end == null || days == null) return null
        return `Dealings and equity entries are taken from ${start} to ${daysAfter(end, days)}: the period plus ${days} days after period end.`
      },
    },
  },
  'GEN-08': {
    zh: {
      purpose:
        '通过 ABN 和 ASIC 登记，向上穿透被审计单位的股东，并检索董事和秘书在其他公司的任职与持股，画出股权与关联方关系图，再与管理层申报的关联方清单比对，找出未申报的关联方候选。针对的认定：完整性。',
      when: '属于“关联方”功能，打开时每个项目都在外勤阶段执行，不依赖其他程序，不调用模型。你上传或修改 ASIC 摘录后，本程序过期，重新执行时出关系图的下一版。',
      logic: [
        {
          name: '确定被审计单位',
          detail:
            '用客户画像的 ABN 在 ABN Lookup 查到 ACN，再取被审计单位的 ASIC 公司摘录。查不到或客户不是公司（没有 ACN）时本程序执行失败。',
        },
        {
          name: '向上穿透股东',
          detail:
            '逐层取持股不低于 20% 的法人股东（控制或重大影响）的摘录，至多 3 层。持股更低的法人股东只按摘录列入关系图，不再往上查。',
        },
        {
          name: '检索人员',
          detail:
            '对被审计单位和母公司链上出生日期已知的董事和秘书做人员检索，把他们任职或持股的其他公司也取得摘录、列入关系图；只检索一轮，不再检索新公司里的人员。',
        },
        {
          name: '关联候选未申报',
          detail:
            '关系图中被审计单位以外的公司不在管理层申报的关联方或股东清单中（按 ACN 比对，没有 ACN 时按名称比对）。严重程度高。',
        },
        {
          name: '穿透中断',
          detail:
            '发现的公司查不到摘录、没有 ACN 或超过穿透层数，或高管因为没有人员检索来源而未能检索（如新建的客户）。这些公司照样列入关系图，但不再往下查。严重程度中。',
        },
      ],
      result:
        '产出股权与关联方关系图（每次重新执行出新的一版），查到的登记摘录存档作为证据。本程序不建复核任务，两类例外交关联方图谱研判（GEN-03）逐家公司判断。已成功的登记查询会留存，重新执行时只补查没查成的。整次执行失败时建失败任务，由你决定重新执行。',
      project: (p) => {
        const share = num(p, 'significant_pct')
        const depth = num(p, 'max_depth')
        if (share == null || depth == null) return null
        const hops = num(p, 'person_hops')
        return `向上穿透持股不低于 ${pct(share)} 的法人股东，至多 ${depth} 层${hops != null ? `；人员检索 ${hops} 轮` : ''}。`
      },
    },
    en: {
      purpose:
        'Uses the ABN and ASIC registers to trace the audited entity’s shareholders upwards and to find the other companies where its directors and secretaries hold office or shares, builds an ownership and related party map, and compares it with management’s declared related parties to find undeclared related party candidates. Assertion: completeness.',
      when: 'Part of the "Related parties" feature; when the feature is on, it runs during fieldwork on every engagement, depends on no other procedure and calls no model. When you upload or change an ASIC extract, the procedure becomes stale and rerunning it produces the next version of the map.',
      logic: [
        {
          name: 'Identify the audited entity',
          detail:
            'Looks up the client profile’s ABN in ABN Lookup to get the ACN, then obtains the entity’s ASIC company extract. If the ABN is not found or the client is not a company (no ACN), the procedure fails.',
        },
        {
          name: 'Trace shareholders upwards',
          detail:
            'Obtains, level by level, the extracts of corporate shareholders holding at least 20% (control or significant influence), up to 3 levels. Corporate shareholders with smaller holdings go on the map from the extract but are not traced further.',
        },
        {
          name: 'Search people',
          detail:
            'Runs a person search on the directors and secretaries of the entity and its parent chain whose dates of birth are known, and adds the other companies where they hold office or shares, with their extracts. There is one round only: people in the newly found companies are not searched.',
        },
        {
          name: 'Undeclared related party candidate',
          detail:
            'A company on the map, other than the audited entity, is not in management’s declared related parties or shareholders (matched by ACN, or by name where there is no ACN). High severity.',
        },
        {
          name: 'Trace interrupted',
          detail:
            'A company found has no extract, has no ACN or lies beyond the level limit, or its officers could not be searched because there is no person search source (as for a newly created client). Such companies still go on the map but are not traced further. Medium severity.',
        },
      ],
      result:
        'Produces the ownership and related party map (a new version on each rerun) and files the registry extracts as evidence. The procedure creates no review tasks: both kinds of exception go to related party analysis (GEN-03), which assesses each company. Successful register queries are kept, so a rerun repeats only those that did not succeed. If the whole run fails, a failed task lets you run it again.',
      project: (p) => {
        const share = num(p, 'significant_pct')
        const depth = num(p, 'max_depth')
        if (share == null || depth == null) return null
        const hops = num(p, 'person_hops')
        const rounds = hops == null ? '' : `; ${hops === 1 ? 'one round' : `${hops} rounds`} of person search`
        return `Traces corporate shareholders holding at least ${pct(share)}, up to ${depth} levels${rounds}.`
      },
    },
  },
  'GEN-06': {
    zh: {
      purpose:
        '对期后监控（GEN-07）标出的每一笔期后流水或分录，判断它是否构成或揭示期后事项：需要调整的、只需披露的，还是与期末状况无关的新期间常规交易；需要调整而期末未反映的，提出错报。针对的认定：完整性、截止、计价。',
      when: '属于“持续经营”功能，打开时每个项目都执行，在期后监控（GEN-07）之后执行。只研判 GEN-07 尚未归因的例外，没有这样的例外时不调用模型。',
      logic: [
        {
          name: '选判断对象',
          detail: '期后监控（GEN-07）标出、尚未归因的每一笔银行流水或分录行，各判断一次。',
        },
        {
          name: '找相关凭证',
          detail:
            '用流水的参考号（含数字、至少 4 位的词）和对方名称，或分录的单据号，在本期起至期后窗口截止入账的总账中找相关凭证，并带出相关的销售发票及其发运或服务完成记录；作废冲回的分录不算。',
        },
        {
          name: '模型读的资料',
          detail:
            '标出的这一行（分录另给整笔凭证）、找到的相关凭证和发票、涉及科目的期末余额、关联的已登记文件，以及实际执行的重要性和明显微小错报临界值。',
        },
        {
          name: '判断事项类型',
          detail:
            '把交付或义务发生、开票、入账、协议和收付款的日期与期末日比较：反映期末已存在情况的是调整事项，期末后才出现的情况是非调整事项，与期末状况无关的是常规交易。调整事项看期末余额是否已反映，非调整事项看是否重大到需要披露。',
        },
        {
          name: '提出建议',
          detail:
            '建议接受（常规交易、非调整事项，或已在期末反映的调整事项）、认定为错报（期末未反映的调整事项，附错报草稿），或补充证据（看不出是什么交易，或无法确定期末时情况是否已存在）。',
        },
      ],
      result:
        '每笔流水或分录行一条判断。建议接受的先经判断独立复核（GEN-09），复核同意即自动关闭，所解释的例外标为已归因。建议认定为错报或补充证据的、独立复核不同意的（作为分歧交你裁决）、复核没能完成的，交给你复核。打开“自主度”功能时，金额超过实际执行的重要性、置信度偏低、推理有未获证据支撑的步骤，或缺少银行、凭证证据的判断也交给你。模型没能给出有效判断时，该对象的例外标为未归因交给你。',
      project: (p) => {
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        const days = num(p, 'subsequent_days')
        if (start == null || end == null || days == null) return null
        const full = daysAfter(end, days)
        const asOf = str(p, 'data_as_of')
        const to = asOf != null && asOf < full ? asOf : full
        const cut = to === full ? '' : '（截到数据截止日）'
        return `研判 ${daysAfter(end, 1)} 至 ${to} 的期后流水与分录${cut}；相关凭证与发票在 ${start} 至 ${to} 入账的范围内找。`
      },
    },
    en: {
      purpose:
        'For each subsequent bank transaction or journal line flagged by subsequent events monitoring (GEN-07), decides whether it is or reveals an event after the reporting period that requires adjustment, one that requires only disclosure, or an ordinary transaction of the new period; an adjusting event not reflected at period end is proposed as a misstatement. Assertions: completeness, cut-off and valuation.',
      when: 'Part of the "Going concern" feature; when the feature is on, it runs on every engagement after subsequent events monitoring (GEN-07). It assesses only GEN-07 exceptions not yet explained; with none, no model is called.',
      logic: [
        {
          name: 'Choose what to assess',
          detail:
            'Each bank transaction or journal line flagged by subsequent events monitoring (GEN-07) and not yet explained, assessed one at a time.',
        },
        {
          name: 'Find related records',
          detail:
            'Uses the transaction’s reference numbers (words of at least 4 characters containing a digit) and counterparty name, or the entry’s source document number, to find related entries posted from the start of the period to the end of the subsequent window, with the related sales invoices and their delivery or service completion records; reversed pairs are excluded.',
        },
        {
          name: 'What the model reads',
          detail:
            'The flagged line (with the whole entry for a journal line), the related entries and invoices found, the period-end balances of the accounts involved, linked registered documents, and performance materiality and the clearly trivial threshold.',
        },
        {
          name: 'Type of event',
          detail:
            'Compares the dates of delivery or obligation, invoicing, posting, agreements and cash movement with period end: conditions that existed at period end make an adjusting event, conditions arising afterwards a non-adjusting event, and anything unrelated to the period-end position an ordinary transaction. For an adjusting event it checks whether the period-end balances reflect it; for a non-adjusting event, whether it is material enough to disclose.',
        },
        {
          name: 'Proposed outcome',
          detail:
            'Accept (an ordinary transaction, a non-adjusting event, or an adjusting event already reflected), misstatement (an adjusting event not reflected, with a draft), or more evidence (what the item is, or whether the condition existed at period end, cannot be told).',
        },
      ],
      result:
        'One judgment per line. Judgments proposing to accept go to independent review of judgments (GEN-09) first; if the review agrees, they close automatically and the exceptions they explain are marked as attributed. Judgments proposing a misstatement or more evidence, those the review disagrees with (sent to you as a disagreement to settle) and those the review could not complete come to you. With the "Autonomy" feature on, judgments above performance materiality, with low confidence, with unsupported reasoning steps or missing bank or ledger evidence also come to you. If the model cannot produce a valid judgment, the line’s exceptions are marked unattributed and come to you.',
      project: (p) => {
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        const days = num(p, 'subsequent_days')
        if (start == null || end == null || days == null) return null
        const full = daysAfter(end, days)
        const asOf = str(p, 'data_as_of')
        const to = asOf != null && asOf < full ? asOf : full
        const cut = to === full ? '' : ' (cut off at the data date)'
        return `Assesses subsequent bank transactions and entries from ${daysAfter(end, 1)} to ${to}${cut}; related entries and invoices are searched among those posted from ${start} to ${to}.`
      },
    },
  },
  'GEN-07': {
    zh: {
      purpose:
        '扫描期末后一段时间内的银行流水和分录，找出可能反映期后事项的大额未入账流水、非常规分录和资本性支出，作为期后事项的候选。规则只标出，不作判断。针对的认定：完整性。',
      when: '属于“持续经营”功能，打开时每个项目都执行，不调用模型。检查期末次日起 31 天内的银行流水和分录；数据只到更早的某一天（数据截止日）时，截到那天。',
      logic: [
        {
          name: '期后未入账的大额流水',
          detail:
            '窗口内金额（不论收付）超过重要性水平的银行流水，在流水日前后 10 天内，该账户对应的总账银行科目里找不到同方向、同金额的分录。严重程度中。',
        },
        {
          name: '期后非常规分录',
          detail:
            '窗口内的凭证，排除涉及银行科目的、常规赊销和赊购过账，以及各科目净额都为零的；取其中资产或负债科目金额最大的一行，超过明显微小错报临界值即标出。严重程度高。',
        },
        {
          name: '期后资本性支出',
          detail: '窗口内借记不动产、厂场和设备科目，单行借方超过重要性水平的分录行。严重程度中。',
        },
      ],
      result:
        '每处命中记为一条例外，作废并冲回的分录不计入。例外作为期后事项候选，不直接交给你，先由期后事项研判（GEN-06）逐笔判断；本程序本身不建复核任务。',
      project: (p, currency = 'AUD') => {
        const end = str(p, 'period_end')
        const days = num(p, 'subsequent_days')
        if (end == null || days == null) return null
        const full = daysAfter(end, days)
        const asOf = str(p, 'data_as_of')
        const to = asOf != null && asOf < full ? asOf : full
        const cut = to === full ? '' : '（截到数据截止日）'
        const parts = [`检查 ${daysAfter(end, 1)} 至 ${to} 的流水与分录${cut}`]
        const materiality = num(p, 'materiality_cents')
        if (materiality != null) parts.push(`大额流水与资本性支出的门槛为重要性水平 ${money(materiality, currency)}`)
        const trivial = num(p, 'clearly_trivial_cents')
        if (trivial != null) parts.push(`非常规分录的门槛为明显微小错报临界值 ${money(trivial, currency)}`)
        const match = num(p, 'match_days')
        if (match != null) parts.push(`在流水日前后 ${match} 天内找同额分录`)
        return `${parts.join('；')}。`
      },
    },
    en: {
      purpose:
        'Scans bank transactions and journal entries in a period after period end for large unrecorded receipts or payments, unusual entries and capital expenditure that may point to subsequent events, as candidates for evaluation. The rules only flag items and make no judgment. Assertion: completeness.',
      when: 'Part of the "Going concern" feature; when the feature is on, it runs on every engagement and calls no model. It covers bank transactions and entries in the 31 days after period end, cut off at the data date when the data only runs to an earlier day.',
      logic: [
        {
          name: 'Large unrecorded bank transactions',
          detail:
            'A bank transaction in the window above overall materiality (receipt or payment) with no ledger line of the same direction and amount on that account’s bank ledger account within 10 days either side. Medium severity.',
        },
        {
          name: 'Unusual entries after period end',
          detail:
            'Entries in the window, excluding those touching a bank account, routine credit sales and credit purchases, and entries that net to zero on every account; the largest asset or liability line of each is flagged if above the clearly trivial threshold. High severity.',
        },
        {
          name: 'Capital expenditure after period end',
          detail:
            'A debit to a property, plant and equipment account in the window above overall materiality. Medium severity.',
        },
      ],
      result:
        'Each hit is recorded as an exception; reversed and reversing pairs are left out. The exceptions are candidate subsequent events and do not come to you directly: subsequent events evaluation (GEN-06) first assesses each one, and this procedure creates no review tasks itself.',
      project: (p, currency = 'AUD') => {
        const end = str(p, 'period_end')
        const days = num(p, 'subsequent_days')
        if (end == null || days == null) return null
        const full = daysAfter(end, days)
        const asOf = str(p, 'data_as_of')
        const to = asOf != null && asOf < full ? asOf : full
        const cut = to === full ? '' : ' (cut off at the data date)'
        const parts = [`Covers bank transactions and entries from ${daysAfter(end, 1)} to ${to}${cut}`]
        const materiality = num(p, 'materiality_cents')
        if (materiality != null) {
          parts.push(
            `large transactions and capital expenditure are tested against overall materiality of ${money(materiality, currency)}`,
          )
        }
        const trivial = num(p, 'clearly_trivial_cents')
        if (trivial != null)
          parts.push(`unusual entries against the clearly trivial threshold of ${money(trivial, currency)}`)
        const match = num(p, 'match_days')
        if (match != null) parts.push(`matching ledger lines are sought within ${match} days of the bank date`)
        return `${parts.join('; ')}.`
      },
    },
  },
  'GEN-09': {
    zh: {
      purpose:
        '模型判断自动关闭之前，由另一个复核模型只看证据、不看原结论，独立再判断一次，然后与原结论比对，以发现原判断的错误。复核用单独的模型和指令，减少与原判断犯同样错误的可能。',
      when: '不单独安排执行：判断程序每生成一条模型判断，随即复核。属于“独立复核”功能，默认开启；关闭或没有配置复核模型时，判断记为未复核。自主度为“逐条决定”或“逐条复核”的程序由你逐条看，不经独立复核；现有程序中自主度为“仅复核异常”的判断程序是函证差异评价（CSH-02）和期后事项研判（GEN-06）。',
      logic: [
        {
          name: '选哪些判断',
          detail:
            '按顺序看，命中即复核：建议接受或关闭、会自动关闭的判断；推理含假设步骤的判断；其余按判断编号随机抽 5%，重放时结果不变。打开“智能体协作”功能时，另复核证据全为内部副本或推理有未获证据支撑步骤的判断。',
        },
        {
          name: '第一轮：独立结论',
          detail:
            '复核模型只拿到原判断引用的证据内容（数据行、文件页面、准则原文、客户画像字段），看不到原结论、推理链和建议，独立得出结论和推理链，并通过与原判断相同的校验。人员出生日期不发给模型。',
        },
        {
          name: '第二轮：比对',
          detail:
            '再给出原结论和建议，判断两者是否一致：措辞和详略不同不算，发现的问题、金额、期间或处理不同即为不一致，并说明是证据不足、得出不同结论还是推理有误。',
        },
      ],
      result:
        '复核结果写在被复核的判断上，不单独出底稿。一致且独立推理链通过校验时，原判断的置信度计入复核一项，再按原程序的规则处理（例如期后事项研判建议接受的自动关闭）。不一致时建分歧任务交你裁决：维持原结论、采用独立复核的结论（独立推理链未通过校验时不提供），或都不采用、由你给出结论。复核没能完成（证据都解析不了、校验未通过、出错，或判为一致但独立推理链未通过校验）时，这条判断交给你复核。打开“智能体协作”功能时，还会记下复核方的建议和错报草稿，裁决时按所采用一方的建议决定是否要同时登记错报。',
    },
    en: {
      purpose:
        'Before a model judgment can close automatically, a separate review model looks at the evidence alone, without seeing the original conclusion, reaches its own conclusion and then compares the two, to catch errors in the original judgment. The review uses a different model and different instructions to reduce the chance of repeating the same mistake.',
      when: 'Not scheduled on its own: each time a judgment procedure produces a model judgment, the judgment is reviewed straight away. Part of the "Independent review" feature, which is on by default; when it is off or no review model is configured, judgments are recorded as not reviewed. Procedures whose autonomy is "Decide each" or "Review each" come to you item by item and skip independent review; among the current judgment procedures, those at "Review exceptions" are confirmation differences (CSH-02) and subsequent events evaluation (GEN-06).',
      logic: [
        {
          name: 'Which judgments are reviewed',
          detail:
            'Checked in order, and the first match triggers a review: a judgment that proposes to accept or close and would therefore close automatically; reasoning that includes an assumption step; otherwise a random 5% sample by judgment ID, which gives the same result on replay. With the "Agent collaboration" feature on, judgments relying only on internal copies or with unsupported reasoning steps are also reviewed.',
        },
        {
          name: 'First round: own conclusion',
          detail:
            'The review model receives only the evidence the judgment cites (data rows, document pages, standard text, client profile fields), not its conclusion, reasoning or proposal, and reaches its own conclusion and reasoning, which must pass the same checks as the original. Dates of birth are not sent to the model.',
        },
        {
          name: 'Second round: comparison',
          detail:
            'Given the original conclusion and proposal, it decides whether the two agree: differences in wording or detail do not count, but a different finding, amount, period or treatment does, and the reason is given as insufficient evidence, a different conclusion or a reasoning error.',
        },
      ],
      result:
        'The result is written on the reviewed judgment; there is no separate workpaper. If the two agree and the independent reasoning passes the checks, the review counts towards the judgment’s confidence and the judgment follows its own procedure’s rules (for example, an accept from subsequent events evaluation closes automatically). If they disagree, a disagreement task asks you to keep the original conclusion, adopt the independent review’s conclusion (not offered when its reasoning failed the checks) or adopt neither and give your own. If the review cannot be completed (no cited evidence could be resolved, a check failed, an error occurred, or it agreed but its reasoning failed the checks), the judgment comes to you for review. With "Agent collaboration" on, the reviewer’s proposal and misstatement draft are also kept, and whether a misstatement must be recorded with your ruling follows the proposal of the side you adopt.',
    },
  },
  'FIN-03': {
    zh: {
      purpose:
        '判断审计中认定的每条错报的性质：属于事实错报还是判断错报，有没有舞弊迹象；同时把它归入对应的审计目标，并记明期间来源。分类结果随错报进入与管理层的沟通，并在未更正错报评价（FIN-01）时呈报给你。',
      when: '进入完成阶段时执行，即执行阶段的程序全部结束、复核任务都已处理之后。属于“错报闭环”功能，打开时才执行。只处理还没分类的已认定错报，每条调用一次模型；完成阶段又认定了新错报（例如在更正验证中）时会回来再分类，没有待分类的错报时不调用模型。',
      logic: [
        {
          name: '归入审计目标',
          detail: '按规则选科目名完整出现在错报科目中、且认定相同的审计目标；找不到时留空。这一项不经模型。',
        },
        {
          name: '期间来源',
          detail: '一律记为本期发生：本系统按首次审计处理，没有上期结转的未更正错报。这一项不经模型。',
        },
        {
          name: '模型读取的资料',
          detail:
            '错报本身、认定它的那条判断的结论与证据、来源凭证或单据对应的分录行、该判断引用的其他记录与文件，以及风险清单中的舞弊风险。',
        },
        {
          name: '错报类型',
          detail:
            '金额由已入账交易、单据、回函或法定、合同费率确定，没有疑问的，为事实错报；源于管理层的会计估计、确认、计量、列报或会计政策选择不合理的，为判断错报。本系统不做审计抽样，不判推断错报。',
        },
        {
          name: '舞弊迹象',
          detail:
            '资料显示可能出于故意（隐瞒交易、缺乏商业理由、抵销另一笔错报，或与风险清单中的舞弊风险相关）时，标为有舞弊迹象，并必须写明理由。遗漏常规调整、计算错误或公开说明的分录本身不算；手工分录未经独立复核这类控制缺陷，只在结论中作为控制缺陷线索提及。',
        },
      ],
      result:
        '每条错报出一条分类判断，全部进复核队列由你逐条复核。确认后，错报记上类型、舞弊迹象及理由、审计目标和期间来源，状态仍为“已认定”，转入错报沟通与回复记录（FIN-08）；不同意时可以推翻，自己给出类型和是否有舞弊迹象（理由选填）。模型发现另有尚未认定的错报时会建议认定为错报；判断失败的可以重跑，或由你直接给出分类。',
    },
    en: {
      purpose:
        'Assesses the nature of each misstatement identified during the audit: whether it is factual or judgmental and whether it indicates possible fraud. It also links the misstatement to its audit objective and records the period it arose in. The classification travels with the misstatement into the communication with management and is presented to you at evaluation of uncorrected misstatements (FIN-01).',
      when: 'Runs on entering the completion stage, once all fieldwork procedures have finished and their review tasks are resolved. Part of the "Misstatement lifecycle" feature; runs only when it is switched on. Only misstatements not yet classified are covered, with one model call each; if new misstatements are recorded later in completion (for example during correction verification), it runs again for them. With nothing to classify, the model is not called.',
      logic: [
        {
          name: 'Audit objective',
          detail:
            "A rule picks the audit objective whose account name appears as a whole word in the misstatement's account and whose assertion is the same; if none matches, it is left blank. The model is not involved.",
        },
        {
          name: 'Period of origin',
          detail:
            'Always recorded as the current period: the system treats the audit as a first-year audit with no uncorrected misstatements carried forward. The model is not involved.',
        },
        {
          name: 'What the model reads',
          detail:
            'The misstatement, the conclusion and evidence of the judgment it was recognised from, the journal lines of the source voucher or document, other records and documents cited by that judgment, and the fraud risks in the risk register.',
        },
        {
          name: 'Type',
          detail:
            "Factual when the amount follows without doubt from recorded transactions, documents, confirmations or rates set by law or contract; judgmental when it arises from management's estimates, recognition, measurement, presentation or choice of accounting policies that are unreasonable. The system does not use audit sampling, so no misstatement is classified as projected.",
        },
        {
          name: 'Indication of fraud',
          detail:
            'Flagged when the inputs suggest intent, such as concealing a transaction, lacking a business rationale, offsetting another misstatement or a link to a fraud risk in the register, and a reason must then be given. A missed routine adjustment, a calculation error or an openly described entry is not enough on its own; a control weakness such as a manual journal posted without independent review is only noted in the conclusion as a lead on a control deficiency.',
        },
      ],
      result:
        'Each misstatement gets one classification judgment, and every one goes to the review queue for you to review. Once confirmed, the misstatement carries its type, fraud flag and reason, audit objective and period of origin; its status stays Identified and it moves on to misstatement communication (FIN-08). If you disagree you can override and give the type and fraud flag yourself (the reason is optional). If the model finds a further misstatement not yet recorded, it proposes recording it; failed judgments can be run again, or you can enter the classification directly.',
    },
  },
  'FIN-08': {
    zh: {
      purpose:
        '把已分类的错报提交管理层，请其更正，不更正的说明理由，并逐条登记管理层的回复。管理层同意更正的错报转入等待更正，拒绝的记为未更正，并保留回复原文。',
      when: '在错报性质分类（FIN-03）的结果都经你确认之后执行；更正验证（FIN-09）发现更正分录对不上时，会为这些错报再执行一次。属于“错报闭环”功能，打开时才执行。没有待沟通的错报时不建任务，直接进入下一步。',
      logic: [
        {
          name: '沟通材料',
          detail:
            '系统列出每条待沟通错报的科目、认定、金额、多计或少计、类型、舞弊迹象和来源判断的结论，并附一段更正请求：请管理层更正下列错报，不更正的说明理由。',
        },
        {
          name: '逐条登记回复',
          detail:
            '材料中的每条错报登记一行，不能多也不能少：沟通时间、沟通对象（人与职务）、回复时间、回复原文、是否同意更正。另填执行日期，不要求上传证据文件。',
        },
        { name: '时间核对', detail: '回复时间不能早于沟通时间，两者都不能晚于提交时刻。' },
        {
          name: '同意更正',
          detail:
            '错报转为“等待更正”。之后在工作区的“等待客户更正”里：客户过账了调整分录，就勾选错报和更正分录提交，系统载入分录后做更正验证（FIN-09）；不再等的，选“停止等待”，所选错报记为未更正。',
        },
        {
          name: '拒绝更正',
          detail: '错报记为“未更正”，依据为管理层拒绝更正，回复原文随错报保留，直接计入错报累计（FIN-04）。',
        },
      ],
      result:
        '本程序是人工程序，任务在“人工程序”页登记。每条错报按回复出新版本，沟通时间、对象和回复原文都记在错报上，可在“错报与审计意见”页查看。有同意更正的错报时，流程停在等待客户更正；都拒绝时进入错报累计（FIN-04）。',
    },
    en: {
      purpose:
        "Puts the classified misstatements to management, asks them to correct them or give reasons for not doing so, and records management's response to each. Misstatements management agrees to correct move on to await correction; those it refuses are recorded as uncorrected, with the response kept verbatim.",
      when: 'Runs once you have confirmed the results of misstatement classification (FIN-03), and runs again for misstatements whose adjustments did not match in correction verification (FIN-09). Part of the "Misstatement lifecycle" feature; runs only when it is switched on. With no misstatements to communicate, no task is created and the flow moves on.',
      logic: [
        {
          name: 'Material for the discussion',
          detail:
            'The system lists each misstatement to be communicated with its account, assertion, amount, direction, type, fraud flag and the conclusion of its source judgment, together with a correction request: please correct the misstatements below and give reasons for any you do not correct.',
        },
        {
          name: 'One row per misstatement',
          detail:
            'Record exactly one row for every misstatement in the material: when and with whom (name and position) it was discussed, when management replied, the reply verbatim, and whether management agrees to correct it. Also give the date performed; no evidence files are required.',
        },
        {
          name: 'Date checks',
          detail:
            'The reply cannot be dated before the discussion, and neither can be later than the moment you submit.',
        },
        {
          name: 'Agrees to correct',
          detail:
            'The misstatement moves to Awaiting correction. Later, in Waiting for client correction in the workspace, either pick the misstatements and the adjustment entries the client has posted, so the system loads them and verifies the correction (FIN-09), or choose Stop waiting to record the picked misstatements as uncorrected.',
        },
        {
          name: 'Refuses to correct',
          detail:
            'The misstatement is recorded as Uncorrected because management refused to correct it, keeps the reply verbatim and goes straight into misstatement accumulation (FIN-04).',
        },
      ],
      result:
        'This is a manual procedure: you record it on the Manual procedures page. Each misstatement gets a new version carrying the discussion date, the person and the reply, shown on the Misstatements and opinion page. If any misstatement is to be corrected, the flow stops to wait for the client to correct it; if all were refused, it moves on to misstatement accumulation (FIN-04).',
    },
  },
  'FIN-09': {
    zh: {
      purpose:
        '管理层按要求过账审计更正分录后，核对这些分录是否把错报完整更正、方向和金额是否正确，以及对其他科目的影响是否恰当。验证结果决定错报记为已更正、部分更正，还是要与管理层重新沟通。',
      when: '你在工作区的“等待客户更正”中选“客户已过账调整分录”，勾选错报和更正分录提交后执行：系统先载入这些分录，所选错报转为“验证更正中”。属于“错报闭环”功能，打开时才执行。“分录测试”功能也打开时，先由全量分录扫描（GEN-05）对新载入的分录做一次增量扫描，扫出的问题一并交本程序判断。',
      logic: [
        { name: '取更正分录', detail: '取凭证号以约定前缀开头的全部分录行，作为审计更正分录。' },
        {
          name: '匹配错报',
          detail:
            '在更正分录中找注明了错报来源单据（来源判断所针对的凭证号、发票号等）的行：错报有科目代码时取同一科目的行；没有科目代码时取借方或贷方正好等于错报金额的一行，匹配到即视为全额更正。',
        },
        {
          name: '计算已更正额',
          detail:
            '资产、费用类科目，多计的错报以贷方为更正、少计以借方为更正；负债、权益、收入类科目相反。更正方向的金额减去反方向的金额即已更正额，错报金额减已更正额即剩余额。',
        },
        {
          name: '分录对不上',
          detail:
            '没有匹配行、更正方向做反，或更正金额超过错报金额时，记一条例外，严重程度中，验证结果为须与管理层重新沟通。',
        },
        {
          name: '模型判断',
          detail:
            '模型读错报、来源判断的结论、匹配凭证的全部行和规则算出的金额，判断是否完整更正（须与剩余额为零一致），并列出更正分录还涉及的其他科目，说明影响是否恰当，例如冲回原分录的其他行或相关税额。',
        },
        {
          name: '更正造成新错报',
          detail: '更正分录本身使某个科目出现错报（包括更正超额）时，模型建议认定为新的错报。',
        },
      ],
      result:
        '每条错报出一条验证判断，全部进复核队列由你逐条复核。确认后：剩余额为零的记为“已更正”；部分更正的记为“部分更正”，剩余额计入错报累计（FIN-04）；对不上的回到“等待更正”并标明须重新沟通，由错报沟通与回复记录（FIN-08）再沟通。不同意时可以推翻，自己给出验证结果；你认定了新错报的，先回到错报性质分类（FIN-03）。',
      project: (p) => {
        const prefix = str(p, 'adjustment_prefix')
        if (prefix == null) return null
        return `本项目的审计更正分录按凭证号前缀“${prefix}”识别。`
      },
    },
    en: {
      purpose:
        'After management posts the audit adjustments you asked for, checks whether they correct each misstatement in full, in the right direction and amount, and whether their effect on other accounts is appropriate. The result decides whether the misstatement is recorded as corrected, partially corrected, or has to be raised with management again.',
      when: 'Runs after you choose Client has posted adjustments in Waiting for client correction in the workspace and submit the misstatements and adjustment entries you picked: the system first loads those entries and the misstatements move to Verifying correction. Part of the "Misstatement lifecycle" feature; runs only when it is switched on. If the "Journal entry testing" feature is also on, the full journal entry scan (GEN-05) first scans the newly loaded entries, and anything it flags is judged together in this procedure.',
      logic: [
        {
          name: 'Adjustment entries',
          detail: 'Takes all journal lines whose entry number starts with the agreed prefix as the audit adjustments.',
        },
        {
          name: 'Matching to the misstatement',
          detail:
            "Looks for adjustment lines that cite the misstatement's source document (the voucher, invoice or other item the source judgment was about). If the misstatement has an account code, lines on that account are taken; if not, the one line whose debit or credit equals the misstatement amount is taken, and a match counts as a full correction.",
        },
        {
          name: 'Amount corrected',
          detail:
            'For asset and expense accounts an overstatement is corrected by credits and an understatement by debits; for liability, equity and income accounts the reverse. Postings in the correcting direction less postings in the other direction give the amount corrected, and the misstatement amount less that gives the residual.',
        },
        {
          name: 'Adjustments do not match',
          detail:
            'If no line matches, the correction goes the wrong way, or it exceeds the misstatement amount, an exception of medium severity is raised and the verification result is to communicate with management again.',
        },
        {
          name: 'Model judgment',
          detail:
            "The model reads the misstatement, the source judgment's conclusion, all lines of the matched entries and the amounts worked out by the rules. It decides whether the correction is complete (which must agree with a zero residual) and lists the other accounts the adjustments touch, saying whether each effect is appropriate, such as reversing the other lines of the original entry or the related tax.",
        },
        {
          name: 'New misstatement from the correction',
          detail:
            'If the adjustments themselves misstate an account, including by over-correcting, the model proposes recording a new misstatement.',
        },
      ],
      result:
        'Each misstatement gets one verification judgment, and every one goes to the review queue for you to review. Once confirmed, a zero residual makes it Corrected; a partial correction makes it Partially corrected and the residual is accumulated in misstatement accumulation (FIN-04); a mismatch sends it back to Awaiting correction, marked for another round of misstatement communication (FIN-08). If you disagree you can override and give the verification result yourself; any new misstatement you record goes back to misstatement classification (FIN-03) first.',
      project: (p) => {
        const prefix = str(p, 'adjustment_prefix')
        if (prefix == null) return null
        return `Audit adjustments in this engagement are the journal entries whose entry number starts with “${prefix}”.`
      },
    },
  },
  'FIN-04': {
    zh: {
      purpose:
        '把本项目的错报汇总成未更正错报汇总表：逐条算出仍未更正的金额，剔除明显微小的，合计后与重要性水平比较，给出候选审计意见。汇总表是未更正错报评价（FIN-01）的呈报材料。',
      when: '每个项目都执行。“错报闭环”功能打开时，在更正验证（FIN-09）之后、错报都已结清（已更正、部分更正或未更正）时执行，完成阶段每轮回流都会重做；关闭时，执行阶段的程序全部结束后直接执行，错报一律按未更正累计。未更正错报评价（FIN-01）退回时也会重做，出汇总表的新一版。',
      logic: [
        { name: '逐条取未更正金额', detail: '未更正的错报取全额，部分更正的取剩余额，已更正的只列出、不累计。' },
        {
          name: '剔除明显微小',
          detail:
            '未更正金额超过明显微小错报临界值的计入汇总表，带方向和期间来源；等于或低于临界值的单独列出，不累计。',
        },
        { name: '合计', detail: '把计入的金额直接相加，多计和少计不相互抵销。' },
        {
          name: '候选意见',
          detail:
            '合计达到或超过重要性水平时候选保留意见，否则候选无保留意见。候选只是建议，最终意见类型由你在 FIN-01 决定。',
        },
        {
          name: '错报未结清',
          detail:
            '还有已认定、等待更正或验证更正中的错报时，本程序不出汇总表，按执行失败交你处理。正常流程下错报到这一步都已结清。',
        },
      ],
      result:
        '产出未更正错报汇总表的新一版：计入的错报、剔除的明显微小错报、已更正错报、合计、重要性水平和候选意见，可在“错报与审计意见”页查看。本程序自动完成，不建复核任务；执行失败时复核队列会出现“程序未能完成”的任务，可以重跑。汇总表随后呈报到未更正错报评价（FIN-01）。',
      project: (p, currency = 'AUD') => {
        const materiality = num(p, 'materiality_cents')
        const trivial = num(p, 'clearly_trivial_cents')
        if (materiality == null || trivial == null) return null
        return `重要性水平 ${money(materiality, currency)}，明显微小错报临界值 ${money(trivial, currency)}：未更正金额超过 ${money(trivial, currency)} 的错报计入汇总表，合计达到 ${money(materiality, currency)} 时候选保留意见。`
      },
    },
    en: {
      purpose:
        'Compiles the schedule of uncorrected misstatements: works out the amount still uncorrected for each misstatement, leaves out clearly trivial ones, totals the rest and compares the total with overall materiality to give a candidate opinion. The schedule is what you evaluate at evaluation of uncorrected misstatements (FIN-01).',
      when: 'Runs on every engagement. With the misstatement lifecycle feature on, it runs after correction verification (FIN-09) once every misstatement is settled (corrected, partially corrected or uncorrected), and again on each later pass through completion; with it off, it runs as soon as all fieldwork procedures have finished and every misstatement counts as uncorrected. It is also redone, as a new version of the schedule, when FIN-01 is sent back.',
      logic: [
        {
          name: 'Amount still uncorrected',
          detail:
            'Uncorrected misstatements count in full and partially corrected ones at their residual; corrected misstatements are listed but not accumulated.',
        },
        {
          name: 'Clearly trivial',
          detail:
            'Amounts above the clearly trivial threshold go on the schedule with their direction and period of origin; amounts at or below it are listed separately and not accumulated.',
        },
        {
          name: 'Total',
          detail:
            'Amounts on the schedule are simply added together; overstatements and understatements do not offset.',
        },
        {
          name: 'Candidate opinion',
          detail:
            'A total at or above overall materiality gives a candidate qualified opinion, otherwise unqualified. The candidate is only a suggestion; you decide the opinion type at FIN-01.',
        },
        {
          name: 'Unsettled misstatements',
          detail:
            'If any misstatement is still identified, awaiting correction or being verified, no schedule is produced and the run fails over to you. In the normal flow every misstatement is settled by this point.',
        },
      ],
      result:
        'Produces a new version of the schedule: misstatements accumulated, clearly trivial ones left out, corrected ones, the total, overall materiality and the candidate opinion, shown on the Misstatements and opinion page. The procedure closes automatically without review tasks; if it fails, a Procedure did not complete task appears in the review queue and can be run again. The schedule then goes to evaluation of uncorrected misstatements (FIN-01).',
      project: (p, currency = 'AUD') => {
        const materiality = num(p, 'materiality_cents')
        const trivial = num(p, 'clearly_trivial_cents')
        if (materiality == null || trivial == null) return null
        return `Overall materiality ${money(materiality, currency)} and clearly trivial threshold ${money(trivial, currency)}: uncorrected amounts above ${money(trivial, currency)} go on the schedule, and a total of ${money(materiality, currency)} or more gives a candidate qualified opinion.`
      },
    },
  },
  'FIN-01': {
    zh: {
      purpose: '你对照重要性水平评价未更正错报汇总表，确定审计报告的意见类型。系统给出的候选意见只作参考，可以改选。',
      when: '错报累计（FIN-04）出汇总表后停在这里等你决定，每个项目都执行。',
      logic: [
        {
          name: '呈报材料',
          detail:
            '候选意见、未更正错报合计、重要性水平，以及每条错报的状态、类型、金额、舞弊迹象、管理层回复和未更正的依据；另列因无数据未执行的程序及原因。',
        },
        {
          name: '数据已更新的程序',
          detail:
            '程序执行之后数据有改动的，材料中会提示；可到审计程序页用新数据重跑，重跑完重新累计错报后再回到这里。',
        },
        {
          name: '确认意见',
          detail:
            '选定意见类型，只能选无保留意见或保留意见，可以与候选不同（否定意见和无法表示意见本版不支持）。确认后进入审计报告阶段，按所选意见起草报告（RPT-03）。',
        },
        {
          name: '退回',
          detail: '重新累计错报（FIN-04），出汇总表的新一版后再呈报；已做的分类、沟通和更正验证不重做。',
        },
        { name: '信息不足', detail: '暂不作出决定，停在这里，材料重新呈报。' },
        { name: '理由', detail: '各项决定的理由都是选填；确认意见时填的理由会提供给报告起草参考。' },
      ],
      result:
        '确认后，所选意见类型记为本项目的审计意见，项目进入审计报告阶段，由报告文本生成（RPT-03）起草报告。在工作区或“待决定”页作出决定；汇总表和错报也可在“错报与审计意见”页查看。',
    },
    en: {
      purpose:
        'You evaluate the schedule of uncorrected misstatements against overall materiality and decide the opinion type for the audit report. The candidate opinion from the system is only a reference and you may choose otherwise.',
      when: 'Stops here for your decision once misstatement accumulation (FIN-04) has produced the schedule. Applies to every engagement.',
      logic: [
        {
          name: 'What you see',
          detail:
            'The candidate opinion, total uncorrected misstatements and overall materiality, and for each misstatement its status, type, amount, fraud flag, management response and why it is uncorrected; procedures not performed for lack of data are listed with the reasons.',
        },
        {
          name: 'Out-of-date procedures',
          detail:
            'If data has changed since a procedure ran, the material says so; you can rerun it with the new data from the Audit procedures page, and once it has run the misstatements are accumulated again and presented here.',
        },
        {
          name: 'Confirm opinion',
          detail:
            'Choose the opinion type, unqualified or qualified, which may differ from the candidate (adverse and disclaimer opinions are not supported in this version). The engagement then moves to reporting and the report is drafted on that basis (RPT-03).',
        },
        {
          name: 'Send back',
          detail:
            'Misstatement accumulation (FIN-04) is redone and the new version of the schedule is presented again; classification, communication and correction verification already done are not repeated.',
        },
        {
          name: 'Need more information',
          detail: 'No decision is made; the engagement stays here and the material is presented again.',
        },
        {
          name: 'Reason',
          detail:
            'A reason is optional for every decision; a reason given when confirming the opinion is passed to report drafting.',
        },
      ],
      result:
        "Once confirmed, the chosen opinion type becomes the engagement's audit opinion, the engagement moves to reporting and report drafting (RPT-03) prepares the report. Decide in the workspace or from Pending decisions; the schedule and misstatements are also on the Misstatements and opinion page.",
    },
  },
  'RPT-03': {
    zh: {
      purpose:
        '按未更正错报评价（FIN-01）确定的意见类型起草审计报告。模型只写“审计意见”和“形成审计意见的基础”两段，其余段落（董事的责任、审计师的责任、其他法律法规要求、签字）取固定模板，填入公司名称、期末日和项目负责人姓名。',
      when: '在未更正错报评价（FIN-01）确认意见后执行，每个项目都执行。审计报告批准与签字（RPT-01）附批注退回后，按批注再执行一次，出下一稿；在 FIN-01 或 RPT-01 处用新数据重跑过期程序、重新确认意见后，也会重拟一稿。报告用执行时的工作语言起草。',
      logic: [
        {
          name: '模型读取的资料',
          detail:
            '公司名称、意见类型、你在 FIN-01 填的理由（如有）。保留意见时还读汇总表中的未更正错报，以及认定这些错报所依据的已确认判断。',
        },
        {
          name: '审计意见段',
          detail:
            '首行为标题；第一段说明审计了公司的财务报表及其组成；第二段按意见类型写意见，保留意见写成“除……的影响外”。',
        },
        {
          name: '形成审计意见的基础段',
          detail:
            '保留意见时先逐条说明每笔未更正错报的事项、受影响科目、金额以及多计或少计。然后说明按澳大利亚审计准则执行审计、审计师的独立性与独立性声明，以及审计证据充分、适当。',
        },
        {
          name: '写作约束',
          detail:
            '关于错报的事实只能来自上述错报和判断，不写重要性金额、执行过的程序或资料以外的事实。保留意见必须引用至少一条已确认判断。',
        },
        {
          name: '修订',
          detail:
            '退回后的修订逐条处理你的批注，其余措辞不变；重跑后的重拟按新结果重写，资料仍支持的原措辞保留。上一稿语言与当前工作语言不同时，用当前语言重写全文。',
        },
        {
          name: '校验',
          detail: '两段的首行标题须与意见类型相符，引用的判断须是当前已确认的版本；不通过时本次执行失败。',
        },
      ],
      result:
        '产出报告的下一稿，各稿可在报告页查看。本程序不单独建复核任务，草稿直接交审计报告批准与签字（RPT-01）由你审阅；执行失败时复核队列会出现“程序未能完成”的任务，可以重跑。',
      project: (p) => {
        const end = str(p, 'period_end')
        if (end == null) return null
        return `报告针对截至 ${end} 的财务报表。`
      },
    },
    en: {
      purpose:
        "Drafts the audit report for the opinion type decided at evaluation of uncorrected misstatements (FIN-01). The model writes only the Opinion and Basis for opinion sections; the other sections (directors' responsibilities, auditor's responsibilities, other legal and regulatory requirements, signature) come from a fixed template filled with the company name, the period end and the engagement lead's name.",
      when: 'Runs once FIN-01 confirms the opinion, on every engagement. When report approval and sign-off (RPT-01) returns the report with comments, it runs again to produce the next draft; after out-of-date procedures are rerun with new data at FIN-01 or RPT-01 and the opinion is confirmed again, it also redrafts. The report is drafted in the working language at the time of the run.',
      logic: [
        {
          name: 'What the model reads',
          detail:
            'The company name, the opinion type and the reason you gave at FIN-01, if any. For a qualified opinion it also reads the uncorrected misstatements on the schedule and the confirmed judgments they were recognised from.',
        },
        {
          name: 'Opinion section',
          detail:
            "Heading first; the first paragraph states that the company's financial report and its components were audited, and the second gives the opinion for the chosen type, worded “except for the effects of” for a qualified opinion.",
        },
        {
          name: 'Basis for opinion section',
          detail:
            'For a qualified opinion it first describes each uncorrected misstatement: the matter, the account affected, the amount and whether it is overstated or understated. It then states that the audit was conducted under Australian Auditing Standards, covers independence and the independence declaration, and states that the evidence obtained is sufficient and appropriate.',
        },
        {
          name: 'Drafting limits',
          detail:
            'Facts about misstatements may come only from those misstatements and judgments; materiality amounts, procedures performed and facts outside the inputs are not mentioned. A qualified opinion must cite at least one confirmed judgment.',
        },
        {
          name: 'Revisions',
          detail:
            'A revision after a return addresses every comment and leaves other wording unchanged; a redraft after a rerun is rewritten from the new results, keeping earlier wording the inputs still support. If the previous draft is in a different language from the current working language, the whole text is rewritten in the current one.',
        },
        {
          name: 'Checks',
          detail:
            'The first line of each section must be the heading for the opinion type, and cited judgments must be the current confirmed versions; otherwise the run fails.',
        },
      ],
      result:
        'Produces the next draft of the report; all drafts are on the Report page. No review task is created: the draft goes straight to report approval and sign-off (RPT-01) for you to review. If the run fails, a Procedure did not complete task appears in the review queue and can be run again.',
      project: (p) => {
        const end = str(p, 'period_end')
        if (end == null) return null
        return `The report covers the financial report for the period ended ${end}.`
      },
    },
  },
  'RPT-01': {
    zh: {
      purpose:
        '你审阅报告文本生成（RPT-03）起草的最新一稿，决定签字归档，或附批注退回修订。签字后报告定稿，项目归档并锁定。',
      when: 'RPT-03 每出一稿后停在这里等你决定，每个项目都执行。',
      logic: [
        {
          name: '呈报材料',
          detail:
            '当前一稿的全文（审计意见、形成审计意见的基础、董事的责任、审计师的责任、其他法律法规要求、签字），以及此前各次退回的批注。',
        },
        {
          name: '签字并归档',
          detail:
            '报告出签字版，报告日记为签字当天（悉尼时间），并记下签字人和签字时间。项目随即归档、锁定，之后只能查看。',
        },
        {
          name: '附批注退回',
          detail: '至少填一条批注：选段落，可引用原文，写明修改意见。AI 按批注修订出下一稿，再回到这里。',
        },
        { name: '信息不足', detail: '暂不作出决定，停在这里，报告重新呈报。' },
        {
          name: '数据有变动',
          detail:
            '程序执行之后数据有改动的，也可以在这一步到审计程序页用新数据重跑过期程序；重跑结果须重新经过未更正错报评价（FIN-01），报告随后重拟。',
        },
      ],
      result:
        '签字后的报告在报告页的“版本”中标为“已签字归档”，项目只读。退回时按批注出下一稿。可在工作区、“待决定”页或报告页作出决定。',
    },
    en: {
      purpose:
        'You review the latest draft from report drafting (RPT-03) and either sign and archive it or return it with comments for revision. Once signed, the report is final and the engagement is archived and locked.',
      when: 'Stops here for your decision each time RPT-03 produces a draft. Applies to every engagement.',
      logic: [
        {
          name: 'What you see',
          detail:
            "The full text of the current draft (opinion, basis for opinion, directors' responsibilities, auditor's responsibilities, other legal and regulatory requirements, signature) and the comments from earlier returns.",
        },
        {
          name: 'Sign and archive',
          detail:
            'The report gets a signed version dated the day of signing (Sydney time), recording who signed and when. The engagement is then archived and locked, and can only be viewed.',
        },
        {
          name: 'Return with comments',
          detail:
            'Give at least one comment: pick the section, optionally quote the text, and say what to change. The AI revises the report into the next draft and brings it back here.',
        },
        {
          name: 'Need more information',
          detail: 'No decision is made; the engagement stays here and the report is presented again.',
        },
        {
          name: 'Data has changed',
          detail:
            'If data has changed since procedures ran, you can also rerun the out-of-date procedures with the new data from the Audit procedures page at this point; the new results go through FIN-01 again and the report is then redrafted.',
        },
      ],
      result:
        'The signed report is marked Signed and archived under Versions on the Report page, and the engagement becomes read-only. A return produces the next draft. You can decide in the workspace, from Pending decisions or on the Report page.',
    },
  },
}

export function procedureIntro(id: string, lang: Lang): ProcedureIntro | undefined {
  return PROCEDURE_INTROS[id]?.[lang]
}
