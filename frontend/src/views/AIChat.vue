<script setup lang="ts">
import { computed, onMounted, onUnmounted, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute } from 'vue-router'
import { useChatComposerKeyboard } from '@/composables/useChatComposerKeyboard'
import { deviceLinkApi } from '@/api/deviceLink'
import { userApi, userToken } from '@/api/user'
import type { DeviceInfo } from '@/types'
import { renderMarkdown, processMermaidBlocks } from '@/utils/markdownRenderer'
import { formatAgentErrorMessage } from '@/utils/agentErrorMessage'
import { loadMermaid } from '@/utils/mermaidLoader'
import { jsPDF } from 'jspdf'
import html2canvas from 'html2canvas-pro'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Copy, FileDown } from 'lucide-vue-next'
import { useUserStore } from '@/stores/user'
import { useAppStore } from '@/stores/app'
import { useChatSessionStore } from '@/stores/chatSession'
import {
  useConversationRunsStore,
  THINKING_PLACEHOLDER,
  type ChatEntry,
  type ChatEntryImage,
  type OcrStatusInfo,
  type PendingPermission,
} from '@/stores/conversationRuns'
import type { PendingClarification } from '@/types/agentTrace'
import AgentTraceStream from '@/components/AgentTraceStream.vue'
import StreamingAnswer from '@/components/StreamingAnswer.vue'
import ClarificationCard from '@/components/ClarificationCard.vue'
import PackagePublicationCard from '@/components/PackagePublicationCard.vue'
import ShareConversationModal from '@/components/ShareConversationModal.vue'
import ProjectRepoSelect from '@/components/ProjectRepoSelect.vue'
import { projectRepoApi, type ProjectRepoOption } from '@/api'
import type { ChatImageAttachment } from '@/api/chat'
import { copyToClipboard, downloadFile } from '@/utils'
import {
  extractVisualAnalysis,
  parseAgentMessage,
  type AgentMessageView,
  type VisualAnalysis,
} from '@/utils/agentResultSummary'
import {
  proceedAfterLogWorkspaceReplacementCheck,
  requiresLogWorkspaceReplacementConfirmation,
  resolveLogWorkspaceReplacementAction,
  type LogWorkspaceReplacementDecision,
} from '@/utils/logWorkspaceReplacement'
import {
  configurationManagerRequiresProject,
  mergeUniquePackageFiles,
} from '@/utils/packageAttachments'

type AgentOption = {
  id: string
  nameKey: string
  descriptionKey?: string
  agentType: 'package-manager' | 'log-analysis' | 'project-expert'
}

const packageAgentOption: AgentOption = {
  id: 'package-manager',
  nameKey: 'aiChat.agents.packageManager',
  agentType: 'package-manager',
  descriptionKey: 'aiChat.agentDescriptions.packageManager'
}

const logAnalysisAgentOption: AgentOption = {
  id: 'log-analysis',
  nameKey: 'aiChat.agents.logAnalysis',
  agentType: 'log-analysis',
  descriptionKey: 'aiChat.agentDescriptions.logAnalysis'
}

const projectExpertAgentOption: AgentOption = {
  id: 'project-expert',
  nameKey: 'aiChat.agents.projectExpert',
  agentType: 'project-expert',
  descriptionKey: 'aiChat.agentDescriptions.projectExpert'
}

// Archive packages (decompressed server-side) plus plain-text logs (analyzed as-is).
const acceptedLogArchiveExtensions = ['.zip', '.tar', '.tgz', '.gz', '.tar.gz', '.tar.bz2', '.bz2', '.tar.xz', '.xz', '.7z', '.rar']
const acceptedLogTextExtensions = [
  '.log', '.txt', '.out', '.err', '.trace', '.json', '.xml', '.csv', '.tsv',
  '.md', '.markdown', '.ini', '.cfg', '.conf', '.yaml', '.yml', '.toml', '.properties'
]
const acceptedLogSpreadsheetExtensions = ['.xls', '.xlsx', '.xlsm']
const acceptedLogArchiveTypes = [
  ...acceptedLogArchiveExtensions, ...acceptedLogTextExtensions, ...acceptedLogSpreadsheetExtensions
].join(',')

function isArchiveLogFile(file: File): boolean {
  const name = file.name.toLowerCase()
  return acceptedLogArchiveExtensions.some((ext) => name.endsWith(ext))
}

// Image attachments (OCR): available for ANY agent, independent of log files.
// Keep these limits in lock-step with the backend OCR_* defaults.
const ACCEPTED_IMAGE_MIME_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif']
const MAX_IMAGE_COUNT = 6
const MAX_IMAGE_SIZE_MB = 5

type PendingImage = {
  id: string
  name: string
  mediaType: string
  /** data: URL from FileReader; used both as thumbnail src and as the sent payload. */
  dataUrl: string
}

const userStore = useUserStore()
const route = useRoute()
const appStore = useAppStore()
const sessionStore = useChatSessionStore()
const runsStore = useConversationRunsStore()
const { t } = useI18n()

const agentName = (option: AgentOption) => t(option.nameKey)

const generateUUID = (): string => {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    const v = c === 'x' ? r : (r & 0x3) | 0x8
    return v.toString(16)
  })
}

const inputMessage = ref('')
const chatContainerRef = ref<HTMLElement | null>(null)
const textareaRef = ref<HTMLTextAreaElement | null>(null)
const deviceMenuRef = ref<HTMLElement | null>(null)
const deviceMenuBtnRef = ref<HTMLElement | null>(null)
const topMoreMenuRef = ref<HTMLElement | null>(null)
const topMoreBtnRef = ref<HTMLElement | null>(null)
const logFileInputRef = ref<HTMLInputElement | null>(null)
const packageFileInputRef = ref<HTMLInputElement | null>(null)

const devices = ref<DeviceInfo[]>([])
const isLoadingDevices = ref(false)

const deviceMenuVisible = ref(false)
const deviceKeyword = ref('')

const targetDeviceId = ref<string | null>(null)
const targetDeviceName = ref<string | null>(null)
const targetAgent = ref<AgentOption | null>(null)
const selectedLogFiles = ref<File[]>([])
// Component inputs are intentionally independent from log attachments: they
// use a different multipart endpoint, skip log metadata checks, and route only
// to the Configuration Manager.
const selectedPackageFiles = ref<File[]>([])
const isLogFileDragOver = ref(false)
let logFileDragDepth = 0

// Pending image attachments (paste / pick / drop). Independent of log files and
// available under any agent. Sent as base64 with the turn; cleared after send.
const pendingImages = ref<PendingImage[]>([])
const imageFileInputRef = ref<HTMLInputElement | null>(null)

const showTopMoreMenu = ref(false)
const showShareModal = ref(false)
const showRenameModal = ref(false)
const renameModalTitle = ref('')
const renameModalInputRef = ref<HTMLInputElement | null>(null)
const renameModalInFlight = ref(false)

// Local session id used until the user sends the first message; once a run is
// created or a history session is selected, the sessionStore drives this.
const localSessionId = ref<string | null>(null)
const cancelInFlight = ref(false)
const loadedSessionId = ref<string | null>(null)

// All per-conversation state (messages, isSending, pendingPermissions,
// activeRunId, runAgentKind, subscription) now lives in the runs store keyed
// by session id. The template binds to ``currentConversation`` so the panel
// only ever reflects the selected session.
const effectiveSessionId = computed<string | null>(
  () => sessionStore.selectedSessionId || localSessionId.value,
)
const currentConversation = computed(() => {
  const id = effectiveSessionId.value
  return id ? runsStore.ensureState(id) : null
})
const chatHistory = computed<ChatEntry[]>(() => currentConversation.value?.messages || [])
const loadingMessages = computed(() => !!currentConversation.value?.loadingMessages)
const isSending = computed(() => !!currentConversation.value?.isSending)
const pendingPermissions = computed<PendingPermission[]>(
  () => currentConversation.value?.pendingPermissions || [],
)
const currentPermission = computed<PendingPermission | null>(() =>
  pendingPermissions.value.length > 0 ? pendingPermissions.value[0] : null,
)
const permissionDecisionInFlight = ref(false)
const pendingClarifications = computed<PendingClarification[]>(
  () => currentConversation.value?.pendingClarifications || [],
)
const currentClarification = computed<PendingClarification | null>(() =>
  pendingClarifications.value.length > 0 ? pendingClarifications.value[0] : null,
)
const publicationNeedsReview = computed(() => !!currentClarification.value?.publication_review?.warnings.length)
// The composer send button morphs into a stop button whenever the current
// session has a run in flight, regardless of agent kind.
const canCancelCurrentRun = computed(() => {
  const s = currentConversation.value
  return !!s && (s.isSending || s.runStatus === 'running')
})

// 关联项目（用于在日志分析 Agent 中显式指定项目身份；留空则回退到 metadata.json）
const projectRepoOptions = ref<ProjectRepoOption[]>([])
const projectRepoOptionsLoading = ref(false)
const projectRepoOptionsLoaded = ref(false)
const selectedProjectRepoId = ref<number | null>(null)

const ensureProjectRepoOptions = async () => {
  if (projectRepoOptionsLoading.value || projectRepoOptionsLoaded.value) return
  projectRepoOptionsLoading.value = true
  try {
    const response = await projectRepoApi.listEnabled()
    projectRepoOptions.value = Array.isArray(response?.data) ? response.data : []
    projectRepoOptionsLoaded.value = true
  } catch (err) {
    console.warn('Failed to load project list:', err)
    projectRepoOptions.value = []
  } finally {
    projectRepoOptionsLoading.value = false
  }
}

const isLoggedIn = computed(() => userStore.isAuthenticated)
const currentUserName = computed(() => userStore.profile?.display_name || userStore.profile?.username || t('aiChat.userFallback'))

const isWelcomeMode = computed(() => chatHistory.value.length === 0 && !loadingMessages.value)

const handleClickOutside = (event: MouseEvent) => {
  const target = event.target as Node

  if (showTopMoreMenu.value && topMoreMenuRef.value && topMoreBtnRef.value &&
      !topMoreMenuRef.value.contains(target) && !topMoreBtnRef.value.contains(target)) {
    showTopMoreMenu.value = false
  }

  if (deviceMenuVisible.value && deviceMenuRef.value && deviceMenuBtnRef.value &&
      !deviceMenuRef.value.contains(target) && !deviceMenuBtnRef.value.contains(target)) {
    deviceMenuVisible.value = false
  }
}

const handleKey = (e: KeyboardEvent) => {
  if (e.key === 'Escape') {
    showTopMoreMenu.value = false
    deviceMenuVisible.value = false
    showRenameModal.value = false
  }
}

onMounted(() => {
  document.addEventListener('click', handleClickOutside)
  document.addEventListener('keydown', handleKey)
  fetchDevices()
  const requestedSession = route.query.session_id
  if (typeof requestedSession === 'string' && isLoggedIn.value) {
    sessionStore.setSelected(requestedSession)
  }
  // Load the currently-selected session's messages on mount.
  if (sessionStore.selectedSessionId) {
    loadMessages(sessionStore.selectedSessionId)
  }
})

onUnmounted(() => {
  document.removeEventListener('click', handleClickOutside)
  document.removeEventListener('keydown', handleKey)
  const id = loadedSessionId.value || effectiveSessionId.value
  if (id) runsStore.abortSubscription(id)
})

const scrollToBottom = async () => {
  await nextTick()
  if (chatContainerRef.value) {
    chatContainerRef.value.scrollTop = chatContainerRef.value.scrollHeight
  }
}

watch(chatHistory, () => { scrollToBottom() }, { deep: true })

watch(() => sessionStore.selectSessionToken, () => {
  const id = sessionStore.selectedSessionId
  if (id) loadMessages(id)
})

watch(() => sessionStore.newChatToken, () => {
  resetPanel()
})

watch(isLoggedIn, (loggedIn) => {
  if (!loggedIn) {
    runsStore.reset()
    localSessionId.value = null
  }
})

watch(selectedProjectRepoId, () => {
  saveAgentSelectionToState()
})

const fetchDevices = async () => {
  isLoadingDevices.value = true
  try {
    const res = await deviceLinkApi.listDevices()
    devices.value = res.devices || []
  } catch (error) {
    console.error('Failed to load devices', error)
  } finally {
    isLoadingDevices.value = false
  }
}

const agentKindToOption: Record<string, AgentOption> = {
  log_analysis: logAnalysisAgentOption,
  package_search: packageAgentOption,
  project_expert: projectExpertAgentOption,
}

const saveAgentSelectionToState = (sessionId?: string | null) => {
  const sid = sessionId ?? effectiveSessionId.value
  if (!sid) return
  const state = runsStore.ensureState(sid)
  const kind = targetAgent.value?.agentType === 'log-analysis' ? 'log_analysis'
    : targetAgent.value?.agentType === 'project-expert' ? 'project_expert'
    : targetAgent.value?.agentType === 'package-manager' ? 'package_search'
    : null
  state.lastAgentKind = kind as import('@/stores/conversationRuns').AgentKind | null
  state.lastProjectRepoId = selectedProjectRepoId.value
}

const restoreAgentSelectionFromState = (state: ReturnType<typeof runsStore.ensureState>) => {
  const agentKind = state.runAgentKind || state.lastAgentKind
  if (agentKind && agentKindToOption[agentKind]) {
    targetAgent.value = agentKindToOption[agentKind]
    if (agentKind === 'log_analysis' || agentKind === 'project_expert' || agentKind === 'package_search') {
      ensureProjectRepoOptions()
    }
  } else {
    targetAgent.value = null
  }
  selectedProjectRepoId.value = state.lastProjectRepoId ?? null
}

const loadMessages = async (id: string) => {
  if (loadedSessionId.value && loadedSessionId.value !== id) {
    saveAgentSelectionToState(loadedSessionId.value)
    runsStore.abortSubscription(loadedSessionId.value)
  }
  loadedSessionId.value = id
  localSessionId.value = id
  try {
    await runsStore.loadSession(id, {
      authToken: (userStore.token as unknown as string) || null,
      isLoggedIn: isLoggedIn.value,
      force: true,
    })
    // A slow history request from the previous selection must not change the
    // current conversation's Agent/project after the user has navigated away.
    if (loadedSessionId.value !== id) return
    const state = runsStore.ensureState(id)
    restoreAgentSelectionFromState(state)
  } catch (error) {
    console.error('Failed to load session messages', error)
    appStore.showNotification({ title: t('aiChat.notifications.loadMessagesFailed'), type: 'error' })
  }
}

const resetPanel = () => {
  saveAgentSelectionToState()
  const id = loadedSessionId.value || effectiveSessionId.value
  if (id) runsStore.abortSubscription(id)
  loadedSessionId.value = null
  localSessionId.value = null
  inputMessage.value = ''
  targetAgent.value = null
  targetDeviceId.value = null
  targetDeviceName.value = null
  selectedProjectRepoId.value = null
  selectedLogFiles.value = []
  selectedPackageFiles.value = []
  deviceMenuVisible.value = false
  nextTick(() => textareaRef.value?.focus())
}


const deleteCurrentSession = async () => {
  showTopMoreMenu.value = false
  const id = sessionStore.selectedSessionId
  if (!id) return
  const confirmed = window.confirm(t('aiChat.confirm.deleteSession'))
  if (!confirmed) return
  try {
    await sessionStore.removeSession(id)
    runsStore.clearSession(id)
    appStore.showNotification({ title: t('aiChat.notifications.sessionDeleted'), type: 'success' })
  } catch (error) {
    console.error('Failed to delete session', error)
    appStore.showNotification({ title: t('aiChat.notifications.deleteFailed'), type: 'error' })
  }
}

const currentSessionPinned = computed(() => {
  const id = sessionStore.selectedSessionId
  if (!id) return false
  return Boolean(sessionStore.sessions.find((s) => s.id === id)?.is_pinned)
})

const toggleCurrentPin = async () => {
  showTopMoreMenu.value = false
  const id = sessionStore.selectedSessionId
  if (!id) {
    appStore.showNotification({ title: t('aiChat.notifications.saveBeforePin'), type: 'warning' })
    return
  }
  const wasPinned = currentSessionPinned.value
  try {
    const ok = await sessionStore.togglePin(id)
    if (ok) {
      appStore.showNotification({
        title: wasPinned ? t('aiChat.notifications.unpinned') : t('aiChat.notifications.pinned'),
        type: 'success',
      })
    }
  } catch (error) {
    console.error('Failed to update pin state', error)
    appStore.showNotification({ title: t('aiChat.notifications.operationFailed'), type: 'error' })
  }
}

const openRenameModal = () => {
  showTopMoreMenu.value = false
  const id = sessionStore.selectedSessionId
  if (!id) {
    appStore.showNotification({ title: t('aiChat.notifications.saveBeforeRename'), type: 'warning' })
    return
  }
  renameModalTitle.value = sessionStore.currentTitle || ''
  showRenameModal.value = true
  nextTick(() => {
    renameModalInputRef.value?.select()
  })
}

