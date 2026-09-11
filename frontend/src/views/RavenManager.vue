<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { projectRepoApi, type ProjectRepoOption } from '@/api'
import {
  listRavenPackages,
  deleteRavenPackage,
  uploadRavenPackages,
  getRavenPackageDetail,
  getRavenPackageDownloadUrl,
  streamPackagesAgentSearch,
} from '@/api/raven'
import { downloadFileByUrl, formatDateTime, formatFileSize } from '@/utils'
import { renderMarkdown } from '@/utils/markdownRenderer'
import type {
  PackageAgentSearchResponse,
  PackageAgentToolTraceEntry,
  PackageAgentTraceEvent,
  RavenComponent,
  RavenPackage,
  RavenUploadMetadata,
} from '@/types'
import type { AgentTraceEvent } from '@/types/agentTrace'
import AgentTraceStream from '@/components/AgentTraceStream.vue'
import WorkbenchTopbar from '@/layouts/WorkbenchTopbar.vue'

const router = useRouter()
const { t } = useI18n()

const filters = reactive({
  search: '',
  projectCode: '',
  version: '',
  tags: '',
  isPatch: '',
  page: 1,
  limit: 10,
})

const packages = ref<RavenPackage[]>([])
const pagination = reactive({
  currentPage: 1,
  totalPages: 1,
  totalItems: 0,
  itemsPerPage: 10,
})

const loadingList = ref(false)

const uploadDialogVisible = ref(false)
const searchDialogVisible = ref(false)

const uploadZoneActive = ref(false)
const uploadFiles = ref<File[]>([])
const uploadMeta = reactive<RavenUploadMetadata>({
  projectCode: '',
  version: '',
  isPatch: false,
  description: '',
  tags: [],
  components: [],
})
const tagDraft = ref('')
const componentDraft = reactive<{ name: string; version: string }>({
  name: '',
  version: '',
})
const uploading = ref(false)
const uploadProgress = ref(0)
const uploadStatus = ref('')
const uploadController = ref<AbortController | null>(null)

const searchQuery = ref('')
const searchProjectRepoId = ref<number | null>(null)
const searchLoading = ref(false)
const searchResult = ref<PackageAgentSearchResponse | null>(null)
const searchTraceEvents = ref<AgentTraceEvent[]>([])
const searchTraceRunning = ref(false)
const searchAbortController = ref<AbortController | null>(null)
const searchRecommendedPackages = ref<RavenPackage[]>([])
const searchRelevantPackages = ref<RavenPackage[]>([])
const searchPackagesLoading = ref(false)
const searchError = ref<string | null>(null)
const searchSuggestions = computed<string[]>(() => [
  t('raven.suggestion1'),
  t('raven.suggestion2'),
  t('raven.suggestion3'),
  t('raven.suggestion4'),
])
const searchDetailVisible = ref(false)
const searchDetailLoading = ref(false)
const searchDetailPackage = ref<RavenPackage | null>(null)
const projectOptions = ref<ProjectRepoOption[]>([])
const projectOptionsLoading = ref(false)
const UNASSOCIATED_PROJECT = '__unassociated__'

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

const normalizeComponents = (value?: unknown): RavenComponent[] => {
  if (!value) return []
  let components: any[] = []
  if (Array.isArray(value)) {
    components = value
  } else if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value)
      if (Array.isArray(parsed)) components = parsed
    } catch {
      components = []
    }
  }

  return components
    .map((item) => {
      if (typeof item === 'string') return { name: item }
      if (item && typeof item === 'object') {
        const name = (item as any).name
        const version = (item as any).version
        if (!name) return null
        return version ? { name, version: String(version) } : { name: String(name) }
      }
      return null
    })
    .filter(Boolean) as RavenComponent[]
}

const isPatchPackage = (pkg?: RavenPackage | null) => {
  if (!pkg?.metadata) return false
  const value = pkg.metadata.isPatch
  return value === true || value === 'true'
}

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

const projectSupportsPackageSearch = (project: ProjectRepoOption) => {
  if (Array.isArray(project.enabled_agent_keys) && project.enabled_agent_keys.length) {
    return project.enabled_agent_keys.includes('package_search')
  }
  return project.has_repo !== false
}

const packageSearchProjectOptions = computed(() =>
  projectOptions.value.filter(projectSupportsPackageSearch)
)

const fetchProjectOptions = async () => {
  projectOptionsLoading.value = true
  try {
    const response = await projectRepoApi.listEnabled()
    projectOptions.value = Array.isArray(response?.data) ? response.data : []
  } catch (error) {
    console.error(error)
    ElMessage.error(t('raven.loadProjectsFail'))
  } finally {
    projectOptionsLoading.value = false
  }
}

const fetchPackages = async () => {
  loadingList.value = true
  try {
    const { data } = await listRavenPackages({
      page: filters.page,
      limit: filters.limit,
      search: filters.search || undefined,
      projectCode: filters.projectCode || undefined,
      tags: filters.tags || undefined,
      version: filters.version || undefined,
      isPatch: filters.isPatch || undefined,
    })

    if (data?.success && data.data) {
      packages.value = data.data.packages || []
      pagination.currentPage = data.data.pagination.currentPage
      pagination.totalPages = data.data.pagination.totalPages
      pagination.totalItems = data.data.pagination.totalItems
      pagination.itemsPerPage = data.data.pagination.itemsPerPage
    } else {
      throw new Error(data?.message || t('raven.fetchListFail'))
    }
  } catch (error: any) {
    console.error(error)
    ElMessage.error(error.message || t('raven.loadListFail'))
  } finally {
    loadingList.value = false
  }
}

const handlePageChange = (page: number) => {
  filters.page = page
  fetchPackages()
}

const resetFilters = () => {
  filters.search = ''
  filters.projectCode = ''
  filters.version = ''
  filters.tags = ''
  filters.isPatch = ''
  filters.page = 1
  fetchPackages()
}

const openPackageDetail = (payload: string | RavenPackage) => {
  const id = typeof payload === 'string' ? payload : payload.id
  router.push({ name: 'RavenPackageDetail', params: { id } })
}

const downloadPackage = (pkg: RavenPackage) => {
  const url = getRavenPackageDownloadUrl(pkg.id)
  const filename =
    pkg.name && pkg.name.includes('.') ? pkg.name : pkg.name ? `${pkg.name}.tgz` : 'package.tgz'
  downloadFileByUrl(url, filename)
  ElMessage.success(t('raven.downloadStart'))
}

