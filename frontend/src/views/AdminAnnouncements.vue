<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import {
  ArrowLeft,
  CircleStop,
  Clock3,
  Eye,
  Image as ImageIcon,
  LogOut,
  Megaphone,
  Menu,
  PanelLeftClose,
  PencilLine,
  RefreshCw,
  Send,
  ShieldCheck,
  Sparkles,
  Type,
} from 'lucide-vue-next'
import { adminApi, adminToken } from '@/api/admin'
import SystemAnnouncementDialog from '@/components/SystemAnnouncementDialog.vue'
import ThemeToggle from '@/components/ThemeToggle.vue'
import { useAdminScope } from '@/composables/useAdminScope'
import { useAppStore } from '@/stores/app'
import type { AnnouncementDraft, SystemAnnouncement } from '@/types'
import { resolveAdminNavKey, type AdminNavItem } from '@/utils/adminNav'
import { renderAnnouncementMarkdown } from '@/utils/markdownRenderer'

const { t, locale } = useI18n()
const appStore = useAppStore()
const route = useRoute()
const router = useRouter()
const { visibleNavItems } = useAdminScope()

const isAuthenticated = ref(false)
const isLoggingIn = ref(false)
const loading = ref(false)
const publishing = ref(false)
const deactivating = ref(false)
const readingCover = ref(false)
const current = ref<SystemAnnouncement | null>(null)
const editorMode = ref<'write' | 'preview'>('write')
const previewOpen = ref(false)
const coverTypes = ['image/png', 'image/jpeg', 'image/webp']
const maxCoverBytes = 2 * 1024 * 1024

const authForm = reactive({ username: '', password: '' })
const form = reactive<Required<AnnouncementDraft>>({
  title: '',
  content: '',
  layout: 'text',
  cover_image: null,
  cover_alt: '',
  fireworks: false,
})

const navVisible = computed(() => appStore.adminSidebarVisible)
const activeNavKey = computed(() => resolveAdminNavKey(route.path))
const isActive = computed(() => Boolean(current.value?.active))
const contentLength = computed(() => form.content.length)
const draftPreviewHtml = computed(() => (
  form.content.trim() ? renderAnnouncementMarkdown(form.content) : ''
))
const currentContentHtml = computed(() => (
  current.value ? renderAnnouncementMarkdown(current.value.content) : ''
))
const canPublish = computed(() => (
  form.title.trim().length > 0
  && form.content.trim().length > 0
  && form.title.trim().length <= 120
  && contentLength.value <= 4000
  && (form.layout === 'text' || !!form.cover_image)
  && !busy.value
))
const busy = computed(() => loading.value || publishing.value || deactivating.value || readingCover.value)

const parseErrorMessage = (error: any): string => {
  const detail = error?.response?.data?.detail || error?.response?.data?.message
  if (typeof detail === 'string') return detail
  return error?.message || t('admin.parseError')
}

const loadDraft = (announcement: SystemAnnouncement) => {
  form.title = announcement.title
  form.content = announcement.content
  form.layout = announcement.layout ?? 'text'
  form.cover_image = announcement.cover_image ?? null
  form.cover_alt = announcement.cover_alt ?? ''
  form.fireworks = announcement.fireworks ?? false
}

const selectCover = async (event: Event) => {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file || busy.value) return

  readingCover.value = true
  try {
    if (!coverTypes.includes(file.type)) throw new Error(t('admin.announcements.coverInvalid'))
    if (file.size > maxCoverBytes) throw new Error(t('admin.announcements.coverTooLarge'))
    const source = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.onerror = () => reject(new Error(t('admin.announcements.coverInvalid')))
      reader.readAsDataURL(file)
    })
    const image = new Image()
    image.src = source
    try {
      await image.decode()
    } catch {
      throw new Error(t('admin.announcements.coverInvalid'))
    }
    form.cover_image = source
  } catch (error) {
    appStore.showNotification({
      title: t('admin.announcements.coverFailed'),
      message: parseErrorMessage(error),
      type: 'error',
    })
  } finally {
    readingCover.value = false
  }
}