const commitRenameFromModal = async () => {
  const id = sessionStore.selectedSessionId
  if (!id || renameModalInFlight.value) return
  const title = renameModalTitle.value.trim()
  if (!title) {
    appStore.showNotification({ title: t('aiChat.notifications.nameRequired'), type: 'warning' })
    return
  }
  renameModalInFlight.value = true
  try {
    const ok = await sessionStore.renameSession(id, title)
    if (ok) {
      showRenameModal.value = false
      appStore.showNotification({ title: t('aiChat.notifications.renamed'), type: 'success' })
    }
  } catch {
    appStore.showNotification({ title: t('aiChat.notifications.renameFailed'), type: 'error' })
  } finally {
    renameModalInFlight.value = false
  }
}

const padDatePart = (value: number) => String(value).padStart(2, '0')

const formatExportDateTime = (date: Date) => {
  const year = date.getFullYear()
  const month = padDatePart(date.getMonth() + 1)
  const day = padDatePart(date.getDate())
  const hour = padDatePart(date.getHours())
  const minute = padDatePart(date.getMinutes())
  const second = padDatePart(date.getSeconds())
  return `${year}-${month}-${day} ${hour}:${minute}:${second}`
}

const formatExportFileStamp = (date: Date) =>
  formatExportDateTime(date).replace(/[-:]/g, '').replace(' ', '-')

const escapeHtml = (value: string): string =>
  value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')