const deletePackage = async (pkg: RavenPackage) => {
  try {
    await ElMessageBox.confirm(
      t('raven.deleteConfirmMsg', { name: pkg.name || pkg.id }),
      t('raven.deletingConfirm'),
      {
        type: 'warning',
        confirmButtonText: t('common.delete'),
        cancelButtonText: t('common.cancel'),
      }
    )
    const { data } = await deleteRavenPackage(pkg.id)
    if (!data?.success) throw new Error(data?.message || t('raven.deleteFail'))
    ElMessage.success(t('raven.deleteSuccess'))
    fetchPackages()
  } catch (error: any) {
    if (error === 'cancel' || error === 'close') return
    console.error(error)
    ElMessage.error(error.message || t('raven.deleteFail'))
  }
}

const humanizePatch = (pkg?: RavenPackage | null) =>
  isPatchPackage(pkg) ? t('raven.patch') : t('raven.release')

const addFiles = (files: File[]) => {
  if (!files.length) return
  const validFiles: File[] = []

  files.forEach((file) => {
    const name = file.name.toLowerCase()
    if (name.endsWith('.tgz') || name.endsWith('.tar.gz') || name.endsWith('.upkg')) {
      validFiles.push(file)
    } else {
      ElMessage.warning(t('raven.fileTypeUnsupported', { name: file.name }))
    }
  })

  if (validFiles.length) {
    uploadFiles.value = [...uploadFiles.value, ...validFiles]
  }
}

const handleFileInput = (event: Event) => {
  const target = event.target as HTMLInputElement
  if (target.files) {
    addFiles(Array.from(target.files))
    target.value = ''
  }
}

const triggerUploadInput = () => {
  const input = globalThis.document?.getElementById('raven-upload-input') as HTMLInputElement | null
  input?.click()
}

const handleDrop = (event: DragEvent) => {
  event.preventDefault()
  uploadZoneActive.value = false
  const files = Array.from(event.dataTransfer?.files || [])
  addFiles(files)
}

const removeFile = (file: File) => {
  uploadFiles.value = uploadFiles.value.filter((item) => item !== file)
}

const clearUpload = () => {
  uploadFiles.value = []
  uploadMeta.description = ''
  uploadMeta.tags = []
  uploadMeta.components = []
  uploadMeta.version = ''
  uploadMeta.isPatch = false
  uploadMeta.projectCode = ''
  uploadProgress.value = 0
  uploadStatus.value = ''
}

const startUpload = async () => {
  if (!uploadFiles.value.length) {
    ElMessage.warning(t('raven.uploadSelectFirst'))
    return
  }
  if (!uploadMeta.projectCode) {
    ElMessage.warning(t('raven.projectRequired'))
    return
  }

  uploading.value = true
  uploadProgress.value = 0
  uploadStatus.value = t('raven.uploadPreparing')
  uploadController.value = new AbortController()

  try {
    await uploadRavenPackages(
      uploadFiles.value,
      uploadMeta,
      (payload) => {
        uploadProgress.value = payload.percent
        if (payload.speedText) {
          uploadStatus.value =
            payload.percent >= 100
              ? t('raven.uploadProcessing')
              : t('raven.uploadingSpeed', { speed: payload.speedText }) +
                (payload.etaSeconds
                  ? t('raven.uploadEta', { eta: Math.max(1, Math.round(payload.etaSeconds)) })
                  : '')
        }
      },
      uploadController.value.signal
    )

    uploadProgress.value = 100
    uploadStatus.value = t('raven.uploadComplete')
    ElMessage.success(t('raven.uploadCompleteMsg'))
    clearUpload()
    uploadDialogVisible.value = false
    await fetchPackages()
  } catch (error: any) {
    if (error?.code === 'ERR_CANCELED') {
      ElMessage.info(t('raven.uploadCancelled'))
      return
    }
    console.error(error)
    ElMessage.error(error.message || t('raven.uploadFail'))
  } finally {
    uploading.value = false
    uploadController.value = null
  }
}

const cancelUpload = () => {
  if (uploadController.value) {
    uploadController.value.abort()
  }
}

const addTag = () => {
  if (!tagDraft.value.trim()) return
  uploadMeta.tags = [...(uploadMeta.tags || []), tagDraft.value.trim()]
  tagDraft.value = ''
}

const removeTag = (tag: string) => {
  uploadMeta.tags = (uploadMeta.tags || []).filter((item) => item !== tag)
}

const addComponent = () => {
  if (!componentDraft.name.trim()) return
  uploadMeta.components = [
    ...(uploadMeta.components || []),
    componentDraft.version
      ? { name: componentDraft.name.trim(), version: componentDraft.version.trim() }
      : { name: componentDraft.name.trim() },
  ]
  componentDraft.name = ''
  componentDraft.version = ''
}

const removeComponent = (component: RavenComponent) => {
  uploadMeta.components = (uploadMeta.components || []).filter(
    (item) => item.name !== component.name || item.version !== component.version
  )
}

const renderedAnswer = computed(() =>
  searchResult.value ? renderMarkdown(searchResult.value.answer || '', { cleanXml: true }) : ''
)

const searchDetailDescription = computed(() =>
  renderMarkdown(searchDetailPackage.value?.metadata?.description || t('raven.noDesc'), { cleanXml: true })
)

const recommendedIdSet = computed(
  () => new Set(searchResult.value?.recommended_package_ids || [])
)

const sortedRelevantPackages = computed<RavenPackage[]>(() => {
  const recommended = searchRecommendedPackages.value
  const others = searchRelevantPackages.value.filter(
    (pkg) => !recommendedIdSet.value.has(pkg.id)
  )
  return [...recommended, ...others]
})

const isRecommendedPackage = (pkg: RavenPackage) => recommendedIdSet.value.has(pkg.id)

const searchWarnings = computed<PackageAgentToolTraceEntry[]>(() =>
  (searchResult.value?.tool_trace || []).filter(
    (entry: PackageAgentToolTraceEntry) => entry.type === 'warning'
  )
)

const openSearchPackageDetail = async (payload: RavenPackage) => {
  searchDetailVisible.value = true
  searchDetailLoading.value = true
  searchDetailPackage.value = null
  try {
    const { data } = await getRavenPackageDetail(payload.id)
    if (data?.success && data.data) {
      searchDetailPackage.value = data.data
    } else {
      throw new Error(data?.message || t('raven.fetchDetailFail'))
    }
  } catch (error: any) {
    console.error(error)
    searchDetailPackage.value = payload
    ElMessage.error(error.message || t('raven.loadDetailFailShort'))
  } finally {
    searchDetailLoading.value = false
  }
}

