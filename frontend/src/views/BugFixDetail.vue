<script setup lang="ts">
import { computed, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { ArrowLeft, ExternalLink, GitBranch, RefreshCw, RotateCcw } from 'lucide-vue-next'
import WorkbenchTopbar from '@/layouts/WorkbenchTopbar.vue'
import AgentTraceStream from '@/components/AgentTraceStream.vue'
import { useBugFixStore } from '@/stores/bugFixes'
import type {
  BugFixChangedFile,
  BugFixDiffStat,
  BugFixFixOutcome,
  BugFixMergeRequest,
  BugFixMergeRequestStatus,
  BugFixProposedFix,
  BugFixTaskStatus,
} from '@/types'

const route = useRoute()
const router = useRouter()
const bugFixStore = useBugFixStore()
const { t } = useI18n()

const taskId = computed(() => String(route.params.id || ''))
const task = computed(() => bugFixStore.currentTask)
const canRetry = computed(() => String(task.value?.status || '') === 'failed')
const reviewWithoutChanges = computed(() => {
  const outcomes = task.value?.fix_outcomes || []
  return task.value?.status === 'succeeded' && outcomes.length > 0 &&
    outcomes.every(item => ['rejected', 'already_implemented', 'skipped'].includes(item.outcome))
})
const topbarMeta = computed(() => {
  if (!task.value) return t('bugFix.loading')
  const project = task.value.project_name || task.value.project_code || t('bugFix.noProject')
  return `${project} · ${statusText(task.value.status)}`
})

const statusMeta: Record<string, { text: string; className: string }> = {
  pending: { text: t('bugFix.statusText.pending'), className: 'rw-pill-neutral' },
  running: { text: t('bugFix.statusText.running'), className: 'rw-pill-info' },
  succeeded: { text: t('bugFix.statusText.succeeded'), className: 'rw-pill-success' },
  partial: { text: t('bugFix.statusText.partial'), className: 'rw-pill-warning' },
  failed: { text: t('bugFix.statusText.failed'), className: 'rw-pill-danger' },
  cancelled: { text: t('bugFix.statusText.cancelled'), className: 'rw-pill-neutral' },
}

const mrStatusMeta: Record<string, { text: string; className: string }> = {
  created: { text: t('bugFix.statusText.created'), className: 'rw-pill-success' },
  open: { text: t('bugFix.statusText.open'), className: 'rw-pill-success' },
  push_failed: { text: t('bugFix.statusText.push_failed'), className: 'rw-pill-danger' },
  mr_failed: { text: t('bugFix.statusText.mr_failed'), className: 'rw-pill-danger' },
}

const statusText = (status: BugFixTaskStatus) =>
  statusMeta[String(status)]?.text || String(status || t('bugFix.statusText.unknown'))

const statusClass = (status: BugFixTaskStatus) =>
  statusMeta[String(status)]?.className || 'rw-pill-neutral'

const mrStatusText = (status: BugFixMergeRequestStatus) =>
  mrStatusMeta[String(status)]?.text || String(status || t('bugFix.statusText.unknown'))

const mrStatusClass = (status: BugFixMergeRequestStatus) =>
  mrStatusMeta[String(status)]?.className || 'rw-pill-neutral'

// 逐个拟修复项的处理结局：把每个 proposed_fix 与它的结局配对，让「拟修复项数 >
// MR 数」的差异可被逐项解释（如某项已在基线实现，因而没有产出 MR）。
const outcomeMeta: Record<string, { text: string; className: string }> = {
  created_mr: { text: t('bugFix.outcome.created_mr'), className: 'rw-pill-success' },
  already_implemented: { text: t('bugFix.outcome.already_implemented'), className: 'rw-pill-info' },
  rejected: { text: t('bugFix.outcome.rejected'), className: 'rw-pill-warning' },
  skipped: { text: t('bugFix.outcome.skipped'), className: 'rw-pill-neutral' },
  failed: { text: t('bugFix.outcome.failed'), className: 'rw-pill-danger' },
}

const outcomeText = (outcome?: BugFixFixOutcome) =>
  outcome ? outcomeMeta[String(outcome.outcome)]?.text || String(outcome.outcome) : ''

const outcomeClass = (outcome?: BugFixFixOutcome) =>
  (outcome && outcomeMeta[String(outcome.outcome)]?.className) || 'rw-pill-neutral'

const outcomeForIndex = (index: number): BugFixFixOutcome | undefined => {
  const list = task.value?.fix_outcomes || []
  if (!list.length) return undefined
  const oneBased = index + 1
  const proposed = task.value?.proposed_fixes || []
  return (
    list.find((o) => o.fix_index != null && Number(o.fix_index) === oneBased) ||
    list.find((o) => !!o.title && o.title === proposed[index]?.title) ||
    list[index]
  )
}

// 把 proposed_fixes 与其结局打包，模板里直接遍历，避免每格重复查找。
const fixRows = computed<
  Array<{ fix: BugFixProposedFix; index: number; outcome?: BugFixFixOutcome }>
>(() =>
  (task.value?.proposed_fixes || []).map((fix, index) => ({
    fix,
    index,
    outcome: outcomeForIndex(index),
  })),
)

const formatDateTime = (value?: string | null) => {
  if (!value) return '-'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    hour12: false,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

const normalizeChangedFiles = (value: unknown): BugFixChangedFile[] => {
  if (!value) return []
  if (Array.isArray(value)) return value as BugFixChangedFile[]
  if (typeof value === 'object') {
    return Object.entries(value as Record<string, any>).map(([path, stat]) => ({
      path,
      ...(stat && typeof stat === 'object' ? stat : {}),
    }))
  }
  return []
}

const fileName = (file: BugFixChangedFile) =>
  file.path || file.file_path || file.filename || file.name || t('bugFix.unknownFile')

const additions = (file: BugFixChangedFile) =>
  Number(file.additions ?? file.insertions ?? file.added ?? 0) || 0

const deletions = (file: BugFixChangedFile) =>
  Number(file.deletions ?? file.removed ?? 0) || 0

const diffStat = (mr: BugFixMergeRequest) => (mr.diff_stat || {}) as BugFixDiffStat

const diffStatText = (mr: BugFixMergeRequest) => {
  const files = normalizeChangedFiles(mr.changed_files)
  const stat = diffStat(mr)
  const fileCount = Number(stat.files ?? stat.file_count ?? files.length ?? 0) || 0
  const added =
    Number(stat.additions ?? stat.insertions ?? files.reduce((sum, file) => sum + additions(file), 0)) || 0
  const removed =
    Number(stat.deletions ?? stat.removed ?? files.reduce((sum, file) => sum + deletions(file), 0)) || 0
  return t('bugFix.fileStats', { count: fileCount, added, removed })
}

const shortSha = (sha?: string | null) => (sha ? sha.slice(0, 10) : '-')

const loadDetail = async () => {
  if (!taskId.value) return
  try {
    await bugFixStore.fetchDetail(taskId.value)
  } catch (error: any) {
    ElMessage.error(bugFixStore.error || error?.message || t('bugFix.fetchDetailFail'))
  }
}

const retryTask = async () => {
  if (!taskId.value || !canRetry.value || bugFixStore.retrying) return
  try {
    await bugFixStore.retryTask(taskId.value)
    ElMessage.success(t('bugFix.retrySuccess'))
  } catch (error: any) {
    ElMessage.error(bugFixStore.error || error?.message || t('bugFix.retryFail'))
  }
}

onMounted(() => {
  bugFixStore.resetCurrent()
  loadDetail()
})

onUnmounted(() => {
  bugFixStore.resetCurrent()
})
</script>

<template>
  <div class="rw-page bug-fix-detail-page">
    <WorkbenchTopbar :title="task?.title || $t('bugFix.detailTitle')" :meta="topbarMeta">
      <template #actions>
        <button class="rw-btn-secondary" @click="router.push('/bug-fixes')">
          <ArrowLeft :size="14" />
          <span>{{ $t('bugFix.backToList') }}</span>
        </button>
        <button
          v-if="canRetry"
          class="rw-btn-primary"
          :disabled="bugFixStore.retrying"
          @click="retryTask"
        >
          <RotateCcw :size="14" :class="{ spin: bugFixStore.retrying }" />
          <span>{{ $t(bugFixStore.retrying ? 'bugFix.retrying' : 'bugFix.retry') }}</span>
        </button>
        <button class="rw-btn-secondary" :disabled="bugFixStore.detailLoading" @click="loadDetail">
          <RefreshCw :size="14" :class="{ spin: bugFixStore.detailLoading }" />
          <span>{{ $t('bugFix.refresh') }}</span>
        </button>
      </template>
    </WorkbenchTopbar>

    <div class="rw-page-scroll">
      <section v-if="bugFixStore.detailLoading && !task" class="rw-card loading-card">
        <el-skeleton :rows="6" animated />
      </section>

      <section v-else-if="!task" class="rw-card empty-card">
        <h2>{{ $t('bugFix.notFound') }}</h2>
        <p>{{ $t('bugFix.notFoundDesc') }}</p>
      </section>

      <template v-else>
        <section class="rw-card title-card">
          <div class="title-main">
            <div>
              <h1>{{ task.title }}</h1>
              <p v-if="task.summary">{{ task.summary }}</p>
            </div>
            <span :class="['rw-pill', statusClass(task.status)]">{{ statusText(task.status) }}</span>
          </div>
          <div class="title-meta-grid">
            <div>
              <span>{{ $t('bugFix.project') }}</span>
              <strong>{{ task.project_name || task.project_code || '-' }}</strong>
            </div>
            <div>
              <span>{{ $t('bugFix.sourceLog') }}</span>
              <router-link v-if="task.source_log_id" :to="`/log/${task.source_log_id}`">
                {{ task.source_log_id.slice(0, 8) }}
              </router-link>
              <strong v-else>-</strong>
            </div>
            <div>
              <span>{{ $t('bugFix.mrCount') }}</span>
              <strong>{{ task.merge_request_count }}</strong>
            </div>
            <div>
              <span>{{ $t('bugFix.createdAt') }}</span>
              <strong>{{ formatDateTime(task.created_at) }}</strong>
            </div>
          </div>
        </section>

        <div class="detail-grid">
          <section class="rw-card info-card">
            <div class="section-head">
              <h2>{{ $t('bugFix.fixItems') }}</h2>
              <span class="rw-pill rw-pill-neutral">{{ task.proposed_fixes.length }}</span>
            </div>
            <div v-if="fixRows.length" class="fix-list">
              <article v-for="row in fixRows" :key="row.index" class="fix-row">
                <div class="fix-index">{{ row.index + 1 }}</div>
                <div class="fix-body">
                  <div class="fix-head">
                    <h3>{{ row.fix.title || $t('bugFix.fixItemTitle', { index: row.index + 1 }) }}</h3>
                    <span
                      v-if="row.outcome"
                      :class="['rw-pill', outcomeClass(row.outcome)]"
                    >{{ outcomeText(row.outcome) }}</span>
                  </div>
                  <p v-if="row.fix.description">{{ row.fix.description }}</p>
                  <p v-if="row.fix.rationale" class="fix-rationale">{{ row.fix.rationale }}</p>
                  <p v-if="row.outcome?.reason" class="fix-outcome-reason">
                    {{ row.outcome.reason }}
                  </p>
                  <a
                    v-if="row.outcome?.mr_url"
                    :href="row.outcome.mr_url"
                    target="_blank"
                    rel="noreferrer"
                    class="fix-mr-link"
                  >
                    {{ $t('bugFix.viewOutcomeMr') }}
                    <ExternalLink :size="12" />
                  </a>
                  <div v-if="row.fix.suspected_files?.length || row.fix.suspected_symbols?.length" class="fix-tags">
                    <code v-for="file in row.fix.suspected_files || []" :key="`file-${file}`">{{ file }}</code>
                    <code v-for="symbol in row.fix.suspected_symbols || []" :key="`symbol-${symbol}`">{{ symbol }}</code>
                  </div>
                </div>
              </article>
            </div>
            <p v-else class="muted-text">{{ $t('bugFix.noFixItems') }}</p>
          </section>

          <section class="rw-card info-card">
            <div class="section-head">
              <h2>{{ $t('bugFix.executionInfo') }}</h2>
            </div>
            <AgentTraceStream v-if="task.trace_events?.length" :events="task.trace_events" />
            <dl class="info-list">
              <div v-if="task.model">
                <dt>{{ $t('bugFix.model') }}</dt><dd>{{ task.model }}</dd>
              </div>
              <div v-if="task.context_availability">
                <dt>{{ $t('bugFix.context') }}</dt>
                <dd>{{ $t(task.context_availability === 'snapshot' ? 'bugFix.contextSnapshot' : 'bugFix.contextLegacy') }}</dd>
              </div>
              <div>
                <dt>{{ $t('bugFix.startTime') }}</dt>
                <dd>{{ formatDateTime(task.started_at) }}</dd>
              </div>
              <div>
                <dt>{{ $t('bugFix.finishTime') }}</dt>
                <dd>{{ formatDateTime(task.finished_at) }}</dd>
              </div>
              <div>
                <dt>{{ $t('bugFix.analysisTask') }}</dt>
                <dd>{{ task.source_analysis_task_id || '-' }}</dd>
              </div>
              <div v-if="task.error">
                <dt>{{ $t('bugFix.error') }}</dt>
                <dd class="error-text">{{ task.error }}</dd>
              </div>
            </dl>
          </section>
        </div>

        <section class="mr-section">
          <div class="section-head">
            <h2>Merge Requests</h2>
            <span class="rw-pill rw-pill-neutral">{{ task.merge_requests.length }}</span>
          </div>

          <div v-if="!task.merge_requests.length" class="rw-card empty-card compact">
            <h2>{{ $t(reviewWithoutChanges ? 'bugFix.reviewComplete' : 'bugFix.noMr') }}</h2>
            <p>{{ $t(reviewWithoutChanges ? 'bugFix.reviewNoChanges' : 'bugFix.noMrDesc') }}</p>
          </div>

          <article
            v-for="mr in task.merge_requests"
            :key="mr.id"
            class="mr-card"
          >
            <div class="mr-card-head">
              <div class="mr-title-wrap">
                <h3>{{ mr.title }}</h3>
                <div class="branch-line">
                  <GitBranch :size="13" />
                  <code>{{ mr.branch_name }}</code>
                  <span>→</span>
                  <code>{{ mr.base_branch }}</code>
                </div>
              </div>
              <span :class="['rw-pill', mrStatusClass(mr.status)]">{{ mrStatusText(mr.status) }}</span>
            </div>

            <div class="mr-meta-grid">
              <div>
                <span>MR IID</span>
                <strong>{{ mr.mr_iid || '-' }}</strong>
              </div>
              <div>
                <span>Commit</span>
                <strong class="mono">{{ shortSha(mr.commit_sha) }}</strong>
              </div>
              <div>
                <span>Diff</span>
                <strong>{{ diffStatText(mr) }}</strong>
              </div>
              <a
                v-if="mr.mr_url"
                :href="mr.mr_url"
                target="_blank"
                rel="noreferrer"
                class="mr-link"
              >
                {{ $t('bugFix.openMr') }}
                <ExternalLink :size="13" />
              </a>
            </div>

            <div class="changed-files">
              <div
                v-for="file in normalizeChangedFiles(mr.changed_files)"
                :key="fileName(file)"
                class="changed-file-row"
              >
                <code>{{ fileName(file) }}</code>
                <span>
                  <b>+{{ additions(file) }}</b>
                  <em>-{{ deletions(file) }}</em>
                </span>
              </div>
              <p v-if="!normalizeChangedFiles(mr.changed_files).length" class="muted-text">
                {{ $t('bugFix.noStats') }}
              </p>
            </div>
          </article>
        </section>
      </template>
    </div>
  </div>
</template>

<style scoped>
.rw-page {
  /* --rw-* tokens come from src/styles/theme.css (light + dark). */
  height: 100%;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--rw-canvas);
  color: var(--rw-ink);
}

.rw-page-scroll {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 24px 28px 36px;
}

.rw-page-scroll > * + * {
  margin-top: 16px;
}

.rw-card,
.mr-card {
  background: var(--rw-surface-card);
  border: 1px solid var(--rw-hairline);
  border-radius: 8px;
}

.rw-btn-secondary {
  min-height: 32px;
  padding: 0 12px;
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 8px;
  background: var(--rw-canvas);
  color: var(--rw-ink);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
}

.rw-btn-primary {
  min-height: 32px;
  padding: 0 13px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: var(--rw-primary);
  color: var(--rw-on-primary);
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 650;
  box-shadow: var(--rw-shadow-soft);
}

.rw-btn-primary:hover:not(:disabled) {
  background: var(--rw-primary-hover);
}

.rw-btn-primary:active:not(:disabled) {
  background: var(--rw-primary-active);
}

.rw-btn-secondary:hover:not(:disabled) {
  background: var(--rw-surface-strong);
}

.rw-btn-secondary:disabled,
.rw-btn-primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.spin {
  animation: spin 0.9s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.title-card {
  padding: 22px;
}

.title-main {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 18px;
}

.title-main h1 {
  margin: 0;
  font-size: 24px;
  line-height: 1.25;
  font-weight: 700;
  color: var(--rw-ink);
}

.title-main p {
  margin: 10px 0 0;
  max-width: 880px;
  color: var(--rw-body);
  font-size: 14px;
  line-height: 1.65;
}

.title-meta-grid,
.mr-meta-grid {
  margin-top: 18px;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.title-meta-grid div,
.mr-meta-grid div,
.mr-link {
  min-width: 0;
  border: 1px solid var(--rw-hairline);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--rw-canvas-soft);
}

.title-meta-grid span,
.mr-meta-grid span,
.info-list dt {
  display: block;
  color: var(--rw-muted);
  font-size: 12px;
  font-weight: 600;
}

.title-meta-grid strong,
.title-meta-grid a,
.mr-meta-grid strong {
  display: block;
  margin-top: 4px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--rw-ink);
  font-size: 13px;
  font-weight: 650;
}

.title-meta-grid a {
  font-family: var(--rw-mono);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.45fr) minmax(320px, 0.8fr);
  gap: 16px;
}

