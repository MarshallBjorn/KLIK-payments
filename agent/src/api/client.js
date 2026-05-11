import { useConfig } from '../composables/useConfig.js'

const BASE = '/api/v1'

async function request(path, options = {}) {
  const { get } = useConfig()
  const cfg = get()
  if (!cfg) throw new Error('Brak konfiguracji agenta')

  const headers = {
    'Content-Type': 'application/json',
    'X-KLIK-Agent-Api-Key': cfg.apiKey,
    ...(options.headers || {}),
  }
  if (options.method && options.method !== 'GET') {
    headers['Idempotency-Key'] = crypto.randomUUID()
  }

  const res = await fetch(`${BASE}${path}`, { ...options, headers })
  const text = await res.text()
  const body = text ? JSON.parse(text) : null

  if (!res.ok) {
    const err = new Error(body?.error?.message || `HTTP ${res.status}`)
    err.code = body?.error?.code
    err.status = res.status
    err.body = body
    throw err
  }
  return body
}

export const api = {
  get: (path) => request(path),
  post: (path, data) =>
    request(path, { method: 'POST', body: JSON.stringify(data) }),
}
