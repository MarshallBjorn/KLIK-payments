<script setup>
import { ref, onMounted } from 'vue'
import { listMerchants } from '../api/merchants.js'

const props = defineProps({ modelValue: String })
const emit = defineEmits(['update:modelValue'])

const merchants = ref([])
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    merchants.value = await listMerchants()
    if (merchants.value.length && !props.modelValue) {
      emit('update:modelValue', merchants.value[0].id)
    }
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div>
    <label class="block text-sm font-medium mb-1">Merchant</label>
    <div v-if="loading" class="text-sm text-slate-500">Ładowanie…</div>
    <div v-else-if="error" class="text-sm text-red-600">{{ error }}</div>
    <select
      v-else
      :value="modelValue"
      @change="emit('update:modelValue', $event.target.value)"
      class="w-full px-3 py-2 border rounded focus:outline-none focus:ring-2 focus:ring-emerald-500"
    >
      <option v-for="m in merchants" :key="m.id" :value="m.id">{{ m.name }}</option>
    </select>
  </div>
</template>
