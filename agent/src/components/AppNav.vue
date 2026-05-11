<script setup>
import { useConfig } from '../composables/useConfig.js'
import { useRouter } from 'vue-router'

const { get, clear } = useConfig()
const router = useRouter()
const cfg = get()

function logout() {
  clear()
  router.push('/setup')
}
</script>

<template>
  <nav class="bg-slate-900 text-white px-6 py-3 flex items-center justify-between">
    <div class="flex items-center gap-6">
      <span class="font-bold text-lg">KLIK Agent</span>
      <router-link to="/terminal/c2b" class="hover:underline" active-class="text-emerald-400">C2B</router-link>
      <router-link to="/terminal/cheque" class="hover:underline" active-class="text-emerald-400">Czek</router-link>
      <router-link to="/history" class="hover:underline" active-class="text-emerald-400">Historia</router-link>
    </div>
    <div class="flex items-center gap-4 text-sm">
      <span class="text-slate-400">strefa: {{ cfg?.zone }}</span>
      <button @click="logout" class="px-3 py-1 bg-slate-700 hover:bg-slate-600 rounded">Wyloguj</button>
    </div>
  </nav>
</template>
