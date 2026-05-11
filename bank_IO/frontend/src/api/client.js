import { useConfig } from '../composables/useConfig.js'

async function request(path, options = {}) {
  const { get } = useConfig()
  const cfg = get()
  if (!cfg) throw new Error('Brak konfiguracji — ustaw URL backendu banku')
  const base = cfg.backendUrl.replace(/\/$/, '')

  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) }
  const res = await fetch(`${base}${path}`, { ...options, headers })
  const text = await res.text()
  let body = null
  if (text) {
    try { body = JSON.parse(text) } catch { body = { detail: text } }
  }
  if (!res.ok) {
    const d = body?.detail
    const isObj = d && typeof d === 'object'
    const err = new Error((isObj ? d.message : d) || `HTTP ${res.status}`)
    err.code = isObj ? d.code : undefined
    err.status = res.status
    err.body = body
    throw err
  }
  return body
}

export const api = {
  get: (p) => request(p),
  post: (p, data) => request(p, { method: 'POST', body: JSON.stringify(data ?? {}) }),
}