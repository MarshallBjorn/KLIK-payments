<script setup>
import AppNav from './components/AppNav.vue'
import Toasts from './components/Toasts.vue'
import { useConfig } from './composables/useConfig.js'

const { isConfigured } = useConfig()
</script>

<template>
  <div class="min-h-screen flex flex-col">
    <AppNav v-if="isConfigured()" />
    <main class="flex-1 p-6 max-w-4xl mx-auto w-full">
      <router-view />
    </main>
    <Toasts />
  </div>
</template>