const formatPublishedAt = (value?: string) => {
  if (!value) return '--'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat(locale.value === 'en' ? 'en' : 'zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

const clearAuth = () => {
  adminToken.clear()
  isAuthenticated.value = false
  current.value = null
  authForm.password = ''
}

const handleNavClick = (item: AdminNavItem) => {
  if (item.path && route.path !== item.path) router.push(item.path)
}

const toggleNavVisibility = () => appStore.toggleAdminSidebar()

const loadAnnouncement = async () => {
  if (!isAuthenticated.value) return
  loading.value = true
  try {
    const resp = await adminApi.getCurrentAnnouncement()
    if (!resp?.success) throw new Error(resp?.message || t('admin.announcements.loadFailed'))
    current.value = resp.data ?? null
    if (current.value) loadDraft(current.value)
  } catch (error: any) {
    appStore.showNotification({
      title: t('admin.announcements.loadFailed'),
      message: parseErrorMessage(error),
      type: 'error',
    })
  } finally {
    loading.value = false
  }
}

const handleLogin = async () => {
  if (!authForm.username || !authForm.password) {
    appStore.showNotification({ title: t('admin.loginWarning'), type: 'warning' })
    return
  }
  isLoggingIn.value = true
  try {
    const resp = await adminApi.login(authForm.username.trim(), authForm.password)
    if (!resp?.success || !resp.data) {
      throw new Error(resp?.message || t('admin.loginFailFallback'))
    }
    adminToken.set(resp.data.token)
    isAuthenticated.value = true
    appStore.showNotification({
      title: t('admin.loginSuccessTitle'),
      message: t('admin.loginSuccessMsg', { username: resp.data.username }),
      type: 'success',
    })
    await loadAnnouncement()
  } catch (error: any) {
    appStore.showNotification({
      title: t('admin.loginFailFallback'),
      message: parseErrorMessage(error),
      type: 'error',
    })
  } finally {
    isLoggingIn.value = false
  }
}

const handleLogout = async () => {
  try {
    await adminApi.logout()
  } catch {
    // Admin logout is client-side; token clearing remains authoritative here.
  } finally {
    clearAuth()
    appStore.showNotification({ title: t('admin.logoutSuccessTitle'), type: 'info' })
  }
}

const publishAnnouncement = async () => {
  if (!canPublish.value) return
  if (current.value && !window.confirm(t('admin.announcements.publishConfirm'))) return

  publishing.value = true
  try {
    const resp = await adminApi.publishAnnouncement({
      title: form.title.trim(),
      content: form.content.trim(),
      layout: form.layout,
      ...(form.layout === 'cover' && {
        cover_image: form.cover_image,
        cover_alt: form.cover_alt.trim(),
        fireworks: form.fireworks,
      }),
    })
    if (!resp?.success || !resp.data) {
      throw new Error(resp?.message || t('admin.announcements.publishFailed'))
    }
    current.value = resp.data
    loadDraft(resp.data)
    appStore.showNotification({
      title: t('admin.announcements.publishSuccess'),
      type: 'success',
    })
  } catch (error: any) {
    appStore.showNotification({
      title: t('admin.announcements.publishFailed'),
      message: parseErrorMessage(error),
      type: 'error',
    })
  } finally {
    publishing.value = false
  }
}

const deactivateAnnouncement = async () => {
  if (!isActive.value || deactivating.value) return
  if (!window.confirm(t('admin.announcements.deactivateConfirm'))) return

  deactivating.value = true
  try {
    const resp = await adminApi.deactivateAnnouncement()
    if (!resp?.success || !resp.data) {
      throw new Error(resp?.message || t('admin.announcements.deactivateFailed'))
    }
    current.value = resp.data
    appStore.showNotification({
      title: t('admin.announcements.deactivateSuccess'),
      type: 'success',
    })
  } catch (error: any) {
    appStore.showNotification({
      title: t('admin.announcements.deactivateFailed'),
      message: parseErrorMessage(error),
      type: 'error',
    })
  } finally {
    deactivating.value = false
  }
}

const bootstrap = async () => {
  if (!adminToken.get()) return
  try {
    const resp = await adminApi.me()
    if (!resp?.success || resp.data?.access_level !== 'global_admin') {
      clearAuth()
      return
    }
    isAuthenticated.value = true
    await loadAnnouncement()
  } catch {
    clearAuth()
  }
}

onMounted(() => bootstrap())
</script>

<template>
  <div class="admin-console admin-announcements-page">
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
            :title="navVisible ? t('admin.toggleSidebarHide') : t('admin.toggleSidebarShow')"
            :aria-label="t('admin.toggleSidebarAriaLabel')"
            @click="toggleNavVisibility"
          >
            <PanelLeftClose v-if="navVisible" :size="18" />
            <Menu v-else :size="18" />
          </button>
          <div>
            <h1 class="admin-title">{{ t('admin.title') }}</h1>
            <p class="admin-subtitle">{{ t('admin.announcements.subtitle') }}</p>
          </div>
        </div>
        <div class="admin-topbar-right">
          <ThemeToggle class="admin-theme-toggle" />
          <span class="admin-status-badge" :class="{ live: isActive }">
            <span class="admin-status-dot"></span>
            {{ isAuthenticated
              ? (isActive ? t('admin.announcements.badgeActive') : t('admin.announcements.badgeInactive'))
              : t('admin.badgeNotLoggedIn') }}
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
      :aria-label="t('admin.closeSidebarAriaLabel')"
      @click="toggleNavVisibility"
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
            <ShieldCheck :size="22" class="text-cyan-600" />
          </div>
          <form class="space-y-4 max-w-md" @submit.prevent="handleLogin">
            <label class="block">
              <span class="text-sm text-slate-700">{{ t('admin.usernameLabel') }}</span>
              <input v-model="authForm.username" type="text" class="admin-input" placeholder="admin" autocomplete="username" />
            </label>
            <label class="block">
              <span class="text-sm text-slate-700">{{ t('admin.passwordLabel') }}</span>
              <input v-model="authForm.password" type="password" class="admin-input" placeholder="••••••••" autocomplete="current-password" />
            </label>
            <button type="submit" class="primary-btn" :disabled="isLoggingIn">
              {{ isLoggingIn ? t('admin.loginBtnLoading') : t('admin.loginBtn') }}
            </button>
          </form>
        </div>
      </section>

      <section v-else class="announcement-workspace">
        <div class="announcement-hero">
          <div class="announcement-hero-copy">
            <div class="announcement-hero-icon"><Megaphone :size="22" /></div>
            <div>
              <h2>{{ t('admin.announcements.pageTitle') }}</h2>
              <p>{{ t('admin.announcements.pageDesc') }}</p>
            </div>
          </div>
          <button class="refresh-btn" :disabled="busy" @click="loadAnnouncement">
            <RefreshCw :size="15" :class="{ spin: loading }" />
            {{ t('common.refresh') }}
          </button>
        </div>

        <div class="announcement-grid">
          <article class="announcement-editor panel-card">
            <div class="panel-heading">
              <div>
                <span class="panel-index">01 / COMPOSE</span>
                <h3>{{ t('admin.announcements.editorTitle') }}</h3>
              </div>
              <Send :size="19" class="text-cyan-600" />
            </div>

            <form class="editor-form" @submit.prevent="publishAnnouncement">
              <fieldset class="layout-field" :disabled="busy">
                <legend>{{ t('admin.announcements.layoutLabel') }}</legend>
                <div class="layout-options">
                  <label class="layout-option">
                    <input v-model="form.layout" type="radio" name="announcement-layout" value="text" />
                    <Type :size="17" aria-hidden="true" />
                    <span>{{ t('admin.announcements.layoutText') }}</span>
                  </label>
                  <label class="layout-option">
                    <input v-model="form.layout" type="radio" name="announcement-layout" value="cover" />
                    <ImageIcon :size="17" aria-hidden="true" />
                    <span>{{ t('admin.announcements.layoutCover') }}</span>
                  </label>
                </div>
              </fieldset>

              <div v-if="form.layout === 'cover'" class="cover-fields">
                <img
                  v-if="form.cover_image"
                  :src="form.cover_image"
                  :alt="form.cover_alt"
                  class="cover-thumbnail"
                />
                <label class="cover-file-label">
                  <span>{{ t('admin.announcements.coverLabel') }}</span>
                  <input
                    class="cover-file"
                    type="file"
                    :accept="coverTypes.join(',')"
                    :disabled="busy"
                    aria-describedby="announcement-cover-hint"
                    @change="selectCover"
                  />
                </label>
                <p id="announcement-cover-hint" class="cover-hint">
                  {{ t('admin.announcements.coverHint') }}
                </p>
                <button
                  v-if="form.cover_image"
                  class="cover-remove"
                  type="button"
                  :disabled="busy"
                  @click="form.cover_image = null"
                >
                  {{ t('admin.announcements.coverRemove') }}
                </button>
                <label>
                  <span>{{ t('admin.announcements.coverAlt') }}</span>
                  <input v-model="form.cover_alt" class="admin-input" type="text" maxlength="240" />
                </label>
                <label class="fireworks-field">
                  <input v-model="form.fireworks" type="checkbox" />
                  <Sparkles :size="15" aria-hidden="true" />
                  <span>{{ t('admin.announcements.fireworks') }}</span>
                </label>
              </div>

              <label>
                <span>{{ t('admin.announcements.titleLabel') }}</span>
                <input
                  v-model="form.title"
                  class="admin-input"
                  type="text"
                  maxlength="120"
                  :placeholder="t('admin.announcements.titlePlaceholder')"
                />
              </label>
              <div class="content-field">
                <div class="content-field-head">
                  <label id="announcement-content-label" for="announcement-content">
                    {{ t('admin.announcements.contentLabel') }}
                  </label>
                  <span class="markdown-badge">{{ t('admin.announcements.markdownSupported') }}</span>
                </div>

                <div class="markdown-editor-shell">
                  <div
                    class="editor-tabs"
                    role="tablist"
                    :aria-label="t('admin.announcements.editorTabsLabel')"
                  >
                    <button
                      id="announcement-write-tab"
                      type="button"
                      class="editor-tab"
                      :class="{ active: editorMode === 'write' }"
                      role="tab"
                      :aria-selected="editorMode === 'write'"
                      aria-controls="announcement-content"
                      @click="editorMode = 'write'"
                    >
                      <PencilLine :size="14" />
                      {{ t('admin.announcements.writeTab') }}
                    </button>
                    <button
                      id="announcement-preview-tab"
                      type="button"
                      class="editor-tab"
                      :class="{ active: editorMode === 'preview' }"
                      role="tab"
                      :aria-selected="editorMode === 'preview'"
                      aria-controls="announcement-preview-panel"
                      @click="editorMode = 'preview'"
                    >
                      <Eye :size="14" />
                      {{ t('admin.announcements.previewTab') }}
                    </button>
                  </div>

                  <textarea
                    v-show="editorMode === 'write'"
                    id="announcement-content"
                    v-model="form.content"
                    class="admin-textarea markdown-textarea"
                    maxlength="4000"
                    rows="12"
                    :placeholder="t('admin.announcements.contentPlaceholder')"
                    role="tabpanel"
                    aria-labelledby="announcement-write-tab announcement-content-label"
                  ></textarea>
                  <div
                    v-show="editorMode === 'preview'"
                    id="announcement-preview-panel"
                    class="markdown-preview"
                    role="tabpanel"
                    tabindex="0"
                    aria-labelledby="announcement-preview-tab"
                  >
                    <div v-if="draftPreviewHtml" v-html="draftPreviewHtml"></div>
                    <div v-else class="markdown-preview-empty">
                      <Eye :size="22" />
                      <span>{{ t('admin.announcements.previewEmpty') }}</span>
                    </div>
                  </div>
                </div>

                <div class="editor-meta-row">
                  <span class="format-hint">{{ t('admin.announcements.markdownHint') }}</span>
                  <small :class="{ limit: contentLength >= 3900 }">
                    {{ t('admin.announcements.charCount', { count: contentLength }) }}
                  </small>
                </div>
              </div>

              <div v-if="current" class="replace-hint">
                <Clock3 :size="16" />
                <span>{{ t('admin.announcements.replaceHint') }}</span>
              </div>

              <div class="editor-actions">
                <button
                  type="button"
                  class="preview-dialog-btn"
                  :disabled="!canPublish"
                  @click="previewOpen = true"
                >
                  <Eye :size="16" />
                  {{ t('admin.announcements.previewDialog') }}
                </button>
                <button type="submit" class="publish-btn" :disabled="!canPublish">
                  <Send :size="16" />
                  {{ publishing ? t('admin.announcements.publishing') : t('admin.announcements.publish') }}
                </button>
              </div>
            </form>
          </article>

          <aside class="current-panel panel-card">
            <div class="panel-heading">
              <div>
                <span class="panel-index">02 / STATUS</span>
                <h3>{{ t('admin.announcements.currentTitle') }}</h3>
              </div>
              <span v-if="current" class="current-state" :class="{ live: current.active }">
                {{ current.active ? t('admin.announcements.currentActive') : t('admin.announcements.currentInactive') }}
              </span>
            </div>

            <div v-if="loading" class="current-empty">
              <RefreshCw :size="24" class="spin" />
            </div>
            <div v-else-if="!current" class="current-empty">
              <div class="empty-icon"><Megaphone :size="25" /></div>
              <h4>{{ t('admin.announcements.noCurrentTitle') }}</h4>
              <p>{{ t('admin.announcements.noCurrentDesc') }}</p>
            </div>
            <div v-else class="current-card" :class="{ inactive: !current.active }">
              <div class="current-card-stripe"></div>
              <img
                v-if="current.layout === 'cover' && current.cover_image"
                :src="current.cover_image"
                :alt="current.cover_alt ?? ''"
                class="current-cover"
              />
              <h4>{{ current.title }}</h4>
              <div class="current-content" v-html="currentContentHtml"></div>
              <div class="current-meta">
                <span>{{ t('admin.announcements.publishedBy', { name: current.published_by }) }}</span>
                <time :datetime="current.published_at">{{ formatPublishedAt(current.published_at) }}</time>
              </div>
              <button
                v-if="current.active"
                class="deactivate-btn"
                :disabled="busy"
                @click="deactivateAnnouncement"
              >
                <CircleStop :size="15" />
                {{ deactivating ? t('admin.announcements.deactivating') : t('admin.announcements.deactivate') }}
              </button>
            </div>

            <div class="persistence-note">
              <ShieldCheck :size="16" />
              <span>{{ t('admin.announcements.statusNote') }}</span>
            </div>
          </aside>
        </div>
      </section>
    </main>
    <SystemAnnouncementDialog v-if="previewOpen" :preview="form" @close-preview="previewOpen = false" />
  </div>
</template>

<style scoped>
.admin-console {
  --admin-topbar-height: 72px;
  --admin-sidebar-width: 280px;
  min-height: 100vh;
  background: var(--admin-page-bg);
}
.admin-topbar { position: fixed; inset: 0 0 auto; height: var(--admin-topbar-height); z-index: 70; background: rgba(15,23,42,.97); border-bottom: 1px solid rgba(148,163,184,.3); backdrop-filter: blur(10px); }
.admin-topbar-inner { height: 100%; padding: 0 1rem; display: flex; align-items: center; justify-content: space-between; gap: .75rem; }
.admin-topbar-left, .admin-topbar-right { display: flex; align-items: center; gap: .75rem; min-width: 0; }
.admin-icon-btn { display: grid; place-items: center; width: 2.25rem; height: 2.25rem; border: 1px solid rgba(148,163,184,.35); border-radius: .625rem; color: #f8fafc; background: rgba(51,65,85,.6); }
.admin-icon-btn:disabled { opacity: .45; cursor: not-allowed; }
.admin-title { color: #f8fafc; font-size: .95rem; font-weight: 700; line-height: 1.1; }
.admin-subtitle { color: #94a3b8; font-size: .75rem; }
.admin-logout-btn { border: 1px solid rgba(148,163,184,.35); border-radius: .55rem; color: #e2e8f0; background: rgba(51,65,85,.45); font-size: .75rem; font-weight: 600; padding: .45rem .7rem; display: inline-flex; align-items: center; gap: .35rem; }
.admin-status-badge { display: inline-flex; align-items: center; gap: .4rem; padding: .3rem .65rem; color: #cbd5e1; background: rgba(51,65,85,.7); border-radius: 999px; font-size: .69rem; font-weight: 700; }
.admin-status-dot { width: 6px; height: 6px; background: #64748b; border-radius: 50%; }
.admin-status-badge.live { color: #a5f3fc; }
.admin-status-badge.live .admin-status-dot { background: #22d3ee; box-shadow: 0 0 0 4px rgba(34,211,238,.12); }
.admin-sidebar { position: fixed; left: 0; top: 0; width: var(--admin-sidebar-width); height: 100vh; z-index: 60; padding: calc(var(--admin-topbar-height) + 1rem) 1rem 1rem; overflow-y: auto; background: #0f172a; border-right: 1px solid rgba(148,163,184,.25); transition: transform .25s ease; }
.admin-sidebar.is-hidden { transform: translateX(calc(-1 * var(--admin-sidebar-width))); }
.admin-side-nav-item { width: 100%; padding: .8rem; text-align: left; color: #cbd5e1; background: rgba(30,41,59,.45); border: 1px solid rgba(100,116,139,.45); border-radius: .75rem; }
.admin-side-nav-item.is-active { color: #0f172a; background: #22d3ee; border-color: #22d3ee; }
.admin-main { min-height: 100vh; padding: calc(var(--admin-topbar-height) + 1rem) 1rem 1.5rem calc(var(--admin-sidebar-width) + 1rem); transition: padding-left .25s ease; }
.admin-main.is-sidebar-hidden { padding-left: 1rem; }
.admin-login-wrap { max-width: 720px; margin: 1.25rem auto 0; }
.admin-sidebar-backdrop { display: none; }
.admin-input, .admin-textarea { width: 100%; margin-top: .4rem; padding: .7rem .8rem; color: var(--admin-ink); background: var(--admin-surface); border: 1px solid var(--admin-hairline-strong); border-radius: .65rem; outline: none; font-size: .87rem; transition: border-color .15s, box-shadow .15s; }
.admin-input:focus, .admin-textarea:focus { border-color: #06b6d4; box-shadow: 0 0 0 3px rgba(6,182,212,.12); }
.admin-textarea { resize: vertical; min-height: 220px; line-height: 1.65; }
.primary-btn, .publish-btn { display: inline-flex; align-items: center; justify-content: center; gap: .45rem; padding: .67rem 1rem; color: #083344; background: #22d3ee; border: 0; border-radius: .65rem; font-size: .82rem; font-weight: 800; }
.primary-btn:disabled, .publish-btn:disabled { opacity: .48; cursor: not-allowed; }
.announcement-workspace { width: 100%; }
.announcement-hero { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-bottom: 1rem; padding: 1.15rem 1.25rem; color: #e2e8f0; background: #111c2f; border: 1px solid rgba(148,163,184,.2); border-radius: 16px; box-shadow: 0 12px 32px rgba(15,23,42,.12); }
.announcement-hero-copy { display: flex; align-items: center; gap: .9rem; }
.announcement-hero-icon { display: grid; width: 42px; height: 42px; place-items: center; flex: 0 0 auto; color: #083344; background: #22d3ee; border-radius: 12px; }
.announcement-hero h2 { margin: 0; color: #f8fafc; font-size: 1.05rem; font-weight: 750; }
.announcement-hero p { max-width: 760px; margin: .2rem 0 0; color: #94a3b8; font-size: .78rem; line-height: 1.5; }
.refresh-btn { display: inline-flex; align-items: center; gap: .35rem; flex: 0 0 auto; padding: .5rem .65rem; color: #cbd5e1; background: rgba(51,65,85,.45); border: 1px solid rgba(148,163,184,.22); border-radius: .55rem; font-size: .75rem; font-weight: 650; }
.announcement-grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(320px, .85fr); gap: 1rem; align-items: start; }
.panel-card { overflow: hidden; background: var(--admin-surface-translucent); border: 1px solid var(--admin-hairline); border-radius: 16px; box-shadow: 0 10px 26px rgba(15,23,42,.06); }
.panel-heading { display: flex; align-items: center; justify-content: space-between; gap: .75rem; padding: 1rem 1.1rem; border-bottom: 1px solid var(--admin-hairline); }
.panel-index { display: block; margin-bottom: .15rem; color: var(--admin-accent-soft-ink); font-size: .62rem; font-weight: 850; letter-spacing: .12em; }
.panel-heading h3 { margin: 0; color: var(--admin-ink); font-size: .98rem; font-weight: 750; }
.editor-form { display: grid; gap: 1rem; padding: 1.15rem; }
.editor-form label > span { color: var(--admin-body); font-size: .78rem; font-weight: 700; }
.layout-field { min-width: 0; margin: 0; padding: 0; border: 0; }
.layout-field legend { margin-bottom: .45rem; color: var(--admin-body); font-size: .78rem; font-weight: 700; }
.layout-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .55rem; }
.layout-option {
  display: flex;
  align-items: center;
  gap: .45rem;
  padding: .65rem .55rem;
  color: var(--admin-body);
  background: var(--admin-surface);
  border: 1px solid var(--admin-hairline-strong);
  border-radius: .65rem;
  cursor: pointer;
}
.layout-option:has(:checked) {
  color: var(--admin-accent-soft-ink);
  background: var(--admin-accent-soft-bg);
  border-color: var(--admin-accent-soft-border);
}
.layout-option svg, .layout-option input { flex: none; }
.layout-option input, .fireworks-field input { accent-color: #0891b2; }
.cover-fields {
  display: grid;
  min-width: 0;
  gap: .6rem;
  padding: .8rem;
  background: var(--admin-canvas-soft);
  border: 1px solid var(--admin-hairline);
  border-radius: .7rem;
}
.cover-thumbnail, .current-cover {
  display: block;
  width: 100%;
  max-height: 180px;
  aspect-ratio: 2 / 1;
  object-fit: contain;
  background: #080e19;
  border-radius: .5rem;
}
.current-cover { margin-bottom: .85rem; }
.cover-file-label { display: grid; gap: .4rem; min-width: 0; }
.cover-file { width: 100%; color: var(--admin-body); font-size: .76rem; }
.cover-file::file-selector-button {
  margin-right: .5rem;
  padding: .35rem .55rem;
  color: var(--admin-ink);
  background: var(--admin-surface);
  border: 1px solid var(--admin-hairline-strong);
  border-radius: .4rem;
  cursor: pointer;
}
.cover-hint { margin: 0; color: var(--admin-muted); font-size: .72rem; line-height: 1.45; }
.cover-remove { justify-self: start; color: var(--admin-body); font-size: .73rem; text-decoration: underline; }
.fireworks-field { display: flex; align-items: center; gap: .45rem; color: var(--admin-accent-soft-ink); }
.editor-actions { display: flex; flex-wrap: wrap; gap: .55rem; }
.editor-actions .publish-btn { flex: 1 1 auto; width: auto; }
.preview-dialog-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: .4rem;
  padding: .6rem .8rem;
  color: var(--admin-body);
  background: var(--admin-surface);
  border: 1px solid var(--admin-hairline-strong);
  border-radius: .65rem;
  font-size: .8rem;
  font-weight: 700;
}
.preview-dialog-btn:disabled { opacity: .55; cursor: not-allowed; }
.editor-form small { display: block; margin-top: .35rem; color: var(--admin-muted); text-align: right; font-size: .68rem; }
.editor-form small.limit { color: #e11d48; }
.content-field { display: grid; gap: .45rem; }
.content-field-head, .editor-meta-row { display: flex; align-items: center; justify-content: space-between; gap: .75rem; }
.content-field-head label { color: var(--admin-body); font-size: .78rem; font-weight: 700; }
.markdown-badge { padding: .2rem .48rem; color: var(--admin-accent-soft-ink); background: var(--admin-accent-soft-bg); border: 1px solid var(--admin-accent-soft-border); border-radius: 999px; font-size: .62rem; font-weight: 800; letter-spacing: .02em; }
.markdown-editor-shell { overflow: hidden; background: var(--admin-surface); border: 1px solid var(--admin-hairline-strong); border-radius: .72rem; transition: border-color .15s, box-shadow .15s; }
.markdown-editor-shell:focus-within { border-color: #06b6d4; box-shadow: 0 0 0 3px rgba(6,182,212,.12); }
.editor-tabs { display: flex; gap: .25rem; padding: .35rem; background: var(--admin-canvas-soft); border-bottom: 1px solid var(--admin-hairline); }
.editor-tab { display: inline-flex; align-items: center; gap: .35rem; min-height: 32px; padding: .4rem .68rem; color: var(--admin-body); background: transparent; border: 0; border-radius: .48rem; font-size: .72rem; font-weight: 750; transition: color .15s, background .15s, box-shadow .15s; }
.editor-tab:hover { color: var(--admin-ink); background: var(--admin-surface-strong); }
.editor-tab.active { color: var(--admin-ink); background: var(--admin-surface); box-shadow: 0 1px 3px rgba(15,23,42,.1), inset 0 0 0 1px var(--admin-hairline); }
.editor-tab:focus-visible { outline: 2px solid #0891b2; outline-offset: 1px; }
.markdown-textarea.admin-textarea { min-height: 300px; margin: 0; padding: 1rem; color: var(--admin-ink); background: var(--admin-surface); border: 0; border-radius: 0; box-shadow: none; font-family: var(--admin-code-font, ui-monospace, monospace); font-size: .8rem; line-height: 1.75; resize: vertical; }
.markdown-textarea.admin-textarea:focus { border: 0; outline: none; box-shadow: none; }
.markdown-preview { min-height: 300px; max-height: 430px; padding: 1rem 1.1rem; overflow-y: auto; color: var(--admin-body); background: var(--admin-surface); outline: none; scrollbar-color: var(--admin-hairline-strong) transparent; }
.markdown-preview:focus-visible { box-shadow: inset 0 0 0 2px #0891b2; }
.markdown-preview-empty { display: grid; min-height: 265px; place-items: center; align-content: center; gap: .55rem; color: #94a3b8; font-size: .75rem; }
.editor-meta-row { align-items: flex-start; }
.editor-meta-row small { flex: 0 0 auto; margin: 0; }
.format-hint { color: var(--admin-muted); font-family: var(--admin-code-font, ui-monospace, monospace); font-size: .64rem; line-height: 1.5; }
.replace-hint { display: flex; align-items: flex-start; gap: .55rem; padding: .75rem .8rem; color: var(--admin-body); background: var(--admin-accent-soft-bg); border: 1px solid var(--admin-accent-soft-border); border-radius: .7rem; font-size: .73rem; line-height: 1.5; }
.replace-hint svg { flex: 0 0 auto; margin-top: .12rem; color: #0891b2; }
.publish-btn { width: 100%; padding: .78rem 1rem; transition: transform .15s, background .15s; }
.publish-btn:hover:not(:disabled) { transform: translateY(-1px); background: #67e8f9; }
.current-state { padding: .25rem .55rem; color: var(--admin-body); background: var(--admin-surface-strong); border-radius: 999px; font-size: .67rem; font-weight: 800; }
.current-state.live { color: var(--admin-accent-soft-ink); background: var(--admin-accent-soft-bg); }
.current-empty { display: grid; min-height: 330px; place-items: center; align-content: center; padding: 2rem; color: var(--admin-muted); text-align: center; }
.empty-icon { display: grid; width: 56px; height: 56px; place-items: center; margin-bottom: .8rem; color: var(--admin-accent-soft-ink); background: var(--admin-accent-soft-bg); border: 1px solid var(--admin-accent-soft-border); border-radius: 16px; }
.current-empty h4 { margin: 0; color: var(--admin-body); font-size: .9rem; }
.current-empty p { max-width: 290px; margin: .45rem 0 0; font-size: .75rem; line-height: 1.55; }
.current-card { position: relative; margin: 1rem; padding: 1.2rem; overflow: hidden; color: #dbeafe; background: #111c2f; border-radius: 13px; box-shadow: 0 12px 28px rgba(15,23,42,.14); }
.current-card.inactive { filter: saturate(.55); opacity: .82; }
.current-card-stripe { position: absolute; inset: 0 auto 0 0; width: 3px; background: #22d3ee; }
.current-card h4 { margin: 0 0 .75rem; color: #f8fafc; font-size: 1.05rem; line-height: 1.4; overflow-wrap: anywhere; }
.current-content { max-height: 300px; overflow-y: auto; overflow-wrap: anywhere; color: #cbd5e1; font-size: .8rem; line-height: 1.7; scrollbar-color: #475569 transparent; }
.current-meta { display: flex; flex-wrap: wrap; justify-content: space-between; gap: .45rem; margin-top: 1rem; padding-top: .75rem; color: #64748b; border-top: 1px solid rgba(148,163,184,.16); font-size: .66rem; }
.deactivate-btn { display: inline-flex; align-items: center; gap: .38rem; width: 100%; justify-content: center; margin-top: .85rem; padding: .6rem; color: #fecdd3; background: rgba(190,24,93,.12); border: 1px solid rgba(251,113,133,.24); border-radius: .6rem; font-size: .73rem; font-weight: 750; }
.deactivate-btn:disabled { opacity: .5; cursor: wait; }
.persistence-note { display: flex; align-items: flex-start; gap: .5rem; margin: 0 1rem 1rem; padding: .75rem; color: var(--admin-body); background: var(--admin-canvas-soft); border: 1px solid var(--admin-hairline); border-radius: .7rem; font-size: .7rem; line-height: 1.5; }
.persistence-note svg { flex: 0 0 auto; color: #0891b2; }

.markdown-preview {
  --announcement-md-text: var(--admin-body);
  --announcement-md-heading: var(--admin-ink);
  --announcement-md-muted: var(--admin-muted);
  --announcement-md-border: var(--admin-hairline);
  --announcement-md-soft: var(--admin-canvas-soft);
  --announcement-md-accent: var(--admin-accent-soft-ink);
  --announcement-md-accent-soft: var(--admin-accent-soft-bg);
}
.current-content {
  --announcement-md-text: #cbd5e1;
  --announcement-md-heading: #f8fafc;
  --announcement-md-muted: #94a3b8;
  --announcement-md-border: rgba(148,163,184,.22);
  --announcement-md-soft: rgba(15,23,42,.5);
  --announcement-md-accent: #67e8f9;
  --announcement-md-accent-soft: rgba(34,211,238,.08);
}
.markdown-preview :deep(.announcement-markdown),
.current-content :deep(.announcement-markdown) { color: var(--announcement-md-text); font-size: inherit; line-height: 1.72; }
.markdown-preview :deep(.announcement-markdown > :first-child),
.current-content :deep(.announcement-markdown > :first-child) { margin-top: 0; }
.markdown-preview :deep(.announcement-markdown > :last-child),
.current-content :deep(.announcement-markdown > :last-child) { margin-bottom: 0; }
.markdown-preview :deep(.announcement-markdown h1),
.current-content :deep(.announcement-markdown h1) { margin: 1.1rem 0 .65rem; padding-bottom: .42rem; color: var(--announcement-md-heading); border-bottom: 1px solid var(--announcement-md-border); font-size: 1.28rem; font-weight: 750; line-height: 1.3; }
.markdown-preview :deep(.announcement-markdown h2),
.current-content :deep(.announcement-markdown h2) { margin: 1rem 0 .55rem; color: var(--announcement-md-heading); font-size: 1.08rem; font-weight: 750; line-height: 1.35; }
.markdown-preview :deep(.announcement-markdown h3),
.current-content :deep(.announcement-markdown h3) { margin: .9rem 0 .45rem; color: var(--announcement-md-heading); font-size: .95rem; font-weight: 750; }
.markdown-preview :deep(.announcement-markdown h4),
.markdown-preview :deep(.announcement-markdown h5),
.markdown-preview :deep(.announcement-markdown h6),
.current-content :deep(.announcement-markdown h4),
.current-content :deep(.announcement-markdown h5),
.current-content :deep(.announcement-markdown h6) { margin: .8rem 0 .4rem; color: var(--announcement-md-heading); font-size: .88rem; font-weight: 750; line-height: 1.4; }
.markdown-preview :deep(.announcement-markdown p),
.current-content :deep(.announcement-markdown p) { margin: .62rem 0; color: var(--announcement-md-text); }
.markdown-preview :deep(.announcement-markdown strong),
.current-content :deep(.announcement-markdown strong) { color: var(--announcement-md-heading); font-weight: 750; }
.markdown-preview :deep(.announcement-markdown ul),
.markdown-preview :deep(.announcement-markdown ol),
.current-content :deep(.announcement-markdown ul),
.current-content :deep(.announcement-markdown ol) { margin: .65rem 0; padding-left: 1.35rem; }
.markdown-preview :deep(.announcement-markdown ul),
.current-content :deep(.announcement-markdown ul) { list-style-type: disc; }
.markdown-preview :deep(.announcement-markdown ol),
.current-content :deep(.announcement-markdown ol) { list-style-type: decimal; }
.markdown-preview :deep(.announcement-markdown ul ul),
.current-content :deep(.announcement-markdown ul ul) { list-style-type: circle; }
.markdown-preview :deep(.announcement-markdown ul ul ul),
.current-content :deep(.announcement-markdown ul ul ul) { list-style-type: square; }
.markdown-preview :deep(.announcement-markdown li),
.current-content :deep(.announcement-markdown li) { margin: .25rem 0; }
.markdown-preview :deep(.announcement-markdown li::marker),
.current-content :deep(.announcement-markdown li::marker) { color: var(--announcement-md-accent); }
.markdown-preview :deep(.announcement-markdown li > p),
.current-content :deep(.announcement-markdown li > p) { margin: .22rem 0; }
.markdown-preview :deep(.announcement-markdown li > ul),
.markdown-preview :deep(.announcement-markdown li > ol),
.current-content :deep(.announcement-markdown li > ul),
.current-content :deep(.announcement-markdown li > ol) { margin: .28rem 0; }
.markdown-preview :deep(.announcement-markdown blockquote),
.current-content :deep(.announcement-markdown blockquote) { margin: .8rem 0; padding: .55rem .75rem; color: var(--announcement-md-text); background: var(--announcement-md-accent-soft); border-left: 3px solid var(--announcement-md-accent); border-radius: 0 .45rem .45rem 0; }
.markdown-preview :deep(.announcement-markdown blockquote p),
.current-content :deep(.announcement-markdown blockquote p) { margin: 0; }
.markdown-preview :deep(.announcement-markdown a),
.current-content :deep(.announcement-markdown a) { color: var(--announcement-md-accent); text-decoration: underline; text-decoration-color: var(--announcement-md-border); text-underline-offset: 3px; }
.markdown-preview :deep(.announcement-markdown code:not(pre code)),
.current-content :deep(.announcement-markdown code:not(pre code)) { padding: .12rem .32rem; color: var(--announcement-md-heading); background: var(--announcement-md-soft); border: 1px solid var(--announcement-md-border); border-radius: .32rem; font-family: var(--admin-code-font, ui-monospace, monospace); font-size: .88em; }
.markdown-preview :deep(.announcement-markdown pre),
.current-content :deep(.announcement-markdown pre) { margin: .8rem 0; border: 1px solid var(--announcement-md-border); border-radius: .55rem; box-shadow: none; }
.markdown-preview :deep(.announcement-markdown .table-wrapper),
.current-content :deep(.announcement-markdown .table-wrapper) { margin: .8rem 0; overflow-x: auto; border: 1px solid var(--announcement-md-border); border-radius: .55rem; }
.markdown-preview :deep(.announcement-markdown table),
.current-content :deep(.announcement-markdown table) { width: 100%; border-collapse: collapse; background: transparent; }
.markdown-preview :deep(.announcement-markdown th),
.markdown-preview :deep(.announcement-markdown td),
.current-content :deep(.announcement-markdown th),
.current-content :deep(.announcement-markdown td) { padding: .5rem .6rem; color: var(--announcement-md-text); border-bottom: 1px solid var(--announcement-md-border); font-size: .76rem; text-align: left; }
.markdown-preview :deep(.announcement-markdown th),
.current-content :deep(.announcement-markdown th) { color: var(--announcement-md-heading); background: var(--announcement-md-soft); font-weight: 750; }
.markdown-preview :deep(.announcement-markdown tr:last-child td),
.current-content :deep(.announcement-markdown tr:last-child td) { border-bottom: 0; }
.markdown-preview :deep(.announcement-markdown hr),
.current-content :deep(.announcement-markdown hr) { margin: 1rem 0; border: 0; border-top: 1px solid var(--announcement-md-border); }
.markdown-preview :deep(.announcement-markdown img),
.current-content :deep(.announcement-markdown img) { display: block; max-width: 100%; height: auto; margin: .8rem 0; border-radius: .55rem; }
.spin { animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

@media (max-width: 980px) {
  .announcement-grid { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  .admin-console { --admin-sidebar-width: min(84vw, 320px); }
  .admin-main, .admin-main.is-sidebar-hidden { padding-left: 1rem; }
  .admin-sidebar-backdrop { display: block; position: fixed; inset: 0; z-index: 55; background: rgba(15,23,42,.45); border: 0; }
  .announcement-hero { align-items: flex-start; }
  .announcement-hero p { display: none; }
  .admin-status-badge { display: none; }
}
@media (max-width: 520px) {
  .announcement-hero { padding: .9rem; }
  .announcement-hero-icon { width: 36px; height: 36px; }
  .refresh-btn { padding: .48rem; }
  .refresh-btn :deep(svg) { margin: 0; }
  .refresh-btn { font-size: 0; }
  .panel-heading, .editor-form { padding: .9rem; }
  .admin-textarea { min-height: 190px; }
}
</style>
