import { api } from './client.js'

export const redeemCheque = (payload) => api.post('/cheques/redeem', payload)
export const getChequeStatus = (id) => api.get(`/cheques/status/${id}`)
