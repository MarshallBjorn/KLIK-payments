<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { listClients, generateCode, getInfo } from '../api/bank.js'
import { useToasts } from '../composables/useToasts.js'

const clients = ref([])
const info = ref(null)
const { push } = useToasts()

const modal = ref(null)        // { code, userName, total, left }
const generating = ref('')
let countdown = null

async function load() {
  try {
    clients.value = await listClients()
    info.value = await getInfo()
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  }
}
onMounted(load)
onUnmounted(() => clearInterval(countdown))

async function onGenerate(c) {
  generating.value = c.id
  try {
    const r = await generateCode(c.id)
    openModal(r.code, c.name, r.expires_in || 120)
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  } finally {
    generating.value = ''
  }
}

function openModal(code, userName, seconds) {
  clearInterval(countdown)
  modal.value = { code, userName, total: seconds, left: seconds }
  countdown = setInterval(() => {
    if (!modal.value) return
    modal.value.left -= 1
    if (modal.value.left <= 0) closeModal()
  }, 1000)
}
function closeModal() { clearInterval(countdown); modal.value = null }

const pct = computed(() => (modal.value ? Math.max(0, (modal.value.left / modal.value.total) * 100) : 0))
</script>

<template>
  <div class="bg-white rounded-lg shadow p-6">
    <h1 class="text-2xl font-bold mb-6">Klienci banku</h1>
    <table class="w-full text-sm">
      <thead class="text-left text-slate-500 border-b">
        <tr><th class="py-2">Klient</th><th class="py-2">ID</th><th class="py-2 text-right">Saldo</th><th class="py-2"></th></tr>
      </thead>
      <tbody>
        <tr v-for="c in clients" :key="c.id" class="border-b last:border-0">
          <td class="py-2">{{ c.name }}</td>
          <td class="py-2 text-xs text-slate-400 font-mono">{{ c.id }}</td>
          <td class="py-2 text-right font-mono">{{ c.balance.toFixed(2) }} {{ info?.currency }}</td>
          <td class="py-2 text-right">
            <button @click="onGenerate(c)" :disabled="generating === c.id"
                    class="px-3 py-1.5 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white rounded text-sm">
              {{ generating === c.id ? '…' : 'Wygeneruj kod KLIK' }}
            </button>
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  <div v-if="modal" class="fixed inset-0 bg-black/40 flex items-center justify-center z-40" @click.self="closeModal">
    <div class="bg-white rounded-xl shadow-2xl p-8 w-96 text-center">
      <p class="text-sm text-slate-500">Kod KLIK dla</p>
      <p class="font-medium mb-4">{{ modal.userName }}</p>
      <div class="text-5xl font-mono font-bold tracking-[0.25em] text-slate-800 mb-4">{{ modal.code }}</div>
      <div class="h-2 bg-slate-200 rounded overflow-hidden mb-2">
        <div class="h-full bg-emerald-500 transition-all duration-1000 ease-linear" :style="{ width: pct + '%' }"></div>
      </div>
      <p class="text-sm text-slate-500 mb-3">Wygasa za {{ modal.left }}s</p>
      <p class="text-xs text-slate-400 mb-4">Wpisz ten kod w terminalu agenta (:5173), podaj kwotę, kliknij „Zapłać”.</p>
      <button @click="closeModal" class="px-4 py-2 bg-slate-200 hover:bg-slate-300 rounded">Zamknij</button>
    </div>
  </div>
</template>
