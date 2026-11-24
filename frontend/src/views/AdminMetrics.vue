<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElDialog } from 'element-plus'
import AdminMermaidPreview from '@/components/AdminMermaidPreview.vue'
import { streamAdminFollowUp, type TemporaryTurn } from '@/api/adminFollowUp'
import { useMermaidPreview } from '@/composables/useMermaidPreview'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ArrowLeft, LogOut, Menu, PanelLeftClose, RefreshCw, X } from 'lucide-vue-next'
import { adminApi, adminToken } from '@/api/admin'
import ThemeToggle from '@/components/ThemeToggle.vue'
import { useAppStore } from '@/stores/app'
import { resolveAdminNavKey, type AdminNavItem } from '@/utils/adminNav'
import { useAdminScope } from '@/composables/useAdminScope'
import AgentTraceStream from '@/components/AgentTraceStream.vue'
import { splitLogAnalysisResult } from '@/utils/agentResultSummary'
import { renderMarkdown } from '@/utils/markdownRenderer'
import type { AgentTraceEvent } from '@/types/agentTrace'
import type {
  AdminConversationDetail,
  MetricsRawEvent,
  MetricsSystemOverview,
  MetricsUserDetail,
  MetricsUserRow,
  ProjectRepo,
} from '@/types'

declare const __VITE_USD_TO_CNY_RATE__: string | undefined

const { t } = useI18n()
const appStore = useAppStore()
const route = useRoute()
const router = useRouter()

const { visibleNavItems } = useAdminScope()

const isAuthenticated = ref(false)
const isLoggingIn = ref(false)

const authForm = reactive({ username: '', password: '' })

const navVisible = computed(() => appStore.adminSidebarVisible)
const activeNavKey = computed(() => resolveAdminNavKey(route.path))

// ==================== Controls ====================

type RangePreset = '24h' | '7d' | '30d'
type Bucket = 'hour' | 'day'

const rangePreset = ref<RangePreset>('7d')
const bucket = ref<Bucket>('day')
const selectedProjectRepoId = ref('system')

const rangePresets = computed<{ key: RangePreset; label: string }[]>(() => [
  { key: '24h', label: t('admin.metrics.timeRange24h') },
  { key: '7d', label: t('admin.metrics.timeRange7d') },
  { key: '30d', label: t('admin.metrics.timeRange30d') },
])

const computeRange = (): { from: string; to: string } => {
  const to = new Date()
  const from = new Date(to)
  if (rangePreset.value === '24h') from.setHours(from.getHours() - 24)
  else if (rangePreset.value === '30d') from.setDate(from.getDate() - 30)
  else from.setDate(from.getDate() - 7)
  return { from: from.toISOString(), to: to.toISOString() }
}

// ==================== Data state ====================

const loadingOverview = ref(false)
const overview = ref<MetricsSystemOverview | null>(null)

const loadingProjects = ref(false)
const projectRepos = ref<ProjectRepo[]>([])

const loadingUsers = ref(false)
const users = ref<MetricsUserRow[]>([])
const userTotal = ref(0)
const userPage = ref(1)
const userPerPage = 20
const userSort = ref('total_tokens')

const detailVisible = ref(false)
const loadingDetail = ref(false)
const detail = ref<MetricsUserDetail | null>(null)

const loadingEvents = ref(false)
const events = ref<MetricsRawEvent[]>([])
const eventsTotal = ref(0)
const eventsPage = ref(1)
const eventsPerPage = 50
const eventSourceFilter = ref('')

const conversationVisible = ref(false)
const loadingConversation = ref(false)
const conversationLoadError = ref('')
const conversation = ref<AdminConversationDetail | null>(null)
const conversationThreadRef = ref<HTMLElement | null>(null)

watch(conversationThreadRef, (thread) => {
  const reply = thread?.querySelector<HTMLElement>('[data-event-reply]')
  if (thread && reply) thread.scrollTop = reply.offsetTop - thread.clientTop
}, { flush: 'post' })

// The transcript is behind the loading branch, so render Mermaid only after
// its template ref is actually mounted (rather than while the loader is shown).
const followUpHistory = ref<TemporaryTurn[]>([])
const followUpQuestion = ref('')
const followUpPendingQuestion = ref('')
const followUpAnswer = ref('')
const followUpError = ref('')
const followUpRunning = ref(false)
const followUpInput = ref<HTMLTextAreaElement | null>(null)
const diagramSource = ref<string | null>(null)
let followUpController: AbortController | null = null
let conversationRevision = 0
useMermaidPreview(conversationThreadRef, computed(() => JSON.stringify([
  conversation.value?.messages, followUpHistory.value, followUpAnswer.value,
])))

const resetFollowUp = () => {
  followUpController?.abort()
  followUpController = null
  followUpRunning.value = false
  followUpHistory.value = []
  followUpQuestion.value = ''
  followUpPendingQuestion.value = ''
  followUpAnswer.value = ''
  followUpError.value = ''
  diagramSource.value = null
}

const sendFollowUp = async () => {
  const question = followUpQuestion.value.trim()
  if (!question || !conversation.value || followUpRunning.value) return
  const controller = new AbortController()
  followUpController = controller
  followUpRunning.value = true
  followUpPendingQuestion.value = question
  followUpAnswer.value = ''
  followUpError.value = ''
  await nextTick()
  conversationThreadRef.value?.scrollTo({ top: conversationThreadRef.value.scrollHeight })
  try {
    const answer = await streamAdminFollowUp(conversation.value.event_id, question,
      followUpHistory.value, controller.signal, (text) => {
        if (followUpController === controller) {
          const thread = conversationThreadRef.value
          const nearBottom = thread && thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80
          followUpAnswer.value = text
          if (nearBottom) void nextTick(() => thread.scrollTo({ top: thread.scrollHeight }))
        }
      })
    if (followUpController !== controller) return
    followUpHistory.value.push({ role: 'user', content: question }, { role: 'assistant', content: answer })
    followUpQuestion.value = ''
  } catch (error) {
    if (followUpController !== controller || controller.signal.aborted) return
    followUpError.value = parseErrorMessage(error)
  } finally {
    if (followUpController === controller) {
      followUpController = null
      followUpRunning.value = false
      followUpPendingQuestion.value = ''
      followUpAnswer.value = ''
      await nextTick()
      followUpInput.value?.focus()
    }
  }
}

const onConversationClick = async (event: MouseEvent) => {
  if (!(event.target instanceof Element)) return
  const target = event.target
  const copy = target.closest('.mermaid-copy-btn, .table-copy-btn')
  if (copy) {
    const block = copy.closest<HTMLElement>('.mermaid-container, .table-block')
    const source = block?.dataset.mermaidSource || block?.dataset.tableMd
    if (source) {
      try { await navigator.clipboard.writeText(source) }
      catch { appStore.showNotification({ title: t('aiChat.mermaid.copySourceFailed'), type: 'error' }) }
    }
    return
  }
  const diagram = target.closest<HTMLElement>('.mermaid-container[data-mermaid-state="done"]')
  if (diagram) diagramSource.value = diagram.dataset.mermaidSource || null
}

/**
 * Blob object URLs for the images attached to the open conversation, keyed by
 * image id. The bytes endpoint needs the admin bearer token, so `<img src>`
 * cannot point at it directly. Revoked when the modal closes — a different
 * event means a different conversation, so nothing here is worth keeping.
 */
const conversationImageUrls = ref<Record<string, string>>({})
/** The image the admin clicked to enlarge, or null while none is open. */
const previewImageUrl = ref<string | null>(null)

// ==================== Helpers ====================

const parseErrorMessage = (err: any) => {
  if (err?.response?.data?.detail) return err.response.data.detail
  if (err?.response?.data?.message) return err.response.data.message
  if (err?.message) return err.message
  return t('admin.parseError')
}

const parseMetricTimestamp = (value: string) => {
  const trimmed = value.trim()
  const hasExplicitTimezone = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(trimmed)
  return new Date(hasExplicitTimezone ? trimmed : `${trimmed}Z`)
}

const formatTimestamp = (value?: string | null) => {
  if (!value) return '--'
  try {
    const date = parseMetricTimestamp(value)
    if (Number.isNaN(date.getTime())) return value

    // 不指定 timeZone，按浏览器本地时区显示；无时区的后端时间仍按 UTC 解析。
    return date.toLocaleString('zh-CN', {
      hour12: false,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      timeZoneName: 'shortOffset',
    })
  } catch {
    return value
  }
}

const formatNumber = (value?: number | null) => {
  if (value === null || value === undefined) return '0'
  return value.toLocaleString('en-US')
}

// ==================== Merged OCR sub-events ====================
//
// Image OCR is a preprocessing step for the agent run it shares a `run_id`
// with, so the backend folds those events into the parent row. These read that
// merged payload for the events table.

const mergedOcrEvents = (event: MetricsRawEvent) => event.ocr_events || []

/** Images OCR'd across this request; 0 when the sub-events predate the count. */
const ocrImageCount = (event: MetricsRawEvent) =>
  mergedOcrEvents(event).reduce((sum, ocr) => sum + (ocr.image_count || 0), 0)

const ocrTokens = (event: MetricsRawEvent) =>
  mergedOcrEvents(event).reduce((sum, ocr) => sum + (ocr.total_tokens || 0), 0)

/** What the whole request cost: the agent run plus the OCR it triggered. */
const combinedTokens = (event: MetricsRawEvent) =>
  (event.total_tokens || 0) + ocrTokens(event)

const renderConversationAi = (content: string) =>
  renderMarkdown(content || '', { wrapperClass: 'markdown-content text-ink' })

const conversationMessages = computed(() => conversation.value?.messages.map((message) => ({
  message,
  logResult: message.role === 'ai' ? splitLogAnalysisResult(message.content) : null,
})) ?? [])

const conversationTraceEvents = (events?: unknown[] | null): AgentTraceEvent[] =>
  Array.isArray(events) ? (events as AgentTraceEvent[]) : []

