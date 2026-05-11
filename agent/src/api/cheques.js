// MOCK: backend dla /cheques/redeem nie istnieje. Symuluje synchroniczną realizację.
export function redeemCheque({ cheque_code }) {
  return new Promise((resolve, reject) => {
    setTimeout(() => {
      if (cheque_code === '000000000') {
        reject(Object.assign(new Error('Czek wygasł lub nie istnieje.'), {
          code: '404_CHEQUE_EXPIRED',
          status: 404,
        }))
        return
      }
      const amount = 100 + Math.floor(Math.random() * 400)
      resolve({
        cheque_id: crypto.randomUUID(),
        transaction_id: crypto.randomUUID(),
        amount_gross: amount.toFixed(2),
        merchant_net: (amount * 0.987).toFixed(2),
        klik_fee: (amount * 0.005).toFixed(2),
        agent_fee: (amount * 0.008).toFixed(2),
        currency: 'PLN',
        redeemed_at: new Date().toISOString(),
        _mock: true,
      })
    }, 800)
  })
}
