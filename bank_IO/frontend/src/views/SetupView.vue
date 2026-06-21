<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useConfig } from '../composables/useConfig.js'
import { getInfo, setApiKey } from '../api/bank.js'
import { useToasts } from '../composables/useToasts.js'

const { get, save } = useConfig()
const router = useRouter()
const { push } = useToasts()

const existing = get()
const backendUrl = ref(existing?.backendUrl || 'http://localhost:8100')
const apiKey = ref('')
const info = ref(null)
const error = ref('')

async function loadInfo() {
  if (!get()?.backendUrl) { info.value = null; return }
  try { info.value = await getInfo() } catch { info.value = null }
}
onMounted(loadInfo)

function saveBackend() {
  if (!backendUrl.value.trim()) { error.value = 'Podaj URL backendu banku'; return }
  save({ backendUrl: backendUrl.value.trim().replace(/\/$/, '') })
  error.value = ''
  loadInfo()
}

async function saveKey() {
  if (!apiKey.value.trim()) return
  try {
    const r = await setApiKey(apiKey.value.trim())
    push(`Klucz KLIK ustawiony (${r.preview}).`, 'success')
    apiKey.value = ''
    loadInfo()
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  }
}

function goToApp() { router.push('/') }
</script>

<template>
  <div class="max-w-md mx-auto mt-12 space-y-4">
    <div class="bg-white rounded-lg shadow p-8">
      <h1 class="text-2xl font-bold mb-1">Mock Banku — konfiguracja</h1>
      <p class="text-sm text-slate-500 mb-6">Adres backendu mocka (FastAPI, :8100).</p>
      <form @submit.prevent="saveBackend" class="space-y-3">
        <input v-model="backendUrl" placeholder="http://localhost:8100"
               class="w-full px-3 py-2 border rounded focus:outline-none focus:ring-2 focus:ring-emerald-500" />
        <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-medium py-2 rounded">Zapisz URL</button>
      </form>
    </div>

    <div v-if="info" class="bg-white rounded-lg shadow p-8">
      <h2 class="font-bold mb-1">Klucz KLIK API banku</h2>
      <p class="text-sm mb-4">
        Status:
        <span :class="info.klik_api_key_configured ? 'text-emerald-600 font-medium' : 'text-red-600 font-medium'">
          {{ info.klik_api_key_configured ? `skonfigurowany (${info.klik_api_key_preview})` : 'BRAK' }}
        </span>
      </p>
      <form @submit.prevent="saveKey" class="space-y-3">
        <input v-model="apiKey" type="password" placeholder="wklej api_key z Django Admin"
               class="w-full px-3 py-2 border rounded font-mono focus:outline-none focus:ring-2 focus:ring-emerald-500" />
        <button type="submit" class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-medium py-2 rounded">
          {{ info.klik_api_key_configured ? 'Podmień klucz' : 'Ustaw klucz' }}
        </button>
      </form>
      <button @click="goToApp" class="mt-4 w-full px-4 py-2 bg-slate-200 hover:bg-slate-300 rounded">Przejdź do aplikacji</button>
    </div>

    <div v-if="error" class="text-red-600 text-sm text-center">{{ error }}</div>
  </div>
</template>