const resolveRecommendedPackages = async (
  result: PackageAgentSearchResponse
): Promise<void> => {
  const recommendedIds = result.recommended_package_ids || []
  const relevantIds = result.relevant_package_ids || []
  const allIds = Array.from(new Set([...recommendedIds, ...relevantIds]))
  if (!allIds.length) {
    searchRecommendedPackages.value = []
    searchRelevantPackages.value = []
    return
  }
  searchPackagesLoading.value = true
  try {
    const lookups = await Promise.all(
      allIds.map(async (id) => {
        try {
          const { data } = await getRavenPackageDetail(id)
          if (data?.success && data.data) return data.data
        } catch (err) {
          console.warn('Failed to fetch package detail', id, err)
        }
        return null
      })
    )
    const map = new Map<string, RavenPackage>()
    lookups.forEach((pkg) => {
      if (pkg && pkg.id) map.set(pkg.id, pkg)
    })
    searchRecommendedPackages.value = recommendedIds
      .map((id) => map.get(id))
      .filter((pkg): pkg is RavenPackage => Boolean(pkg))
    searchRelevantPackages.value = relevantIds
      .map((id) => map.get(id))
      .filter((pkg): pkg is RavenPackage => Boolean(pkg))
  } finally {
    searchPackagesLoading.value = false
  }
}

const performSearch = async () => {
  const q = searchQuery.value.trim()
  if (!q) {
    ElMessage.warning(t('raven.searchEmpty'))
    return
  }
  if (q.length > 1000) {
    ElMessage.warning(t('raven.searchTooLong'))
    return
  }
  if (searchProjectRepoId.value === null) {
    ElMessage.warning(t('raven.searchProjectRequired'))
    return
  }

  cancelSearch()

  searchLoading.value = true
  searchTraceEvents.value = []
  searchTraceRunning.value = true
  searchResult.value = null
  searchRecommendedPackages.value = []
  searchRelevantPackages.value = []
  searchError.value = null

  const controller = new AbortController()
  searchAbortController.value = controller

  try {
    await streamPackagesAgentSearch(q, {
      projectRepoId: searchProjectRepoId.value,
      signal: controller.signal,
      onEvent: (event: PackageAgentTraceEvent) => {
        if (event.type === 'final' && (event as any).data) {
          searchResult.value = (event as any).data as PackageAgentSearchResponse
          return
        }
        if (typeof (event as any).seq === 'number' && typeof event.type === 'string') {
          searchTraceEvents.value.push(event as unknown as AgentTraceEvent)
        }
      },
      onError: (err) => {
        console.warn('agent-search SSE error', err)
      },
    })

    if (!searchResult.value) {
      throw new Error(t('raven.noSearchResult'))
    }
    await resolveRecommendedPackages(searchResult.value)
  } catch (error: any) {
    if (error?.name === 'AbortError') return
    console.error(error)
    searchError.value = error?.message || t('raven.searchFail')
    ElMessage.error(searchError.value || t('raven.searchFail'))
  } finally {
    searchTraceRunning.value = false
    searchLoading.value = false
    if (searchAbortController.value === controller) searchAbortController.value = null
  }
}

const cancelSearch = () => {
  if (searchAbortController.value) {
    searchAbortController.value.abort()
    searchAbortController.value = null
  }
  searchTraceRunning.value = false
}

const triggerSuggestion = (item: string) => {
  searchQuery.value = item
  performSearch()
}

const openUploadDialog = () => {
  if (!projectOptions.value.length) fetchProjectOptions()
  uploadDialogVisible.value = true
}

const openSearchDialog = () => {
  if (!projectOptions.value.length) fetchProjectOptions()
  searchDialogVisible.value = true
}

const closeSearchDialog = () => {
  cancelSearch()
  searchDialogVisible.value = false
}

const topbarMeta = computed(() =>
  t('raven.topbarMeta', { count: pagination.totalItems || packages.value.length })
)

onMounted(() => {
  fetchPackages()
  fetchProjectOptions()
})
</script>

