<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { listPending, acceptPending, rejectPending } from '../api/bank.js'
import { useToasts } from '../composables/useToasts.js'

const REJECT_REASONS = ['INSUFFICIENT_FUNDS', 'USER_DECLINED', 'PIN_FAILED', 'AML_BLOCK', 'OTHER']

const items = ref([])
const { push } = useToasts()
let timer = null
const seenIds = new Set()
const resolvedIds = new Set()   // tx, które sami zamknęliśmy (accept/reject)

const pinModal = ref(null)      // { tx }
const pin = ref('')
const busy = ref('')

async function refresh() {
  try {
    const next = await listPending()
    const nextIds = new Set(next.map((i) => i.transaction_id))
    for (const id of seenIds) {
      if (!nextIds.has(id) && !resolvedIds.has(id)) {
        push('Autoryzacja wygasła zanim została zatwierdzona.', 'info')
      }
    }
    seenIds.clear()
    next.forEach((i) => seenIds.add(i.transaction_id))
    items.value = next
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  }
}
onMounted(() => { refresh(); timer = setInterval(refresh, 2000) })
onUnmounted(() => clearInterval(timer))

function onAuthorize(tx) {
  // brak środków → nie pokazujemy PIN-u, od razu odrzucamy z INSUFFICIENT_FUNDS
  if (tx.client_balance !== null && !tx.sufficient_balance) {
    doReject(tx, 'INSUFFICIENT_FUNDS')
    return
  }
  pinModal.value = { tx }
  pin.value = '1234'
}
function closePin() { pinModal.value = null; pin.value = '' }

async function confirmAccept() {
  const tx = pinModal.value.tx
  busy.value = tx.transaction_id
  try {
    const r = await acceptPending(tx.transaction_id, pin.value)
    resolvedIds.add(tx.transaction_id)
    if (r.auto_rejected) push(`Odrzucono automatycznie: ${r.reject_reason}`, 'error')
    else push(`Zaakceptowano. Merchant net: ${r.merchant_net ?? '—'} ${r.currency ?? ''}`, 'success')
    closePin()
    refresh()
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  } finally {
    busy.value = ''
  }
}

async function doReject(tx, reason, ev) {
  if (ev) { const d = ev.target.closest('details'); if (d) d.removeAttribute('open') }
  busy.value = tx.transaction_id
  try {
    await rejectPending(tx.transaction_id, reason)
    resolvedIds.add(tx.transaction_id)
    push(`Odrzucono (${reason}).`, 'success')
    refresh()
  } catch (e) {
    push(`${e.code ? e.code + ': ' : ''}${e.message}`, 'error')
  } finally {
    busy.value = ''
  }
}

function fmt(s) {
  if (s == null) return '—'
  const m = Math.floor(s / 60)
  const ss = s % 60
  return `${m}:${String(ss).padStart(2, '0')}`
}
</script>

<template>
  <div>
    <h1 class="text-2xl font-bold mb-6">Oczekujące autoryzacje</h1>

    <div v-if="!items.length" class="text-slate-500 bg-white rounded-lg shadow p-6">
      Brak oczekujących autoryzacji. Wygeneruj kod w zakładce „Klienci” i użyj go w terminalu agenta (:5173).
    </div>

    <div class="grid gap-4 md:grid-cols-2">
      <div v-for="tx in items" :key="tx.transaction_id"
           class="bg-white rounded-lg shadow p-5 border-l-4"
           :class="tx.sufficient_balance ? 'border-emerald-500' : 'border-red-500'">
        <div class="flex justify-between items-start">
          <div>
            <div class="font-semibold">{{ tx.user_name }}</div>
            <div class="text-sm text-slate-600">
              {{ tx.merchant_name }} — <span class="font-mono">{{ tx.amount }} {{ tx.currency }}</span>
            </div>
          </div>
          <div class="text-right text-xs text-slate-400">
            <div>tx {{ tx.transaction_id.slice(0, 8) }}…</div>
            <div v-if="tx.is_on_us" class="text-slate-500">on-us</div>
          </div>
        </div>

        <div class="mt-2 text-sm">
          <span class="text-slate-500">Wygasa za:</span> <span class="font-mono">{{ fmt(tx.seconds_left) }}</span>
          <template v-if="tx.client_balance !== null">
            <span class="ml-3 text-slate-500">saldo:</span>
            <span class="font-mono" :class="tx.sufficient_balance ? '' : 'text-red-600'">{{ tx.client_balance.toFixed(2) }}</span>
          </template>
        </div>
        <div v-if="tx.client_balance !== null && !tx.sufficient_balance" class="mt-1 text-xs text-red-600">
          Brak środków — „Autoryzuj” odrzuci automatycznie (INSUFFICIENT_FUNDS).
        </div>

        <div class="mt-4 flex gap-2 items-center">
          <button @click="onAuthorize(tx)" :disabled="busy === tx.transaction_id"
                  class="px-3 py-2 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white rounded text-sm">
            Autoryzuj PIN-em
          </button>
          <details class="relative">
            <summary class="px-3 py-2 bg-slate-200 hover:bg-slate-300 rounded text-sm cursor-pointer list-none select-none">Odrzuć ▾</summary>
            <div class="absolute mt-1 bg-white border rounded shadow z-10 w-56">
              <button v-for="r in REJECT_REASONS" :key="r" @click="doReject(tx, r, $event)"
                      class="block w-full text-left px-3 py-2 hover:bg-slate-100 text-sm">{{ r }}</button>
            </div>
          </details>
        </div>
      </div>
    </div>

    <div v-if="pinModal" class="fixed inset-0 bg-black/40 flex items-center justify-center z-40" @click.self="closePin">
      <div class="bg-white rounded-xl shadow-2xl p-8 w-80">
        <h3 class="font-bold mb-1">Autoryzacja PIN-em</h3>
        <p class="text-sm text-slate-500 mb-4">
          {{ pinModal.tx.user_name }} — {{ pinModal.tx.merchant_name }} {{ pinModal.tx.amount }} {{ pinModal.tx.currency }}
        </p>
        <input v-model="pin" maxlength="8" inputmode="numeric" type="password" @keyup.enter="confirmAccept"
               class="w-full px-3 py-2 border rounded font-mono text-center text-2xl tracking-widest mb-2 focus:outline-none focus:ring-2 focus:ring-emerald-500" />
        <p class="text-xs text-slate-400 mb-4">PIN klientów demo: 1234</p>
        <div class="flex gap-2">
          <button @click="confirmAccept" :disabled="!!busy"
                  class="flex-1 bg-emerald-600 hover:bg-emerald-700 disabled:bg-slate-300 text-white py-2 rounded">Autoryzuj</button>
          <button @click="closePin" class="px-4 py-2 bg-slate-200 hover:bg-slate-300 rounded">Anuluj</button>
        </div>
      </div>
    </div>
  </div>
</template>
