import { ref } from 'vue'

const STORAGE_KEY = 'klik-bank-mock-config'
const state = ref(load())

function load() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function useConfig() {
  function isConfigured() {
    return state.value !== null && !!state.value.backendUrl
  }
  function get() {
    return state.value
  }
  function save(cfg) {
    state.value = cfg
    localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg))
  }
  function clear() {
    state.value = null
    localStorage.removeItem(STORAGE_KEY)
  }
  return { isConfigured, get, save, clear }
}
