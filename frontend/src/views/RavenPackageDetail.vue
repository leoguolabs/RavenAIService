<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useUserStore } from '@/stores/user'
import {
  deleteRavenPackage,
  getRavenPackageDetail,
  ravenBaseUrl,
  getRavenPackageDownloadUrl,
} from '@/api/raven'
import { projectRepoApi, type ProjectRepoOption } from '@/api'
import { copyToClipboard, downloadFileByUrl, formatDateTime, formatFileSize } from '@/utils'
import { renderMarkdown } from '@/utils/markdownRenderer'
import { normalizeComponents, useRavenMetadataEditor } from '@/composables/useRavenMetadataEditor'
import type { RavenPackage } from '@/types'

const { t } = useI18n()

const route = useRoute()
const router = useRouter()
const userStore = useUserStore()
const isAdmin = computed(
  () => userStore.isAuthenticated && String(userStore.profile?.role || '').toLowerCase() === 'admin'
)

const loading = ref(false)
const deleting = ref(false)
const pkg = ref<RavenPackage | null>(null)
const errorMessage = ref('')
const projectOptions = ref<ProjectRepoOption[]>([])

const packageId = computed(() => String(route.params.id || ''))

const shareLink = computed(() =>
  pkg.value ? `${ravenBaseUrl}/package/${encodeURIComponent(pkg.value.id)}` : ''
)
const downloadLink = computed(() =>
  pkg.value ? getRavenPackageDownloadUrl(pkg.value.id) : ''
)

const normalizeTags = (value?: unknown) => {
  if (!value) return []
  if (Array.isArray(value)) return value.map((t) => String(t)).filter(Boolean)
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      if (Array.isArray(parsed)) return parsed.map((t) => String(t)).filter(Boolean)
    } catch {
      return value
        .split(',')
        .map((t) => t.trim())
        .filter(Boolean)
    }
  }
  return []
}

const isPatchPackage = (value?: RavenPackage | null) => {
  if (!value?.metadata) return false
  const flag = value.metadata.isPatch
  return flag === true || flag === 'true'
}

const humanizePatch = (value?: RavenPackage | null) =>
  isPatchPackage(value) ? t('raven.patch') : t('raven.release')

const projectByCode = computed(() => {
  const map = new Map<string, ProjectRepoOption>()
  projectOptions.value.forEach((project) => map.set(project.project_code, project))
  return map
})

const projectText = (code?: string) => {
  const normalized = String(code || '')
  if (!normalized) return t('raven.unassociatedProject')
  const project = projectByCode.value.get(normalized)
  if (project) return project.project_name || project.project_code
  return t('raven.unassociatedProjectWithCode', { code: normalized })
}

const projectPillClass = (code?: string) => {
  const normalized = String(code || '')
  if (!normalized) return 'rw-pill-warning'
  return projectByCode.value.has(normalized) ? 'rw-pill-info' : 'rw-pill-warning'
}

const renderedDescription = computed(() =>
  renderMarkdown(pkg.value?.metadata?.description || t('raven.noDesc'), { cleanXml: true })
)

const tags = computed(() => normalizeTags(pkg.value?.metadata?.tags))
const components = computed(() => normalizeComponents(pkg.value?.metadata?.components))

// Inline metadata editing. Only renders affordances when the backend
// reports `canEditMetadata`; the PATCH endpoint re-checks authorization.
const {
  canEdit,
  editingDesc,
  descDraft,
  savingDesc,
  startEditDesc,
  cancelEditDesc,
  saveDesc,
  editingTags,
  tagsDraft,
  tagInput,
  savingTags,
  startEditTags,
  cancelEditTags,
  addTag,
  removeTag,
  saveTags,
  editingComponents, componentsDraft, savingComponents,
  startEditComponents, cancelEditComponents, addComponent, removeComponent, saveComponents,
} = useRavenMetadataEditor({
  pkg,
  onSaved: (saved) => {
    pkg.value = saved
  },
  onSuccess: (message) => ElMessage.success(message),
  onError: (message) => ElMessage.error(message),
  onWarn: (message) => ElMessage.warning(message),
  t,
})

