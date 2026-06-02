<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from '../api/client.js'

// ---------- State ----------
const clients = ref([])
const cheques = ref([])
const loading = ref(false)
const issuing = ref(false)
const error = ref('')
const successMsg = ref('')

// Formularz wystawienia czeku
const form = ref({ user_id: '', amount: '', ttl_hours: 24 })

// Nowo wystawiony czek (pokazany tylko raz)
const newCheque = ref(null)

let pollTimer = null

// ---------- API helpers ----------
const fetchClients = () => api.get('/api/clients').then(r => { clients.value = r.data })
const fetchCheques = () => api.get('/api/cheques').then(r => { cheques.value = r.data })

async function issueCheque() {
  if (!form.value.user_id || !form.value.amount) return
  issuing.value = true
  error.value = ''
  successMsg.value = ''
  newCheque.value = null
  try {
    const r = await api.post(`/api/clients/${encodeURIComponent(form.value.user_id)}/issue-cheque`, {
      user_id: form.value.user_id,
      amount: parseFloat(form.value.amount),
      ttl_seconds: parseInt(form.value.ttl_hours) * 3600,
    })
    newCheque.value = r.data
    successMsg.value = 'Czek wystawiony pomyślnie!'
    form.value.amount = ''
    await fetchCheques()
    await fetchClients()
  } catch (e) {
    error.value = e.response?.data?.detail?.message || e.response?.data?.message || e.message || 'Błąd wystawiania czeku'
  } finally {
    issuing.value = false
  }
}

async function cancelCheque(chequeId) {
  error.value = ''
  successMsg.value = ''
  try {
    await api.post(`/api/cheques/${chequeId}/cancel`)
    successMsg.value = 'Czek anulowany.'
    await fetchCheques()
    await fetchClients()
  } catch (e) {
    error.value = e.response?.data?.detail?.message || e.message || 'Błąd anulowania czeku'
  }
}

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).catch(() => {})
}

function statusColor(status) {
  return {
    ACTIVE: 'bg-emerald-100 text-emerald-800',
    REDEEMED: 'bg-blue-100 text-blue-800',
    CANCELLED: 'bg-red-100 text-red-800',
    EXPIRED: 'bg-slate-100 text-slate-600',
  }[status] || 'bg-gray-100 text-gray-600'
}

function formatDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('pl-PL')
}

function ttlLabel(hours) {
  if (hours < 24) return `${hours}h`
  return `${hours / 24}d`
}