const sanitizeMarkdownFilename = (name: string) => {
  const cleaned = name
    .replace(/[\\/:*?"<>|]+/g, '-')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 80)
  return cleaned || t('aiChat.export.defaultFilename')
}

const messageSpeakerName = (role: ChatEntry['role']) => {
  if (role === 'user') return currentUserName.value
  if (role === 'ai') return 'RAVENAI'
  return t('aiChat.systemRole')
}

const buildConversationMarkdown = (exportedAt: Date) => {
  const title = currentChatTitle.value || t('aiChat.export.defaultTitle')
  const lines: string[] = [
    `# ${title}`,
    '',
    `- ${t('aiChat.export.exportedAt')}: ${formatExportDateTime(exportedAt)}`,
    `- ${t('aiChat.export.sessionId')}: ${effectiveSessionId.value || t('aiChat.export.localSession')}`,
    `- ${t('aiChat.export.messageCount')}: ${chatHistory.value.length}`,
    '',
    '---',
  ]

  chatHistory.value.forEach((message, index) => {
    const visibleContent =
      message.role === 'ai'
        ? getAgentMessageView(message.content).displayMarkdown
        : message.content
    const content = (visibleContent || '').trim() || t('aiChat.export.emptyMessage')
    lines.push(
      '',
      `## ${index + 1}. ${messageSpeakerName(message.role)}`,
      '',
      content,
    )
  })

  return `${lines.join('\n').trimEnd()}\n`
}

const exportCurrentConversationMarkdown = () => {
  showTopMoreMenu.value = false
  if (!chatHistory.value.length) {
    appStore.showNotification({ title: t('aiChat.notifications.noExportMessages'), type: 'warning' })
    return
  }

  const exportedAt = new Date()
  const markdown = buildConversationMarkdown(exportedAt)
  const filename = `${sanitizeMarkdownFilename(currentChatTitle.value || t('aiChat.export.defaultFilename'))}-${formatExportFileStamp(exportedAt)}.md`
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  downloadFile(blob, filename)
  appStore.showNotification({ title: t('aiChat.notifications.markdownExported'), type: 'success' })
}

const copyAiMessageMarkdown = async (message: ChatEntry) => {
  const content = getAgentMessageView(message.content).displayMarkdown.trim()
  if (!content || content === THINKING_PLACEHOLDER) return
  const ok = await copyToClipboard(content)
  if (ok) {
    ElMessage.success(t('aiChat.export.markdownCopied'))
  } else {
    ElMessage.error(t('aiChat.export.markdownCopyFailed'))
  }
}

// 回复 PDF 容器样式。所有规则都用 `.ai-pdf-root` 作为前缀作用域，
// 因此即便以全局 <style> 注入也不会污染当前页面（光栅化离屏容器使用）。
const AI_MESSAGE_PDF_STYLES = `
.ai-pdf-root, .ai-pdf-root * { box-sizing: border-box; }
.ai-pdf-root {
  width: auto;
  max-width: none;
  margin: 0;
  padding: 0;
  background: #fff;
  color: #171717;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  font-size: 13px;
  line-height: 1.62;
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
.ai-pdf-title {
  margin: 0 0 6px;
  font-size: 20px;
  font-weight: 650;
  line-height: 1.25;
  color: #111827;
}
.ai-pdf-meta {
  margin: 0 0 18px;
  padding-bottom: 12px;
  border-bottom: 1px solid #e5e7eb;
  color: #8a8f98;
  font-size: 11.5px;
}
.ai-pdf-content h1,
.ai-pdf-content h2,
.ai-pdf-content h3,
.ai-pdf-content h4 {
  margin: 18px 0 8px;
  color: #111827;
  font-weight: 650;
  line-height: 1.35;
  page-break-after: avoid;
}
.ai-pdf-content h1 { font-size: 18px; }
.ai-pdf-content h2 { font-size: 16px; }
.ai-pdf-content h3 { font-size: 14.5px; }
.ai-pdf-content h4 { font-size: 13.5px; }
.ai-pdf-content p { margin: 0 0 10px; }
.ai-pdf-content ul,
.ai-pdf-content ol { margin: 8px 0 10px; padding-left: 1.6em; }
.ai-pdf-content ul { list-style-type: disc; }
.ai-pdf-content ol { list-style-type: decimal; }
.ai-pdf-content ul ul { list-style-type: circle; }
.ai-pdf-content ul ul ul { list-style-type: square; }
.ai-pdf-content li { margin: 4px 0; }
.ai-pdf-content a { color: #0d74ce; text-decoration: none; }
.ai-pdf-content blockquote {
  margin: 10px 0;
  padding: 8px 12px;
  border-left: 3px solid #d1d5db;
  background: #f9fafb;
  color: #4b5563;
}
.ai-pdf-content code {
  font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  font-size: 12px;
  background: #f3f4f6;
  color: #111827;
  border-radius: 4px;
  padding: 1px 5px;
}
.ai-pdf-content pre {
  margin: 10px 0;
  padding: 12px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  border-radius: 8px;
  background: #111827;
  color: #f9fafb;
  page-break-inside: avoid;
}
.ai-pdf-content pre code {
  padding: 0;
  background: transparent;
  color: inherit;
  font-size: 11.5px;
}
.ai-pdf-content table {
  width: 100%;
  border-collapse: collapse;
  margin: 10px 0;
  page-break-inside: avoid;
}
.ai-pdf-content th,
.ai-pdf-content td {
  border: 1px solid #e5e7eb;
  padding: 5px 8px;
  text-align: left;
  vertical-align: top;
}
.ai-pdf-content th { background: #f9fafb; font-weight: 650; }
.ai-pdf-content img,
.ai-pdf-content svg { max-width: 100%; height: auto; }
.ai-pdf-content .mermaid-container {
  margin: 12px 0;
  padding: 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
  page-break-inside: avoid;
}
.ai-pdf-content .mermaid-loading,
.ai-pdf-content .mermaid-copy-btn,
.ai-pdf-content .table-copy-btn { display: none; }
`

const buildAiMessagePdfFilename = (exportedAt: Date) => {
  const title = currentChatTitle.value || t('aiChat.export.defaultFilename')
  return `${sanitizeMarkdownFilename(title)}-${formatExportFileStamp(exportedAt)}.pdf`
}

// A4 纸张与内容区尺寸（mm），以及离屏渲染宽度（px）。
const AI_PDF_PAGE = {
  marginMm: 10,
  widthMm: 210,
  heightMm: 297,
  get contentWidthMm() { return this.widthMm - this.marginMm * 2 },
  get contentHeightMm() { return this.heightMm - this.marginMm * 2 },
  renderWidthPx: 760,
  scale: 2,
}

// 导出本次回复为 PDF：使用内置渲染（html2canvas-pro 光栅化 + jsPDF 直接生成），
// 与日志详情的「导出报告（PDF）」一致，不再依赖浏览器打印对话框。
const exportAiMessagePdf = async (message: ChatEntry) => {
  const content = getAgentMessageView(message.content).displayMarkdown.trim()
  if (!content || content === THINKING_PLACEHOLDER) return

  // 离屏容器：渲染回复 DOM，供 html2canvas 光栅化后切片成 PDF。
  let container: HTMLDivElement | null = null
  const cleanupContainer = () => {
    if (container && container.parentNode) {
      container.parentNode.removeChild(container)
    }
    container = null
  }

  try {
    const exportedAt = new Date()
    const title = t('aiChat.export.singlePdfTitle', { title: currentChatTitle.value || t('aiChat.export.defaultTitle') })
    const contentHtml = renderMarkdown(content, { wrapperClass: 'ai-pdf-content', cleanXml: true })

    const innerHtml = `
<h1 class="ai-pdf-title">${escapeHtml(title)}</h1>
<div class="ai-pdf-meta">${escapeHtml(t('aiChat.export.singlePdfMeta', { time: formatExportDateTime(exportedAt) }))}</div>
${contentHtml}
`

    // 离屏渲染容器：固定到视口外，宽度固定以保证排版/换行稳定。
    container = document.createElement('div')
    container.setAttribute('aria-hidden', 'true')
    container.style.cssText = [
      'position: fixed',
      'left: -10000px',
      'top: 0',
      `width: ${AI_PDF_PAGE.renderWidthPx}px`,
      'background: #ffffff',
      'pointer-events: none',
      'z-index: -1',
    ].join('; ')
    // 样式全部以 `.ai-pdf-root` 作用域，随容器一起注入即可，不污染主页面。
    container.innerHTML = `<style>${AI_MESSAGE_PDF_STYLES}</style>` +
      `<div class="ai-pdf-root" id="ai-pdf-root">${innerHtml}</div>`
    document.body.appendChild(container)

    const root = container.querySelector('#ai-pdf-root') as HTMLElement | null
    if (!root) throw new Error(t('aiChat.export.pdfContainerFail'))

    // 先渲染 mermaid 图（异步插入 SVG），再等 web 字体就绪与一次回流，
    // 避免中文回退字体或未完成布局导致的度量偏差。
    await processMermaidBlocks(root)
    if ((document as any).fonts?.ready) {
      try { await (document as any).fonts.ready } catch { /* noop */ }
    }
    await new Promise((r) => setTimeout(r, 50))

    const scale = AI_PDF_PAGE.scale
    const canvas = await html2canvas(root, {
      scale,
      backgroundColor: '#ffffff',
      useCORS: true,
      logging: false,
      windowWidth: AI_PDF_PAGE.renderWidthPx,
    })
    if (!canvas.width || !canvas.height) {
      throw new Error(t('aiChat.export.pdfExportFailed'))
    }

    // 采集"安全分页点"：块级元素底部的 Y 坐标（画布像素），
    // 分页时把页底对齐到这些边界，避免把一行文字从中间切断。
    const rootTop = root.getBoundingClientRect().top
    const breakYs: number[] = []
    root
      .querySelectorAll('h1, h2, h3, h4, p, li, tr, pre, blockquote, table, ul, ol, .mermaid-container')
      .forEach((el) => {
        const bottom = (el.getBoundingClientRect().bottom - rootTop) * scale
        if (bottom > 0 && bottom <= canvas.height) breakYs.push(bottom)
      })
    breakYs.sort((a, b) => a - b)

    const pxPerMm = canvas.width / AI_PDF_PAGE.contentWidthMm
    const pageHeightPx = AI_PDF_PAGE.contentHeightMm * pxPerMm

    const pdf = new jsPDF({ unit: 'mm', format: 'a4', compress: true })

    let startY = 0
    let firstPage = true
    while (startY < canvas.height - 1) {
      let endY = Math.min(startY + pageHeightPx, canvas.height)
      // 若不是最后一页，尽量把页底对齐到一个安全分页点（至少填满半页，
      // 否则遇到超高元素会退化为硬切，避免出现大量空白页）。
      if (endY < canvas.height) {
        let snapped = -1
        for (const y of breakYs) {
          if (y > endY) break
          if (y > startY + pageHeightPx * 0.5) snapped = y
        }
        if (snapped > 0) endY = snapped
      }

      const sliceHeightPx = Math.max(1, Math.round(endY - startY))
      const pageCanvas = document.createElement('canvas')
      pageCanvas.width = canvas.width
      pageCanvas.height = sliceHeightPx
      const ctx = pageCanvas.getContext('2d')
      if (!ctx) throw new Error(t('aiChat.export.canvasCtxFail'))
      ctx.fillStyle = '#ffffff'
      ctx.fillRect(0, 0, pageCanvas.width, pageCanvas.height)
      ctx.drawImage(
        canvas,
        0, startY, canvas.width, sliceHeightPx,
        0, 0, canvas.width, sliceHeightPx,
      )

      const imgData = pageCanvas.toDataURL('image/png')
      const sliceHeightMm = sliceHeightPx / pxPerMm
      if (!firstPage) pdf.addPage()
      pdf.addImage(
        imgData, 'PNG',
        AI_PDF_PAGE.marginMm, AI_PDF_PAGE.marginMm,
        AI_PDF_PAGE.contentWidthMm, sliceHeightMm,
        undefined, 'FAST',
      )

      firstPage = false
      startY = endY
    }

    pdf.save(buildAiMessagePdfFilename(exportedAt))
    ElMessage.success(t('aiChat.export.pdfDownloadStarted'))
  } catch (error: any) {
    console.error('Failed to export AI message PDF:', error)
    ElMessage.error(error?.message || t('aiChat.export.pdfExportFailed'))
  } finally {
    cleanupContainer()
  }
}

const sortedDevices = computed<DeviceInfo[]>(() =>
  devices.value
    .slice()
    .sort((a, b) => (a.status === b.status ? 0 : a.status === 'online' ? -1 : 1)),
)

const filteredDeviceOptions = computed<DeviceInfo[]>(() => {
  const keyword = deviceKeyword.value.trim().toLowerCase()
  const list = sortedDevices.value
  if (!keyword) return list
  return list.filter((device) =>
    `${device.name || ''} ${device.id}`.toLowerCase().includes(keyword),
  )
})

const targetAgentName = computed(() => targetAgent.value ? agentName(targetAgent.value) : null)
const isPackageAgentSelected = computed(() =>
  targetAgent.value?.agentType === 'package-manager' || selectedPackageFiles.value.length > 0
)
const isProjectExpertAgentSelected = computed(() => targetAgent.value?.agentType === 'project-expert')
const isLogAnalysisAgentSelected = computed(() =>
  targetAgent.value?.agentType === 'log-analysis' || selectedLogFiles.value.length > 0
)
const isProjectRepoSelectVisible = computed(() =>
  isLogAnalysisAgentSelected.value || isProjectExpertAgentSelected.value || isPackageAgentSelected.value
)
const isLogFileUploadDisabled = computed(() =>
  isPackageAgentSelected.value || isProjectExpertAgentSelected.value
)
// A pure Configuration Manager search remains project-bound. Packaging turns
// with component files may start unbound so the Skill can infer candidates and
// ask the human to confirm the authoritative project.
const isProjectRepoRequired = computed(() =>
  isProjectExpertAgentSelected.value ||
  (
    isPackageAgentSelected.value &&
    configurationManagerRequiresProject(selectedPackageFiles.value.length)
  )
)
// Filter by each project's enabled Agent keys. Repository presence is not a
// Configuration Manager requirement; its package catalog and repository
// operations do not clone source code.
const selectedProjectAgentKey = computed(() => {
  if (isProjectExpertAgentSelected.value) return 'project_expert'
  if (isPackageAgentSelected.value) return 'package_search'
  if (isLogAnalysisAgentSelected.value) return 'log_analysis'
  return null
})

const repoSupportsAgent = (repo: ProjectRepoOption, agentKey: string): boolean => {
  if (Array.isArray(repo.enabled_agent_keys) && repo.enabled_agent_keys.length) {
    return repo.enabled_agent_keys.includes(agentKey)
  }
  if (agentKey === 'project_expert' || agentKey === 'package_search') return true
  return repo.has_repo !== false
}

const visibleProjectRepoOptions = computed(() => {
  const agentKey = selectedProjectAgentKey.value
  if (!agentKey) return []
  return projectRepoOptions.value.filter((repo) => repoSupportsAgent(repo, agentKey))
})
const isProjectRepoRequiredMissing = computed(() =>
  isProjectRepoRequired.value && selectedProjectRepoId.value === null
)

watch(visibleProjectRepoOptions, (options) => {
  // 历史/Fork 恢复时，项目列表仍在请求中；空列表不能清除已保存的项目。
  if (!projectRepoOptionsLoaded.value || projectRepoOptionsLoading.value) return
  if (selectedProjectRepoId.value === null) return
  if (!options.some((repo) => repo.id === selectedProjectRepoId.value)) {
    selectedProjectRepoId.value = null
  }
})

// ZIP inspection: read central directory to check if metadata.json is present
const zipMetadataCheckResults = ref<(boolean | null)[]>([])

async function checkZipForMetadata(file: File): Promise<boolean | null> {
  if (!file.name.toLowerCase().endsWith('.zip')) return null
  try {
    const maxTail = Math.min(file.size, 22 + 65535)
    const tailBuf = await file.slice(file.size - maxTail).arrayBuffer()
    const tail = new DataView(tailBuf)
    let eocdPos = -1
    for (let i = tailBuf.byteLength - 22; i >= 0; i--) {
      if (tail.getUint32(i, true) === 0x06054b50) { eocdPos = i; break }
    }
    if (eocdPos < 0) return null
    const cdSize = tail.getUint32(eocdPos + 12, true)
    const cdOff  = tail.getUint32(eocdPos + 16, true)
    if (cdOff + cdSize > file.size) return null
    const cdBuf = await file.slice(cdOff, cdOff + cdSize).arrayBuffer()
    const cd = new DataView(cdBuf)
    const dec = new TextDecoder('utf-8', { fatal: false })
    let p = 0
    while (p + 46 <= cdBuf.byteLength) {
      if (cd.getUint32(p, true) !== 0x02014b50) break
      const fnLen  = cd.getUint16(p + 28, true)
      const extLen = cd.getUint16(p + 30, true)
      const cmtLen = cd.getUint16(p + 32, true)
      if (p + 46 + fnLen > cdBuf.byteLength) break
      const name = dec.decode(new Uint8Array(cdBuf, p + 46, fnLen))
      if (name === 'metadata.json' || name.endsWith('/metadata.json')) return true
      p += 46 + fnLen + extLen + cmtLen
    }
    return false
  } catch { return null }
}

watch(selectedLogFiles, async (files) => {
  zipMetadataCheckResults.value = []
  const results = await Promise.all(files.map((file) => checkZipForMetadata(file)))
  if (selectedLogFiles.value === files) zipMetadataCheckResults.value = results
})

// Warning: log-analysis agent active but no file attached in the composer.
// Only relevant before the first submission — once the conversation already
// has messages, a log-analysis request has been sent and follow-up questions
// don't need a new attachment. This also prevents the reminder from
// re-appearing after send, when the composer's file selection is cleared.
const logAnalysisNoAttachmentWarning = computed(() =>
  targetAgent.value?.agentType === 'log-analysis' &&
  selectedLogFiles.value.length === 0 &&
  chatHistory.value.length === 0
)

// Error: attachment cannot be tied to a project and none was manually selected.
// Two cases force this: a ZIP we inspected has no metadata.json, or a plain-text
// log (which can never carry metadata.json) was attached.
const logAnalysisMetadataError = computed(() => {
  if (!isLogAnalysisAgentSelected.value) return false
  const files = selectedLogFiles.value
  if (!files.length) return false
  if (selectedProjectRepoId.value !== null) return false
  return files.every((file, index) => {
    if (!isArchiveLogFile(file)) return true
    if (!file.name.toLowerCase().endsWith('.zip')) return false
    return zipMetadataCheckResults.value[index] === false
  })
})

const sendDisabled = computed(() =>
  !isLoggedIn.value ||
  isSending.value ||
  (
    !inputMessage.value.trim() &&
    !selectedLogFiles.value.length &&
    !selectedPackageFiles.value.length &&
    !pendingImages.value.length
  ) ||
  isProjectRepoRequiredMissing.value ||
  logAnalysisMetadataError.value
)

const setTargetAgent = (option: AgentOption) => {
  targetAgent.value = option
  targetDeviceId.value = null
  targetDeviceName.value = null
  if (
    option.agentType === 'log-analysis' ||
    option.agentType === 'project-expert' ||
    option.agentType === 'package-manager'
  ) {
    ensureProjectRepoOptions()
  }
  saveAgentSelectionToState()
}

// ── GeneralAgent 路由建议（suggested_agent_type）的展示与一键切换 ──
// 当用户未选专门 Agent、通用 Agent 判定请求更适合某个专门 Agent 时，后端会回带
// suggested_agent_type；这里给出醒目提示并支持一键切换（设备操作走独立入口，
// 仅文字引导）。
const SUGGESTED_AGENT_LABELS: Record<string, string> = {
  device: 'aiChat.agents.device',
  log_analysis: 'aiChat.agents.logAnalysis',
  package_search: 'aiChat.agents.packageManager',
  project_expert: 'aiChat.agents.projectExpert',
}

const suggestedAgentType = computed(() => currentConversation.value?.suggestedAgentType || null)

// 用户已经切到对应 Agent 后无需再提示。
const suggestedAgentAlreadySelected = computed(() => {
  const key = suggestedAgentType.value
  const at = targetAgent.value?.agentType
  return (
    (key === 'log_analysis' && at === 'log-analysis') ||
    (key === 'package_search' && at === 'package-manager') ||
    (key === 'project_expert' && at === 'project-expert')
  )
})

const suggestedAgentLabel = computed(() => {
  const key = suggestedAgentType.value
  if (!key || suggestedAgentAlreadySelected.value) return null
  return SUGGESTED_AGENT_LABELS[key] ? t(SUGGESTED_AGENT_LABELS[key]) : null
})

// 设备操作通过独立下拉选择，不在 AgentOption 体系内，故不提供一键切换。
const suggestedAgentSwitchable = computed(() => {
  const key = suggestedAgentType.value
  return key === 'log_analysis' || key === 'package_search' || key === 'project_expert'
})

const chooseSuggestedAgent = () => {
  switch (suggestedAgentType.value) {
    case 'log_analysis':
      setTargetAgent(logAnalysisAgentOption)
      break
    case 'package_search':
      setTargetAgent(packageAgentOption)
      break
    case 'project_expert':
      setTargetAgent(projectExpertAgentOption)
      break
    default:
      break
  }
}

// Keep result metadata stable; StreamingAnswer owns the paced Markdown view.
const agentMessageViewCache = new Map<string, AgentMessageView>()
const AI_MESSAGE_CACHE_LIMIT = 200
const answerDisplayBusy = ref<Record<string, boolean>>({})

const getAgentMessageView = (content: string): AgentMessageView => {
  const key = formatAgentErrorMessage(content || '', t)
  const cached = agentMessageViewCache.get(key)
  if (cached !== undefined) return cached
  const view = parseAgentMessage(key)
  if (agentMessageViewCache.size >= AI_MESSAGE_CACHE_LIMIT) {
    const oldest = agentMessageViewCache.keys().next().value
    if (oldest !== undefined) agentMessageViewCache.delete(oldest)
  }
  agentMessageViewCache.set(key, view)
  return view
}

const visualAnalysisForMessage = (message: ChatEntry, messageIndex: number): VisualAnalysis | null => {
  if (message.role !== 'ai' || (message.kind && message.kind !== 'answer')) return null
  if (message.visualAnalysis?.text) return message.visualAnalysis
  for (let index = messageIndex - 1; index >= 0; index -= 1) {
    const candidate = chatHistory.value[index]
    if (candidate.role === 'user') return extractVisualAnalysis(candidate.content)
  }
  return null
}

// The persisted user message carries the merged <user_image_ocr> block so
// follow-up turns keep the context, but it is machine-facing material — the
// bubble shows the user's own words, with the images themselves rendered as
// thumbnails above.
const OCR_BLOCK_RE = /\n*<user_image_ocr\b[^>]*>[\s\S]*?<\/user_image_ocr>\s*/g
const userBubbleText = (content: string) => (content || '').replace(OCR_BLOCK_RE, '').trim()

// Full-size preview overlay for an attached image.
const viewerImage = ref<ChatEntryImage | null>(null)
const openImageViewer = (img: ChatEntryImage) => {
  viewerImage.value = img
}
const closeImageViewer = () => {
  viewerImage.value = null
}

// Human-readable "images not recognized" hint for a degraded OCR turn.
const ocrDegradedText = (info: OcrStatusInfo) => {
  const count = info.imageCount || 0
  if (info.status === 'unconfigured') {
    return t('aiChat.image.degradedUnconfigured', { count })
  }
  return t('aiChat.image.degradedFailed', { count })
}

// ─── Mermaid 图表渲染 ───────────────────────────────────────────────
// 消息内容通过 v-html 插入后，占位的 .mermaid-container 需异步渲染为 SVG。
// 仅在流式输出结束（!isSending）后处理，避免未闭合的代码块反复渲染与闪烁。
const mermaidDialogVisible = ref(false)
const mermaidDialogContainerRef = ref<HTMLElement | null>(null)
let mermaidZoomSource = ''
let mermaidRenderScheduled = false

const scheduleMermaidRender = () => {
  if (
    isSending.value || loadingMessages.value || mermaidRenderScheduled ||
    Object.values(answerDisplayBusy.value).some(Boolean)
  ) return
  mermaidRenderScheduled = true
  nextTick(() => {
    mermaidRenderScheduled = false
    if (isSending.value || loadingMessages.value) return
    void processMermaidBlocks(chatContainerRef.value)
  })
}

watch(
  [chatHistory, isSending, loadingMessages, effectiveSessionId],
  scheduleMermaidRender,
  // immediate: 会话在挂载时可能已加载（store 缓存/恢复），此时 chatHistory
  // 不会再触发变更事件，需主动渲染一次已存在的 Mermaid 占位块。历史加载时
  // 消息会先写入 store，再从 loading 占位切换到 thread DOM，因此也必须监听
  // loadingMessages，确保真实消息 DOM 出现后再扫 pending Mermaid 容器。
  { deep: true, immediate: true, flush: 'post' }
)

// 事件委托：处理 Mermaid 图表的「复制源码」「点击放大」与表格的「复制 Markdown」交互。
const onThreadClick = (event: MouseEvent) => {
  const target = event.target as HTMLElement | null
  if (!target) return

  const tableCopyBtn = target.closest('.table-copy-btn')
  if (tableCopyBtn) {
    event.stopPropagation()
    const block = tableCopyBtn.closest('.table-block') as HTMLElement | null
    const source = block?.dataset.tableMd || ''
    if (!source) return
    navigator.clipboard.writeText(source).then(
      () => ElMessage.success(t('aiChat.table.markdownCopied')),
      () => ElMessage.error(t('aiChat.table.copyFailed'))
    )
    return
  }

  const copyBtn = target.closest('.mermaid-copy-btn')
  if (copyBtn) {
    event.stopPropagation()
    const container = copyBtn.closest('.mermaid-container') as HTMLElement | null
    const source = container?.dataset.mermaidSource || ''
    if (!source) return
    navigator.clipboard.writeText(source).then(
      () => ElMessage.success(t('aiChat.mermaid.sourceCopied')),
      () => ElMessage.error(t('aiChat.mermaid.copySourceFailed'))
    )
    return
  }

  const rendered = target.closest('.mermaid-container.is-rendered') as HTMLElement | null
  if (rendered) {
    openMermaidZoom(rendered)
  }
}

const openMermaidZoom = (container: HTMLElement) => {
  mermaidZoomSource = container.dataset.mermaidSource || ''
  if (!mermaidZoomSource) return
  mermaidZoomScale.value = 1
  mermaidDialogVisible.value = true
  nextTick(async () => {
    const host = mermaidDialogContainerRef.value
    if (!host) return
    host.innerHTML = `<div class="mermaid-loading">${t('aiChat.mermaid.rendering')}</div>`
    try {
      const mermaid = await loadMermaid()
      const { svg } = await mermaid.render(`mermaid-zoom-${Date.now()}`, mermaidZoomSource)
      host.innerHTML = svg
      applyMermaidZoom()
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err)
      host.innerHTML = `<div class="mermaid-error">${t('aiChat.mermaid.renderFailed', { message })}</div>`
    }
  })
}

// ─── 放大弹窗：缩放控制 ───────────────────────────────────────────────
const mermaidZoomScale = ref(1)
const MERMAID_ZOOM_MIN = 0.25
const MERMAID_ZOOM_MAX = 5
const MERMAID_ZOOM_STEP = 0.2

const getZoomSvgElement = (): SVGSVGElement | null =>
  mermaidDialogContainerRef.value?.querySelector('svg') ?? null

const applyMermaidZoom = () => {
  const svg = getZoomSvgElement()
  if (!svg) return
  svg.style.transformOrigin = 'top center'
  svg.style.transform = `scale(${mermaidZoomScale.value})`
}

const setMermaidZoom = (value: number) => {
  mermaidZoomScale.value = Math.min(
    MERMAID_ZOOM_MAX,
    Math.max(MERMAID_ZOOM_MIN, Math.round(value * 100) / 100)
  )
  applyMermaidZoom()
}

const zoomInMermaid = () => setMermaidZoom(mermaidZoomScale.value + MERMAID_ZOOM_STEP)
const zoomOutMermaid = () => setMermaidZoom(mermaidZoomScale.value - MERMAID_ZOOM_STEP)
const resetMermaidZoom = () => setMermaidZoom(1)

// Ctrl/⌘ + 滚轮：以当前位置为中心平滑缩放
const onMermaidZoomWheel = (event: WheelEvent) => {
  if (!event.ctrlKey && !event.metaKey) return
  event.preventDefault()
  const delta = event.deltaY > 0 ? -MERMAID_ZOOM_STEP : MERMAID_ZOOM_STEP
  setMermaidZoom(mermaidZoomScale.value + delta)
}

// ─── 放大弹窗：拖拽平移 ───────────────────────────────────────────────
// 放大超过 100% 后，图表会溢出可视区域，通过鼠标拖拽滚动 body 查看细节。
const mermaidIsPanning = ref(false)
let panStartX = 0
let panStartY = 0
let panStartScrollLeft = 0
let panStartScrollTop = 0

// 仅当存在可滚动溢出（即放大后超出视口）时才允许拖拽
const isMermaidPannable = (): boolean => {
  const host = mermaidDialogContainerRef.value
  if (!host) return false
  return host.scrollWidth > host.clientWidth || host.scrollHeight > host.clientHeight
}

const onMermaidPanStart = (event: MouseEvent) => {
  if (event.button !== 0) return // 仅响应左键
  const host = mermaidDialogContainerRef.value
  if (!host || !isMermaidPannable()) return
  event.preventDefault()
  mermaidIsPanning.value = true
  panStartX = event.clientX
  panStartY = event.clientY
  panStartScrollLeft = host.scrollLeft
  panStartScrollTop = host.scrollTop
  window.addEventListener('mousemove', onMermaidPanMove)
  window.addEventListener('mouseup', onMermaidPanEnd)
}

const onMermaidPanMove = (event: MouseEvent) => {
  const host = mermaidDialogContainerRef.value
  if (!host || !mermaidIsPanning.value) return
  host.scrollLeft = panStartScrollLeft - (event.clientX - panStartX)
  host.scrollTop = panStartScrollTop - (event.clientY - panStartY)
}

const onMermaidPanEnd = () => {
  mermaidIsPanning.value = false
  window.removeEventListener('mousemove', onMermaidPanMove)
  window.removeEventListener('mouseup', onMermaidPanEnd)
}

// ─── 放大弹窗：导出图片（下载 / 复制）────────────────────────────────
// 将渲染后的 SVG 光栅化为 PNG。2x 缩放保证清晰度，白色底避免透明背景。
const mermaidSvgToCanvas = async (svgEl: SVGSVGElement): Promise<HTMLCanvasElement> => {
  const clone = svgEl.cloneNode(true) as SVGSVGElement
  clone.style.transform = '' // 去除缩放，导出原始尺寸

  const viewBox = svgEl.viewBox?.baseVal
  const rect = svgEl.getBoundingClientRect()
  const width =
    (viewBox && viewBox.width) ||
    parseFloat(svgEl.getAttribute('width') || '') ||
    rect.width ||
    800
  const height =
    (viewBox && viewBox.height) ||
    parseFloat(svgEl.getAttribute('height') || '') ||
    rect.height ||
    600

  clone.setAttribute('width', String(width))
  clone.setAttribute('height', String(height))
  if (!clone.getAttribute('xmlns')) {
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  }

  const xml = new XMLSerializer().serializeToString(clone)
  const src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(xml)

  const img = new Image()
  await new Promise<void>((resolve, reject) => {
    img.onload = () => resolve()
    img.onerror = () => reject(new Error('SVG load failed'))
    img.src = src
  })

  const dpr = 2
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(width * dpr))
  canvas.height = Math.max(1, Math.round(height * dpr))
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('Unable to create canvas context')
  ctx.fillStyle = '#ffffff'
  ctx.fillRect(0, 0, canvas.width, canvas.height)
  ctx.scale(dpr, dpr)
  ctx.drawImage(img, 0, 0, width, height)
  return canvas
}

