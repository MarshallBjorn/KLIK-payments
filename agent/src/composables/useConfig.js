import { ref } from 'vue'

const STORAGE_KEY = 'klik-agent-config'

const state = ref(loadFromStorage())

function loadFromStorage() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function useConfig() {
  function isConfigured() {
    return state.value !== null && !!state.value.apiKey
  }

  function get() {
    return state.value
  }

  function save(config) {
    state.value = config
    localStorage.setItem(STORAGE_KEY, JSON.stringify(config))
  }

  function clear() {
    state.value = null
    localStorage.removeItem(STORAGE_KEY)
  }

  return { isConfigured, get, save, clear }
}
