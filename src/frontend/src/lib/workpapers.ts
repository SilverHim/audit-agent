// 下载使用后端 RFC 5987 文件名, 不从标题猜测实际文件名.
export function downloadName(disposition: string | null, fallback: string): string {
  const encoded = disposition?.match(/filename\*=UTF-8''([^;]+)/i)?.[1]
  if (encoded) {
    try {
      return decodeURIComponent(encoded)
    } catch {
      return fallback
    }
  }
  return disposition?.match(/filename="([^"]+)"/i)?.[1] ?? fallback
}

export function saveDownload(data: Blob, response: Response, fallback: string) {
  const url = URL.createObjectURL(data)
  const link = document.createElement('a')
  link.href = url
  link.download = downloadName(response.headers.get('Content-Disposition'), fallback)
  link.click()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