const canvasToBlob = (canvas: HTMLCanvasElement): Promise<Blob | null> =>
  new Promise((resolve) => canvas.toBlob(resolve, 'image/png'))

const downloadMermaidImage = async () => {
  const svg = getZoomSvgElement()
  if (!svg) return
  try {
    const canvas = await mermaidSvgToCanvas(svg)
    const blob = await canvasToBlob(canvas)
    if (!blob) throw new Error('Export failed')
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `mermaid-${Date.now()}.png`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
    ElMessage.success(t('aiChat.mermaid.imageDownloaded'))
  } catch (err) {
    console.warn('Failed to download Mermaid image:', err)
    ElMessage.error(t('aiChat.mermaid.imageDownloadFailed'))
  }
}

const copyMermaidImage = async () => {
  const svg = getZoomSvgElement()
  if (!svg) return
  if (typeof ClipboardItem === 'undefined' || !navigator.clipboard?.write) {
    ElMessage.warning(t('aiChat.mermaid.copyUnsupported'))
    return
  }
  try {
    const canvas = await mermaidSvgToCanvas(svg)
    const blob = await canvasToBlob(canvas)
    if (!blob) throw new Error('Export failed')
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': blob })])
    ElMessage.success(t('aiChat.mermaid.imageCopied'))
  } catch (err) {
    console.warn('Failed to copy Mermaid image:', err)
    ElMessage.error(t('aiChat.mermaid.imageCopyFailed'))
  }
}

const toggleDeviceMenu = () => {
  if (isSending.value) return
  deviceMenuVisible.value = !deviceMenuVisible.value
  if (deviceMenuVisible.value) {
    deviceKeyword.value = ''
    showTopMoreMenu.value = false
    if (!devices.value.length && !isLoadingDevices.value) fetchDevices()
    nextTick(() => deviceMenuRef.value?.querySelector('input')?.focus?.())
  }
}

const selectDevice = (device: DeviceInfo) => {
  targetDeviceId.value = device.id
  targetDeviceName.value = device.name || device.id
  // 设备操作与配置管理员 / 日志分析 Agent 互斥
  clearTargetAgent()
  selectedLogFiles.value = []
  deviceMenuVisible.value = false
}

const isDeviceSelected = computed(() => !!targetDeviceId.value)
const clearTargetDevice = () => { targetDeviceId.value = null; targetDeviceName.value = null }
const clearTargetAgent = () => {
  if (targetAgent.value?.agentType === 'package-manager') {
    selectedPackageFiles.value = []
  }
  targetAgent.value = null
  selectedProjectRepoId.value = null
  saveAgentSelectionToState()
}
const clearSelectedLogFile = (file: File) => {
  selectedLogFiles.value = selectedLogFiles.value.filter(
    (candidate) => candidate !== file,
  )
}
const clearSelectedPackageFile = (file: File) => {
  selectedPackageFiles.value = selectedPackageFiles.value.filter(
    (candidate) => candidate !== file,
  )
}

const triggerLogFilePicker = () => {
  if (isSending.value || isLogFileUploadDisabled.value) return
  setTargetAgent(logAnalysisAgentOption)
  ensureProjectRepoOptions()
  logFileInputRef.value?.click()
}

const selectLogFiles = (files: File[]) => {
  if (!files.length) return
  selectedPackageFiles.value = []
  selectedLogFiles.value = mergeUniquePackageFiles(selectedLogFiles.value, files)
  setTargetAgent(logAnalysisAgentOption)
  ensureProjectRepoOptions()
}

const handleLogFileChange = (event: Event) => {
  const input = event.target as HTMLInputElement
  selectLogFiles(Array.from(input.files || []))
  input.value = ''
}

const triggerPackageFilePicker = () => {
  if (isSending.value || isProjectExpertAgentSelected.value) return
  setTargetAgent(packageAgentOption)
  ensureProjectRepoOptions()
  packageFileInputRef.value?.click()
}

const triggerAttachmentPicker = () => {
  if (isPackageAgentSelected.value) triggerPackageFilePicker()
  else triggerLogFilePicker()
}

const selectPackageFiles = (files: File[]) => {
  if (!files.length) return
  selectedLogFiles.value = []
  selectedPackageFiles.value = mergeUniquePackageFiles(selectedPackageFiles.value, files)
  setTargetAgent(packageAgentOption)
  ensureProjectRepoOptions()
}

const handlePackageFileChange = (event: Event) => {
  const input = event.target as HTMLInputElement
  selectPackageFiles(Array.from(input.files || []))
  input.value = ''
}

const isFileDragEvent = (event: DragEvent) =>
  Array.from(event.dataTransfer?.types || []).includes('Files')

const handleLogFileDragEnter = (event: DragEvent) => {
  // Images are droppable under any agent, so gate only on isSending here.
  if (isSending.value || !isFileDragEvent(event)) return
  logFileDragDepth += 1
  isLogFileDragOver.value = true
}

const handleLogFileDragOver = (event: DragEvent) => {
  if (!isFileDragEvent(event) || !event.dataTransfer) return
  if (isSending.value) {
    event.dataTransfer.dropEffect = 'none'
    return
  }
  event.dataTransfer.dropEffect = 'copy'
}

const handleLogFileDragLeave = (event: DragEvent) => {
  if (!isFileDragEvent(event)) return
  logFileDragDepth = Math.max(0, logFileDragDepth - 1)
  if (logFileDragDepth === 0) isLogFileDragOver.value = false
}

const handleLogFileDrop = (event: DragEvent) => {
  logFileDragDepth = 0
  isLogFileDragOver.value = false
  if (isSending.value) return
  const files = Array.from(event.dataTransfer?.files || [])
  if (!files.length) return
  // In Configuration Manager mode every dropped file is a component input,
  // including formats that browsers report as images. The dedicated image
  // picker remains available when visual/OCR context is intended instead.
  if (isPackageAgentSelected.value) {
    selectPackageFiles(files)
    return
  }
  const imageFiles = files.filter((f) => (f.type || '').startsWith('image/'))
  const otherFiles = files.filter((f) => !(f.type || '').startsWith('image/'))
  // Images attach under any agent, independent of the log-file restrictions.
  if (imageFiles.length) void addImageFiles(imageFiles)
  // A dropped non-image follows the existing log-attachment rules.
  if (otherFiles.length && !isLogFileUploadDisabled.value) {
    selectLogFiles(otherFiles)
  }
}

// ── Image attachments (paste / pick / drop) — available for any agent ──

const readImageAsDataUrl = (file: File): Promise<string> =>
  new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(reader.error || new Error('read failed'))
    reader.readAsDataURL(file)
  })

const addImageFiles = async (files: File[] | FileList | null) => {
  const list = files ? Array.from(files) : []
  const images = list.filter((f) => f && (f.type || '').startsWith('image/'))
  if (!images.length) return
  for (const file of images) {
    const mime = (file.type || '').toLowerCase()
    if (!ACCEPTED_IMAGE_MIME_TYPES.includes(mime)) {
      appStore.showNotification({
        title: t('aiChat.image.unsupportedType', { name: file.name || 'image' }),
        type: 'warning',
      })
      continue
    }
    if (file.size > MAX_IMAGE_SIZE_MB * 1024 * 1024) {
      appStore.showNotification({
        title: t('aiChat.image.tooLarge', { name: file.name || 'image', mb: MAX_IMAGE_SIZE_MB }),
        type: 'warning',
      })
      continue
    }
    if (pendingImages.value.length >= MAX_IMAGE_COUNT) {
      appStore.showNotification({
        title: t('aiChat.image.tooMany', { max: MAX_IMAGE_COUNT }),
        type: 'warning',
      })
      break
    }
    try {
      const dataUrl = await readImageAsDataUrl(file)
      if (!dataUrl) continue
      pendingImages.value.push({
        id: generateUUID(),
        name: file.name || 'image',
        mediaType: mime,
        dataUrl,
      })
    } catch {
      appStore.showNotification({
        title: t('aiChat.image.readFailed', { name: file.name || 'image' }),
        type: 'error',
      })
    }
  }
}

const removePendingImage = (id: string) => {
  pendingImages.value = pendingImages.value.filter((img) => img.id !== id)
}

const triggerImagePicker = () => {
  if (isSending.value) return
  imageFileInputRef.value?.click()
}

const handleImageFileChange = (event: Event) => {
  const input = event.target as HTMLInputElement
  void addImageFiles(input.files)
  input.value = ''
}

const handleComposerPaste = (event: ClipboardEvent) => {
  const items = event.clipboardData?.items
  if (!items) return
  const files: File[] = []
  for (const item of Array.from(items)) {
    if (item.kind === 'file' && (item.type || '').startsWith('image/')) {
      const f = item.getAsFile()
      if (f) files.push(f)
    }
  }
  if (files.length) {
    // Clipboard carries an image: attach it instead of pasting raw content.
    // Plain-text paste (no image items) falls through to default behavior.
    event.preventDefault()
    void addImageFiles(files)
  }
}

const togglePackageAgent = () => {
  if (isPackageAgentSelected.value) {
    selectedPackageFiles.value = []
    clearTargetAgent()
    return
  }
  selectedLogFiles.value = []
  setTargetAgent(packageAgentOption)
}

const toggleLogAnalysisAgent = () => {
  if (
    targetAgent.value?.agentType === 'log-analysis'
    && selectedLogFiles.value.length === 0
  ) {
    clearTargetAgent()
    return
  }
  selectedPackageFiles.value = []
  setTargetAgent(logAnalysisAgentOption)
  ensureProjectRepoOptions()
}

const toggleProjectExpertAgent = () => {
  if (isProjectExpertAgentSelected.value) {
    clearTargetAgent()
    return
  }
  selectedLogFiles.value = []
  selectedPackageFiles.value = []
  setTargetAgent(projectExpertAgentOption)
  ensureProjectRepoOptions()
}

// NOTE: stream event handling, plan/device-action formatting, message
// upserts, and SSE buffer parsing have all moved to
// ``stores/conversationRuns.ts``. The remaining helpers here only support the
// synchronous package-agent path (which doesn't go through ChatRunService).

const submitPermissionDecision = async (
  decision: 'allow' | 'deny',
  options: { useEdited?: boolean } = {},
) => {
  const head = currentPermission.value
  const sid = effectiveSessionId.value
  if (!head || !sid || permissionDecisionInFlight.value) return

  let updatedArgs: Record<string, unknown> | null = null
  if (decision === 'allow' && options.useEdited) {
    try {
      const parsed = JSON.parse(head.editingArgs || '{}')
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
        head.editingError = t('aiChat.permission.jsonObjectRequired')
        return
      }
      updatedArgs = parsed as Record<string, unknown>
      head.editingError = null
    } catch (err: any) {
      head.editingError = t('aiChat.permission.jsonParseFailed', { error: err?.message || String(err) })
      return
    }
  }

  permissionDecisionInFlight.value = true
  try {
    const authToken = userStore.token as unknown as string | undefined
    await runsStore.submitPermission(sid, head.request_id, decision, {
      updatedArgs,
      authToken: authToken || null,
    })
  } catch {
    // store has already recorded ``editingError`` on the entry
  } finally {
    permissionDecisionInFlight.value = false
  }
}

const submitClarificationAnswers = async () => {
  const head = currentClarification.value
  const sid = effectiveSessionId.value
  if (!head || !sid || head.submitting) return
  try {
    const authToken = userStore.token as unknown as string | undefined
    await runsStore.submitClarification(sid, head.request_id, {
      authToken: authToken || null,
    })
  } catch {
    // store has already recorded ``error`` on the entry
  }
}

const submitPublicationDecision = (decision: 'confirm' | 'cancel') => {
  const head = currentClarification.value
  if (!head?.publication_review || head.submitting) return
  const option = head.questions[0]?.options[decision === 'confirm' ? 0 : 1]
  if (!option) return
  head.draftSelected[0] = [option.label]
  return submitClarificationAnswers()
}

const { handleKeydown, handleCompositionStart, handleCompositionEnd } =
  useChatComposerKeyboard(() => { void sendMessage() })

const cancelCurrentRun = async () => {
  const sid = effectiveSessionId.value
  if (!sid || cancelInFlight.value) return
  cancelInFlight.value = true
  try {
    await runsStore.cancelActiveRun(sid, {
      authToken: (userStore.token as unknown as string) || null,
    })
  } finally {
    cancelInFlight.value = false
  }
}

const triggerSessionSummary = (userContent: string, sid: string | null) => {
  if (!userContent || !userContent.trim()) return
  // Fire-and-forget: lightweight model generates a sidebar session summary.
  userApi
    .summarizeUserMessage({
      user_content: userContent,
      session_id: sid || undefined,
      max_length: 16,
      persist: isLoggedIn.value,
    })
    .then((resp) => {
      const summary = (resp?.summary || '').trim()
      const resolvedId = resp?.session_id || sid
      if (!summary || !resolvedId) return
      if (isLoggedIn.value) {
        sessionStore.upsertSessionTitle(resolvedId, summary)
      }
    })
    .catch((err) => {
      console.warn('Failed to generate session summary', err)
    })
}

const requestLogWorkspaceReplacementDecision = async (): Promise<LogWorkspaceReplacementDecision> => {
  try {
    await ElMessageBox.confirm(
      t('aiChat.logWorkspaceReplacement.message'),
      t('aiChat.logWorkspaceReplacement.title'),
      {
        type: 'warning',
        confirmButtonText: t('aiChat.logWorkspaceReplacement.newChat'),
        cancelButtonText: t('aiChat.logWorkspaceReplacement.replace'),
        distinguishCancelAndClose: true,
        showClose: true,
        closeOnClickModal: false,
        customClass: 'rw-log-workspace-replacement-box',
      },
    )
    return resolveLogWorkspaceReplacementAction('confirm')
  } catch (action: unknown) {
    return resolveLogWorkspaceReplacementAction(action)
  }
}

/**
 * Move the untouched composer draft to a fresh session. Do not auto-send:
 * correlated analysis may still need the user to add the earlier log files.
 */
const startNewConversationWithLogDraft = async () => {
  const draftMessage = inputMessage.value
  const draftFiles = [...selectedLogFiles.value]
  const draftImages = [...pendingImages.value]
  const draftProjectRepoId = selectedProjectRepoId.value

  sessionStore.startNewChat()
  await nextTick()

  inputMessage.value = draftMessage
  selectedLogFiles.value = draftFiles
  pendingImages.value = draftImages
  setTargetAgent(logAnalysisAgentOption)
  selectedProjectRepoId.value = draftProjectRepoId
  ensureProjectRepoOptions()
  textareaRef.value?.focus()
}

