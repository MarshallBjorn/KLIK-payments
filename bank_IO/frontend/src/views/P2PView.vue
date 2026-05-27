<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import {
  listClients,
  listAliases,
  registerAlias,
  deleteAlias,
  lookupAlias,
  getInfo,
} from '../api/bank.js'
import { useToasts } from '../composables/useToasts.js'

const { push } = useToasts()

const clients = ref([])
const aliases = ref([])
const info = ref(null)

const busyRegister = ref('')  // user_id w trakcie rejestracji
const busyDelete = ref('')    // phone w trakcie usuwania

const lookupPhone = ref('')
const lookupBusy = ref(false)
const lookupResult = ref(null)  // null | { found, ... }

let timer = null

async function refreshAll() {
  try {
    const [c, a, i] = await Promise.all([listClients(), listAliases(), getInfo()])
    clients.value = c
    aliases.value = a
    info.value = i
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  }
}

onMounted(() => {
  refreshAll()
  // Co 5s — żeby reagować na zmiany dokonane np. przez smoke testy / inną sesję.
  timer = setInterval(refreshAll, 5000)
})
onUnmounted(() => clearInterval(timer))

async function onRegister(client) {
  if (!client.phone) {
    push('Klient nie ma przypisanego telefonu', 'error')
    return
  }
  busyRegister.value = client.id
  try {
    await registerAlias(client.id, null)  // backend weźmie phone z clients[]
    push(`Alias ${client.phone} zarejestrowany`, 'success')
    await refreshAll()
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  } finally {
    busyRegister.value = ''
  }
}

async function onDelete(alias) {
  if (!confirm(`Usunąć alias ${alias.phone} (${alias.user_name})?`)) return
  busyDelete.value = alias.phone
  try {
    await deleteAlias(alias.phone)
    push(`Alias ${alias.phone} usunięty`, 'success')
    await refreshAll()
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  } finally {
    busyDelete.value = ''
  }
}

async function onLookup() {
  const phone = lookupPhone.value.trim()
  if (!phone) {
    push('Podaj numer telefonu', 'error')
    return
  }
  lookupBusy.value = true
  lookupResult.value = null
  try {
    lookupResult.value = await lookupAlias(phone)
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  } finally {
    lookupBusy.value = false
  }
}

function clearLookup() {
  lookupResult.value = null
  lookupPhone.value = ''
}

// Klienci, dla których alias NIE jest jeszcze zarejestrowany (do akcji "Zarejestruj")
const unregisteredClients = computed(() =>
  clients.value.filter((c) => c.phone && !c.alias_registered)
)
</script>

