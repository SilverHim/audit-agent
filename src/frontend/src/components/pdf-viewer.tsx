import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy, Util } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import type { TextItem } from 'pdfjs-dist/types/src/display/api'
import { useEffect, useRef, useState } from 'react'

GlobalWorkerOptions.workerSrc = workerUrl

type Rect = { left: number; top: number; width: number; height: number }

// 比对前去掉空白并统一引号与大小写, 抽取文本与 PDF 文字层的换行, 空格差异不影响定位
function plain(text: string): string {
  return text.replace(/[‘’]/g, "'").replace(/[“”]/g, '"').replace(/\s+/g, '').toLowerCase()
}

// 在一页的文字层里按候选文本依次查找, 返回第一个命中的各行矩形 (CSS 像素, 相对画布)
function locate(items: TextItem[], candidates: string[], transform: number[], scale: number): Rect[] {
  let text = ''
  const spans: Array<{ start: number; end: number; item: TextItem }> = []
  for (const item of items) {
    const chars = plain(item.str)
    if (!chars) continue
    spans.push({ start: text.length, end: text.length + chars.length, item })
    text += chars
  }
  for (const candidate of candidates) {
    const query = plain(candidate)
    if (!query) continue
    const at = text.indexOf(query)
    if (at < 0) continue
    const end = at + query.length
    const pieces = spans
      .filter((span) => span.end > at && span.start < end)
      .map(({ start, end: spanEnd, item }) => {
        const [, , c, d, e, f] = Util.transform(transform, item.transform)
        const height = Math.hypot(c, d)
        const width = item.width * scale
        const from = Math.max(0, at - start) / (spanEnd - start)
        const to = Math.min(spanEnd, end) - start
        return {
          left: e + width * from,
          top: f - height,
          width: width * (to / (spanEnd - start) - from),
          height: height * 1.25,
        }
      })
    // 文字层按词切分, 同一行的片段合成一个框
    const lines: Rect[] = []
    for (const piece of pieces) {
      const line = lines.find((item) => Math.abs(item.top - piece.top) < piece.height / 2)
      if (!line) {
        lines.push({ ...piece })
        continue
      }
      const right = Math.max(line.left + line.width, piece.left + piece.width)
      line.left = Math.min(line.left, piece.left)
      line.width = right - line.left
      line.height = Math.max(line.height, piece.height)
    }
    return lines
  }
  return []
}

export function PdfViewer({
  data,
  page,
  zoom,
  highlight,
  onLoaded,
  onLocated,
}: {
  data: ArrayBuffer
  page: number
  zoom: number
  highlight: string[] | null
  onLoaded: (pages: number) => void
  onLocated?: (found: boolean) => void
}) {
  const box = useRef<HTMLDivElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null)
  const [width, setWidth] = useState(0)
  const [rects, setRects] = useState<Rect[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    // pdf.js 会转移传入的缓冲区, 传副本以便重新渲染
    // 卸载或换文件时会取消上一次加载, 被取消的那次不报错
    let cancelled = false
    const task = getDocument({ data: data.slice(0) })
    task.promise.then(
      (loaded) => {
        if (cancelled) return
        setError(null)
        setDoc(loaded)
        onLoaded(loaded.numPages)
      },
      (caught: unknown) => {
        if (!cancelled) setError(String(caught))
      },
    )
    return () => {
      cancelled = true
      void task.destroy()
    }
  }, [data, onLoaded])

  useEffect(() => {
    const element = box.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => setWidth(Math.floor(entry.contentRect.width)))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!doc || !width || !canvas.current) return
    let cancelled = false
    const target = canvas.current
    let renderTask: ReturnType<Awaited<ReturnType<PDFDocumentProxy['getPage']>>['render']> | null = null
    void (async () => {
      const pdfPage = await doc.getPage(Math.min(Math.max(1, page), doc.numPages))
      const scale = (width / pdfPage.getViewport({ scale: 1 }).width) * zoom
      const viewport = pdfPage.getViewport({ scale })
      const ratio = window.devicePixelRatio || 1
      target.width = Math.floor(viewport.width * ratio)
      target.height = Math.floor(viewport.height * ratio)
      target.style.width = `${Math.floor(viewport.width)}px`
      target.style.height = `${Math.floor(viewport.height)}px`
      renderTask = pdfPage.render({
        canvas: target,
        viewport,
        transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
      })
      await renderTask.promise.catch(() => undefined)
      if (cancelled) return
      if (!highlight?.length) {
        setRects([])
        return
      }
      const content = await pdfPage.getTextContent()
      if (cancelled) return
      const items = content.items.filter((item): item is TextItem => 'str' in item)
      const found = locate(items, highlight, viewport.transform, scale)
      setRects(found)
      onLocated?.(found.length > 0)
    })()
    return () => {
      cancelled = true
      renderTask?.cancel()
    }
  }, [doc, page, width, zoom, highlight, onLocated])

  return (
    <div ref={box} className="w-full overflow-x-auto">
      {error ? <p className="text-destructive text-sm">{error}</p> : null}
      <div className="relative mx-auto w-fit bg-white shadow-[0_1px_3px_rgba(17,24,39,0.12),0_8px_24px_rgba(17,24,39,0.08)]">
        <canvas ref={canvas} className="block" />
        {rects.map((rect) => (
          <span
            key={`${rect.left}:${rect.top}`}
            aria-hidden="true"
            className="pointer-events-none absolute rounded-[3px] border-[1.5px] border-[#e0a800] bg-[rgba(255,214,102,0.35)]"
            style={{ left: rect.left - 2, top: rect.top - 1, width: rect.width + 4, height: rect.height }}
          />
        ))}
      </div>
    </div>
  )
}