const formatBytes = (value?: number | null) => {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let v = value
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i += 1
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`
}

const DEFAULT_USD_TO_CNY_RATE = 7.2
const resolveUsdToCnyRate = () => {
  const rawRate =
    (import.meta.env.VITE_USD_TO_CNY_RATE as string | undefined) ||
    (typeof __VITE_USD_TO_CNY_RATE__ !== 'undefined' ? __VITE_USD_TO_CNY_RATE__ : undefined)
  const rate = Number(rawRate)
  return Number.isFinite(rate) && rate > 0 ? rate : DEFAULT_USD_TO_CNY_RATE
}
const USD_TO_CNY_RATE = resolveUsdToCnyRate()
const cnyCostFormatter = new Intl.NumberFormat('zh-CN', {
  style: 'currency',
  currency: 'CNY',
  currencyDisplay: 'narrowSymbol',
  minimumFractionDigits: 4,
  maximumFractionDigits: 4,
})

const formatCost = (overviewOrDetail: { estimated_cost_usd: number | null; cost_estimated: boolean } | null) => {
  if (!overviewOrDetail || !overviewOrDetail.cost_estimated || overviewOrDetail.estimated_cost_usd === null) {
    return t('admin.metrics.noPriceConfig')
  }
  return cnyCostFormatter.format(overviewOrDetail.estimated_cost_usd * USD_TO_CNY_RATE)
}

const formatBucketLabel = (value: string) => {
  if (!value) return '--'
  try {
    const d = new Date(value)
    if (bucket.value === 'hour') {
      return d.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', hour12: false })
    }
    return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  } catch {
    return value
  }
}

const objToPairs = (obj?: Record<string, number> | null) =>
  Object.entries(obj || {}).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1])

const isSystemProjectScope = computed(() => selectedProjectRepoId.value === 'system')
const selectedProjectParam = computed(() =>
  isSystemProjectScope.value ? undefined : selectedProjectRepoId.value,
)
const projectRepoById = computed(() => {
  const map = new Map<string, ProjectRepo>()
  projectRepos.value.forEach((repo) => map.set(String(repo.id), repo))
  return map
})
const formatProjectOption = (repo: ProjectRepo) => {
  const base = `${repo.project_name || repo.project_code} (${repo.project_code})`
  return repo.enabled ? base : `${base} ${t('admin.metrics.projectDisabledSuffix')}`
}
const formatProjectGroupLabel = (key?: string | null) => {
  if (!key) return t('admin.metrics.unknownProject')
  const repo = projectRepoById.value.get(String(key))
  if (!repo) return t('admin.metrics.unknownProjectWithId', { id: key })
  return `${repo.project_name || repo.project_code} (${repo.project_code})`
}

// Build a "nice" axis scale: a rounded maximum plus evenly spaced ticks
// (descending, from niceMax down to 0) so bars and Y-axis ticks line up.
const buildNiceScale = (rawMax: number, tickCount = 4): { niceMax: number; ticks: number[] } => {
  const safeMax = rawMax > 0 ? rawMax : tickCount
  const rough = safeMax / tickCount
  const pow = Math.pow(10, Math.floor(Math.log10(rough)))
  const normalized = rough / pow
  const niceStep =
    normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 2.5 ? 2.5 : normalized <= 5 ? 5 : 10
  const step = niceStep * pow
  const ticks: number[] = []
  for (let i = tickCount; i >= 0; i -= 1) ticks.push(step * i)
  return { niceMax: step * tickCount, ticks }
}

const tokenScale = computed(() =>
  buildNiceScale((overview.value?.time_series || []).reduce((m, b) => Math.max(m, b.total_tokens), 0)),
)

const agentScale = computed(() =>
  buildNiceScale(
    (overview.value?.time_series || []).reduce((m, b) => {
      const totalCallsInBucket = Object.entries(b.counts_by_agent || {})
        .filter(([k]) => k !== 'title_generator')
        .reduce((sum, [, v]) => sum + (v as number), 0)
      return Math.max(m, totalCallsInBucket as number)
    }, 0),
  ),
)

const axisTickFormatter = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 })
const formatAxisTick = (value: number) =>
  value >= 1000 ? axisTickFormatter.format(value) : String(Math.round(value))

// X-axis labels render horizontally by default and only rotate when they would
// overlap. We measure the widest label against the per-column width (which
// depends on bucket count and the live chart width) to decide.
const LABEL_GAP_PX = 5.6 // matches the .metrics-chart column gap (0.35rem)
let labelMeasureCanvas: HTMLCanvasElement | null = null
const measureLabelWidth = (text: string): number => {
  if (typeof document === 'undefined') return text.length * 6
  if (!labelMeasureCanvas) labelMeasureCanvas = document.createElement('canvas')
  const ctx = labelMeasureCanvas.getContext('2d')
  if (!ctx) return text.length * 6
  ctx.font = `9.9px ${getComputedStyle(document.body).fontFamily || 'sans-serif'}`
  return ctx.measureText(text).width
}

const chartInnerWidth = ref(0)
let chartResizeObserver: ResizeObserver | null = null
const setChartRef = (el: unknown) => {
  chartResizeObserver?.disconnect()
  chartResizeObserver = null
  if (el instanceof HTMLElement && typeof ResizeObserver !== 'undefined') {
    chartResizeObserver = new ResizeObserver((entries) => {
      chartInnerWidth.value = entries[0].contentRect.width
    })
    chartResizeObserver.observe(el)
    chartInnerWidth.value = el.clientWidth
  }
}
onBeforeUnmount(() => {
  chartResizeObserver?.disconnect()
  resetFollowUp()
  conversationRevision++
  releaseConversationImages()
})

const rotateBucketLabels = computed(() => {
  const series = overview.value?.time_series || []
  const n = series.length
  if (n <= 1 || chartInnerWidth.value <= 0) return false
  const maxLabelWidth = series.reduce(
    (m, b) => Math.max(m, measureLabelWidth(formatBucketLabel(b.bucket_start))),
    0,
  )
  const colSlot = (chartInnerWidth.value - (n - 1) * LABEL_GAP_PX) / n
  return maxLabelWidth + 4 > colSlot + LABEL_GAP_PX
})

const agentColors = [
  '#06b6d4', '#8b5cf6', '#ec4899', '#f59e0b', '#10b981', '#3b82f6', '#ef4444',
]
const getAgentColor = (agent: string) => {
  let hash = 0
  for (let i = 0; i < agent.length; i++) hash = agent.charCodeAt(i) + ((hash << 5) - hash)
  return agentColors[Math.abs(hash) % agentColors.length]
}

// ==================== Loaders ====================

const loadProjectRepos = async () => {
  if (!isAuthenticated.value) return
  loadingProjects.value = true
  try {
    const resp = await adminApi.listProjectRepos({ include_disabled: true, limit: 200 })
    if (!resp?.success || !resp.data) throw new Error(resp?.message || t('admin.projectRepos.loadFailFallback'))
    projectRepos.value = resp.data
    if (
      !isSystemProjectScope.value &&
      !projectRepos.value.some((repo) => String(repo.id) === selectedProjectRepoId.value)
    ) {
      selectedProjectRepoId.value = 'system'
    }
  } catch (err: any) {
    appStore.showNotification({ title: t('admin.loadFail'), message: parseErrorMessage(err), type: 'error' })
  } finally {
    loadingProjects.value = false
  }
}

const loadOverview = async () => {
  if (!isAuthenticated.value) return
  loadingOverview.value = true
  try {
    const { from, to } = computeRange()
    const resp = await adminApi.metricsOverview({
      from,
      to,
      bucket: bucket.value,
      project_repo_id: selectedProjectParam.value,
    })
    if (!resp?.success || !resp.data) throw new Error(resp?.message || t('admin.metrics.loadOverviewFail'))
    overview.value = resp.data
  } catch (err: any) {
    appStore.showNotification({ title: t('admin.loadFail'), message: parseErrorMessage(err), type: 'error' })
  } finally {
    loadingOverview.value = false
  }
}

const loadUsers = async () => {
  if (!isAuthenticated.value) return
  loadingUsers.value = true
  try {
    const { from, to } = computeRange()
    const resp = await adminApi.metricsUsers({
      from,
      to,
      project_repo_id: selectedProjectParam.value,
      page: userPage.value,
      per_page: userPerPage,
      sort: userSort.value,
    })
    if (!resp?.success || !resp.data) throw new Error(resp?.message || t('admin.metrics.loadUserListFail'))
    users.value = resp.data.rows
    userTotal.value = resp.data.total
    userPage.value = resp.data.page
  } catch (err: any) {
    appStore.showNotification({ title: t('admin.loadFail'), message: parseErrorMessage(err), type: 'error' })
  } finally {
    loadingUsers.value = false
  }
}

const loadEvents = async () => {
  if (!isAuthenticated.value) return
  loadingEvents.value = true
  try {
    const { from, to } = computeRange()
    const resp = await adminApi.metricsEvents({
      from,
      to,
      project_repo_id: selectedProjectParam.value,
      source: eventSourceFilter.value || undefined,
      page: eventsPage.value,
      per_page: eventsPerPage,
    })
    if (!resp?.success || !resp.data) throw new Error(resp?.message || t('admin.metrics.loadEventsFail'))
    events.value = resp.data.events
    eventsTotal.value = resp.data.total
    eventsPage.value = resp.data.page
  } catch (err: any) {
    appStore.showNotification({ title: t('admin.loadFail'), message: parseErrorMessage(err), type: 'error' })
  } finally {
    loadingEvents.value = false
  }
}

const openUserDetail = async (row: MetricsUserRow) => {
  detailVisible.value = true
  loadingDetail.value = true
  detail.value = null
  try {
    const { from, to } = computeRange()
    const resp = await adminApi.metricsUserDetail(row.user_id, {
      from,
      to,
      bucket: bucket.value,
      project_repo_id: selectedProjectParam.value,
    })
    if (!resp?.success || !resp.data) throw new Error(resp?.message || t('admin.metrics.loadUserDetailFail'))
    detail.value = resp.data
  } catch (err: any) {
    appStore.showNotification({ title: t('admin.loadFail'), message: parseErrorMessage(err), type: 'error' })
    detailVisible.value = false
  } finally {
    loadingDetail.value = false
  }
}

const closeDetail = () => {
  detailVisible.value = false
  detail.value = null
}

/**
 * Fill in the thumbnails for a conversation, detached from its first paint.
 *
 * The thread renders from text alone and each image appears as its bytes land,
 * so one slow or missing attachment never holds up the transcript. A failed
 * image just keeps no URL, which renders as the "unavailable" placeholder.
 */
const loadConversationImages = async (detail: AdminConversationDetail) => {
  const revision = conversationRevision
  const ids = detail.messages.flatMap((message) =>
    (message.images || []).map((image) => image.id)
  )
  await Promise.all(
    ids.map(async (imageId) => {
      try {
        const blob = await adminApi.metricsEventChatImage(detail.event_id, imageId)
        // The modal may have been closed or reopened while
        // this was in flight; dropping the bytes avoids a leaked object URL.
        if (revision !== conversationRevision || conversation.value?.event_id !== detail.event_id) return
        conversationImageUrls.value[imageId] = URL.createObjectURL(blob)
      } catch {
        // Cleaned-up or unreadable image: leave it out, the template covers it.
      }
    })
  )
}

const releaseConversationImages = () => {
  Object.values(conversationImageUrls.value).forEach((url) => URL.revokeObjectURL(url))
  conversationImageUrls.value = {}
  previewImageUrl.value = null
}

const openEventConversation = async (event: MetricsRawEvent) => {
  if (!event.conversation_available) return
  const revision = ++conversationRevision
  resetFollowUp()
  conversationVisible.value = true
  loadingConversation.value = true
  conversationLoadError.value = ''
  conversation.value = null
  releaseConversationImages()
  try {
    const resp = await adminApi.metricsEventConversation(event.id)
    if (revision !== conversationRevision) return
    if (!resp?.success || !resp.data) {
      throw new Error(resp?.message || t('admin.metrics.loadConversationFail'))
    }
    conversation.value = resp.data
    // Not awaited: thumbnails stream in behind the already-rendered transcript.
    void loadConversationImages(resp.data)
  } catch (err: any) {
    if (revision !== conversationRevision) return
    appStore.showNotification({
      title: t('admin.loadFail'),
      message: parseErrorMessage(err),
      type: 'error',
    })
    conversationLoadError.value = parseErrorMessage(err)
  } finally {
    if (revision === conversationRevision) loadingConversation.value = false
  }
}

const closeConversation = () => {
  conversationRevision++
  resetFollowUp()
  conversationVisible.value = false
  conversation.value = null
  releaseConversationImages()
}

const refreshAll = async () => {
  userPage.value = 1
  eventsPage.value = 1
  await Promise.all([loadOverview(), loadUsers(), loadEvents()])
}

const applyRange = async (preset: RangePreset) => {
  rangePreset.value = preset
  await refreshAll()
}

const applyBucket = async (value: Bucket) => {
  bucket.value = value
  await loadOverview()
}

const applyProjectScope = async () => {
  closeDetail()
  await refreshAll()
}

const changeSort = async (sort: string) => {
  userSort.value = sort
  userPage.value = 1
  await loadUsers()
}

const userPageCount = computed(() => Math.max(1, Math.ceil(userTotal.value / userPerPage)))
const eventsPageCount = computed(() => Math.max(1, Math.ceil(eventsTotal.value / eventsPerPage)))

const gotoUserPage = async (page: number) => {
  if (page < 1 || page > userPageCount.value) return
  userPage.value = page
  await loadUsers()
}

const gotoEventsPage = async (page: number) => {
  if (page < 1 || page > eventsPageCount.value) return
  eventsPage.value = page
  await loadEvents()
}

const applyEventFilter = async () => {
  eventsPage.value = 1
  await loadEvents()
}

// ==================== Auth ====================

const handleNavClick = (item: AdminNavItem) => {
  if (item.path && route.path !== item.path) router.push(item.path)
}

const toggleNavVisibility = () => appStore.toggleAdminSidebar()

const clearAuth = () => {
  adminToken.clear()
  isAuthenticated.value = false
  authForm.password = ''
}

const handleLogin = async () => {
  if (!authForm.username || !authForm.password) {
    appStore.showNotification({ title: t('admin.loginWarning'), type: 'warning' })
    return
  }
  isLoggingIn.value = true
  try {
    const resp = await adminApi.login(authForm.username.trim(), authForm.password)
    if (!resp?.success || !resp.data) throw new Error(resp?.message || t('admin.loginFailFallback'))
    adminToken.set(resp.data.token)
    isAuthenticated.value = true
    appStore.showNotification({ title: t('admin.loginSuccessTitle'), message: t('admin.loginSuccessMsg', { username: resp.data.username }), type: 'success' })
    await loadProjectRepos()
    await refreshAll()
  } catch (err: any) {
    appStore.showNotification({ title: t('admin.loginFailFallback'), message: parseErrorMessage(err), type: 'error' })
  } finally {
    isLoggingIn.value = false
  }
}

const handleLogout = async () => {
  try {
    await adminApi.logout()
  } catch {
    // ignore
  } finally {
    clearAuth()
    overview.value = null
    users.value = []
    events.value = []
    closeConversation()
    projectRepos.value = []
    selectedProjectRepoId.value = 'system'
    appStore.showNotification({ title: t('admin.logoutSuccessTitle'), type: 'info' })
  }
}

const bootstrap = async () => {
  const token = adminToken.get()
  if (!token) return
  try {
    const resp = await adminApi.me()
    if (resp?.success) {
      isAuthenticated.value = true
      await loadProjectRepos()
      await refreshAll()
    } else {
      clearAuth()
    }
  } catch {
    clearAuth()
  }
}

onMounted(() => {
  bootstrap()
})
</script>

<template>
  <div class="admin-console admin-metrics-page">
    <header class="admin-topbar">
      <div class="admin-topbar-inner">
        <div class="admin-topbar-left">
          <button
            class="admin-back-btn"
            :title="t('admin.backToChatTitle')"
            :aria-label="t('admin.backToChatTitle')"
            @click="router.push('/workbench')"
          >
            <ArrowLeft :size="16" />
            <span class="admin-back-btn-label">{{ t('admin.backToChat') }}</span>
          </button>
          <button
            class="admin-icon-btn"
            :disabled="!isAuthenticated"
            @click="toggleNavVisibility"
            :title="navVisible ? t('admin.toggleSidebarHide') : t('admin.toggleSidebarShow')"
            :aria-label="t('admin.toggleSidebarAriaLabel')"
          >
            <PanelLeftClose v-if="navVisible" :size="18" />
            <Menu v-else :size="18" />
          </button>
          <div>
            <h1 class="admin-title">{{ t('admin.title') }}</h1>
            <p class="admin-subtitle">{{ t('admin.metrics.subtitle') }}</p>
          </div>
        </div>
        <div class="admin-topbar-right">
          <ThemeToggle class="admin-theme-toggle" />
          <button v-if="isAuthenticated" class="admin-logout-btn" @click="handleLogout">
            <LogOut :size="14" />
            <span>{{ t('admin.logoutBtn') }}</span>
          </button>
        </div>
      </div>
    </header>

    <button
      v-if="isAuthenticated && navVisible"
      class="admin-sidebar-backdrop"
      @click="toggleNavVisibility"
      :aria-label="t('admin.closeSidebarAriaLabel')"
    ></button>

    <aside v-if="isAuthenticated" class="admin-sidebar" :class="{ 'is-hidden': !navVisible }">
      <div class="space-y-2">
        <button
          v-for="item in visibleNavItems"
          :key="item.key"
          class="admin-side-nav-item"
          :class="{ 'is-active': activeNavKey === item.key }"
          @click="handleNavClick(item)"
        >
          <div class="text-sm font-semibold">{{ item.label }}</div>
          <p v-if="item.description" class="text-xs mt-1 text-slate-400">{{ item.description }}</p>
        </button>
      </div>
    </aside>

    <main class="admin-main" :class="{ 'is-sidebar-hidden': !isAuthenticated || !navVisible }">
      <!-- Login gate -->
      <section v-if="!isAuthenticated" class="admin-login-wrap">
        <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6">
          <div class="flex items-center justify-between mb-4">
            <div>
              <h2 class="text-lg font-semibold text-slate-900">{{ t('admin.loginCardTitle') }}</h2>
              <p class="text-sm text-slate-500">{{ t('admin.metrics.loginCardDesc') }}</p>
            </div>
            <span class="text-xs text-slate-500">{{ t('admin.secureAccess') }}</span>
          </div>
          <form class="space-y-4 max-w-sm" @submit.prevent="handleLogin">
            <label class="block">
              <span class="text-sm text-slate-700">{{ t('admin.usernameLabel') }}</span>
              <input
                v-model="authForm.username"
                type="text"
                class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
                placeholder="admin"
                autocomplete="username"
              />
            </label>
            <label class="block">
              <span class="text-sm text-slate-700">{{ t('admin.passwordLabel') }}</span>
              <input
                v-model="authForm.password"
                type="password"
                class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
                placeholder="••••••••"
                autocomplete="current-password"
              />
            </label>
            <button
              type="submit"
              class="px-4 py-2 bg-cyan-600 text-white rounded-lg text-sm font-semibold hover:bg-cyan-700 transition disabled:opacity-50"
              :disabled="isLoggingIn"
            >
              {{ isLoggingIn ? t('admin.loginBtnLoading') : t('admin.loginBtn') }}
            </button>
          </form>
        </div>
      </section>

      <section v-else class="space-y-4">
        <!-- Controls -->
        <div class="metrics-toolbar">
          <div class="metrics-control-group">
            <span class="metrics-control-label">{{ t('admin.metrics.timeRangeLabel') }}</span>
            <div class="metrics-segment">
              <button
                v-for="preset in rangePresets"
                :key="preset.key"
                class="metrics-segment-btn"
                :class="{ 'is-active': rangePreset === preset.key }"
                @click="applyRange(preset.key)"
              >
                {{ preset.label }}
              </button>
            </div>
          </div>
          <div class="metrics-control-group">
            <span class="metrics-control-label">{{ t('admin.metrics.bucketLabel') }}</span>
            <div class="metrics-segment">
              <button class="metrics-segment-btn" :class="{ 'is-active': bucket === 'hour' }" @click="applyBucket('hour')">{{ t('admin.metrics.bucketHour') }}</button>
              <button class="metrics-segment-btn" :class="{ 'is-active': bucket === 'day' }" @click="applyBucket('day')">{{ t('admin.metrics.bucketDay') }}</button>
            </div>
          </div>
          <div class="metrics-control-group">
            <span class="metrics-control-label">{{ t('admin.metrics.projectFilterLabel') }}</span>
            <select
              v-model="selectedProjectRepoId"
              class="metrics-select"
              :disabled="loadingProjects"
              @change="applyProjectScope"
            >
              <option value="system">{{ t('admin.metrics.allSystemProject') }}</option>
              <option v-for="repo in projectRepos" :key="repo.id" :value="String(repo.id)">
                {{ formatProjectOption(repo) }}
              </option>
            </select>
          </div>
          <button class="metrics-refresh-btn" :disabled="loadingOverview" @click="refreshAll">
            <RefreshCw :size="14" :class="{ 'animate-spin': loadingOverview }" />
            <span>{{ t('admin.metrics.refreshBtn') }}</span>
          </button>
        </div>

        <!-- KPI cards -->
        <div class="metrics-kpi-grid">
          <div class="metrics-card metrics-kpi">
            <span class="metrics-kpi-label">{{ t('admin.metrics.kpiTokens') }}</span>
            <span class="metrics-kpi-value">{{ formatNumber(overview?.tokens.total_tokens) }}</span>
            <span class="metrics-kpi-sub">
              {{ t('admin.metrics.kpiTokensDetail', { input: formatNumber(overview?.tokens.input_tokens), output: formatNumber(overview?.tokens.output_tokens) }) }}
            </span>
          </div>
          <div class="metrics-card metrics-kpi">
            <span class="metrics-kpi-label">{{ t('admin.metrics.kpiCalls') }}</span>
            <span class="metrics-kpi-value">{{ formatNumber(overview?.invocation_count) }}</span>
            <span class="metrics-kpi-sub">
              {{ t('admin.metrics.kpiCallsDetail', { success: formatNumber(overview?.status_counts.succeeded), fail: formatNumber(overview?.status_counts.failed) }) }}
            </span>
          </div>
          <div class="metrics-card metrics-kpi">
            <span class="metrics-kpi-label">{{ t('admin.metrics.kpiCost') }}</span>
            <span class="metrics-kpi-value">{{ formatCost(overview) }}</span>
            <span class="metrics-kpi-sub">{{ t('admin.metrics.kpiCacheRead', { count: formatNumber(overview?.tokens.cache_read_tokens) }) }}</span>
          </div>
          <div class="metrics-card metrics-kpi">
            <span class="metrics-kpi-label">{{ t('admin.metrics.kpiDuration') }}</span>
            <span class="metrics-kpi-value">{{ overview?.duration_ms_avg ? Math.round(overview.duration_ms_avg) : '--' }}</span>
            <span class="metrics-kpi-sub">{{ t('admin.metrics.kpiDurationDetail', { p95: overview?.duration_ms_p95 ? Math.round(overview.duration_ms_p95) : '--', errors: formatNumber(overview?.error_count) }) }}</span>
          </div>
        </div>

        <!-- Time series -->
        <div class="metrics-card">
          <h3 class="metrics-card-title">{{ t('admin.metrics.tsTitle') }}</h3>
          <div v-if="!overview || !overview.time_series.length" class="metrics-empty">{{ t('admin.metrics.emptyData') }}</div>
          <div v-else class="metrics-graph" :class="{ 'labels-rotated': rotateBucketLabels }">
            <div class="metrics-graph-body">
              <div class="metrics-yaxis-title">{{ t('admin.metrics.axisTokens') }}</div>
              <div class="metrics-yaxis">
                <span
                  v-for="(tick, ti) in tokenScale.ticks"
                  :key="ti"
                  class="metrics-yaxis-tick"
                  :style="{ top: `${(ti / (tokenScale.ticks.length - 1)) * 100}%` }"
                >{{ formatAxisTick(tick) }}</span>
              </div>
              <div class="metrics-plot">
                <div class="metrics-gridlines">
                  <span
                    v-for="(tick, ti) in tokenScale.ticks"
                    :key="tick"
                    class="metrics-gridline"
                    :class="{ 'is-base': ti === tokenScale.ticks.length - 1 }"
                    :style="{ top: `${(ti / (tokenScale.ticks.length - 1)) * 100}%` }"
                  ></span>
                </div>
                <div class="metrics-chart" :ref="setChartRef">
                  <div
                    v-for="(b, i) in overview.time_series"
                    :key="i"
                    class="metrics-bar-col"
                    :title="`${formatBucketLabel(b.bucket_start)}\nToken: ${formatNumber(b.total_tokens)}\n${t('admin.metrics.colCalls')}: ${formatNumber(b.invocation_count)}`"
                  >
                    <div class="metrics-bar-track">
                      <div
                        class="metrics-bar-fill"
                        :style="{ height: `${Math.max(2, (b.total_tokens / tokenScale.niceMax) * 100)}%` }"
                      ></div>
                    </div>
                    <div class="metrics-bar-label"><span>{{ formatBucketLabel(b.bucket_start) }}</span></div>
                  </div>
                </div>
              </div>
            </div>
            <div class="metrics-xaxis-title">{{ t('admin.metrics.axisTime') }}</div>
          </div>
        </div>

        <!-- Agent Calls Time Series -->
        <div class="metrics-card">
          <h3 class="metrics-card-title">{{ $t('admin.metrics.agentTrendTitle') }}</h3>
          <div v-if="!overview || !overview.time_series.length" class="metrics-empty">{{ t('admin.metrics.emptyData') }}</div>
          <div v-else class="metrics-graph" :class="{ 'labels-rotated': rotateBucketLabels }">
            <div class="metrics-graph-body">
              <div class="metrics-yaxis-title">{{ t('admin.metrics.axisCalls') }}</div>
              <div class="metrics-yaxis">
                <span
                  v-for="(tick, ti) in agentScale.ticks"
                  :key="ti"
                  class="metrics-yaxis-tick"
                  :style="{ top: `${(ti / (agentScale.ticks.length - 1)) * 100}%` }"
                >{{ formatAxisTick(tick) }}</span>
              </div>
              <div class="metrics-plot">
                <div class="metrics-gridlines">
                  <span
                    v-for="(tick, ti) in agentScale.ticks"
                    :key="tick"
                    class="metrics-gridline"
                    :class="{ 'is-base': ti === agentScale.ticks.length - 1 }"
                    :style="{ top: `${(ti / (agentScale.ticks.length - 1)) * 100}%` }"
                  ></span>
                </div>
                <div class="metrics-chart">
                  <div
                    v-for="(b, i) in overview.time_series"
                    :key="i"
                    class="metrics-bar-col"
                    :title="`${formatBucketLabel(b.bucket_start)}\n${Object.entries(b.counts_by_agent || {}).filter(([k]) => k !== 'title_generator').map(([k, v]) => `${k}: ${formatNumber(v as number)}`).join('\\n')}`"
                  >
                    <div class="metrics-bar-track" style="flex-direction: column-reverse; justify-content: flex-start; align-items: stretch;">
                      <div
                        v-for="[agent, count] in Object.entries(b.counts_by_agent || {}).filter(([k]) => k !== 'title_generator').sort((a, b) => (b[1] as number) - (a[1] as number))"
                        :key="agent"
                        class="metrics-bar-fill-agent"
                        :style="{
                          height: `${((count as number) / agentScale.niceMax) * 100}%`,
                          backgroundColor: getAgentColor(agent),
                          minHeight: '2px',
                          width: '100%'
                        }"
                      ></div>
                    </div>
                    <div class="metrics-bar-label"><span>{{ formatBucketLabel(b.bucket_start) }}</span></div>
                  </div>
                </div>
              </div>
            </div>
            <div class="metrics-xaxis-title">{{ t('admin.metrics.axisTime') }}</div>
          </div>
          <!-- Legend -->
          <div v-if="overview && overview.time_series.length" class="flex flex-wrap gap-3 mt-4 justify-center">
            <div
              v-for="agent in Array.from(new Set(overview.time_series.flatMap(b => Object.keys(b.counts_by_agent || {}).filter(k => k !== 'title_generator'))))"
              :key="agent"
              class="flex items-center text-xs text-slate-600"
            >
              <span class="w-3 h-3 rounded-sm mr-1.5" :style="{ backgroundColor: getAgentColor(agent) }"></span>
              {{ agent }}
            </div>
          </div>
        </div>

        <!-- Distributions -->
        <div class="metrics-distribution-grid" :class="{ 'has-projects': isSystemProjectScope }">
          <div class="metrics-card">
            <h3 class="metrics-card-title">{{ t('admin.metrics.bySourceTitle') }}</h3>
            <table class="metrics-mini-table">
              <thead><tr><th>Source</th><th class="text-right">{{ t('admin.metrics.colCalls') }}</th><th class="text-right">Token</th></tr></thead>
              <tbody>
                <tr v-for="g in overview?.invocations_by_source || []" :key="g.key || 'unknown'">
                  <td>{{ g.key || t('admin.metrics.unknownKey') }}</td>
                  <td class="text-right">{{ formatNumber(g.invocation_count) }}</td>
                  <td class="text-right">{{ formatNumber(g.total_tokens) }}</td>
                </tr>
                <tr v-if="!overview?.invocations_by_source?.length"><td colspan="3" class="metrics-empty">{{ t('admin.metrics.emptyData') }}</td></tr>
              </tbody>
            </table>
          </div>
          <div class="metrics-card">
            <h3 class="metrics-card-title">{{ t('admin.metrics.byModelTitle') }}</h3>
            <table class="metrics-mini-table">
              <thead><tr><th>Model</th><th class="text-right">{{ t('admin.metrics.colCalls') }}</th><th class="text-right">Token</th></tr></thead>
              <tbody>
                <tr v-for="g in overview?.invocations_by_model || []" :key="g.key || 'unknown'">
                  <td>{{ g.key || t('admin.metrics.unknownKey') }}</td>
                  <td class="text-right">{{ formatNumber(g.invocation_count) }}</td>
                  <td class="text-right">{{ formatNumber(g.total_tokens) }}</td>
                </tr>
                <tr v-if="!overview?.invocations_by_model?.length"><td colspan="3" class="metrics-empty">{{ t('admin.metrics.emptyData') }}</td></tr>
              </tbody>
            </table>
          </div>
          <div v-if="isSystemProjectScope" class="metrics-card">
            <h3 class="metrics-card-title">{{ t('admin.metrics.byProjectTitle') }}</h3>
            <table class="metrics-mini-table">
              <thead><tr><th>{{ t('admin.metrics.colProject') }}</th><th class="text-right">{{ t('admin.metrics.colCalls') }}</th><th class="text-right">Token</th></tr></thead>
              <tbody>
                <tr v-for="g in overview?.invocations_by_project || []" :key="g.key || 'unknown'">
                  <td>{{ formatProjectGroupLabel(g.key) }}</td>
                  <td class="text-right">{{ formatNumber(g.invocation_count) }}</td>
                  <td class="text-right">{{ formatNumber(g.total_tokens) }}</td>
                </tr>
                <tr v-if="!overview?.invocations_by_project?.length"><td colspan="3" class="metrics-empty">{{ t('admin.metrics.emptyData') }}</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Business summaries -->
        <div class="metrics-biz-grid">
          <div class="metrics-card">
            <h3 class="metrics-card-title">{{ t('admin.metrics.chatUserTitle') }}</h3>
            <ul class="metrics-stat-list">
              <li><span>{{ t('admin.metrics.chatTotalUsers') }}</span><b>{{ formatNumber(overview?.chat.total_users) }}</b></li>
              <li><span>{{ t('admin.metrics.chatActiveUsers') }}</span><b>{{ formatNumber(overview?.chat.active_users) }}</b></li>
              <li><span>{{ t('admin.metrics.chatSessions') }}</span><b>{{ formatNumber(overview?.chat.chat_session_count) }}</b></li>
              <li><span>{{ t('admin.metrics.chatMessages') }}</span><b>{{ formatNumber(overview?.chat.chat_message_count) }}</b></li>
            </ul>
          </div>
          <div class="metrics-card">
            <h3 class="metrics-card-title">{{ t('admin.metrics.logsTitle') }}</h3>
            <ul class="metrics-stat-list">
              <li><span>{{ t('admin.metrics.logsUploadCount') }}</span><b>{{ formatNumber(overview?.logs.upload_count) }}</b></li>
              <li><span>{{ t('admin.metrics.logsUploadBytes') }}</span><b>{{ formatBytes(overview?.logs.uploaded_bytes) }}</b></li>
              <li v-for="[k, v] in objToPairs(overview?.logs.ai_analysis_counts)" :key="k">
                <span>{{ t('admin.metrics.logsAiAnalysis', { key: k }) }}</span><b>{{ formatNumber(v) }}</b>
              </li>
            </ul>
          </div>
          <div class="metrics-card">
            <h3 class="metrics-card-title">{{ t('admin.metrics.packagesTitle') }}</h3>
            <ul class="metrics-stat-list">
              <li><span>{{ t('admin.metrics.packagesCount') }}</span><b>{{ formatNumber(overview?.packages.package_count) }}</b></li>
              <li><span>{{ t('admin.metrics.packagesTotalSize') }}</span><b>{{ formatBytes(overview?.packages.total_bytes) }}</b></li>
              <li><span>{{ t('admin.metrics.packagesSearchCount') }}</span><b>{{ formatNumber(overview?.packages.search_count) }}</b></li>
              <li v-for="[k, v] in objToPairs(overview?.packages.counts_by_project)" :key="k">
                <span>{{ t('admin.metrics.packagesProjectDistribution', { key: k === 'unassociated' ? t('raven.unassociatedProject') : k }) }}</span><b>{{ formatNumber(v) }}</b>
              </li>
            </ul>
          </div>
          <div class="metrics-card">
            <h3 class="metrics-card-title">{{ t('admin.metrics.devicesTitle') }}</h3>
            <ul class="metrics-stat-list">
              <li v-for="[k, v] in objToPairs(overview?.devices.counts_by_state)" :key="k">
                <span>{{ k }}</span><b>{{ formatNumber(v) }}</b>
              </li>
              <li v-if="!objToPairs(overview?.devices.counts_by_state).length" class="metrics-empty-li">{{ t('admin.metrics.noDeviceConnections') }}</li>
            </ul>
          </div>
        </div>

        <!-- User ranking -->
        <div class="metrics-card">
          <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
            <h3 class="metrics-card-title mb-0">{{ t('admin.metrics.userRankingTitle') }}</h3>
            <div class="flex items-center gap-2">
              <span class="text-xs text-slate-500">{{ t('admin.metrics.sortLabel') }}</span>
              <div class="metrics-segment">
                <button class="metrics-segment-btn" :class="{ 'is-active': userSort === 'total_tokens' }" @click="changeSort('total_tokens')">Token</button>
                <button class="metrics-segment-btn" :class="{ 'is-active': userSort === 'run_count' }" @click="changeSort('run_count')">{{ t('admin.metrics.sortByCalls') }}</button>
              </div>
            </div>
          </div>
          <div v-if="loadingUsers" class="metrics-empty">{{ t('admin.metrics.loadingUsers') }}</div>
          <div v-else-if="!users.length" class="metrics-empty">{{ t('admin.metrics.noUserData') }}</div>
          <div v-else class="overflow-x-auto">
            <table class="metrics-table">
              <thead>
                <tr>
                  <th>{{ t('admin.metrics.colUser') }}</th>
                  <th class="text-right">{{ t('admin.metrics.colTotalTokens') }}</th>
                  <th class="text-right">{{ t('admin.metrics.colInput') }}</th>
                  <th class="text-right">{{ t('admin.metrics.colOutput') }}</th>
                  <th class="text-right">{{ t('admin.metrics.colCallCount') }}</th>
                  <th class="text-right">{{ t('admin.metrics.colSuccessFail') }}</th>
                  <th>{{ t('admin.metrics.colLastActive') }}</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in users" :key="row.user_id" class="metrics-row" @click="openUserDetail(row)">
                  <td>
                    <div class="font-medium text-slate-900">{{ row.display_name || row.username || row.user_id }}</div>
                    <div class="text-xs text-slate-400">{{ row.username || row.user_id }}</div>
                  </td>
                  <td class="text-right font-semibold">{{ formatNumber(row.total_tokens) }}</td>
                  <td class="text-right">{{ formatNumber(row.input_tokens) }}</td>
                  <td class="text-right">{{ formatNumber(row.output_tokens) }}</td>
                  <td class="text-right">{{ formatNumber(row.run_count) }}</td>
                  <td class="text-right">{{ formatNumber(row.success_count) }} / {{ formatNumber(row.failure_count) }}</td>
                  <td class="text-slate-500 text-xs">{{ formatTimestamp(row.last_active_at) }}</td>
                  <td class="text-right"><span class="metrics-link">{{ t('admin.metrics.detailLink') }}</span></td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="userTotal > userPerPage" class="metrics-pager">
            <button :disabled="userPage <= 1" @click="gotoUserPage(userPage - 1)">{{ t('admin.metrics.prevPage') }}</button>
            <span>{{ userPage }} / {{ userPageCount }}</span>
            <button :disabled="userPage >= userPageCount" @click="gotoUserPage(userPage + 1)">{{ t('admin.metrics.nextPage') }}</button>
          </div>
        </div>

        <!-- Raw events -->
        <div class="metrics-card">
          <div class="flex items-center justify-between mb-3 flex-wrap gap-2">
            <h3 class="metrics-card-title mb-0">{{ t('admin.metrics.eventsTitle') }}</h3>
            <div class="flex items-center gap-2">
              <input
                v-model="eventSourceFilter"
                type="text"
                :placeholder="t('admin.metrics.eventSourcePlaceholder')"
                class="rounded-lg border border-slate-200 px-3 py-1.5 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
                @keyup.enter="applyEventFilter"
              />
              <button class="metrics-mini-btn" @click="applyEventFilter">{{ t('admin.metrics.filterBtn') }}</button>
            </div>
          </div>
          <div v-if="loadingEvents" class="metrics-empty">{{ t('admin.metrics.loadingEvents') }}</div>
          <div v-else-if="!events.length" class="metrics-empty">{{ t('admin.metrics.noEvents') }}</div>
          <div v-else class="overflow-x-auto">
            <table class="metrics-table">
              <thead>
                <tr>
                  <th>{{ t('admin.metrics.colTime') }}</th>
                  <th>{{ t('admin.metrics.colType') }}</th>
                  <th>Source</th>
                  <th>{{ t('admin.metrics.colProject') }}</th>
                  <th>{{ t('admin.metrics.colTriggerUser') }}</th>
                  <th>Model</th>
                  <th>{{ t('admin.metrics.colStatus') }}</th>
                  <th class="text-right">Token</th>
                  <th class="text-right">{{ t('admin.metrics.colActions') }}</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="ev in events" :key="ev.id">
                  <td class="text-xs text-slate-500">{{ formatTimestamp(ev.occurred_at) }}</td>
                  <td class="text-xs">{{ ev.event_type }}</td>
                  <td class="text-xs">
                    <div>{{ ev.source }}</div>
                    <!-- OCR shares this run's id, so it is one request, not two rows. -->
                    <span v-if="mergedOcrEvents(ev).length" class="metrics-ocr-chip">
                      {{
                        ocrImageCount(ev)
                          ? t('admin.metrics.ocrMergedWithImages', { count: ocrImageCount(ev) })
                          : t('admin.metrics.ocrMerged')
                      }}
                    </span>
                  </td>
                  <td class="text-xs">{{ formatProjectGroupLabel(ev.project_repo_id) }}</td>
                  <td class="text-xs">{{ ev.display_name || ev.username || ev.user_id || '--' }}</td>
                  <td class="text-xs">
                    <div>{{ ev.model || '--' }}</div>
                    <div
                      v-for="ocr in mergedOcrEvents(ev)"
                      :key="ocr.id"
                      class="metrics-ocr-sub"
                      :title="t('admin.metrics.ocrModelHint')"
                    >
                      OCR · {{ ocr.model || '--' }}
                    </div>
                  </td>
                  <td><span class="metrics-status" :class="`is-${ev.status || 'unknown'}`">{{ ev.status || '--' }}</span></td>
                  <td class="text-right">
                    <div>{{ formatNumber(combinedTokens(ev)) }}</div>
                    <div v-if="mergedOcrEvents(ev).length" class="metrics-ocr-sub">
                      {{ formatNumber(ev.total_tokens) }} + {{ formatNumber(ocrTokens(ev)) }} OCR
                    </div>
                  </td>
                  <td class="text-right">
                    <button
                      class="metrics-conversation-btn"
                      :disabled="!ev.conversation_available"
                      :title="ev.conversation_available ? t('admin.metrics.viewConversation') : t('admin.metrics.noLinkedConversation')"
                      @click="openEventConversation(ev)"
                    >
                      {{ ev.conversation_available ? t('admin.metrics.viewConversation') : t('admin.metrics.noConversation') }}
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="eventsTotal > eventsPerPage" class="metrics-pager">
            <button :disabled="eventsPage <= 1" @click="gotoEventsPage(eventsPage - 1)">{{ t('admin.metrics.prevPage') }}</button>
            <span>{{ eventsPage }} / {{ eventsPageCount }}</span>
            <button :disabled="eventsPage >= eventsPageCount" @click="gotoEventsPage(eventsPage + 1)">{{ t('admin.metrics.nextPage') }}</button>
          </div>
        </div>
      </section>
    </main>

    <!-- User detail drawer -->
    <div v-if="detailVisible" class="admin-modal-backdrop" @click="closeDetail">
      <div class="metrics-drawer" @click.stop>
        <div class="flex items-center justify-between mb-4">
          <div>
            <h3 class="text-base font-semibold text-slate-900">
              {{ detail?.display_name || detail?.username || detail?.user_id || t('admin.metrics.userDetailTitle') }}
            </h3>
            <p class="text-xs text-slate-400">{{ detail?.username || detail?.user_id }}</p>
          </div>
          <button class="admin-icon-btn" @click="closeDetail"><X :size="16" /></button>
        </div>

        <div v-if="loadingDetail" class="metrics-empty">{{ t('admin.metrics.loadingUsers') }}</div>
        <div v-else-if="detail" class="space-y-4">
          <div class="metrics-kpi-grid">
            <div class="metrics-card metrics-kpi">
              <span class="metrics-kpi-label">{{ t('admin.metrics.kpiUserTokens') }}</span>
              <span class="metrics-kpi-value">{{ formatNumber(detail.tokens.total_tokens) }}</span>
            </div>
            <div class="metrics-card metrics-kpi">
              <span class="metrics-kpi-label">{{ t('admin.metrics.kpiUserCalls') }}</span>
              <span class="metrics-kpi-value">{{ formatNumber(detail.invocation_count) }}</span>
            </div>
            <div class="metrics-card metrics-kpi">
              <span class="metrics-kpi-label">{{ t('admin.metrics.kpiUserSuccessFail') }}</span>
              <span class="metrics-kpi-value">{{ formatNumber(detail.status_counts.succeeded) }}/{{ formatNumber(detail.status_counts.failed) }}</span>
            </div>
            <div class="metrics-card metrics-kpi">
              <span class="metrics-kpi-label">{{ t('admin.metrics.kpiUserCost') }}</span>
              <span class="metrics-kpi-value">{{ formatCost(detail) }}</span>
            </div>
          </div>

          <div class="metrics-card">
            <h3 class="metrics-card-title">{{ t('admin.metrics.byAgentTitle') }}</h3>
            <table class="metrics-mini-table">
              <thead><tr><th>Agent</th><th class="text-right">{{ t('admin.metrics.colCalls') }}</th><th class="text-right">Token</th></tr></thead>
              <tbody>
                <tr v-for="g in detail.invocations_by_agent_kind" :key="g.key || 'unknown'">
                  <td>{{ g.key || t('admin.metrics.unknownKey') }}</td>
                  <td class="text-right">{{ formatNumber(g.invocation_count) }}</td>
                  <td class="text-right">{{ formatNumber(g.total_tokens) }}</td>
                </tr>
                <tr v-if="!detail.invocations_by_agent_kind.length"><td colspan="3" class="metrics-empty">{{ t('admin.metrics.emptyData') }}</td></tr>
              </tbody>
            </table>
          </div>

          <div v-if="detail.errors_by_kind.length" class="metrics-card">
            <h3 class="metrics-card-title">{{ t('admin.metrics.errorTypesTitle') }}</h3>
            <table class="metrics-mini-table">
              <thead><tr><th>{{ t('admin.metrics.colErrorType') }}</th><th class="text-right">{{ t('admin.metrics.colErrorCount') }}</th></tr></thead>
              <tbody>
                <tr v-for="g in detail.errors_by_kind" :key="g.key || 'unknown'">
                  <td>{{ g.key || t('admin.metrics.unknownKey') }}</td>
                  <td class="text-right">{{ formatNumber(g.invocation_count) }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="metrics-card">
            <h3 class="metrics-card-title">{{ t('admin.metrics.recentEventsTitle') }}</h3>
            <div v-if="!detail.recent_events.length" class="metrics-empty">{{ t('admin.metrics.noEvents') }}</div>
            <table v-else class="metrics-mini-table">
              <thead><tr><th>{{ t('admin.metrics.colTime') }}</th><th>Source</th><th>{{ t('admin.metrics.colEventStatus') }}</th><th class="text-right">Token</th></tr></thead>
              <tbody>
                <tr v-for="ev in detail.recent_events" :key="ev.id">
                  <td class="text-xs text-slate-500">{{ formatTimestamp(ev.occurred_at) }}</td>
                  <td class="text-xs">{{ ev.source }}</td>
                  <td><span class="metrics-status" :class="`is-${ev.status || 'unknown'}`">{{ ev.status || '--' }}</span></td>
                  <td class="text-right">{{ formatNumber(ev.total_tokens) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- Only the explicit X closes this temporary conversation. -->
    <ElDialog :model-value="conversationVisible" class="admin-conversation-dialog"
      width="min(1100px, 94vw)" align-center :append-to-body="false" :z-index="2100"
      :show-close="false" :close-on-click-modal="false" :close-on-press-escape="false"
      :aria-label="t('admin.metrics.conversationTitle')" destroy-on-close @close="closeConversation">
      <div class="conversation-modal">
        <div class="conversation-drawer-header">
          <div class="min-w-0">
            <div class="conversation-admin-badge">{{ t('admin.metrics.adminConversationBadge') }}</div>
            <h3 class="conversation-title">
              {{ conversation?.title || t('admin.metrics.conversationTitle') }}
            </h3>
            <p v-if="conversation" class="conversation-meta">
              {{ conversation.display_name || conversation.username || conversation.user_id }}
              · {{ t('admin.metrics.conversationMessageCount', { count: conversation.message_count }) }}
              <span v-if="conversation.is_deleted"> · {{ t('admin.metrics.deletedConversation') }}</span>
            </p>
          </div>
          <button class="admin-icon-btn" :aria-label="t('common.close')" @click="closeConversation"><X :size="16" /></button>
        </div>

        <div v-if="loadingConversation" class="metrics-empty">{{ t('admin.metrics.loadingConversation') }}</div>
        <div v-else-if="conversationLoadError" class="metrics-empty follow-up-error" role="alert">{{ conversationLoadError }}</div>
        <div v-else-if="conversation" ref="conversationThreadRef" class="admin-conversation-thread" @click="onConversationClick">
          <div
            v-for="({ message, logResult }, index) in conversationMessages"
            :key="`${message.created_at || 'message'}-${index}`"
            :class="['admin-conversation-message', message.role === 'user' ? 'is-user' : 'is-ai']"
            :data-event-reply="message.is_event_reply || undefined"
          >
            <template v-if="message.role === 'user'">
              <!-- The originals behind the OCR text the agent actually saw. -->
              <div v-if="message.images?.length" class="admin-user-images">
                <button
                  v-for="image in message.images"
                  :key="image.id"
                  type="button"
                  class="admin-user-image"
                  :class="{ 'is-missing': !conversationImageUrls[image.id] }"
                  :title="image.name || t('admin.metrics.attachedImage')"
                  :disabled="!conversationImageUrls[image.id]"
                  @click="previewImageUrl = conversationImageUrls[image.id] || null"
                >
                  <img
                    v-if="conversationImageUrls[image.id]"
                    :src="conversationImageUrls[image.id]"
                    :alt="image.name || t('admin.metrics.attachedImage')"
                  />
                  <span v-else>{{ t('admin.metrics.imageUnavailable') }}</span>
                </button>
              </div>
              <div class="admin-user-bubble">{{ message.content }}</div>
              <div class="admin-message-label">{{ t('sharedConversation.userLabel') }}</div>
            </template>
            <template v-else>
              <div class="admin-ai-label">
                {{ t('sharedConversation.aiLabel') }}
                <span v-if="message.is_event_reply" class="admin-event-reply-label">
                  {{ t('admin.metrics.eventReply') }}
                </span>
              </div>
              <AgentTraceStream
                v-if="message.run_model || conversationTraceEvents(message.trace_events).length"
                class="admin-ai-trace"
                :events="conversationTraceEvents(message.trace_events)"
                :run-model="message.run_model"
                :running="false"
              />
              <details v-if="logResult" class="admin-ai-run-summary">
                <summary>{{ t('agentTrace.runSummary') }}</summary>
                <div class="admin-ai-content" v-html="renderConversationAi(logResult.summaryMarkdown)"></div>
              </details>
              <div
                v-if="!logResult || logResult.bodyMarkdown"
                class="admin-ai-content"
                v-html="renderConversationAi(logResult?.bodyMarkdown ?? message.content)"
              ></div>
            </template>
          </div>
          <div v-if="!conversation.messages.length" class="metrics-empty">{{ t('admin.metrics.emptyConversation') }}</div>
          <section class="admin-follow-up" :aria-label="t('admin.metrics.followUpTitle')">
            <h4>{{ t('admin.metrics.followUpTitle') }}</h4>
            <p class="follow-up-notice">{{ t('admin.metrics.followUpNotice') }}</p>
            <div v-for="(turn, index) in followUpHistory" :key="index"
              :class="['admin-conversation-message', turn.role === 'user' ? 'is-user' : 'is-ai']">
              <div v-if="turn.role === 'user'" class="admin-user-bubble">{{ turn.content }}</div>
              <div v-else class="admin-ai-content" v-html="renderConversationAi(turn.content)"></div>
            </div>
            <template v-if="followUpRunning">
              <div class="admin-user-bubble">{{ followUpPendingQuestion }}</div>
              <div class="admin-ai-content" v-html="renderConversationAi(followUpAnswer)"></div>
              <p role="status">{{ t('admin.metrics.followUpRunning') }}</p>
            </template>
            <p v-if="followUpError" class="follow-up-error" role="alert">{{ followUpError }}</p>
          </section>
        </div>
        <form v-if="conversation && !loadingConversation" class="follow-up-form" @submit.prevent="sendFollowUp">
          <textarea ref="followUpInput" v-model="followUpQuestion" rows="2" maxlength="8000"
            :aria-label="t('admin.metrics.followUpTitle')" :placeholder="t('admin.metrics.followUpPlaceholder')"
            :disabled="followUpRunning" @keydown.ctrl.enter.prevent="sendFollowUp" @keydown.meta.enter.prevent="sendFollowUp"></textarea>
          <button type="submit" class="metrics-conversation-btn" :disabled="followUpRunning || !followUpQuestion.trim()">
            {{ t('admin.metrics.followUpSend') }}
          </button>
        </form>
      </div>
    </ElDialog>
    <AdminMermaidPreview :source="diagramSource" @close="diagramSource = null" />

    <!-- Full-size view of a clicked attachment; screenshots are unreadable as thumbnails. -->
    <div
      v-if="previewImageUrl"
      class="admin-image-preview"
      role="dialog"
      :aria-label="t('admin.metrics.attachedImage')"
      @click="previewImageUrl = null"
    >
      <img :src="previewImageUrl" :alt="t('admin.metrics.attachedImage')" />
    </div>
  </div>
</template>

<style scoped>
.admin-console {
  --admin-topbar-height: 72px;
  --admin-sidebar-width: 280px;
  min-height: 100vh;
  background: var(--admin-page-bg);
}

.admin-topbar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  width: 100%;
  height: var(--admin-topbar-height);
  z-index: 70;
  background: var(--admin-topbar-bg);
  border-bottom: 1px solid var(--admin-hairline);
  backdrop-filter: blur(10px);
}

.admin-topbar-inner {
  height: 100%;
  padding: 0 1rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}

.admin-topbar-left {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.admin-icon-btn {
  width: 2.25rem;
  height: 2.25rem;
  border: 1px solid var(--admin-hairline-strong);
  border-radius: 0.625rem;
  color: var(--admin-ink);
  background: var(--admin-surface);
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.admin-icon-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.admin-title {
  color: var(--admin-ink);
  font-size: 0.95rem;
  font-weight: 700;
  line-height: 1.1;
}

.admin-subtitle {
  color: var(--admin-body);
  font-size: 0.75rem;
}

.admin-topbar-right {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.admin-logout-btn {
  border: 1px solid var(--admin-hairline-strong);
  border-radius: 0.55rem;
  color: var(--admin-ink);
  background: var(--admin-surface);
  font-size: 0.75rem;
  font-weight: 600;
  padding: 0.45rem 0.7rem;
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
}

.admin-sidebar {
  position: fixed;
  left: 0;
  top: 0;
  width: var(--admin-sidebar-width);
  height: 100vh;
  z-index: 60;
  background: var(--admin-sidebar-bg);
  border-right: 1px solid var(--admin-hairline);
  padding: calc(var(--admin-topbar-height) + 1rem) 1rem 1rem;
  transition: transform 0.25s ease;
  overflow-y: auto;
}

.admin-sidebar.is-hidden {
  transform: translateX(calc(-1 * var(--admin-sidebar-width)));
}

.admin-side-nav-item {
  width: 100%;
  text-align: left;
  padding: 0.8rem;
  border-radius: 0.75rem;
  border: 1px solid transparent;
  color: var(--admin-body);
  background: transparent;
}

.admin-side-nav-item.is-active {
  color: var(--admin-on-dark);
  background: var(--admin-primary);
  border-color: var(--admin-primary);
}

.admin-main {
  min-height: 100vh;
  padding: calc(var(--admin-topbar-height) + 1rem) 1rem 1rem calc(var(--admin-sidebar-width) + 1rem);
  transition: padding-left 0.25s ease;
}

.admin-main.is-sidebar-hidden {
  padding-left: 1rem;
}

.admin-login-wrap {
  max-width: 960px;
  margin: 1.25rem auto 0;
}

.admin-sidebar-backdrop {
  display: none;
}

.admin-modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 90;
  background: var(--admin-modal-backdrop-bg);
  display: flex;
  align-items: stretch;
  justify-content: flex-end;
  padding: 0;
}

/* ==================== Metrics-specific ==================== */

.metrics-toolbar {
  display: flex;
  align-items: center;
  gap: 1.25rem;
  flex-wrap: wrap;
  background: var(--admin-surface);
  border: 1px solid var(--admin-hairline);
  border-radius: 1rem;
  padding: 0.75rem 1rem;
}

.metrics-control-group {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.metrics-control-label {
  font-size: 0.75rem;
  color: var(--admin-body);
  font-weight: 600;
}

.metrics-segment {
  display: inline-flex;
  border: 1px solid var(--admin-hairline-strong);
  border-radius: 0.6rem;
  overflow: hidden;
}

.metrics-segment-btn {
  padding: 0.35rem 0.75rem;
  font-size: 0.8rem;
  color: var(--admin-body);
  background: var(--admin-canvas-soft);
  border-left: 1px solid var(--admin-hairline);
}

.metrics-segment-btn:first-child {
  border-left: none;
}

.metrics-segment-btn.is-active {
  background: var(--admin-primary);
  color: var(--admin-on-dark);
}

.metrics-refresh-btn {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.4rem 0.85rem;
  border-radius: 0.6rem;
  border: 1px solid var(--admin-hairline-strong);
  background: var(--admin-surface);
  font-size: 0.8rem;
  color: var(--admin-ink);
}

.metrics-select {
  min-width: 13rem;
  max-width: min(22rem, 72vw);
  border: 1px solid var(--admin-hairline-strong);
  border-radius: 0.6rem;
  background: var(--admin-surface);
  color: var(--admin-ink);
  font-size: 0.8rem;
  padding: 0.4rem 2rem 0.4rem 0.7rem;
  outline: none;
}

.metrics-select:focus {
  border-color: var(--admin-primary);
  box-shadow: 0 0 0 3px var(--admin-focus-ring);
}

.metrics-card {
  background: var(--admin-surface);
  border: 1px solid var(--admin-hairline);
  border-radius: 1rem;
  padding: 1rem 1.1rem;
  box-shadow: var(--rw-shadow-soft);
}

.metrics-card-title {
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--admin-ink);
  margin-bottom: 0.75rem;
}

.metrics-kpi-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.85rem;
}

.metrics-kpi {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.metrics-kpi-label {
  font-size: 0.75rem;
  color: var(--admin-body);
}

.metrics-kpi-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--admin-ink);
  line-height: 1.2;
}

.metrics-kpi-sub {
  font-size: 0.72rem;
  color: var(--admin-muted);
}

.metrics-graph {
  --chart-track-h: 180px;
  --chart-label-h: 20px;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.metrics-graph.labels-rotated {
  --chart-label-h: 34px;
}

.metrics-graph-body {
  display: flex;
  align-items: flex-start;
  gap: 0.4rem;
}

.metrics-yaxis-title {
  flex: none;
  height: var(--chart-track-h);
  display: flex;
  align-items: center;
  justify-content: center;
  writing-mode: vertical-rl;
  /* CJK glyphs default to upright in vertical writing mode; force them sideways
     so the rotate(180deg) below reads bottom-to-top instead of upside down. */
  -webkit-text-orientation: sideways;
  text-orientation: sideways;
  transform: rotate(180deg);
  font-size: 0.7rem;
  font-weight: 600;
  color: var(--admin-body);
}

.metrics-yaxis {
  position: relative;
  flex: none;
  width: 2.5rem;
  height: var(--chart-track-h);
}

.metrics-yaxis-tick {
  position: absolute;
  right: 0.3rem;
  transform: translateY(-50%);
  font-size: 0.62rem;
  color: var(--admin-muted);
  white-space: nowrap;
}

.metrics-plot {
  position: relative;
  flex: 1;
  min-width: 0;
  height: calc(var(--chart-track-h) + var(--chart-label-h));
}

.metrics-gridlines {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: var(--chart-track-h);
  border-left: 1px solid var(--admin-chart-axis);
  pointer-events: none;
}

.metrics-gridline {
  position: absolute;
  left: 0;
  right: 0;
  border-top: 1px dashed var(--admin-chart-grid);
}

.metrics-gridline.is-base {
  border-top: 1px solid var(--admin-chart-axis);
}

.metrics-chart {
  position: relative;
  display: flex;
  align-items: flex-end;
  gap: 0.35rem;
  height: 100%;
  overflow-x: auto;
}

.metrics-bar-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 28px;
  flex: 1;
  height: 100%;
}

.metrics-bar-track {
  flex: 1;
  width: 100%;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  max-width: 36px;
}

.metrics-bar-fill {
  width: 100%;
  background: linear-gradient(180deg, var(--admin-chart-bar-from) 0%, var(--admin-chart-bar-to) 100%);
  border-radius: 0.35rem 0.35rem 0 0;
  min-height: 2px;
}

.metrics-bar-label {
  height: var(--chart-label-h);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  overflow: visible;
}

.metrics-bar-label span {
  font-size: 0.62rem;
  color: var(--admin-muted);
  white-space: nowrap;
  padding-top: 2px;
}

.metrics-graph.labels-rotated .metrics-bar-label span {
  transform: rotate(-30deg);
  transform-origin: center;
}

.metrics-xaxis-title {
  text-align: center;
  font-size: 0.7rem;
  font-weight: 600;
  color: var(--admin-body);
}

.metrics-distribution-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.85rem;
}

.metrics-distribution-grid.has-projects {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.metrics-biz-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0.85rem;
}

.metrics-stat-list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.metrics-stat-list li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 0.82rem;
  color: var(--admin-body);
}

.metrics-stat-list b {
  color: var(--admin-ink);
  font-weight: 600;
}

.metrics-empty,
.metrics-empty-li {
  color: var(--admin-muted);
  font-size: 0.85rem;
  text-align: center;
  padding: 1rem 0;
}

.metrics-mini-table,
.metrics-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.82rem;
  color: var(--admin-body);
}

.metrics-mini-table th,
.metrics-table th {
  text-align: left;
  font-weight: 600;
  color: var(--admin-body);
  padding: 0.4rem 0.5rem;
  border-bottom: 1px solid var(--admin-hairline-strong);
  font-size: 0.75rem;
}

.metrics-mini-table td,
.metrics-table td {
  padding: 0.45rem 0.5rem;
  border-bottom: 1px solid var(--admin-hairline);
}

.text-right {
  text-align: right;
}

.metrics-row {
  cursor: pointer;
}

.metrics-row:hover {
  background: var(--admin-canvas-soft);
}

.metrics-link {
  color: var(--admin-link);
  font-size: 0.78rem;
  font-weight: 600;
}

.metrics-status {
  display: inline-block;
  padding: 0.1rem 0.5rem;
  border-radius: 999px;
  font-size: 0.7rem;
  font-weight: 600;
  background: var(--admin-surface-strong);
  color: var(--admin-body);
}

.metrics-status.is-succeeded {
  background: var(--admin-status-success-bg);
  color: var(--admin-success);
}

.metrics-status.is-failed,
.metrics-status.is-timeout {
  background: var(--admin-status-error-bg);
  color: var(--admin-error);
}

.metrics-status.is-cancelled,
.metrics-status.is-stale {
  background: var(--admin-status-warning-bg);
  color: var(--admin-warning);
}

.metrics-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  margin-top: 0.85rem;
  font-size: 0.8rem;
  color: var(--admin-body);
}

.metrics-pager button {
  padding: 0.3rem 0.75rem;
  border-radius: 0.5rem;
  border: 1px solid var(--admin-hairline-strong);
  color: var(--admin-ink);
  background: var(--admin-surface);
}

.metrics-pager button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.metrics-mini-btn {
  padding: 0.35rem 0.75rem;
  border-radius: 0.5rem;
  border: 1px solid var(--admin-hairline-strong);
  color: var(--admin-ink);
  background: var(--admin-surface);
  font-size: 0.8rem;
}

.metrics-mini-btn:hover {
  border-color: var(--admin-ink);
  background: var(--admin-surface-strong);
}

/* Marks a row whose request also spent an OCR call, folded in from its own event. */
.metrics-ocr-chip {
  display: inline-flex;
  margin-top: 0.2rem;
  padding: 0.05rem 0.4rem;
  border-radius: 999px;
  color: var(--admin-accent-soft-ink);
  background: var(--admin-accent-soft-bg);
  font-size: 0.66rem;
  font-weight: 700;
  white-space: nowrap;
}

.metrics-ocr-sub {
  margin-top: 0.15rem;
  color: var(--admin-muted);
  font-size: 0.68rem;
  white-space: nowrap;
}

.metrics-conversation-btn {
  white-space: nowrap;
  color: var(--admin-accent-soft-ink);
  font-size: 0.76rem;
  font-weight: 600;
  padding: 0.28rem 0.55rem;
  border: 1px solid var(--admin-accent-soft-border);
  border-radius: 0.5rem;
  background: var(--admin-accent-soft-bg);
}

.metrics-conversation-btn:hover:not(:disabled) {
  color: var(--admin-on-dark);
  border-color: var(--admin-primary);
  background: var(--admin-primary);
}

.metrics-conversation-btn:disabled {
  color: var(--admin-muted);
  border-color: var(--admin-hairline);
  background: var(--admin-canvas-soft);
  cursor: not-allowed;
}

.metrics-drawer {
  width: min(640px, 100%);
  height: 100vh;
  background: var(--admin-canvas-soft);
  box-shadow: var(--admin-drawer-shadow);
  padding: 1.25rem;
  overflow-y: auto;
}

.conversation-modal {
  display: flex; flex-direction: column; height: 84vh; max-height: 900px;
  background: var(--admin-surface); color: var(--admin-ink); overflow: hidden;
}
.conversation-modal .conversation-drawer-header, .conversation-modal .follow-up-form { flex-shrink: 0; }
.conversation-modal .admin-conversation-thread { overflow-y: auto; min-height: 0; flex: 1; }
.admin-follow-up { border-top: 1px solid var(--admin-hairline); margin-top: 1.5rem; padding-top: 1rem; }
.admin-follow-up h4 { font-weight: 700; }
.follow-up-notice { font-size: .8rem; color: var(--admin-muted); margin: .5rem 0 1rem; }
.follow-up-error { color: var(--el-color-danger); }
.follow-up-form { display: flex; gap: .75rem; align-items: flex-end; padding: 1rem; border-top: 1px solid var(--admin-hairline); }
.follow-up-form textarea { flex: 1; min-width: 0; resize: vertical; max-height: 160px; padding: .6rem; border: 1px solid var(--admin-hairline); border-radius: 8px; background: var(--admin-surface); color: var(--admin-ink); }
:global(.el-dialog.admin-conversation-dialog) { margin: auto !important; padding: 0; overflow: hidden; border-radius: 16px; }
:global(.admin-conversation-dialog .el-dialog__header) { display: none; }
:global(.admin-conversation-dialog .el-dialog__body) { padding: 0; }
.conversation-modal :deep(.mermaid-container.is-rendered) { cursor: zoom-in; }
/* Mermaid is a block diagram inside Markdown's code-fence wrapper. */
.admin-ai-content :deep(pre:has(> code.language-mermaid)),
.admin-ai-content :deep(code.language-mermaid) {
  display: block; padding: 0; border: 0; background: transparent;
  box-shadow: none; white-space: normal;
}

.conversation-drawer-header {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  padding: 1.1rem 1.25rem;
  background: var(--admin-surface-translucent);
  border-bottom: 1px solid var(--admin-hairline);
  backdrop-filter: blur(8px);
}

.conversation-admin-badge {
  display: inline-flex;
  margin-bottom: 0.35rem;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  color: var(--admin-accent-soft-ink);
  background: var(--admin-accent-soft-bg);
  font-size: 0.68rem;
  font-weight: 700;
}

.conversation-title {
  color: var(--admin-ink);
  font-size: 1.05rem;
  font-weight: 700;
  line-height: 1.4;
  word-break: break-word;
}

.conversation-meta {
  margin-top: 0.25rem;
  color: var(--admin-body);
  font-size: 0.75rem;
}

.admin-conversation-thread {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 1.7rem;
  padding: 1.5rem 1.25rem 3rem;
}

.admin-conversation-message.is-user {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
}

.admin-user-bubble {
  max-width: 90%;
  padding: 0.7rem 0.95rem;
  border-radius: 0.9rem 0.9rem 0.25rem 0.9rem;
  color: var(--admin-on-dark);
  background: var(--admin-primary);
  font-size: 0.9rem;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

/* Attachments sit above the bubble and share its right alignment. */
.admin-user-images {
  max-width: 90%;
  margin-bottom: 0.4rem;
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 0.4rem;
}

.admin-user-image {
  width: 7.5rem;
  height: 7.5rem;
  padding: 0;
  border: 1px solid var(--admin-hairline-strong);
  border-radius: 0.6rem;
  background: var(--admin-canvas-soft);
  overflow: hidden;
  cursor: zoom-in;
}

.admin-user-image img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.admin-user-image.is-missing {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0.4rem;
  color: var(--admin-muted);
  font-size: 0.68rem;
  text-align: center;
  cursor: default;
}

.admin-image-preview {
  position: fixed;
  inset: 0;
  z-index: 2300;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2rem;
  background: var(--admin-modal-backdrop-bg);
  cursor: zoom-out;
}

.admin-image-preview img {
  max-width: 100%;
  max-height: 100%;
  border-radius: 0.5rem;
  box-shadow: var(--admin-drawer-shadow);
}

.admin-message-label,
.admin-ai-label {
  margin-top: 0.35rem;
  color: var(--admin-muted);
  font-size: 0.7rem;
}

.admin-event-reply-label {
  margin-inline-start: 0.5rem;
  padding: 0.18rem 0.38rem;
  border-radius: 0.3rem;
  color: var(--admin-accent-soft-ink);
  background: var(--admin-accent-soft-bg);
}

.admin-conversation-message.is-ai {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.admin-ai-label {
  margin-top: 0;
  color: var(--admin-body);
  font-weight: 700;
}

.admin-ai-trace {
  margin: 0.15rem 0 0.35rem;
}

.admin-ai-run-summary > summary {
  width: fit-content;
  padding: 0.2rem 0.55rem;
  border: 1px solid var(--admin-hairline);
  border-radius: 0.4rem;
  color: var(--admin-body);
  background: var(--admin-canvas-soft);
  font-size: 0.75rem;
  cursor: pointer;
}

.admin-ai-run-summary > summary:hover {
  border-color: var(--admin-hairline-strong);
  color: var(--admin-ink);
}

.admin-ai-run-summary > .admin-ai-content {
  margin: 0.6rem 0 0.4rem;
  padding-inline: 0.55rem;
  border-inline-start: 1px solid var(--admin-hairline);
}

.admin-ai-content {
  color: var(--admin-ink);
  font-size: 0.9rem;
  line-height: 1.7;
}

/* styles/markdown.css bakes its light colors in through @apply, which inlines
   the declarations instead of adding the utility classes -- so the dark remaps
   in styles/theme.css can never reach them. Restate the colour-bearing ones
   against admin tokens for this drawer, which is genuinely dark in dark mode.
   (markdown.css now carries its own html.dark token block; these rules stay
   because the drawer wants admin tokens rather than the --md-* defaults.) */
html.dark .admin-ai-content :deep(.markdown-content),
html.dark .admin-ai-content :deep(p),
html.dark .admin-ai-content :deep(li),
html.dark .admin-ai-content :deep(em),
html.dark .admin-ai-content :deep(h6) {
  color: var(--admin-body);
}

html.dark .admin-ai-content :deep(h1),
html.dark .admin-ai-content :deep(h2),
html.dark .admin-ai-content :deep(h3),
html.dark .admin-ai-content :deep(h4),
html.dark .admin-ai-content :deep(h5),
html.dark .admin-ai-content :deep(strong) {
  color: var(--admin-ink);
}

html.dark .admin-ai-content :deep(h1),
html.dark .admin-ai-content :deep(h2) {
  border-bottom-color: var(--admin-hairline);
}

html.dark .admin-ai-content :deep(del) {
  color: var(--admin-muted);
}

html.dark .admin-ai-content :deep(code:not(.hljs code):not(.language-mermaid)) {
  color: #ff7b72;
  background-color: var(--admin-surface-strong);
}

html.dark .admin-ai-content :deep(blockquote) {
  color: var(--admin-body);
  border-left-color: var(--admin-link);
  background-color: var(--admin-accent-soft-bg);
}

html.dark .admin-ai-content :deep(a) {
  color: var(--admin-link);
}

html.dark .admin-ai-content :deep(hr) {
  border-top-color: var(--admin-hairline);
}

html.dark .admin-ai-content :deep(.table-wrapper) {
  border-color: var(--admin-hairline);
}

html.dark .admin-ai-content :deep(.markdown-table) {
  background-color: var(--admin-surface);
}

html.dark .admin-ai-content :deep(.markdown-table thead) {
  background-color: var(--admin-surface-strong);
}

html.dark .admin-ai-content :deep(.markdown-table th) {
  color: var(--admin-ink);
  border-bottom-color: var(--admin-hairline-strong);
}

html.dark .admin-ai-content :deep(.markdown-table td) {
  color: var(--admin-body);
  border-bottom-color: var(--admin-hairline);
}

html.dark .admin-ai-content :deep(.markdown-table tbody tr:hover) {
  background-color: var(--admin-surface-strong);
}

html.dark .admin-ai-content :deep(.table-copy-btn) {
  color: var(--admin-body);
  background: var(--admin-surface-strong);
  border-color: var(--admin-hairline-strong);
}

@media (max-width: 1024px) {
  .metrics-kpi-grid,
  .metrics-biz-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .metrics-distribution-grid,
  .metrics-distribution-grid.has-projects {
    grid-template-columns: minmax(0, 1fr);
  }
  .admin-topbar-inner {
    padding: 0 0.75rem;
  }
}

@media (max-width: 768px) {
  .admin-console {
    --admin-sidebar-width: min(84vw, 320px);
  }
  .admin-main {
    padding: calc(var(--admin-topbar-height) + 0.85rem) 0.75rem 0.75rem;
  }
  .admin-main.is-sidebar-hidden {
    padding-left: 0.75rem;
  }
  .admin-sidebar {
    box-shadow: 20px 0 45px rgba(15, 23, 42, 0.35);
  }
  .admin-sidebar-backdrop {
    display: block;
    position: fixed;
    inset: var(--admin-topbar-height) 0 0 0;
    z-index: 55;
    background: var(--admin-backdrop-bg);
  }
  .metrics-kpi-grid,
  .metrics-biz-grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
