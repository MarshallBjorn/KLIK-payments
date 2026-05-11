import { api } from './client.js'
export const listMerchants = () => api.get('/merchants')
