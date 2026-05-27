import { api } from './client.js'

export const getInfo = () => api.get('/api/info')
export const listClients = () => api.get('/api/clients')
export const generateCode = (userId) => api.post(`/api/clients/${encodeURIComponent(userId)}/generate-code`)
export const listPending = () => api.get('/api/pending')
export const acceptPending = (txId, pin) => api.post(`/api/pending/${txId}/accept`, { pin })
export const rejectPending = (txId, rejectReason) => api.post(`/api/pending/${txId}/reject`, { reject_reason: rejectReason })
export const listHistory = () => api.get('/api/history')
export const setApiKey = (apiKey) => api.post('/api/config/api-key', { api_key: apiKey })
export const clearApiKey = () => api.request ? null : null

// P2P (Telefony)
export const registerAlias = (userId, phone) =>
  api.post(`/api/clients/${encodeURIComponent(userId)}/register-alias`, phone ? { phone } : {})
export const listAliases = () => api.get('/api/aliases')
export const deleteAlias = (phone) => api.del(`/api/aliases/${encodeURIComponent(phone)}`)
export const lookupAlias = (phone) => api.post('/api/lookup', { phone })