const fetchDetail = async () => {
  if (!packageId.value) {
    errorMessage.value = t('raven.pkgIdNotFound')
    return
  }
  loading.value = true
  errorMessage.value = ''
  pkg.value = null
  try {
    const { data } = await getRavenPackageDetail(packageId.value)
    if (data?.success && data.data) {
      pkg.value = data.data
    } else {
      throw new Error(data?.message || t('raven.fetchDetailFail'))
    }
  } catch (error: any) {
    console.error(error)
    errorMessage.value = error.message || t('raven.loadDetailFail')
    ElMessage.error(errorMessage.value)
  } finally {
    loading.value = false
  }
}

const fetchProjectOptions = async () => {
  try {
    const response = await projectRepoApi.listEnabled()
    projectOptions.value = Array.isArray(response?.data) ? response.data : []
  } catch (error) {
    console.error(error)
  }
}

const goBack = () => {
  if (window.history.length > 1) {
    router.back()
  } else {
    router.push({ name: 'RavenManager' })
  }
}

const copyShareLink = async (link: string) => {
  if (!link) return
  const ok = await copyToClipboard(link)
  if (ok) {
    ElMessage.success(t('raven.copyLinkSuccess'))
  } else {
    ElMessage.warning(t('raven.copyLinkFail'))
  }
}

const copyRebuildPrompt = async () => {
  if (!pkg.value || !downloadLink.value) {
    ElMessage.warning(t('raven.noDownloadLink'))
    return
  }
  const prompt = t('raven.rebuildPromptTemplate', { url: downloadLink.value })
  const ok = await copyToClipboard(prompt)
  if (ok) {
    ElMessage.success(t('raven.copyPromptSuccess'))
  } else {
    ElMessage.warning(t('raven.copyLinkFail'))
  }
}

const downloadPackage = (value: RavenPackage) => {
  const url = getRavenPackageDownloadUrl(value.id)
  const filename =
    value.name && value.name.includes('.') ? value.name : value.name ? `${value.name}.tgz` : 'package.tgz'
  downloadFileByUrl(url, filename)
  ElMessage.success(t('raven.downloadStart'))
}

const deletePackage = async (value: RavenPackage) => {
  if (!isAdmin.value || deleting.value) return
  try {
    await ElMessageBox.confirm(
      t('raven.deleteConfirmMsg', { name: value.name || value.id }),
      t('raven.deletingConfirm'),
      {
        type: 'warning',
        confirmButtonText: t('common.delete'),
        cancelButtonText: t('common.cancel'),
      }
    )
    deleting.value = true
    const { data } = await deleteRavenPackage(value.id)
    if (!data?.success) throw new Error(data?.message || t('raven.deleteFail'))
    ElMessage.success(t('raven.deleteSuccess'))
    router.push({ name: 'RavenManager' })
  } catch (error: any) {
    if (error === 'cancel' || error === 'close') return
    console.error(error)
    const detail = error?.response?.data?.detail
    ElMessage.error(detail || error.message || t('raven.deleteFail'))
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  fetchDetail()
  fetchProjectOptions()
})

watch(
  () => route.params.id,
  () => fetchDetail()
)
</script>

