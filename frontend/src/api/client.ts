// fetch 封装：统一处理 {ok:true,data} / {ok:false,error} 与 4xx/5xx

interface ApiBody<T> {
  ok?: boolean
  data?: T
  error?: string
  detail?: string
}

export async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, options)
  let body: ApiBody<T> | null = null
  try {
    body = (await res.json()) as ApiBody<T>
  } catch {
    body = null
  }
  if (!res.ok) {
    const msg = body?.error || body?.detail || `HTTP ${res.status}`
    throw new Error(msg)
  }
  return body?.data as T
}

export const api = {
  get: <T>(url: string) => request<T>(url),
  post: <T>(url: string, data?: unknown) =>
    request<T>(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: data === undefined ? undefined : JSON.stringify(data),
    }),
  put: <T>(url: string, data: unknown) =>
    request<T>(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    }),
  del: (url: string) => request<void>(url, { method: 'DELETE' }),
}

// 带上传进度的 zip 上传（fetch 不支持上传进度，用 XHR）
export function uploadWithProgress<T>(
  url: string,
  file: File,
  onProgress?: (pct: number) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', url)
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.(Math.round((e.loaded / e.total) * 100))
    }
    xhr.onload = () => {
      let body: ApiBody<T> | null = null
      try {
        body = JSON.parse(xhr.responseText) as ApiBody<T>
      } catch {
        body = null
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body?.data as T)
      else reject(new Error(body?.error || body?.detail || `HTTP ${xhr.status}`))
    }
    xhr.onerror = () => reject(new Error('网络错误，上传失败'))
    const fd = new FormData()
    fd.append('file', file)
    xhr.send(fd)
  })
}