const sendMessage = async () => {
  if (!isLoggedIn.value) {
    appStore.requestLoginModal('login')
    return
  }
  // Only block the current session — other sessions running in the
  // background do not affect this one.
  if (currentConversation.value?.isSending) return
  const content = inputMessage.value.trim()
  const logFilesForRequest = [...selectedLogFiles.value]
  const packageFilesForRequest = [...selectedPackageFiles.value]
  const pendingImagesForRecovery = [...pendingImages.value]
  const imagesForRequest: ChatImageAttachment[] = pendingImages.value.map((img) => ({
    media_type: img.mediaType,
    data: img.dataUrl,
  }))
  // Local previews for the optimistic user bubble: the same data: URLs already
  // in memory, so the thumbnails appear the instant the message is sent.
  const imagePreviewsForRequest: ChatEntryImage[] = pendingImages.value.map((img) => ({
    name: img.name,
    mediaType: img.mediaType,
    url: img.dataUrl,
  }))
  if (
    !content &&
    !logFilesForRequest.length &&
    !packageFilesForRequest.length &&
    !imagesForRequest.length
  ) return

  const shouldUseProjectExpertAgent =
    isProjectExpertAgentSelected.value || content.includes(`@${agentName(projectExpertAgentOption)}`)

  const shouldUseLogAnalysisAgent =
    !shouldUseProjectExpertAgent &&
    (
      isLogAnalysisAgentSelected.value
      || content.includes(`@${agentName(logAnalysisAgentOption)}`)
      || logFilesForRequest.length > 0
    )

  const shouldUsePackageAgent =
    !shouldUseProjectExpertAgent &&
    !shouldUseLogAnalysisAgent &&
    (
      isPackageAgentSelected.value ||
      packageFilesForRequest.length > 0 ||
      content.includes(`@${agentName(packageAgentOption)}`)
    )

  const authToken = (userStore.token as unknown as string) || null
  if (authToken && userToken.isExpired(authToken)) {
    userStore.clear()
    appStore.showNotification({
      title: t('aiChat.notifications.sessionExpired'),
      type: 'warning',
      duration: 5000,
    })
    appStore.requestLoginModal('login')
    return
  }

  const canContinueSend = await proceedAfterLogWorkspaceReplacementCheck({
    requiresConfirmation: requiresLogWorkspaceReplacementConfirmation({
      hasExistingLogWorkspace: Boolean(currentConversation.value?.hasLogWorkspaceContext),
      isLogAnalysisRequest: shouldUseLogAnalysisAgent,
      logFileCount: logFilesForRequest.length,
    }),
    requestDecision: requestLogWorkspaceReplacementDecision,
    startNewConversation: startNewConversationWithLogDraft,
  })
  if (!canContinueSend) return

  // Allocate a session id locally if this is a brand-new conversation. The
  // run service / DB layer will keep the same id for persistence.
  let sid = effectiveSessionId.value
  if (!sid) {
    sid = generateUUID()
    localSessionId.value = sid
    sessionStore.setSelected(sid)
  }
  // Ensure a state object exists for the session before any local writes.
  const state = runsStore.ensureState(sid)

  // Fire-and-forget sidebar title summary.
  if (content) {
    triggerSessionSummary(content, sid)
  }

  if (shouldUseProjectExpertAgent && !isProjectExpertAgentSelected.value) {
    setTargetAgent(projectExpertAgentOption)
  }
  if (shouldUseLogAnalysisAgent && targetAgent.value?.agentType !== 'log-analysis') {
    setTargetAgent(logAnalysisAgentOption)
  }
  if (shouldUsePackageAgent && !isPackageAgentSelected.value) {
    setTargetAgent(packageAgentOption)
  }

  // Project Expert and pure Configuration Manager searches remain
  // project-bound. Component-bearing packaging turns deliberately start
  // unbound and confirm the inferred project through the mandatory card.
  if (
    (
      shouldUseProjectExpertAgent ||
      (
        shouldUsePackageAgent &&
        configurationManagerRequiresProject(packageFilesForRequest.length)
      )
    ) &&
    selectedProjectRepoId.value === null
  ) {
    ensureProjectRepoOptions()
    appStore.showNotification({ title: t('aiChat.notifications.selectProjectFirst'), type: 'warning' })
    return
  }

  const outgoingContent = content || (
    shouldUsePackageAgent
      ? t('aiChat.defaultPackageBuildMessage')
      : t('aiChat.defaultLogAnalysisMessage')
  )
  // History payload only for anonymous sessions; logged-in sessions reuse
  // the DB transcript on the backend.
  const historyPayload = isLoggedIn.value
    ? []
    : state.messages.map((msg) => ({ role: msg.role, content: msg.content }))

  inputMessage.value = ''
  // Images are sent with this turn; clear the composer chips now (a snapshot
  // was taken in ``imagesForRequest``).
  pendingImages.value = []
  deviceMenuVisible.value = false

  const images = imagesForRequest.length ? imagesForRequest : undefined
  const imagePreviews = imagePreviewsForRequest.length ? imagePreviewsForRequest : undefined

  try {
    if (shouldUseProjectExpertAgent) {
      selectedLogFiles.value = []
      await runsStore.startProjectExpertRun(
        sid,
        {
          message: outgoingContent,
          history: historyPayload,
          project_repo_id: selectedProjectRepoId.value as number,
          remember: true,
          images,
          imagePreviews,
        },
        { authToken },
      )
    } else if (shouldUseLogAnalysisAgent) {
      const filesSnapshot = logFilesForRequest
      // Clear the files from the composer; startLogAnalysisRun will append the
      // user message with attachment info.
      if (filesSnapshot.length) selectedLogFiles.value = []
      try {
        await runsStore.startLogAnalysisRun(
          sid,
          {
            message: outgoingContent,
            history: historyPayload,
            files: filesSnapshot,
            project_repo_id: selectedProjectRepoId.value,
            remember: true,
            images,
            imagePreviews,
          },
          { authToken },
        )
      } catch (err) {
        if (filesSnapshot.length) selectedLogFiles.value = filesSnapshot
        throw err
      }
    } else if (shouldUsePackageAgent) {
      // Configuration Manager — component files are an independent optimistic
      // attachment flow. Restore the untouched selection if stream creation or
      // upload fails so the user can retry without choosing every file again.
      selectedPackageFiles.value = []
      const packageRunSucceeded = await runsStore.startPackageSearchRun(
        sid,
        {
          message: outgoingContent,
          history: historyPayload,
          project_repo_id: selectedProjectRepoId.value,
          files: packageFilesForRequest,
          remember: true,
          images,
          imagePreviews,
        },
        { authToken },
      )
      // The stream helper returns false for both transport failures and an
      // HTTP-200 SSE error/cancelled terminal emitted during preprocessing.
      if (!packageRunSucceeded) {
        selectedPackageFiles.value = mergeUniquePackageFiles(
          selectedPackageFiles.value,
          packageFilesForRequest,
        )
        if (!inputMessage.value.trim()) inputMessage.value = content
        if (!pendingImages.value.length) pendingImages.value = pendingImagesForRecovery
      }
    } else {
      // DeviceAgent — fully delegated to the run store.
      await runsStore.startDeviceRun(
        sid,
        {
          message: outgoingContent,
          history: historyPayload,
          target_device_id: targetDeviceId.value,
          target_device_name: targetDeviceName.value,
          remember: true,
          images,
          imagePreviews,
        },
        { authToken },
      )
    }

    if (isLoggedIn.value) {
      try { await sessionStore.load() } catch (error) { console.warn('Failed to refresh session list', error) }
    }
  } catch (error: any) {
    console.error('Request failed', error)
    if (shouldUsePackageAgent && packageFilesForRequest.length) {
      selectedPackageFiles.value = mergeUniquePackageFiles(
        selectedPackageFiles.value,
        packageFilesForRequest,
      )
      if (!inputMessage.value.trim()) inputMessage.value = content
      if (!pendingImages.value.length) pendingImages.value = pendingImagesForRecovery
    }
  }
}

const welcomeGreeting = computed(() => {
  const h = new Date().getHours()
  if (h < 6) return t('aiChat.greeting.beforeDawn')
  if (h < 11) return t('aiChat.greeting.morning')
  if (h < 13) return t('aiChat.greeting.noon')
  if (h < 18) return t('aiChat.greeting.afternoon')
  return t('aiChat.greeting.evening')
})

type CapabilityCard = {
  icon: string
  labelKey: string
  kind: 'package' | 'device' | 'log' | 'projectExpert'
  descKey: string
  promptKeys: string[]
}

const capabilityCards: CapabilityCard[] = [
  { icon: 'box', labelKey: 'aiChat.capabilities.package.label', kind: 'package',
    descKey: 'aiChat.capabilities.package.desc',
    promptKeys: [
      'aiChat.capabilities.package.prompt1',
      'aiChat.capabilities.package.prompt2',
      'aiChat.capabilities.package.prompt3',
    ] },
  { icon: 'device', labelKey: 'aiChat.capabilities.device.label', kind: 'device',
    descKey: 'aiChat.capabilities.device.desc',
    promptKeys: [
      'aiChat.capabilities.device.prompt1',
      'aiChat.capabilities.device.prompt2',
      'aiChat.capabilities.device.prompt3',
    ] },
  { icon: 'logs', labelKey: 'aiChat.capabilities.log.label', kind: 'log',
    descKey: 'aiChat.capabilities.log.desc',
    promptKeys: [
      'aiChat.capabilities.log.prompt1',
      'aiChat.capabilities.log.prompt2',
      'aiChat.capabilities.log.prompt3',
    ] },
  { icon: 'project', labelKey: 'aiChat.capabilities.projectExpert.label', kind: 'projectExpert',
    descKey: 'aiChat.capabilities.projectExpert.desc',
    promptKeys: [
      'aiChat.capabilities.projectExpert.prompt1',
      'aiChat.capabilities.projectExpert.prompt2',
      'aiChat.capabilities.projectExpert.prompt3',
    ] },
]

// 记录每张卡片当前展示到第几条表达，重复点击则推进到下一条
const capabilityVariantIdx: number[] = capabilityCards.map(() => -1)
// 上次由卡片自动填入的原文，用于判断输入框是否被用户改动过
const lastInjectedPrompt = ref('')

const onPickCapability = (card: CapabilityCard, i: number) => {
  if (card.kind === 'log') setTargetAgent(logAnalysisAgentOption)
  else if (card.kind === 'package') setTargetAgent(packageAgentOption)
  else if (card.kind === 'projectExpert') setTargetAgent(projectExpertAgentOption)
  // 设备操作走独立下拉，不在 AgentOption 体系内，故清除已选中的任何 Agent
  else clearTargetAgent()

  // 仅当输入框为空、或仍是上次自动填入的原文时才覆盖/循环；用户已改动则保留其内容
  const untouched = inputMessage.value === '' || inputMessage.value === lastInjectedPrompt.value
  if (untouched) {
    const next = (capabilityVariantIdx[i] + 1) % card.promptKeys.length
    capabilityVariantIdx[i] = next
    inputMessage.value = t(card.promptKeys[next])
    lastInjectedPrompt.value = inputMessage.value
  }
  nextTick(() => textareaRef.value?.focus())
}

const currentChatTitle = computed(() => {
  if (sessionStore.currentTitle) return sessionStore.currentTitle
  if (isWelcomeMode.value) return t('aiChat.newChat')
  return t('aiChat.currentChat')
})

const sessionMessageCount = computed(() => chatHistory.value.length)

// Sharing requires a persisted session with at least one message; brand-new,
// not-yet-saved local conversations have no backend session_id to share.
const canShareConversation = computed(
  () => chatHistory.value.length > 0 && !!sessionStore.selectedSessionId,
)

const openShareModal = () => {
  if (!canShareConversation.value) return
  showTopMoreMenu.value = false
  showShareModal.value = true
}
</script>

