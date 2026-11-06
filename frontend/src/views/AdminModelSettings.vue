<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Info,
  LogOut,
  Menu,
  PanelLeftClose,
  PlugZap,
  RotateCcw,
  Save,
  XCircle,
} from 'lucide-vue-next'
import {
  adminApi,
  adminToken,
  type EndpointForm,
  type ModelSettingsData,
  type ModelSettingsTarget,
  type ModelSettingsTestResult,
  type TestModelSettingsPayload,
  type UpdateModelSettingsPayload,
} from '@/api/admin'
import AnthropicEndpointCard from '@/components/admin/AnthropicEndpointCard.vue'
import AgentRuntimeCard from '@/components/admin/AgentRuntimeCard.vue'
import ThemeToggle from '@/components/ThemeToggle.vue'
import { useAppStore } from '@/stores/app'
import { resolveAdminNavKey, type AdminNavItem } from '@/utils/adminNav'
import { ROUTER_KNOBS } from '@/utils/routerKnobs'
import { useAdminScope } from '@/composables/useAdminScope'

const { t } = useI18n()
const appStore = useAppStore()
const route = useRoute()
const router = useRouter()

const { visibleNavItems } = useAdminScope()

const isAuthenticated = ref(false)
const isLoggingIn = ref(false)

const authForm = reactive({ username: '', password: '' })

// ── Model settings state ──────────────────────────────────────────────────
const loadingSettings = ref(false)
const savingSettings = ref(false)
const resettingSettings = ref(false)
const settingsData = ref<ModelSettingsData | null>(null)
const magpieEnabled = ref<boolean | null>(null)

// The two Anthropic slots are grouped so each can be handed to
// AnthropicEndpointCard as one object; the flat `anthropic_*` / `ocr_*` keys
// are reassembled at save time.
const form = reactive({
  primary: {
    provider: 'anthropic',
    api_key: '',
    api_keys: '',
    base_url: '',
    model: '',
    small_fast_model: '',
  } as EndpointForm,
  bug_fix: { provider: 'deepseek', api_key: '', api_keys: '', base_url: '', model: 'deepseek-v4-pro', small_fast_model: '' } as EndpointForm,
  bug_fix_agent_max_tokens: 16384,
  bug_fix_agent_max_turns: 150,
  bug_fix_agent_request_timeout_seconds: 3600,
  backup: {
    provider: 'anthropic',
    api_key: '',
    api_keys: '',
    base_url: '',
    model: '',
    small_fast_model: '',
  } as EndpointForm,
  anthropic_backup_enabled: false,
  anthropic_max_tokens: 8192,
  // Routing policy. Kept flat (not nested like the endpoint slots) because the
  // backend validates these as one interdependent set.
  model_router_enabled: true,
  model_router_first_token_deadline_ms: 20000,
  model_router_slow_ttft_ms: 6000,
  model_router_window_size: 8,
  model_router_trip_threshold: 4,
  model_router_min_samples: 4,
  model_router_hard_failure_trip: 2,
  model_router_cooldown_seconds: 120,
  model_router_sample_ttl_seconds: 900,
  ocr_enabled: true,
  ocr_api_key: '',
  ocr_base_url: '',
  ocr_model: '',
  ocr_provider: '',
})

// Secrets are never returned by the API; only whether one is currently set.
const anthropicKeySet = ref(false)
const anthropicKeyCount = ref(0)
const bugFixKeySet = ref(false)
const backupKeySet = ref(false)
const ocrKeySet = ref(false)

const providerOptions = computed(() => settingsData.value?.provider_options ?? [])
const providerProfiles = computed(() => settingsData.value?.provider_profiles ?? [])

// ── Routing state ─────────────────────────────────────────────────────────
// A primary that stays degraded sends every request to the paid backup. Without
// surfacing that, the first symptom is the invoice — so show it plainly.
const router$ = computed(() => settingsData.value?.router ?? null)
const onBackup = computed(() => router$.value?.serving_slot === 'backup')
const backupSince = computed(() => {
  const at = router$.value?.breaker_opened_at
  return at ? new Date(at * 1000).toLocaleString() : ''
})
const primaryWindow = computed(() => {
  const slot = router$.value?.slots?.primary
  if (!slot || !slot.samples) return ''
  return `${slot.bad_samples}/${slot.samples}`
})

const testing = reactive<Record<ModelSettingsTarget, boolean>>({
  anthropic: false,
  anthropic_backup: false,
  bug_fix: false,
  ocr: false,
})
const testResults = reactive<Record<ModelSettingsTarget, ModelSettingsTestResult | null>>({
  anthropic: null,
  anthropic_backup: null,
  bug_fix: null,
  ocr: null,
})