.info-card {
  padding: 18px;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.section-head h2 {
  margin: 0;
  font-size: 16px;
  line-height: 1.35;
  font-weight: 700;
  color: var(--rw-ink);
}

.fix-list {
  margin-top: 14px;
}

.fix-row {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  gap: 12px;
  padding: 14px 0;
  border-top: 1px solid var(--rw-hairline);
}

.fix-row:first-child {
  border-top: none;
  padding-top: 0;
}

.fix-index {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--rw-surface-strong);
  color: var(--rw-ink);
  font-size: 12px;
  font-weight: 700;
}

.fix-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}

.fix-head h3 {
  margin: 0;
  min-width: 0;
}

.fix-head .rw-pill {
  flex-shrink: 0;
}

.fix-body h3 {
  margin: 0;
  font-size: 14px;
  line-height: 1.4;
  font-weight: 700;
}

.fix-body p {
  margin: 7px 0 0;
  color: var(--rw-body);
  line-height: 1.6;
  font-size: 13px;
}

.fix-rationale {
  padding-left: 10px;
  border-left: 2px solid var(--rw-hairline-strong);
}

.fix-outcome-reason {
  margin-top: 7px;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--rw-canvas-soft);
  color: var(--rw-body) !important;
  font-size: 12.5px !important;
}