<template>
  <div class="space-y-6">
    <!-- ====================== REGISTRY PANEL ====================== -->
    <div class="bg-white rounded-lg shadow p-6">
      <h1 class="text-2xl font-bold mb-1">Aliasy P2P (Telefony)</h1>
      <p class="text-sm text-slate-500 mb-6">
        Rejestr telefon → konto klienta tego banku w KLIK. Inne banki używają lookup-u
        żeby ustalić routing przelewu P2P (Elixir / SEPA Instant / FedNow RTP — poza KLIK).
      </p>

      <!-- Klienci dostępni do rejestracji -->
      <h2 class="text-sm font-semibold uppercase text-slate-500 mb-2">Klienci banku</h2>
      <table class="w-full text-sm mb-6">
        <thead class="text-left text-slate-500 border-b">
          <tr>
            <th class="py-2">Klient</th>
            <th class="py-2">Telefon</th>
            <th class="py-2 font-mono">IBAN</th>
            <th class="py-2"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in clients" :key="c.id" class="border-b last:border-0">
            <td class="py-2">{{ c.name }}</td>
            <td class="py-2 font-mono">{{ c.phone || '—' }}</td>
            <td class="py-2 font-mono text-xs text-slate-600">{{ c.iban || '—' }}</td>
            <td class="py-2 text-right">
              <span
                v-if="c.alias_registered"
                class="inline-flex items-center text-xs text-emerald-600 bg-emerald-50 px-2 py-1 rounded"
              >
                ✓ zarejestrowany
              </span>
              <button
                v-else
                @click="onRegister(c)"
                :disabled="busyRegister === c.id || !c.phone"
                class="px-3 py-1.5 bg-sky-600 hover:bg-sky-700 disabled:bg-slate-300 text-white rounded text-sm"
              >
                {{ busyRegister === c.id ? '…' : 'Zarejestruj alias' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <h2 class="text-sm font-semibold uppercase text-slate-500 mb-2">
        Zarejestrowane aliasy
        <span class="text-slate-400 font-normal">({{ aliases.length }})</span>
      </h2>
      <div v-if="aliases.length === 0" class="text-sm text-slate-500 italic py-3">
        Brak zarejestrowanych aliasów.
      </div>
      <table v-else class="w-full text-sm">
        <thead class="text-left text-slate-500 border-b">
          <tr>
            <th class="py-2">Telefon</th>
            <th class="py-2">Klient</th>
            <th class="py-2 font-mono">IBAN</th>
            <th class="py-2">Zarejestrowano</th>
            <th class="py-2"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in aliases" :key="a.phone" class="border-b last:border-0">
            <td class="py-2 font-mono">{{ a.phone }}</td>
            <td class="py-2">{{ a.user_name }}</td>
            <td class="py-2 font-mono text-xs text-slate-600">{{ a.iban }}</td>
            <td class="py-2 text-xs text-slate-500">{{ a.registered_at }}</td>
            <td class="py-2 text-right">
              <button
                @click="onDelete(a)"
                :disabled="busyDelete === a.phone"
                class="px-3 py-1.5 bg-rose-600 hover:bg-rose-700 disabled:bg-slate-300 text-white rounded text-sm"
              >
                {{ busyDelete === a.phone ? '…' : 'Usuń' }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- ====================== LOOKUP PANEL ====================== -->
    <div class="bg-white rounded-lg shadow p-6">
      <h2 class="text-lg font-bold mb-1">Lookup numeru telefonu</h2>
      <p class="text-sm text-slate-500 mb-4">
        Sprawdź gdzie KLIK kieruje przelewy P2P na podany numer. To, co robi bank-nadawca
        zanim wyśle przelew przez RTP.
      </p>

      <form @submit.prevent="onLookup" class="flex gap-2 mb-4">
        <input
          v-model="lookupPhone"
          type="tel"
          placeholder="+48501234567"
          class="flex-1 px-3 py-2 border border-slate-300 rounded font-mono"
        />
        <button
          type="submit"
          :disabled="lookupBusy"
          class="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white rounded"
        >
          {{ lookupBusy ? '…' : 'Szukaj' }}
        </button>
        <button
          v-if="lookupResult"
          type="button"
          @click="clearLookup"
          class="px-4 py-2 bg-slate-200 hover:bg-slate-300 text-slate-700 rounded"
        >
          Wyczyść
        </button>
      </form>

      <div
        v-if="lookupResult?.found"
        class="bg-emerald-50 border border-emerald-200 rounded p-4"
      >
        <div class="flex items-center gap-2 mb-3">
          <span class="text-emerald-600 text-lg">✓</span>
          <span class="font-semibold text-emerald-900">Alias znaleziony</span>
        </div>
        <dl class="grid grid-cols-[140px_1fr] gap-y-1 text-sm">
          <dt class="text-slate-500">Telefon:</dt>
          <dd class="font-mono">{{ lookupResult.phone }}</dd>
          <dt class="text-slate-500">Bank odbiorcy:</dt>
          <dd>
            <span class="font-mono">{{ lookupResult.bank_code || lookupResult.bank_id }}</span>
          </dd>
          <dt class="text-slate-500">IBAN:</dt>
          <dd class="font-mono text-xs">{{ lookupResult.iban }}</dd>
          <dt class="text-slate-500">Strefa:</dt>
          <dd>{{ lookupResult.zone || '—' }}</dd>
        </dl>
        <p class="text-xs text-slate-500 mt-3 italic">
          Dalej: bank-nadawca wysyła przelew przez Elixir Express (PL) / SEPA Instant (EU) /
          Faster Payments (UK) / FedNow RTP (US) — poza KLIK.
        </p>
      </div>

      <!-- Wynik: miss -->
      <div
        v-else-if="lookupResult && !lookupResult.found"
        class="bg-amber-50 border border-amber-200 rounded p-4 text-sm"
      >
        <div class="flex items-center gap-2">
          <span class="text-amber-600 text-lg">⚠</span>
          <span class="font-semibold text-amber-900">Numer nie jest zarejestrowany w KLIK</span>
        </div>
        <p class="text-slate-600 mt-1">
          Telefon <span class="font-mono">{{ lookupResult.phone }}</span> nie ma aktywnego
          aliasu P2P. Bank klienta musi go najpierw zarejestrować.
        </p>
      </div>
    </div>
  </div>
</template>
