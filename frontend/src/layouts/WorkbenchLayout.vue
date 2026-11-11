<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { MessageCircleQuestion, Save, Search, ShieldCheck, UserRound } from 'lucide-vue-next'
import { useAppStore } from '@/stores/app'
import { useUserStore } from '@/stores/user'
import { useChatSessionStore } from '@/stores/chatSession'
import { useConversationRunsStore } from '@/stores/conversationRuns'
import { useAnnouncementStore } from '@/stores/announcement'
import { userApi } from '@/api/user'
import { adminToken } from '@/api/admin'
import { setAdminIdentity } from '@/composables/useAdminScope'
import type { ChatSessionSummary, UserProfileRole } from '@/types'
import {
  firstRegistrationErrorField,
  mapRegistrationServerError,
  validateRegistration,
  type RegistrationErrors,
  type RegistrationInputField,
  type RegistrationValidationMessages,
} from '@/utils/registrationValidation'
import brandIcon from '@/assets/icon.png'
import SystemAnnouncementDialog from '@/components/SystemAnnouncementDialog.vue'
import ThemeToggle from '@/components/ThemeToggle.vue'
import ConversationSearchDialog from '@/components/ConversationSearchDialog.vue'

const route = useRoute()
const router = useRouter()
const { t } = useI18n()
const appStore = useAppStore()
const userStore = useUserStore()
const sessionStore = useChatSessionStore()
const runsStore = useConversationRunsStore()
const announcementStore = useAnnouncementStore()

const profileRoleValues = ['developer', 'tester', 'product', 'ops', 'other'] as const

/**
 * Embedded mode: when the workbench is rendered inside the Raven desktop client
 * (in an <iframe> on the Files tab), the client already provides its own
 * left-hand navigation. Rendering the workbench sidebar as well would produce a
 * duplicate sidebar inside the embedded page, so we hide it and let the routed
 * content fill the full width.
 *
 * The check is evaluated once on load — whether we are framed cannot change for
 * the lifetime of the page — so it stays stable across in-frame router
 * navigation (e.g. opening a log/package detail):
 *   - explicit `?embed=1` query flag (the client passes this; also handy for
 *     direct browser preview)
 *   - otherwise any iframe embedding (`window.self !== window.top`)
 */
const detectEmbedded = (): boolean => {
  if (typeof window === 'undefined') return false
  try {
    const flag = new URLSearchParams(window.location.search).get('embed')
    if (flag === '1' || flag === 'true') return true
  } catch {
    /* ignore malformed query strings */
  }
  try {
    return window.self !== window.top
  } catch {
    // Cross-origin frames throw on access — being unable to reach top means framed.
    return true
  }
}
const isEmbedded = detectEmbedded()

/**
 * Union of backend-reported running sessions (``run_status === 'running'``)
 * and the local optimistic running overlay maintained by the run store. The
 * overlay covers the gap between sendMessage and the next sessions list
 * refresh.
 */
const runningSessionIds = computed(() => {
  const ids = new Set<string>()
  for (const s of sessionStore.sessions) {
    const localState = runsStore.bySession[s.id]
    const locallyFinished =
      localState &&
      !localState.isSending &&
      ['succeeded', 'failed', 'cancelled', 'stale'].includes(localState.runStatus)
    if (s.run_status === 'running' && !locallyFinished) ids.add(s.id)
  }
  for (const id of runsStore.localRunningSessionIds) ids.add(id)
  return ids
})

const isSessionRunning = (id: string) => runningSessionIds.value.has(id)

let pollHandle: number | null = null
const schedulePoll = () => {
  if (typeof window === 'undefined') return
  if (pollHandle !== null) return
  pollHandle = window.setInterval(() => {
    // Only refresh when at least one session is in running state — avoids
    // unnecessary polling for idle users.
    if (runningSessionIds.value.size === 0) return
    // Skip when the tab is hidden or a request is still in flight (slow
    // networks) — prevents request pile-up.
    if (document.visibilityState === 'hidden') return
    if (sessionStore.loading || sessionStore.refreshing) return
    sessionStore.load({ background: true }).catch(() => { /* swallow; overlay still works */ })
  }, 5000)
}
const stopPoll = () => {
  if (pollHandle !== null && typeof window !== 'undefined') {
    window.clearInterval(pollHandle)
    pollHandle = null
  }
}

const showUserMenu = ref(false)
const showSearchBox = ref(false)
const hoverSessionId = ref<string | null>(null)
const openRowMenuId = ref<string | null>(null)

// 当前语言来自 app store（启动期由 localStorage/浏览器/profile 解析）。
const activeLocale = computed(() => appStore.locale)
const setLanguage = (next: 'zh' | 'en') => {
  if (appStore.locale === next) return
  // setLocale 负责更新 store + localStorage + vue-i18n/Element Plus，
  // 已登录时同步 PATCH profile。
  appStore.setLocale(next)
}

const editingSessionId = ref<string | null>(null)
const editingSessionTitle = ref('')
const renameInputRef = ref<HTMLInputElement | null>(null)

const showLoginModal = ref(false)
const authMode = ref<'login' | 'register'>('login')
const loginForm = reactive({
  username: '',
  password: '',
  displayName: '',
  email: '',
  confirmPassword: '',
})
// The admin's note for a disabled account: a toast is too short-lived for
// instructions the user has to act on, so it stays pinned inside the modal.
const loginNotice = ref('')
const registerErrors = reactive<Record<RegistrationInputField | 'form', string>>({
  username: '',
  displayName: '',
  email: '',
  password: '',
  confirmPassword: '',
  form: '',
})
const usernameInputRef = ref<HTMLInputElement | null>(null)
const displayNameInputRef = ref<HTMLInputElement | null>(null)
const emailInputRef = ref<HTMLInputElement | null>(null)
const passwordInputRef = ref<HTMLInputElement | null>(null)
const confirmPasswordInputRef = ref<HTMLInputElement | null>(null)
const isLoggingIn = ref(false)
const showSettingsModal = ref(false)
const activeSettingsSection = ref<'account' | 'agent'>('account')
const isSavingProfile = ref(false)
const profileForm = reactive({
  displayName: '',
  email: '',
  profileRole: 'developer' as UserProfileRole,
  clarificationEnabled: true,
  clarificationMaxRounds: 5,
})

const userMenuRef = ref<HTMLElement | null>(null)
const userButtonRef = ref<HTMLElement | null>(null)

const isLoggedIn = computed(() => userStore.isAuthenticated)
const currentUserName = computed(() =>
  userStore.profile?.display_name || userStore.profile?.username || t('workbench.userFallback')
)
const currentUserEmail = computed(() => userStore.profile?.email || '')
const currentUserRole = computed(() => (userStore.profile?.role || 'user').toString().toLowerCase())
const currentProfileRole = computed(() => (userStore.profile?.profile_role || 'developer').toString())
const isAdmin = computed(() => isLoggedIn.value && currentUserRole.value === 'admin')
const userInitial = computed(() => (currentUserName.value || 'U').slice(0, 2).toUpperCase())
const currentUserStatusText = computed(() =>
  currentUserEmail.value || (isLoggedIn.value ? t('workbench.loggedIn') : t('workbench.notLoggedIn'))
)
const profileRoleOptions = computed(() => {
  const items: { value: UserProfileRole; label: string }[] = profileRoleValues.map((value) => ({
    value,
    label: t(`workbench.settingsPanel.roles.${value}`),
  }))
  const current = currentProfileRole.value
  if (current && !items.some((item) => item.value === current)) {
    items.push({ value: current, label: current })
  }
  return items
})
const currentProfileRoleLabel = computed(() => {
  const current = currentProfileRole.value
  return profileRoleOptions.value.find((item) => item.value === current)?.label || current
})

const openAuthModal = (mode: 'login' | 'register' = 'login') => {
  authMode.value = mode
  showLoginModal.value = true
}

const syncProfileForm = () => {
  profileForm.displayName = userStore.profile?.display_name || ''
  profileForm.email = userStore.profile?.email || ''
  profileForm.profileRole = (userStore.profile?.profile_role || 'developer') as UserProfileRole
  profileForm.clarificationEnabled = userStore.profile?.clarification_enabled ?? true
  profileForm.clarificationMaxRounds = userStore.profile?.clarification_max_rounds ?? 5
}

const openSettingsModal = () => {
  showUserMenu.value = false
  if (!isLoggedIn.value) {
    appStore.showNotification({ title: t('workbench.settingsPanel.loginRequired'), type: 'warning' })
    openAuthModal('login')
    return
  }
  syncProfileForm()
  activeSettingsSection.value = 'account'
  showSettingsModal.value = true
}

const closeSettingsModal = () => {
  showSettingsModal.value = false
}

const handleSaveProfile = async () => {
  if (!isLoggedIn.value) {
    appStore.showNotification({ title: t('workbench.settingsPanel.loginRequired'), type: 'warning' })
    openAuthModal('login')
    return
  }
  isSavingProfile.value = true
  try {
    const resp = await userApi.updateProfile({
      display_name: profileForm.displayName.trim() || null,
      email: profileForm.email.trim() || null,
      profile_role: profileForm.profileRole || 'developer',
      clarification_enabled: profileForm.clarificationEnabled,
      clarification_max_rounds: Number(profileForm.clarificationMaxRounds) || 5,
    })
    if (!resp?.success || !resp.data) {
      throw new Error(resp?.message || t('workbench.settingsPanel.saveFailed'))
    }
    userStore.setProfile(resp.data)
    syncProfileForm()
    appStore.showNotification({ title: t('workbench.settingsPanel.saved'), type: 'success' })
  } catch (error: any) {
    appStore.showNotification({
      title: t('workbench.settingsPanel.saveFailed'),
      message: parseAuthError(error, t('workbench.notifications.tryAgainLater')),
      type: 'error',
    })
  } finally {
    isSavingProfile.value = false
  }
}

