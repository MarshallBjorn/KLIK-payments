import { api } from './client.js'

export const getInfo = () => api.get('/api/info')
export const listClients = () => api.get('/api/clients')
export const generateCode = (userId) => api.post(`/api/clients/${encodeURIComponent(userId)}/generate-code`)
export const listPending = () => api.get('/api/pending')
export const acceptPending = (txId, pin) => api.post(`/api/pending/${txId}/accept`, { pin })
export const rejectPending = (txId, rejectReason) => api.post(`/api/pending/${txId}/reject`, { reject_reason: rejectReason })
export const listHistory = () => api.get('/api/history')
export const setApiKey = (apiKey) => api.post('/api/config/api-key', { api_key: apiKey })
export const clearApiKey = () => api.request ? null : null  // patrz niżej