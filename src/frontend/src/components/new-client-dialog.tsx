import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CircleCheck } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'
import { clientsCreateClientMutation, clientsListClientsQueryKey } from '@/client/@tanstack/react-query.gen'
import type { ClientCreate } from '@/client/types.gen'
import {
  draftFields,
  LabeledField,
  type ProfileDraft,
  type ProfileErrors,
  ProfileFields,
  profileDraft,
  SELECT,
} from '@/components/profile-fields'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { abnProblem, formatAbn, normalizeAbn } from '@/lib/abn'
import { abnTaken, errorText } from '@/lib/errors'
import { useI18n } from '@/lib/i18n'
import type { Messages } from '@/locales/en'

const FRAMEWORKS = ['tier1_gpfs', 'tier2_gpfs', 'spfr'] as const
type Framework = (typeof FRAMEWORKS)[number]

// 4 个常用的扩展字段, 留空即没有申报; 其余扩展字段接口照收, 表单以后再加
type Extras = { yearEnd: string; limit: string; framework: Framework | ''; priorAudited: '' | 'yes' | 'no' }
const NO_EXTRAS: Extras = { yearEnd: '', limit: '', framework: '', priorAudited: '' }

type Problems = ProfileErrors & { yearEnd?: string; limit?: string }

// 与后端的校验一致: 法定名称不为空, ABN 合格; 财年末为合法的 MM-DD (2 月 29 日也可), 审批限额为非负的澳元金额
function problems(draft: ProfileDraft, extras: Extras, t: Messages): Problems {
  const found: Problems = {}
  if (!draft.legalName.trim()) found.legalName = t.newClient.errors.legalName
  const abn = abnProblem(draft.abn)
  if (abn === 'format') found.abn = t.newClient.errors.abnFormat
  if (abn === 'checksum') found.abn = t.newClient.errors.abnChecksum
  const yearEnd = extras.yearEnd.trim()
  if (yearEnd) {
    const match = /^(\d{2})-(\d{2})$/.exec(yearEnd)
    const day = match ? new Date(Date.UTC(2000, Number(match[1]) - 1, Number(match[2]))) : null
    if (!match || !day || day.getUTCMonth() !== Number(match[1]) - 1 || day.getUTCDate() !== Number(match[2])) {
      found.yearEnd = t.newClient.errors.yearEnd
    }
  }
  if (extras.limit.trim() && limitCents(extras.limit) === null) found.limit = t.newClient.errors.limit
  return found
}

// 澳元金额 (至多两位小数, 可带千分位逗号) 换成整数分; 不合格为 null
function limitCents(text: string): number | null {
  const value = text.trim().replaceAll(',', '')
  if (!/^\d+(\.\d{1,2})?$/.test(value)) return null
  const [whole = '0', fraction = ''] = value.split('.')
  const cents = Number(whole) * 100 + Number(fraction.padEnd(2, '0'))
  return Number.isSafeInteger(cents) ? cents : null
}

function requestBody(draft: ProfileDraft, extras: Extras): ClientCreate {
  return {
    ...draftFields(draft),
    financial_year_end: extras.yearEnd.trim() || null,
    approval_limit_cents: extras.limit.trim() ? limitCents(extras.limit) : null,
    applicable_framework: extras.framework || null,
    prior_period_audited: extras.priorAudited === '' ? null : extras.priorAudited === 'yes',
  }
}