<template>
  <div class="rw-page package-detail-page">
    <header class="rw-topbar">
      <div class="rw-topbar-left">
        <button class="back-btn" @click="goBack" :title="t('common.back')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M15 18l-6-6 6-6" />
          </svg>
        </button>
        <span class="rw-crumb">{{ t('raven.detailTitle') }}</span>
        <span v-if="pkg" class="rw-crumb-meta">· {{ pkg.name }}</span>
      </div>
      <div class="rw-topbar-right">
        <button v-if="pkg" class="rw-btn-secondary" @click="copyShareLink(shareLink)">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
          </svg>
          <span>{{ t('raven.copyDetailLink') }}</span>
        </button>
        <button v-if="pkg" class="rw-btn-secondary" @click="copyShareLink(downloadLink)">
          <span>{{ t('raven.copyDownloadLink') }}</span>
        </button>
        <button v-if="pkg" class="rw-btn-secondary" @click="copyRebuildPrompt">
          <span>{{ t('raven.copyRebuildPrompt') }}</span>
        </button>
        <button
          v-if="pkg && isAdmin"
          class="rw-btn-danger"
          :disabled="deleting"
          @click="deletePackage(pkg)"
        >
          <span>{{ t('common.delete') }}</span>
        </button>
        <button v-if="pkg" class="rw-btn-primary" @click="downloadPackage(pkg)">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          <span>{{ t('common.download') }}</span>
        </button>
      </div>
    </header>

    <div class="rw-page-scroll">
      <div v-if="errorMessage" class="rw-error-banner">
        {{ errorMessage }}
      </div>

      <div v-if="loading && !pkg" class="rw-card">
        <el-skeleton :rows="8" animated />
      </div>

      <template v-else-if="pkg">
        <!-- 标题卡 -->
        <section class="rw-card title-card">
          <div class="title-row">
            <div class="title-left">
              <h1 class="title-name">{{ pkg.name }}</h1>
              <div class="title-meta">
                <span class="title-id">ID: {{ pkg.id }}</span>
              </div>
            </div>
            <div class="title-tags">
              <span class="rw-pill" :class="projectPillClass(pkg.projectCode)">
                {{ projectText(pkg.projectCode) }}
              </span>
              <span class="rw-pill rw-pill-neutral rw-pill-mono">v{{ pkg.version || t('raven.unknown') }}</span>
              <span class="rw-pill" :class="isPatchPackage(pkg) ? 'rw-pill-warning' : 'rw-pill-success'">
                {{ humanizePatch(pkg) }}
              </span>
            </div>
          </div>
        </section>

        <!-- 基本信息 -->
        <section class="rw-card">
          <div class="card-head">
            <h2 class="card-title">{{ t('raven.basicInfo') }}</h2>
          </div>
          <div class="info-grid">
            <div class="info-item">
              <label>{{ t('raven.labelName') }}</label>
              <div class="info-value strong">{{ pkg.name }}</div>
            </div>
            <div class="info-item">
              <label>{{ t('raven.labelVersion') }}</label>
              <div class="info-value mono">v{{ pkg.version || t('raven.unknown') }}</div>
            </div>
            <div class="info-item">
              <label>{{ t('raven.labelSize') }}</label>
              <div class="info-value strong">{{ formatFileSize(pkg.size) }}</div>
            </div>
            <div class="info-item">
              <label>{{ t('raven.labelProject') }}</label>
              <div>
                <span class="rw-pill" :class="projectPillClass(pkg.projectCode)">
                  {{ projectText(pkg.projectCode) }}
                </span>
              </div>
            </div>
            <div class="info-item">
              <label>{{ t('raven.labelPatchType') }}</label>
              <div>
                <span class="rw-pill" :class="isPatchPackage(pkg) ? 'rw-pill-warning' : 'rw-pill-success'">
                  {{ humanizePatch(pkg) }}
                </span>
              </div>
            </div>
            <div class="info-item">
              <label>{{ t('raven.labelCreatedAt') }}</label>
              <div class="info-value mono">{{ formatDateTime(pkg.createdAt) }}</div>
            </div>
            <div class="info-item col-span-all">
              <label>{{ t('raven.labelId') }}</label>
              <div class="code-box">{{ pkg.id }}</div>
            </div>
            <div class="info-item col-span-all">
              <label>{{ t('raven.labelPath') }}</label>
              <div class="code-box">{{ pkg.path || '-' }}</div>
            </div>
            <div class="info-item col-span-all" v-if="pkg.metadata?.sha256">
              <label>{{ t('raven.labelSha256') }}</label>
              <div class="code-box">{{ pkg.metadata.sha256 }}</div>
            </div>
          </div>
        </section>

        <!-- 描述 -->
        <section class="rw-card">
          <div class="card-head">
            <h2 class="card-title">{{ t('raven.descSection') }}</h2>
            <button
              v-if="canEdit && !editingDesc"
              class="rw-btn-text card-head-action"
              @click="startEditDesc"
            >
              {{ t('raven.editAction') }}
            </button>
          </div>
          <div v-if="!editingDesc" class="rw-markdown" v-html="renderedDescription" />
          <div v-else class="edit-block">
            <textarea
              v-model="descDraft"
              class="rw-textarea"
              rows="8"
              :placeholder="t('raven.descEditPlaceholder')"
              :disabled="savingDesc"
            />
            <div class="edit-actions">
              <button class="rw-btn-primary" :disabled="savingDesc" @click="saveDesc">
                {{ savingDesc ? t('raven.savingAction') : t('raven.saveAction') }}
              </button>
              <button class="rw-btn-secondary" :disabled="savingDesc" @click="cancelEditDesc">
                {{ t('raven.cancelAction') }}
              </button>
            </div>
          </div>
        </section>

        <!-- 标签 -->
        <section class="rw-card">
          <div class="card-head">
            <h2 class="card-title">{{ t('raven.tagsSection') }}</h2>
            <span v-if="!editingTags && tags.length" class="card-subtitle">{{ t('raven.totalCount', { count: tags.length }) }}</span>
            <button
              v-if="canEdit && !editingTags"
              class="rw-btn-text card-head-action"
              @click="startEditTags"
            >
              {{ t('raven.editAction') }}
            </button>
          </div>
          <div v-if="!editingTags" class="pill-group">
            <span v-for="tag in tags" :key="tag" class="rw-pill rw-pill-neutral">
              {{ tag }}
            </span>
            <span v-if="!tags.length" class="empty-inline">{{ t('raven.noTags') }}</span>
          </div>
          <div v-else class="edit-block">
            <div class="pill-group">
              <span
                v-for="tag in tagsDraft"
                :key="tag"
                class="rw-pill rw-pill-neutral editable-pill"
              >
                {{ tag }}
                <button
                  type="button"
                  class="pill-remove"
                  :title="t('raven.removeTagAction')"
                  :disabled="savingTags"
                  @click="removeTag(tag)"
                >×</button>
              </span>
              <span v-if="!tagsDraft.length" class="empty-inline">{{ t('raven.noTags') }}</span>
            </div>
            <div class="tag-input-row">
              <input
                v-model="tagInput"
                class="rw-input"
                :placeholder="t('raven.tagInputPlaceholder')"
                :disabled="savingTags"
                @keydown.enter.prevent="addTag"
              />
              <button class="rw-btn-secondary" :disabled="savingTags" @click="addTag">
                {{ t('raven.addTagAction') }}
              </button>
            </div>
            <div class="edit-actions">
              <button class="rw-btn-primary" :disabled="savingTags" @click="saveTags">
                {{ savingTags ? t('raven.savingAction') : t('raven.saveAction') }}
              </button>
              <button class="rw-btn-secondary" :disabled="savingTags" @click="cancelEditTags">
                {{ t('raven.cancelAction') }}
              </button>
            </div>
          </div>
        </section>

        <!-- 组件 -->
        <section class="rw-card">
          <div class="card-head">
            <h2 class="card-title">{{ t('raven.componentsSection') }}</h2>
            <span v-if="!editingComponents && components.length" class="card-subtitle">{{ t('raven.totalCount', { count: components.length }) }}</span>
            <button v-if="canEdit && !editingComponents" class="rw-btn-text card-head-action" @click="startEditComponents">
              {{ t('raven.editAction') }}
            </button>
          </div>
          <div v-if="!editingComponents" class="pill-group">
            <span
              v-for="comp in components"
              :key="`${comp.name}-${comp.version || 'na'}`"
              class="rw-pill rw-pill-info"
            >
              {{ comp.name }}<span v-if="comp.version" class="rw-pill-sub"> · {{ comp.version }}</span>
            </span>
            <span v-if="!components.length" class="empty-inline">{{ t('raven.noComponents') }}</span>
          </div>
          <div v-else class="edit-block">
            <div v-for="(component, index) in componentsDraft" :key="index" class="component-input-row">
              <label>
                <span>{{ t('raven.componentNamePlaceholder') }}</span>
                <input v-model="component.name" class="rw-input" maxlength="256" :disabled="savingComponents" :placeholder="t('raven.componentNamePlaceholder')" />
              </label>
              <label>
                <span>{{ t('raven.componentVersionOptional') }}</span>
                <input v-model="component.version" class="rw-input" maxlength="256" :disabled="savingComponents" :placeholder="t('raven.componentVersionOptional')" />
              </label>
              <button class="rw-btn-secondary" :disabled="savingComponents" @click="removeComponent(index)">{{ t('raven.removeComponentAction') }}</button>
            </div>
            <span v-if="!componentsDraft.length" class="empty-inline">{{ t('raven.noComponents') }}</span>
            <div class="edit-actions">
              <button class="rw-btn-secondary" :disabled="savingComponents || componentsDraft.length >= 100" @click="addComponent">{{ t('raven.addComponentAction') }}</button>
              <button class="rw-btn-primary" :disabled="savingComponents" @click="saveComponents">{{ savingComponents ? t('raven.savingAction') : t('raven.saveAction') }}</button>
              <button class="rw-btn-secondary" :disabled="savingComponents" @click="cancelEditComponents">{{ t('raven.cancelAction') }}</button>
            </div>
          </div>
        </section>

        <!-- 操作 -->
        <section class="rw-card">
          <div class="card-head">
            <h2 class="card-title">{{ t('common.actions') }}</h2>
          </div>
          <div class="actions-grid">
            <button class="rw-btn-primary" @click="downloadPackage(pkg)">{{ t('raven.downloadPkg') }}</button>
            <button class="rw-btn-secondary" @click="copyShareLink(shareLink)">{{ t('raven.copyDetailLink') }}</button>
            <button class="rw-btn-secondary" @click="copyShareLink(downloadLink)">{{ t('raven.copyDownloadLink') }}</button>
            <button class="rw-btn-secondary" @click="copyRebuildPrompt">{{ t('raven.copyRebuildPrompt') }}</button>
          </div>
        </section>
      </template>

      <div v-else-if="!loading" class="not-found">
        <el-result icon="warning" :title="t('raven.pkgNotFound')" :sub-title="t('raven.pkgNotFoundHint')">
          <template #extra>
            <button class="rw-btn-primary" @click="$router.push({ name: 'RavenManager' })">{{ t('raven.backToList') }}</button>
          </template>
        </el-result>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rw-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--rw-canvas, #fff);
  color: var(--rw-ink, #171717);
  font-family: var(--rw-sans, Inter, system-ui, sans-serif);
}

