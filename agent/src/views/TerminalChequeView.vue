<script setup>
import { ref } from 'vue'
import { redeemCheque } from '../api/cheques.js'
import MerchantPicker from '../components/MerchantPicker.vue'
import PaymentResult from '../components/PaymentResult.vue'

const code = ref('')
const merchantId = ref('')

const status = ref('idle')
const result = ref(null)
const errorMsg = ref('')

async function redeem() {
  errorMsg.value = ''
  result.value = null
  status.value = 'pending'

  try {
    const r = await redeemCheque({
      cheque_code: code.value,
      merchant_id: merchantId.value,
    })
    result.value = r
    status.value = 'completed'
  } catch (e) {
    status.value = 'error'
    errorMsg.value = e.message
  }
}

function reset() {
  code.value = ''
  status.value = 'idle'
  result.value = null
  errorMsg.value = ''
}

const canSubmit = () =>
  /^\d{9}$/.test(code.value) && merchantId.value && status.value !== 'pending'
</script>

<template>
  <div class="bg-white rounded-lg shadow p-6">
    <h1 class="text-2xl font-bold mb-1">Terminal Czek — Realizacja czeku KLIK</h1>
    <p class="text-sm text-slate-500 mb-6">
      Wprowadź 9-cyfrowy kod czeku otrzymany od klienta i wybierz merchanta.
    </p>

    <form @submit.prevent="redeem" class="space-y-4">
      <MerchantPicker v-model="merchantId" />

      <div>
        <label class="block text-sm font-medium mb-1">Kod czeku (9 cyfr)</label>
        <input
          v-model="code"
          maxlength="9"
          inputmode="numeric"
          placeholder="123456789"
          class="w-full px-3 py-2 border rounded font-mono text-center text-2xl tracking-widest focus:outline-none focus:ring-2 focus:ring-emerald-500"
        />
      </div>

      <div class="flex gap-3 pt-2">
        <button
          type="submit"
          :disabled="!canSubmit()"
          class="flex-1 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white font-medium py-3 rounded"
        >
          Zrealizuj czek
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