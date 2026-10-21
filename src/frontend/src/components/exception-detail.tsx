import { Fragment } from 'react'
import { type DetailRow, detailRows } from '@/lib/exception-detail'
import { useI18n } from '@/lib/i18n'

// 例外明细按字段显示 (lib/exception-detail, 需求 R350), 闸门材料的例外列表与复核页无归因任务的失败原因共用;
// 对象的字段缩进一层. 有的规则 (如 ACC-04) 例外没有明细, 空的不显示
function DetailRows({ rows }: { rows: DetailRow[] }) {
  return (
    <dl className="m-0 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-[13px]">
      {rows.map((item) => (
        <Fragment key={item.key}>
          <dt className="text-muted-foreground">{item.label}</dt>
          <dd className="m-0 text-secondary-foreground">
            {'rows' in item ? <DetailRows rows={item.rows} /> : item.text}
          </dd>
        </Fragment>
      ))}
    </dl>
  )
}

export function ExceptionDetail({ detail }: { detail: Record<string, unknown> }) {
  const { t } = useI18n()
  const rows = detailRows(detail, t.exceptionDetail)
  return rows.length ? <DetailRows rows={rows} /> : null
}
