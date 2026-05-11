<script setup>
import { ref, onMounted } from 'vue'
import { listPaymentHistory } from '../api/payments.js'

const items = ref([])
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    items.value = await listPaymentHistory()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
})

function statusClass(s) {
  return {
    COMPLETED: 'bg-emerald-100 text-emerald-800',
    PENDING: 'bg-amber-100 text-amber-800',
    AUTHORIZED: 'bg-blue-100 text-blue-800',
    REJECTED: 'bg-red-100 text-red-800',
    TIMEOUT: 'bg-red-100 text-red-800',
  }[s] || 'bg-slate-100 text-slate-800'
}

function formatDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('pl-PL')
}
</script>

<template>
  <div class="bg-white rounded-lg shadow p-6">
    <h1 class="text-2xl font-bold mb-6">Historia transakcji</h1>

    <div v-if="loading" class="text-slate-500">Ładowanie…</div>
    <div v-else-if="error" class="text-red-600">{{ error }}</div>
    <div v-else-if="!items.length" class="text-slate-500">Brak transakcji.</div>

    <table v-else class="w-full text-sm">
      <thead class="text-left text-slate-500 border-b">
        <tr>
          <th class="py-2">Data</th>
          <th class="py-2">Status</th>
          <th class="py-2 text-right">Kwota</th>
          <th class="py-2 text-right">Merchant net</th>
          <th class="py-2">ID</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="t in items" :key="t.transaction_id" class="border-b last:border-0">
          <td class="py-2">{{ formatDate(t.created_at) }}</td>
          <td class="py-2">
            <span class="px-2 py-0.5 rounded text-xs font-medium" :class="statusClass(t.status)">{{ t.status }}</span>
          </td>
          <td class="py-2 text-right font-mono">{{ t.amount_gross }} {{ t.currency }}</td>
          <td class="py-2 text-right font-mono">{{ t.merchant_net || '—' }}</td>
          <td class="py-2 text-xs text-slate-400 font-mono">{{ t.transaction_id.slice(0, 8) }}…</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
