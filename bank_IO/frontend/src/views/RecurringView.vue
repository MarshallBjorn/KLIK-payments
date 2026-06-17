<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import {
  createRecurring,
  listRecurring,
  listRecurringExecutions,
  pauseRecurring,
  resumeRecurring,
  cancelRecurring,
  revokeRecurringLocally,
} from '../api/bank.js'
import { api } from '../api/client.js'

// ---------- State ----------
const clients = ref([])
const mandates = ref([])
const creating = ref(false)
const error = ref('')
const successMsg = ref('')

// Historia runów rozwiniętego mandate-a
const expandedId = ref(null)
const executions = ref([])
const executionsLoading = ref(false)

const today = new Date().toISOString().slice(0, 10)
const form = ref({
  user_id: '',
  recipient_phone: '',
  amount: '',
  cycle: 'MONTHLY',
  start_date: today,
  end_date: '',
  pin: '',
})

let pollTimer = null

// ---------- API helpers ----------
const fetchClients = () => api.get('/api/clients').then(r => { clients.value = r })
const fetchMandates = () => listRecurring().then(r => { mandates.value = r })

async function submitCreate() {
  creating.value = true
  error.value = ''
  successMsg.value = ''
  try {
    const r = await createRecurring(form.value.user_id, {
      recipient_phone: form.value.recipient_phone,
      amount: parseFloat(form.value.amount),
      cycle: form.value.cycle,
      start_date: form.value.start_date,
      end_date: form.value.end_date || null,
      pin: form.value.pin,
    })
    successMsg.value = `Zlecenie aktywne. Pierwszy przelew: ${formatDate(r.next_run_at)}.`
    form.value.amount = ''
    form.value.pin = ''
    await Promise.all([fetchMandates(), fetchClients()])
  } catch (e) {
    error.value = e.body?.detail?.message || e.body?.error?.message || e.message || 'Błąd tworzenia zlecenia'
  } finally {
    creating.value = false
  }
}

async function doAction(id, fn, okMsg) {
  error.value = ''
  successMsg.value = ''
  try {
    await fn(id)
    successMsg.value = okMsg
    await fetchMandates()
    if (expandedId.value === id) await loadExecutions(id)
  } catch (e) {
    error.value = e.body?.detail?.message || e.body?.error?.message || e.message || 'Błąd operacji'
  }
}

async function toggleExecutions(id) {
  if (expandedId.value === id) {
    expandedId.value = null
    executions.value = []
    return
  }
  expandedId.value = id
  await loadExecutions(id)
}

async function loadExecutions(id) {
  executionsLoading.value = true
  try {
    const r = await listRecurringExecutions(id, 50)
    executions.value = r.items || []
  } catch (e) {
    executions.value = []
    error.value = e.message || 'Błąd pobierania historii runów'
  } finally {
    executionsLoading.value = false
  }
}

// ---------- UI helpers ----------
function statusColor(status) {
  return {
    ACTIVE: 'bg-emerald-100 text-emerald-800',
    PAUSED: 'bg-amber-100 text-amber-800',
    CANCELLED: 'bg-red-100 text-red-800',
    COMPLETED: 'bg-blue-100 text-blue-800',
  }[status] || 'bg-gray-100 text-gray-600'
}

function runStatusColor(status) {
  return {
    SUCCESS: 'bg-emerald-100 text-emerald-800',
    FAILED: 'bg-red-100 text-red-800',
    SKIPPED: 'bg-slate-100 text-slate-600',
    EXECUTING: 'bg-amber-100 text-amber-800',
    SCHEDULED: 'bg-slate-100 text-slate-600',
  }[status] || 'bg-gray-100 text-gray-600'
}

function cycleLabel(cycle) {
  return { DAILY: 'codziennie', WEEKLY: 'co tydzień', MONTHLY: 'co miesiąc' }[cycle] || cycle
}

function formatDate(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('pl-PL')
}