.fix-mr-link {
  margin-top: 8px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--rw-ink);
  font-size: 12px;
  font-weight: 650;
  text-decoration: underline;
  text-underline-offset: 3px;
}

.fix-tags {
  margin-top: 10px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.fix-tags code,
.branch-line code,
.changed-file-row code,
.mono {
  font-family: var(--rw-mono);
}

.fix-tags code {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  border-radius: 6px;
  padding: 3px 6px;
  background: var(--rw-surface-strong);
  color: var(--rw-body);
  font-size: 11px;
}

.info-list {
  margin: 14px 0 0;
}

.info-list div {
  padding: 12px 0;
  border-top: 1px solid var(--rw-hairline);
}

.info-list div:first-child {
  border-top: none;
  padding-top: 0;
}

.info-list dd {
  margin: 5px 0 0;
  color: var(--rw-ink);
  font-size: 13px;
  overflow-wrap: anywhere;
}

.error-text {
  color: var(--rw-danger) !important;
}

.mr-section {
  padding-top: 4px;
}

.mr-section > .section-head {
  margin-bottom: 12px;
}

.mr-card {
  padding: 18px;
}

.mr-card + .mr-card {
  margin-top: 12px;
}

.mr-card-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

.mr-title-wrap {
  min-width: 0;
}

.mr-title-wrap h3 {
  margin: 0;
  font-size: 15px;
  line-height: 1.35;
  color: var(--rw-ink);
  font-weight: 700;
}

.branch-line {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--rw-body);
  font-size: 12px;
}

