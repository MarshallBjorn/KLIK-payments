<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useConfig } from '../composables/useConfig.js'

const { get, save } = useConfig()
const router = useRouter()

const existing = get()
const apiKey = ref(existing?.apiKey || '')
const zone = ref(existing?.zone || 'PL')
const baseUrl = ref(existing?.baseUrl || '/api/v1')
const error = ref('')

const ZONE_CURRENCY = { PL: 'PLN', EU: 'EUR', UK: 'GBP', US: 'USD' }

function submit() {
  if (!apiKey.value.trim()) {
    error.value = 'Podaj klucz API agenta'
    return
  }
  save({
    apiKey: apiKey.value.trim(),
    zone: zone.value,
    currency: ZONE_CURRENCY[zone.value],
    baseUrl: baseUrl.value,
  })
  router.push('/terminal/c2b')
}
</script>

<template>
  <div class="max-w-md mx-auto mt-12 bg-white rounded-lg shadow p-8">
    <h1 class="text-2xl font-bold mb-1">KLIK Agent — Konfiguracja</h1>
    <p class="text-sm text-slate-500 mb-6">Wprowadź dane uwierzytelniające terminala.</p>

    <form @submit.prevent="submit" class="space-y-4">
      <div>
        <label class="block text-sm font-medium mb-1">Klucz API agenta</label>
        <input
          v-model="apiKey"
          type="password"
          placeholder="klik_..."
          class="w-full px-3 py-2 border rounded focus:outline-none focus:ring-2 focus:ring-emerald-500"
        />
      </div>

      <div>
        <label class="block text-sm font-medium mb-1">Strefa</label>
        <select
          v-model="zone"
          class="w-full px-3 py-2 border rounded focus:outline-none focus:ring-2 focus:ring-emerald-500"
        >
          <option value="PL">PL — PLN</option>
          <option value="EU">EU — EUR</option>
          <option value="UK">UK — GBP</option>
          <option value="US">US — USD</option>
        </select>
      </div>

      <div v-if="error" class="text-red-600 text-sm">{{ error }}</div>

      <button
        type="submit"
        class="w-full bg-emerald-600 hover:bg-emerald-700 text-white font-medium py-2 rounded"
      >
        Zapisz i zaloguj
      </button>
    </form>
  </div>
</template>
