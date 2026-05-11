<script setup>
defineProps({
  status: String,
  result: Object,
  error: String,
})
</script>

<template>
  <div v-if="status === 'idle'" />

  <div v-else-if="status === 'pending'" class="bg-amber-50 border border-amber-200 rounded p-4 text-center">
    <div class="animate-pulse text-amber-700 font-medium">Oczekiwanie na autoryzację banku…</div>
    <div class="text-sm text-amber-600 mt-1">Klient wpisuje PIN w aplikacji bankowej</div>
  </div>

  <div v-else-if="status === 'completed'" class="bg-emerald-50 border border-emerald-200 rounded p-4">
    <div class="text-emerald-800 font-bold text-lg">✓ Płatność zaakceptowana</div>
    <dl class="mt-3 text-sm space-y-1">
      <div class="flex justify-between"><dt>Kwota brutto:</dt><dd class="font-mono">{{ result.amount_gross }} {{ result.currency }}</dd></div>
      <div v-if="result.merchant_net" class="flex justify-between"><dt>Merchant net:</dt><dd class="font-mono">{{ result.merchant_net }} {{ result.currency }}</dd></div>
      <div v-if="result.klik_fee" class="flex justify-between text-slate-500"><dt>Prowizja KLIK:</dt><dd class="font-mono">{{ result.klik_fee }}</dd></div>
      <div v-if="result.agent_fee" class="flex justify-between text-slate-500"><dt>Prowizja agenta:</dt><dd class="font-mono">{{ result.agent_fee }}</dd></div>
    </dl>
  </div>

  <div v-else-if="status === 'rejected' || status === 'timeout' || status === 'error'" class="bg-red-50 border border-red-200 rounded p-4">
    <div class="text-red-800 font-bold">✗ Płatność nie powiodła się</div>
    <div class="text-sm text-red-700 mt-1">{{ error || result?.reject_reason || 'Brak szczegółów' }}</div>
  </div>
</template>