// ── Connectivity test ─────────────────────────────────────────────────────
const ENDPOINT_SLOT: Partial<Record<ModelSettingsTarget, 'primary' | 'backup' | 'bug_fix'>> = {
  anthropic: 'primary',
  anthropic_backup: 'backup',
  bug_fix: 'bug_fix',
}

const parseKeyPool = (value: string) =>
  value.split(/[\r\n,]+/).map((key) => key.trim()).filter(Boolean)

const runTest = async (target: ModelSettingsTarget) => {
  testing[target] = true
  testResults[target] = null
  try {
    // Send the form's current values so a config can be verified before it is
    // saved; the API key is omitted unless it is being changed, in which case
    // the backend tests the stored one.
    const slot = ENDPOINT_SLOT[target]
    const endpoint = slot ? form[slot] : null
    const payload: TestModelSettingsPayload = endpoint
      ? {
          target,
          provider: endpoint.provider,
          base_url: endpoint.base_url.trim(),
          model: endpoint.model.trim(),
        }
      : {
          target,
          base_url: form.ocr_base_url.trim(),
          model: form.ocr_model.trim(),
        }
    if (target === 'anthropic') {
      const typedKeys = parseKeyPool(form.primary.api_keys)
      if (typedKeys.length) payload.api_keys = typedKeys
    } else {
      const typedKey = endpoint ? endpoint.api_key.trim() : form.ocr_api_key.trim()
      if (typedKey) payload.api_key = typedKey
    }

    const resp = await adminApi.testModelSettings(payload)
    if (!resp?.data) throw new Error(resp?.message || t('admin.modelSettings.testFail'))
    testResults[target] = resp.data
  } catch (err: any) {
    testResults[target] = {
      ok: false,
      target,
      error_kind: 'request_failed',
      detail: parseErrorMessage(err),
    }
  } finally {
    testing[target] = false
  }
}

const sourceOf = (key: string) => settingsData.value?.fields?.[key]?.source ?? 'env'
const sourceLabel = (key: string) => {
  const src = sourceOf(key)
  if (src === 'override') return t('admin.modelSettings.sourceOverride')
  if (src === 'unset') return t('admin.modelSettings.sourceUnset')
  return t('admin.modelSettings.sourceEnv')
}

const populateForm = (data: ModelSettingsData) => {
  settingsData.value = data
  const f = data.fields || {}
  form.primary.provider = String(f.anthropic_provider?.value ?? 'anthropic')
  form.primary.base_url = String(f.anthropic_base_url?.value ?? '')
  form.primary.model = String(f.anthropic_model?.value ?? '')
  form.primary.small_fast_model = String(f.anthropic_small_fast_model?.value ?? '')
  form.anthropic_max_tokens = Number(f.anthropic_max_tokens?.value ?? 8192)
  // An unconfigured backup has no provider yet; default the dropdown to the
  // primary's so the card opens on a coherent vendor rather than a blank one.
  form.anthropic_backup_enabled = Boolean(f.anthropic_backup_enabled?.value ?? false)
  form.backup.provider = String(
    f.anthropic_backup_provider?.value || form.primary.provider || 'anthropic',
  )
  form.backup.base_url = String(f.anthropic_backup_base_url?.value ?? '')
  form.backup.model = String(f.anthropic_backup_model?.value ?? '')
  form.backup.small_fast_model = String(f.anthropic_backup_small_fast_model?.value ?? '')
  form.bug_fix.provider = String(f.bug_fix_agent_provider?.value || 'deepseek')
  form.bug_fix.base_url = String(f.bug_fix_agent_base_url?.value || '')
  form.bug_fix.model = String(f.bug_fix_agent_model?.value || '')
  form.bug_fix.small_fast_model = String(f.bug_fix_agent_small_fast_model?.value || '')
  form.bug_fix.api_key = ''
  bugFixKeySet.value = Boolean(f.bug_fix_agent_api_key?.is_set)
  form.bug_fix_agent_max_tokens = Number(f.bug_fix_agent_max_tokens?.value ?? 16384)
  form.bug_fix_agent_max_turns = Number(f.bug_fix_agent_max_turns?.value ?? 150)
  form.bug_fix_agent_request_timeout_seconds = Number(f.bug_fix_agent_request_timeout_seconds?.value ?? 3600)
  const routerNum = (key: string, fallback: number) => Number(f[key]?.value ?? fallback)
  form.model_router_enabled = Boolean(f.model_router_enabled?.value ?? true)
  form.model_router_first_token_deadline_ms = routerNum('model_router_first_token_deadline_ms', 20000)
  form.model_router_slow_ttft_ms = routerNum('model_router_slow_ttft_ms', 6000)
  form.model_router_window_size = routerNum('model_router_window_size', 8)
  form.model_router_trip_threshold = routerNum('model_router_trip_threshold', 4)
  form.model_router_min_samples = routerNum('model_router_min_samples', 4)
  form.model_router_hard_failure_trip = routerNum('model_router_hard_failure_trip', 2)
  form.model_router_cooldown_seconds = routerNum('model_router_cooldown_seconds', 120)
  form.model_router_sample_ttl_seconds = routerNum('model_router_sample_ttl_seconds', 900)
  form.ocr_enabled = Boolean(f.ocr_enabled?.value ?? true)
  form.ocr_base_url = String(f.ocr_base_url?.value ?? '')
  form.ocr_model = String(f.ocr_model?.value ?? '')
  form.ocr_provider = String(f.ocr_provider?.value ?? '')
  // Reset secret inputs — only their "is set" state is known.
  form.primary.api_key = ''
  form.primary.api_keys = ''
  form.backup.api_key = ''
  form.backup.api_keys = ''
  form.ocr_api_key = ''
  anthropicKeyCount.value = Number(f.anthropic_api_keys?.count || (f.anthropic_api_key?.is_set ? 1 : 0))
  anthropicKeySet.value = Boolean(f.anthropic_api_keys?.is_set || f.anthropic_api_key?.is_set)
  backupKeySet.value = Boolean(f.anthropic_backup_api_key?.is_set)
  ocrKeySet.value = Boolean(f.ocr_api_key?.is_set)
}

