import { createRouter, createWebHistory } from 'vue-router'
import { useConfig } from './composables/useConfig.js'

const routes = [
  { path: '/', redirect: '/terminal/c2b' },
  { path: '/setup', component: () => import('./views/SetupView.vue') },
  { path: '/terminal/c2b', component: () => import('./views/TerminalC2BView.vue') },
  { path: '/terminal/cheque', component: () => import('./views/TerminalChequeView.vue') },
  { path: '/history', component: () => import('./views/HistoryView.vue') },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const { isConfigured } = useConfig()
  if (!isConfigured() && to.path !== '/setup') return '/setup'
})

export default router