onMounted(async () => {
  await Promise.all([fetchClients(), fetchCheques()])
  pollTimer = setInterval(fetchCheques, 5000)
})
onUnmounted(() => clearInterval(pollTimer))
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-bold mb-1">KLIK Czeki — moduł Cheques</h1>
      <p class="text-sm text-slate-500">
        Wystawiaj czeki dla klientów (9-cyfrowy kod, hold środków), przeglądaj stan,
        anuluj aktywne czeki. Realizacja odbywa się przez agenta w terminalu.
      </p>
    </div>

    <!-- Flash messages -->
    <div v-if="successMsg" class="bg-emerald-50 border border-emerald-200 text-emerald-800 rounded p-3 flex justify-between">
      {{ successMsg }}
      <button @click="successMsg = ''" class="text-emerald-600 hover:text-emerald-800">✕</button>
    </div>
    <div v-if="error" class="bg-red-50 border border-red-200 text-red-800 rounded p-3 flex justify-between">
      {{ error }}
      <button @click="error = ''" class="text-red-600 hover:text-red-800">✕</button>
    </div>

    <!-- Nowy czek — pokazany jednorazowo po wystawieniu -->
    <div v-if="newCheque" class="bg-amber-50 border-2 border-amber-400 rounded-lg p-5">
      <div class="flex items-center gap-2 mb-3">
        <span class="text-2xl">🧾</span>
        <h2 class="text-lg font-bold text-amber-900">Czek wystawiony — kod jednorazowy</h2>
      </div>
      <p class="text-sm text-amber-700 mb-4">
        Kod <strong>widoczny tylko teraz</strong> — KLIK nie udostępnia go ponownie.
        Pokaż klientowi lub wpisz go w terminalu agenta (:5175).
      </p>
      <div class="flex items-center gap-3 mb-3">
        <span class="font-mono text-4xl tracking-[0.3em] font-bold text-amber-900 bg-white border border-amber-300 px-4 py-2 rounded">
          {{ newCheque.code }}
        </span>
        <button
          @click="copyToClipboard(newCheque.code)"
          class="px-3 py-2 bg-amber-200 hover:bg-amber-300 text-amber-900 rounded text-sm"
        >
          Kopiuj
        </button>
      </div>
      <dl class="text-sm space-y-1 text-amber-800">
        <div class="flex gap-4">
          <dt class="text-amber-600">Kwota:</dt>
          <dd class="font-mono font-semibold">{{ newCheque.amount }} {{ newCheque.currency }}</dd>
        </div>
        <div class="flex gap-4">
          <dt class="text-amber-600">Wygasa:</dt>
          <dd>{{ formatDate(newCheque.expires_at) }}</dd>
        </div>
        <div class="flex gap-4">
          <dt class="text-amber-600">ID czeku:</dt>
          <dd class="font-mono text-xs">{{ newCheque.cheque_id }}</dd>
        </div>
      </dl>
      <button @click="newCheque = null" class="mt-4 text-sm text-amber-600 hover:underline">
        Zamknij (kod nie będzie widoczny ponownie)
      </button>
    </div>

    <!-- Formularz wystawienia czeku -->
    <div class="bg-white rounded-lg shadow p-6">
      <h2 class="text-lg font-semibold mb-4">Wystaw czek dla klienta</h2>
      <div class="grid gap-4 sm:grid-cols-3">
        <div>
          <label class="block text-sm font-medium mb-1">Klient</label>
          <select v-model="form.user_id" class="w-full px-3 py-2 border rounded focus:outline-none focus:ring-2 focus:ring-amber-400">
            <option value="">— wybierz klienta —</option>
            <option v-for="c in clients" :key="c.id" :value="c.id">
              {{ c.name }} ({{ c.balance.toFixed(2) }} PLN)
            </option>
          </select>
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Kwota (PLN)</label>
          <input
            v-model="form.amount"
            type="number"
            step="0.01"
            min="0.01"
            placeholder="100.00"
            class="w-full px-3 py-2 border rounded font-mono focus:outline-none focus:ring-2 focus:ring-amber-400"
          />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Ważność czeku</label>
          <select v-model="form.ttl_hours" class="w-full px-3 py-2 border rounded focus:outline-none focus:ring-2 focus:ring-amber-400">
            <option :value="1">1 godzina</option>
            <option :value="6">6 godzin</option>
            <option :value="24">24 godziny (domyślne)</option>
            <option :value="48">48 godzin</option>
            <option :value="72">72 godziny (max)</option>
          </select>
        </div>
      </div>
      <div class="mt-4 flex items-center gap-3">
        <button
          @click="issueCheque"
          :disabled="issuing || !form.user_id || !form.amount"
          class="px-6 py-2 bg-amber-500 hover:bg-amber-600 disabled:bg-slate-300 text-white font-medium rounded"
        >
          <span v-if="issuing">Wystawianie…</span>
          <span v-else>Wystaw czek</span>
        </button>
        <p class="text-xs text-slate-500">
          Środki zostaną zablokowane (hold) — klient nie będzie mógł ich użyć do czasu realizacji lub anulacji.
        </p>
      </div>
    </div>

    <!-- Lista czeków -->
    <div class="bg-white rounded-lg shadow">
      <div class="p-4 border-b flex justify-between items-center">
        <h2 class="text-lg font-semibold">Wystawione czeki</h2>
        <button @click="fetchCheques" class="text-sm text-slate-500 hover:text-slate-700">
          ↻ Odśwież
        </button>
      </div>

      <div v-if="cheques.length === 0" class="p-8 text-center text-slate-400">
        Brak wystawionych czeków.
      </div>

      <div v-else class="divide-y">
        <div v-for="ch in cheques" :key="ch.cheque_id" class="p-4 flex flex-col sm:flex-row sm:items-center gap-3">
          <!-- Kod + status -->
          <div class="flex items-center gap-3 flex-1">
            <div class="text-center">
              <div class="font-mono text-xl font-bold tracking-widest text-slate-700">
                {{ ch.code }}
              </div>
              <span
                class="inline-block text-xs px-2 py-0.5 rounded-full font-medium mt-1"
                :class="statusColor(ch.status)"
              >
                {{ ch.status }}
              </span>
            </div>

            <!-- Szczegóły -->
            <div class="ml-2 flex-1 min-w-0">
              <div class="font-medium">{{ ch.user_name }}</div>
              <div class="text-sm text-slate-600 font-mono">
                {{ ch.amount.toFixed ? ch.amount.toFixed(2) : ch.amount }} {{ ch.currency }}
              </div>
              <div class="text-xs text-slate-400 mt-1">
                <span>Wystawiony: {{ formatDate(ch.issued_at) }}</span>
                <span class="mx-2">·</span>
                <span>Wygasa: {{ formatDate(ch.expires_at) }}</span>
              </div>
              <div v-if="ch.redeemed_at" class="text-xs text-blue-600 mt-0.5">
                ✓ Zrealizowany: {{ formatDate(ch.redeemed_at) }}
                <span v-if="ch.transaction_id" class="font-mono ml-1">
                  tx: {{ ch.transaction_id.slice(0, 8) }}…
                </span>
              </div>
              <div v-if="ch.cancelled_at && ch.status !== 'REDEEMED'" class="text-xs text-red-600 mt-0.5">
                Zakończony: {{ formatDate(ch.cancelled_at) }}
              </div>
            </div>
          </div>

          <!-- Akcje -->
          <div class="flex gap-2 shrink-0">
            <button
              v-if="ch.status === 'ACTIVE'"
              @click="cancelCheque(ch.cheque_id)"
              class="px-3 py-1.5 bg-red-100 hover:bg-red-200 text-red-700 rounded text-sm font-medium"
            >
              Anuluj
            </button>
            <button
              @click="copyToClipboard(ch.cheque_id)"
              class="px-3 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-600 rounded text-sm"
              title="Kopiuj ID czeku"
            >
              ID
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Info o flow -->
    <div class="bg-slate-50 border border-slate-200 rounded-lg p-4 text-sm text-slate-600">
      <h3 class="font-semibold mb-2 text-slate-700">Jak działa KLIK Czek?</h3>
      <ol class="space-y-1 list-decimal list-inside">
        <li>Bank wystawia czek → KLIK rejestruje, zwraca <strong>9-cyfrowy kod</strong> (jednorazowy)</li>
        <li>Klient pokazuje kasjerowi kod → kasjer wpisuje go w terminalu agenta (:5175 → zakładka „Czek")</li>
        <li>Agent wywołuje KLIK <code>/cheques/redeem</code> → KLIK atomowo zmienia stan na REDEEMED</li>
        <li>KLIK wysyła webhook <code>/webhook/cheques/redeemed</code> → hold zwalniany, debet księgowany</li>
        <li>Jeśli klient chce anulować → klik „Anuluj" → KLIK webhook <code>/cheques/released</code> → hold zwracany</li>
      </ol>
    </div>
  </div>
</template>
