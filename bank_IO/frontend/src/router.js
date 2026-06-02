import { createRouter, createWebHistory } from 'vue-router'
import { useConfig } from './composables/useConfig.js'

const routes = [
  { path: '/', component: () => import('./views/DashboardView.vue') },
  { path: '/setup', component: () => import('./views/SetupView.vue') },
  { path: '/clients', component: () => import('./views/ClientsView.vue') },
  { path: '/pending', component: () => import('./views/PendingView.vue') },
  { path: '/p2p', component: () => import('./views/P2PView.vue') },
  { path: '/cheques', component: () => import('./views/ChequesView.vue') },
  { path: '/history', component: () => import('./views/HistoryView.vue') },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  const { isConfigured } = useConfig()
  if (!isConfigured() && to.path !== '/setup') return '/setup'
})

export default router
