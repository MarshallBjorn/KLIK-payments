<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { listHistory } from '../api/bank.js'
import { useToasts } from '../composables/useToasts.js'

const items = ref([])
const { push } = useToasts()
let timer = null

async function refresh() {
  try { items.value = await listHistory() } catch (e) { push(e.message, 'error') }
}
onMounted(() => { refresh(); timer = setInterval(refresh, 3000) })
onUnmounted(() => clearInterval(timer))

const badge = (t) => ({
  CODE_GENERATED: 'bg-blue-100 text-blue-800',
  WEBHOOK_RECEIVED: 'bg-amber-100 text-amber-800',
  AUTHORIZED: 'bg-emerald-100 text-emerald-800',
  REJECTED: 'bg-red-100 text-red-800',
  EXPIRED: 'bg-slate-200 text-slate-700',
}[t] || 'bg-slate-100 text-slate-800')

function fmt(iso) { return iso ? new Date(iso).toLocaleString('pl-PL') : '—' }
</script>

<template>
  <div class="bg-white rounded-lg shadow p-6">
    <h1 class="text-2xl font-bold mb-6">Audit log</h1>
    <div v-if="!items.length" class="text-slate-500">Brak operacji.</div>
    <table v-else class="w-full text-sm">
      <thead class="text-left text-slate-500 border-b">
        <tr>
          <th class="py-2">Czas</th><th class="py-2">Typ</th><th class="py-2">Klient</th>
          <th class="py-2 text-right">Kwota / kod</th><th class="py-2">Transakcja</th><th class="py-2">Wynik</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="h in items" :key="h.id" class="border-b last:border-0 align-top">
          <td class="py-2 whitespace-nowrap">{{ fmt(h.timestamp) }}</td>
          <td class="py-2"><span class="px-2 py-0.5 rounded text-xs font-medium" :class="badge(h.type)">{{ h.type }}</span></td>
          <td class="py-2">{{ h.user_name || h.user_id || '—' }}</td>
          <td class="py-2 text-right font-mono">
            {{ h.amount ? `${h.amount} ${h.currency || ''}` : (h.code ? `kod ${h.code}` : '—') }}
          </td>
          <td class="py-2 text-xs text-slate-400 font-mono">{{ h.transaction_id ? h.transaction_id.slice(0, 8) + '…' : '—' }}</td>
          <td class="py-2 text-slate-600">
            {{ h.result }}<span v-if="h.reject_reason"> ({{ h.reject_reason }})</span><span v-if="h.merchant_net"> · net {{ h.merchant_net }}</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