const goToAdminConsole = () => {
  showUserMenu.value = false
  // Enter as the current user and let the router apply their project scope.
  adminToken.clear()
  setAdminIdentity(userStore.adminIdentity)
  router.push('/admin')
}

const goToBugFixes = () => {
  showUserMenu.value = false
  router.push('/bug-fixes')
}

const goToDownload = () => {
  showUserMenu.value = false
  router.push({ name: 'Download' })
}

const navItems = computed(() => ([
  { id: 'logs',    label: t('navbar.logs'),    to: '/logs',          icon: 'logs', activeNames: ['Logs', 'LogDetail'] },
  { id: 'devices', label: t('navbar.devices'), to: '/devices',       icon: 'device', activeNames: ['DeviceList', 'DeviceDetail'] },
  { id: 'pkgs',    label: t('navbar.raven'),   to: '/raven-manager', icon: 'box' },
]))

const isNavItemActive = (item: { activeNames?: string[] }, isActive: boolean) => {
  const routeName = (route.name as string) || ''
  return isActive || Boolean(item.activeNames?.includes(routeName))
}

const isHomeRoute = computed(() =>
  route.name === 'Workbench' || route.path === '/workbench' || route.path === '/ai-chat'
)

const filteredSessions = computed(() => sessionStore.sessions)

const groupedSessions = computed(() => {
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const yesterday = today - 24 * 60 * 60 * 1000
  const weekStart = today - 7 * 24 * 60 * 60 * 1000

  const pinned: ChatSessionSummary[] = []
  const groups: { label: string; items: ChatSessionSummary[] }[] = [
    { label: t('workbench.sessionGroups.today'), items: [] },
    { label: t('workbench.sessionGroups.yesterday'), items: [] },
    { label: t('workbench.sessionGroups.thisWeek'), items: [] },
    { label: t('workbench.sessionGroups.earlier'), items: [] },
  ]

  for (const s of filteredSessions.value) {
    if (s.is_pinned) {
      pinned.push(s)
      continue
    }
    const t = s.last_message_at ? new Date(s.last_message_at).getTime() : 0
    if (t >= today) groups[0].items.push(s)
    else if (t >= yesterday) groups[1].items.push(s)
    else if (t >= weekStart) groups[2].items.push(s)
    else groups[3].items.push(s)
  }
  const ordered = pinned.length
    ? [{ label: t('workbench.sessionGroups.pinned'), items: pinned }, ...groups]
    : groups
  return ordered.filter((g) => g.items.length > 0)
})

const startRenameSession = (session: ChatSessionSummary) => {
  openRowMenuId.value = null
  editingSessionId.value = session.id
  editingSessionTitle.value = session.title || ''
  nextTick(() => renameInputRef.value?.select())
}

const commitRename = async (id: string) => {
  const title = editingSessionTitle.value.trim()
  editingSessionId.value = null
  if (!title) return
  try {
    const ok = await sessionStore.renameSession(id, title)
    if (ok) appStore.showNotification({ title: t('workbench.notifications.renamed'), type: 'success' })
  } catch {
    appStore.showNotification({ title: t('workbench.notifications.renameFailed'), type: 'error' })
  }
}

const cancelRename = () => {
  editingSessionId.value = null
}

const handleRenameKeydown = (e: KeyboardEvent, id: string) => {
  if (e.key === 'Enter') { e.preventDefault(); commitRename(id) }
  if (e.key === 'Escape') { e.preventDefault(); cancelRename() }
}

const handleClickOutside = (event: MouseEvent) => {
  const target = event.target as Node

  if (showUserMenu.value && userMenuRef.value && userButtonRef.value &&
      !userMenuRef.value.contains(target) && !userButtonRef.value.contains(target)) {
    showUserMenu.value = false
  }

  // The row menu lives inside a v-for, so `rowMenuRef` resolves to an array and
  // `.contains()` can't be used reliably. Detect outside clicks via `closest()`
  // against the menu and its trigger button instead.
  if (openRowMenuId.value) {
    const el = target instanceof Element ? target : (target as Node).parentElement
    if (!el?.closest('.rw-row-menu') && !el?.closest('.rw-row-more')) {
      openRowMenuId.value = null
    }
  }

  // Commit rename when clicking outside the inline input.
  if (editingSessionId.value) {
    const el = target instanceof Element ? target : (target as Node).parentElement
    if (!el?.closest('.rw-rename-input-wrap')) {
      commitRename(editingSessionId.value)
    }
  }
}

const openConversationSearch = () => {
  if (!isLoggedIn.value) { openAuthModal('login'); return }
  showUserMenu.value = false
  openRowMenuId.value = null
  showSearchBox.value = true
}

const handleKey = (e: KeyboardEvent) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k' && !e.altKey && !e.isComposing) {
    e.preventDefault()
    if (!showLoginModal.value && !showSettingsModal.value) openConversationSearch()
    return
  }
  if (e.key === 'Escape') {
    showSettingsModal.value = false
    showUserMenu.value = false
    openRowMenuId.value = null
  }
}

const bootstrapUser = async () => {
  await userStore.bootstrap()
  // 启动期语言解析：已登录时优先采用 profile 偏好
  appStore.initLocale()
  if (isLoggedIn.value) {
    try { await sessionStore.load() } catch { /* notification handled below */ }
    try { await announcementStore.checkPending() } catch { /* retry on next trigger */ }
  }
}

onMounted(() => {
  document.addEventListener('click', handleClickOutside)
  document.addEventListener('keydown', handleKey)
  bootstrapUser()
  schedulePoll()
})

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside)
  document.removeEventListener('keydown', handleKey)
  stopPoll()
})

watch(isLoggedIn, async (loggedIn) => {
  if (loggedIn) {
    try { await sessionStore.load() } catch {
      appStore.showNotification({ title: t('workbench.notifications.syncSessionsFailed'), type: 'error' })
    }
    try { await announcementStore.checkPending() } catch { /* retry on next trigger */ }
  } else {
    showSearchBox.value = false
    sessionStore.reset()
    announcementStore.reset()
  }
})

watch(showUserMenu, (open) => {
  if (open) userStore.refreshAdminAccess()
})

watch(() => appStore.loginModalRequest, (val) => {
  if (val.seq > 0) openAuthModal(val.mode)
})

watch(() => userStore.profile, () => {
  if (showSettingsModal.value) syncProfileForm()
})

const handleSelectSession = (session: ChatSessionSummary) => {
  openRowMenuId.value = null
  showSearchBox.value = false
  sessionStore.selectSession(session.id)
  router.push({ path: '/workbench', query: { session_id: session.id } })
}

const startNewChat = () => {
  showSearchBox.value = false
  sessionStore.startNewChat()
  if (!isHomeRoute.value || route.query.session_id) router.push('/workbench')
  if (isLoggedIn.value) {
    announcementStore.checkPending().catch(() => { /* retry on next trigger */ })
  }
}

const reloadSessions = async () => {
  try { await sessionStore.load() } catch {
    appStore.showNotification({ title: t('workbench.notifications.syncSessionsFailed'), type: 'error' })
  }
}

const togglePinSession = async (session: ChatSessionSummary) => {
  openRowMenuId.value = null
  const wasPinned = Boolean(session.is_pinned)
  try {
    const ok = await sessionStore.togglePin(session.id)
    if (ok) {
      appStore.showNotification({
        title: wasPinned ? t('workbench.notifications.unpinned') : t('workbench.notifications.pinned'),
        type: 'success',
      })
    }
  } catch (error) {
    console.error('Pin operation failed', error)
    appStore.showNotification({ title: t('workbench.notifications.operationFailed'), type: 'error' })
  }
}

const deleteSession = async (id: string) => {
  openRowMenuId.value = null
  const confirmed = window.confirm(t('workbench.confirm.deleteSession'))
  if (!confirmed) return
  try {
    await sessionStore.removeSession(id)
    runsStore.clearSession(id)
    appStore.showNotification({ title: t('workbench.notifications.sessionDeleted'), type: 'success' })
  } catch (error) {
    console.error('Delete session failed', error)
    appStore.showNotification({ title: t('workbench.notifications.deleteFailed'), type: 'error' })
  }
}

const resetAuthForm = () => {
  loginForm.username = ''
  loginForm.password = ''
  loginForm.displayName = ''
  loginForm.email = ''
  loginForm.confirmPassword = ''
  loginNotice.value = ''
  clearRegisterErrors()
}

const closeAuthModal = () => {
  showLoginModal.value = false
  resetAuthForm()
}

const switchAuthMode = (mode: 'login' | 'register') => {
  authMode.value = mode
  loginForm.password = ''
  loginForm.confirmPassword = ''
  loginNotice.value = ''
  clearRegisterErrors()
}

const clearRegisterErrors = () => {
  for (const field of Object.keys(registerErrors) as (RegistrationInputField | 'form')[]) {
    registerErrors[field] = ''
  }
}

const clearRegisterError = (field: RegistrationInputField) => {
  if (authMode.value !== 'register') return
  registerErrors[field] = ''
  registerErrors.form = ''
}

const focusRegistrationError = (errors: RegistrationErrors) => {
  const field = firstRegistrationErrorField(errors)
  if (!field || field === 'form') return
  const inputRefs: Record<RegistrationInputField, HTMLInputElement | null> = {
    username: usernameInputRef.value,
    displayName: displayNameInputRef.value,
    email: emailInputRef.value,
    password: passwordInputRef.value,
    confirmPassword: confirmPasswordInputRef.value,
  }
  nextTick(() => inputRefs[field]?.focus())
}

