import { ref } from 'vue'

const toasts = ref([])
let nextId = 1

export function useToasts() {
  function push(message, type = 'info', timeout = 6000) {
    const id = nextId++
    toasts.value.push({ id, message, type })
    if (timeout) setTimeout(() => dismiss(id), timeout)
  }
  function dismiss(id) {
    toasts.value = toasts.value.filter((t) => t.id !== id)
  }
  return { toasts, push, dismiss }
}