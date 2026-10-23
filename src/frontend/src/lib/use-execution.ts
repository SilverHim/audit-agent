import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import type { TrailEntry } from '@/client/types.gen'
import { getToken } from '@/lib/auth'
import { invalidateEngagement } from '@/lib/engagement'
import { mergeEvents } from '@/lib/execution'

// 服务器重放持久化事件; 游标仅在完整解析一条事件后推进. 断线保留已收记录并重连补读.
export function useExecution(eid: string) {
  const queryClient = useQueryClient()
  const [events, setEvents] = useState<TrailEntry[]>([])
  const [connected, setConnected] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    let cursor: string | null = null
    let retry: ReturnType<typeof setTimeout> | undefined
    setEvents([])
    setConnected(false)
    async function connect() {
      try {
        const query = cursor ? `?after=${encodeURIComponent(cursor)}` : ''
        const response = await fetch(`/api/v1/engagements/${encodeURIComponent(eid)}/execution/stream${query}`, {
          headers: { Authorization: `Bearer ${getToken() ?? ''}`, Accept: 'text/event-stream' },
          signal: controller.signal,
        })
        if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`)
        setConnected(true)
        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
        let buffer = ''
        try {
          for (;;) {
            const { done, value } = await reader.read()
            if (done) break
            buffer += value
            let boundary = buffer.indexOf('\n\n')
            while (boundary >= 0) {
              const frame = buffer.slice(0, boundary)
              buffer = buffer.slice(boundary + 2)
              boundary = buffer.indexOf('\n\n')
              const line = frame.split('\n').find((item) => item.startsWith('data:'))
              if (!line) continue
              const entry = JSON.parse(line.slice(5)) as TrailEntry
              setEvents((previous) => mergeEvents(previous, [entry]))
              cursor = entry.id ?? cursor
              if (entry.kind === 'assistant_action' || (entry.kind === 'run' && entry.payload?.action === 'finished')) {
                void invalidateEngagement(queryClient, eid)
              }
            }
          }
        } finally {
          reader.releaseLock()
        }
      } catch {
        // 状态由重连提示呈报, 不清除已持久化的执行记录.
      }
      if (!controller.signal.aborted) {
        setConnected(false)
        retry = setTimeout(() => void connect(), 2000)
      }
    }
    void connect()
    return () => {
      controller.abort()
      clearTimeout(retry)
    }
  }, [eid, queryClient])
  return { events, connected, prepend: (older: TrailEntry[]) => setEvents((previous) => mergeEvents(previous, older)) }
}