const applyRegistrationErrors = (errors: RegistrationErrors) => {
  clearRegisterErrors()
  for (const [field, message] of Object.entries(errors)) {
    if (message) registerErrors[field as RegistrationInputField | 'form'] = message
  }
  focusRegistrationError(errors)
}

const registrationValidationMessages = (): RegistrationValidationMessages => ({
  usernameRequired: t('workbench.auth.validation.usernameRequired'),
  usernameTooShort: t('workbench.auth.validation.usernameTooShort'),
  usernameTooLong: t('workbench.auth.validation.usernameTooLong'),
  usernameWhitespace: t('workbench.auth.validation.usernameWhitespace'),
  displayNameTooLong: t('workbench.auth.validation.displayNameTooLong'),
  emailRequired: t('workbench.auth.validation.emailRequired'),
  emailInvalid: t('workbench.auth.validation.emailInvalid'),
  emailTooLong: t('workbench.auth.validation.emailTooLong'),
  passwordRequired: t('workbench.auth.validation.passwordRequired'),
  passwordTooShort: t('workbench.auth.validation.passwordTooShort'),
  passwordTooLong: t('workbench.auth.validation.passwordTooLong'),
  confirmPasswordRequired: t('workbench.auth.validation.confirmPasswordRequired'),
  passwordMismatch: t('workbench.auth.validation.passwordMismatch'),
})

const parseAuthError = (error: any, fallback: string) => {
  const detail = error?.response?.data?.detail || error?.response?.data?.message
  if (typeof detail === 'string') return detail
  return error?.message || fallback
}

const handleUserLogin = async () => {
  if (!loginForm.username || !loginForm.password) {
    appStore.showNotification({ title: t('workbench.notifications.usernamePasswordRequired'), type: 'warning' })
    return
  }
  isLoggingIn.value = true
  loginNotice.value = ''
  try {
    const resp = await userApi.login(loginForm.username.trim(), loginForm.password)
    if (!resp?.success || !resp.data) throw new Error(resp?.message || t('workbench.notifications.loginFailed'))
    userStore.setToken(resp.data.token)
    userStore.setProfile(resp.data.user)
    // 登录后 profile 语言偏好优先于本地 localStorage
    appStore.initLocale()
    appStore.showNotification({ title: t('workbench.notifications.loginSuccess'), type: 'success' })
    closeAuthModal()
    await sessionStore.load()
  } catch (error: any) {
    const detail = parseAuthError(error, t('workbench.notifications.checkCredentials'))
    // 403 是账号被停用：把管理员留言留在弹窗里，用户可以照着做。
    if (error?.response?.status === 403) {
      loginNotice.value = detail
      loginForm.password = ''
    }
    appStore.showNotification({
      title: error?.response?.status === 403
        ? t('workbench.auth.accountDisabledTitle')
        : t('workbench.notifications.loginFailed'),
      message: detail,
      type: 'error',
    })
  } finally {
    isLoggingIn.value = false
  }
}

const handleUserRegister = async () => {
  const validationErrors = validateRegistration(loginForm, registrationValidationMessages())
  if (Object.keys(validationErrors).length) {
    applyRegistrationErrors(validationErrors)
    return
  }
  clearRegisterErrors()
  isLoggingIn.value = true
  try {
    const resp = await userApi.register({
      username: loginForm.username.trim(),
      password: loginForm.password,
      display_name: loginForm.displayName.trim() || null,
      email: loginForm.email.trim(),
    })
    if (!resp?.success || !resp.data) throw new Error(resp?.message || t('workbench.notifications.registerFailed'))
    userStore.setToken(resp.data.token)
    userStore.setProfile(resp.data.user)
    // 新用户：把当前匿名期选择的语言写入新 profile
    appStore.setLocale(appStore.locale)
    appStore.showNotification({ title: t('workbench.notifications.registerSuccess'), type: 'success' })
    closeAuthModal()
    await sessionStore.load()
  } catch (error: any) {
    applyRegistrationErrors(mapRegistrationServerError(
      error,
      t('workbench.auth.validation.registerRequestFailed'),
    ))
  } finally {
    isLoggingIn.value = false
  }
}

const handleAuthSubmit = () => {
  if (authMode.value === 'register') return handleUserRegister()
  return handleUserLogin()
}

const handleUserLogout = () => {
  userStore.clear()
  sessionStore.reset()
  announcementStore.reset()
  showUserMenu.value = false
}
</script>