<template>
  <div class="rw-page">
    <WorkbenchTopbar :title="t('raven.listTitle')" :meta="topbarMeta">
      <template #actions>
        <button class="rw-btn-secondary" @click="openSearchDialog">
          <el-icon><MagicStick /></el-icon>
          {{ t('raven.searchAi') }}
        </button>
        <button class="rw-btn-primary" @click="openUploadDialog">
          <el-icon><UploadFilled /></el-icon>
          {{ t('raven.uploadNew') }}
        </button>
      </template>
    </WorkbenchTopbar>

    <div class="rw-page-scroll">
      <section class="rw-card rw-filter-card">
        <div class="rw-filter-row">
          <el-input
            v-model="filters.search"
            :placeholder="t('raven.searchPlaceholder')"
            clearable
            class="rw-filter-search"
            @change="fetchPackages"
            @clear="fetchPackages"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
          <el-select
            v-model="filters.projectCode"
            :placeholder="t('raven.projectFilterPlaceholder')"
            clearable
            class="rw-filter-control"
            :loading="projectOptionsLoading"
            @change="fetchPackages"
          >
            <el-option
              v-for="project in projectOptions"
              :key="project.id"
              :label="project.project_name || project.project_code"
              :value="project.project_code"
            />
            <el-option :label="t('raven.unassociatedProject')" :value="UNASSOCIATED_PROJECT" />
          </el-select>
          <el-input
            v-model="filters.version"
            :placeholder="t('raven.versionPlaceholder')"
            clearable
            class="rw-filter-control rw-filter-narrow"
            @change="fetchPackages"
            @clear="fetchPackages"
          />
          <el-input
            v-model="filters.tags"
            :placeholder="t('raven.tagPlaceholder')"
            clearable
            class="rw-filter-control"
            @change="fetchPackages"
            @clear="fetchPackages"
          />
          <el-select
            v-model="filters.isPatch"
            :placeholder="t('raven.patchTypePlaceholder')"
            clearable
            class="rw-filter-control rw-filter-narrow"
            @change="fetchPackages"
          >
            <el-option :label="t('raven.release')" value="false" />
            <el-option :label="t('raven.patch')" value="true" />
          </el-select>
          <div class="rw-filter-actions">
            <button class="rw-btn-primary" @click="fetchPackages">
              <el-icon><Search /></el-icon>
              {{ t('common.search') }}
            </button>
            <button class="rw-btn-secondary" @click="resetFilters">{{ t('common.reset') }}</button>
          </div>
        </div>
      </section>

      <section class="rw-card rw-list-card">
        <div class="rw-table-wrap">
          <el-table
            :data="packages"
            v-loading="loadingList"
            class="rw-table"
            :row-class-name="() => 'rw-row'"
            @row-click="openPackageDetail"
          >
            <el-table-column prop="name" :label="t('raven.colName')" min-width="340">
              <template #default="{ row }">
                <div class="rw-name-cell">
                  <div class="rw-name-head">
                    <span class="rw-pkg-name" :title="row.name">{{ row.name }}</span>
                    <span class="rw-pill" :class="projectPillClass(row.projectCode)">
                      {{ projectText(row.projectCode) }}
                    </span>
                    <span class="rw-pill rw-pill-neutral rw-pill-mono">v{{ row.version || t('raven.unknown') }}</span>
                    <span class="rw-pill" :class="isPatchPackage(row) ? 'rw-pill-warning' : 'rw-pill-success'">
                      {{ humanizePatch(row) }}
                    </span>
                  </div>
                  <p class="rw-pkg-desc" :title="row.metadata?.description">
                    {{ row.metadata?.description || t('raven.noDesc') }}
                  </p>
                </div>
              </template>
            </el-table-column>
            <el-table-column :label="t('raven.colTags')" min-width="160">
              <template #default="{ row }">
                <div class="rw-pill-group">
                  <span
                    v-for="tag in normalizeTags(row.metadata?.tags)"
                    :key="tag"
                    class="rw-pill rw-pill-neutral"
                  >
                    {{ tag }}
                  </span>
                  <span v-if="!normalizeTags(row.metadata?.tags).length" class="rw-cell-empty">—</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column :label="t('raven.colComponents')" min-width="200">
              <template #default="{ row }">
                <div class="rw-pill-group">
                  <span
                    v-for="comp in normalizeComponents(row.metadata?.components)"
                    :key="`${comp.name}-${comp.version || 'na'}`"
                    class="rw-pill rw-pill-info"
                  >
                    {{ comp.name }}<span v-if="comp.version" class="rw-pill-sub"> · {{ comp.version }}</span>
                  </span>
                  <span v-if="!normalizeComponents(row.metadata?.components).length" class="rw-cell-empty">—</span>
                </div>
              </template>
            </el-table-column>
            <el-table-column :label="t('raven.colSize')" prop="size" width="110">
              <template #default="{ row }">
                <span class="rw-cell-mono">{{ formatFileSize(row.size) }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('raven.colCreatedAt')" prop="createdAt" width="170">
              <template #default="{ row }">
                <span class="rw-cell-muted">{{ formatDateTime(row.createdAt) }}</span>
              </template>
            </el-table-column>
            <el-table-column :label="t('common.actions')" width="200" fixed="right">
              <template #default="{ row }">
                <div class="rw-row-actions" @click.stop>
                  <button class="rw-btn-ghost" @click="openPackageDetail(row.id)">{{ t('common.detail') }}</button>
                  <button class="rw-btn-ghost" @click="downloadPackage(row)">{{ t('common.download') }}</button>
                  <button class="rw-btn-ghost rw-btn-ghost-danger" @click="deletePackage(row)">{{ t('common.delete') }}</button>
                </div>
              </template>
            </el-table-column>
            <template #empty>
              <div class="rw-empty">{{ t('raven.emptyList') }}</div>
            </template>
          </el-table>
        </div>

        <div class="rw-pagination-row">
          <span class="rw-pagination-meta">{{ t('raven.paginationCount', { count: pagination.totalItems }) }}</span>
          <el-pagination
            background
            layout="prev, pager, next, jumper"
            :current-page="pagination.currentPage"
            :total="pagination.totalItems"
            :page-size="pagination.itemsPerPage"
            @current-change="handlePageChange"
          />
        </div>
      </section>

    </div>

    <el-dialog
      v-model="uploadDialogVisible"
      width="780px"
      destroy-on-close
      :close-on-click-modal="false"
      class="rw-dialog"
      :title="t('raven.uploadTitle')"
    >
      <div class="rw-upload-body">
        <div
          class="rw-upload-zone"
          :class="{ 'is-active': uploadZoneActive }"
          @dragover.prevent="uploadZoneActive = true"
          @dragleave.prevent="uploadZoneActive = false"
          @drop="handleDrop"
        >
          <input
            type="file"
            multiple
            accept=".tgz,.tar.gz,.upkg"
            class="rw-upload-input"
            id="raven-upload-input"
            @change="handleFileInput"
          />
          <el-icon class="rw-upload-icon"><UploadFilled /></el-icon>
          <p class="rw-upload-hint">{{ t('raven.dragHint') }}</p>
          <button
            class="rw-btn-secondary"
            @click="triggerUploadInput"
          >
            {{ t('raven.selectFile') }}
          </button>
          <p class="rw-upload-sub">{{ t('raven.uploadFormatHint') }}</p>
        </div>

        <div v-if="uploadFiles.length" class="rw-upload-files">
          <div class="rw-upload-files-head">
            <span>{{ t('raven.selectedCount', { count: uploadFiles.length }) }}</span>
            <button class="rw-btn-ghost" @click="uploadFiles = []">{{ t('raven.clear') }}</button>
          </div>
          <div class="rw-pill-group">
            <span
              v-for="file in uploadFiles"
              :key="file.name + file.size"
              class="rw-pill rw-pill-neutral rw-pill-removable"
            >
              {{ file.name }}
              <button class="rw-pill-close" @click="removeFile(file)" :aria-label="t('raven.removeFile')">×</button>
            </span>
          </div>
        </div>

        <div class="rw-upload-grid">
          <div class="rw-subcard">
            <h4 class="rw-subcard-title">{{ t('raven.metadata') }}</h4>
            <div class="rw-form-grid">
              <el-select
                v-model="uploadMeta.projectCode"
                :placeholder="t('raven.projectPlaceholder')"
                :loading="projectOptionsLoading"
                filterable
              >
                <el-option
                  v-for="project in packageSearchProjectOptions"
                  :key="project.id"
                  :label="project.project_name || project.project_code"
                  :value="project.project_code"
                />
              </el-select>
              <el-input v-model="uploadMeta.version" :placeholder="t('raven.versionPlaceholderInput')" />
              <el-select v-model="uploadMeta.isPatch" :placeholder="t('raven.patchTypeSelect')">
                <el-option :label="t('raven.release')" :value="false" />
                <el-option :label="t('raven.patch')" :value="true" />
              </el-select>
            </div>

            <el-input
              v-model="uploadMeta.description"
              type="textarea"
              :rows="3"
              :placeholder="t('raven.descPlaceholder')"
            />

            <div class="rw-field">
              <p class="rw-field-label">{{ t('raven.tagsSection') }}</p>
              <div class="rw-pill-group">
                <span
                  v-for="tag in uploadMeta.tags"
                  :key="tag"
                  class="rw-pill rw-pill-neutral rw-pill-removable"
                >
                  {{ tag }}
                  <button class="rw-pill-close" @click="removeTag(tag)" :aria-label="t('raven.removeTag')">×</button>
                </span>
                <el-input
                  v-model="tagDraft"
                  size="small"
                  :placeholder="t('raven.tagInputPlaceholder')"
                  class="rw-tag-input"
                  @keyup.enter="addTag"
                  @blur="addTag"
                />
              </div>
            </div>

            <div class="rw-field">
              <p class="rw-field-label">{{ t('raven.componentsSection') }}</p>
              <div class="rw-pill-group">
                <span
                  v-for="comp in uploadMeta.components"
                  :key="`${comp.name}-${comp.version || 'na'}`"
                  class="rw-pill rw-pill-info rw-pill-removable"
                >
                  {{ comp.name }}<span v-if="comp.version" class="rw-pill-sub"> · {{ comp.version }}</span>
                  <button class="rw-pill-close" @click="removeComponent(comp)" :aria-label="t('raven.componentsSection')">×</button>
                </span>
              </div>
              <div class="rw-component-input">
                <el-input v-model="componentDraft.name" size="small" :placeholder="t('raven.componentNamePlaceholder')" />
                <el-input v-model="componentDraft.version" size="small" :placeholder="t('raven.componentVersionPlaceholder')" />
                <button class="rw-btn-secondary" @click="addComponent">{{ t('common.add') }}</button>
              </div>
            </div>
          </div>

          <div class="rw-subcard">
            <h4 class="rw-subcard-title">{{ t('raven.uploadProgress') }}</h4>
            <el-progress :percentage="uploadProgress" :indeterminate="uploading && uploadProgress === 0" />
            <p class="rw-progress-status">{{ uploadStatus || t('raven.notStarted') }}</p>
            <div class="rw-upload-actions">
              <button
                class="rw-btn-primary"
                :disabled="!uploadFiles.length || !uploadMeta.projectCode || uploading"
                @click="startUpload"
              >
                <el-icon><UploadFilled /></el-icon>
                {{ uploading ? t('raven.uploadingBtn') : t('raven.startUpload') }}
              </button>
              <button v-if="uploading" class="rw-btn-secondary" @click="cancelUpload">{{ t('common.cancel') }}</button>
              <button class="rw-btn-ghost" :disabled="uploading" @click="clearUpload">{{ t('common.reset') }}</button>
            </div>
            <p class="rw-progress-hint">
              {{ t('raven.uploadIndexHint') }}
            </p>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="rw-dialog-footer">
          <button class="rw-btn-ghost" @click="uploadDialogVisible = false">{{ t('common.close') }}</button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="searchDialogVisible"
      width="880px"
      destroy-on-close
      :close-on-click-modal="false"
      class="rw-dialog"
      :title="t('raven.searchDialogTitle')"
      :before-close="(done: () => void) => { closeSearchDialog(); done() }"
    >
      <div class="rw-search-body">
        <div class="rw-search-bar">
          <el-select
            v-model="searchProjectRepoId"
            :placeholder="t('raven.projectPlaceholder')"
            :loading="projectOptionsLoading"
            filterable
            class="rw-search-project"
            :disabled="searchLoading"
          >
            <el-option
              v-for="project in packageSearchProjectOptions"
              :key="project.id"
              :label="project.project_name || project.project_code"
              :value="project.id"
            />
          </el-select>
          <el-input
            v-model="searchQuery"
            :placeholder="t('raven.searchInputPlaceholder')"
            clearable
            class="rw-search-input"
            :disabled="searchLoading"
            maxlength="1000"
            @keyup.enter="performSearch"
          >
            <template #prefix>
              <el-icon><MagicStick /></el-icon>
            </template>
          </el-input>
          <button
            v-if="!searchLoading"
            class="rw-btn-primary"
            :disabled="searchLoading || searchProjectRepoId === null"
            @click="performSearch"
          >
            <el-icon><Search /></el-icon>
            {{ t('raven.searchAi') }}
          </button>
          <button v-else class="rw-btn-secondary" @click="cancelSearch">
            {{ t('raven.stop') }}
          </button>
        </div>

        <div class="rw-suggestion-row">
          <span
            v-for="item in searchSuggestions"
            :key="item"
            class="rw-suggestion-chip"
            @click="triggerSuggestion(item)"
          >
            {{ item }}
          </span>
        </div>

        <AgentTraceStream
          v-if="searchTraceEvents.length > 0 || searchTraceRunning"
          class="rw-search-trace"
          :events="searchTraceEvents"
          :running="searchTraceRunning"
        />

        <div v-if="searchError" class="rw-search-error">
          {{ searchError }}
        </div>

        <div v-if="searchResult" class="rw-search-results">
          <div class="rw-card rw-answer-card">
            <div class="rw-answer-head">
              <span class="rw-pill rw-pill-ai">
                <el-icon><StarFilled /></el-icon>
                {{ t('raven.aiAnswer') }}
              </span>
              <span class="rw-answer-disclaimer">{{ t('raven.aiAnswerDisclaimer') }}</span>
            </div>
            <div class="rw-markdown" v-html="renderedAnswer" />
            <p v-if="searchResult.notes" class="rw-answer-notes">{{ searchResult.notes }}</p>
            <div v-if="searchWarnings.length" class="rw-answer-warnings">
              <span
                v-for="(warning, idx) in searchWarnings"
                :key="idx"
                class="rw-pill rw-pill-warning"
              >
                ⚠ {{ warning.message || warning.type }}
              </span>
            </div>
          </div>

          <div
            v-if="sortedRelevantPackages.length || searchPackagesLoading"
            v-loading="searchPackagesLoading"
            class="rw-card rw-match-card"
          >
            <div class="rw-match-head">
              <span class="rw-match-title">{{ t('raven.recommendedPkgs') }}</span>
              <span class="rw-match-meta">
                {{ t('raven.matchMeta', { rec: searchResult.recommended_package_ids.length, rel: searchResult.relevant_package_ids.length }) }}
              </span>
            </div>
            <div class="rw-match-grid">
              <div
                v-for="pkg in sortedRelevantPackages"
                :key="pkg.id"
                class="rw-match-item"
                :class="{ 'is-recommended': isRecommendedPackage(pkg) }"
              >
                <div class="rw-match-item-head">
                  <div class="rw-match-item-meta">
                    <div class="rw-match-item-title">
                      <span class="rw-pkg-name">{{ pkg.name }}</span>
                      <span class="rw-pill" :class="projectPillClass(pkg.projectCode)">
                        {{ projectText(pkg.projectCode) }}
                      </span>
                      <span class="rw-pill rw-pill-neutral rw-pill-mono">v{{ pkg.version || t('raven.unknown') }}</span>
                    </div>
                    <p class="rw-match-item-desc">
                      {{ pkg.metadata?.description || t('raven.noDesc') }}
                    </p>
                  </div>
                  <div class="rw-match-item-tags">
                    <span v-if="isRecommendedPackage(pkg)" class="rw-pill rw-pill-ai">
                      <el-icon><StarFilled /></el-icon>
                      {{ t('raven.aiRecommended') }}
                    </span>
                  </div>
                </div>
                <div class="rw-pill-group">
                  <span
                    v-for="tag in normalizeTags(pkg.metadata?.tags)"
                    :key="tag"
                    class="rw-pill rw-pill-neutral"
                  >
                    {{ tag }}
                  </span>
                </div>
                <div class="rw-match-item-actions">
                  <button class="rw-btn-secondary" @click="openSearchPackageDetail(pkg)">{{ t('common.detail') }}</button>
                  <button class="rw-btn-primary" @click="downloadPackage(pkg)">{{ t('common.download') }}</button>
                </div>
              </div>
            </div>
          </div>

          <div
            v-else-if="!searchPackagesLoading"
            class="rw-search-empty"
          >
            {{ t('raven.noRecommend') }}
          </div>
        </div>

        <div
          v-else-if="!searchTraceRunning && searchTraceEvents.length === 0"
          class="rw-search-empty"
        >
          {{ t('raven.searchEmptyHint') }}
        </div>
      </div>

      <template #footer>
        <div class="rw-dialog-footer">
          <button class="rw-btn-ghost" @click="closeSearchDialog">{{ t('common.close') }}</button>
        </div>
      </template>
    </el-dialog>

    <el-dialog
      v-model="searchDetailVisible"
      width="780px"
      destroy-on-close
      :close-on-click-modal="false"
      class="rw-dialog"
      :title="t('raven.pkgDetailTitle')"
    >
      <div v-loading="searchDetailLoading" class="rw-detail-body">
        <div class="rw-detail-head">
          <h2 class="rw-detail-title">
            {{ searchDetailPackage?.name || t('raven.loadingPkg') }}
          </h2>
          <div class="rw-pill-group">
            <span v-if="searchDetailPackage" class="rw-pill rw-pill-neutral rw-pill-mono">
              v{{ searchDetailPackage.version || t('raven.unknown') }}
            </span>
            <span v-if="searchDetailPackage" class="rw-pill" :class="projectPillClass(searchDetailPackage.projectCode)">
              {{ projectText(searchDetailPackage.projectCode) }}
            </span>
            <span
              v-if="searchDetailPackage"
              class="rw-pill"
              :class="isPatchPackage(searchDetailPackage) ? 'rw-pill-warning' : 'rw-pill-success'"
            >
              {{ humanizePatch(searchDetailPackage) }}
            </span>
          </div>
        </div>

        <div class="rw-detail-meta">
          <div>
            <span class="rw-detail-label">{{ t('raven.colCreatedAt') }}</span>
            <span class="rw-detail-value">{{ searchDetailPackage ? formatDateTime(searchDetailPackage.createdAt) : '-' }}</span>
          </div>
          <div>
            <span class="rw-detail-label">{{ t('raven.colSize') }}</span>
            <span class="rw-detail-value">{{ searchDetailPackage ? formatFileSize(searchDetailPackage.size) : '-' }}</span>
          </div>
        </div>

        <div class="rw-field">
          <p class="rw-field-label">{{ t('raven.descSection') }}</p>
          <div class="rw-markdown rw-detail-desc" v-html="searchDetailDescription" />
        </div>

        <div v-if="normalizeTags(searchDetailPackage?.metadata?.tags).length" class="rw-field">
          <p class="rw-field-label">{{ t('raven.tagsSection') }}</p>
          <div class="rw-pill-group">
            <span
              v-for="tag in normalizeTags(searchDetailPackage?.metadata?.tags)"
              :key="tag"
              class="rw-pill rw-pill-neutral"
            >
              {{ tag }}
            </span>
          </div>
        </div>

        <div v-if="normalizeComponents(searchDetailPackage?.metadata?.components).length" class="rw-field">
          <p class="rw-field-label">{{ t('raven.componentsSection') }}</p>
          <div class="rw-pill-group">
            <span
              v-for="comp in normalizeComponents(searchDetailPackage?.metadata?.components)"
              :key="`${comp.name}-${comp.version || 'na'}`"
              class="rw-pill rw-pill-info"
            >
              {{ comp.name }}<span v-if="comp.version" class="rw-pill-sub"> · {{ comp.version }}</span>
            </span>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="rw-dialog-footer rw-dialog-footer-split">
          <span class="rw-detail-footnote">{{ t('raven.detailFootnote') }}</span>
          <div class="rw-dialog-footer-actions">
            <button
              v-if="searchDetailPackage"
              class="rw-btn-secondary"
              @click="downloadPackage(searchDetailPackage)"
            >
              {{ t('common.download') }}
            </button>
            <button class="rw-btn-primary" @click="searchDetailVisible = false">{{ t('common.close') }}</button>
          </div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.rw-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--rw-canvas, #fff);
  font-family: var(--rw-sans, Inter, system-ui, sans-serif);
  color: var(--rw-ink, #171717);
}

.rw-page-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 24px 28px 32px;
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.rw-card {
  background: var(--rw-canvas, #fff);
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
  border-radius: 12px;
  padding: 20px;
}

.rw-filter-card { padding: 16px 20px; }

.rw-filter-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  width: 100%;
}

.rw-filter-search {
  flex: 1 1 240px;
  min-width: 220px;
}

.rw-filter-control { width: 160px; flex-shrink: 0; }
.rw-filter-narrow { width: 130px; }

.rw-filter-actions {
  margin-left: auto;
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

/* Buttons */
.rw-btn-primary {
  height: 32px;
  padding: 0 14px;
  background: var(--rw-primary, #000);
  color: var(--rw-on-primary, #fff);
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  border: none;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: inherit;
  transition: background 0.15s ease;
  white-space: nowrap;
}
.rw-btn-primary:hover:not(:disabled) { background: var(--rw-primary-active, #1a1a1a); }
.rw-btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

.rw-btn-secondary {
  height: 32px;
  padding: 0 14px;
  background: var(--rw-canvas, #fff);
  color: var(--rw-ink, #171717);
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: inherit;
  transition: background 0.15s ease;
  white-space: nowrap;
}
.rw-btn-secondary:hover:not(:disabled) { background: var(--rw-surface-strong, #f0f0f3); }
.rw-btn-secondary:disabled { opacity: 0.5; cursor: not-allowed; }

.rw-btn-danger {
  height: 32px;
  padding: 0 14px;
  background: var(--rw-danger, #c0382b);
  color: #fff;
  border: none;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: inherit;
}

.rw-btn-ghost {
  background: none;
  color: var(--rw-body, #60646c);
  height: 28px;
  padding: 0 8px;
  border-radius: 6px;
  border: none;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  font-family: inherit;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  transition: background 0.15s ease, color 0.15s ease;
}
.rw-btn-ghost:hover:not(:disabled) {
  background: var(--rw-surface-strong, #f0f0f3);
  color: var(--rw-ink, #171717);
}
.rw-btn-ghost:disabled { opacity: 0.5; cursor: not-allowed; }
.rw-btn-ghost-danger { color: var(--rw-danger, #c0382b); }
.rw-btn-ghost-danger:hover:not(:disabled) {
  background: rgba(192, 56, 43, 0.08);
  color: var(--rw-danger, #c0382b);
}

.rw-icon-btn {
  width: 30px;
  height: 30px;
  border-radius: 6px;
  background: transparent;
  border: none;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--rw-body, #60646c);
}
.rw-icon-btn:hover { background: var(--rw-surface-strong, #f0f0f3); }

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
  line-height: 1;
  white-space: nowrap;
}
.rw-pill-neutral { background: var(--rw-surface-strong, #f0f0f3); color: var(--rw-body, #60646c); }
.rw-pill-success { background: rgba(22, 163, 74, 0.12); color: #15803d; }
.rw-pill-info { background: var(--rw-surface-strong, #f0f0f3); color: var(--rw-ink, #171717); }
.rw-pill-warning { background: rgba(171, 100, 0, 0.10); color: #ab6400; }
.rw-pill-danger { background: rgba(192, 56, 43, 0.10); color: #c0382b; }
.rw-pill-preview { background: rgba(129, 69, 181, 0.10); color: #8145b5; }
.rw-pill-ai {
  background: var(--rw-surface-dark, #171717);
  color: var(--rw-on-primary, #fff);
}
.rw-pill-mono { font-family: var(--rw-mono, JetBrains Mono, monospace); font-weight: 500; }
.rw-pill-sub { font-weight: 500; opacity: 0.8; }

.rw-pill-group {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.rw-pill-removable { padding-right: 4px; }
.rw-pill-close {
  border: none;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
  padding: 0 2px;
  margin-left: 2px;
  opacity: 0.6;
}
.rw-pill-close:hover { opacity: 1; }

/* Element Plus input overrides */
:deep(.el-input__wrapper),
:deep(.el-select .el-select__wrapper),
:deep(.el-textarea__inner) {
  background: var(--rw-canvas, #fff) !important;
  border: 1px solid var(--rw-hairline-strong, #dcdee0) !important;
  border-radius: 8px !important;
  box-shadow: none !important;
  min-height: 36px;
  font-family: inherit;
}
:deep(.el-input__wrapper.is-focus),
:deep(.el-input__wrapper:hover),
:deep(.el-select .el-select__wrapper.is-focused),
:deep(.el-select .el-select__wrapper:hover),
:deep(.el-textarea__inner:focus) {
  border-color: var(--rw-ink, #171717) !important;
  box-shadow: none !important;
}
:deep(.el-input__inner)::placeholder,
:deep(.el-textarea__inner)::placeholder {
  color: var(--rw-muted, #999);
}
:deep(.el-input__inner) { color: var(--rw-ink, #171717); }

/* Table */
.rw-list-card { padding: 0; }
.rw-table-wrap { width: 100%; overflow-x: auto; }

:deep(.el-table) {
  --el-table-border-color: var(--rw-hairline, #f0f0f3);
  --el-table-header-bg-color: var(--rw-canvas, #fff);
  --el-table-row-hover-bg-color: var(--rw-hairline-soft, #f5f5f7);
  background: transparent;
  color: var(--rw-ink, #171717);
  font-family: inherit;
}
:deep(.el-table th.el-table__cell) {
  background: var(--rw-canvas, #fff);
  border-bottom: 1px solid var(--rw-hairline-strong, #dcdee0);
  padding: 12px 14px;
}
:deep(.el-table th.el-table__cell .cell) {
  font-size: 10.5px;
  font-weight: 600;
  color: var(--rw-muted, #999);
  letter-spacing: 0.6px;
  text-transform: uppercase;
  padding: 0;
}
:deep(.el-table td.el-table__cell) {
  border-bottom: 1px solid var(--rw-hairline, #f0f0f3);
  padding: 14px;
  font-size: 13px;
}
:deep(.el-table td.el-table__cell .cell) { padding: 0; }
:deep(.el-table--enable-row-hover .el-table__body tr:hover > td.el-table__cell) {
  background: var(--rw-hairline-soft, #f5f5f7);
}
:deep(.el-table .rw-row) { cursor: pointer; }
:deep(.el-table::before),
:deep(.el-table--border::after) { display: none; }

.rw-name-cell { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.rw-name-head { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
.rw-pkg-name {
  font-size: 14.5px;
  font-weight: 600;
  color: var(--rw-ink, #171717);
  max-width: 360px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rw-pkg-desc {
  margin: 0;
  font-size: 12.5px;
  color: var(--rw-body, #60646c);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rw-cell-empty { color: var(--rw-muted-soft, #cccccc); font-size: 12px; }
.rw-cell-mono { font-family: var(--rw-mono, JetBrains Mono, monospace); font-size: 12px; color: var(--rw-body, #60646c); }
.rw-cell-muted { color: var(--rw-body, #60646c); font-size: 12.5px; }

.rw-row-actions { display: flex; align-items: center; gap: 4px; }

.rw-pagination-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
  border-top: 1px solid var(--rw-hairline, #f0f0f3);
  font-size: 13px;
  color: var(--rw-body, #60646c);
}
.rw-pagination-meta { font-size: 12.5px; color: var(--rw-muted, #999); }

:deep(.el-pagination) {
  font-size: 13px;
  color: var(--rw-body, #60646c);
  --el-pagination-bg-color: var(--rw-canvas, #fff);
}
:deep(.el-pagination .btn-prev),
:deep(.el-pagination .btn-next),
:deep(.el-pagination .el-pager li) {
  background: var(--rw-canvas, #fff) !important;
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
  border-radius: 6px;
  color: var(--rw-ink, #171717);
  min-width: 30px;
  height: 30px;
  margin: 0 2px;
  font-weight: 500;
}
:deep(.el-pagination .el-pager li.is-active) {
  background: var(--rw-ink, #171717) !important;
  color: var(--rw-on-primary, #fff) !important;
  border-color: var(--rw-ink, #171717);
}
:deep(.el-pagination .el-pagination__jump) { color: var(--rw-body, #60646c); }

.rw-empty {
  text-align: center;
  color: var(--rw-muted, #999);
  font-size: 13px;
  padding: 48px 0;
}

/* Dialog */
:deep(.rw-dialog) {
  border-radius: 14px;
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.18);
  padding: 0;
}
:deep(.rw-dialog .el-dialog__header) {
  padding: 22px 22px 0;
  margin: 0;
}
:deep(.rw-dialog .el-dialog__title) {
  font-size: 16px;
  font-weight: 600;
  color: var(--rw-ink, #171717);
}
:deep(.rw-dialog .el-dialog__headerbtn) {
  top: 18px;
  right: 18px;
}
:deep(.rw-dialog .el-dialog__body) {
  padding: 18px 22px;
  color: var(--rw-ink, #171717);
}
:deep(.rw-dialog .el-dialog__footer) {
  padding: 12px 22px 22px;
}

.rw-dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.rw-dialog-footer-split { justify-content: space-between; align-items: center; }
.rw-dialog-footer-actions { display: flex; gap: 8px; }

/* Upload dialog */
.rw-upload-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.rw-upload-zone {
  border: 1.5px dashed var(--rw-hairline-strong, #dcdee0);
  border-radius: 12px;
  padding: 28px 16px;
  text-align: center;
  background: var(--rw-canvas-soft, #fafafa);
  transition: border-color 0.2s ease, background 0.2s ease;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.rw-upload-zone.is-active {
  border-color: var(--rw-ink, #171717);
  background: var(--rw-hairline-soft, #f5f5f7);
}
.rw-upload-input { display: none; }
.rw-upload-icon { font-size: 28px; color: var(--rw-muted, #999); }
.rw-upload-hint { margin: 0; font-size: 13.5px; color: var(--rw-ink, #171717); font-weight: 500; }
.rw-upload-sub { margin: 4px 0 0; font-size: 12px; color: var(--rw-muted, #999); }

.rw-upload-files {
  background: var(--rw-canvas-soft, #fafafa);
  border: 1px solid var(--rw-hairline, #f0f0f3);
  border-radius: 10px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.rw-upload-files-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 12.5px;
  color: var(--rw-body, #60646c);
}

.rw-upload-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}
@media (max-width: 820px) {
  .rw-upload-grid { grid-template-columns: 1fr; }
}

.rw-subcard {
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
  border-radius: 12px;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.rw-subcard-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--rw-ink, #171717);
  letter-spacing: 0.2px;
}

.rw-form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.rw-form-grid > :last-child:nth-child(odd) { grid-column: span 2; }

.rw-field { display: flex; flex-direction: column; gap: 8px; }
.rw-field-label {
  margin: 0;
  font-size: 12px;
  font-weight: 600;
  color: var(--rw-muted, #999);
  text-transform: uppercase;
  letter-spacing: 0.6px;
}

.rw-tag-input { width: 180px; }
.rw-component-input {
  display: flex;
  gap: 8px;
  align-items: center;
}
.rw-component-input :deep(.el-input) { flex: 1; }

.rw-progress-status {
  margin: 4px 0 0;
  font-size: 12.5px;
  color: var(--rw-body, #60646c);
  min-height: 18px;
}
.rw-progress-hint {
  margin: 0;
  font-size: 11.5px;
  color: var(--rw-muted, #999);
}
.rw-upload-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
}

:deep(.el-progress-bar__outer) {
  background: var(--rw-surface-strong, #f0f0f3) !important;
}
:deep(.el-progress-bar__inner) {
  background: var(--rw-ink, #171717) !important;
}
:deep(.el-progress__text) { color: var(--rw-body, #60646c) !important; font-size: 12px !important; }

/* Search dialog */
.rw-search-body { display: flex; flex-direction: column; gap: 14px; }

.rw-search-bar { display: flex; gap: 8px; align-items: center; }
.rw-search-project { width: 220px; flex-shrink: 0; }
.rw-search-input { flex: 1; }

.rw-search-tools {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 12.5px;
  color: var(--rw-body, #60646c);
}
.rw-search-stat { font-size: 12px; color: var(--rw-muted, #999); }

.rw-suggestion-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.rw-suggestion-chip {
  display: inline-flex;
  align-items: center;
  height: 26px;
  padding: 0 10px;
  border-radius: 999px;
  background: var(--rw-surface-strong, #f0f0f3);
  color: var(--rw-body, #60646c);
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}
.rw-suggestion-chip:hover {
  background: var(--rw-surface-dark, #171717);
  color: var(--rw-on-primary, #fff);
}

.rw-search-empty {
  text-align: center;
  color: var(--rw-muted, #999);
  font-size: 13px;
  padding: 40px 0;
}

.rw-search-results { display: flex; flex-direction: column; gap: 14px; }

.rw-answer-card { padding: 18px; display: flex; flex-direction: column; gap: 12px; }
.rw-answer-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.rw-answer-disclaimer { font-size: 11.5px; color: var(--rw-muted, #999); }

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

.rw-match-card { padding: 18px; display: flex; flex-direction: column; gap: 12px; }
.rw-match-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.rw-match-title { font-size: 13px; font-weight: 600; color: var(--rw-ink, #171717); }
.rw-match-meta { font-size: 12px; color: var(--rw-muted, #999); }

.rw-match-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}
@media (max-width: 820px) {
  .rw-match-grid { grid-template-columns: 1fr; }
}

.rw-match-item {
  border: 1px solid var(--rw-hairline-strong, #dcdee0);
  border-radius: 12px;
  padding: 14px;
  background: var(--rw-canvas, #fff);
  display: flex;
  flex-direction: column;
  gap: 10px;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.rw-match-item:hover {
  border-color: var(--rw-ink, #171717);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.04);
}
.rw-match-item.is-recommended {
  border-color: var(--rw-ink, #171717);
  background: var(--rw-canvas, #fff);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.06);
}

.rw-match-item-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 8px;
}
.rw-match-item-meta { min-width: 0; flex: 1; }
.rw-match-item-title { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.rw-match-item-desc {
  margin: 6px 0 0;
  font-size: 12.5px;
  color: var(--rw-body, #60646c);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.rw-match-item-tags { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; flex-shrink: 0; }
.rw-match-item-actions { display: flex; gap: 8px; }

/* Detail dialog */
.rw-detail-body { display: flex; flex-direction: column; gap: 16px; }
.rw-detail-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}
.rw-detail-title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: var(--rw-ink, #171717);
}
.rw-detail-meta {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  font-size: 12.5px;
  color: var(--rw-body, #60646c);
}
.rw-detail-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--rw-muted, #999);
  text-transform: uppercase;
  letter-spacing: 0.6px;
  margin-right: 6px;
}
.rw-detail-value { font-family: var(--rw-mono, JetBrains Mono, monospace); font-size: 12px; color: var(--rw-ink, #171717); }
.rw-detail-desc { font-size: 13px; color: var(--rw-body, #60646c); }
.rw-detail-footnote { font-size: 11.5px; color: var(--rw-muted, #999); }

@media (max-width: 900px) {
  .rw-page-scroll { padding: 16px; gap: 16px; }
}
</style>