.rw-page *,
.rw-page *::before,
.rw-page *::after { box-sizing: border-box; }

.rw-page button {
  font-family: inherit;
  cursor: pointer;
  border: none;
  background: none;
  padding: 0;
  color: inherit;
}

/* Topbar */
.rw-topbar {
  height: 56px;
  flex-shrink: 0;
  border-bottom: 1px solid var(--rw-hairline, #f0f0f3);
  display: flex;
  align-items: center;
  padding: 0 28px;
  gap: 14px;
  background: var(--rw-canvas, #fff);
  position: sticky;
  top: 0;
  z-index: 10;
}
.rw-topbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
}
.rw-topbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.rw-crumb {
  font-size: 14.5px;
  font-weight: 600;
  color: var(--rw-ink, #171717);
  letter-spacing: -0.1px;
  flex-shrink: 0;
}
.rw-crumb-meta {
  font-size: 12px;
  color: var(--rw-muted, #999);
  font-family: var(--rw-mono, JetBrains Mono, monospace);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.back-btn {
  width: 32px;
  height: 32px;
  border-radius: 6px;
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
  background: var(--rw-canvas, #fff);
  color: var(--rw-ink, #171717);
  display: inline-grid;
  place-items: center;
  cursor: pointer;
  transition: background-color .15s ease, border-color .15s ease;
}
.back-btn:hover { background: var(--rw-surface-strong, #f0f0f3); }

/* Buttons */
.rw-page .rw-btn-primary,
.rw-page .rw-btn-secondary,
.rw-page .rw-btn-danger {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 32px;
  padding: 0 14px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  font-family: var(--rw-sans, Inter, system-ui, sans-serif);
  cursor: pointer;
  transition: background-color .15s ease, border-color .15s ease, color .15s ease;
  white-space: nowrap;
  border: none;
  line-height: 1;
}
.rw-page .rw-btn-primary {
  background: var(--rw-primary, #171717);
  color: var(--rw-on-primary, #fff);
}
.rw-page .rw-btn-primary:hover:not(:disabled) {
  background: var(--rw-primary-active, #404040);
}
.rw-page .rw-btn-secondary {
  background: var(--rw-canvas, #fff);
  color: var(--rw-ink, #171717);
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
}
.rw-page .rw-btn-secondary:hover:not(:disabled) {
  background: var(--rw-surface-strong, #f0f0f3);
}
.rw-page .rw-btn-danger {
  background: var(--rw-danger, #c0382b);
  color: #fff;
}
.rw-page .rw-btn-danger:hover:not(:disabled) {
  background: #a02f24;
}
.rw-page .rw-btn-primary:disabled,
.rw-page .rw-btn-secondary:disabled,
.rw-page .rw-btn-danger:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* Pills */
.rw-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 9px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.2px;
  white-space: nowrap;
  line-height: 1;
}
.rw-pill-success { background: rgba(22, 163, 74, 0.12); color: #15803d; }
.rw-pill-info { background: var(--rw-surface-strong, #f0f0f3); color: var(--rw-ink, #171717); }
.rw-pill-warning { background: rgba(171, 100, 0, 0.10); color: #ab6400; }
.rw-pill-danger { background: rgba(192, 56, 43, 0.10); color: #c0382b; }
.rw-pill-neutral { background: var(--rw-surface-strong, #f0f0f3); color: var(--rw-body, #60646c); }
.rw-pill-mono {
  font-family: var(--rw-mono, JetBrains Mono, monospace);
  font-weight: 500;
}
.rw-pill-sub { font-weight: 500; opacity: 0.8; }

.pill-group {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.empty-inline {
  color: var(--rw-muted, #999);
  font-size: 13px;
}

/* Inline metadata editing */
.card-head-action {
  margin-left: auto;
  font-size: 13px;
  font-weight: 600;
  color: var(--rw-accent, #2563eb);
}
.card-head-action:hover:not(:disabled) { text-decoration: underline; }

.edit-block {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.rw-textarea,
.rw-input {
  width: 100%;
  font-family: inherit;
  font-size: 13px;
  color: var(--rw-ink, #171717);
  background: var(--rw-canvas, #fff);
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
  border-radius: 8px;
  padding: 10px 12px;
  resize: vertical;
}
.rw-textarea:focus,
.rw-input:focus {
  outline: none;
  border-color: var(--rw-accent, #2563eb);
}
.rw-textarea:disabled,
.rw-input:disabled { opacity: 0.6; cursor: not-allowed; }

.component-input-row { display: flex; gap: 8px; align-items: end; flex-wrap: wrap; }
.component-input-row label { flex: 1 1 180px; min-width: 0; }
.component-input-row label span { display: block; margin-bottom: 6px; font-size: 12px; color: var(--rw-body); }
.component-input-row .rw-input { box-sizing: border-box; }

.tag-input-row {
  display: flex;
  gap: 8px;
}
.tag-input-row .rw-input { flex: 1; }

.editable-pill { padding-right: 4px; }
.pill-remove {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  border-radius: 999px;
  font-size: 13px;
  line-height: 1;
  color: var(--rw-body, #60646c);
}
.pill-remove:hover:not(:disabled) {
  background: rgba(0, 0, 0, 0.08);
  color: var(--rw-ink, #171717);
}
.pill-remove:disabled { opacity: 0.5; cursor: not-allowed; }

.edit-actions {
  display: flex;
  gap: 10px;
}

/* Page scroll */
.rw-page-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 24px 28px 48px;
  display: flex;
  flex-direction: column;
  gap: 20px;
  max-width: 1200px;
  margin: 0 auto;
  width: 100%;
}

.rw-card {
  background: var(--rw-surface-card, #fff);
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
  border-radius: 12px;
  padding: 24px;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.02);
}

/* Title card */
.title-card { padding: 20px 24px; }
.title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}
.title-left { flex: 1; min-width: 0; }
.title-name {
  font-size: 22px;
  font-weight: 600;
  letter-spacing: -0.5px;
  color: var(--rw-ink, #171717);
  margin: 0;
  word-break: break-all;
  line-height: 1.3;
}
.title-meta { margin-top: 6px; }
.title-id {
  font-family: var(--rw-mono, JetBrains Mono, monospace);
  font-size: 12px;
  color: var(--rw-muted, #999);
}
.title-tags { display: flex; gap: 8px; flex-wrap: wrap; }

/* Card head */
.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--rw-ink, #171717);
  letter-spacing: -0.1px;
  margin: 0;
}
.card-subtitle {
  font-size: 12px;
  color: var(--rw-muted, #999);
}

/* Info grid */
.info-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 18px 22px;
}
.info-item {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}
.info-item > label {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.6px;
  color: var(--rw-muted, #999);
  text-transform: uppercase;
}
.info-value {
  font-size: 13.5px;
  color: var(--rw-ink, #171717);
  word-break: break-word;
}
.info-value.strong { font-weight: 600; }
.info-value.mono {
  font-family: var(--rw-mono, JetBrains Mono, monospace);
  font-size: 12.5px;
  color: var(--rw-body, #60646c);
}
.info-item.col-span-all { grid-column: 1 / -1; }

.code-box {
  background: var(--rw-canvas-soft, #fafafa);
  border: 1px solid var(--rw-hairline, #f0f0f3);
  border-radius: 6px;
  padding: 8px 10px;
  font-family: var(--rw-mono, JetBrains Mono, monospace);
  font-size: 12px;
  color: var(--rw-body, #60646c);
  word-break: break-all;
  line-height: 1.5;
}

/* Markdown */
.rw-markdown {
  font-size: 13.5px;
  color: var(--rw-ink, #171717);
  line-height: 1.6;
}
.rw-markdown :deep(img) { max-width: 100%; }
.rw-markdown :deep(p) { margin: 0 0 8px; }
.rw-markdown :deep(p:last-child) { margin-bottom: 0; }
.rw-markdown :deep(pre) {
  background: var(--rw-surface-dark, #171717);
  color: var(--rw-on-primary, #fff);
  padding: 12px 14px;
  border-radius: 8px;
  font-family: var(--rw-mono, JetBrains Mono, monospace);
  font-size: 12.5px;
  overflow: auto;
}
.rw-markdown :deep(code) {
  font-family: var(--rw-mono, JetBrains Mono, monospace);
  font-size: 12px;
  background: var(--rw-surface-strong, #f0f0f3);
  color: var(--rw-ink, #171717);
  padding: 1px 6px;
  border-radius: 4px;
}
.rw-markdown :deep(pre code) {
  background: transparent;
  color: inherit;
  padding: 0;
  font-size: 12.5px;
}

/* Actions grid */
.actions-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 10px;
}

/* Error / not found */
.rw-error-banner {
  background: rgba(192, 56, 43, 0.08);
  color: #c0382b;
  border: 1px solid rgba(192, 56, 43, 0.18);
  border-radius: 12px;
  padding: 14px 18px;
  font-size: 13px;
}
.not-found {
  background: var(--rw-canvas, #fff);
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
  border-radius: 12px;
  padding: 32px;
}

@media (max-width: 1024px) {
  .info-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 720px) {
  .rw-topbar { padding: 0 16px; }
  .rw-page-scroll { padding: 16px 16px 32px; gap: 14px; }
  .info-grid { grid-template-columns: 1fr; gap: 14px; }
  .title-name { font-size: 18px; }
}
</style>