<template>
  <div class="raven-workbench" :class="{ 'lang-en': activeLocale === 'en', 'is-embedded': isEmbedded }">
    <ConversationSearchDialog
      v-if="showSearchBox && isLoggedIn" :key="userStore.profile?.id"
      @close="showSearchBox = false" @select="handleSelectSession" @new-chat="startNewChat"
    />
    <!-- Sidebar — hidden when embedded in the Raven desktop client (Files tab),
         which provides its own navigation. -->
    <aside v-if="!isEmbedded" class="rw-sidebar">
      <!-- Brand -->
      <div class="rw-brand">
        <div class="rw-brand-left">
          <img :src="brandIcon" alt="" class="rw-brand-mark" aria-hidden="true" />
          <div>
            <div class="rw-brand-name">RavenAI</div>
            <div class="rw-brand-sub">{{ t('workbench.brandSub') }}</div>
          </div>
        </div>
        <button class="rw-icon-btn" :title="`${t('workbench.searchConversations')} (⌘K / Ctrl+K)`" @click="openConversationSearch" aria-haspopup="dialog" :aria-expanded="showSearchBox" aria-keyshortcuts="Meta+K Control+K" :aria-label="t('workbench.searchConversations')">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        </button>
      </div>

      <!-- New chat -->
      <button class="rw-new-btn" @click="startNewChat">
        <span class="rw-new-btn-left">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
          {{ t('workbench.newChat') }}
        </span>
        <span class="rw-kbd">⌘ N</span>
      </button>

      <!-- Nav -->
      <nav class="rw-nav-list">
        <router-link
          v-for="item in navItems"
          :key="item.id"
          :to="item.to"
          class="rw-nav-item"
          custom
          v-slot="{ navigate, isActive }"
        >
          <div class="rw-nav-row" :class="{ 'is-active': isNavItemActive(item, isActive) }" @click="navigate">
            <span class="rw-nav-icon">
              <svg v-if="item.icon === 'logs'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h10M4 18h16"/><circle cx="18" cy="12" r="1.4"/></svg>
              <svg v-else-if="item.icon === 'device'" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="11" rx="1.5"/><path d="M8 21h8M12 17v4"/><circle cx="7" cy="11" r="0.4" fill="currentColor"/></svg>
              <svg v-else width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z"/><path d="M3 7.5 12 12l9-4.5M12 12v9"/></svg>
            </span>
            <span>{{ item.label }}</span>
          </div>
        </router-link>
      </nav>

      <div class="rw-divider"/>

      <!-- Chat list -->
      <div class="rw-chat-list">
        <div class="rw-group-header">
          <span class="rw-group-label">{{ t('workbench.recentConversations') }}</span>
          <span v-if="isLoggedIn" class="rw-group-count">{{ sessionStore.sessions.length }}</span>
          <button
            v-if="isLoggedIn"
            class="rw-refresh-btn"
            :disabled="sessionStore.loading || sessionStore.refreshing"
            @click="reloadSessions"
            :title="t('common.refresh')"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" :class="{ spin: sessionStore.loading || sessionStore.refreshing }"><path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>
          </button>
        </div>

        <template v-if="!isLoggedIn">
          <div class="rw-login-hint">
            <div class="rw-login-hint-title">{{ t('workbench.loginHintTitle') }}</div>
            <div class="rw-login-actions">
              <button class="rw-login-link" @click="openAuthModal('login')">{{ t('workbench.loginNow') }}</button>
              <button class="rw-login-link" @click="openAuthModal('register')">{{ t('workbench.registerAccount') }}</button>
            </div>
          </div>
        </template>
        <!-- Placeholder only when we truly have nothing to show; background
             refreshes keep the existing list rendered (stale-while-revalidate). -->
        <template v-else-if="sessionStore.loading && !sessionStore.sessions.length">
          <div class="rw-empty">{{ t('workbench.sessionsLoading') }}</div>
        </template>
        <template v-else-if="!sessionStore.sessions.length">
          <div class="rw-empty">{{ t('workbench.emptySessions') }}</div>
        </template>
        <template v-else>
          <div v-for="group in groupedSessions" :key="group.label">
            <div class="rw-chat-group">{{ group.label }}</div>
            <div
              v-for="session in group.items"
              :key="session.id"
              class="rw-chat-row"
              :class="{
                'is-active': sessionStore.selectedSessionId === session.id && isHomeRoute,
                'is-hover': hoverSessionId === session.id || openRowMenuId === session.id,
                'is-editing': editingSessionId === session.id,
              }"
              @click="editingSessionId !== session.id && handleSelectSession(session)"
              @mouseenter="hoverSessionId = session.id"
              @mouseleave="hoverSessionId = null"
            >
              <!-- Inline rename input -->
              <div
                v-if="editingSessionId === session.id"
                class="rw-rename-input-wrap"
                @click.stop
              >
                <input
                  ref="renameInputRef"
                  v-model="editingSessionTitle"
                  class="rw-rename-input"
                  type="text"
                  maxlength="80"
                  @keydown="handleRenameKeydown($event, session.id)"
                  @blur="commitRename(session.id)"
                />
              </div>
              <template v-else>
                <span class="rw-chat-row-text">{{ session.title || t('workbench.untitledSession') }}</span>
                <span
                  v-if="isSessionRunning(session.id)"
                  class="rw-row-thinking"
                  :title="t('workbench.thinking')"
                  :aria-label="t('workbench.thinking')"
                >
                  <span></span>
                  <span></span>
                  <span></span>
                </span>
                <button
                  class="rw-row-more"
                  :class="{ visible: hoverSessionId === session.id || openRowMenuId === session.id }"
                  @click.stop="openRowMenuId = openRowMenuId === session.id ? null : session.id"
                  :aria-label="t('common.more')"
                >
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="19" cy="12" r="1" fill="currentColor"/></svg>
                </button>
                <div
                  v-if="openRowMenuId === session.id"
                  class="rw-row-menu"
                  @click.stop
                >
                  <button class="rw-menu-item" @click="startRenameSession(session)">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4l10-10-4-4L4 16zM14 6l4 4"/></svg>
                    {{ t('workbench.renameConversation') }}
                  </button>
                  <button class="rw-menu-item" @click="togglePinSession(session)">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v6M9 8h6l2 6H7zM12 14v8"/></svg>
                    {{ session.is_pinned ? t('workbench.unpinConversation') : t('workbench.pinConversation') }}
                  </button>
                  <div class="rw-menu-divider"/>
                  <button class="rw-menu-item is-danger" @click="deleteSession(session.id)">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>
                    {{ t('workbench.deleteConversation') }}
                  </button>
                </div>
              </template>
            </div>
          </div>
        </template>
      </div>

      <!-- Bottom user card -->
      <div class="rw-bottom">
        <div class="rw-menu-wrap">
          <div
            v-if="showUserMenu"
            ref="userMenuRef"
            class="rw-user-menu"
            role="menu"
          >
            <div class="rw-user-menu-head">
              <div class="rw-avatar lg">{{ userInitial }}</div>
              <div style="flex:1; min-width:0;">
                <div class="rw-user-menu-name">
                  {{ currentUserName }}
                  <span v-if="isAdmin" class="rw-role-badge">{{ t('workbench.adminRole') }}</span>
                </div>
                <div class="rw-user-menu-mail">{{ currentUserStatusText }}</div>
              </div>
            </div>
            <button
              v-if="userStore.canAccessAdmin"
              class="rw-user-menu-item"
              @click="goToAdminConsole"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="rw-menu-leading"><path d="M12 3l8 4v6c0 4.5-3.5 7-8 8-4.5-1-8-3.5-8-8V7l8-4z"/><path d="m9 12 2 2 4-4"/></svg>
              {{ t('workbench.adminConsole') }}
              <span class="rw-kbd-right">/admin</span>
            </button>
            <button
              v-if="isLoggedIn"
              class="rw-user-menu-item"
              @click="goToBugFixes"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="rw-menu-leading"><path d="M8 6h13"/><path d="M8 12h13"/><path d="M8 18h13"/><path d="M3 6h.01"/><path d="M3 12h.01"/><path d="M3 18h.01"/></svg>
              {{ t('workbench.bugFixes') }}
              <span class="rw-kbd-right">/bug-fixes</span>
            </button>
            <button
              type="button"
              class="rw-user-menu-item"
              @click="goToDownload"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="rw-menu-leading"><path d="M12 3v12m-4-4 4 4 4-4"/><path d="M5 16v4a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-4"/></svg>
              {{ t('workbench.downloadClient') }}
              <span class="rw-kbd-right">/download</span>
            </button>
            <div class="rw-menu-divider"/>
            <div class="rw-menu-section">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="rw-menu-leading"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>
              <span>{{ t('language.label') }}</span>
              <span class="rw-lang-pill" role="group" :aria-label="t('language.switchTo')">
                <span class="rw-lang-opt" :class="{ active: activeLocale === 'zh' }" @click="setLanguage('zh')">{{ t('workbench.zhShort') }}</span>
                <span class="rw-lang-opt" :class="{ active: activeLocale === 'en' }" @click="setLanguage('en')">EN</span>
              </span>
            </div>
            <div class="rw-menu-section">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="rw-menu-leading"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/></svg>
              <span>{{ t('theme.label') }}</span>
              <ThemeToggle />
            </div>
            <button class="rw-user-menu-item" @click="openSettingsModal">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="rw-menu-leading"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33 1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82 1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
              {{ t('workbench.settings') }}
              <span class="rw-kbd-right">⌘ ,</span>
            </button>
            <button class="rw-user-menu-item" @click="showUserMenu = false">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="rw-menu-leading"><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 1.5-2.5 2-2.5 3.5"/><circle cx="12" cy="17" r="0.4" fill="currentColor"/></svg>
              {{ t('workbench.helpShortcuts') }}
              <span class="rw-kbd-right">?</span>
            </button>
            <div class="rw-menu-divider"/>
            <button
              class="rw-user-menu-item"
              @click="isLoggedIn ? handleUserLogout() : (showUserMenu = false, openAuthModal('login'))"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="rw-menu-leading"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg>
              {{ isLoggedIn ? t('workbench.logout') : t('workbench.loginNow') }}
            </button>
          </div>
          <div
            ref="userButtonRef"
            class="rw-user-card"
            :class="{ open: showUserMenu }"
            @click="showUserMenu = !showUserMenu"
          >
            <div class="rw-avatar">{{ userInitial }}</div>
            <div class="rw-user-meta">
              <div class="rw-user-name">{{ currentUserName }}</div>
              <div class="rw-user-role">
                <span v-if="isAdmin">{{ t('workbench.adminRole') }} · {{ currentUserStatusText }}</span>
                <span v-else-if="isLoggedIn">{{ currentUserStatusText }}</span>
                <span v-else>{{ t('workbench.notLoggedInCta') }}</span>
              </div>
            </div>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="rw-chevron" :class="{ flipped: showUserMenu }"><path d="m6 9 6 6 6-6"/></svg>
          </div>
        </div>
      </div>
    </aside>

    <!-- Main pane: routed content provides its own topbar + body -->
    <main class="rw-main">
      <div v-if="!isEmbedded" class="rw-mobile-search-bar">
        <span>RavenAI</span>
        <button class="rw-icon-btn" :aria-label="t('workbench.searchConversations')" aria-haspopup="dialog" :aria-expanded="showSearchBox" @click="openConversationSearch"><Search :size="18" aria-hidden="true" /></button>
      </div>
      <router-view />
    </main>

    <SystemAnnouncementDialog />

    <!-- Settings modal — category navigation on the left, focused settings on the right. -->
    <div v-if="showSettingsModal" class="rw-modal-backdrop" @click.self="closeSettingsModal">
      <div
        class="rw-modal rw-settings-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="rw-settings-title"
      >
        <div class="rw-modal-head">
          <div>
            <h3 id="rw-settings-title" class="rw-modal-title">{{ t('workbench.settingsPanel.title') }}</h3>
            <p class="rw-modal-sub">{{ t('workbench.settingsPanel.subtitle') }}</p>
          </div>
          <button class="rw-modal-close" @click="closeSettingsModal" :aria-label="t('workbench.settingsPanel.close')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>
          </button>
        </div>

        <div class="rw-settings-layout">
          <aside class="rw-settings-sidebar">
            <div class="rw-settings-account-strip">
              <div class="rw-avatar lg">{{ userInitial }}</div>
              <div class="rw-settings-account-meta">
                <strong>{{ currentUserName }}</strong>
                <span>{{ userStore.profile?.username }}</span>
              </div>
              <span class="rw-profile-role-chip">{{ currentProfileRoleLabel }}</span>
            </div>

            <nav class="rw-settings-nav" :aria-label="t('workbench.settingsPanel.categoryLabel')">
              <button
                type="button"
                class="rw-settings-nav-item"
                :class="{ active: activeSettingsSection === 'account' }"
                :aria-current="activeSettingsSection === 'account' ? 'page' : undefined"
                @click="activeSettingsSection = 'account'"
              >
                <span class="rw-settings-nav-icon"><UserRound :size="17" stroke-width="1.8" /></span>
                <span class="rw-settings-nav-copy">
                  <strong>{{ t('workbench.settingsPanel.categories.account') }}</strong>
                  <small>{{ t('workbench.settingsPanel.categories.accountHint') }}</small>
                </span>
              </button>
              <button
                type="button"
                class="rw-settings-nav-item"
                :class="{ active: activeSettingsSection === 'agent' }"
                :aria-current="activeSettingsSection === 'agent' ? 'page' : undefined"
                @click="activeSettingsSection = 'agent'"
              >
                <span class="rw-settings-nav-icon"><MessageCircleQuestion :size="17" stroke-width="1.8" /></span>
                <span class="rw-settings-nav-copy">
                  <strong>{{ t('workbench.settingsPanel.categories.agent') }}</strong>
                  <small>{{ t('workbench.settingsPanel.categories.agentHint') }}</small>
                </span>
              </button>
            </nav>

            <div class="rw-settings-permission">
              <ShieldCheck :size="16" stroke-width="1.8" />
              <span>{{ t('workbench.settingsPanel.permissionRole') }}</span>
              <strong>{{ isAdmin ? t('workbench.adminRole') : t('workbench.settingsPanel.permissionUser') }}</strong>
            </div>
          </aside>

          <form class="rw-settings-form" @submit.prevent="handleSaveProfile">
            <div class="rw-settings-content">
              <section v-if="activeSettingsSection === 'account'" class="rw-settings-pane" aria-labelledby="rw-account-settings-title">
                <div class="rw-settings-pane-head">
                  <span class="rw-settings-eyebrow">{{ t('workbench.settingsPanel.categories.account') }}</span>
                  <h4 id="rw-account-settings-title">{{ t('workbench.settingsPanel.account.title') }}</h4>
                  <p>{{ t('workbench.settingsPanel.account.description') }}</p>
                </div>

                <div class="rw-settings-field-grid">
                  <label class="rw-form-field rw-form-field--wide">
                    <span class="rw-form-label">{{ t('workbench.settingsPanel.username') }}</span>
                    <input :value="userStore.profile?.username || ''" type="text" class="rw-input" disabled autocomplete="username" />
                  </label>
                  <label class="rw-form-field">
                    <span class="rw-form-label">{{ t('workbench.settingsPanel.displayName') }}</span>
                    <input
                      v-model="profileForm.displayName"
                      type="text"
                      class="rw-input"
                      maxlength="128"
                      :placeholder="t('workbench.settingsPanel.displayNamePlaceholder')"
                      autocomplete="name"
                    />
                  </label>
                  <label class="rw-form-field">
                    <span class="rw-form-label">{{ t('workbench.settingsPanel.email') }}</span>
                    <input
                      v-model="profileForm.email"
                      type="email"
                      class="rw-input"
                      maxlength="255"
                      :placeholder="t('workbench.settingsPanel.emailPlaceholder')"
                      autocomplete="email"
                    />
                  </label>
                  <label class="rw-form-field rw-form-field--wide">
                    <span class="rw-form-label">{{ t('workbench.settingsPanel.profileRole') }}</span>
                    <select v-model="profileForm.profileRole" class="rw-select">
                      <option v-for="item in profileRoleOptions" :key="item.value" :value="item.value">
                        {{ item.label }}
                      </option>
                    </select>
                  </label>
                </div>
              </section>

              <section v-else class="rw-settings-pane" aria-labelledby="rw-agent-settings-title">
                <div class="rw-settings-pane-head">
                  <span class="rw-settings-eyebrow">{{ t('workbench.settingsPanel.categories.agent') }}</span>
                  <h4 id="rw-agent-settings-title">{{ t('workbench.settingsPanel.clarification.section') }}</h4>
                  <p>{{ t('workbench.settingsPanel.clarification.description') }}</p>
                </div>

                <label class="rw-setting-toggle-card">
                  <span class="rw-setting-toggle-copy">
                    <strong>{{ t('workbench.settingsPanel.clarification.enabledLabel') }}</strong>
                    <small>{{ t('workbench.settingsPanel.clarification.enabledHint') }}</small>
                  </span>
                  <span class="rw-switch">
                    <input v-model="profileForm.clarificationEnabled" type="checkbox" />
                    <span aria-hidden="true"></span>
                  </span>
                </label>

                <div class="rw-settings-field-grid rw-settings-field-grid--agent" :class="{ disabled: !profileForm.clarificationEnabled }">
                  <label class="rw-form-field">
                    <span class="rw-form-label">{{ t('workbench.settingsPanel.clarification.maxRoundsLabel') }}</span>
                    <input
                      v-model.number="profileForm.clarificationMaxRounds"
                      type="number"
                      min="0"
                      max="20"
                      class="rw-input"
                      :disabled="!profileForm.clarificationEnabled"
                    />
                  </label>
                </div>
              </section>
            </div>

            <div class="rw-settings-actions">
              <button type="button" class="rw-btn-ghost" @click="closeSettingsModal">{{ t('common.cancel') }}</button>
              <button type="submit" class="rw-btn-primary rw-save-profile-btn" :disabled="isSavingProfile">
                <Save v-if="!isSavingProfile" :size="14" stroke-width="1.8" />
                {{ isSavingProfile ? t('workbench.settingsPanel.saving') : t('workbench.settingsPanel.save') }}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>

    <!-- Login modal -->
    <div v-if="showLoginModal" class="rw-modal-backdrop">
      <div class="rw-modal rw-auth-modal" role="dialog" aria-modal="true" aria-labelledby="rw-auth-title">
        <div class="rw-modal-head">
          <div>
            <h3 id="rw-auth-title" class="rw-modal-title">{{ authMode === 'register' ? t('workbench.auth.registerTitle') : t('workbench.auth.loginTitle') }}</h3>
            <p class="rw-modal-sub">{{ authMode === 'register' ? t('workbench.auth.registerSubtitle') : t('workbench.auth.loginSubtitle') }}</p>
          </div>
          <button class="rw-modal-close" @click="closeAuthModal" :aria-label="t('common.close')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>
          </button>
        </div>
        <div class="rw-auth-tabs" role="tablist" :aria-label="t('workbench.auth.accountActions')">
          <button type="button" :class="{ active: authMode === 'login' }" @click="switchAuthMode('login')">{{ t('workbench.auth.loginTab') }}</button>
          <button type="button" :class="{ active: authMode === 'register' }" @click="switchAuthMode('register')">{{ t('workbench.auth.registerTab') }}</button>
        </div>
        <form class="rw-modal-form" novalidate @submit.prevent="handleAuthSubmit">
          <div class="rw-form-field" :class="{ 'has-error': authMode === 'register' && registerErrors.username }">
            <label class="rw-form-label" for="rw-auth-username">{{ t('workbench.auth.username') }}</label>
            <input
              id="rw-auth-username"
              ref="usernameInputRef"
              v-model="loginForm.username"
              type="text"
              class="rw-input"
              maxlength="128"
              :placeholder="t('workbench.auth.usernamePlaceholder')"
              autocomplete="username"
              autocapitalize="none"
              :aria-invalid="authMode === 'register' && registerErrors.username ? 'true' : undefined"
              :aria-describedby="authMode === 'register' && registerErrors.username ? 'rw-auth-username-error' : undefined"
              @input="clearRegisterError('username')"
            />
            <span v-if="authMode === 'register' && registerErrors.username" id="rw-auth-username-error" class="rw-form-error" role="alert">
              {{ registerErrors.username }}
            </span>
          </div>
          <div v-if="authMode === 'register'" class="rw-form-field" :class="{ 'has-error': registerErrors.displayName }">
            <label class="rw-form-label" for="rw-auth-display-name">{{ t('workbench.auth.displayName') }}</label>
            <input
              id="rw-auth-display-name"
              ref="displayNameInputRef"
              v-model="loginForm.displayName"
              type="text"
              class="rw-input"
              maxlength="128"
              :placeholder="t('workbench.auth.optional')"
              autocomplete="name"
              :aria-invalid="registerErrors.displayName ? 'true' : undefined"
              :aria-describedby="registerErrors.displayName ? 'rw-auth-display-name-error' : undefined"
              @input="clearRegisterError('displayName')"
            />
            <span v-if="registerErrors.displayName" id="rw-auth-display-name-error" class="rw-form-error" role="alert">
              {{ registerErrors.displayName }}
            </span>
          </div>
          <div v-if="authMode === 'register'" class="rw-form-field" :class="{ 'has-error': registerErrors.email }">
            <label class="rw-form-label" for="rw-auth-email">{{ t('workbench.auth.email') }}</label>
            <input
              id="rw-auth-email"
              ref="emailInputRef"
              v-model="loginForm.email"
              type="email"
              class="rw-input"
              maxlength="255"
              :placeholder="t('workbench.auth.emailPlaceholder')"
              autocomplete="email"
              autocapitalize="none"
              spellcheck="false"
              :aria-invalid="registerErrors.email ? 'true' : undefined"
              :aria-describedby="registerErrors.email ? 'rw-auth-email-error' : undefined"
              @input="clearRegisterError('email')"
            />
            <span v-if="registerErrors.email" id="rw-auth-email-error" class="rw-form-error" role="alert">
              {{ registerErrors.email }}
            </span>
          </div>
          <div class="rw-form-field" :class="{ 'has-error': authMode === 'register' && registerErrors.password }">
            <label class="rw-form-label" for="rw-auth-password">{{ t('workbench.auth.password') }}</label>
            <input
              id="rw-auth-password"
              ref="passwordInputRef"
              v-model="loginForm.password"
              type="password"
              class="rw-input"
              maxlength="256"
              :placeholder="t('workbench.auth.passwordPlaceholder')"
              :autocomplete="authMode === 'register' ? 'new-password' : 'current-password'"
              :aria-invalid="authMode === 'register' && registerErrors.password ? 'true' : undefined"
              :aria-describedby="authMode === 'register' && registerErrors.password ? 'rw-auth-password-error' : undefined"
              @input="clearRegisterError('password')"
            />
            <span v-if="authMode === 'register' && registerErrors.password" id="rw-auth-password-error" class="rw-form-error" role="alert">
              {{ registerErrors.password }}
            </span>
          </div>
          <div v-if="authMode === 'register'" class="rw-form-field" :class="{ 'has-error': registerErrors.confirmPassword }">
            <label class="rw-form-label" for="rw-auth-confirm-password">{{ t('workbench.auth.confirmPassword') }}</label>
            <input
              id="rw-auth-confirm-password"
              ref="confirmPasswordInputRef"
              v-model="loginForm.confirmPassword"
              type="password"
              class="rw-input"
              maxlength="256"
              :placeholder="t('workbench.auth.confirmPasswordPlaceholder')"
              autocomplete="new-password"
              :aria-invalid="registerErrors.confirmPassword ? 'true' : undefined"
              :aria-describedby="registerErrors.confirmPassword ? 'rw-auth-confirm-password-error' : undefined"
              @input="clearRegisterError('confirmPassword')"
            />
            <span v-if="registerErrors.confirmPassword" id="rw-auth-confirm-password-error" class="rw-form-error" role="alert">
              {{ registerErrors.confirmPassword }}
            </span>
          </div>
          <div v-if="authMode === 'register' && registerErrors.form" class="rw-form-alert" role="alert">
            {{ registerErrors.form }}
          </div>
          <div v-if="authMode === 'login' && loginNotice" class="rw-form-alert rw-login-notice" role="alert">
            <strong class="rw-login-notice-title">{{ t('workbench.auth.accountDisabledTitle') }}</strong>
            <span class="rw-login-notice-body">{{ loginNotice }}</span>
          </div>
          <div class="rw-modal-actions">
            <button type="submit" class="rw-btn-primary" :disabled="isLoggingIn">
              {{ isLoggingIn ? (authMode === 'register' ? t('workbench.auth.registerLoading') : t('workbench.auth.loginLoading')) : (authMode === 'register' ? t('workbench.auth.registerSubmit') : t('workbench.loginNow')) }}
            </button>
            <button type="button" class="rw-btn-ghost" @click="closeAuthModal">{{ t('common.cancel') }}</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>

