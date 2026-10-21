import { createContext, type ReactNode, useContext, useMemo } from 'react'
import { formatMoney, formatMoneyShort } from '@/lib/money'

const Currency = createContext('AUD')

export function MoneyProvider({ currency, children }: { currency: string; children: ReactNode }) {
  return <Currency value={currency}>{children}</Currency>
}

// 币种只属于当前项目树; 多个项目与预览并排渲染也不会共享可变设置.
export function useMoney() {
  const currency = useContext(Currency)
  return useMemo(
    () => ({
      currency,
      formatMoney: (cents: number | null | undefined) => formatMoney(cents, currency),
      formatMoneyShort: (cents: number | null | undefined) => formatMoneyShort(cents, currency),
    }),
    [currency],
  )
}