<template>
  <div class="rw-chat-panel">
    <!-- Topbar -->
    <header class="rw-topbar">
      <div class="rw-topbar-left">
        <span class="rw-crumb">{{ currentChatTitle }}</span>
        <span v-if="!isWelcomeMode" class="rw-crumb-meta">· {{ t('aiChat.messageCount', { count: sessionMessageCount }) }}</span>
      </div>
      <div class="rw-topbar-right">
        <button class="rw-model-pill" type="button">
          <span class="rw-model-dot"></span>
          {{ t('aiChat.deviceSummary', { online: devices.filter(d => d.status === 'online').length, total: devices.length || 0 }) }}
        </button>
        <div class="rw-top-more-wrap">
          <button
            ref="topMoreBtnRef"
            class="rw-top-action"
            @click="showTopMoreMenu = !showTopMoreMenu"
            aria-haspopup="menu"
            :aria-expanded="showTopMoreMenu"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="5" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="19" cy="12" r="1" fill="currentColor"/></svg>
          </button>
          <div
            v-if="showTopMoreMenu"
            ref="topMoreMenuRef"
            class="rw-top-menu"
            role="menu"
          >
            <div class="rw-top-menu-group">{{ t('aiChat.menu.conversation') }}</div>
            <button class="rw-menu-item" @click="openRenameModal">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h4l10-10-4-4L4 16zM14 6l4 4"/></svg>
              {{ t('aiChat.menu.rename') }} <span class="rw-kbd-right">F2</span>
            </button>
            <button class="rw-menu-item" @click="toggleCurrentPin">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v6M9 8h6l2 6H7zM12 14v8"/></svg>
              {{ currentSessionPinned ? t('aiChat.menu.unpin') : t('aiChat.menu.pin') }}
            </button>

            <div class="rw-menu-divider"/>
            <div class="rw-top-menu-group">{{ t('aiChat.menu.export') }}</div>
            <button
              v-if="canShareConversation"
              class="rw-menu-item"
              @click="openShareModal"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7M16 6l-4-4-4 4M12 2v14"/></svg>
              {{ t('aiChat.menu.share') }}
            </button>
            <button class="rw-menu-item" @click="exportCurrentConversationMarkdown">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 15v-6l3 3 3-3v6M17 9v6M14 12l3 3 3-3"/></svg>
              {{ t('aiChat.menu.exportMarkdown') }}
            </button>

            <div class="rw-menu-divider"/>
            <button
              v-if="sessionStore.selectedSessionId"
              class="rw-menu-item is-danger"
              @click="deleteCurrentSession"
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6"/></svg>
              {{ t('aiChat.menu.deleteSession') }}
            </button>
          </div>
        </div>
      </div>
    </header>

    <!-- Scroll body -->
    <div ref="chatContainerRef" class="rw-scroll" @click="onThreadClick">
      <!-- Welcome -->
      <div v-if="isWelcomeMode" class="rw-welcome">
        <div class="rw-welcome-badge">
          <span class="rw-dot-success"></span>
          {{ t('aiChat.welcomeDeviceSummary', { online: devices.filter(d => d.status === 'online').length, total: devices.length || 0 }) }}
        </div>
        <h1 class="rw-welcome-title">
          {{ t('aiChat.welcomeTitle', { greeting: welcomeGreeting, name: currentUserName }) }}<br/>{{ t('aiChat.welcomeQuestion') }}
        </h1>
        <div class="rw-welcome-sub">
          {{ t('aiChat.welcomeSubtitle') }}
        </div>
        <div class="rw-cap-grid">
          <div
            v-for="(c, i) in capabilityCards"
            :key="i"
            class="rw-cap-card"
            @click="onPickCapability(c, i)"
          >
            <div class="rw-cap-label">
              <svg v-if="c.icon === 'logs'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h10M4 18h16"/><circle cx="18" cy="12" r="1.4"/></svg>
              <svg v-else-if="c.icon === 'device'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="11" rx="1.5"/><path d="M8 21h8M12 17v4"/><circle cx="7" cy="11" r="0.4" fill="currentColor"/></svg>
              <svg v-else-if="c.icon === 'project'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5V6.75A2.75 2.75 0 0 1 6.75 4H20v13H6.75A2.75 2.75 0 0 0 4 19.5Z"/><path d="M8 8h8M8 12h6"/></svg>
              <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z"/><path d="M3 7.5 12 12l9-4.5M12 12v9"/></svg>
              {{ t(c.labelKey) }}
            </div>
            <div class="rw-cap-desc">{{ t(c.descKey) }}</div>
          </div>
        </div>
      </div>

      <!-- Loading -->
      <div v-else-if="loadingMessages" class="rw-loading">{{ t('aiChat.loadingHistory') }}</div>

      <!-- Thread -->
      <div v-else class="rw-thread">
        <div
          v-for="(msg, msgIndex) in chatHistory"
          :key="msg.id"
          :class="['rw-msg', msg.role === 'user' ? 'is-user' : 'is-ai']"
        >
          <template v-if="msg.role === 'user'">
            <div v-if="msg.images && msg.images.length" class="rw-user-images">
              <button
                v-for="(img, imgIndex) in msg.images"
                :key="img.id || `${msg.id}-${imgIndex}`"
                type="button"
                class="rw-user-image"
                :title="img.name"
                :aria-label="t('aiChat.image.preview', { name: img.name })"
                @click="openImageViewer(img)"
              >
                <img :src="img.url" :alt="img.name" loading="lazy" />
              </button>
            </div>
            <div v-if="userBubbleText(msg.content)" class="rw-user-bubble">
              {{ userBubbleText(msg.content) }}
            </div>
            <div class="rw-user-meta-line">{{ currentUserName }}</div>
          </template>
          <template v-else>
            <div class="rw-ai-body">
              <div
                v-if="msg.ocrStatus && msg.ocrStatus.imageCount > 0"
                class="rw-ocr-degraded"
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>
                <span>{{ ocrDegradedText(msg.ocrStatus) }}</span>
              </div>
              <AgentTraceStream
                v-if="
                  (msg.traceEvents && msg.traceEvents.length > 0) ||
                  msg.traceRunning ||
                  getAgentMessageView(msg.content).completionSummary ||
                  visualAnalysisForMessage(msg, msgIndex)
                "
                class="rw-ai-trace"
                :events="msg.traceEvents || []"
                :running="!!msg.traceRunning"
                :completion-summary="getAgentMessageView(msg.content).completionSummary"
                :visual-analysis="visualAnalysisForMessage(msg, msgIndex)?.text"
                :visual-image-count="visualAnalysisForMessage(msg, msgIndex)?.imageCount"
              />
              <template v-if="msg.content === THINKING_PLACEHOLDER">
                <div v-if="!msg.traceRunning" class="rw-thinking">{{ t('aiChat.thinking') }}</div>
              </template>
              <StreamingAnswer
                :content="msg.content === THINKING_PLACEHOLDER ? '' : getAgentMessageView(msg.content).displayMarkdown"
                :animate="!!msg.animateAnswer"
                :running="!!msg.traceRunning && !currentClarification?.paused"
                @progress="scrollToBottom"
                @busy="answerDisplayBusy[msg.id] = $event"
                @settled="msg.animateAnswer = false"
              />
              <template v-if="msg.content !== THINKING_PLACEHOLDER">
                <div
                  v-if="msg.content && msg.content.trim() && !msg.traceRunning && !answerDisplayBusy[msg.id]"
                  class="rw-ai-actions"
                >
                  <div class="rw-ai-action-group">
                    <button
                      class="rw-ai-action-btn"
                      type="button"
                      :aria-label="t('aiChat.export.copyMarkdownAction')"
                      :data-tooltip="t('aiChat.export.copyMarkdownAction')"
                      @click="copyAiMessageMarkdown(msg)"
                    >
                      <Copy :size="17" :stroke-width="1.8" aria-hidden="true" />
                    </button>
                    <button
                      class="rw-ai-action-btn"
                      type="button"
                      :aria-label="t('aiChat.export.singlePdfAction')"
                      :data-tooltip="t('aiChat.export.singlePdfAction')"
                      @click="exportAiMessagePdf(msg)"
                    >
                      <FileDown :size="17" :stroke-width="1.8" aria-hidden="true" />
                    </button>
                  </div>
                  <div class="rw-ai-run-meta">
                    <span v-if="getAgentMessageView(msg.content).completionSummary?.model" class="rw-ai-run-model">
                      {{ t('aiChat.export.modelMeta', {
                        model: getAgentMessageView(msg.content).completionSummary?.model,
                      }) }}
                    </span>
                    <template v-if="getAgentMessageView(msg.content).completionSummary?.duration">
                      <span class="rw-ai-run-separator" aria-hidden="true"></span>
                      <span>
                        {{ t('aiChat.export.durationMeta', {
                          duration: getAgentMessageView(msg.content).completionSummary?.duration,
                        }) }}
                      </span>
                    </template>
                    <span
                      v-if="getAgentMessageView(msg.content).completionSummary?.model"
                      class="rw-ai-run-separator"
                      aria-hidden="true"
                    ></span>
                    <span class="rw-ai-disclaimer">{{ t('aiChat.disclaimer') }}</span>
                    <span
                      v-if="getAgentMessageView(msg.content).completionSummary?.packageBuildCompleted"
                      class="rw-ai-packaging-notice"
                    >{{ t('aiChat.packageBuildConfirmationNotice') }}</span>
                  </div>
                </div>
              </template>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- Composer -->
    <div class="rw-composer-wrap">
      <ClarificationCard
        v-if="currentClarification?.paused"
        :key="currentClarification.request_id"
        :pending="currentClarification"
        class="rw-clarification-inline"
        @submit="submitClarificationAnswers"
      />
      <div
        v-if="currentClarification?.publication_review"
        :class="publicationNeedsReview ? 'rw-modal-backdrop rw-hitl-backdrop' : 'rw-publication-inline'"
      >
        <div
          :class="{ 'rw-modal rw-hitl-modal rw-clarification-modal': publicationNeedsReview }"
          :role="publicationNeedsReview ? 'dialog' : undefined"
          :aria-modal="publicationNeedsReview ? true : undefined"
          :aria-label="publicationNeedsReview ? t('aiChat.clarification.publicationTitle') : undefined"
        >
          <PackagePublicationCard
            :review="currentClarification.publication_review"
            :submitting="currentClarification.submitting"
            :error="currentClarification.error"
            @decide="submitPublicationDecision"
          />
        </div>
      </div>
      <!-- 未登录提示 -->
      <div v-if="!isLoggedIn" class="rw-composer-alert is-login">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;margin-top:1px"><circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>
        <span>
          {{ t('aiChat.loginRequired.prefix') }}
          <button type="button" class="rw-alert-link" @click="appStore.requestLoginModal('login')">{{ t('aiChat.loginRequired.login') }}</button>
          {{ t('aiChat.loginRequired.or') }}
          <button type="button" class="rw-alert-link" @click="appStore.requestLoginModal('register')">{{ t('aiChat.loginRequired.register') }}</button>
        </span>
      </div>
      <!-- Log analysis inline warnings — placed above the composer so the input
           box, tool chips and project dropdown stay anchored to the bottom and
           do not shift under the cursor when an alert appears/disappears. -->
      <!-- GeneralAgent 路由建议：提示用户该请求需切换到对应专门 Agent。 -->
      <div v-if="suggestedAgentLabel" class="rw-composer-alert is-suggest">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;margin-top:1px"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>
        <span>
          {{ t('aiChat.suggest.prefix') }} <strong>{{ suggestedAgentLabel }}</strong> {{ t('aiChat.suggest.suffix') }}
          <button v-if="suggestedAgentSwitchable" type="button" class="rw-alert-link" @click="chooseSuggestedAgent">{{ t('aiChat.suggest.switchTo', { agent: suggestedAgentLabel }) }}</button>
        </span>
      </div>
      <div v-if="logAnalysisMetadataError" class="rw-composer-alert is-error">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;margin-top:1px"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <span>
          {{ t('aiChat.alerts.metadataErrorBefore') }} <code>metadata.json</code>{{ t('aiChat.alerts.metadataErrorAfter') }}
        </span>
      </div>
      <div v-else-if="logAnalysisNoAttachmentWarning" class="rw-composer-alert is-warn">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;margin-top:1px"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        <span>
          <strong>{{ t('aiChat.agents.logAnalysis') }}</strong>{{ t('aiChat.alerts.logAttachmentBefore') }} <code>.zip</code> <code>.tar.gz</code> <code>.tgz</code> <code>.tar.bz2</code> <code>.tar.xz</code> <code>.7z</code> <code>.rar</code> {{ t('aiChat.alerts.logAttachmentMiddle') }} <code>.log</code> <code>.txt</code> <code>.json</code> <code>.xml</code> <code>.csv</code> <code>.md</code> <code>.ini</code> <code>.yaml</code> <code>.toml</code> <code>.xls</code> <code>.xlsx</code> {{ t('aiChat.alerts.logAttachmentAfter') }}
          <button type="button" class="rw-alert-link" @click="toggleProjectExpertAgent">{{ t('aiChat.agents.projectExpert') }}</button>{{ t('aiChat.sentencePeriod') }}
        </span>
      </div>
      <div
        class="rw-composer"
        :class="{ 'is-log-drag-over': isLogFileDragOver }"
        @dragenter.prevent="handleLogFileDragEnter"
        @dragover.prevent="handleLogFileDragOver"
        @dragleave.prevent="handleLogFileDragLeave"
        @drop.prevent="handleLogFileDrop"
      >
        <!-- Target chip -->
        <div v-if="targetAgentName || targetDeviceName" class="rw-target-chip">
          <span class="rw-target-label">{{ t('aiChat.currentTarget') }}</span>
          <span class="rw-target-value">
            <template v-if="targetAgentName">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z"/><path d="M3 7.5 12 12l9-4.5M12 12v9"/></svg>
              {{ targetAgentName }}
            </template>
            <template v-else>{{ targetDeviceName }}</template>
          </span>
          <button
            class="rw-target-clear"
            type="button"
            @click="targetAgentName ? clearTargetAgent() : clearTargetDevice()"
            :aria-label="t('aiChat.clearTarget')"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>
          </button>
        </div>

        <div v-if="selectedLogFiles.length" class="rw-file-strip">
          <span class="rw-file-count">
            {{ t('aiChat.logAttachmentsSelected', { count: selectedLogFiles.length }) }}
          </span>
          <div
            v-for="file in selectedLogFiles"
            :key="`${file.name}:${file.size}:${file.lastModified}`"
            class="rw-file-chip rw-file-chip--above"
            :title="file.name"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m21 11.5-9.5 9.5a5 5 0 0 1-7-7l9-9a3.5 3.5 0 0 1 5 5L9.5 18.5a2 2 0 0 1-3-3L15 7"/></svg>
            <span>{{ file.name }}</span>
            <button
              type="button"
              :aria-label="t('aiChat.removeNamedAttachment', { name: file.name })"
              @click="clearSelectedLogFile(file)"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>
            </button>
          </div>
        </div>

        <div v-if="selectedPackageFiles.length" class="rw-file-strip">
          <span class="rw-file-count">
            {{ t('aiChat.packageAttachmentsSelected', { count: selectedPackageFiles.length }) }}
          </span>
          <div
            v-for="file in selectedPackageFiles"
            :key="`package:${file.name}:${file.size}:${file.lastModified}`"
            class="rw-file-chip rw-file-chip--above"
            :title="file.name"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z"/><path d="M3 7.5 12 12l9-4.5M12 12v9"/></svg>
            <span>{{ file.name }}</span>
            <button
              type="button"
              :aria-label="t('aiChat.removeNamedAttachment', { name: file.name })"
              @click="clearSelectedPackageFile(file)"
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>
            </button>
          </div>
        </div>

        <div v-if="pendingImages.length" class="rw-image-strip">
          <div v-for="img in pendingImages" :key="img.id" class="rw-image-thumb" :title="img.name">
            <img :src="img.dataUrl" :alt="img.name" />
            <button
              type="button"
              class="rw-image-thumb-remove"
              :aria-label="t('aiChat.image.remove')"
              @click="removePendingImage(img.id)"
            >
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>
            </button>
          </div>
        </div>

        <textarea
          v-model="inputMessage"
          ref="textareaRef"
          class="rw-textarea"
          :placeholder="isLoggedIn ? t('aiChat.inputPlaceholder') : t('aiChat.inputLoginPlaceholder')"
          rows="2"
          :readonly="!isLoggedIn"
          @keydown="handleKeydown"
          @compositionstart="handleCompositionStart"
          @compositionend="handleCompositionEnd"
          @blur="handleCompositionEnd"
          @paste="handleComposerPaste"
          @click="!isLoggedIn && appStore.requestLoginModal('login')"
        ></textarea>

        <div class="rw-composer-row">
          <button
            class="rw-mini-btn"
            :disabled="isProjectExpertAgentSelected"
            :title="isProjectExpertAgentSelected ? t('aiChat.attachmentDisabled') : isPackageAgentSelected ? t('aiChat.attachPackageFiles') : t('aiChat.attachLog')"
            :aria-label="isPackageAgentSelected ? t('aiChat.attachPackageFiles') : t('aiChat.attachLog')"
            @click="triggerAttachmentPicker"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m21 11.5-9.5 9.5a5 5 0 0 1-7-7l9-9a3.5 3.5 0 0 1 5 5L9.5 18.5a2 2 0 0 1-3-3L15 7"/></svg>
          </button>
          <input
            ref="logFileInputRef"
            class="rw-file-input"
            type="file"
            :accept="acceptedLogArchiveTypes"
            multiple
            @change="handleLogFileChange"
          />
          <input
            ref="packageFileInputRef"
            class="rw-file-input"
            type="file"
            multiple
            @change="handlePackageFileChange"
          />
          <button
            class="rw-mini-btn"
            :title="t('aiChat.image.attach')"
            :aria-label="t('aiChat.image.attach')"
            @click="triggerImagePicker"
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="m21 15-5-5L5 21"/></svg>
          </button>
          <input
            ref="imageFileInputRef"
            class="rw-file-input"
            type="file"
            accept="image/*"
            multiple
            @change="handleImageFileChange"
          />
          <div class="rw-device-wrap">
            <button
              ref="deviceMenuBtnRef"
              class="rw-tool-chip"
              :class="{ active: isDeviceSelected || deviceMenuVisible }"
              type="button"
              aria-haspopup="listbox"
              :aria-expanded="deviceMenuVisible"
              @click="toggleDeviceMenu"
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="18" height="11" rx="1.5"/><path d="M8 21h8M12 17v4"/><circle cx="7" cy="11" r="0.4" fill="currentColor"/></svg>
              {{ t('aiChat.agents.device') }}
              <svg class="rw-chip-caret" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>
            </button>
            <div
              v-if="deviceMenuVisible"
              ref="deviceMenuRef"
              class="rw-device-menu"
              role="listbox"
            >
              <div class="rw-device-search">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
                <input
                  v-model="deviceKeyword"
                  type="text"
                  :placeholder="t('aiChat.deviceSearchPlaceholder')"
                  @keydown.stop
                />
              </div>
              <div v-if="isLoadingDevices" class="rw-device-empty">{{ t('aiChat.deviceLoading') }}</div>
              <div v-else-if="!filteredDeviceOptions.length" class="rw-device-empty">{{ t('aiChat.deviceNoMatch') }}</div>
              <template v-else>
                <button
                  v-for="device in filteredDeviceOptions"
                  :key="device.id"
                  type="button"
                  class="rw-device-row"
                  :class="{ active: device.id === targetDeviceId }"
                  role="option"
                  :aria-selected="device.id === targetDeviceId"
                  @click="selectDevice(device)"
                >
                  <span class="rw-status-dot" :class="device.status === 'online' ? 'online' : 'offline'"></span>
                  <div class="rw-device-meta">
                    <div class="rw-device-title">
                      {{ device.name || device.id }}
                      <span class="rw-device-tag">{{ device.status === 'online' ? t('aiChat.status.online') : t('aiChat.status.offline') }}</span>
                    </div>
                    <div class="rw-device-sub">ID: {{ device.id }}</div>
                  </div>
                </button>
              </template>
            </div>
          </div>
          <button
            class="rw-tool-chip"
            :class="{ active: isPackageAgentSelected }"
            @click="togglePackageAgent"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z"/><path d="M3 7.5 12 12l9-4.5M12 12v9"/></svg>
            {{ t('aiChat.capabilities.package.label') }}
          </button>
          <button
            class="rw-tool-chip"
            :class="{ active: isLogAnalysisAgentSelected }"
            @click="toggleLogAnalysisAgent"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h10M4 18h16"/><circle cx="18" cy="12" r="1.4"/></svg>
            {{ t('aiChat.agents.logAnalysis') }}
          </button>
          <button
            class="rw-tool-chip"
            :class="{ active: isProjectExpertAgentSelected }"
            @click="toggleProjectExpertAgent"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5V6.75A2.75 2.75 0 0 1 6.75 4H20v13H6.75A2.75 2.75 0 0 0 4 19.5Z"/><path d="M8 8h8M8 12h6"/></svg>
            {{ t('aiChat.agents.projectExpert') }}
          </button>
          <ProjectRepoSelect
            v-if="isProjectRepoSelectVisible"
            v-model="selectedProjectRepoId"
            :options="visibleProjectRepoOptions"
            :loading="projectRepoOptionsLoading"
            :required="isProjectRepoRequired"
          />
          <button
            class="rw-send-btn"
            :class="{ 'is-stop': canCancelCurrentRun }"
            :disabled="canCancelCurrentRun ? cancelInFlight : sendDisabled"
            :title="canCancelCurrentRun ? (cancelInFlight ? t('aiChat.canceling') : t('aiChat.cancelTask')) : ''"
            @click="canCancelCurrentRun ? cancelCurrentRun() : sendMessage()"
          >
            <svg v-if="canCancelCurrentRun" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="6.5" y="6.5" width="11" height="11" rx="2" fill="currentColor" stroke="none"/></svg>
            <svg v-else width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12 19 5l-3 15-5-7-6-1Z"/></svg>
          </button>
        </div>
      </div>
    </div>

    <!-- Share conversation modal -->
    <ShareConversationModal
      :visible="showShareModal"
      :session-id="sessionStore.selectedSessionId"
      :message-count="sessionMessageCount"
      @close="showShareModal = false"
    />

    <!-- Rename modal -->
    <div v-if="showRenameModal" class="rw-rename-backdrop" @click.self="showRenameModal = false">
      <div class="rw-modal rw-rename-modal">
        <div class="rw-modal-head">
          <div>
            <h3 class="rw-modal-title">{{ t('aiChat.rename.title') }}</h3>
            <p class="rw-modal-sub">{{ t('aiChat.rename.subtitle') }}</p>
          </div>
          <button class="rw-modal-close" @click="showRenameModal = false" :aria-label="t('common.close')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 6l12 12M18 6 6 18"/></svg>
          </button>
        </div>
        <div class="rw-rename-modal-body">
          <label class="rw-form-field">
            <span class="rw-form-label">{{ t('aiChat.rename.nameLabel') }}</span>
            <input
              ref="renameModalInputRef"
              v-model="renameModalTitle"
              type="text"
              class="rw-input"
              :placeholder="t('aiChat.rename.placeholder')"
              maxlength="80"
              @keydown.enter.prevent="commitRenameFromModal"
              @keydown.esc.prevent="showRenameModal = false"
            />
          </label>
        </div>
        <div class="rw-modal-actions">
          <button
            type="button"
            class="rw-btn-primary"
            :disabled="renameModalInFlight || !renameModalTitle.trim()"
            @click="commitRenameFromModal"
          >{{ renameModalInFlight ? t('aiChat.rename.saving') : t('common.save') }}</button>
          <button type="button" class="rw-btn-ghost" @click="showRenameModal = false">{{ t('common.cancel') }}</button>
        </div>
      </div>
    </div>

    <!-- DeviceAgent HITL: tool permission modal -->
    <div v-if="currentPermission" class="rw-modal-backdrop rw-hitl-backdrop">
      <div class="rw-modal rw-hitl-modal">
        <div class="rw-modal-head">
          <div>
            <h3 class="rw-modal-title">{{ t('aiChat.permission.title') }}</h3>
            <p class="rw-modal-sub">
              <span class="rw-hitl-risk" :class="`risk-${currentPermission.risk}`">
                {{ currentPermission.risk === 'destructive' ? t('aiChat.permission.risk.destructive') : currentPermission.risk === 'write' ? t('aiChat.permission.risk.write') : t('aiChat.permission.risk.read') }}
              </span>
              <span class="rw-hitl-tool mono">{{ currentPermission.tool_name }}</span>
            </p>
          </div>
        </div>
        <div class="rw-hitl-body">
          <div v-if="currentPermission.rationale" class="rw-hitl-rationale">
            {{ currentPermission.rationale }}
          </div>
          <label class="rw-form-field">
            <span class="rw-form-label">{{ t('aiChat.permission.argsLabel') }}</span>
            <textarea
              v-model="currentPermission.editingArgs"
              class="rw-input rw-hitl-args mono"
              rows="8"
              spellcheck="false"
            ></textarea>
          </label>
          <div v-if="currentPermission.editingError" class="rw-hitl-error">
            {{ currentPermission.editingError }}
          </div>
          <p class="rw-hitl-warn">
            {{ t('aiChat.permission.warning') }}
          </p>
        </div>
        <div class="rw-modal-actions rw-hitl-actions">
          <button
            type="button"
            class="rw-btn-ghost"
            :disabled="permissionDecisionInFlight"
            @click="submitPermissionDecision('deny')"
          >{{ t('aiChat.permission.deny') }}</button>
          <button
            type="button"
            class="rw-btn-ghost"
            :disabled="permissionDecisionInFlight"
            @click="submitPermissionDecision('allow', { useEdited: true })"
          >{{ t('aiChat.permission.allowEdited') }}</button>
          <button
            type="button"
            class="rw-btn-primary"
            :disabled="permissionDecisionInFlight"
            @click="submitPermissionDecision('allow')"
          >{{ t('aiChat.permission.allow') }}</button>
        </div>
      </div>
    </div>

    <!-- AskUserQuestion clarification modal -->
    <div v-if="currentClarification && !currentClarification.paused && !currentClarification.publication_review" class="rw-modal-backdrop rw-hitl-backdrop">
      <div class="rw-modal rw-hitl-modal rw-clarification-modal">
        <ClarificationCard
          :pending="currentClarification"
          @submit="submitClarificationAnswers"
        />
      </div>
    </div>

    <!-- 附带图片放大查看 -->
    <div
      v-if="viewerImage"
      class="rw-image-viewer"
      role="dialog"
      :aria-label="t('aiChat.image.preview', { name: viewerImage.name })"
      @click="closeImageViewer"
      @keydown.esc="closeImageViewer"
    >
      <img :src="viewerImage.url" :alt="viewerImage.name" @click.stop />
      <button
        type="button"
        class="rw-image-viewer-close"
        :aria-label="t('common.close')"
        @click="closeImageViewer"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>
      </button>
    </div>

    <!-- Mermaid 图表放大查看 -->
    <el-dialog
      v-model="mermaidDialogVisible"
      width="80%"
      top="6vh"
      append-to-body
      class="mermaid-zoom-dialog"
    >
      <template #header>
        <div class="mermaid-zoom-header">
          <span class="mermaid-zoom-title">{{ t('aiChat.mermaid.title') }}</span>
          <div class="mermaid-zoom-toolbar">
            <button class="mz-btn" type="button" :title="t('aiChat.mermaid.zoomOut')" @click="zoomOutMermaid">−</button>
            <span class="mz-scale">{{ Math.round(mermaidZoomScale * 100) }}%</span>
            <button class="mz-btn" type="button" :title="t('aiChat.mermaid.zoomIn')" @click="zoomInMermaid">＋</button>
            <button class="mz-btn" type="button" :title="t('aiChat.mermaid.resetZoom')" @click="resetMermaidZoom">{{ t('aiChat.mermaid.reset') }}</button>
            <span class="mz-divider"></span>
            <button class="mz-btn" type="button" :title="t('aiChat.mermaid.copyImage')" @click="copyMermaidImage">{{ t('aiChat.mermaid.copyImage') }}</button>
            <button class="mz-btn mz-btn-primary" type="button" :title="t('aiChat.mermaid.downloadImage')" @click="downloadMermaidImage">{{ t('aiChat.mermaid.downloadImage') }}</button>
          </div>
        </div>
      </template>
      <div
        ref="mermaidDialogContainerRef"
        class="mermaid-zoom-body"
        :class="{ 'is-pannable': mermaidZoomScale > 1, 'is-panning': mermaidIsPanning }"
        @wheel="onMermaidZoomWheel"
        @mousedown="onMermaidPanStart"
      ></div>
    </el-dialog>
  </div>
