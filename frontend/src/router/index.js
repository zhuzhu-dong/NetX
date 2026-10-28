import { createRouter, createWebHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import Login from '../views/Login.vue'
import { useAuthStore } from '../stores/authStore'

// Heavy operational pages stay out of the initial shell bundle.  Each is
// loaded only when the operator opens the corresponding real API surface.
const NetworkOverview = () => import('../views/dashboard/NetworkOverview.vue')
const SelfHealingReport = () => import('../views/dashboard/SelfHealingReport.vue')
const SystemDiagnostics = () => import('../views/dashboard/SystemDiagnostics.vue')
const IntentDetail = () => import('../views/dashboard/IntentDetail.vue')
const NetworkEvidence = () => import('../views/dashboard/NetworkEvidence.vue')
const DashboardOverview = () => import('../views/dashboard/DashboardOverview.vue')
const WorkflowPage = () => import('../views/dashboard/WorkflowPage.vue')

/** Competition demo access restores origin/main's browser-local session gate. */
const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    { path: '/', redirect: '/login' },
    {
      path: '/dashboard',
      component: Dashboard,
      children: [
        { path: '', name: 'intent-assistant', component: NetworkOverview },
        { path: 'overview', name: 'network-overview', component: NetworkOverview },
        { path: 'topology', name: 'topology', component: DashboardOverview },
        { path: 'intents/:intentId', name: 'intent-assistant-context', component: NetworkOverview },
        { path: 'intents/:intentId/detail', name: 'intent-detail', component: IntentDetail },
        { path: 'evidence', name: 'network-evidence', component: NetworkEvidence },
        { path: 'evidence/:intentId', name: 'network-evidence-intent', component: NetworkEvidence },
        { path: 'decision/:intentId', name: 'decision-report', component: IntentDetail },
        { path: 'workflow', name: 'workflow', component: WorkflowPage },
        { path: 'report', name: 'self-healing-report', component: SelfHealingReport },
        { path: 'diagnostics', name: 'system-diagnostics', component: SystemDiagnostics },
        { path: 'network', redirect: { name: 'topology' } },
        { path: 'runtime', redirect: { name: 'system-diagnostics' } },
        { path: 'system', redirect: { name: 'system-diagnostics' } },
        { path: 'chat', redirect: { name: 'intent-assistant' } },
        { path: 'intent', redirect: { name: 'intent-assistant' } },
        { path: 'run', redirect: { name: 'intent-assistant' } },
        { path: 'guide', redirect: '/dashboard' },
      ],
    },
    { path: '/login', name: 'login', component: Login },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const user = await auth.refresh()
  if (to.name === 'login') return user ? (to.query.redirect || '/dashboard') : true
  if (user) return true
  return { name: 'login', query: { redirect: to.fullPath } }
})

export default router