const fetchModelSettings = async () => {
  if (!isAuthenticated.value) return
  loadingSettings.value = true
  try {
    const resp = await adminApi.getModelSettings()
    if (!resp?.success || !resp.data) {
      throw new Error(resp?.message || t('admin.modelSettings.loadFail'))
    }
    populateForm(resp.data)
  } catch (err: any) {
    appStore.showNotification({
      title: t('admin.modelSettings.loadFail'),
      message: parseErrorMessage(err),
      type: 'error',
    })
  } finally {
    loadingSettings.value = false
  }
}

const handleSaveSettings = async () => {
  savingSettings.value = true
  try {
    const payload: UpdateModelSettingsPayload = {
      bug_fix_agent_provider: form.bug_fix.provider,
      bug_fix_agent_base_url: form.bug_fix.base_url.trim(),
      bug_fix_agent_model: form.bug_fix.model.trim(),
      bug_fix_agent_small_fast_model: form.bug_fix.small_fast_model.trim(),
      bug_fix_agent_max_tokens: Number(form.bug_fix_agent_max_tokens),
      bug_fix_agent_max_turns: Number(form.bug_fix_agent_max_turns),
      bug_fix_agent_request_timeout_seconds: Number(form.bug_fix_agent_request_timeout_seconds),
      anthropic_provider: form.primary.provider,
      anthropic_base_url: form.primary.base_url.trim(),
      anthropic_model: form.primary.model.trim(),
      anthropic_small_fast_model: form.primary.small_fast_model.trim(),
      anthropic_max_tokens: Number(form.anthropic_max_tokens),
      anthropic_backup_enabled: form.anthropic_backup_enabled,
      anthropic_backup_provider: form.backup.provider,
      anthropic_backup_base_url: form.backup.base_url.trim(),
      anthropic_backup_model: form.backup.model.trim(),
      anthropic_backup_small_fast_model: form.backup.small_fast_model.trim(),
      model_router_enabled: form.model_router_enabled,
      model_router_first_token_deadline_ms: Number(form.model_router_first_token_deadline_ms),
      model_router_slow_ttft_ms: Number(form.model_router_slow_ttft_ms),
      model_router_window_size: Number(form.model_router_window_size),
      model_router_trip_threshold: Number(form.model_router_trip_threshold),
      model_router_min_samples: Number(form.model_router_min_samples),
      model_router_hard_failure_trip: Number(form.model_router_hard_failure_trip),
      model_router_cooldown_seconds: Number(form.model_router_cooldown_seconds),
      model_router_sample_ttl_seconds: Number(form.model_router_sample_ttl_seconds),
      ocr_enabled: form.ocr_enabled,
      ocr_base_url: form.ocr_base_url.trim(),
      ocr_model: form.ocr_model.trim(),
      ocr_provider: form.ocr_provider.trim(),
    }
    // Only send secrets when the admin typed a new value; blank keeps the old.
    const primaryKeys = parseKeyPool(form.primary.api_keys)
    if (primaryKeys.length) payload.anthropic_api_keys = primaryKeys
    if (form.bug_fix.api_key.trim()) payload.bug_fix_agent_api_key = form.bug_fix.api_key.trim()
    if (form.backup.api_key.trim()) payload.anthropic_backup_api_key = form.backup.api_key.trim()
    if (form.ocr_api_key.trim()) payload.ocr_api_key = form.ocr_api_key.trim()

    const resp = await adminApi.updateModelSettings(payload)
    if (!resp?.success || !resp.data) {
      throw new Error(resp?.message || t('admin.modelSettings.saveFail'))
    }
    populateForm(resp.data)
    appStore.showNotification({ title: t('admin.modelSettings.saved'), type: 'success' })
  } catch (err: any) {
    appStore.showNotification({
      title: t('admin.modelSettings.saveFail'),
      message: parseErrorMessage(err),
      type: 'error',
    })
  } finally {
    savingSettings.value = false
  }
}

