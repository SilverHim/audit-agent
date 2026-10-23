import type { Lang } from '@/lib/i18n'
import { formatMoney, formatPct } from '@/lib/money'

// 程序介绍: 审计目标, 执行条件, 所需资料, 方法及复核要求; 程序页侧栏与程序详情页共用.
// 内容按程序库 (knowledge/procedures/*.yaml) 与提示词写, 改程序时同步改这里. project 按本项目最近一次执行的参数
// 快照 (ProcedureRun.params_snapshot) 写出实际取值, 没有执行或快照里没有相关参数时返回 null, 不显示
export type ProcedureIntro = {
  purpose: string // 审计目标及相关认定
  when: string // 执行时点, 风险适用条件与功能范围
  inputs?: string // 必需资料及缺失处理
  logic: { name: string; detail: string }[] // 执行方法与评价标准
  result: string // 程序产出及审计师复核要求
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
      purpose: '依据客户资料及 ASIC / ABN 核查（ACC-04）形成的承接评分卡，评价业务承接条件，并由审计师作出承接决定。',
      when: '适用于全部项目，在承接阶段（P0）完成 ASIC / ABN 核查（ACC-04）后提交承接审批，不受风险等级影响。',
      logic: [
        {
          name: '登记核查结果复核',
          detail:
            '审阅 ABN 状态、登记名称、ASIC 登记及新建客户董事名单的核查结果，区分通过、未通过和待人工确认事项。未通过事项附列例外，并保留查询记录或摘录页作为依据。',
        },
        {
          name: '客户资料修订',
          detail:
            '客户资料存在错误时，可在承接审批期间修订。保存后重新执行 ACC-04；新建公司客户先核查 ASIC 摘录需求，再以更新结果重新呈报。',
        },
        {
          name: '承接决定',
          detail:
            '决定类型为承接、不予承接或信息不足，理由选填。承接后进入计划阶段；不予承接则关闭并锁定项目；信息不足时记录决定并保留待处理状态。',
        },
      ],
      result:
        '承接决定及相关理由记录于审计轨迹。批准承接后，项目进入审计计划阶段（P1），待财务资料接入并确认完整后开展计划工作；不予承接时，项目关闭并锁定。信息不足时保留待决定状态。',
    },
    en: {
      purpose:
        'Supports the auditor’s engagement acceptance decision through evaluation of the client profile and the acceptance scorecard prepared by ASIC and ABN check (ACC-04).',
      when: 'Applies to all engagements at acceptance (P0), following ASIC and ABN check (ACC-04). Acceptance approval is independent of assessed risk level.',
      logic: [
        {
          name: 'Review of registry checks',
          detail:
            'The auditor reviews the ABN status, registered name, ASIC registration and, for new clients, directors. Results distinguish passed, failed and manually verifiable items. Failed checks include exceptions and links to supporting registry queries or extract pages.',
        },
        {
          name: 'Client profile amendments',
          detail:
            'Errors may be corrected during acceptance approval. Saving amendments triggers ACC-04 again, including assessment of ASIC extract requirements for a new company client, before updated results are presented.',
        },
        {
          name: 'Acceptance determination',
          detail:
            'Available decisions are accept, decline or request further information, with optional rationale. Acceptance initiates planning; declining closes and locks the engagement; further information records the decision while leaving the item pending.',
        },
      ],
      result:
        'The acceptance decision and any supporting rationale are recorded in the audit trail. Acceptance moves the engagement to planning (P1), with planning work commencing after the financial data is loaded and confirmed complete. Declining closes and locks the engagement; a request for further information leaves the decision pending.',
    },
  },
  'ACC-04': {
    zh: {
      purpose:
        '将客户资料中的 ABN、法定名称及公司登记信息与 ABN Lookup 和 ASIC 记录核对；新建客户另核对董事名单。核查结果构成业务承接（ACC-01）的评价依据。',
      when: '适用于全部项目，在承接阶段（P0）开始时执行。承接审批期间修订客户资料后重新核查，不受风险等级影响。',
      inputs:
        '演示项目采用数据集中的登记记录；新建客户通过 ABN Lookup 在线查询。具有 ACN 的新建公司客户须提供 ASIC 公司摘录，资料未齐时等待补交。确无相关资料并完成缺失登记后，程序继续执行，ASIC 登记及董事核查转由审计师确认。',
      logic: [
        {
          name: 'ABN 有效性',
          detail:
            'ABN 应能通过登记查询检索，且状态为有效（Active）；未检索到或状态非有效时判为未通过。例外严重程度为高。',
        },
        {
          name: '登记名称与客户资料一致',
          detail:
            '登记名称与客户法定名称在忽略大小写、空格和标点后应一致。ABN 未检索到时判为未通过；登记名称未公开时转人工确认。例外严重程度为高。',
        },
        {
          name: '公司已在 ASIC 登记',
          detail:
            '根据 ABN 记录中的 ACN 核查 ASIC 登记状态。演示记录中 ABN 未检索到或缺少 ACN 时判为未通过。新建公司客户核对已上传摘录中的登记状态、ACN 与法定名称；摘录均不匹配时判为未通过，非公司或缺少可用摘录时转人工确认。例外严重程度为高。',
        },
        {
          name: '董事名单核对（新建客户）',
          detail:
            '客户资料中的董事名单与 ASIC 摘录所列董事逐一核对；名单不完整或存在额外人员时判为未通过，不包括公司秘书。例外严重程度为高。',
        },
        {
          name: '登记查询受限',
          detail:
            'ABN Lookup 未配置、无法连接或返回错误时，各项检查转人工确认，不直接生成例外。成功及失败的查询均记录于审计轨迹，并保留为核查依据。',
        },
      ],
      result:
        '未通过的检查形成严重程度为高的例外；需人工核实的事项不直接认定为例外。核查结果及例外汇入承接评分卡，在业务承接（ACC-01）一并呈报，不单独形成复核任务。程序执行失败时生成可重新执行的失败任务。',
    },
    en: {
      purpose:
        'Compares the client’s ABN, legal name and company registration details with ABN Lookup and ASIC records. Directors are also checked for new clients. The results support Engagement acceptance (ACC-01).',
      when: 'Applies to all engagements at the start of acceptance (P0). Amendments to the client profile during acceptance approval trigger a fresh check, independently of assessed risk level.',
      inputs:
        'Demo engagements use registry records supplied with the dataset; new clients are queried through ABN Lookup. A new company client with an ACN requires an ASIC company extract. Execution waits for the extract unless its absence is recorded, in which case ASIC registration and director checks require auditor confirmation.',
      logic: [
        {
          name: 'ABN is active',
          detail:
            'The ABN is found on the ABN register with status Active. Not found, or any other status, fails. Exception severity is high.',
        },
        {
          name: 'Registered name matches the client profile',
          detail:
            'The registered name must match the client’s legal name after ignoring case, spaces and punctuation. An ABN not found fails the check; a non-public registered name requires manual confirmation. Exception severity is high.',
        },
        {
          name: 'Company is registered with ASIC',
          detail:
            'ASIC registration is checked against the ACN on the ABN record. For demo records, an absent ABN or ACN fails the check. New company clients are checked against uploaded extracts for registration status, ACN and legal name. No matching extract fails the check; a non-company client or absence of a usable extract requires manual confirmation. Exception severity is high.',
        },
        {
          name: 'Directors match the ASIC extract (new clients)',
          detail:
            'The directors in the client profile match, name for name, those shown as directors on the extract; any extra or missing name fails. The company secretary is not compared. Exception severity is high.',
        },
        {
          name: 'Registry access limitations',
          detail:
            'An unconfigured, unavailable or failed ABN Lookup query requires manual confirmation of the checks without directly raising exceptions. Successful and failed queries are retained in the audit trail as supporting records.',
        },
      ],
      result:
        'Failed checks produce high-severity exceptions; matters requiring manual verification do not themselves create exceptions. The checks and exceptions form the acceptance scorecard presented at Engagement acceptance (ACC-01), without separate review tasks. An execution failure creates a task permitting a rerun.',
    },
  },
  'ACC-08': {
    zh: {
      purpose:
        '核验已签署审计业务约定书及委任文件的完整性，评价审计前提条件、董事责任确认及审计师委任的有效性，为约定条款与委任的批准提供依据。本程序不编制业务约定书。',
      when: '本版尚未实现，以下为设计说明。计划在启用承接功能时，于 ACC-01 批准后、进入计划阶段前执行，不受风险等级影响。',
      inputs: '客户签回的约定书和委任文件（董事决议、股东指示、审计师同意书）。',
      logic: [
        {
          name: '事实提取（模型）',
          detail:
            '模型按页分析全部约定文件，提取约定书写明的财务报告框架、约定书的五项要素和董事确认的五项责任是否写明、约定书日期与签回人、股东指示、委任审计师的决议、审计师同意书、缩短财年的决议。每项标出所在文件和页码；文件未载明的项目留空，不推测。',
        },
        {
          name: '核对要求（规则）',
          detail:
            '按提取的事实逐项核对：框架可以接受（特殊目的框架也可以，但报告须加强调事项段）；五项要素齐全；五项责任都已确认；小型私人公司须有持至少 5% 表决权的股东签署、要求审计、并在财年结束后 12 个月内作出的指示；委任之前已取得审计师以事务所和本人名义签署的书面同意；约定书由董事签回；本期不足 12 个月时，缩短财年的决议合规。各项未通过检查分别形成例外。',
        },
        {
          name: '给出建议（模型）',
          detail:
            '模型根据事实和核对结果，建议“具备承接前提”或“需要补充证据”，说明每项不通过意味着什么，客户能否补正（如在重新委任之前取得签署的同意书），还是应当不承接。建议不替代核对结果。',
        },
      ],
      result:
        '以下为设计产出：提取事实、核查结果、例外及模型建议统一提交阶段审批，不单独进入复核队列。审批结果包括通过、不予承接或要求补充资料。确认的财务报告框架及约定书用于总体审计计划（PLN-15）。本版尚未实现该流程。',
    },
    en: {
      purpose:
        'Evaluates the completeness of the signed engagement letter and appointment documents, the audit preconditions, acknowledgement of directors’ responsibilities and validity of the auditor’s appointment. The procedure supports approval of the engagement terms and appointment; it does not draft the engagement letter.',
      when: 'Not implemented in the current version; the following describes the intended design. Where the Acceptance feature is enabled, execution is intended after ACC-01 approval and before planning, independently of assessed risk level.',
      inputs:
        'The signed engagement letter and the appointment documents (directors’ resolution, members’ direction, auditor’s consent).',
      logic: [
        {
          name: 'Fact extraction (model)',
          detail:
            'The model reads every page of the documents and records the financial reporting framework the letter identifies, whether the letter covers the five required matters and the directors’ five acknowledgements, the letter’s date and signatory, the members’ direction, the resolution appointing the auditor, the auditor’s consent and any resolution shortening the financial year. Each fact points to its document and page; facts the documents do not state are left blank, never inferred.',
        },
        {
          name: 'Requirements verification (rules)',
          detail:
            'The facts are checked one by one: the framework is acceptable (a special purpose framework is too, but the report then needs an emphasis of matter paragraph); all five matters are in the letter; all five responsibilities are acknowledged; for a small proprietary company, a direction signed by members with at least 5% of the votes, asking for an audit and made within 12 months after year end; the auditor’s written consent, signed in the firm name and in the auditor’s own name, given before the appointment; the letter signed back by a director; and, where the period is shorter than 12 months, a compliant resolution changing the financial year. Each failed check is recorded as an exception.',
        },
        {
          name: 'Recommendation (model)',
          detail:
            'From the facts and checks the model recommends either that the preconditions are present or that more evidence is needed, explaining what each failed check means and whether the client can cure it (for example, a consent signed before a fresh appointment) or the engagement should not be accepted. The recommendation does not replace the checks.',
        },
      ],
      result:
        'Intended outputs are the extracted facts, checks, exceptions and model recommendation, presented together for stage approval rather than as separate review tasks. The decision may approve the terms, decline the engagement or request further information. The confirmed reporting framework and engagement letter inform Overall audit plan (PLN-15). This workflow is not implemented in the current version.',
    },
  },
  'DOC-04': {
    zh: {
      purpose:
        '从收入合同中提取交易对方、ABN、合同总价、签署日期及影响收入确认的条款，并保留原文与页码，形成收入合同条款评价（REV-03）等程序所需的结构化资料。',
      when: '适用于全部项目，在计划阶段（P1）确认财务资料完整后、初步分析前执行。晚于计划阶段取得的合同在执行阶段补充处理。',
      inputs:
        '收入合同（PDF）。资料缺失时本程序等待补充，其他计划工作继续；确认客户无相关资料并完成缺失登记后，本程序记为未执行。',
      logic: [
        {
          name: '合同版本确定',
          detail:
            '按合同编号（文件名）选取最新上传版本。已在上传阶段完成提取的结果直接复用，不重复处理或覆盖人工修订。',
        },
        {
          name: '合同要素提取（模型）',
          detail:
            '模型按页分析合同，提取对方（客户）名称、ABN、合同总价（同时列出含税与不含税金额时取不含 GST 的；仅列明单价时留空）和签订日期（双方签署日期不同时取较晚者），每项标出页码。合同未载明的项目留空，不推算。',
        },
        {
          name: '合同条款提取（模型）',
          detail:
            '按付款、控制权转移、退货权、返利与折扣、终止、超出常规质保的保证、关联方声明七类，提取条款原文（含条款编号）和起始页；仅声明某项权利不适用的条款不纳入提取。',
        },
        {
          name: '结果校验与重试',
          detail:
            '每个提取的值都要有出处，出处只能指向这份合同，页码不能超出页数。未通过校验时向模型反馈问题，最多尝试三次。',
        },
      ],
      result:
        '校验通过的提取结果保存为合同记录，文件状态更新为已完成；审计师可在文件详情中核查并修订结果。三次尝试后仍未通过校验的合同转为待人工处理，并生成仅支持重新执行的失败任务。结果供 REV-03 及 GEN-03 使用。',
    },
    en: {
      purpose:
        'Extracts the counterparty, ABN, total consideration, signing date and terms relevant to revenue recognition from revenue contracts. Source wording and page references are retained for Revenue contract terms (REV-03) and related procedures.',
      when: 'Applies to all engagements during planning (P1), after financial data completeness is confirmed and before preliminary analysis. Contracts received later are processed during fieldwork.',
      inputs:
        'Revenue contracts (PDF). Without them this procedure waits for data while the rest of planning carries on. If the client has none, mark it as such and the procedure is recorded as not performed.',
      logic: [
        {
          name: 'Contract version selection',
          detail:
            'The latest uploaded file is selected for each contract number derived from the file name. Results already extracted on upload are reused without reprocessing or overwriting auditor amendments.',
        },
        {
          name: 'Contract detail extraction (model)',
          detail:
            'The model reads the contract page by page and records the counterparty (the customer), its ABN, the total price (the GST-exclusive amount when both are shown; blank when the contract sets only unit prices) and the signing date (the later date when the parties signed on different days), each with its page. Anything the contract does not state is left blank, never calculated.',
        },
        {
          name: 'Clause extraction (model)',
          detail:
            'Clauses of seven types are copied word for word, with their numbers and starting page: payment, transfer of control, return rights, rebates and discounts, termination, guarantees beyond standard warranty, and related party statements. A clause that only says a right does not apply is skipped.',
        },
        {
          name: 'Validation and retry',
          detail:
            'Every extracted value must point to a page of this contract within its page count. If not, the problems go back to the model to try again, up to three attempts in all.',
        },
      ],
      result:
        'Validated extractions are saved as contract records and the files marked complete. Extracted details remain available for auditor review and amendment in the file detail view. A contract failing validation after three attempts is marked as requiring auditor action and receives a failure task permitting a rerun only. REV-03 and GEN-03 use the resulting records.',
    },
  },
  'DOC-06': {
    zh: {
      purpose:
        '提取银行函证回函载明的确认余额、余额截至日及应计未入账利息，更新账户函证记录，为函证差异评价（CSH-02）提供依据。',
      when: '在执行阶段（P2）发起银行函证（CSH-04）后，对已收到的回函执行提取。货币资金存在认定风险为中、高或特别风险时，作为 CSH-02 的前置程序纳入。',
      logic: [
        { name: '回函范围确定', detail: '选取银行账户清单中仍有效、账户信息未变更且已取得回函的函证记录。' },
        {
          name: '回函要素提取（模型）',
          detail:
            '模型按页分析回函，提取出具回函的银行、账号（不含 BSB）、确认余额（透支为负数）、余额截至日、回函方式（直接寄给或发邮件给审计师、经客户或客户能访问的门户、口头），以及截至余额日已计提未入账的利息（银行应付客户利息为正，客户应付银行利息为负；写明为零记 0，未载明或仅提供整期利息时留空，不推算）。每项标出页码。',
        },
        {
          name: '账户及回函渠道核对',
          detail:
            '提取账号须与被函证账户一致，不符时本次提取不通过。回函载明渠道与发函回执记录不一致时作提示记录，渠道认定以回执为准。',
        },
        {
          name: '结果校验与重试',
          detail:
            '证据出处仅限本回函且页码须在有效范围内；应计利息提取须附证据出处。未通过校验时反馈问题，最多尝试三次。',
        },
        {
          name: '人工修订保留',
          detail: '回函原件未更换且提取结果已有人工修订时，保留修订结果，不重复提取，并记录相应审计轨迹。',
        },
      ],
      result:
        '校验通过后更新函证记录并将文件标记为已完成，不逐份生成复核任务。银行名称、余额截至日及确认余额可在文件详情中修订。三次尝试后仍未通过校验的回函转为待人工处理，并生成仅支持重新执行的失败任务。CSH-02 随后评价回函与账面记录的差异。',
    },
    en: {
      purpose:
        'Extracts the confirmed balance, balance date and accrued but unposted interest from bank confirmation replies. The account confirmation record is updated to support Confirmation differences (CSH-02).',
      when: 'Extracts received replies during fieldwork (P2), after Bank confirmation requests (CSH-04). Included as a prerequisite of CSH-02 where cash existence risk is medium, high or significant.',
      logic: [
        {
          name: 'Reply population',
          detail:
            'Confirmations for accounts still in the bank accounts table whose details have not changed, where a reply has been received.',
        },
        {
          name: 'Reply extraction (model)',
          detail:
            'The model reads the reply page by page and records the bank that issued it, the account number (without the BSB), the confirmed balance (negative if overdrawn), the date it is confirmed as at, how the reply was sent (posted or emailed directly to the auditor, through the client or a portal the client can access, or orally) and the interest accrued to that date but not yet posted (positive if the bank owes it, negative if the customer does; 0 when stated as nil; blank when not mentioned or when only a whole period’s interest is given, never calculated). Each value points to its page.',
        },
        {
          name: 'Account number verification',
          detail:
            'If the account number read differs from that of the confirmed account, the reply is not for that account and the reading fails. If the delivery method stated differs from the dispatch receipt, this is only noted; the receipt prevails.',
        },
        {
          name: 'Validation and retry',
          detail:
            'Every value must point to a page of this reply within its page count, and accrued interest must have its own pointer. If not, the problems go back to the model to try again, up to three attempts in all.',
        },
        {
          name: 'Retention of auditor amendments',
          detail:
            'If a reply has not been replaced and its extracted details have been amended, the amendments are retained without re-extraction and the reuse is recorded in the audit trail.',
        },
      ],
      result:
        'Validated results update the confirmation record and mark the file complete without a separate review task for each reply. The bank name, balance date and confirmed balance can be amended in the file detail view. A reply failing validation after three attempts requires auditor action through a failure task permitting a rerun only. CSH-02 subsequently evaluates differences from the accounting records.',
    },
  },
  'PLN-01': {
    zh: {
      purpose:
        '依据试算表及初步分析结果，形成重要性基准与适用比例区间，支持审计师确定财务报表整体的重要性、实际执行的重要性及明显微小错报临界值。',
      when: '适用于全部项目，在计划阶段（P1）初步分析（PLN-07）完成后执行。重要性与风险审批被退回时重新计算。',
      inputs: '科目表和本期试算表。上述资料为财务资料完整性确认的前提，资料缺失时，计划阶段暂停并等待补充。',
      logic: [
        {
          name: '基准金额计算（规则）',
          detail:
            '营业收入取收入类科目余额（贷方为正）；资产总额取全部资产类科目余额；税前利润取全部收入与费用类科目余额相抵，所得税费用除外。',
        },
        {
          name: '重要性区间计算',
          detail:
            '每个基准乘以它的比例区间上下限，得出重要性水平的候选金额。基准不为正的（如税前亏损）照样列出，标为本期不适用，不能选。',
        },
        {
          name: '重要性确定（人工）',
          detail:
            '审计师选定基准及适用区间内的比例，确定整体重要性。实际执行的重要性须处于规定比例区间，明显微小错报临界值须大于零且不超过设定上限。系统按确认参数计算三个金额。',
        },
      ],
      result:
        '重要性方案与风险清单在 PLN-01/03 一并审批。批准时确定基准、比例及三个金额，供后续程序筛选、复核分档和错报汇总使用。首次执行同时载入项目审计目标与风险清单，提交风险清单终审（PLN-03）。',
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
        'Uses trial balance data and preliminary analytical results to establish materiality benchmarks and applicable percentage ranges, supporting the auditor’s determination of overall materiality, performance materiality and the clearly trivial threshold.',
      when: 'Applies to all engagements during planning (P1), following preliminary analysis (PLN-07). A return of the materiality and risk decision initiates recalculation.',
      inputs:
        'The chart of accounts and the current trial balance. Both are required before the financial data can be confirmed complete, so without them planning waits for data.',
      logic: [
        {
          name: 'Benchmark calculation (rules)',
          detail:
            'Revenue is the balance of the revenue accounts (credit positive); total assets is the balance of all asset accounts; profit before tax is the net of all revenue and expense accounts, excluding income tax expense.',
        },
        {
          name: 'Calculation of materiality ranges',
          detail:
            'Each benchmark is multiplied by the low and high ends of its percentage range to give candidate amounts for overall materiality. A benchmark that is not positive (such as a loss before tax) is still listed but marked not applicable this period and cannot be chosen.',
        },
        {
          name: 'Auditor materiality determination',
          detail:
            'The auditor selects a benchmark and a percentage within its applicable range to determine overall materiality. Performance materiality must fall within the permitted proportion and the clearly trivial threshold must be positive and no greater than the configured ceiling. The system calculates all three amounts from the confirmed parameters.',
        },
      ],
      result:
        'Materiality and the risk register are approved together at PLN-01/03. Approval establishes the benchmark, percentages and three monetary thresholds used for subsequent procedure selection, review tiers and misstatement aggregation. The initial run also loads the engagement’s audit objectives and risk register for Risk register sign-off (PLN-03).',
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
        '复核并批准审计目标与风险清单，确定各项风险的最终等级。风险评估结果用于选择执行阶段的审计程序，并确定相应执行参数。',
      when: '适用于全部项目，在计划阶段（P1）与重要性水平（PLN-01）通过同一阶段决定一并审批。',
      logic: [
        {
          name: '风险清单来源',
          detail:
            '重要性水平（PLN-01）首次执行时带出本项目的审计目标与风险清单：使用演示数据集的项目取数据集预置的清单，新建的客户取系统的通用模板。',
        },
        {
          name: '风险等级调整',
          detail: '审计师可逐项修订风险等级，理由选填，并记录相应确认人。每个审计目标的等级取所关联风险的最高等级。',
        },
        {
          name: '检查程序覆盖',
          detail:
            '按调整后的等级，每个审计目标都要至少触发一条程序。有目标没有程序覆盖时不能批准，只能退回或再调整等级。',
        },
        {
          name: '风险与重要性审批',
          detail:
            '决定类型包括批准、退回或信息不足，理由选填。批准须同时确定重要性；退回时重新执行初步分析及重要性计算；信息不足时保留待决定状态。',
        },
      ],
      result:
        '批准后，系统根据审计目标及风险等级确定程序组合，纳入适用的固定程序并补齐前置依赖。启用总体审计计划功能时，先提交 PLN-15 计划审批；未启用时直接进入执行阶段。风险等级调整及批准信息保留于审计轨迹。',
    },
    en: {
      purpose:
        'Supports review and approval of the audit objectives and risk register, establishing the final assessed risk levels used to select fieldwork procedures and determine their execution parameters.',
      when: 'Applies to all engagements during planning (P1), through the same stage decision as Materiality (PLN-01).',
      logic: [
        {
          name: 'Risk register source',
          detail:
            'The first run of Materiality (PLN-01) brings in the engagement’s audit objectives and risk register: the preset list in the demo dataset, or the system’s general template for a new client.',
        },
        {
          name: 'Risk level amendments',
          detail:
            'The auditor may amend individual risk levels, with optional rationale and recorded approval. Each audit objective takes the highest assessed level among its associated risks.',
        },
        {
          name: 'Procedure coverage verification',
          detail:
            'Every audit objective must trigger at least one procedure at its final risk level. An uncovered objective prevents approval until the assessment is amended or returned.',
        },
        {
          name: 'Risk and materiality approval',
          detail:
            'Available decisions are approval, return or request for further information, with optional rationale. Approval requires a materiality determination. A return initiates preliminary analysis and recalculation; further information leaves the decision pending.',
        },
      ],
      result:
        'Following approval, the system assembles procedures matched to the audit objectives and risk levels, adds applicable fixed procedures and includes their prerequisites. Where overall audit planning is enabled, PLN-15 approval precedes fieldwork; otherwise execution begins directly. Risk amendments and approval details are retained in the audit trail.',
    },
  },
  'PLN-07': {
    zh: {
      purpose:
        '通过跨期余额比较、财务比率及月度趋势分析，识别异常变动和潜在重大错报风险领域，为重要性确定与风险评估提供依据。',
      when: '适用于全部项目，在计划阶段（P1）确认财务资料完整后执行，之后开展 PLN-01。重要性与风险审批被退回时重新执行分析。',
      inputs:
        '科目表、本期与上期试算表、本期总账分录。上述资料为财务资料完整性确认的前提，资料缺失时，计划阶段暂停并等待补充。',
      logic: [
        {
          name: '科目变动超阈值',
          detail:
            '资产负债类科目比较本期末与期初余额（期初取上期末余额，上期损益结转进留存收益）；损益类科目只在本期与上期等长时比较两期金额。变动同时达到比较基数绝对值的一定比例和本期收入的一定比例时记为例外。例外严重程度为中。',
        },
        {
          name: '财务比率分析',
          detail:
            '计算本期及上期毛利率〔（收入－销售成本）÷收入〕及收入增长率；对应期间资料缺失或收入为零时，不计算相关比率。',
        },
        {
          name: '月度趋势分析',
          detail: '按月汇总本期总账分录中的收入、费用和其中的员工费用；无分录月份按零列示，以反映月度记录缺口。',
        },
      ],
      result:
        '超出设定阈值的科目形成例外，与比率及趋势分析结果在 PLN-01/03 一并呈报，不单独生成复核任务。结果亦用于总体审计计划（PLN-15）。后续资料变更时标记结果过期；重新分析需通过计划阶段的退回流程完成。',
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
        'Identifies unusual movements and potential areas of material misstatement through comparative balance analysis, financial ratios and monthly trends, supporting materiality determination and risk assessment.',
      when: 'Applies to all engagements during planning (P1), after financial data completeness is confirmed and before PLN-01. Returning the materiality and risk decision initiates reassessment.',
      inputs:
        'The chart of accounts, the current and prior trial balances and the current journal entries. They are required before the financial data can be confirmed complete, so without them planning waits for data.',
      logic: [
        {
          name: 'Account movement above threshold',
          detail:
            'Balance sheet accounts compare the period-end balance with the opening balance (the prior period-end balance, with the prior profit or loss carried into retained earnings); profit or loss accounts compare the two periods only when they are of equal length. A movement is an exception when it reaches both a set share of the comparison base (in absolute terms) and a set share of current revenue. Exception severity is medium.',
        },
        {
          name: 'Financial ratio analysis',
          detail:
            'Gross margin (revenue less cost of sales, over revenue) for both periods, and revenue growth. A ratio is left out when a period has no data or its revenue is zero.',
        },
        {
          name: 'Monthly trend analysis',
          detail:
            'Current-period journal entries are totalled by month into revenue, expenses and, within expenses, employee costs. Months without entries show zero, so a missing month stands out.',
        },
      ],
      result:
        'Accounts exceeding the configured thresholds produce exceptions, presented with the ratio and trend analyses at PLN-01/03 without separate review tasks. The results also inform Overall audit plan (PLN-15). Subsequent data changes mark the results as out of date; reassessment follows the planning-stage return process.',
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
        '编制总体审计策略及审计计划草稿，说明审计范围、风险应对、程序的性质、时间安排和范围，以及项目质量复核和专家需求。已批准的重要性、审计目标及程序参数由系统固定，叙述部分由模型起草并提交审计师批准。',
      when: '启用总体审计计划功能时，在重要性与风险批准、执行阶段程序组合确定后，进入执行阶段前编制。计划被退回时，依据审阅意见形成下一修订。',
      logic: [
        {
          name: '计划结构确定（规则）',
          detail:
            '纳入审计期间、经 ACC-08 确认的财务报告框架及约定书（未核验时留空）、已批准重要性及三个金额、特别风险审计目标，以及各执行程序及参数。上述内容不由模型增删或修改。',
        },
        {
          name: '策略与计划叙述（模型）',
          detail:
            '依据客户资料、目标与风险、计划程序及参数、试算表、初步分析和重要性编制总体策略。范围、特别风险应对、重要性及时间安排为必备内容；其他段落按资料适用性编制。逐项说明程序的性质、时间安排和范围，形成时间表、项目质量复核建议及依据和专家需求评价。退回后按审阅意见修订相关内容。',
        },
        {
          name: '结果校验与重试',
          detail:
            '引用的准则只能是本程序列出的；每项计划程序须具有唯一对应说明；时间表不早于期末；必备段落完整；项目质量复核建议须写依据。未通过校验时向模型反馈问题，最多尝试三次。',
        },
        {
          name: '计划审批（人工）',
          detail:
            '审计师审阅计划及修订记录。批准须确认项目质量复核需求及依据，并记录是否确认不需要审计师专家；后者未确认时仅记录该状态。退回后形成下一修订，理由选填；信息不足时保留待决定状态。',
        },
      ],
      result:
        '计划批准后记录批准人、时间及确认事项，并进入执行阶段（P2）。退回修订时保留历史版本，旧修订标记为已被取代。三次尝试后仍未通过校验的草稿生成失败任务。未启用总体审计计划功能的项目在程序组合确定后直接进入执行阶段。',
      project: (p) => {
        const start = str(p, 'period_start')
        const end = str(p, 'period_end')
        if (start == null || end == null) return null
        return `计划覆盖审计期间 ${start} 至 ${end}。`
      },
    },
    en: {
      purpose:
        'Prepares the overall audit strategy and plan, addressing scope, risk responses, the nature, timing and extent of procedures, and the need for engagement quality review and an auditor’s expert. Approved materiality, audit objectives and procedure parameters are fixed by the system; the model drafts the narrative for auditor approval.',
      when: 'Where overall audit planning is enabled, drafting follows approval of materiality and risks and assembly of fieldwork procedures, before execution begins. A returned plan is revised in response to review comments.',
      logic: [
        {
          name: 'Plan structure determination (rules)',
          detail:
            'The fixed structure comprises the audit period, ACC-08-confirmed reporting framework and engagement letter where available, approved materiality and all three amounts, significant-risk objectives, and fieldwork procedures and parameters. The model cannot add, remove or amend these components.',
        },
        {
          name: 'Strategy and plan narrative (model)',
          detail:
            'The narrative uses the client profile, objectives, risks, planned procedures and parameters, trial balance, preliminary analysis and materiality. Required sections cover scope, significant-risk responses, materiality and timing; other sections depend on available evidence. Each procedure receives a nature, timing and extent note. The plan includes a timetable, an engagement quality review recommendation with its basis, and an assessment of expert needs. Redrafting addresses the review comments.',
        },
        {
          name: 'Validation and retry',
          detail:
            'Cited standards must be among those listed for this procedure; every planned procedure needs exactly one note; no milestone may fall before period end; the required sections must be present; and the engagement quality review recommendation must state its basis. If not, the problems go back to the model to try again, up to three attempts in all.',
        },
        {
          name: 'Plan approval (auditor)',
          detail:
            'The auditor reviews the plan and revision history. Approval requires confirmation of engagement quality review needs and the basis, and records whether no auditor’s expert is required; leaving the latter unconfirmed is recorded without blocking approval. A return produces the next revision, with optional rationale. Further information leaves the decision pending.',
        },
      ],
      result:
        'Approval records the approver, time and confirmations and initiates fieldwork (P2). Returned plans retain their revision history, with earlier revisions marked superseded. A draft failing validation after three attempts creates a failure task. Engagements without overall audit planning enabled proceed to fieldwork once the procedure pack is assembled.',
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
        '对期末前设定期间内入账的销售发票实施全量匹配，将发票、发运或服务完成记录与银行收款核对，识别与收入发生及截止认定相关的差异。',
      when: '收入发生或截止认定存在风险时适用，覆盖全部风险等级。检查期间按风险等级确定：低风险 5 天、中风险 10 天、高风险 15 天、特别风险 20 天。',
      inputs: '销售发票、发运单、银行流水。任一资料缺失时，程序暂停并等待补充。',
      logic: [
        {
          name: '入账期间正确',
          detail: '发票在期末前入账，对应发运单的签收日却在期末之后，可能提前确认了收入。例外严重程度为高。',
        },
        { name: '履约依据完整性', detail: '发票未找到对应的发运单，或发运单没有签收日。例外严重程度为高。' },
        {
          name: '收款金额核对',
          detail:
            '按发票号在银行流水里找到的收款合计不等于发票金额。例外严重程度为中。截至数据日还没收款的发票不作判断。',
        },
      ],
      result:
        '匹配差异分别记录为例外。收入截止风险为中、高或特别风险时，由收入截止性判断（REV-02）进一步评价相关发票；其他情形下，例外保留于本程序记录供审阅，不直接生成复核任务。',
      project: (p) => {
        const days = num(p, 'window_days')
        const end = str(p, 'period_end')
        if (days == null || end == null) return null
        return `检查期末前 ${days} 天，即 ${daysBefore(end, days)} 至 ${end} 入账的销售发票。`
      },
    },
    en: {
      purpose:
        'Performs a full-population match of sales invoices posted within the configured period-end window against delivery or service completion records and bank receipts, identifying differences relevant to revenue occurrence and cut-off.',
      when: 'Applies where revenue occurrence or cut-off risk is identified, at any risk level. Testing windows are 5 days for low, 10 for medium, 15 for high and 20 for significant risk.',
      inputs:
        'Sales invoices, delivery notes and bank transactions. If any of them is missing, the procedure waits for data.',
      logic: [
        {
          name: 'Recorded in the correct period',
          detail:
            'The invoice is posted before period end but its delivery note was signed after period end, so revenue may have been recognised early. Exception severity is high.',
        },
        {
          name: 'Service completion record exists',
          detail:
            'The invoice has no delivery note, or the delivery note has no signed date. Exception severity is high.',
        },
        {
          name: 'Receipts agree to the invoice',
          detail:
            'The receipts found in the bank transactions by invoice number do not add up to the invoice amount. Exception severity is medium. Invoices not yet paid at the data date are not assessed.',
        },
      ],
      result:
        'Matching differences are recorded as exceptions. Revenue cut-off (REV-02) evaluates the affected invoices where cut-off risk is medium, high or significant. Otherwise, exceptions remain available in the procedure record for review without directly generating review tasks.',
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
      purpose: '对项目银行账户清单中的账户发起期末余额函证，并登记、归档银行回函，为货币资金存在认定提供外部证据。',
      when: '货币资金存在认定风险为中、高或特别风险时适用。',
      inputs: '银行账户清单。资料缺失时，程序暂停并等待补充。',
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
          name: '回函缺失处理',
          detail:
            '本版仅支持已取得回函的账户。任一账户尚未收到回函时，本次执行失败并提交审计师处理，可在取得回函后重新执行。',
        },
      ],
      result:
        '形成各账户的函证记录及回函证据。本程序不作审计判断或生成例外；函证回函抽取（DOC-06）提取余额和应计利息后，由函证差异评价（CSH-02）开展后续评价。',
      project: (p) => {
        const asOf = str(p, 'as_of')
        if (asOf == null) return null
        return `询证各账户 ${asOf} 的余额。`
      },
    },
    en: {
      purpose:
        'Initiates period-end balance confirmation for accounts on the engagement’s bank account list and registers and archives replies as external evidence relevant to the existence of cash balances.',
      when: 'Applies where cash existence risk is assessed as medium, high or significant.',
      inputs: 'The list of bank accounts. If it is missing, the procedure waits for data.',
      logic: [
        {
          name: 'Accounts confirmed',
          detail: 'Every account on the bank account list gets one request, asking for the balance at period end.',
        },
        {
          name: 'Duplicate request controls',
          detail:
            "An account already confirmed is not sent another request. If an account's bank, BSB or account number changes, it is treated as a new account and confirmed again; the old confirmation is kept as history.",
        },
        {
          name: 'Reply filed',
          detail:
            'The reply is filed with the evidence. A reply the bank posted or emailed directly to the auditor is treated as an external original; one received any other way, such as through the client, as an external copy.',
        },
        {
          name: 'Missing replies',
          detail:
            'The current version supports accounts with received replies only. Any outstanding reply causes the run to fail and be referred for auditor action, with a rerun available after receipt.',
        },
      ],
      result:
        'Produces account confirmation records and archived reply evidence. The procedure makes no audit judgment and raises no exceptions. Confirmation reply extraction (DOC-06) extracts balances and accrued interest before Confirmation differences (CSH-02) performs the subsequent evaluation.',
      project: (p) => {
        const asOf = str(p, 'as_of')
        if (asOf == null) return null
        return `Confirms each account's balance at ${asOf}.`
      },
    },
  },
  'CSH-05': {
    zh: {
      purpose: '核对各银行账户的期末对账单余额与对应总账余额，识别未调节差异，为货币资金存在认定的进一步评价提供依据。',
      when: '货币资金存在认定存在风险时适用，覆盖全部风险等级。',
      inputs: '银行账户清单、银行流水。任一资料缺失时，程序暂停并等待补充。',
      logic: [
        {
          name: '对账单期末余额与总账一致',
          detail:
            '以每个账户期末当天或之前最后一笔银行流水后的余额作为对账单余额，与该账户总账科目的本期期末余额比较。两者不等，或缺少期末前流水、试算表里没有该科目时，记为例外，金额为对账单减账面。例外严重程度为中。',
        },
      ],
      result:
        '每个存在差异或缺少核对依据的账户形成例外。CSH-02 对相关账户开展函证差异评价时，一并考虑本程序例外；未发现例外时，本程序自动完成。例外不直接生成复核任务。',
      project: (p) => {
        const end = str(p, 'period_end')
        if (end == null) return null
        return `核对各账户 ${end} 的对账单余额与总账余额。`
      },
    },
    en: {
      purpose:
        'Compares each bank account’s period-end statement balance with the corresponding general ledger balance to identify unreconciled differences supporting further evaluation of cash existence.',
      when: 'Applies where cash existence risk is identified, at any risk level.',
      inputs:
        'The list of bank accounts and the bank transactions. If either is missing, the procedure waits for data.',
      logic: [
        {
          name: 'Statement balance agrees to the general ledger',
          detail:
            "The statement balance is the balance after the last bank transaction on or before period end, compared with the period-end balance of the account's general ledger account. A difference, no transactions up to period end, or no trial balance line for the ledger account is an exception, measured as statement minus book. Exception severity is medium.",
        },
      ],
      result:
        'Each account with a difference or missing reconciliation evidence produces an exception. CSH-02 considers these exceptions when evaluating confirmation differences on the relevant account. The procedure completes automatically when no exceptions arise and does not directly generate review tasks.',
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
        '核对银行函证回函与期末总账余额，检查回函所列应计利息的入账情况，并评价调节项目能否充分解释差异，以识别货币资金及相关利息的潜在错报。主要涉及存在认定。',
      when: '货币资金存在认定风险为中、高或特别风险时适用，在 CSH-05 及 DOC-06 完成后执行。',
      inputs: '银行账户清单、银行流水。任一资料缺失时，程序暂停并等待补充。',
      logic: [
        {
          name: '回函与账面余额核对',
          detail:
            '逐户核对回函余额与对应总账科目的期末余额，按回函余额减账面余额计算差异，存在差异时记录严重程度为中的例外。函证记录同步保存账面余额、差额及核对结果。',
        },
        {
          name: '应计利息入账检查',
          detail:
            '回函所列应计利息的绝对值超过明显微小错报临界值，且期末前检查期间内未找到借方或贷方金额相等的分录行时，记录严重程度为中的例外。检索覆盖全部科目，原凭证及其冲回凭证均不纳入。',
        },
        {
          name: '评价对象确定',
          detail:
            '存在上述任一例外的账户逐户形成判断，并合并考虑该账户的 CSH-05 例外。回函余额与账面一致且应计利息已入账的账户不另行形成判断。',
        },
        {
          name: '模型评价依据',
          detail:
            '回函（余额、截至日、应计利息、回函渠道及可靠性）、账面余额与差额、该账户期后的银行流水，以及期末前该银行科目的分录（不含互相冲回的凭证）。',
        },
        {
          name: '调节项目评价',
          detail:
            '回函余额须对应期末日。对期末前已入账、期后完成银行清算的收付款，依次按金额、日期及参考号匹配在途存款或未达付款，评价调节项目能否全额解释差异，金额精确至分。仅于期后发生且无期末前账面记录的交易不列为调节项。',
        },
        {
          name: '处理建议',
          detail:
            '截至日正确、差额被调节项完全解释且没有应计利息问题，建议关闭；账面存在记录差错（如同一参考号的分录与银行流水金额不同、重复记账）或应计利息未入账，建议认定为错报；截至日不符，或差异未充分解释且无法确认记录差错，建议补充证据。',
        },
      ],
      result:
        '每个异常账户形成一项判断，均须审计师复核；符合条件的判断先经独立模型复核，结论不一致时提交分歧裁决。拟认定错报时附列科目、方向及金额：余额差错列示货币资金高估或低估，应计利息遗漏列示利息收入或支出低估。每户仅形成一份错报草稿，是否登记由审计师决定。判断确认后，相关函证、利息及 CSH-05 例外标记为已解释；判断失败时支持重新执行或人工形成结论。',
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
        'Reconciles bank confirmation replies to period-end ledger balances, checks the recording of accrued interest and evaluates whether reconciling items explain the differences. The procedure identifies potential misstatements in cash and related interest, primarily addressing existence.',
      when: 'Applies where cash existence risk is medium, high or significant, following completion of CSH-05 and DOC-06.',
      inputs:
        'The list of bank accounts and the bank transactions. If either is missing, the procedure waits for data.',
      logic: [
        {
          name: 'Confirmed balance agrees to the books',
          detail:
            "For each account the confirmed balance is compared with the period-end balance of the account's general ledger account; a difference is an exception, measured as confirmed minus book. Exception severity is medium. The confirmation record is updated with the book balance, the difference and whether they agree.",
        },
        {
          name: 'Accrued interest on the reply is booked',
          detail:
            'If the reply states accrued interest above the clearly trivial threshold and no journal line on any account in the window before period end has a debit or credit of that amount, it is an exception. Exception severity is medium. Reversed entries and the entries reversing them are left out.',
        },
        {
          name: 'Accounts selected for evaluation',
          detail:
            'Each account with either exception is judged once, together with the Bank statement to ledger (CSH-05) exceptions on that account. Accounts whose reply agrees and whose accrued interest is recorded are not judged.',
        },
        {
          name: 'Evaluation evidence',
          detail:
            "The reply (balance, balance date, accrued interest, how it was received and its reliability), the book balance and difference, the account's bank transactions after period end, and the entries on its bank ledger account just before period end, excluding reversed entries.",
        },
        {
          name: 'Evaluation of reconciling items',
          detail:
            'The confirmed balance must be at period end. Receipts and payments recorded on or before period end that cleared the bank after it are matched as deposits in transit or unpresented payments, on amount first and then date and reference, to see whether they explain the difference to the cent. A bank transaction after period end with no entry on or before it is not a reconciling item.',
        },
        {
          name: 'Recommended disposition',
          detail:
            'Closure is recommended where the balance date is correct, reconciling items fully explain the difference and no accrued interest issue remains. Recording errors or omitted accrued interest lead to a proposed misstatement. An incorrect balance date or unexplained difference without an identified error leads to a request for further evidence.',
        },
      ],
      result:
        'Each exception account receives a judgment requiring auditor review. Eligible judgments first undergo independent model review, with disagreements referred for resolution. Proposed misstatements include the account, direction and amount: balance errors as cash overstatement or understatement, and omitted accrued interest as understated interest income or expense. One draft is prepared per account; recognition requires an auditor decision. Confirmation marks the related confirmation, interest and CSH-05 exceptions as explained. Failed judgments permit a rerun or a manually prepared conclusion.',
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
        '评价收入合同中控制权转移及其他影响收入确认的条款，确定适用的履约与确认依据，为收入截止性判断（REV-02）提供支持。涉及发生及截止认定。',
      when: '收入发生或截止认定风险为中、高或特别风险时适用，在合同要素抽取（DOC-04）完成后执行。',
      logic: [
        { name: '合同范围确定', detail: '本项目的全部收入合同，每份判断一次。' },
        {
          name: '模型评价依据',
          detail:
            '合同要素抽取（DOC-04）抽出的合同对方、金额、签订日和各条款（带所在页），以及合同文件的可靠性。只依据抽出的条款，需要而未找到的条款记为局限。',
        },
        {
          name: '控制权转移条件评价',
          detail:
            '确定控制权是在某一时点转移（如客户签收、验收、服务完成），还是随履约在一段时间内转移，并指出决定它的条款。明确写出控制权、所有权或风险转移事件的条款即为转移时点，除非有其他条款与之矛盾。',
        },
        {
          name: '其他影响收入确认的条款',
          detail: '找出退货权、返利、验收条件、开票持有安排、寄售、担保和关联方等条款，在结论中列明，不改变建议。',
        },
        {
          name: '处理建议',
          detail: '能从条款确定转移时点或随时间转移的方式，建议接受；条款定不了或互相矛盾，建议补充证据。',
        },
      ],
      result:
        '每份合同形成一项判断，均提交审计师复核；模型建议限于接受或补充证据，不直接提出错报。全部合同处理完成后，REV-02 采用各合同当前有效结论；存在人工替代结论时，以人工结论为准。判断失败时支持重新执行或人工提交结果。',
    },
    en: {
      purpose:
        'Evaluates contractual terms governing transfer of control and other matters affecting revenue recognition, establishing the performance and recognition basis used by Revenue cut-off (REV-02). Assertions addressed are occurrence and cut-off.',
      when: 'Applies where revenue occurrence or cut-off risk is medium, high or significant, following Contract term extraction (DOC-04).',
      logic: [
        { name: 'Contract population', detail: 'Every revenue contract of the engagement, one judgment each.' },
        {
          name: 'Evaluation evidence',
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
          name: 'Recommended disposition',
          detail:
            'Acceptance is recommended where the terms determine the point or pattern of control transfer. Missing or conflicting terms lead to a request for further evidence.',
        },
      ],
      result:
        'Each contract receives a judgment requiring auditor review. Model recommendations are limited to acceptance or further evidence and do not directly propose a misstatement. REV-02 begins after all contracts are resolved and uses each contract’s current valid conclusion, including any auditor override. Failed judgments permit a rerun or manual submission of results.',
    },
  },
  'REV-02': {
    zh: {
      purpose:
        '对期末前入账且达到金额阈值的销售发票，以及三单匹配存在未解释例外的发票，评价收入确认期间是否与控制权转移时点一致，识别提前确认收入。涉及发生及截止认定。',
      when: '收入截止认定风险为中、高或特别风险时适用，在 REV-03 及 REV-08 完成后执行。期末前检查期间分别为 10、15、20 天，金额阈值为实际执行的重要性。',
      inputs: '销售发票。资料缺失时，程序暂停并等待补充。',
      logic: [
        {
          name: '评价对象确定',
          detail:
            '规则选出期末前窗口内入账、金额不低于实际执行的重要性的销售发票，再加上凭证三单匹配（REV-08）发现例外而尚未解释的发票，每张发票判断一次。',
        },
        {
          name: '模型评价依据',
          detail:
            '发票、发运单的发货日与签收日、该发票入账分录的全部行（带科目名称与类别）、所属合同及其条款、收入合同条款评价（REV-03）对该合同的结论，以及该发票上的三单匹配例外。合同评价结论只作参考，引用时以合同条款原文为准。',
        },
        {
          name: '确定控制权转移日',
          detail:
            '按合同的控制权转移条款确定转移事件，没有合同时按发运或服务完成记录确定；以签收、验收或服务完成为准的取签收日，以发货为准的取发货日。条款与合同评价结论不一致时以条款原文为准，并记为局限。',
        },
        {
          name: '收入归属期间评价',
          detail:
            '收入属于转移日所在的期间；随时间转移的服务按服务期间落在各期的部分判断。每条三单匹配例外都要在推理中回应，收款不符本身不构成截止错报。',
        },
        {
          name: '处理建议',
          detail:
            '转移日在期末或之前，建议接受；期末前入账而转移日在期末之后，建议认定为错报；缺发运或服务完成记录、缺转移日期、条款定不了转移时点，或一张发票涉及跨期的交付或服务，建议补充证据。',
        },
      ],
      result:
        '每张发票形成一项判断，均须审计师复核。拟认定错报时，收入高估金额取对应收入分录金额，不含 GST；不存在收入分录时取发票金额。登记错报须经审计师决定，并纳入 FIN-04 汇总。判断确认后，相关三单匹配例外标记为已解释；判断失败时支持重新执行或人工形成结论。',
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
        'Evaluates whether the recognition period of sales invoices agrees with the timing of control transfer, covering invoices meeting the period-end monetary threshold and invoices with unresolved matching exceptions. The procedure identifies premature revenue recognition and addresses occurrence and cut-off.',
      when: 'Applies where revenue cut-off risk is medium, high or significant, following REV-03 and REV-08. The respective period-end testing windows are 10, 15 and 20 days, with performance materiality as the monetary threshold.',
      inputs: 'Sales invoices. If they are missing, the procedure waits for data.',
      logic: [
        {
          name: 'Invoices selected',
          detail:
            'A rule selects sales invoices posted in the window before period end at or above performance materiality, and adds invoices behind Three-way match (REV-08) exceptions not yet explained. Each invoice is judged once.',
        },
        {
          name: 'Evaluation evidence',
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
          name: 'Recommended disposition',
          detail:
            'Acceptance is recommended where control transfers on or before period end. Recognition before period end with control transferring afterwards leads to a proposed misstatement. Missing delivery or service evidence, an undetermined transfer date or terms, or an invoice spanning periods leads to a request for further evidence.',
        },
      ],
      result:
        'Each invoice receives a judgment requiring auditor review. A proposed revenue overstatement uses the related revenue entry amount excluding GST, or the invoice amount where no revenue line exists. Recognition requires an auditor decision and feeds into FIN-04. Confirmation marks the related matching exceptions as explained; failed judgments permit a rerun or a manually prepared conclusion.',
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
        '对本期总账分录实施全量规则筛查，识别异常、非标准或可能涉及管理层凌驾于控制之上的分录。筛查结果为风险线索，不直接构成错报结论。涉及发生、完整性及准确性认定。',
      when: '启用分录测试功能时，对业务日期属于本期的全部分录执行筛查。载入审计更正分录后另行实施增量筛查。录入时间被声明为不可靠时，不执行周末录入、审计调整期间其他分录、非工作时间录入及期末后补记四类时间检查。',
      logic: [
        {
          name: '周末录入的重大收入',
          detail: '收入科目的贷方金额超过实际执行的重要性，且录入时间（当地时间）在周六或周日。例外严重程度为高。',
        },
        {
          name: '整数金额的调整分录',
          detail:
            '手工调整分录中，借方或贷方金额是整数（10,000.00 的倍数）且不低于实际执行的重要性；作废及其冲回分录不纳入。例外严重程度为中。',
        },
        {
          name: '恰低于审批限额的付款',
          detail:
            '贷记货币资金的付款凭证中，对方科目的借方金额不低于审批限额的 95%、但低于限额。客户资料未记录审批限额时无法执行。例外严重程度为高。',
        },
        {
          name: '审批缺失或自审自批',
          detail:
            '分录缺少审批人，或审批人与录入人为同一人。本期全部分录均缺少审批人记录时无法执行。例外严重程度为高。',
        },
        {
          name: '期末前三日冲销',
          detail: '业务日期在期末前三天至期末之间，摘要或单据号含“冲销”或 reverse、reversal 等字样。例外严重程度为高。',
        },
        {
          name: '审计调整期间的其他分录',
          detail:
            '首次向管理层发出错报沟通之后录入、业务日期在本期内的分录，审计更正分录除外。发出错报沟通之前不执行，因此仅在再次扫描更正凭证时执行，并查这段时间录入的全部其他分录。例外严重程度为高。',
        },
        {
          name: '非常规录入人',
          detail:
            '本期录入凭证不超过 2 笔的录入人，其录入的凭证全部标出。本期录入人少于两个时无法执行。例外严重程度为中。',
        },
        { name: '非工作时间录入', detail: '录入时间（当地时间）在早 7 点之前或晚 8 点及以后。例外严重程度为中。' },
        {
          name: '非标准分录',
          detail:
            '手工录入的调整分录，作废并冲回的分录和审计更正分录除外，金额超过明显微小错报临界值。例外严重程度为高。',
        },
        {
          name: '收入全额冲减',
          detail:
            '借记收入、贷记应收且不涉及货币资金的凭证，冲减的应收金额正好等于本期此前开具的某张发票的全额。只能按金额匹配，同额发票全部列出，由分录例外归因（GEN-02）判断冲的是哪张。金额超过明显微小错报临界值。例外严重程度为中。',
        },
        {
          name: '作废后重记且对方名称改变',
          detail:
            '期内一笔凭证被作废冲回后，同一天有一笔科目和金额完全相同的重记，但对方名称不同（有同名重记的视为正常更正）。数据集没有声明作废冲回的写法时不执行。金额超过明显微小错报临界值。例外严重程度为中。',
        },
        {
          name: '疑似重复开票',
          detail:
            '同一客户、同一开票日、同一金额的本期发票有两张以上，其中没有发运或服务完成记录的每张标出。本期发票都没有发运或服务完成记录时无法执行。金额超过明显微小错报临界值。例外严重程度为高。',
        },
        {
          name: '期末后补记',
          detail: '业务日期在本期内，录入日（当地时间）却在期末之后，审计更正分录除外。例外严重程度为中。',
        },
      ],
      result:
        '规则命中结果形成例外，同一规则对每笔凭证最多记录一项；疑似重复开票按发票记录。GEN-02 按凭证评价尚未归因的例外，本程序不直接生成复核任务。跳过或无法执行的检查及其原因保留于执行记录。',
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
        'Screens the full population of current-period journal entries for unusual, non-standard or potentially control-overriding activity. Flags are risk indicators rather than misstatement conclusions. Assertions addressed are occurrence, completeness and accuracy.',
      when: 'Where journal entry testing is enabled, screens all entries with transaction dates in the current period and performs an incremental scan after audit adjustments are loaded. If entry timestamps are declared unreliable, the weekend, other entries during audit adjustment, out-of-hours and post-period entry checks are skipped.',
      logic: [
        {
          name: 'Large revenue entered at a weekend',
          detail:
            'A credit to a revenue account above performance materiality, entered on a Saturday or Sunday (local time). Exception severity is high.',
        },
        {
          name: 'Round-sum adjusting entries',
          detail:
            'A manual adjusting entry with a debit or credit that is a round sum (a multiple of 10,000.00) and not below performance materiality; reversed and reversing pairs are excluded. Exception severity is medium.',
        },
        {
          name: 'Payments just below the approval limit',
          detail:
            'A payment crediting cash whose other side is a debit of at least 95% of the approval limit but below it. Not executable when the client profile has no approval limit. Exception severity is high.',
        },
        {
          name: 'Missing or self approval',
          detail:
            'The entry has no approver, or was approved by the person who entered it. Not executable when no entry in the period records an approver. Exception severity is high.',
        },
        {
          name: 'Reversals in the last three days',
          detail:
            'An entry dated from three days before period end to period end whose description or source document mentions a reversal. Exception severity is high.',
        },
        {
          name: 'Other entries during the audit adjustment period',
          detail:
            'An entry dated in the period but entered after the first misstatement communication to management, other than the auditor’s correcting entries. It does not run before that communication, so in practice it runs only when the correcting entries are rescanned, and then covers every other entry made in that time. Exception severity is high.',
        },
        {
          name: 'Infrequent preparers',
          detail:
            'All entries of a preparer who entered no more than 2 entries in the period. Not executable when fewer than two people entered entries. Exception severity is medium.',
        },
        {
          name: 'Out-of-hours entries',
          detail: 'Entered before 7 am or from 8 pm onwards (local time). Exception severity is medium.',
        },
        {
          name: 'Non-standard entries',
          detail:
            'A manual adjusting entry above the clearly trivial threshold, excluding reversed and reversing pairs and the auditor’s correcting entries. Exception severity is high.',
        },
        {
          name: 'Revenue credited in full',
          detail:
            'An entry that debits revenue and credits receivables without touching cash, where the receivable credited equals the full amount of an invoice issued earlier in the period. Only amounts can be matched, so every invoice of that amount is listed for journal entry exception attribution (GEN-02) to identify. Above the clearly trivial threshold. Exception severity is medium.',
        },
        {
          name: 'Voided and reposted under a different name',
          detail:
            'An entry voided by reversal in the period is reposted on the same date with identical accounts and amounts but a different counterparty name (a repost under the same name is treated as a normal correction). Does not run when the dataset does not declare how reversals are referenced. Above the clearly trivial threshold. Exception severity is medium.',
        },
        {
          name: 'Possible duplicate invoices',
          detail:
            'Two or more invoices in the period to the same customer, on the same date, for the same amount; each one without a delivery or service completion record is flagged. Not executable when no invoice in the period has such a record. Above the clearly trivial threshold. Exception severity is high.',
        },
        {
          name: 'Entries made after period end',
          detail:
            'Dated in the period but entered after period end (local date), other than the auditor’s correcting entries. Exception severity is medium.',
        },
      ],
      result:
        'Rule matches produce exceptions, limited to one per rule and journal, except suspected duplicate billing, which is recorded by invoice. GEN-02 evaluates unresolved exceptions by journal. This procedure does not directly create review tasks. Execution records retain the reasons for skipped or unavailable checks.',
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
        '评价全量分录扫描（GEN-05）识别的异常凭证，结合交易依据及相关风险判断会计处理是否适当、是否构成错报，或是否需要追加审计证据。涉及管理层凌驾于控制之上的风险及发生、完整性、准确性认定。',
      when: '启用分录测试功能时，在 GEN-05 完成后评价尚未归因的例外；不存在待评价例外时不调用模型。',
      logic: [
        { name: '评价对象确定', detail: '把全量分录扫描（GEN-05）尚未归因的例外按凭证归组，每笔凭证判断一次。' },
        {
          name: '模型评价依据',
          detail:
            '本凭证的全部分录行、涉及科目的本期和上期余额；凭证涉及货币资金时另给凭证日前后 3 天的银行对账单行；例外指向的其他凭证（原凭证、冲回、重记、贷项凭证）、发票及其发运或服务完成记录和合同、与本凭证关联的文件，以及计划阶段识别的舞弊风险。',
        },
        {
          name: '异常原因与依据评价',
          detail:
            '逐项评价规则例外能否得到合理解释，分析分录是否属于手工、非标准、冲回、重记或冲减交易，并核查金额、日期及交易对方与原始依据的一致性。重记后交易对方发生变化须有解释；仅按金额匹配的冲减须进一步确定对应发票及会计处理的适当性。',
        },
        {
          name: '舞弊风险考虑',
          detail:
            '考虑该分录能否被用来凌驾于控制之上，或错报舞弊风险涉及的科目。缺少解释列为评价局限，不据此直接认定舞弊。',
        },
        {
          name: '处理建议',
          detail:
            '已取得充分解释及支持依据，或已由具有依据的分录全额冲回时，建议接受；存在无依据确认的收入、费用或资产时，建议认定错报并列明科目、认定、方向及不含 GST 的金额；证据不足时提出补充资料要求。',
        },
      ],
      result:
        '每笔凭证形成一项判断，直接提交审计师逐项复核，不经独立模型复核。处理方式包括确认、推翻或认定错报；确认后，将判断解释的例外标记为已归因。判断失败时，例外保持未归因状态，并支持重新执行、人工形成结论或登记错报。',
      project: (p) => {
        const days = num(p, 'bank_window_days')
        if (days == null) return null
        return `凭证涉及货币资金时，取凭证日前后各 ${days} 天（含两端）的银行对账单行。`
      },
    },
    en: {
      purpose:
        'Evaluates journals flagged by Full journal entry scan (GEN-05) against supporting evidence and assessed risks to determine whether the accounting is appropriate, a misstatement exists or further evidence is required. The procedure addresses management override and occurrence, completeness and accuracy.',
      when: 'Where journal entry testing is enabled, evaluates unattributed exceptions after GEN-05. No model call is made when no exceptions remain for evaluation.',
      logic: [
        {
          name: 'Evaluation population',
          detail:
            'Groups the unexplained exceptions from the full journal entry scan (GEN-05) by entry and assesses each entry once.',
        },
        {
          name: 'Evaluation evidence',
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
          name: 'Recommended disposition',
          detail:
            'Acceptance is recommended for supported, explained entries or entries fully reversed by supported transactions. Unsupported recognition leads to a proposed misstatement identifying the affected revenue, expense or asset account, assertion, direction and amount excluding GST. Insufficient evidence leads to a specified evidence request.',
        },
      ],
      result:
        'Each journal receives a judgment for direct auditor review without independent model review. Available dispositions include confirmation, override and misstatement recognition. Confirmation attributes the explained exceptions to the judgment. Failed judgments leave exceptions unattributed and permit a rerun, a manual conclusion or misstatement recognition.',
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
        '评价股权及任职关系识别的公司是否构成关联方，核对管理层申报，并分析本期及期后交易和条款，识别未申报关联关系与交易。主要涉及完整性认定。',
      when: '启用关联方功能时，在 GEN-08 及 DOC-04 完成后执行，交易检索期间覆盖本期及期末后 30 天。',
      logic: [
        { name: '评价对象确定', detail: '关系图中被审计单位以外的每一家公司，各判断一次。' },
        {
          name: '交易检索',
          detail:
            '按去除 Pty Ltd 等后缀的公司名称，检索本期及期后分录、银行流水和销售发票，发票另按 ABN 匹配，作废及冲回分录除外。该公司直接持有被审计单位股份时，另取得同期权益科目分录。',
        },
        {
          name: '模型评价依据',
          detail:
            '登记摘录中该公司的持股和任职关系、相关人员与被审计单位的关系（不含出生日期）、管理层申报清单中对应的一项、找到的往来、与它的合同和关联文件，以及关系图对它标出的例外。',
        },
        {
          name: '关联关系与交易评价',
          detail:
            '依据适用财务报告框架的关联方定义，评价控制、共同控制、重大影响或共同关键管理人员关系，并与管理层申报核对。未申报事项列为新识别关联方，交易分析列明期间、金额及条款，并评价是否属于正常经营活动。',
        },
        {
          name: '处理建议',
          detail:
            '建议接受（不是关联方，或已申报且交易与申报相符），或补充证据（未申报的关联方、关系无法确定，或交易及条款需要进一步取证）。本程序不直接提出错报，关联方的会计处理和披露在之后评价。',
        },
      ],
      result:
        '每家公司形成一项判断，直接提交审计师逐项复核，不经独立模型复核。可确认、推翻或认定错报；确认后，对应关系图例外标记为已归因。判断失败时生成失败任务，支持重新执行或人工提交结果。',
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
        'Evaluates whether companies identified through ownership and officer relationships are related parties, reconciles them to management declarations and analyses current-period and subsequent transactions and terms. The procedure primarily addresses completeness of related-party identification.',
      when: 'Where related-party procedures are enabled, runs after GEN-08 and DOC-04. Transaction searches cover the current period and 30 days after period end.',
      logic: [
        {
          name: 'Evaluation population',
          detail: 'Every company on the map other than the audited entity, assessed one at a time.',
        },
        {
          name: 'Transaction search',
          detail:
            'Searches journal entries, bank transactions and sales invoices in and after the period for the company’s name, ignoring suffixes such as Pty Ltd; invoices also match on ABN, and reversed pairs are excluded. If the company holds shares in the audited entity directly, equity entries for the same window are added.',
        },
        {
          name: 'Evaluation evidence',
          detail:
            'The company’s ownership and office links from the registry extracts, the linked people’s relationships with the audited entity (without dates of birth), the matching item in management’s declaration, the dealings found, contracts and linked documents, and the exceptions the map raised on the company.',
        },
        {
          name: 'Relationship and transactions',
          detail:
            'Applies the related party definition of the client’s financial reporting framework: control, joint control, significant influence or key management personnel in common. Compares with management’s declaration, treating an undeclared related party as newly identified, and describes the amounts, dates and terms of transactions in and after the period and whether they are in the normal course of business.',
        },
        {
          name: 'Recommended disposition',
          detail:
            'Acceptance is recommended where no related-party relationship exists, or where the relationship and transactions agree with management’s declaration. Undeclared or uncertain relationships and transactions requiring additional support lead to a request for further evidence. The procedure does not itself propose misstatements; related accounting and disclosures are evaluated subsequently.',
        },
      ],
      result:
        'Each company receives a judgment for direct auditor review without independent model review. Dispositions include confirmation, override and misstatement recognition. Confirmation attributes the corresponding relationship-graph exceptions to the judgment. Failed judgments create a failure task permitting a rerun or manual submission of results.',
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
        '利用 ABN 与 ASIC 登记信息追溯法人股东，并检索董事、秘书的其他任职及持股关系，形成股权与关联关系图，识别管理层申报清单以外的关联方候选。主要涉及完整性认定。',
      when: '启用关联方功能时，在执行阶段开展，无前置程序且不调用模型。ASIC 摘录新增或修订后，原结果标记为过期；重新执行生成关系图新版本。',
      logic: [
        {
          name: '被审计单位识别',
          detail:
            '根据客户 ABN 通过 ABN Lookup 取得 ACN，并获取对应 ASIC 公司摘录。未取得记录或客户无 ACN 时，程序执行失败。',
        },
        {
          name: '法人股东穿透',
          detail:
            '对持股不低于 20% 的法人股东逐层获取登记摘录，最多追溯 3 层；本程序将其作为控制或重大影响关系线索。持股低于该比例的法人股东列入关系图，但不继续追溯。',
        },
        {
          name: '高管关联检索',
          detail:
            '对被审计单位及母公司链中出生日期已知的董事和秘书开展人员检索，取得其其他任职或持股公司的摘录并纳入关系图。检索限一轮，不进一步扩展新识别公司的人员。',
        },
        {
          name: '未申报关联方候选',
          detail:
            '关系图中被审计单位以外的公司不在管理层申报的关联方或股东清单中（按 ACN 比对，没有 ACN 时按名称比对）。例外严重程度为高。',
        },
        {
          name: '穿透限制',
          detail:
            '未取得公司摘录、缺少 ACN、超过追溯层数，或缺少高管人员检索来源时，记录穿透限制。已识别公司仍纳入关系图，但停止相应路径扩展。例外严重程度为中。',
        },
      ],
      result:
        '生成带版本记录的股权与关联关系图，并归档取得的登记摘录。未申报候选及穿透受限事项形成例外，由 GEN-03 逐家公司评价，不直接生成复核任务。成功查询结果可复用，重新执行时仅补充未成功的查询；整体执行失败时提交失败任务。',
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
        'Traces corporate shareholders and the other appointments and holdings of directors and secretaries using ABN and ASIC records. The resulting ownership and relationship graph identifies potential related parties absent from management declarations, primarily addressing completeness.',
      when: 'Where related-party procedures are enabled, runs during fieldwork without prerequisites or model calls. New or amended ASIC extracts mark existing results as out of date; a rerun creates a revised relationship graph.',
      logic: [
        {
          name: 'Entity identification',
          detail:
            'Looks up the client profile’s ABN in ABN Lookup to get the ACN, then obtains the entity’s ASIC company extract. If the ABN is not found or the client is not a company (no ACN), the procedure fails.',
        },
        {
          name: 'Corporate shareholder tracing',
          detail:
            'Registry extracts are obtained for successive corporate shareholders holding at least 20%, up to three levels. The threshold identifies potential control or significant-influence relationships for evaluation. Corporate shareholders below the threshold remain on the graph without further tracing.',
        },
        {
          name: 'Officer relationship search',
          detail:
            'Runs a person search on the directors and secretaries of the entity and its parent chain whose dates of birth are known, and adds the other companies where they hold office or shares, with their extracts. There is one round only: people in the newly found companies are not searched.',
        },
        {
          name: 'Undeclared related party candidate',
          detail:
            'A company on the map, other than the audited entity, is not in management’s declared related parties or shareholders (matched by ACN, or by name where there is no ACN). Exception severity is high.',
        },
        {
          name: 'Tracing limitations',
          detail:
            'A company found has no extract, has no ACN or lies beyond the level limit, or its officers could not be searched because there is no person search source (as for a newly created client). Such companies still go on the map but are not traced further. Exception severity is medium.',
        },
      ],
      result:
        'Produces a versioned ownership and relationship graph and archives obtained registry extracts. Undeclared candidates and tracing limitations create exceptions for company-level evaluation by GEN-03, without direct review tasks. Successful queries are retained for reuse; reruns retry unsuccessful queries. A failed overall execution creates a failure task.',
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
        '评价期后监控（GEN-07）识别的交易是否反映期末已存在的情况，区分调整事项、非调整事项及新期间常规交易，并识别尚未反映于期末余额的调整事项。涉及完整性、截止及计价认定。',
      when: '启用持续经营功能时，在 GEN-07 完成后评价尚未归因的例外；不存在待评价事项时不调用模型。',
      logic: [
        { name: '评价对象确定', detail: '期后监控（GEN-07）标出、尚未归因的每一笔银行流水或分录行，各判断一次。' },
        {
          name: '相关凭证检索',
          detail:
            '依据流水参考号中含数字且不少于 4 个字符的词项、交易对方或分录单据号，检索本期起至期后窗口截止的总账，并取得相关销售发票及发运或服务完成记录，作废及冲回分录除外。',
        },
        {
          name: '模型评价依据',
          detail:
            '标出的这一行（分录另给整笔凭证）、找到的相关凭证和发票、涉及科目的期末余额、关联的已登记文件，以及实际执行的重要性和明显微小错报临界值。',
        },
        {
          name: '期后事项分类',
          detail:
            '比较交付或义务发生、开票、入账、协议及收付款日期与期末日。反映期末已存在情况的列为调整事项，期后新发生情况列为非调整事项；与期末状况无关的列为常规交易。调整事项核查期末余额是否已反映，非调整事项评价是否具有披露重要性。',
        },
        {
          name: '处理建议',
          detail:
            '常规交易、非调整事项或期末已反映的调整事项建议接受；期末尚未反映的调整事项建议认定错报并附草稿；交易性质或期末状况无法确定时，建议补充证据。',
        },
      ],
      result:
        '每笔候选流水或分录行形成一项判断。接受建议经 GEN-09 独立复核同意后可自动关闭，并完成例外归因；错报建议、补充证据要求、复核失败及分歧均提交审计师处理。启用自主度调整时，金额、置信度、推理支持或证据质量触及降级条件的判断亦须人工复核。判断失败时，相关例外保持未归因状态。',
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
        'Evaluates transactions flagged by Subsequent event monitoring (GEN-07) to distinguish adjusting events, non-adjusting events and routine transactions of the new period. Adjusting events omitted from period-end balances may give rise to misstatements. Assertions addressed are completeness, cut-off and valuation.',
      when: 'Where the Going concern feature is enabled, evaluates unattributed exceptions following GEN-07. No model call is made when no items remain for evaluation.',
      logic: [
        {
          name: 'Evaluation population',
          detail:
            'Each bank transaction or journal line flagged by subsequent events monitoring (GEN-07) and not yet explained, assessed one at a time.',
        },
        {
          name: 'Related record search',
          detail:
            'Uses the transaction’s reference numbers (words of at least 4 characters containing a digit) and counterparty name, or the entry’s source document number, to find related entries posted from the start of the period to the end of the subsequent window, with the related sales invoices and their delivery or service completion records; reversed pairs are excluded.',
        },
        {
          name: 'Evaluation evidence',
          detail:
            'The flagged line (with the whole entry for a journal line), the related entries and invoices found, the period-end balances of the accounts involved, linked registered documents, and performance materiality and the clearly trivial threshold.',
        },
        {
          name: 'Type of event',
          detail:
            'Compares the dates of delivery or obligation, invoicing, posting, agreements and cash movement with period end: conditions that existed at period end make an adjusting event, conditions arising afterwards a non-adjusting event, and anything unrelated to the period-end position an ordinary transaction. For an adjusting event it checks whether the period-end balances reflect it; for a non-adjusting event, whether it is material enough to disclose.',
        },
        {
          name: 'Recommended disposition',
          detail:
            'Acceptance is recommended for routine transactions, non-adjusting events or adjusting events already reflected at period end. An omitted adjusting event leads to a proposed misstatement. Uncertain transaction substance or period-end conditions lead to a request for further evidence.',
        },
      ],
      result:
        'Each candidate bank transaction or journal line receives a judgment. An acceptance recommendation may close automatically after agreeing independent review by GEN-09, attributing the related exceptions. Misstatement recommendations, evidence requests, failed reviews and disagreements require auditor action. Where autonomy adjustment is enabled, monetary, confidence, reasoning-support or evidence-quality downgrade conditions also require manual review. Failed judgments leave the exceptions unattributed.',
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
        '筛查期后银行流水及总账分录中的重大未入账交易、非常规分录与资本性支出，识别需要进一步评价的期后事项候选。主要涉及完整性认定；规则命中不直接构成事项性质或错报结论。',
      when: '启用持续经营功能时执行规则筛查，不调用模型。检查期间为期末次日起 31 天；实际资料截止日早于该日期时，以资料截止日为限。',
      logic: [
        {
          name: '期后未入账的大额流水',
          detail:
            '窗口内金额（不论收付）超过重要性水平的银行流水，在流水日前后 10 天内，该账户对应的总账银行科目里未找到同方向、同金额的分录。例外严重程度为中。',
        },
        {
          name: '期后非常规分录',
          detail:
            '窗口内的凭证，排除涉及银行科目的、常规赊销和赊购过账，以及各科目净额都为零的；取其中资产或负债科目金额最大的一行，超过明显微小错报临界值即标出。例外严重程度为高。',
        },
        {
          name: '期后资本性支出',
          detail: '窗口内借记不动产、厂场和设备科目，单行借方超过重要性水平的分录行。例外严重程度为中。',
        },
      ],
      result:
        '各项规则命中分别形成例外，作废及其冲回分录不计入筛查。例外作为期后事项候选提交 GEN-06 逐项评价，本程序不直接生成复核任务。',
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
        'Screens subsequent bank transactions and journal entries for material unrecorded transactions, non-routine entries and capital expenditure requiring further evaluation. The procedure primarily addresses completeness; a rule match does not itself establish an event classification or misstatement.',
      when: 'Where the Going concern feature is enabled, performs rule-based screening without a model call. The review window extends 31 days from the day after period end, limited to the available data cutoff if earlier.',
      logic: [
        {
          name: 'Large unrecorded bank transactions',
          detail:
            'A bank transaction in the window above overall materiality (receipt or payment) with no ledger line of the same direction and amount on that account’s bank ledger account within 10 days either side. Exception severity is medium.',
        },
        {
          name: 'Unusual entries after period end',
          detail:
            'Entries in the window, excluding those touching a bank account, routine credit sales and credit purchases, and entries that net to zero on every account; the largest asset or liability line of each is flagged if above the clearly trivial threshold. Exception severity is high.',
        },
        {
          name: 'Capital expenditure after period end',
          detail:
            'A debit to a property, plant and equipment account in the window above overall materiality. Exception severity is medium.',
        },
      ],
      result:
        'Rule matches are recorded as exceptions, excluding voided entries and their reversals. Exceptions are subsequent-event candidates for individual evaluation by GEN-06 and do not directly generate review tasks.',
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
        '通过独立模型对原判断引用的证据重新评价，再与原结论比较，识别结论或推理分歧。复核模型在独立评价阶段不接收原结论，以降低原判断对复核的影响。',
      when: '作为判断程序的嵌入式复核，不单独调度。独立复核功能默认启用；功能关闭或未配置复核模型时标记为未复核。自主度为逐条决定或逐条复核的判断直接提交审计师，其他判断按复核条件选择。',
      logic: [
        {
          name: '复核对象选择',
          detail:
            '按以下顺序适用复核条件：拟自动接受或关闭的判断、推理包含假设步骤的判断、其余判断按编号确定性抽取 5%。重放保持抽取结果一致。启用智能体协作时，另纳入证据全部为内部副本或推理存在未获证据支持步骤的判断。',
        },
        {
          name: '独立评价',
          detail:
            '复核模型仅接收原判断引用的数据行、文件页面、准则原文及客户资料字段，不接收原结论、推理链或建议，独立形成结论及推理链并适用相同校验。人员出生日期不传入模型。',
        },
        {
          name: '结论一致性评价',
          detail:
            '独立评价完成后，再提供原结论及建议进行比较。措辞或详略差异不单独构成分歧；事项、金额、期间或处理方式不同构成分歧，并区分证据不足、结论差异或推理错误。',
        },
      ],
      result:
        '复核结果附于原判断，不单独编制底稿。一致且独立推理链校验通过时，按原程序规则处理并计入置信度评价；不一致时提交审计师裁决。可维持原结论、采用有效独立结论或由审计师另行形成结论。复核未完成或校验失败时转人工复核。启用智能体协作后，同时保留复核建议及错报草稿，并按采用的建议校验错报登记要求。',
    },
    en: {
      purpose:
        'Uses a separate model to reassess the evidence cited by an original judgment and then compare conclusions, identifying differences in conclusions or reasoning. The independent assessment withholds the original conclusion to reduce its influence on the review.',
      when: 'Operates as an embedded judgment review rather than a separately scheduled procedure. Independent review is enabled by default; disabling it or omitting the reviewer model records the judgment as unreviewed. Judgments requiring individual decisions or reviews go directly to the auditor; others are selected under the review criteria.',
      logic: [
        {
          name: 'Judgment selection',
          detail:
            'Checked in order, and the first match triggers a review: a judgment that proposes to accept or close and would therefore close automatically; reasoning that includes an assumption step; otherwise a random 5% sample by judgment ID, which gives the same result on replay. With the "Agent collaboration" feature on, judgments relying only on internal copies or with unsupported reasoning steps are also reviewed.',
        },
        {
          name: 'Independent assessment',
          detail:
            'The review model receives only the evidence the judgment cites (data rows, document pages, standard text, client profile fields), not its conclusion, reasoning or proposal, and reaches its own conclusion and reasoning, which must pass the same checks as the original. Dates of birth are not sent to the model.',
        },
        {
          name: 'Conclusion comparison',
          detail:
            'Given the original conclusion and proposal, it decides whether the two agree: differences in wording or detail do not count, but a different finding, amount, period or treatment does, and the reason is given as insufficient evidence, a different conclusion or a reasoning error.',
        },
      ],
      result:
        'The review is attached to the original judgment without a separate workpaper. Agreement with validated independent reasoning contributes to confidence and follows the original procedure’s disposition rules. Disagreement requires auditor resolution: retain the original conclusion, adopt a valid independent conclusion or form a separate conclusion. Incomplete or invalid reviews require manual review. Agent collaboration additionally retains the reviewer’s recommendation and misstatement draft and applies recognition requirements for the adopted recommendation.',
    },
  },
  'FIN-03': {
    zh: {
      purpose:
        '对已认定错报区分事实错报与判断错报，评价舞弊迹象，并记录对应审计目标及期间来源，为管理层沟通和未更正错报评价（FIN-01）提供分类依据。',
      when: '启用错报闭环功能时，在执行阶段程序及复核完成后，对尚未分类的已认定错报执行分类；完成阶段新增错报时再次执行。无待分类事项时不调用模型。',
      logic: [
        {
          name: '审计目标关联',
          detail: '按规则选科目名完整出现在错报科目中、且认定相同的审计目标；未找到时留空。这一项不经模型。',
        },
        {
          name: '期间来源',
          detail: '一律记为本期发生：本系统按首次审计处理，没有上期结转的未更正错报。这一项不经模型。',
        },
        {
          name: '模型评价依据',
          detail:
            '错报本身、认定它的那条判断的结论与证据、来源凭证或单据对应的分录行、该判断引用的其他记录与文件，以及风险清单中的舞弊风险。',
        },
        {
          name: '错报类型',
          detail:
            '金额由已入账交易、单据、回函或法定、合同费率确定，不存在判断不确定性的，为事实错报；源于管理层的会计估计、确认、计量、列报或会计政策选择不合理的，为判断错报。本系统未实施审计抽样，因此不形成推断错报分类。',
        },
        {
          name: '舞弊迹象',
          detail:
            '资料显示可能存在故意行为（隐瞒交易、缺乏商业理由、抵销另一笔错报，或与风险清单中的舞弊风险相关）时，标为有舞弊迹象，并必须写明理由。不单凭常规调整遗漏、计算错误或已公开说明的分录认定舞弊迹象；手工分录未经独立复核这类控制缺陷，只在结论中作为控制缺陷线索提及。',
        },
      ],
      result:
        '每条错报形成一项分类判断，均须审计师复核。确认后更新类型、舞弊迹象及理由、审计目标和期间来源，错报状态保持已认定并转入 FIN-08 沟通。审计师可替代分类；新增错报建议另行处理。判断失败时支持重新执行或人工分类。',
    },
    en: {
      purpose:
        'Classifies recognised misstatements as factual or judgmental, evaluates fraud indicators and records the related audit objective and period of origin, supporting management communication and Evaluation of uncorrected misstatements (FIN-01).',
      when: 'Where the misstatement lifecycle is enabled, classifies recognised but unclassified misstatements after fieldwork and reviews are resolved. New misstatements identified during completion return for classification. No model call is made without items requiring classification.',
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
          name: 'Evaluation evidence',
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
        'Each misstatement receives a classification judgment requiring auditor review. Confirmation updates the type, fraud indicator and rationale, audit objective and period of origin. The misstatement remains identified and proceeds to FIN-08 communication. Auditor overrides may replace the classification; further proposed misstatements are handled separately. Failed judgments permit a rerun or manual classification.',
    },
  },
  'FIN-08': {
    zh: {
      purpose:
        '记录已分类错报与管理层的沟通、更正要求及逐项回复。管理层同意更正的事项转入更正跟踪，拒绝更正的事项保留回复并列为未更正错报。',
      when: '启用错报闭环功能时，在 FIN-03 分类结果经审计师确认后执行。FIN-09 识别更正异常时，对相关错报重新沟通。无待沟通事项时不生成任务。',
      logic: [
        {
          name: '沟通材料',
          detail:
            '系统列出每条待沟通错报的科目、认定、金额、高估或低估、类型、舞弊迹象和来源判断的结论，并附一段更正请求：要求管理层更正所列错报，并说明不予更正的理由。',
        },
        {
          name: '管理层回复登记',
          detail:
            '每项待沟通错报须对应一条完整回复记录，包括沟通时间、对象及职务、回复时间、回复原文及更正意向。另记录执行日期，本程序不强制附列证据文件。',
        },
        { name: '沟通与回复时点核验', detail: '回复时间不能早于沟通时间，两者都不能晚于提交时刻。' },
        {
          name: '同意更正',
          detail:
            '管理层同意更正后，错报转为等待更正。客户已过账时，审计师提交对应错报及调整分录，载入后进入 FIN-09；终止更正跟踪时，将所选事项记录为未更正。',
        },
        {
          name: '拒绝更正',
          detail: '错报记为“未更正”，依据为管理层拒绝更正，回复原文随错报保留，直接计入错报累计（FIN-04）。',
        },
      ],
      result:
        '本程序通过人工任务记录执行结果。各项错报生成包含沟通时间、沟通对象及回复内容的新版本，可在错报与审计意见页查看。存在待更正事项时暂停等待客户更正；全部拒绝更正时进入 FIN-04 汇总。',
    },
    en: {
      purpose:
        'Documents communication of classified misstatements to management, correction requests and individual responses. Agreed corrections enter follow-up tracking; refusals retain management’s response and are classified as uncorrected.',
      when: 'Where the misstatement lifecycle is enabled, follows auditor confirmation of FIN-03 classifications. FIN-09 correction mismatches initiate renewed communication for affected misstatements. No task is created without items requiring communication.',
      logic: [
        {
          name: 'Material for the discussion',
          detail:
            'The communication pack lists each misstatement’s account, assertion, amount, direction, type, fraud indicator and source conclusion, together with a request for correction and management’s reasons for any refusal.',
        },
        {
          name: 'Management response records',
          detail:
            'Each communicated misstatement requires one complete response record containing the communication time, management contact and position, response time, verbatim response and correction intention. The execution date is also recorded; evidence file attachment is not mandatory.',
        },
        {
          name: 'Date checks',
          detail:
            'The response must not precede the communication, and neither date may be later than the submission time.',
        },
        {
          name: 'Agreement to correct',
          detail:
            'Management agreement moves the misstatement to Awaiting correction. After posting, the auditor submits the corresponding items and adjustment entries for loading and FIN-09 verification. Ending correction follow-up records the selected items as uncorrected.',
        },
        {
          name: 'Refuses to correct',
          detail:
            'The misstatement is recorded as Uncorrected because management refused to correct it, keeps the reply verbatim and goes straight into misstatement accumulation (FIN-04).',
        },
      ],
      result:
        'Execution results are recorded through a manual task. Each misstatement receives a new version containing communication dates, the management contact and the response, available on the Misstatements and opinion page. Pending agreed corrections pause the workflow; where all corrections are refused, FIN-04 aggregation follows.',
    },
  },
  'FIN-09': {
    zh: {
      purpose:
        '在管理层过账审计调整后，核验调整分录与原错报的对应关系、更正方向及金额，并评价对其他科目的影响，确定更正是否完整及是否需要再次沟通。',
      when: '启用错报闭环功能时，在审计师提交客户已过账调整及对应错报后执行。系统先载入调整分录并将错报转为验证更正中；同时启用分录测试时，先经 GEN-05 增量筛查，再一并评价相关例外。',
      logic: [
        { name: '更正分录范围确定', detail: '取凭证号以约定前缀开头的全部分录行，作为审计更正分录。' },
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
          name: '更正异常识别',
          detail:
            '没有匹配行、更正方向做反，或更正金额超过错报金额时，记一条例外，严重程度中，验证结果为须与管理层重新沟通。',
        },
        {
          name: '更正适当性评价',
          detail:
            '模型读错报、来源判断的结论、匹配凭证的全部行和规则算出的金额，判断是否完整更正（须与剩余额为零一致），并列出更正分录还涉及的其他科目，说明影响是否恰当，例如冲回原分录的其他行或相关税额。',
        },
        {
          name: '更正造成新错报',
          detail: '更正分录本身使某个科目出现错报（包括更正超额）时，模型建议认定为新的错报。',
        },
      ],
      result:
        '每条错报形成一项验证判断，均须审计师复核。确认后按剩余额分别记录为已更正或部分更正，剩余错报纳入 FIN-04；未能匹配或更正异常的事项返回等待更正并重新沟通。验证结论可由审计师替代。调整导致新错报时，先转入 FIN-03 分类。',
      project: (p) => {
        const prefix = str(p, 'adjustment_prefix')
        if (prefix == null) return null
        return `本项目的审计更正分录按凭证号前缀“${prefix}”识别。`
      },
    },
    en: {
      purpose:
        'After management posts audit adjustments, verifies their correspondence to the original misstatement, direction and amount, and evaluates effects on other accounts to determine whether correction is complete or further communication is required.',
      when: 'Where the misstatement lifecycle is enabled, follows submission of management’s posted adjustments and the related misstatements by the auditor. Adjustments are loaded and items marked as undergoing correction verification. Where journal testing is also enabled, GEN-05 first screens the adjustments and its exceptions are included in the evaluation.',
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
        'Each misstatement receives a verification judgment requiring auditor review. Confirmation records corrected or partially corrected status according to the residual, with any residual included in FIN-04. Unmatched or inappropriate corrections return to correction follow-up and renewed communication. Auditor conclusions may replace the verification result. New misstatements arising from adjustments first enter FIN-03 classification.',
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
        '编制未更正错报汇总表，按更正状态确定剩余金额，区分明显微小事项，并将累计金额与整体重要性比较，形成供 FIN-01 评价的系统候选意见。',
      when: '适用于全部项目。启用错报闭环时，在错报更正状态结清后执行，每轮回流重新汇总；未启用时，在执行阶段结束后按未更正金额汇总。FIN-01 退回时形成新版本。',
      logic: [
        { name: '未更正金额确定', detail: '未更正的错报取全额，部分更正的取剩余额，已更正的只列出、不累计。' },
        {
          name: '明显微小事项区分',
          detail:
            '未更正金额超过明显微小错报临界值的计入汇总表，列明错报方向及期间来源；等于或低于临界值的单独列出，不累计。',
        },
        { name: '合计', detail: '把计入的金额直接相加，多计和少计不相互抵销。' },
        {
          name: '系统候选意见',
          detail:
            '累计金额达到或超过整体重要性时，系统候选为保留意见；否则为无保留意见。该比较仅形成系统建议，最终意见类型由审计师在 FIN-01 确定。',
        },
        {
          name: '未结清事项检查',
          detail:
            '存在已认定、等待更正或验证更正中的错报时，不生成汇总表，并提交执行失败任务。正常流程要求在本阶段汇总前结清更正状态。',
        },
      ],
      result:
        '形成新版未更正错报汇总表，列示累计事项、明显微小事项、已更正事项、累计金额、重要性及候选意见。本程序自动完成，不单独生成复核任务；执行失败时生成可重新执行的失败任务。汇总表提交 FIN-01 评价。',
      project: (p, currency = 'AUD') => {
        const materiality = num(p, 'materiality_cents')
        const trivial = num(p, 'clearly_trivial_cents')
        if (materiality == null || trivial == null) return null
        return `重要性水平 ${money(materiality, currency)}，明显微小错报临界值 ${money(trivial, currency)}：未更正金额超过 ${money(trivial, currency)} 的错报计入汇总表，合计达到 ${money(materiality, currency)} 时候选保留意见。`
      },
    },
    en: {
      purpose:
        'Prepares the schedule of uncorrected misstatements, determines residual amounts by correction status, distinguishes clearly trivial items and compares the aggregate with overall materiality to produce a system candidate opinion for FIN-01 evaluation.',
      when: 'Applies to all engagements. With the misstatement lifecycle enabled, aggregation follows resolution of correction status and is refreshed on each completion cycle. Otherwise, misstatements are accumulated as uncorrected after fieldwork. A return from FIN-01 produces a revised schedule.',
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
          name: 'System candidate opinion',
          detail:
            'An aggregate at or above overall materiality produces a qualified candidate opinion; otherwise the candidate is unqualified. This comparison is advisory only. The auditor determines the final opinion at FIN-01.',
        },
        {
          name: 'Unresolved item check',
          detail:
            'No schedule is produced if a misstatement remains identified, awaiting correction or under verification. Execution fails and creates an auditor task. The normal workflow requires correction status to be resolved before aggregation.',
        },
      ],
      result:
        'Produces a revised schedule showing accumulated, clearly trivial and corrected items, the aggregate amount, materiality and the candidate opinion. The procedure completes automatically without a separate review task; failed execution creates a task permitting a rerun. The schedule is submitted to FIN-01 for evaluation.',
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
      purpose:
        '由审计师评价未更正错报的性质及金额、重要性和相关审计范围限制，确定本项目审计意见类型。系统候选意见仅为辅助建议，最终意见须经审计师确认。',
      when: '适用于全部项目，在 FIN-04 形成汇总表后提交阶段决定。',
      logic: [
        {
          name: '呈报材料',
          detail:
            '候选意见、未更正错报合计、重要性水平，以及每条错报的状态、类型、金额、舞弊迹象、管理层回复和未更正的依据；另列因无数据未执行的程序及原因。',
        },
        {
          name: '结果过期处理',
          detail:
            '程序执行之后数据有改动的，材料中会提示；可到审计程序页用新数据重跑，重跑完重新累计错报后再回到这里。',
        },
        {
          name: '审计意见确定',
          detail:
            '审计师选择最终意见，可与系统候选不同。本版仅支持无保留意见及保留意见，不支持否定意见或无法表示意见。确认后进入报告阶段，由 RPT-03 起草报告。',
        },
        {
          name: '退回',
          detail: '重新累计错报（FIN-04），出汇总表的新一版后再呈报；已做的分类、沟通和更正验证不重做。',
        },
        { name: '信息不足', detail: '暂缓形成结论，保留待决定状态并重新呈报材料。' },
        { name: '理由', detail: '各项决定的理由都是选填；确认意见时填的理由会提供给报告起草参考。' },
      ],
      result:
        '确认后，将所选意见类型记为项目审计意见，进入报告阶段并由 RPT-03 起草报告。退回时重新累计错报后呈报；信息不足时保持待决定状态。决定通过工作区或待决定页提交，相关汇总表在错报与审计意见页展示。',
    },
    en: {
      purpose:
        'Supports the auditor’s evaluation of the nature and amount of uncorrected misstatements, materiality and relevant scope limitations to determine the engagement’s audit opinion. The system candidate is advisory and requires an auditor decision.',
      when: 'Applies to all engagements as a stage decision following preparation of the FIN-04 schedule.',
      logic: [
        {
          name: 'Decision materials',
          detail:
            'The candidate opinion, total uncorrected misstatements and overall materiality, and for each misstatement its status, type, amount, fraud flag, management response and why it is uncorrected; procedures not performed for lack of data are listed with the reasons.',
        },
        {
          name: 'Out-of-date procedure results',
          detail:
            'Data changes after a procedure’s execution are identified in the decision materials. Affected fieldwork procedures can be rerun using updated data; misstatements are then aggregated again before renewed presentation.',
        },
        {
          name: 'Audit opinion determination',
          detail:
            'The auditor selects the final opinion, which may differ from the system candidate. The current version supports unqualified and qualified opinions only, excluding adverse opinions and disclaimers. Confirmation initiates report drafting by RPT-03.',
        },
        {
          name: 'Return for reassessment',
          detail:
            'Misstatement accumulation (FIN-04) is redone and the new version of the schedule is presented again; classification, communication and correction verification already done are not repeated.',
        },
        {
          name: 'Insufficient information',
          detail: 'No decision is made; the engagement stays here and the material is presented again.',
        },
        {
          name: 'Reason',
          detail:
            'A reason is optional for every decision; a reason given when confirming the opinion is passed to report drafting.',
        },
      ],
      result:
        'Confirmation records the selected engagement opinion and initiates report drafting by RPT-03. A return refreshes the misstatement aggregation and presentation; insufficient information leaves the decision pending. Decisions are submitted through the workspace or Pending decisions page, with schedules available on the Misstatements and opinion page.',
    },
  },
  'RPT-03': {
    zh: {
      purpose:
        '依据 FIN-01 已确认的意见类型编制审计报告草稿。模型起草审计意见及形成审计意见的基础，其他段落采用固定模板，并填入被审计单位、报告期间及项目负责人信息。',
      when: '适用于全部项目，在 FIN-01 确认意见后执行。RPT-01 退回时依据批注修订；过期程序重新执行并确认意见后，依据最新结果重拟。报告采用本次执行的工作语言。',
      logic: [
        {
          name: '起草依据',
          detail:
            '包括被审计单位名称、已确认意见类型及 FIN-01 记录的理由（如有）。保留意见另依据汇总表中的未更正错报及其已确认来源判断。',
        },
        {
          name: '审计意见段',
          detail:
            '首行为标题；第一段说明审计了公司的财务报表及其组成；第二段按意见类型写意见，保留意见写成“除……的影响外”。',
        },
        {
          name: '形成审计意见的基础段',
          detail:
            '保留意见时先逐条说明每笔未更正错报的事项、受影响科目、金额以及高估或低估。然后说明按澳大利亚审计准则执行审计、审计师的独立性与独立性声明，以及审计证据充分、适当。',
        },
        {
          name: '写作约束',
          detail:
            '关于错报的事实只能来自上述错报和判断，不写重要性金额、执行过的程序或资料以外的事实。保留意见必须引用至少一条已确认判断。',
        },
        {
          name: '报告修订',
          detail:
            '退回后逐项回应审阅批注，其余措辞保持不变；程序重新执行后，依据最新结果修订并保留仍有依据的原表述。工作语言变更时，以当前语言重新编制全文。',
        },
        {
          name: '校验',
          detail: '两段的首行标题须与意见类型相符，引用的判断须是当前已确认的版本；不通过时本次执行失败。',
        },
      ],
      result:
        '生成可追溯的报告草稿版本，并直接提交 RPT-01 审阅和批准，不单独生成复核任务。各稿保留在报告页；执行失败时生成可重新执行的失败任务。',
      project: (p) => {
        const end = str(p, 'period_end')
        if (end == null) return null
        return `报告针对截至 ${end} 的财务报表。`
      },
    },
    en: {
      purpose:
        'Prepares the audit report draft using the opinion confirmed at FIN-01. The model drafts the opinion and basis for opinion; other sections use fixed templates populated with the entity, reporting period and engagement leader details.',
      when: 'Applies to all engagements following opinion confirmation at FIN-01. A return from RPT-01 initiates revision against comments; rerun procedures and a reconfirmed opinion initiate redrafting from updated results. The report uses the working language of the current run.',
      logic: [
        {
          name: 'Drafting basis',
          detail:
            'The entity name, confirmed opinion and any rationale recorded at FIN-01. A qualified opinion additionally uses the scheduled uncorrected misstatements and their confirmed source judgments.',
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
        'Produces a traceable report draft version for direct review and approval at RPT-01 without a separate review task. Draft history remains available on the Report page. Failed execution creates a task permitting a rerun.',
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
        '由审计师审阅 RPT-03 编制的最新报告草稿，决定批准签署并归档，或提出批注退回修订。签署后形成最终报告，项目归档并锁定。',
      when: '适用于全部项目，在 RPT-03 形成各版报告草稿后提交审阅及阶段决定。',
      logic: [
        {
          name: '呈报材料',
          detail:
            '当前一稿的全文（审计意见、形成审计意见的基础、董事的责任、审计师的责任、其他法律法规要求、签字），以及此前各次退回的批注。',
        },
        {
          name: '报告签署与归档',
          detail:
            '报告出签字版，报告日记为签字当天（悉尼时间），并记下签字人和签字时间。项目随即归档、锁定，之后只能查看。',
        },
        {
          name: '审阅批注与退回修订',
          detail: '退回至少须提供一项批注，明确所涉段落及修订要求，可附引用原文。模型据此编制下一稿并重新提交审阅。',
        },
        { name: '信息不足', detail: '暂缓形成结论，保留待决定状态并重新呈报报告。' },
        {
          name: '数据有变动',
          detail:
            '程序执行之后数据有改动的，也可以在这一步到审计程序页用新数据重跑过期程序；重跑结果须重新经过未更正错报评价（FIN-01），报告随后重拟。',
        },
      ],
      result:
        '签署版本在报告页标记为已签字归档，项目转为只读。退回后依据批注生成下一稿并再次呈报。决定可通过工作区、待决定页或报告页提交。',
    },
    en: {
      purpose:
        'Supports auditor review of the latest RPT-03 report draft for approval, signature and archiving, or return with revision comments. Signature finalises the report and archives and locks the engagement.',
      when: 'Applies to all engagements following each RPT-03 draft, for review and a stage decision.',
      logic: [
        {
          name: 'Decision materials',
          detail:
            "The full text of the current draft (opinion, basis for opinion, directors' responsibilities, auditor's responsibilities, other legal and regulatory requirements, signature) and the comments from earlier returns.",
        },
        {
          name: 'Report signature and archiving',
          detail:
            'The report gets a signed version dated the day of signing (Sydney time), recording who signed and when. The engagement is then archived and locked, and can only be viewed.',
        },
        {
          name: 'Review comments and return',
          detail:
            'A return requires at least one comment specifying the report section and requested amendment, with an optional quotation. The model revises the draft accordingly and resubmits it for review.',
        },
        {
          name: 'Insufficient information',
          detail: 'No decision is made; the engagement stays here and the report is presented again.',
        },
        {
          name: 'Changes in underlying data',
          detail:
            'Out-of-date fieldwork procedures can be rerun with updated data before report approval. Updated results must pass FIN-01 opinion evaluation before the report is redrafted.',
        },
      ],
      result:
        'The signed version is marked as signed and archived on the Report page and the engagement becomes read-only. Returned drafts are revised against the comments and presented again. Decisions may be submitted through the workspace, Pending decisions page or Report page.',
    },
  },
}

export function procedureIntro(id: string, lang: Lang): ProcedureIntro | undefined {
  return PROCEDURE_INTROS[id]?.[lang]
}