<style scoped>
.raven-workbench {
  /* Design tokens (--rw-*) come from src/styles/theme.css: light values
     follow DESIGN.md, dark values follow DESIGN-Dark.md via html.dark. */
  display: flex;
  height: 100%;
  min-height: 0;
  background: var(--rw-canvas);
  color: var(--rw-ink);
  font-family: var(--rw-sans);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}

.raven-workbench *,
.raven-workbench *::before,
.raven-workbench *::after {
  box-sizing: border-box;
}

.raven-workbench button {
  font-family: inherit;
  cursor: pointer;
  border: none;
  background: none;
  padding: 0;
  color: inherit;
}

.raven-workbench input,
.raven-workbench textarea {
  font-family: inherit;
  color: inherit;
}

.spin { animation: rw-spin 1s linear infinite; }
@keyframes rw-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }

/* ---------- Sidebar ---------- */
.rw-sidebar {
  width: 272px;
  flex-shrink: 0;
  background: var(--rw-canvas-soft);
  border-right: 1px solid var(--rw-hairline);
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.rw-brand { display: flex; align-items: center; justify-content: space-between; padding: 14px 16px 12px; }
.rw-brand-left { display: flex; align-items: center; gap: 9px; }
.rw-brand-mark {
  width: 26px; height: 26px; border-radius: 6px;
  object-fit: contain; flex-shrink: 0;
}
.rw-brand-name { font-size: 14px; font-weight: 600; letter-spacing: -0.1px; line-height: 1; }
.rw-brand-sub {
  font-size: 10.5px; color: var(--rw-muted);
  font-weight: 500; letter-spacing: 0.4px;
  margin-top: 3px;
}

.rw-icon-btn {
  width: 28px; height: 28px; border-radius: 6px;
  display: grid; place-items: center;
  color: var(--rw-body); transition: background .15s, color .15s;
}
.rw-icon-btn:hover { background: var(--rw-surface-strong); color: var(--rw-ink); }

.raven-workbench button.rw-new-btn {
  margin: 4px 12px 10px; height: 36px;
  background: var(--rw-primary); color: var(--rw-on-primary);
  border-radius: 8px;
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 12px;
  font-size: 13.5px; font-weight: 500;
  transition: background .15s, color .15s;
}
.raven-workbench button.rw-new-btn:hover {
  background: var(--rw-primary-hover);
  color: var(--rw-on-primary);
}
.raven-workbench button.rw-new-btn:active {
  background: var(--rw-primary-active);
  color: var(--rw-on-primary);
}
.rw-new-btn-left { display: inline-flex; align-items: center; gap: 8px; }
.rw-kbd {
  font-family: var(--rw-mono); font-size: 11px;
  color: rgba(255,255,255,.55);
  border: 1px solid rgba(255,255,255,.18);
  border-radius: 4px; padding: 1px 5px; line-height: 1.2;
}


.rw-nav-list { padding: 0 8px; display: flex; flex-direction: column; gap: 1px; }
.rw-nav-item { display: block; text-decoration: none; color: inherit; }
.rw-nav-row {
  display: flex; align-items: center; gap: 10px;
  height: 32px; padding: 0 8px; border-radius: 6px;
  font-size: 13.5px; font-weight: 500; color: var(--rw-body);
  cursor: pointer; transition: background .12s, color .12s;
}
.rw-nav-row:hover { background: var(--rw-hairline-soft); color: var(--rw-ink); }
.rw-nav-row.is-active { background: var(--rw-surface-strong); color: var(--rw-ink); }
.rw-nav-icon { display: inline-flex; color: inherit; }

.rw-divider { margin: 14px 16px 10px; height: 1px; background: var(--rw-hairline); }

.rw-chat-list {
  flex: 1; min-height: 0; overflow: auto;
  padding: 0 8px 8px;
}
.rw-group-header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 16px 6px; gap: 8px;
}
.rw-group-label {
  font-size: 10.5px; font-weight: 600; letter-spacing: 0.8px;
  text-transform: uppercase; color: var(--rw-muted);
}
.rw-group-count { font-family: var(--rw-mono); font-size: 11px; color: var(--rw-muted); }
.rw-refresh-btn {
  width: 22px; height: 22px; border-radius: 4px;
  display: grid; place-items: center; color: var(--rw-muted);
  margin-left: auto;
}
.rw-refresh-btn:hover { background: var(--rw-surface-strong); color: var(--rw-ink); }
.rw-refresh-btn[disabled] { opacity: .6; cursor: default; }

