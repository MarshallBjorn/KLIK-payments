import { api } from './client.js'

export const initiatePayment = (payload) => api.post('/payments/initiate', payload)
export const getPaymentStatus = (id) => api.get(`/payments/status/${id}`)
export const listPaymentHistory = () => api.get('/payments')