export function NewClientDialog({
  open,
  onClose,
  onCreated,
  onPick,
}: {
  open: boolean
  onClose: () => void
  onCreated: (clientId: string) => void // 建好后, 新建项目表单改选这个客户
  onPick: (clientId: string) => void // ABN 已登记时改选已有的客户
}) {
  const { t } = useI18n()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState(() => profileDraft())
  const [extras, setExtras] = useState(NO_EXTRAS)
  const [submitted, setSubmitted] = useState(false) // 提交过一次后才显示必填项的错误, 免得一打开就满屏红字
  const create = useMutation({
    ...clientsCreateClientMutation(),
    onSuccess: async (client) => {
      toast.success(t.newClient.created(client.legal_name))
      await queryClient.invalidateQueries({ queryKey: clientsListClientsQueryKey() })
      reset()
      onCreated(client.client_id)
    },
  })
  const reset = () => {
    setDraft(profileDraft())
    setExtras(NO_EXTRAS)
    setSubmitted(false)
    create.reset()
  }
  const close = () => {
    reset()
    onClose()
  }
  const found = problems(draft, extras, t)
  const shown: Problems = submitted ? found : { ...found, legalName: undefined }
  // ABN 填满 11 位前不提示格式错误; 提交后照常提示
  if (!submitted && draft.abn.replace(/\s+/g, '').length < 11 && found.abn === t.newClient.errors.abnFormat) {
    shown.abn = undefined
  }
  const abnOk = draft.abn.trim() !== '' && found.abn === undefined
  // R289 接上 ABN Lookup 后, 在线核对的结果也显示在这里
  const abnHint = abnOk ? (
    <span className="flex flex-col gap-0.5">
      <span className="inline-flex items-center gap-1">
        <CircleCheck className="size-3.5 shrink-0 text-ok" aria-hidden="true" />
        {t.newClient.abnChecksumOk(formatAbn(draft.abn))}
      </span>
      <span>{t.newClient.abnLookupLater}</span>
    </span>
  ) : (
    t.newClient.abnHint
  )
  // 已登记的提示只对发出请求时的 ABN: 请求返回时表单里的 ABN 已改, 就不显示 (审核 R288-09)
  const rejected = abnTaken(create.error)
  const taken = rejected && create.variables?.body.abn === normalizeAbn(draft.abn) ? rejected : null
  const set = (changes: Partial<Extras>) => setExtras({ ...extras, ...changes })
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close()
      }}
    >
      <DialogContent className="max-h-[calc(100vh-2rem)] overflow-y-auto sm:max-w-[680px]">
        <DialogHeader>
          <DialogTitle className="font-semibold text-base">{t.newClient.title}</DialogTitle>
          <DialogDescription>{t.newClient.intro}</DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-3"
          noValidate
          onSubmit={(event) => {
            event.preventDefault()
            setSubmitted(true)
            if (Object.keys(found).length === 0) create.mutate({ body: requestBody(draft, extras) })
          }}
        >
          <ProfileFields
            idPrefix="nc"
            draft={draft}
            onChange={(next) => {
              if (create.error && next.abn !== draft.abn) create.reset() // 改了 ABN: 上次的拒绝 (如已登记) 不再适用
              setDraft(next)
            }}
            errors={shown}
            abnHint={abnHint}
          />
          <fieldset className="m-0 flex flex-col gap-3 rounded-lg border p-3">
            <legend className="px-1 text-[12.5px] text-muted-foreground">{t.newClient.optional}</legend>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(200px,1fr))] gap-3">
              <LabeledField id="nc-year-end" label={t.newClient.yearEnd}>
                <Input
                  id="nc-year-end"
                  className="h-10 font-mono"
                  placeholder="06-30"
                  value={extras.yearEnd}
                  aria-invalid={shown.yearEnd ? true : undefined}
                  onChange={(event) => set({ yearEnd: event.target.value })}
                />
                {shown.yearEnd ? <span className="text-[12.5px] text-destructive">{shown.yearEnd}</span> : null}
              </LabeledField>
              <LabeledField id="nc-limit" label={t.newClient.approvalLimit}>
                <Input
                  id="nc-limit"
                  className="h-10 font-mono"
                  inputMode="decimal"
                  value={extras.limit}
                  aria-invalid={shown.limit ? true : undefined}
                  onChange={(event) => set({ limit: event.target.value })}
                />
                {shown.limit ? <span className="text-[12.5px] text-destructive">{shown.limit}</span> : null}
              </LabeledField>
              <LabeledField id="nc-framework" label={t.newClient.framework}>
                <select
                  id="nc-framework"
                  className={SELECT}
                  value={extras.framework}
                  onChange={(event) => set({ framework: event.target.value as Framework | '' })}
                >
                  <option value="">{t.newClient.notDeclared}</option>
                  {FRAMEWORKS.map((value) => (
                    <option key={value} value={value}>
                      {t.newClient.frameworks[value]}
                    </option>
                  ))}
                </select>
              </LabeledField>
              <LabeledField id="nc-prior" label={t.newClient.priorAudited}>
                <select
                  id="nc-prior"
                  className={SELECT}
                  value={extras.priorAudited}
                  onChange={(event) => set({ priorAudited: event.target.value as Extras['priorAudited'] })}
                >
                  <option value="">{t.newClient.notDeclared}</option>
                  <option value="yes">{t.clientPage.yes}</option>
                  <option value="no">{t.clientPage.no}</option>
                </select>
              </LabeledField>
            </div>
          </fieldset>
          <p className="m-0 text-[13px] text-muted-foreground">{t.newClient.registryNote}</p>
          {taken ? (
            <Alert variant="destructive">
              <AlertDescription className="flex flex-wrap items-center justify-between gap-2">
                <span>{t.newClient.taken(taken.legal_name, taken.client_id)}</span>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    reset()
                    onPick(taken.client_id)
                  }}
                >
                  {t.newClient.pickExisting}
                </Button>
              </AlertDescription>
            </Alert>
          ) : create.error ? (
            <Alert variant="destructive">
              <AlertDescription>{errorText(create.error, t)}</AlertDescription>
            </Alert>
          ) : null}
          <div className="flex justify-end gap-2.5">
            <Button type="button" variant="outline" onClick={close}>
              {t.projectsPage.cancel}
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {t.newClient.save}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  )
}
