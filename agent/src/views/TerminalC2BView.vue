<script setup>
import { ref } from 'vue'
import { useConfig } from '../composables/useConfig.js'
import { initiatePayment, getPaymentStatus } from '../api/payments.js'
import MerchantPicker from '../components/MerchantPicker.vue'
import PaymentResult from '../components/PaymentResult.vue'

const { get } = useConfig()
const cfg = get()

const code = ref('')
const amount = ref('')
const merchantId = ref('')

const status = ref('idle')   // idle | pending | completed | rejected | timeout | error
const result = ref(null)
const errorMsg = ref('')

const POLL_INTERVAL_MS = 1500
const POLL_MAX_MS = 180000   // 3 min

async function pay() {
  errorMsg.value = ''
  result.value = null
  status.value = 'pending'

  try {
    const tx = await initiatePayment({
      code: code.value,
      amount: amount.value,
      currency: cfg.currency,
      merchant_id: merchantId.value,
    })
    await pollStatus(tx.transaction_id)
  } catch (e) {
    status.value = 'error'
    errorMsg.value = e.message
  }
}

async function pollStatus(transactionId) {
  const startedAt = Date.now()
  while (Date.now() - startedAt < POLL_MAX_MS) {
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS))
    try {
      const s = await getPaymentStatus(transactionId)
      result.value = s
      if (s.status === 'COMPLETED') { status.value = 'completed'; return }
      if (s.status === 'REJECTED')  { status.value = 'rejected';  return }
      if (s.status === 'TIMEOUT')   { status.value = 'timeout';   return }
    } catch (e) {
      status.value = 'error'
      errorMsg.value = e.message
      return
    }
  }
  status.value = 'timeout'
  errorMsg.value = 'Przekroczono czas oczekiwania'
}

function reset() {
  code.value = ''
  amount.value = ''
  status.value = 'idle'
  result.value = null
  errorMsg.value = ''
}

const canSubmit = () =>
  /^\d{6}$/.test(code.value) &&
  parseFloat(amount.value) > 0 &&
  merchantId.value &&
  status.value !== 'pending'
</script>

<template>
  <div class="bg-white rounded-lg shadow p-6">
    <h1 class="text-2xl font-bold mb-6">Terminal C2B — Płatność kodem KLIK</h1>

    <form @submit.prevent="pay" class="space-y-4">
      <MerchantPicker v-model="merchantId" />

      <div>
        <label class="block text-sm font-medium mb-1">Kod KLIK (6 cyfr)</label>
        <input
          v-model="code"
          maxlength="6"
          inputmode="numeric"
          placeholder="123456"
          class="w-full px-3 py-2 border rounded font-mono text-center text-2xl tracking-widest focus:outline-none focus:ring-2 focus:ring-emerald-500"
        />
      </div>

      <div>
        <label class="block text-sm font-medium mb-1">Kwota ({{ cfg.currency }})</label>
        <input
          v-model="amount"
          type="number"
          step="0.01"
          min="0.01"
          placeholder="0.00"
          class="w-full px-3 py-2 border rounded font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500"
        />
      </div>

      <div class="flex gap-3 pt-2">
        <button
          type="submit"
          :disabled="!canSubmit()"
          class="flex-1 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-medium py-3 rounded"
        >
          Zapłać
        </button>
        <button
          type="button"
          @click="reset"
          class="px-4 py-3 bg-slate-200 hover:bg-slate-300 rounded"
        >
          Reset
        </button>
      </div>
    </form>

    <div class="mt-6">
      <PaymentResult :status="status" :result="result" :error="errorMsg" />
    </div>
  </div>
</template>
