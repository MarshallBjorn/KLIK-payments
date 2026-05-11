<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { getInfo, listClients } from '../api/bank.js'
import { useToasts } from '../composables/useToasts.js'

const info = ref(null)
const clients = ref([])
const { push } = useToasts()
let timer = null

async function refresh() {
  try {
    info.value = await getInfo()
    clients.value = await listClients()
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  }
}
onMounted(() => { refresh(); timer = setInterval(refresh, 3000) })
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="space-y-6">
    <div class="bg-white rounded-lg shadow p-6">
      <div class="flex items-start justify-between">
        <div>
          <h1 class="text-2xl font-bold">{{ info?.bank_name || 'Bank' }}</h1>
          <p class="text-sm text-slate-500">Strefa {{ info?.zone }} · waluta {{ info?.currency }}</p>
        </div>
        <div class="text-right text-sm">
          <div>
            Status w KLIK:
            <span :class="info?.klik_api_key_configured ? 'text-emerald-600 font-medium' : 'text-red-600 font-medium'">
              {{ info?.klik_api_key_configured ? 'API key skonfigurowany' : 'BRAK API key' }}
            </span>
          </div>
          <div class="text-slate-400 break-all">{{ info?.klik_base_url }}</div>
        </div>
      </div>
      <div class="mt-4 flex gap-4">
        <router-link to="/pending" class="px-4 py-3 rounded bg-amber-50 border border-amber-200 hover:bg-amber-100">
          <div class="text-2xl font-bold text-amber-700">{{ info?.pending_count ?? 0 }}</div>
          <div class="text-xs text-amber-600">oczekujące autoryzacje</div>
        </router-link>
        <router-link to="/clients" class="px-4 py-3 rounded bg-slate-50 border border-slate-200 hover:bg-slate-100">
          <div class="text-2xl font-bold text-slate-700">{{ info?.clients_count ?? 0 }}</div>
          <div class="text-xs text-slate-500">klientów banku</div>
        </router-link>
      </div>
    </div>

    <div class="bg-white rounded-lg shadow p-6">
      <h2 class="font-bold mb-3">Klienci (skrót)</h2>
      <table class="w-full text-sm">
        <thead class="text-left text-slate-500 border-b">
          <tr><th class="py-2">Klient</th><th class="py-2">ID</th><th class="py-2 text-right">Saldo</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in clients" :key="c.id" class="border-b last:border-0">
            <td class="py-2">{{ c.name }}</td>
            <td class="py-2 text-xs text-slate-400 font-mono">{{ c.id }}</td>
            <td class="py-2 text-right font-mono">{{ c.balance.toFixed(2) }} {{ info?.currency }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>