const handleResetSettings = async () => {
  if (!window.confirm(t('admin.modelSettings.resetConfirm'))) return
  resettingSettings.value = true
  try {
    const resp = await adminApi.resetModelSettings()
    if (!resp?.success || !resp.data) {
      throw new Error(resp?.message || t('admin.modelSettings.resetFail'))
    }
    populateForm(resp.data)
    // The form now describes a different upstream; any earlier probe is stale.
    testResults.anthropic = null
    testResults.anthropic_backup = null
    testResults.bug_fix = null
    testResults.ocr = null
    appStore.showNotification({ title: t('admin.modelSettings.resetDone'), type: 'success' })
  } catch (err: any) {
    appStore.showNotification({
      title: t('admin.modelSettings.resetFail'),
      message: parseErrorMessage(err),
      type: 'error',
    })
  } finally {
    resettingSettings.value = false
  }
}

const navVisible = computed(() => appStore.adminSidebarVisible)
const activeNavKey = computed(() => resolveAdminNavKey(route.path))

const parseErrorMessage = (err: any) => {
  if (err?.response?.data?.detail) return err.response.data.detail
  if (err?.response?.data?.message) return err.response.data.message
  if (err?.message) return err.message
  return t('admin.parseError')
}

const persistToken = (token: string) => {
  adminToken.set(token)
}

const clearAuth = () => {
  adminToken.clear()
  isAuthenticated.value = false
  authForm.password = ''
}

const handleNavClick = (item: AdminNavItem) => {
  if (item.path && route.path !== item.path) router.push(item.path)
}