</template>

<style scoped>
.rw-chat-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  background: var(--rw-canvas, #ffffff);
  color: var(--rw-ink, #171717);
}

.mono { font-family: var(--rw-mono); font-weight: 500; }
.spin { animation: rw-spin 1s linear infinite; }
@keyframes rw-spin { from { transform: rotate(0); } to { transform: rotate(360deg); } }

/* Topbar */
.rw-topbar {
  height: 56px; flex-shrink: 0;
  border-bottom: 1px solid var(--rw-hairline);
  display: flex; align-items: center;
  padding: 0 28px; gap: 14px;
  background: var(--rw-canvas);
}
.rw-topbar-left { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
.rw-crumb { font-size: 14.5px; font-weight: 600; color: var(--rw-ink); letter-spacing: -0.1px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rw-crumb-meta { font-size: 12px; color: var(--rw-muted); font-family: var(--rw-mono); flex-shrink: 0; }
.rw-topbar-right { display: flex; align-items: center; gap: 8px; }
.rw-model-pill {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--rw-surface-strong); color: var(--rw-ink);
  height: 28px; padding: 0 10px; border-radius: 999px;
  font-size: 12px; font-weight: 500;
}
.rw-model-pill:hover { background: var(--rw-hairline-strong); }
.rw-model-dot { width: 6px; height: 6px; border-radius: 999px; background: var(--rw-success); }
.rw-top-action {
  height: 30px; padding: 0 8px;
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--rw-hairline-strong);
  background: var(--rw-canvas);
  border-radius: 8px;
  font-size: 13px; font-weight: 500; color: var(--rw-ink);
}
.rw-top-action:hover { background: var(--rw-surface-strong); }
.rw-top-more-wrap { position: relative; }
.rw-top-menu {
  position: absolute; top: calc(100% + 6px); right: 0;
  width: 240px; background: var(--rw-canvas);
  border: 1px solid var(--rw-hairline-strong); border-radius: 10px;
  padding: 4px;
  box-shadow: 0 12px 32px rgba(0,0,0,.12), 0 2px 6px rgba(0,0,0,.04);
  z-index: 30;
}
.rw-top-menu-group {
  padding: 6px 10px 2px;
  font-size: 10.5px; font-weight: 600; letter-spacing: 0.6px;
  text-transform: uppercase; color: var(--rw-muted);
}
.rw-menu-item {
  display: flex; align-items: center; gap: 9px;
  width: 100%; height: 30px; padding: 0 10px;
  border-radius: 5px; font-size: 12.5px; font-weight: 500;
  color: var(--rw-ink); cursor: pointer; background: none; border: none;
  text-align: left;
}
.rw-menu-item:hover { background: var(--rw-surface-strong); }
.rw-menu-item.is-danger { color: var(--rw-danger); }
.rw-menu-item.is-danger:hover { background: rgba(192,56,43,.06); }
.rw-menu-divider { height: 1px; background: var(--rw-hairline); margin: 4px 6px; }
.rw-kbd-right { margin-left: auto; font-family: var(--rw-mono); font-size: 11px; color: var(--rw-muted); }

/* Scroll area */
.rw-scroll { flex: 1; min-height: 0; overflow: auto; padding: 32px 0 24px; }

/* Welcome */
.rw-welcome {
  height: 100%; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 22px;
  padding: 0 32px; text-align: center;
}
.rw-welcome-badge {
  display: inline-flex; align-items: center; gap: 8px;
  background: var(--rw-surface-strong);
  padding: 5px 12px; border-radius: 999px;
  font-size: 11px; font-weight: 600;
  letter-spacing: 0.8px; text-transform: uppercase;
  color: var(--rw-ink);
}
.rw-dot-success { width: 6px; height: 6px; border-radius: 999px; background: var(--rw-success); }
.rw-welcome-title {
  font-size: 40px; font-weight: 600; letter-spacing: -1.2px;
  line-height: 1.1; color: var(--rw-ink); max-width: 680px;
  margin: 0;
}
.rw-welcome-sub {
  font-size: 15.5px; color: var(--rw-body);
  max-width: 540px; line-height: 1.55; font-weight: 400;
}
.rw-cap-grid {
  margin-top: 8px;
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px; width: 100%; max-width: 920px;
}
.rw-cap-card {
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 12px;
  padding: 14px 16px;
  background: var(--rw-canvas);
  text-align: left;
  display: flex; flex-direction: column; gap: 6px;
  cursor: pointer; transition: all .15s;
}
.rw-cap-card:hover {
  border-color: var(--rw-ink);
  box-shadow: 0 4px 12px rgba(0,0,0,.04);
}
.rw-cap-label { font-size: 13px; font-weight: 600; color: var(--rw-ink); display: inline-flex; align-items: center; gap: 8px; }
.rw-cap-desc { font-size: 12.5px; color: var(--rw-body); line-height: 1.5; }

/* Loading */
.rw-loading { text-align: center; color: var(--rw-muted); font-size: 13px; padding-top: 48px; }

/* Thread */
.rw-thread {
  max-width: 820px; margin: 0 auto;
  padding: 0 32px;
  display: flex; flex-direction: column; gap: 28px;
}
.rw-msg.is-user { display: flex; flex-direction: column; align-items: flex-end; }

/* Thumbnails for images attached to a user turn (send + history reload). */
.rw-user-images {
  display: flex; flex-wrap: wrap; justify-content: flex-end;
  gap: 6px; margin-bottom: 6px; max-width: 100%;
}
.rw-user-image {
  width: 96px; height: 96px; padding: 0; cursor: zoom-in;
  border-radius: 10px; overflow: hidden;
  border: 1px solid var(--rw-hairline-strong); background: var(--rw-surface-strong);
}
.rw-user-image img { width: 100%; height: 100%; object-fit: cover; display: block; }
.rw-user-image:hover { border-color: var(--rw-ink); }

.rw-image-viewer {
  position: fixed; inset: 0; z-index: 3000;
  display: grid; place-items: center; padding: 40px;
  background: rgba(0, 0, 0, 0.72); cursor: zoom-out;
}
.rw-image-viewer img {
  max-width: 100%; max-height: 100%;
  object-fit: contain; border-radius: 8px; cursor: default;
}
.rw-image-viewer-close {
  position: absolute; top: 16px; right: 16px;
  width: 34px; height: 34px; border-radius: 999px;
  display: grid; place-items: center; cursor: pointer;
  color: #fff; background: rgba(255, 255, 255, 0.16); border: none;
}
.rw-image-viewer-close:hover { background: rgba(255, 255, 255, 0.28); }
.rw-user-bubble {
  background: var(--rw-surface-strong); color: var(--rw-ink);
  padding: 12px 16px; border-radius: 12px;
  font-size: 14.5px; line-height: 1.55;
  max-width: 85%; white-space: pre-wrap;
  word-break: break-word;
}
.rw-user-meta-line { font-size: 11px; color: var(--rw-muted); margin-top: 6px; font-weight: 500; }
.rw-ai-body { min-width: 0; }
.rw-ai-trace { margin-bottom: 12px; }
.rw-ocr-degraded {
  display: inline-flex; align-items: center; gap: 6px;
  margin-bottom: 10px; padding: 5px 10px;
  border-radius: 8px; font-size: 12.5px; line-height: 1.4;
  color: var(--rw-warn, #92400e);
  background: color-mix(in srgb, var(--rw-warn, #f59e0b) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--rw-warn, #f59e0b) 30%, transparent);
}
.rw-ocr-degraded svg { flex-shrink: 0; }
.rw-ai-text { font-size: 14.5px; color: var(--rw-ink); line-height: 1.62; }
.rw-ai-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 34px;
  margin-top: 14px;
  padding-top: 8px;
  border-top: 1px solid var(--rw-hairline);
}
.rw-ai-action-group {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
.rw-ai-action-btn {
  position: relative;
  width: 30px;
  height: 30px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: #6f737a;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background .15s, color .15s;
}
.rw-ai-action-btn:hover,
.rw-ai-action-btn:focus-visible {
  background: var(--rw-surface-strong);
  color: var(--rw-ink);
}
.rw-ai-action-btn:focus-visible { outline: none; }
.rw-ai-action-btn::after {
  content: attr(data-tooltip);
  position: absolute;
  left: 50%;
  bottom: calc(100% + 8px);
  transform: translateX(-50%) translateY(2px);
  padding: 6px 9px;
  border-radius: 7px;
  background: #111;
  color: #fff;
  font-size: 12px;
  line-height: 1.2;
  font-weight: 500;
  white-space: nowrap;
  box-shadow: 0 8px 18px rgba(0,0,0,.18);
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  transition: opacity .12s, transform .12s, visibility .12s;
  z-index: 20;
}
.rw-ai-action-btn::before {
  content: '';
  position: absolute;
  left: 50%;
  bottom: calc(100% + 3px);
  transform: translateX(-50%) translateY(2px);
  border: 5px solid transparent;
  border-top-color: #111;
  opacity: 0;
  visibility: hidden;
  pointer-events: none;
  transition: opacity .12s, transform .12s, visibility .12s;
  z-index: 20;
}
.rw-ai-action-btn:hover::after,
.rw-ai-action-btn:hover::before,
.rw-ai-action-btn:focus-visible::after,
.rw-ai-action-btn:focus-visible::before {
  opacity: 1;
  visibility: visible;
  transform: translateX(-50%) translateY(0);
}
.rw-ai-run-meta {
  display: inline-flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 4px 8px;
  min-width: 0;
  color: var(--rw-muted);
  font-family: var(--rw-mono);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
  line-height: 1.3;
  letter-spacing: .01em;
}
.rw-ai-run-model {
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rw-ai-run-separator {
  width: 3px;
  height: 3px;
  flex: 0 0 3px;
  border-radius: 50%;
  background: var(--rw-hairline-strong);
}
@media (max-width: 560px) {
  .rw-ai-actions {
    align-items: flex-start;
    flex-direction: column-reverse;
    gap: 4px;
  }
  .rw-ai-run-meta {
    justify-content: flex-start;
    width: 100%;
  }
  .rw-ai-run-model {
    max-width: min(70vw, 280px);
  }
}
.rw-thinking {
  display: inline-block;
  background: linear-gradient(90deg, #9ca3af 0%, #e5e7eb 50%, #9ca3af 100%);
  background-size: 200% 100%;
  -webkit-background-clip: text; background-clip: text;
  color: transparent; font-weight: 600;
  animation: rw-shimmer 1.6s ease-in-out infinite;
}
@keyframes rw-shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

/* Markdown content adjustments */
.rw-ai-text :deep(p) { margin: 0 0 10px; }
.rw-ai-text :deep(p:last-child) { margin-bottom: 0; }
.rw-ai-text :deep(code) {
  font-family: var(--rw-mono); font-size: 12.5px;
  background: var(--rw-surface-strong); color: var(--rw-ink);
  padding: 1px 6px; border-radius: 4px;
}
.rw-ai-text :deep(pre) {
  background: var(--rw-surface-dark); color: #fff;
  padding: 14px; border-radius: 8px;
  font-family: var(--rw-mono); font-size: 12.5px; line-height: 1.55;
  overflow: auto; margin: 10px 0;
}
.rw-ai-text :deep(pre code) {
  background: transparent; color: inherit; padding: 0; border-radius: 0;
  font-size: inherit;
}
.rw-ai-text :deep(h1),
.rw-ai-text :deep(h2),
.rw-ai-text :deep(h3) {
  color: var(--rw-ink); font-weight: 600; letter-spacing: -0.2px;
  margin: 16px 0 8px;
}
.rw-ai-text :deep(h1) { font-size: 18px; }
.rw-ai-text :deep(h2) { font-size: 16px; }
.rw-ai-text :deep(h3) { font-size: 14.5px; }
.rw-ai-text :deep(ul),
.rw-ai-text :deep(ol) { padding-left: 22px; margin: 8px 0; }
.rw-ai-text :deep(li) { margin: 4px 0; }
.rw-ai-text :deep(a) { color: var(--rw-link); text-decoration: none; }
.rw-ai-text :deep(a:hover) { text-decoration: underline; }

/* Composer */
.rw-clarification-inline { margin-bottom: 12px; }
.rw-composer-wrap { flex-shrink: 0; padding: 12px 32px 24px; background: var(--rw-canvas); }
.rw-publication-inline { max-width: 820px; margin: 0 auto 10px; }
.rw-composer {
  max-width: 820px; margin: 0 auto;
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 14px;
  background: var(--rw-canvas);
  box-shadow: 0 1px 2px rgba(0,0,0,.02), 0 6px 22px rgba(0,0,0,.05);
  padding: 12px 14px 10px;
  display: flex; flex-direction: column; gap: 8px;
  position: relative;
}
.rw-composer:focus-within { border-color: var(--rw-ink); }
.rw-composer.is-log-drag-over {
  border-color: var(--rw-ink);
  background: var(--rw-surface-strong);
  box-shadow: 0 0 0 3px rgba(23, 23, 23, .08), 0 8px 24px rgba(0,0,0,.08);
}
.rw-textarea {
  width: 100%; min-height: 50px;
  border: none; outline: none; resize: none;
  font-size: 14.5px; line-height: 1.55;
  font-family: var(--rw-sans);
  background: transparent; color: var(--rw-ink);
}
.rw-textarea::placeholder { color: var(--rw-muted); }

.rw-composer-row { display: flex; align-items: center; gap: 6px; }
.rw-mini-btn {
  width: 30px; height: 30px; border-radius: 6px;
  display: grid; place-items: center;
  color: var(--rw-body); transition: background .12s, color .12s;
  background: none; border: none; cursor: pointer;
}
.rw-mini-btn:hover { background: var(--rw-surface-strong); color: var(--rw-ink); }
.rw-mini-btn:disabled { opacity: 0.35; cursor: not-allowed; }
.rw-mini-btn:disabled:hover { background: none; color: var(--rw-body); }
.rw-file-input { display: none; }
.rw-tool-chip {
  height: 28px; padding: 0 11px;
  display: inline-flex; align-items: center; gap: 6px;
  border: 1px solid var(--rw-hairline-strong);
  background: var(--rw-canvas); color: var(--rw-ink);
  border-radius: 999px; font-size: 12.5px; font-weight: 500;
  cursor: pointer; transition: all .15s;
}
.rw-tool-chip:hover { border-color: var(--rw-ink); }
.rw-tool-chip.active { background: var(--rw-ink); color: var(--rw-on-ink); border-color: var(--rw-ink); }
.rw-file-strip {
  width: 100%; display: flex; flex-wrap: wrap; align-items: center; gap: 6px;
  padding: 2px 0 1px;
}
.rw-file-count {
  flex: none; color: var(--rw-muted); font-size: 11px; font-weight: 650;
  letter-spacing: .02em;
}
.rw-file-chip {
  min-width: 0; max-width: 260px; height: 28px; padding: 0 8px 0 10px;
  display: inline-flex; align-items: center; gap: 7px;
  border-radius: 999px; background: var(--rw-surface-strong);
  color: var(--rw-ink); font-size: 12px; font-weight: 500;
}
.rw-file-chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rw-file-chip button {
  width: 18px; height: 18px; border-radius: 999px;
  display: grid; place-items: center; flex-shrink: 0;
  color: var(--rw-muted); background: none; border: none; cursor: pointer;
}
.rw-file-chip button:hover { background: var(--rw-hairline-strong); color: var(--rw-ink); }
.rw-file-chip--above {
  max-width: 100%; align-self: flex-start;
  border: 1px solid var(--rw-hairline-strong);
  background: var(--rw-canvas);
  padding: 0 8px 0 8px;
}

/* Pending image attachment thumbnails (paste / pick / drop). */
.rw-image-strip {
  display: flex; flex-wrap: wrap; gap: 8px;
  align-self: flex-start; max-width: 100%;
  margin-bottom: 2px;
}
.rw-image-thumb {
  position: relative; width: 56px; height: 56px;
  border-radius: 8px; overflow: hidden;
  border: 1px solid var(--rw-hairline-strong); background: var(--rw-surface-strong);
}
.rw-image-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.rw-image-thumb-remove {
  position: absolute; top: 2px; right: 2px;
  width: 16px; height: 16px; border-radius: 999px;
  display: grid; place-items: center;
  color: #fff; background: rgba(0, 0, 0, 0.55); border: none; cursor: pointer;
}
.rw-image-thumb-remove:hover { background: rgba(0, 0, 0, 0.75); }

.rw-send-btn {
  width: 36px; height: 32px; border-radius: 8px;
  background: var(--rw-primary); color: var(--rw-on-primary);
  display: inline-flex; align-items: center; justify-content: center;
  margin-left: auto;
  transition: background .15s;
  border: none; cursor: pointer;
}
.rw-send-btn:hover:not(:disabled) { background: var(--rw-primary-active); }
.rw-send-btn:disabled {
  background: var(--rw-surface-strong); color: var(--rw-muted);
  cursor: not-allowed;
}
.rw-send-btn.is-stop { background: var(--rw-ink, #171717); color: var(--rw-canvas, #fff); }
.rw-send-btn.is-stop:hover:not(:disabled) { background: var(--rw-danger, #b91c1c); }

.rw-composer-alert {
  display: flex; align-items: flex-start; gap: 8px;
  max-width: 820px; margin: 0 auto 8px;
  padding: 9px 12px; border-radius: 8px;
  font-size: 12.5px; line-height: 1.55;
}
.rw-composer-alert.is-warn {
  background: #fffbeb; color: #92400e;
  border: 1px solid #fcd34d;
}
.rw-composer-alert.is-error {
  background: #fef2f2; color: #991b1b;
  border: 1px solid #fca5a5;
}
.rw-composer-alert.is-suggest {
  background: #eff6ff; color: #1e40af;
  border: 1px solid #93c5fd;
}
.rw-composer-alert.is-suggest .rw-alert-link {
  font-weight: 600; margin-left: 2px;
}
.rw-composer-alert.is-login {
  background: #f5f3ff; color: #4c1d95;
  border: 1px solid #c4b5fd;
}
.rw-composer-alert.is-login .rw-alert-link {
  font-weight: 600;
}
html.dark .rw-composer-alert.is-warn {
  background: rgba(146, 64, 14, 0.18); color: #fcd34d;
  border-color: rgba(252, 211, 77, 0.35);
}
html.dark .rw-composer-alert.is-error {
  background: rgba(153, 27, 27, 0.2); color: #fca5a5;
  border-color: rgba(252, 165, 165, 0.35);
}
html.dark .rw-composer-alert.is-suggest {
  background: rgba(30, 64, 175, 0.2); color: #93c5fd;
  border-color: rgba(147, 197, 253, 0.3);
}
html.dark .rw-composer-alert.is-login {
  background: rgba(76, 29, 149, 0.22); color: #c4b5fd;
  border-color: rgba(196, 181, 253, 0.3);
}
html.dark .rw-composer-alert code {
  background: rgba(255, 255, 255, 0.08);
}
.rw-composer-alert code {
  font-family: var(--rw-mono); font-size: 11.5px;
  background: rgba(0,0,0,.06); border-radius: 3px; padding: 1px 4px;
}
.rw-alert-link {
  background: none; border: none; padding: 0; cursor: pointer;
  color: inherit; text-decoration: underline; font-size: inherit;
  font-family: inherit;
}

/* Device operation dropdown */
.rw-device-wrap { position: relative; display: inline-flex; }
.rw-chip-caret { margin-left: 1px; opacity: .65; }
.rw-device-menu {
  position: absolute; left: 0;
  bottom: calc(100% + 8px);
  width: 280px; max-height: 300px; overflow: auto;
  background: var(--rw-canvas);
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 12px;
  box-shadow: 0 12px 32px rgba(0,0,0,.12), 0 2px 6px rgba(0,0,0,.04);
  z-index: 30;
}
.rw-device-search {
  display: flex; align-items: center; gap: 8px;
  padding: 9px 12px; border-bottom: 1px solid var(--rw-hairline);
  color: var(--rw-muted);
  position: sticky; top: 0; background: var(--rw-canvas); z-index: 1;
}
.rw-device-search input {
  flex: 1; min-width: 0; border: none; outline: none; background: transparent;
  font-size: 12.5px; color: var(--rw-ink); font-family: var(--rw-sans);
}
.rw-device-search input::placeholder { color: var(--rw-muted); }
.rw-device-empty { padding: 12px 14px; font-size: 12.5px; color: var(--rw-muted); }
.rw-device-row {
  display: flex; align-items: center; gap: 10px;
  width: 100%; padding: 10px 14px;
  text-align: left;
  border-bottom: 1px solid var(--rw-hairline-soft);
  background: none; border-left: none; border-right: none; border-top: none;
  cursor: pointer;
}
.rw-device-row:last-child { border-bottom: none; }
.rw-device-row:hover { background: var(--rw-hairline-soft); }
.rw-device-row.active { background: var(--rw-surface-strong); }
.rw-status-dot { width: 7px; height: 7px; border-radius: 999px; flex-shrink: 0; }
.rw-status-dot.online { background: var(--rw-success); }
.rw-status-dot.offline { background: var(--rw-muted-soft); }
.rw-device-meta { flex: 1; min-width: 0; }
.rw-device-title {
  font-size: 13.5px; font-weight: 600; color: var(--rw-ink);
  display: flex; align-items: center; gap: 6px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.rw-device-tag {
  font-family: var(--rw-mono); font-size: 10.5px;
  text-transform: uppercase; color: var(--rw-muted);
  font-weight: 500;
}
.rw-device-sub { font-size: 12px; color: var(--rw-muted); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* Target chip */
.rw-target-chip {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; border-radius: 999px;
  background: var(--rw-surface-strong);
  font-size: 12.5px; align-self: flex-start;
}
.rw-target-label { color: var(--rw-muted); font-family: var(--rw-mono); font-size: 11.5px; }
.rw-target-value {
  display: inline-flex; align-items: center; gap: 6px;
  font-weight: 600; color: var(--rw-ink);
}
.rw-target-clear {
  width: 20px; height: 20px; border-radius: 999px;
  display: grid; place-items: center; color: var(--rw-body);
  background: none; border: none; cursor: pointer;
}
.rw-target-clear:hover { background: var(--rw-hairline-strong); }

/* Scrollbar */
.rw-scroll::-webkit-scrollbar,
.rw-device-menu::-webkit-scrollbar { width: 10px; height: 10px; }
.rw-scroll::-webkit-scrollbar-track,
.rw-device-menu::-webkit-scrollbar-track { background: transparent; }
.rw-scroll::-webkit-scrollbar-thumb,
.rw-device-menu::-webkit-scrollbar-thumb {
  background: #e6e6ea; border-radius: 999px; border: 2px solid var(--rw-canvas);
}
.rw-scroll::-webkit-scrollbar-thumb:hover,
.rw-device-menu::-webkit-scrollbar-thumb:hover { background: var(--rw-muted-soft); }

/* DeviceAgent HITL modal */
.rw-hitl-backdrop {
  position: fixed; inset: 0;
  background: rgba(15, 23, 42, 0.42);
  display: flex; align-items: center; justify-content: center;
  z-index: 80;
}
.rw-hitl-modal {
  width: min(520px, 92vw);
  background: var(--rw-canvas, #fff);
  border-radius: 12px;
  padding: 20px 22px;
  box-shadow: 0 18px 40px rgba(15, 23, 42, 0.18);
  display: flex; flex-direction: column; gap: 14px;
}
.rw-hitl-modal.rw-clarification-modal {
  width: min(720px, 94vw);
  max-width: 720px;
  max-height: calc(100vh - 32px);
  padding: 0;
  overflow: hidden;
}
.rw-hitl-risk {
  display: inline-block; padding: 1px 8px; border-radius: 999px;
  font-size: 11px; font-weight: 600; margin-right: 8px;
}
.rw-hitl-risk.risk-read { background: #e0f2fe; color: #0369a1; }
.rw-hitl-risk.risk-write { background: #fef3c7; color: #92400e; }
.rw-hitl-risk.risk-destructive { background: #fee2e2; color: #b91c1c; }
.rw-hitl-tool { font-size: 13px; color: var(--rw-ink, #111827); }
.rw-hitl-body { display: flex; flex-direction: column; gap: 10px; }
.rw-hitl-rationale {
  font-size: 13px; color: var(--rw-muted, #6b7280);
  background: var(--rw-canvas-soft, #fafafa);
  padding: 8px 10px; border-radius: 8px;
}
.rw-hitl-args {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px; line-height: 1.5;
  min-height: 140px; resize: vertical;
}
.rw-hitl-error {
  color: #b91c1c; font-size: 12px;
  background: #fee2e2; padding: 6px 10px; border-radius: 6px;
}
.rw-hitl-warn { font-size: 12px; color: var(--rw-muted, #6b7280); margin: 0; }
.rw-hitl-actions { display: flex; gap: 8px; justify-content: flex-end; }

/* Rename modal */
.rw-rename-backdrop {
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
.rw-modal-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.rw-modal-title { font-size: 16px; font-weight: 600; color: var(--rw-ink); margin: 0; }
.rw-modal-sub { font-size: 12px; color: var(--rw-muted); margin: 4px 0 0; }
.rw-modal-close { width: 28px; height: 28px; border-radius: 6px; display: grid; place-items: center; color: var(--rw-body); }
.rw-modal-close:hover { background: var(--rw-surface-strong); color: var(--rw-ink); }
.rw-rename-modal-body { margin-top: 16px; }
.rw-modal-actions { display: flex; gap: 10px; padding-top: 16px; }
.rw-form-field { display: flex; flex-direction: column; gap: 6px; }
.rw-form-label { font-size: 12px; color: var(--rw-body); font-weight: 500; }
.rw-input {
  width: 100%; height: 38px;
  border: 1px solid var(--rw-hairline-strong); border-radius: 8px;
  padding: 0 12px; font-size: 13.5px; outline: none;
  background: var(--rw-canvas); color: var(--rw-ink);
  font-family: inherit;
}
.rw-input:focus { border-color: var(--rw-ink); }
.rw-btn-primary {
  height: 36px; padding: 0 16px;
  background: var(--rw-primary); color: var(--rw-on-primary);
  border-radius: 8px; font-size: 13.5px; font-weight: 500;
  border: none; cursor: pointer;
  transition: background .15s, color .15s;
}
.rw-btn-primary:hover:not(:disabled) { background: var(--rw-primary-hover); }
.rw-btn-primary:active:not(:disabled) { background: var(--rw-primary-active); }
.rw-btn-primary:disabled { opacity: .6; cursor: default; }
.rw-btn-ghost {
  height: 36px; padding: 0 14px;
  background: var(--rw-canvas); color: var(--rw-ink);
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 8px; font-size: 13.5px; font-weight: 500;
  cursor: pointer;
}
.rw-btn-ghost:hover { background: var(--rw-surface-strong); }

:global(.rw-log-workspace-replacement-box .el-message-box__message p) {
  white-space: pre-line;
  line-height: 1.65;
}

:global(.rw-log-workspace-replacement-box) {
  width: min(560px, calc(100vw - 32px));
  border: 1px solid color-mix(in srgb, #d97706 30%, var(--rw-hairline-strong, #cbd5e1));
  border-top: 3px solid #d97706;
  border-radius: 14px;
  box-shadow: 0 24px 70px rgba(30, 41, 59, .2);
}

:global(.rw-log-workspace-replacement-box .el-message-box__title) {
  color: var(--rw-ink);
  font-size: 17px;
  font-weight: 650;
  letter-spacing: -.01em;
}

:global(.rw-log-workspace-replacement-box .el-message-box__btns) {
  gap: 8px;
  padding-top: 8px;
}

/* Responsive */
@media (max-width: 900px) {
  .rw-thread, .rw-composer { padding-left: 16px; padding-right: 16px; }
  .rw-composer-wrap { padding-left: 16px; padding-right: 16px; }
  .rw-topbar { padding: 0 16px; }
  .rw-cap-grid { grid-template-columns: 1fr; max-width: 480px; }
  .rw-welcome-title { font-size: 32px; letter-spacing: -0.8px; }
}
</style>