function clientPhone(userId) {
  return clients.value.find(c => c.id === userId)?.phone || ''
}

onMounted(async () => {
  await Promise.all([fetchClients(), fetchMandates()])
  pollTimer = setInterval(fetchMandates, 5000)
})
onUnmounted(() => clearInterval(pollTimer))
</script>

<template>
  <div class="space-y-6">
    <div>
      <h1 class="text-2xl font-bold mb-1">KLIK Zlecenia stałe — moduł Recurring</h1>
      <p class="text-sm text-slate-500">
        Twórz zlecenia stałe (mandate podpisany PIN-em), wstrzymuj, wznawiaj i anuluj.
        KLIK trigger-uje wykonania cronem — bank dostaje webhook <code>/recurring/execute</code>
        i sam wykonuje przelew RTP.
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

    <!-- Formularz nowego zlecenia -->
    <div class="bg-white rounded-lg shadow p-6">
      <h2 class="text-lg font-semibold mb-4">Nowe zlecenie stałe</h2>
      <div class="grid gap-4 sm:grid-cols-3">
        <div>
          <label class="block text-sm font-medium mb-1">Klient (płatnik)</label>
          <select v-model="form.user_id" class="w-full px-3 py-2 border rounded focus:outline-none focus:ring-2 focus:ring-violet-400">
            <option value="">— wybierz klienta —</option>
            <option v-for="c in clients" :key="c.id" :value="c.id">
              {{ c.name }} ({{ c.balance.toFixed(2) }} PLN)
            </option>
          </select>
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Telefon odbiorcy (alias P2P)</label>
          <input
            v-model="form.recipient_phone"
            type="text"
            placeholder="+48501111111"
            class="w-full px-3 py-2 border rounded font-mono focus:outline-none focus:ring-2 focus:ring-violet-400"
          />
          <p class="text-xs text-slate-400 mt-1">
            Odbiorca musi mieć zarejestrowany alias (zakładka P2P).
          </p>
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Kwota (PLN)</label>
          <input
            v-model="form.amount"
            type="number"
            step="0.01"
            min="0.01"
            placeholder="50.00"
            class="w-full px-3 py-2 border rounded font-mono focus:outline-none focus:ring-2 focus:ring-violet-400"
          />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Cykl</label>
          <select v-model="form.cycle" class="w-full px-3 py-2 border rounded focus:outline-none focus:ring-2 focus:ring-violet-400">
            <option value="DAILY">Codziennie</option>
            <option value="WEEKLY">Co tydzień</option>
            <option value="MONTHLY">Co miesiąc</option>
          </select>
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Data startu</label>
          <input v-model="form.start_date" type="date" :min="today"
                 class="w-full px-3 py-2 border rounded focus:outline-none focus:ring-2 focus:ring-violet-400" />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Data końca (opcjonalna)</label>
          <input v-model="form.end_date" type="date" :min="form.start_date"
                 class="w-full px-3 py-2 border rounded focus:outline-none focus:ring-2 focus:ring-violet-400" />
          <p class="text-xs text-slate-400 mt-1">Puste = zlecenie bezterminowe.</p>
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">PIN klienta (podpis mandate)</label>
          <input
            v-model="form.pin"
            type="password"
            maxlength="4"
            placeholder="••••"
            class="w-full px-3 py-2 border rounded font-mono tracking-widest focus:outline-none focus:ring-2 focus:ring-violet-400"
          />
        </div>
      </div>
      <div class="mt-4 flex items-center gap-3">
        <button
          @click="submitCreate"
          :disabled="creating || !form.user_id || !form.recipient_phone || !form.amount || !form.pin"
          class="px-6 py-2 bg-violet-500 hover:bg-violet-600 disabled:bg-slate-300 text-white font-medium rounded"
        >
          <span v-if="creating">Rejestrowanie…</span>
          <span v-else>Podpisz PIN-em i aktywuj</span>
        </button>
        <p class="text-xs text-slate-500">
          Autoryzacja jednorazowa — kolejne wykonania nie wymagają potwierdzenia klienta.
        </p>
      </div>
    </div>

    <!-- Lista zleceń -->
    <div class="bg-white rounded-lg shadow">
      <div class="p-4 border-b flex justify-between items-center">
        <h2 class="text-lg font-semibold">Zlecenia stałe</h2>
        <button @click="fetchMandates" class="text-sm text-slate-500 hover:text-slate-700">
          ↻ Odśwież
        </button>
      </div>

      <div v-if="mandates.length === 0" class="p-8 text-center text-slate-400">
        Brak zleceń stałych.
      </div>

      <div v-else class="divide-y">
        <div v-for="m in mandates" :key="m.recurring_transfer_id" class="p-4">
          <div class="flex flex-col sm:flex-row sm:items-center gap-3">
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 flex-wrap">
                <span class="font-medium">{{ m.user_name }}</span>
                <span class="text-slate-400">→</span>
                <span class="font-mono text-sm">{{ m.recipient_phone }}</span>
                <span class="inline-block text-xs px-2 py-0.5 rounded-full font-medium" :class="statusColor(m.status)">
                  {{ m.status }}
                </span>
                <span v-if="m.revoked_locally" class="inline-block text-xs px-2 py-0.5 rounded-full font-medium bg-orange-100 text-orange-800"
                      title="Klient odwołał mandate w banku — KLIK dowie się przy następnym wykonaniu">
                  odwołany lokalnie
                </span>
              </div>
              <div class="text-sm text-slate-600 font-mono mt-0.5">
                {{ m.amount.toFixed ? m.amount.toFixed(2) : m.amount }} {{ m.currency }}
                <span class="text-slate-400 font-sans">· {{ cycleLabel(m.cycle) }}</span>
              </div>
              <div class="text-xs text-slate-400 mt-1">
                <span>Start: {{ m.start_date }}</span>
                <span class="mx-1">·</span>
                <span>Koniec: {{ m.end_date || 'bezterminowo' }}</span>
                <span v-if="m.status === 'ACTIVE'" class="mx-1">·</span>
                <span v-if="m.status === 'ACTIVE'">Najbliższy przelew: {{ formatDate(m.next_run_at) }}</span>
              </div>
              <div v-if="m.last_run_at" class="text-xs mt-0.5"
                   :class="m.last_run_status === 'EXECUTED' ? 'text-emerald-600' : 'text-red-600'">
                Ostatni run: {{ formatDate(m.last_run_at) }} — {{ m.last_run_status }}
                <span v-if="m.last_run_detail" class="font-mono">({{ m.last_run_detail }})</span>
              </div>
              <div v-if="m.end_reason && m.status !== 'ACTIVE' && m.status !== 'PAUSED'" class="text-xs text-slate-500 mt-0.5">
                Powód zakończenia: {{ m.end_reason }}
              </div>
            </div>

            <!-- Akcje -->
            <div class="flex gap-2 shrink-0 flex-wrap">
              <button
                v-if="m.status === 'ACTIVE'"
                @click="doAction(m.recurring_transfer_id, pauseRecurring, 'Zlecenie wstrzymane.')"
                class="px-3 py-1.5 bg-amber-100 hover:bg-amber-200 text-amber-800 rounded text-sm font-medium"
              >
                Wstrzymaj
              </button>
              <button
                v-if="m.status === 'PAUSED'"
                @click="doAction(m.recurring_transfer_id, resumeRecurring, 'Zlecenie wznowione.')"
                class="px-3 py-1.5 bg-emerald-100 hover:bg-emerald-200 text-emerald-800 rounded text-sm font-medium"
              >
                Wznów
              </button>
              <button
                v-if="m.status === 'ACTIVE' || m.status === 'PAUSED'"
                @click="doAction(m.recurring_transfer_id, cancelRecurring, 'Zlecenie anulowane.')"
                class="px-3 py-1.5 bg-red-100 hover:bg-red-200 text-red-700 rounded text-sm font-medium"
              >
                Anuluj
              </button>
              <button
                v-if="m.status === 'ACTIVE' && !m.revoked_locally"
                @click="doAction(m.recurring_transfer_id, revokeRecurringLocally, 'Mandate odwołany lokalnie — KLIK dowie się przy następnym wykonaniu.')"
                class="px-3 py-1.5 bg-orange-100 hover:bg-orange-200 text-orange-800 rounded text-sm"
                title="Edge case: klient odwołuje w banku, bank nie informuje KLIK"
              >
                Odwołaj lokalnie
              </button>
              <button
                @click="toggleExecutions(m.recurring_transfer_id)"
                class="px-3 py-1.5 bg-slate-100 hover:bg-slate-200 text-slate-600 rounded text-sm"
              >
                {{ expandedId === m.recurring_transfer_id ? 'Zwiń' : 'Historia runów' }}
              </button>
            </div>
          </div>

          <!-- Historia runów (rozwijana) -->
          <div v-if="expandedId === m.recurring_transfer_id" class="mt-3 bg-slate-50 rounded p-3">
            <div v-if="executionsLoading" class="text-sm text-slate-400">Ładowanie…</div>
            <div v-else-if="executions.length === 0" class="text-sm text-slate-400">
              Brak wykonań — pierwszy run o godzinie execution w dniu startu.
            </div>
            <table v-else class="w-full text-sm">
              <thead>
                <tr class="text-left text-xs text-slate-400">
                  <th class="py-1 pr-3">Zaplanowany</th>
                  <th class="py-1 pr-3">Wykonany</th>
                  <th class="py-1 pr-3">Status</th>
                  <th class="py-1">Szczegóły</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="ex in executions" :key="ex.execution_id" class="border-t border-slate-200">
                  <td class="py-1.5 pr-3">{{ formatDate(ex.scheduled_for) }}</td>
                  <td class="py-1.5 pr-3">{{ formatDate(ex.executed_at) }}</td>
                  <td class="py-1.5 pr-3">
                    <span class="inline-block text-xs px-2 py-0.5 rounded-full font-medium" :class="runStatusColor(ex.status)">
                      {{ ex.status }}
                    </span>
                  </td>
                  <td class="py-1.5 font-mono text-xs text-slate-500">
                    {{ ex.rtp_reference || ex.failure_reason || '—' }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- Info o flow -->
    <div class="bg-slate-50 border border-slate-200 rounded-lg p-4 text-sm text-slate-600">
      <h3 class="font-semibold mb-2 text-slate-700">Jak działa KLIK Recurring?</h3>
      <ol class="space-y-1 list-decimal list-inside">
        <li>Klient podpisuje mandate PIN-em → bank rejestruje w KLIK <code>POST /recurring/create</code> (autoryzacja jednorazowa)</li>
        <li>Cron KLIK (co 5 min) sprawdza terminy → przy <code>next_run_at</code> robi lookup aliasu (płatny) i uderza w <code>POST /webhook/recurring/execute</code></li>
        <li>Bank sprawdza lokalny mandate + saldo, wykonuje przelew RTP i odpowiada <code>EXECUTED</code> (z <code>rtp_reference</code>) lub <code>REJECTED</code></li>
        <li>3 odrzucenia z rzędu → KLIK auto-pauzuje i wysyła <code>/webhook/recurring/auto-paused</code> (push do klienta, „Wznów” odblokowuje)</li>
        <li>„Odwołaj lokalnie” symuluje klienta kasującego zlecenie w banku — następny run zwraca <code>MANDATE_REVOKED_LOCALLY</code> i KLIK sam anuluje mandate</li>
      </ol>
    </div>
  </div>
</template>
