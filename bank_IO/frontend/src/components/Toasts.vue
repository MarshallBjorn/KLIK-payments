<script setup>
import { useToasts } from '../composables/useToasts.js'

const { toasts, dismiss } = useToasts()
const cls = (t) => ({ error: 'bg-red-600', success: 'bg-emerald-600', info: 'bg-slate-800' }[t] || 'bg-slate-800')
</script>

<template>
  <div class="fixed bottom-4 right-4 space-y-2 z-50 w-80">
    <div v-for="t in toasts" :key="t.id" :class="cls(t.type)"
         class="text-white rounded-lg shadow-lg px-4 py-3 text-sm flex justify-between gap-3">
      <span>{{ t.message }}</span>
      <button @click="dismiss(t.id)" class="opacity-70 hover:opacity-100">✕</button>
    </div>
  </div>
</template>