.branch-line code {
  min-width: 0;
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mr-link {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--rw-ink);
  font-size: 13px;
  font-weight: 650;
  text-decoration: none;
}

.mr-link:hover {
  border-color: var(--rw-ink);
}

.changed-files {
  margin-top: 14px;
  border-top: 1px solid var(--rw-hairline);
  padding-top: 12px;
}

.changed-file-row {
  min-height: 32px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--rw-body);
  font-size: 12px;
}

.changed-file-row code {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--rw-ink);
}

.changed-file-row span {
  flex-shrink: 0;
  display: inline-flex;
  gap: 8px;
}

.changed-file-row b {
  color: #15803d;
  font-weight: 700;
}

.changed-file-row em {
  color: var(--rw-danger);
  font-style: normal;
  font-weight: 700;
}

.rw-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 24px;
  padding: 0 9px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 650;
  white-space: nowrap;
}

.rw-pill-success { background: rgba(22, 163, 74, 0.12); color: #15803d; }
.rw-pill-info { background: var(--rw-surface-strong); color: var(--rw-ink); }
.rw-pill-warning { background: rgba(171, 100, 0, 0.10); color: #ab6400; }
.rw-pill-danger { background: rgba(192, 56, 43, 0.10); color: #c0382b; }
.rw-pill-neutral { background: var(--rw-surface-strong); color: var(--rw-body); }

.loading-card {
  padding: 22px;
}

.empty-card {
  min-height: 220px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 32px;
  color: var(--rw-body);
  text-align: center;
}

.empty-card.compact {
  min-height: 150px;
}

.empty-card h2 {
  margin: 0;
  font-size: 17px;
  font-weight: 700;
  color: var(--rw-ink);
}

.empty-card p,
.muted-text {
  margin: 0;
  color: var(--rw-muted);
  font-size: 13px;
}

@media (max-width: 1100px) {
  .detail-grid,
  .title-meta-grid,
  .mr-meta-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 760px) {
  .rw-page-scroll {
    padding: 16px 16px 28px;
  }

  .title-main,
  .mr-card-head {
    flex-direction: column;
  }

  .title-main h1 {
    font-size: 20px;
  }

  .detail-grid,
  .title-meta-grid,
  .mr-meta-grid {
    grid-template-columns: 1fr;
  }
}
</style>