const toggleNavVisibility = () => {
  appStore.toggleAdminSidebar()
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
    persistToken(resp.data.token)
    isAuthenticated.value = true
    appStore.showNotification({ title: t('admin.loginSuccessTitle'), message: t('admin.loginSuccessMsg', { username: resp.data.username }), type: 'success' })
    fetchModelSettings()
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
      fetchModelSettings()
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
  <div class="admin-console admin-users-page">
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
            <p class="admin-subtitle">{{ t('admin.modelSettings.subtitle') }}</p>
          </div>
        </div>
        <div class="admin-topbar-right">
          <ThemeToggle class="admin-theme-toggle" />
          <span class="px-3 py-1 text-xs font-semibold rounded-full bg-slate-700 text-slate-100">
            {{ isAuthenticated ? t('admin.modelSettings.badge') : t('admin.badgeNotLoggedIn') }}
          </span>
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
      <section v-if="!isAuthenticated" class="admin-login-wrap">
        <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6">
          <div class="flex items-center justify-between mb-4">
            <div>
              <h2 class="text-lg font-semibold text-slate-900">{{ t('admin.loginCardTitle') }}</h2>
              <p class="text-sm text-slate-500">{{ t('admin.loginCardDesc') }}</p>
            </div>
          </div>
          <form class="space-y-4 max-w-md" @submit.prevent="handleLogin">
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

      <section v-else class="space-y-4 admin-model-form">
        <AgentRuntimeCard @routing-change="magpieEnabled = $event" />
        <div v-if="loadingSettings" class="bg-white rounded-2xl shadow-sm border border-slate-200 p-6 text-sm text-slate-500">
          {{ t('admin.modelSettings.loading') }}
        </div>

        <form v-else class="space-y-4" @submit.prevent="handleSaveSettings">
          <!-- Anthropic 主力模型 -->
          <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-5">
            <div class="mb-4">
              <h2 class="flex flex-wrap items-center gap-2 text-lg font-semibold text-slate-900">
                {{ t('admin.modelSettings.anthropicSectionTitle') }}
                <span v-if="magpieEnabled" class="ms-badge ms-badge--override">{{ t('admin.modelSettings.magpieManagedBadge') }}</span>
              </h2>
              <div v-if="magpieEnabled" class="ms-magpie-notice" role="status">
                <Info :size="20" class="shrink-0 mt-0.5" aria-hidden="true" />
                <div class="min-w-0">
                  <p class="font-semibold">{{ t('admin.modelSettings.magpieNoticeTitle') }}</p>
                  <p class="mt-1">{{ t('admin.modelSettings.magpieNoticeBody') }}</p>
                  <p class="mt-2 text-xs">{{ t('admin.modelSettings.magpieNoticeScope') }}</p>
                </div>
              </div>
              <p class="text-sm text-slate-500">{{ t('admin.modelSettings.anthropicSectionDesc') }}</p>
            </div>

            <AnthropicEndpointCard
              slot-name="primary"
              :form="form.primary"
              :fields="settingsData?.fields"
              :provider-options="providerOptions"
              :profiles="providerProfiles"
              :key-set="anthropicKeySet"
              :key-count="anthropicKeyCount"
              :testing="testing.anthropic"
              :test-result="testResults.anthropic"
              @test="runTest('anthropic')"
            >
              <!-- Token budget is a workload property, not an endpoint one, so
                   both slots share it and it lives only on the primary card. -->
              <template #extra>
                <label class="block text-sm text-slate-700">
                  <span class="flex items-center gap-2">
                    {{ t('admin.modelSettings.maxTokensLabel') }}
                    <span class="ms-badge" :class="`ms-badge--${sourceOf('anthropic_max_tokens')}`">{{ sourceLabel('anthropic_max_tokens') }}</span>
                  </span>
                  <input
                    v-model.number="form.anthropic_max_tokens"
                    type="number"
                    min="1"
                    max="200000"
                    class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
                  />
                  <p class="text-xs text-slate-500 mt-1">{{ t('admin.modelSettings.maxTokensHint') }}</p>
                </label>
              </template>
            </AnthropicEndpointCard>
          </div>

          <!-- Anthropic 备用模型（故障转移） -->
          <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-5">
            <div class="mb-4">
              <h2 class="text-lg font-semibold text-slate-900">{{ t('admin.modelSettings.backupSectionTitle') }}</h2>
              <p :class="magpieEnabled ? 'ms-magpie-note' : 'text-sm text-slate-500'">{{ t(magpieEnabled ? 'admin.modelSettings.magpieLegacyHint' : 'admin.modelSettings.backupSectionDesc') }}</p>
            </div>

            <label class="flex items-center gap-2 text-sm text-slate-700 mb-1">
              <input v-model="form.anthropic_backup_enabled" type="checkbox" class="h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500" />
              <span>{{ t('admin.modelSettings.backupEnabledLabel') }}</span>
              <span class="ms-badge" :class="`ms-badge--${sourceOf('anthropic_backup_enabled')}`">{{ sourceLabel('anthropic_backup_enabled') }}</span>
            </label>
            <p v-if="!magpieEnabled" class="text-xs text-slate-500 mb-4">{{ t('admin.modelSettings.backupEnabledHint') }}</p>

            <!-- Live routing state: which endpoint is answering right now. -->
            <div v-if="router$ && magpieEnabled === false" class="ms-route" :class="onBackup ? 'is-backup' : 'is-primary'">
              <component :is="onBackup ? AlertTriangle : CheckCircle2" :size="16" class="ms-test-icon" />
              <div class="min-w-0">
                <p class="ms-test-title">
                  {{
                    onBackup
                      ? t('admin.modelSettings.routeOnBackup', { since: backupSince })
                      : t('admin.modelSettings.routeOnPrimary')
                  }}
                </p>
                <p class="ms-test-meta">
                  {{ t('admin.modelSettings.routeMode', {
                    mode: router$.enabled
                      ? t('admin.modelSettings.routeModeActive')
                      : t('admin.modelSettings.routeModeObserve'),
                  }) }}
                  <template v-if="primaryWindow">
                    · {{ t('admin.modelSettings.routeWindow', { ratio: primaryWindow }) }}
                  </template>
                </p>
              </div>
            </div>

            <AnthropicEndpointCard
              slot-name="backup"
              :form="form.backup"
              :fields="settingsData?.fields"
              :provider-options="providerOptions"
              :profiles="providerProfiles"
              :key-set="backupKeySet"
              :testing="testing.anthropic_backup"
              :test-result="testResults.anthropic_backup"
              :inactive="!form.anthropic_backup_enabled"
              @test="runTest('anthropic_backup')"
            />

            <p v-if="!magpieEnabled" class="text-xs text-slate-500 mt-3">{{ t('admin.modelSettings.backupRoutingNote') }}</p>
          </div>

          <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-5">
            <div class="mb-4">
              <h2 class="text-lg font-semibold text-slate-900">{{ t('admin.modelSettings.bugFixTitle') }}</h2>
              <p :class="magpieEnabled ? 'ms-magpie-note' : 'text-sm text-slate-500'">{{ t(magpieEnabled ? 'admin.modelSettings.magpieBugFixHint' : 'admin.modelSettings.bugFixDesc') }}</p>
            </div>
            <AnthropicEndpointCard slot-name="bug_fix" :form="form.bug_fix"
              :fields="settingsData?.fields" :provider-options="providerOptions" :profiles="providerProfiles"
              :key-set="bugFixKeySet" :testing="testing.bug_fix" :test-result="testResults.bug_fix"
              @test="runTest('bug_fix')" />
            <div class="grid gap-4 lg:grid-cols-3 mt-4">
              <label class="block text-sm text-slate-700">{{ t('admin.modelSettings.bugFixTokens') }}
                <input v-model.number="form.bug_fix_agent_max_tokens" type="number" min="1" max="200000" class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none" />
              </label>
              <label class="block text-sm text-slate-700">{{ t('admin.modelSettings.bugFixTurns') }}
                <input v-model.number="form.bug_fix_agent_max_turns" type="number" min="1" max="1000" class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none" />
              </label>
              <label class="block text-sm text-slate-700">{{ t('admin.modelSettings.bugFixTimeout') }}
                <input v-model.number="form.bug_fix_agent_request_timeout_seconds" type="number" min="30" max="14400" class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none" />
              </label>
            </div>
          </div>

          <!-- 路由策略：何时离开主力、何时切回 -->
          <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-5">
            <div class="mb-4">
              <h2 class="text-lg font-semibold text-slate-900">{{ t('admin.modelSettings.routerSectionTitle') }}</h2>
              <p :class="magpieEnabled ? 'ms-magpie-note' : 'text-sm text-slate-500'">{{ t(magpieEnabled ? 'admin.modelSettings.magpieLegacyHint' : 'admin.modelSettings.routerSectionDesc') }}</p>
            </div>

            <label class="flex items-center gap-2 text-sm text-slate-700 mb-1">
              <input v-model="form.model_router_enabled" type="checkbox" class="h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500" />
              <span>{{ t('admin.modelSettings.routerEnabledLabel') }}</span>
              <span class="ms-badge" :class="`ms-badge--${sourceOf('model_router_enabled')}`">{{ sourceLabel('model_router_enabled') }}</span>
            </label>
            <p v-if="!magpieEnabled" class="text-xs text-slate-500 mb-4">{{ t('admin.modelSettings.routerEnabledHint') }}</p>

            <div class="grid gap-4 lg:grid-cols-2" :class="{ 'opacity-50 pointer-events-none': !form.model_router_enabled }">
              <label
                v-for="knob in ROUTER_KNOBS"
                :key="knob.key"
                class="block text-sm text-slate-700"
              >
                <span class="flex items-center gap-2">
                  {{ t(`admin.modelSettings.${knob.label}`) }}
                  <span class="ms-badge" :class="`ms-badge--${sourceOf(knob.key)}`">{{ sourceLabel(knob.key) }}</span>
                </span>
                <input
                  v-model.number="form[knob.key]"
                  type="number"
                  :min="knob.min"
                  :max="knob.max"
                  class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
                />
                <p class="text-xs text-slate-500 mt-1">{{ t(`admin.modelSettings.${knob.hint}`) }}</p>
              </label>
            </div>
          </div>

          <!-- OCR / 视觉模型 -->
          <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-5">
            <div class="mb-4">
              <h2 class="text-lg font-semibold text-slate-900">{{ t('admin.modelSettings.ocrSectionTitle') }}</h2>
              <p class="text-sm text-slate-500">{{ t('admin.modelSettings.ocrSectionDesc') }}</p>
            </div>

            <label class="flex items-center gap-2 text-sm text-slate-700 mb-4">
              <input v-model="form.ocr_enabled" type="checkbox" class="h-4 w-4 rounded border-slate-300 text-cyan-600 focus:ring-cyan-500" />
              <span>{{ t('admin.modelSettings.ocrEnabledLabel') }}</span>
              <span class="ms-badge" :class="`ms-badge--${sourceOf('ocr_enabled')}`">{{ sourceLabel('ocr_enabled') }}</span>
            </label>

            <div class="grid gap-4 lg:grid-cols-2" :class="{ 'opacity-50 pointer-events-none': !form.ocr_enabled }">
              <label class="block text-sm text-slate-700">
                <span class="flex items-center gap-2">
                  {{ t('admin.modelSettings.ocrApiKeyLabel') }}
                  <span class="ms-badge" :class="`ms-badge--${sourceOf('ocr_api_key')}`">{{ sourceLabel('ocr_api_key') }}</span>
                </span>
                <input
                  v-model="form.ocr_api_key"
                  type="password"
                  autocomplete="off"
                  class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
                  :placeholder="ocrKeySet ? t('admin.modelSettings.apiKeySetPlaceholder') : t('admin.modelSettings.apiKeyUnsetPlaceholder')"
                />
                <p class="text-xs text-slate-500 mt-1">{{ t('admin.modelSettings.ocrApiKeyHint') }}</p>
              </label>

              <label class="block text-sm text-slate-700">
                <span class="flex items-center gap-2">
                  {{ t('admin.modelSettings.ocrModelLabel') }}
                  <span class="ms-badge" :class="`ms-badge--${sourceOf('ocr_model')}`">{{ sourceLabel('ocr_model') }}</span>
                </span>
                <input
                  v-model="form.ocr_model"
                  type="text"
                  spellcheck="false"
                  class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
                  :placeholder="t('admin.modelSettings.ocrModelPlaceholder')"
                />
                <p class="text-xs text-slate-500 mt-1">{{ t('admin.modelSettings.ocrModelHint') }}</p>
              </label>

              <label class="block text-sm text-slate-700">
                <span class="flex items-center gap-2">
                  {{ t('admin.modelSettings.ocrBaseUrlLabel') }}
                  <span class="ms-badge" :class="`ms-badge--${sourceOf('ocr_base_url')}`">{{ sourceLabel('ocr_base_url') }}</span>
                </span>
                <input
                  v-model="form.ocr_base_url"
                  type="text"
                  spellcheck="false"
                  class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
                  :placeholder="t('admin.modelSettings.ocrBaseUrlPlaceholder')"
                />
                <p class="text-xs text-slate-500 mt-1">{{ t('admin.modelSettings.ocrBaseUrlHint') }}</p>
              </label>

              <label class="block text-sm text-slate-700">
                <span class="flex items-center gap-2">
                  {{ t('admin.modelSettings.ocrProviderLabel') }}
                  <span class="ms-badge" :class="`ms-badge--${sourceOf('ocr_provider')}`">{{ sourceLabel('ocr_provider') }}</span>
                </span>
                <input
                  v-model="form.ocr_provider"
                  type="text"
                  spellcheck="false"
                  class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
                  :placeholder="t('admin.modelSettings.ocrProviderPlaceholder')"
                />
                <p class="text-xs text-slate-500 mt-1">{{ t('admin.modelSettings.ocrProviderHint') }}</p>
              </label>
            </div>

            <div class="mt-4 pt-4 border-t border-slate-200" :class="{ 'opacity-50 pointer-events-none': !form.ocr_enabled }">
              <div class="flex flex-wrap items-center gap-3">
                <button
                  type="button"
                  class="ms-test-btn"
                  :disabled="testing.ocr || !form.ocr_enabled"
                  @click="runTest('ocr')"
                >
                  <PlugZap :size="15" />
                  {{ testing.ocr ? t('admin.modelSettings.testingBtn') : t('admin.modelSettings.testOcrBtn') }}
                </button>
                <span class="text-xs text-slate-500">{{ t('admin.modelSettings.testOcrHint') }}</span>
              </div>

              <div
                v-if="testResults.ocr"
                class="ms-test-result"
                :class="testResults.ocr.ok ? 'is-ok' : 'is-fail'"
              >
                <component
                  :is="testResults.ocr.ok ? CheckCircle2 : XCircle"
                  :size="16"
                  class="ms-test-icon"
                />
                <div class="min-w-0">
                  <p class="ms-test-title">
                    {{
                      testResults.ocr.ok
                        ? t('admin.modelSettings.testOk', { ms: testResults.ocr.latency_ms ?? 0 })
                        : t('admin.modelSettings.testFailed')
                    }}
                  </p>
                  <p class="ms-test-meta">{{ testResults.ocr.model }} · {{ testResults.ocr.base_url }}</p>
                  <p v-if="testResults.ocr.ok && testResults.ocr.reply" class="ms-test-meta">
                    {{ t('admin.modelSettings.testReply') }}: {{ testResults.ocr.reply }}
                  </p>
                  <p v-if="!testResults.ocr.ok" class="ms-test-meta">
                    [{{ testResults.ocr.error_kind }}] {{ testResults.ocr.detail }}
                  </p>
                </div>
              </div>
            </div>

            <p class="text-xs text-slate-500 mt-3">{{ t('admin.modelSettings.ocrComplianceNote') }}</p>
          </div>

          <!-- Notes + Actions -->
          <div class="bg-white rounded-2xl shadow-sm border border-slate-200 p-5">
            <div class="text-sm text-slate-500 space-y-1 mb-4">
              <p>{{ t('admin.modelSettings.effectiveNote') }}</p>
              <p>{{ t('admin.modelSettings.envFallbackNote') }}</p>
              <p>{{ t('admin.modelSettings.providerCapabilityNote') }}</p>
            </div>
            <div class="flex flex-wrap items-center gap-3">
              <button
                type="submit"
                class="inline-flex items-center gap-2 px-4 py-2 bg-cyan-600 text-white rounded-lg text-sm font-semibold hover:bg-cyan-700 transition disabled:opacity-60"
                :disabled="savingSettings || resettingSettings"
              >
                <Save :size="15" />
                {{ savingSettings ? t('admin.modelSettings.savingBtn') : t('admin.modelSettings.saveBtn') }}
              </button>
              <button
                type="button"
                class="inline-flex items-center gap-2 px-4 py-2 border border-slate-300 text-slate-700 rounded-lg text-sm font-semibold hover:bg-slate-50 transition disabled:opacity-60"
                :disabled="savingSettings || resettingSettings"
                @click="handleResetSettings"
              >
                <RotateCcw :size="15" />
                {{ resettingSettings ? t('admin.modelSettings.resettingBtn') : t('admin.modelSettings.resetBtn') }}
              </button>
            </div>
          </div>
        </form>
      </section>
    </main>
  </div>
</template>

<style scoped src="@/styles/model-settings-fields.css"></style>

<style scoped>
.admin-console {
  --admin-topbar-height: 72px;
  --admin-sidebar-width: 280px;
  min-height: 100vh;
  background: linear-gradient(180deg, #f1f5f9 0%, #e2e8f0 100%);
}

.admin-topbar {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  width: 100%;
  height: var(--admin-topbar-height);
  z-index: 70;
  background: rgba(15, 23, 42, 0.96);
  border-bottom: 1px solid rgba(148, 163, 184, 0.3);
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
  border: 1px solid rgba(148, 163, 184, 0.35);
  border-radius: 0.625rem;
  color: #f8fafc;
  background: rgba(51, 65, 85, 0.6);
}

.admin-icon-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.admin-title {
  color: #f8fafc;
  font-size: 0.95rem;
  font-weight: 700;
  line-height: 1.1;
}

.admin-subtitle {
  color: #94a3b8;
  font-size: 0.75rem;
}

.admin-topbar-right {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.admin-logout-btn {
  border: 1px solid rgba(148, 163, 184, 0.35);
  border-radius: 0.55rem;
  color: #e2e8f0;
  background: rgba(51, 65, 85, 0.45);
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
  background: #0f172a;
  border-right: 1px solid rgba(148, 163, 184, 0.25);
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
  border: 1px solid rgba(100, 116, 139, 0.45);
  color: #cbd5e1;
  background: rgba(30, 41, 59, 0.45);
}

.admin-side-nav-item.is-active {
  color: #0f172a;
  background: #22d3ee;
  border-color: #22d3ee;
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
  max-width: 720px;
  margin: 1.25rem auto 0;
}

.admin-sidebar-backdrop {
  display: none;
}

.admin-model-form :deep(code) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.ms-magpie-notice,
.ms-magpie-note {
  color: var(--admin-ink, #0f172a);
  background: var(--admin-accent-soft-bg, #ecfeff);
  border-left: 3px solid var(--admin-accent-soft-ink, #0e7490);
  border-radius: 0.5rem;
  padding: 0.85rem 1rem;
  font-size: 0.875rem;
  line-height: 1.6;
}

.ms-magpie-notice {
  display: flex;
  align-items: flex-start;
  gap: 0.65rem;
  margin: 0.75rem 0;
}

.ms-magpie-note {
  margin-top: 0.5rem;
}

@media (max-width: 768px) {
  .admin-console {
    --admin-sidebar-width: min(84vw, 320px);
  }
  .admin-main.is-sidebar-hidden {
    padding-left: 1rem;
  }
  .admin-sidebar-backdrop {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 55;
    background: rgba(15, 23, 42, 0.45);
    border: 0;
  }
}
</style>
