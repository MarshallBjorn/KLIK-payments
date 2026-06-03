<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useConfig } from '../composables/useConfig.js'
import { getInfo } from '../api/bank.js'

const { get, clear } = useConfig()
const router = useRouter()
const cfg = get()
const info = ref(null)
let timer = null

async function refresh() {
  try { info.value = (await getInfo()).data } catch { /* nav nie krzyczy */ }
}
onMounted(() => { refresh(); timer = setInterval(refresh, 3000) })
onUnmounted(() => clearInterval(timer))

function logout() { clear(); router.push('/setup') }
</script>

<template>
  <nav class="bg-slate-900 text-white px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-5 text-sm">
      <router-link to="/" class="font-bold text-base">BANK MOCK</router-link>
      <router-link to="/clients" class="hover:underline" active-class="text-emerald-400">Klienci</router-link>
      <router-link to="/pending" class="hover:underline" active-class="text-emerald-400">
        Pending
        <span v-if="info?.pending_count"
              class="ml-1 inline-flex items-center justify-center text-xs bg-emerald-500 text-white rounded-full px-1.5">
          {{ info.pending_count }}
        </span>
      </router-link>
      <router-link to="/p2p" class="hover:underline" active-class="text-emerald-400">P2P</router-link>
      <router-link to="/cheques" class="hover:underline" active-class="text-amber-400">
        Czeki
        <span v-if="info?.cheques_count"
              class="ml-1 inline-flex items-center justify-center text-xs bg-amber-500 text-white rounded-full px-1.5">
          {{ info.cheques_count }}
        </span>
      </router-link>
      <router-link to="/history" class="hover:underline" active-class="text-emerald-400">Historia</router-link>
    </div>
    <div class="flex items-center gap-4 text-sm">
      <span :class="info?.klik_api_key_configured ? 'text-emerald-400' : 'text-red-400'">
        {{ info?.bank_name || '—' }} · strefa: {{ info?.zone || cfg?.zone || '?' }}
      </span>
      <button @click="logout" class="px-3 py-1 bg-slate-700 hover:bg-slate-600 rounded">Zmień backend</button>
    </div>
  </nav>
</template>
