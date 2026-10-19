import { useEffect, useState } from 'react'

// 值停止变化 delay 毫秒后才返回新值 (输入防抖): 边打字边发请求时, 只在停顿后查一次
export function useDebounced<T>(value: T, delay = 500): T {
  const [settled, setSettled] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay)
    return () => window.clearTimeout(timer)
  }, [value, delay])
  return settled
}