.rw-login-hint {
  margin: 4px 8px 8px; padding: 10px 12px;
  background: var(--rw-canvas); border: 1px solid var(--rw-hairline-strong);
  border-radius: 8px;
}
.rw-login-hint-title { font-size: 12.5px; font-weight: 600; color: var(--rw-ink); }
.rw-login-actions { display: flex; align-items: center; gap: 12px; margin-top: 4px; }
.rw-login-link {
  font-size: 12px; color: var(--rw-ink);
  text-decoration: underline; text-underline-offset: 2px;
  text-decoration-color: var(--rw-hairline-strong);
}
.rw-login-link:hover { text-decoration-color: var(--rw-ink); }
.rw-empty { padding: 6px 16px 8px; font-size: 12px; color: var(--rw-muted); }

.rw-chat-group {
  font-size: 10.5px; font-weight: 600; letter-spacing: 0.6px;
  color: var(--rw-muted); text-transform: uppercase;
  padding: 10px 8px 4px;
}
.rw-chat-row {
  display: flex; align-items: center; gap: 8px;
  height: 30px; padding: 0 8px; border-radius: 6px;
  font-size: 13px; color: var(--rw-body); font-weight: 400;
  cursor: pointer; transition: background .12s, color .12s;
  position: relative;
}
.rw-chat-row:hover, .rw-chat-row.is-hover { background: var(--rw-hairline-soft); }
.rw-chat-row.is-active { background: var(--rw-surface-strong); color: var(--rw-ink); font-weight: 500; }
.rw-chat-row-text { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rw-row-more {
  width: 22px; height: 22px; border-radius: 4px;
  display: grid; place-items: center;
  color: var(--rw-body); flex-shrink: 0;
  visibility: hidden; margin-left: auto;
}
.rw-row-more.visible { visibility: visible; }
.rw-row-more:hover { background: var(--rw-hairline-strong); }

.rw-chat-row.is-editing {
  padding: 0 4px;
  cursor: default;
}
.rw-rename-input-wrap {
  flex: 1; min-width: 0; display: flex; align-items: center;
}
.rw-rename-input {
  width: 100%; height: 26px;
  background: var(--rw-canvas);
  border: 1px solid var(--rw-ink);
  border-radius: 5px;
  padding: 0 7px;
  font-size: 13px; font-family: inherit;
  color: var(--rw-ink); outline: none;
}
.rw-rename-input:focus {
  box-shadow: 0 0 0 2px rgba(23,23,23,.12);
}

.rw-row-thinking {
  display: inline-flex; align-items: center; justify-content: center; gap: 2px;
  width: 16px; height: 16px;
  color: var(--rw-muted);
  flex-shrink: 0;
}
.rw-row-thinking span {
  width: 3px; height: 3px; border-radius: 999px;
  background: currentColor;
  animation: rw-row-thinking-dot 1.05s ease-in-out infinite;
}
.rw-row-thinking span:nth-child(2) { animation-delay: .14s; }
.rw-row-thinking span:nth-child(3) { animation-delay: .28s; }
@keyframes rw-row-thinking-dot {
  0%, 72%, 100% { opacity: .35; transform: translateY(0); }
  36% { opacity: 1; transform: translateY(-2px); }
}

.rw-row-menu {
  position: absolute; top: 100%; right: 4px; margin-top: 2px;
  width: 168px; background: var(--rw-canvas);
  border: 1px solid var(--rw-hairline-strong); border-radius: 8px;
  padding: 4px;
  box-shadow: 0 10px 24px rgba(0,0,0,.12), 0 2px 6px rgba(0,0,0,.04);
  z-index: 25;
}

.rw-menu-item {
  display: flex; align-items: center; gap: 9px;
  width: 100%; height: 30px; padding: 0 10px;
  border-radius: 5px; font-size: 12.5px; font-weight: 500;
  color: var(--rw-ink); cursor: pointer;
}
.rw-menu-item:hover { background: var(--rw-surface-strong); }
.rw-menu-item.is-danger { color: var(--rw-danger); }
.rw-menu-item.is-danger:hover { background: rgba(192,56,43,.06); }
.rw-menu-divider { height: 1px; background: var(--rw-hairline); margin: 4px 6px; }

/* Bottom user card */
.rw-bottom {
  border-top: 1px solid var(--rw-hairline);
  padding: 10px;
  display: flex; flex-direction: column; gap: 4px;
  background: var(--rw-canvas-soft);
}
.rw-menu-wrap { position: relative; }
.rw-user-menu {
  position: absolute; left: 0; right: 0;
  bottom: calc(100% + 6px);
  background: var(--rw-canvas);
  border: 1px solid var(--rw-hairline-strong); border-radius: 10px;
  padding: 4px;
  box-shadow: 0 12px 32px rgba(0,0,0,.12), 0 2px 6px rgba(0,0,0,.04);
  z-index: 20;
}
.rw-user-menu-head {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 10px 12px;
  border-bottom: 1px solid var(--rw-hairline);
  margin-bottom: 4px;
}
.rw-user-menu-name {
  font-size: 13.5px; font-weight: 600; color: var(--rw-ink); line-height: 1.2;
  display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
}
.rw-role-badge {
  display: inline-flex; align-items: center;
  padding: 1px 6px; border-radius: 4px;
  background: #171717; color: #ffffff;
  font-size: 10px; font-weight: 600; letter-spacing: 0.4px;
  text-transform: uppercase; line-height: 1.4;
  font-family: var(--rw-mono);
}
.rw-user-menu-mail { font-size: 11.5px; color: var(--rw-muted); margin-top: 3px; line-height: 1.2; font-family: var(--rw-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rw-menu-section,
.raven-workbench button.rw-user-menu-item {
  display: flex; align-items: center; gap: 10px;
  padding: 6px 10px; height: 32px;
  border-radius: 6px; font-size: 13px; color: var(--rw-ink); font-weight: 500;
}
.rw-menu-section {
  cursor: default;
}
.raven-workbench button.rw-user-menu-item {
  cursor: pointer; width: 100%; text-align: left;
}
.raven-workbench button.rw-user-menu-item:hover { background: var(--rw-surface-strong); }
.rw-menu-leading { color: var(--rw-body); flex-shrink: 0; }
.rw-kbd-right { margin-left: auto; font-family: var(--rw-mono); font-size: 11px; color: var(--rw-muted); }
.rw-lang-pill {
  display: inline-flex; margin-left: auto;
  background: var(--rw-surface-strong); border-radius: 6px;
  padding: 2px; font-family: var(--rw-mono); font-size: 11px; font-weight: 600;
}
.rw-lang-opt { padding: 3px 8px; border-radius: 4px; color: var(--rw-muted); cursor: pointer; }
.rw-lang-opt.active { background: var(--rw-canvas); color: var(--rw-ink); box-shadow: 0 1px 2px rgba(0,0,0,.06); }
.rw-menu-section .theme-toggle { margin-left: auto; }

.rw-user-card {
  display: flex; align-items: center; gap: 10px;
  padding: 6px; border-radius: 8px; cursor: pointer;
  transition: background .12s;
}
.rw-user-card:hover, .rw-user-card.open { background: var(--rw-surface-strong); }
.rw-avatar {
  width: 30px; height: 30px; border-radius: 999px;
  background: linear-gradient(135deg, #cfe7ff 0%, #a8c8e8 100%);
  color: var(--rw-ink);
  display: grid; place-items: center;
  font-size: 11px; font-weight: 600;
  flex-shrink: 0;
  border: 1px solid rgba(0,0,0,.06);
  text-transform: uppercase;
}
.rw-avatar.lg { width: 34px; height: 34px; font-size: 13px; }
.rw-user-meta { flex: 1; min-width: 0; }
.rw-user-name { font-size: 13px; font-weight: 600; color: var(--rw-ink); line-height: 1.2; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rw-user-role { font-size: 11px; color: var(--rw-muted); margin-top: 2px; line-height: 1.2; font-family: var(--rw-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rw-chevron { color: var(--rw-muted); transition: transform .15s; flex-shrink: 0; }
.rw-chevron.flipped { transform: rotate(180deg); }

.rw-mobile-search-bar { display: none; }

/* ---------- Main pane ---------- */
.rw-main {
  flex: 1; min-width: 0; min-height: 0;
  display: flex; flex-direction: column;
  background: var(--rw-canvas); height: 100%;
}

/* ---------- Modal ---------- */
.rw-modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,.4);
  backdrop-filter: blur(4px);
  display: flex; align-items: center; justify-content: center;
  padding: 16px; z-index: 100;
}
.rw-modal {
  width: 100%; max-width: 380px;
  background: var(--rw-canvas);
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 14px;
  padding: 22px;
  box-shadow: 0 24px 64px rgba(0,0,0,.18);
}
.rw-auth-modal {
  max-height: calc(100vh - 32px);
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--rw-hairline-strong) transparent;
}
.rw-modal.rw-settings-modal {
  max-width: 840px;
  min-height: 560px;
  max-height: calc(100vh - 40px);
  padding: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  animation: rw-settings-enter .2s cubic-bezier(.2,.8,.2,1);
}
@keyframes rw-settings-enter {
  from { opacity: 0; transform: translateY(8px) scale(.99); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
.rw-settings-modal > .rw-modal-head {
  min-height: 78px;
  padding: 19px 22px;
  border-bottom: 1px solid var(--rw-hairline);
  background: var(--rw-canvas);
}
.rw-settings-layout {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: 236px minmax(0, 1fr);
}
.rw-settings-sidebar {
  min-width: 0;
  padding: 18px 14px 14px;
  border-right: 1px solid var(--rw-hairline);
  background: var(--rw-canvas-soft);
  display: flex;
  flex-direction: column;
}
.rw-settings-account-strip {
  padding: 2px 6px 18px;
  border-bottom: 1px solid var(--rw-hairline);
  display: flex;
  align-items: center;
  gap: 10px;
}
.rw-settings-account-meta {
  display: grid;
  gap: 2px;
  min-width: 0;
}
.rw-settings-account-meta strong {
  font-size: 13.5px;
  font-weight: 650;
  color: var(--rw-ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rw-settings-account-meta span {
  font-size: 11.5px;
  color: var(--rw-muted);
  font-family: var(--rw-mono);
}
.rw-profile-role-chip {
  display: none;
}
.rw-settings-nav {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding-top: 14px;
}
.raven-workbench button.rw-settings-nav-item {
  width: 100%;
  min-height: 58px;
  padding: 9px 10px;
  border-radius: 9px;
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--rw-body);
  text-align: left;
  transition: background .15s ease, color .15s ease, box-shadow .15s ease;
}
.raven-workbench button.rw-settings-nav-item:hover {
  background: var(--rw-surface-strong);
  color: var(--rw-ink);
}
.raven-workbench button.rw-settings-nav-item.active {
  background: var(--rw-canvas);
  color: var(--rw-ink);
  box-shadow: 0 1px 3px rgba(0,0,0,.08), inset 0 0 0 1px var(--rw-hairline);
}
.raven-workbench button.rw-settings-nav-item:focus-visible {
  outline: 2px solid var(--rw-ink);
  outline-offset: 2px;
}
.rw-settings-nav-icon {
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  border-radius: 8px;
  display: grid;
  place-items: center;
  color: var(--rw-muted);
  background: var(--rw-canvas);
  border: 1px solid var(--rw-hairline);
  transition: color .15s ease, border-color .15s ease;
}
.rw-settings-nav-item.active .rw-settings-nav-icon {
  color: var(--rw-ink);
  border-color: var(--rw-hairline-strong);
}
.rw-settings-nav-copy {
  min-width: 0;
  display: grid;
  gap: 1px;
}
.rw-settings-nav-copy strong {
  font-size: 12.5px;
  font-weight: 650;
  line-height: 1.35;
}
.rw-settings-nav-copy small {
  color: var(--rw-muted);
  font-size: 10.5px;
  line-height: 1.4;
  white-space: nowrap;
}
.rw-settings-form {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--rw-canvas);
}
.rw-settings-content {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 32px 38px 28px;
  scrollbar-width: thin;
  scrollbar-color: var(--rw-hairline-strong) transparent;
}
.rw-settings-pane {
  animation: rw-settings-pane-in .18s ease-out;
}
@keyframes rw-settings-pane-in {
  from { opacity: 0; transform: translateX(5px); }
  to { opacity: 1; transform: translateX(0); }
}
.rw-settings-pane-head {
  max-width: 520px;
  padding-bottom: 24px;
}
.rw-settings-eyebrow {
  display: block;
  margin-bottom: 6px;
  color: var(--rw-muted);
  font-family: var(--rw-mono);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.rw-settings-pane-head h4 {
  margin: 0;
  color: var(--rw-ink);
  font-size: 19px;
  font-weight: 650;
  letter-spacing: -.02em;
}
.rw-settings-pane-head p {
  margin: 7px 0 0;
  color: var(--rw-muted);
  font-size: 12px;
  line-height: 1.6;
}
.rw-settings-field-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 18px 16px;
}
.rw-form-field--wide {
  grid-column: 1 / -1;
}
.rw-settings-field-grid--agent {
  margin-top: 20px;
  transition: opacity .15s ease;
}
.rw-settings-field-grid--agent.disabled {
  opacity: .55;
}
.rw-setting-toggle-card {
  min-height: 86px;
  padding: 16px;
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 11px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 22px;
  cursor: pointer;
  transition: border-color .15s ease, background .15s ease;
}
.rw-setting-toggle-card:hover {
  border-color: var(--rw-muted-soft);
  background: var(--rw-canvas-soft);
}
.rw-setting-toggle-copy {
  display: grid;
  gap: 5px;
}
.rw-setting-toggle-copy strong {
  color: var(--rw-ink);
  font-size: 13px;
  font-weight: 600;
}
.rw-setting-toggle-copy small {
  max-width: 390px;
  color: var(--rw-muted);
  font-size: 11.5px;
  line-height: 1.5;
}
.rw-switch {
  position: relative;
  width: 38px;
  height: 22px;
  flex: 0 0 38px;
}
.rw-switch input {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
}
.rw-switch span {
  position: absolute;
  inset: 0;
  border-radius: 999px;
  background: var(--rw-muted-soft);
  box-shadow: inset 0 0 0 1px rgba(0,0,0,.08);
  transition: background .18s ease;
}
.rw-switch span::after {
  content: '';
  position: absolute;
  left: 3px;
  top: 3px;
  width: 16px;
  height: 16px;
  border-radius: 999px;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0,0,0,.22);
  transition: transform .18s cubic-bezier(.2,.8,.2,1);
}
.rw-switch input:checked + span {
  background: var(--rw-primary);
}
.rw-switch input:checked + span::after {
  transform: translateX(16px);
}
.rw-switch input:focus-visible + span {
  outline: 2px solid var(--rw-ink);
  outline-offset: 2px;
}
.rw-settings-note {
  margin: 18px 0 0;
  padding: 10px 12px;
  border-left: 2px solid var(--rw-hairline-strong);
  color: var(--rw-muted);
  background: var(--rw-canvas-soft);
  font-size: 11.5px;
  line-height: 1.55;
}
.rw-settings-actions {
  min-height: 68px;
  padding: 15px 22px;
  border-top: 1px solid var(--rw-hairline);
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 9px;
}
.rw-select {
  width: 100%;
  height: 38px;
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 8px;
  padding: 0 34px 0 12px;
  font-size: 13.5px;
  outline: none;
  background-color: var(--rw-canvas);
  color: var(--rw-ink);
  font-family: inherit;
  appearance: none;
  background-image: linear-gradient(45deg, transparent 50%, var(--rw-muted) 50%), linear-gradient(135deg, var(--rw-muted) 50%, transparent 50%);
  background-position: calc(100% - 17px) 16px, calc(100% - 12px) 16px;
  background-size: 5px 5px, 5px 5px;
  background-repeat: no-repeat;
}
.rw-select:focus {
  border-color: var(--rw-ink);
}
.rw-input:disabled,
.rw-select:disabled {
  background: var(--rw-canvas-soft);
  color: var(--rw-muted);
  cursor: not-allowed;
}
.rw-settings-permission {
  margin-top: auto;
  min-height: 38px;
  padding: 0 8px;
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--rw-muted);
  font-size: 11px;
}
.rw-settings-permission strong {
  margin-left: auto;
  color: var(--rw-body);
  font-size: 11px;
  font-weight: 600;
}
.raven-workbench button.rw-save-profile-btn {
  display: inline-flex;
  align-items: center;
  gap: 7px;
}
.rw-modal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.rw-modal-title { font-size: 16px; font-weight: 600; color: var(--rw-ink); margin: 0; }
.rw-modal-sub { font-size: 12px; color: var(--rw-muted); margin: 4px 0 0; }
.rw-modal-close { width: 28px; height: 28px; border-radius: 6px; display: grid; place-items: center; color: var(--rw-body); }
.rw-modal-close:hover { background: var(--rw-surface-strong); color: var(--rw-ink); }
.rw-auth-tabs {
  margin-top: 18px;
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 3px;
  padding: 3px;
  background: var(--rw-surface-strong);
  border-radius: 8px;
}
.rw-auth-tabs button {
  height: 30px;
  border-radius: 6px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--rw-body);
}
.rw-auth-tabs button.active {
  background: var(--rw-canvas);
  color: var(--rw-ink);
  box-shadow: 0 1px 2px rgba(0,0,0,.08);
}
.rw-modal-form { margin-top: 18px; display: flex; flex-direction: column; gap: 14px; }
.rw-form-field { display: flex; flex-direction: column; gap: 6px; }
.rw-form-label { font-size: 12px; color: var(--rw-body); font-weight: 500; }
.rw-input {
  width: 100%; height: 38px;
  border: 1px solid var(--rw-hairline-strong); border-radius: 8px;
  padding: 0 12px; font-size: 13.5px; outline: none;
  background: var(--rw-canvas);
}
.rw-input:focus { border-color: var(--rw-ink); }
.rw-form-field.has-error .rw-input {
  border-color: var(--rw-danger);
  background: rgba(192,56,43,.025);
}
.rw-form-field.has-error .rw-input:focus {
  border-color: var(--rw-danger);
  box-shadow: 0 0 0 3px rgba(192,56,43,.09);
}
.rw-form-error {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  color: var(--rw-danger);
  font-size: 11.5px;
  line-height: 1.45;
  animation: rw-form-error-in .14s ease-out;
}
.rw-form-error::before {
  content: '';
  width: 4px;
  height: 4px;
  margin-top: 6px;
  flex: 0 0 4px;
  border-radius: 999px;
  background: currentColor;
}
.rw-form-alert {
  padding: 9px 11px;
  border-left: 2px solid var(--rw-danger);
  border-radius: 0 7px 7px 0;
  background: rgba(192,56,43,.055);
  color: var(--rw-danger);
  font-size: 11.5px;
  line-height: 1.5;
  animation: rw-form-error-in .14s ease-out;
}
.rw-login-notice {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.rw-login-notice-title {
  font-weight: 600;
}
.rw-login-notice-body {
  white-space: pre-wrap;
  word-break: break-word;
}
@keyframes rw-form-error-in {
  from { opacity: 0; transform: translateY(-2px); }
  to { opacity: 1; transform: translateY(0); }
}
.rw-modal-actions { display: flex; gap: 10px; padding-top: 4px; }
.raven-workbench button.rw-btn-primary {
  height: 36px; padding: 0 16px;
  background: var(--rw-primary); color: var(--rw-on-primary);
  border-radius: 8px; font-size: 13.5px; font-weight: 500;
  transition: background .15s, color .15s;
}
.raven-workbench button.rw-btn-primary:hover:not(:disabled) {
  background: var(--rw-primary-hover);
  color: var(--rw-on-primary);
}
.raven-workbench button.rw-btn-primary:active:not(:disabled) {
  background: var(--rw-primary-active);
  color: var(--rw-on-primary);
}
.rw-btn-primary:disabled { opacity: .6; cursor: default; }
.rw-btn-ghost {
  height: 36px; padding: 0 14px;
  background: var(--rw-canvas); color: var(--rw-ink);
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 8px; font-size: 13.5px; font-weight: 500;
}
.rw-btn-ghost:hover { background: var(--rw-surface-strong); }

/* ---------- Scrollbar ---------- */
.rw-chat-list::-webkit-scrollbar { width: 10px; height: 10px; }
.rw-chat-list::-webkit-scrollbar-track { background: transparent; }
.rw-chat-list::-webkit-scrollbar-thumb {
  background: #e6e6ea; border-radius: 999px; border: 2px solid var(--rw-canvas);
}
.rw-chat-list::-webkit-scrollbar-thumb:hover { background: var(--rw-muted-soft); }

/* ---------- Responsive ---------- */
@media (max-width: 900px) {
  .rw-sidebar { width: 240px; }
}

@media (max-width: 720px) {
  .raven-workbench { position: relative; }
  .rw-mobile-search-bar { display: flex; align-items: center; justify-content: space-between; padding: 6px 16px; flex-shrink: 0; border-bottom: 1px solid var(--rw-hairline); font-weight: 600; }
  .rw-mobile-search-bar + * { min-height: 0; flex: 1; }
  .rw-sidebar {
    position: absolute; left: 0; top: 0; bottom: 0;
    width: min(82vw, 320px);
    z-index: 40;
    box-shadow: 0 10px 30px rgba(15, 23, 42, 0.12);
    transform: translateX(-100%);
    transition: transform .25s ease;
  }
  .rw-modal.rw-settings-modal {
    min-height: min(620px, calc(100vh - 16px));
    max-height: calc(100vh - 16px);
  }
  .rw-settings-modal > .rw-modal-head {
    min-height: 70px;
    padding: 15px 17px;
  }
  .rw-settings-layout {
    grid-template-columns: 1fr;
    grid-template-rows: auto minmax(0, 1fr);
  }
  .rw-settings-sidebar {
    padding: 9px 12px;
    border-right: 0;
    border-bottom: 1px solid var(--rw-hairline);
  }
  .rw-settings-account-strip,
  .rw-settings-permission {
    display: none;
  }
  .rw-settings-nav {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 7px;
    padding: 0;
  }
  .raven-workbench button.rw-settings-nav-item {
    min-height: 46px;
    padding: 7px 9px;
  }
  .rw-settings-nav-icon {
    width: 28px;
    height: 28px;
  }
  .rw-settings-nav-copy small {
    display: none;
  }
  .rw-settings-content {
    padding: 24px 20px 22px;
  }
  .rw-settings-pane-head {
    padding-bottom: 20px;
  }
  .rw-settings-actions {
    min-height: 62px;
    padding: 12px 16px;
  }
}

@media (max-width: 520px) {
  .rw-modal-backdrop {
    padding: 8px;
  }
  .rw-settings-field-grid {
    grid-template-columns: 1fr;
    gap: 15px;
  }
  .rw-form-field--wide {
    grid-column: auto;
  }
  .rw-setting-toggle-card {
    padding: 14px;
    gap: 14px;
  }
  .rw-settings-pane-head h4 {
    font-size: 17px;
  }
}
</style>
