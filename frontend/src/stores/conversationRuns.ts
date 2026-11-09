import { defineStore } from 'pinia'
import { computed, reactive, ref } from 'vue'
import { API_BASE_URL } from '@/api'
import { userApi } from '@/api/user'
import {
  packageSearchStream,
  projectExpertStream,
  resolveChatPermission,
  resolveChatClarification,
  type ChatImageAttachment,
} from '@/api/chat'
import { i18n } from '@/i18n'
import { getActiveLocale, LOCALE_HEADER } from '@/i18n/runtime'
import type {
  AgentTraceEvent,
  ClarificationAnswer,
  ClarificationQuestion,
  PendingClarification,
} from '@/types/agentTrace'
import type { ChatMessageRecord } from '@/types'
import { hasPersistedLogAttachmentMarker } from '@/utils/logWorkspaceReplacement'
import { formatPackageAttachmentMessage } from '@/utils/packageAttachments'
import { appendChatImageFiles } from '@/utils/chatImageUploads'
import { projectStreamedAnswer } from '@/utils/streamedAnswer'

class ChatHttpError extends Error {}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ChatRole = 'user' | 'ai' | 'system'

/** OCR degradation surfaced for an image-bearing turn (see backend ocr_status event). */
export type OcrStatusInfo = {
  status: 'unconfigured' | 'failed' | string
  imageCount: number
  errorKind?: string | null
}

/**
 * One image shown in a user bubble.
 *
 * ``url`` is a local ``data:`` URL on the optimistic send path (instant, no
 * round-trip) and a blob object URL after a history reload — the chat-images
 * endpoint is Bearer-authenticated, so bytes cannot be fetched by putting the
 * endpoint straight into ``<img src>``.
 */
export type ChatEntryImage = {
  id?: string
  name: string
  mediaType: string
  url: string
}

export type ChatEntry = {
  id: string
  role: ChatRole
  content: string
  kind?: 'plan' | 'device_action' | 'answer' | 'user'
  traceEvents?: AgentTraceEvent[]
  traceRunning?: boolean
  /** Raw provider increments, kept separate from their readable answer projection. */
  streamedAnswer?: string
  /** Pace newly generated answers only; persisted history is shown immediately. */
  animateAnswer?: boolean
  /** Set when this turn attached images but OCR was unconfigured/failed. */
  ocrStatus?: OcrStatusInfo
  /** Images the user attached to this turn, rendered as thumbnails in the bubble. */
  images?: ChatEntryImage[]
  /** Successful OCR/vision description for images attached to this turn. */
  visualAnalysis?: {
    text: string
    imageCount: number
  }
}

export type PendingPermission = {
  request_id: string
  tool_name: string
  risk: 'read' | 'write' | 'destructive'
  rationale?: string
  tool_input?: Record<string, unknown>
  session_id?: string
  run_id?: string
  editingArgs: string
  editingError?: string | null
}

export type AgentKind = 'device' | 'log_analysis' | 'project_expert' | 'package_search'

export type RunStatus = 'idle' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'stale'

export const THINKING_PLACEHOLDER = '__RAVEN_AI_THINKING__'

const t = (key: string, params?: Record<string, unknown>) =>
  i18n.global.t(key, params || {})

export type ConversationState = {
  sessionId: string
  messages: ChatEntry[]
  loadingMessages: boolean
  isSending: boolean
  activeRunId: string | null
  runStatus: RunStatus
  runAgentKind: AgentKind | null
  /**
   * GeneralAgent 的结构化路由建议（device|log_analysis|package_search|
   * project_expert）。非空时表示用户最新请求更适合用对应专门 Agent，前端据此
   * 给出醒目提示与一键切换。每开始新一轮 run 重置为 null。
   */
  suggestedAgentType: string | null
  pendingPermissions: PendingPermission[]
  /** Pending AskUserQuestion clarifications awaiting the user's answer. */
  pendingClarifications: PendingClarification[]
  /** Map of `${runId}:${seq}` -> 1 so replayed frames are deduped. */
  seenSeq: Record<string, number>
  /** Stable assistant placeholder id for the current run: `run:<run_id>:assistant`. */
  currentAnswerId: string | null
  /** Abort-controller for the running SSE pump; aborting drops the reader, not the backend job. */
  subscription: AbortController | null
  /** Loaded once per session-id so loadSession + selectSessionToken don't both refetch. */
  loaded: boolean
  /** Last agent kind used in this conversation, derived from message records. */
  lastAgentKind: AgentKind | null
  /** Last project repo id selected for this conversation (frontend-only, not persisted to backend). */
  lastProjectRepoId: number | null
  /** The backend has successfully associated this session with a log workspace. */
  hasLogWorkspaceContext: boolean
}

export type StartDeviceRunPayload = {
  message: string
  history?: { role: string; content: string }[]
  target_device_id?: string | null
  target_device_name?: string | null
  remember?: boolean
  images?: ChatImageAttachment[]
  /** Local previews for the optimistic user bubble (data: URLs, no round-trip). */
  imagePreviews?: ChatEntryImage[]
}

export type StartLogAnalysisPayload = {
  message: string
  history?: { role: string; content: string }[]
  files?: File[]
  /** @deprecated Compatibility for callers that still submit one attachment. */
  file?: File | null
  project_repo_id?: number | null
  remember?: boolean
  images?: ChatImageAttachment[]
  /** Local previews for the optimistic user bubble (data: URLs, no round-trip). */
  imagePreviews?: ChatEntryImage[]
}

export type StartProjectExpertPayload = {
  message: string
  history?: { role: string; content: string }[]
  project_repo_id: number
  remember?: boolean
  images?: ChatImageAttachment[]
  /** Local previews for the optimistic user bubble (data: URLs, no round-trip). */
  imagePreviews?: ChatEntryImage[]
}

export type StartPackageSearchPayload = {
  message: string
  history?: { role: string; content: string }[]
  /** Optional only when ``files`` contains component inputs for packaging. */
  project_repo_id?: number | null
  files?: File[]
  remember?: boolean
  images?: ChatImageAttachment[]
  /** Local previews for the optimistic user bubble (data: URLs, no round-trip). */
  imagePreviews?: ChatEntryImage[]
}

const DEVICE_TRACE_TYPES = new Set([
  'run_start',
  'run_complete',
  'cancelled',
  'step_start',
  'step_delta',
  'step_end',
  'thinking_start',
  'thinking_delta',
  'thinking_end',
  'system_notice',
  'tool_permission_request',
  'tool_permission_resolved',
  'result_validation',
  'clarification_request',
  'clarification_resolved',
])

/** Coerce a raw `questions` payload into well-formed ClarificationQuestion[]. */
const normalizeClarificationQuestions = (raw: unknown): ClarificationQuestion[] => {
  if (!Array.isArray(raw)) return []
  const out: ClarificationQuestion[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const obj = item as Record<string, unknown>
    const question = String(obj.question || '').trim()
    if (!question) continue
    const optionsRaw = Array.isArray(obj.options) ? obj.options : []
    const options = optionsRaw
      .filter((o): o is Record<string, unknown> => !!o && typeof o === 'object')
      .map((o) => ({ label: String(o.label || '').trim(), description: String(o.description || '').trim() }))
      .filter((o) => o.label)
    out.push({
      header: String(obj.header || '').trim(),
      question,
      multiSelect: Boolean(obj.multiSelect),
      ...(typeof obj.question_key === 'string' ? { question_key: obj.question_key } : {}),
      ...(typeof obj.allow_custom === 'boolean' ? { allow_custom: obj.allow_custom } : {}),
      ...(typeof obj.required === 'boolean' ? { required: obj.required } : {}),
      options,
    })
  }
  return out
}

/** Build a PendingClarification with empty per-question draft state. */
const makePendingClarification = (
  requestId: string,
  questions: ClarificationQuestion[],
  meta: { session_id?: string; run_id?: string },
): PendingClarification => ({
  request_id: requestId,
  questions,
  session_id: meta.session_id,
  run_id: meta.run_id,
  draftSelected: questions.map(() => []),
  draftCustom: questions.map(() => ''),
  submitting: false,
  error: null,
})

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

const getServiceUrl = (path: string) => {
  if (API_BASE_URL && /^https?:\/\//i.test(API_BASE_URL)) {
    return `${API_BASE_URL.replace(/\/$/, '')}${path}`
  }
  if (typeof window !== 'undefined') {
    return `http://${window.location.hostname}:8085${path}`
  }
  return path
}

/**
 * Blob object URLs for history images, keyed by `${sessionId}/${imageId}`.
 *
 * The chat-images endpoint requires a Bearer token, so bytes are fetched with
 * `fetch` and wrapped in an object URL rather than pointing `<img src>` at the
 * endpoint. Cached for the page's lifetime: the same thumbnail is re-rendered
 * every time the user switches back to a session, and re-fetching each time
 * would be pure waste. URLs are revoked in `resetImageCache` (session delete).
 */
const historyImageUrls = new Map<string, string>()

const loadHistoryImage = async (
  sessionId: string,
  imageId: string,
  authToken?: string | null,
): Promise<string | null> => {
  const cacheKey = `${sessionId}/${imageId}`
  const cached = historyImageUrls.get(cacheKey)
  if (cached) return cached
  try {
    const resp = await fetch(
      getServiceUrl(
        `/api/v1/ai-chat/chat-images/${encodeURIComponent(sessionId)}/${encodeURIComponent(imageId)}`,
      ),
      {
        headers: authToken
          ? { [LOCALE_HEADER]: getActiveLocale(), Authorization: `Bearer ${authToken}` }
          : { [LOCALE_HEADER]: getActiveLocale() },
        credentials: 'include',
      },
    )
    if (!resp.ok) return null
    const url = URL.createObjectURL(await resp.blob())
    historyImageUrls.set(cacheKey, url)
    return url
  } catch (err) {
    console.debug('chat image load failed', err)
    return null
  }
}

/**
 * Attach thumbnails to already-rendered history messages.
 *
 * Runs detached from the initial paint: the message list is shown from text
 * alone, then each image is filled in as its bytes arrive. A message whose
 * images all fail to load simply keeps no `images` array, which renders the
 * same as an image-free turn.
 */
const hydrateHistoryImages = async (
  state: { messages: ChatEntry[] },
  records: ChatMessageRecord[],
  sessionId: string,
  authToken?: string | null,
) => {
  for (const record of records) {
    const metas = Array.isArray(record.images) ? record.images : []
    if (!metas.length) continue
    const resolved: ChatEntryImage[] = []
    for (const meta of metas) {
      const url = await loadHistoryImage(sessionId, meta.id, authToken)
      if (url) {
        resolved.push({ id: meta.id, name: meta.name, mediaType: meta.media_type, url })
      }
    }
    if (!resolved.length) continue
    const target = state.messages.find((m) => m.id === record.id)
    if (target) target.images = resolved
  }
}

/** Revoke and drop cached object URLs for a session (used when it is deleted). */
export const resetImageCache = (sessionId?: string) => {
  for (const [key, url] of historyImageUrls.entries()) {
    if (sessionId && !key.startsWith(`${sessionId}/`)) continue
    URL.revokeObjectURL(url)
    historyImageUrls.delete(key)
  }
}

const formatPlan = (steps: any[]) => {
  if (!Array.isArray(steps) || steps.length === 0) return t('aiChat.runs.noPlan')
  const lines: string[] = [`**${t('aiChat.runs.planSteps')}**`]
  steps.forEach((step, index) => {
    const id = step?.id || `S${index + 1}`
    const type = step?.type ? ` (${step.type})` : ''
    const goal = step?.goal || t('aiChat.runs.noDescription')
    lines.push(`- ${id}${type}: ${goal}`)
    if (Array.isArray(step?.success_criteria) && step.success_criteria.length) {
      lines.push(`  - ${t('aiChat.runs.validation')}: ${step.success_criteria.join('; ')}`)
    }
  })
  return lines.join('\n')
}

const formatDeviceAction = (payload: any) => {
  const order = typeof payload?.step_index === 'number' ? payload.step_index + 1 : null
  const label = payload?.step_id || (order ? t('aiChat.runs.stepLabel', { order }) : t('aiChat.runs.defaultDeviceActionLabel'))
  const goal = payload?.step_goal ? `${t('aiChat.runs.goalSeparator')}${payload.step_goal}` : ''
  const lines: string[] = [`**${t('aiChat.runs.deviceAction')} ${label}${goal}**`]
  const answerText =
    typeof payload?.answer === 'string' ? payload.answer : payload?.answer ? String(payload.answer) : ''
  if (answerText) lines.push(answerText)
  else if (payload?.raw) lines.push(String(payload.raw))
  else lines.push(t('aiChat.runs.emptyResponse'))
  if (payload?.topic_id) lines.push(`- ${t('aiChat.runs.topicId')}: ${payload.topic_id}`)
  return lines.join('\n')
}

export const useConversationRunsStore = defineStore('conversationRuns', () => {
  const bySession = reactive<Record<string, ConversationState>>({})
  const localRunningSet = ref<Set<string>>(new Set())

  const ensureState = (sessionId: string): ConversationState => {
    let state = bySession[sessionId]
    if (!state) {
      state = {
        sessionId,
        messages: [],
        loadingMessages: false,
        isSending: false,
        activeRunId: null,
        runStatus: 'idle',
        runAgentKind: null,
        suggestedAgentType: null,
        pendingPermissions: [],
        pendingClarifications: [],
        seenSeq: {},
        currentAnswerId: null,
        subscription: null,
        loaded: false,
        lastAgentKind: null,
        lastProjectRepoId: null,
        hasLogWorkspaceContext: false,
      }
      bySession[sessionId] = state
    }
    // Return the proxy even on first creation so async upload failures repaint immediately.
    return bySession[sessionId]
  }

  const localRunningSessionIds = computed(() => Array.from(localRunningSet.value))

  // ---- message helpers ----------------------------------------------------

  const findMessageIndex = (state: ConversationState, id: string) =>
    state.messages.findIndex((m) => m.id === id)

  const ensureAnswerMessage = (state: ConversationState, answerId: string): ChatEntry => {
    const idx = findMessageIndex(state, answerId)
    if (idx !== -1) return state.messages[idx]
    const placeholder: ChatEntry = {
      id: answerId, role: 'ai', content: THINKING_PLACEHOLDER, kind: 'answer', animateAnswer: true,
    }
    state.messages.push(placeholder)
    return placeholder
  }

  const insertBeforeAnswer = (state: ConversationState, answerId: string, entry: ChatEntry) => {
    const idx = findMessageIndex(state, answerId)
    if (idx === -1) state.messages.push(entry)
    else state.messages.splice(idx, 0, entry)
  }

  // Specialist agents GeneralAgent may route the user to. Unknown values are
  // ignored so a future backend key can't crash the panel.
  const VALID_SUGGESTED_AGENTS = new Set([
    'device',
    'log_analysis',
    'package_search',
    'project_expert',
  ])

  const applySuggestedAgent = (state: ConversationState, raw: unknown) => {
    if (typeof raw === 'string' && VALID_SUGGESTED_AGENTS.has(raw)) {
      state.suggestedAgentType = raw
    }
  }

  /**
   * Apply an AskUserQuestion clarification event to the panel state.
   *
   * Kept agent-agnostic and called from both frame shapes: DeviceAgent streams
   * trace events bare (`type: "clarification_request"`), while the workspace
   * agents wrap theirs in `event: "agent_trace"`. The global user preference
   * "let the agent ask me when instructions are unclear" applies to all of
   * them, so both paths must land in the same `pendingClarifications`.
   *
   * Returns true when the event was a clarification event.
   */
  const applyClarificationEvent = (
    state: ConversationState,
    traceType: string,
    payload: any,
  ): boolean => {
    if (traceType === 'clarification_request') {
      const requestId = String(payload?.request_id || '')
      const questions = normalizeClarificationQuestions(payload?.questions)
      if (
        requestId &&
        questions.length &&
        !state.pendingClarifications.some((c) => c.request_id === requestId)
      ) {
        state.pendingClarifications.push(
          makePendingClarification(requestId, questions, {
            session_id: payload?.session_id || state.sessionId,
            run_id: payload?.run_id || state.activeRunId || undefined,
          }),
        )
      }
      return true
    }
    if (traceType === 'clarification_resolved') {
      const requestId = String(payload?.request_id || '')
      if (requestId) {
        state.pendingClarifications = state.pendingClarifications.filter(
          (c) => c.request_id !== requestId,
        )
      }
      return true
    }
    return false
  }

  // ---- event application --------------------------------------------------

  const applyEventToState = (state: ConversationState, payload: any) => {
    // Specialist transports wrap answer deltas in agent_trace. They are answer
    // prose, not trace cards, and must reach the same path as bare deltas.
    const type = payload?.event === 'agent_trace' && payload?.type === 'answer_delta'
      ? 'answer_delta'
      : payload?.event || payload?.type
    const eventRunId: string | undefined = payload?.run_id
    const eventSessionId: string | undefined = payload?.session_id

    // Reject events that explicitly target a different session — defense in
    // depth so stale subscriptions can't write into the wrong panel.
    if (eventSessionId && eventSessionId !== state.sessionId) return

    // Reject events from a different run unless we have no current run yet.
    if (eventRunId && state.activeRunId && eventRunId !== state.activeRunId) return

    // Latch run_id from the first event that carries it, e.g. backend's
    // ``{"event":"session","run_id":"..."}`` prologue or run_start.
    if (eventRunId && !state.activeRunId) {
      state.activeRunId = eventRunId
      state.runStatus = 'running'
      // New run starts: clear any prior agent-routing suggestion.
      state.suggestedAgentType = null
      if (!state.currentAnswerId) {
        state.currentAnswerId = `run:${eventRunId}:assistant`
      }
    }

    // Dedupe by `(run_id, seq)` so replay+follow doesn't double-render.
    const seq = typeof payload?.seq === 'number' ? payload.seq : null
    // Specialist trace envelopes carry task_id/seq but omit run_id; the
    // subscription's session prologue already established their owning run.
    const sequenceRunId = eventRunId || state.activeRunId
    if (sequenceRunId && seq !== null) {
      const key = `${sequenceRunId}:${seq}`
      if (state.seenSeq[key]) return
      state.seenSeq[key] = 1
    }

    const answerId = state.currentAnswerId || `run:${eventRunId || 'pending'}:assistant`
    if (!state.currentAnswerId) state.currentAnswerId = answerId

    if (type === 'session') return

    if (type === 'plan') {
      insertBeforeAnswer(state, answerId, {
        id: generateUUID(),
        role: 'ai',
        content: formatPlan(payload?.plan),
        kind: 'plan',
      })
      return
    }
    if (type === 'device_action') {
      insertBeforeAnswer(state, answerId, {
        id: generateUUID(),
        role: 'ai',
        content: formatDeviceAction(payload),
        kind: 'device_action',
      })
      return
    }
    if (type === 'log_analysis_status') {
      const target = ensureAnswerMessage(state, answerId)
      const statusText = payload?.message || t('aiChat.runs.logAnalysisProcessing')
      target.content = `**${t('aiChat.agents.logAnalysis')} Agent**\n\n${statusText}`
      return
    }
    if (type === 'log_analysis_context') {
      state.hasLogWorkspaceContext = true
      return
    }

    // OCR degradation for an image-bearing turn: attach to the assistant bubble
    // so the UI can show an "images not recognized" hint. The turn still answers
    // on text only, so we do not change run status here.
    if (type === 'ocr_status') {
      const target = ensureAnswerMessage(state, answerId)
      target.ocrStatus = {
        status: String(payload?.status || 'failed'),
        imageCount: Number(payload?.image_count || 0),
        errorKind: payload?.error_kind ?? null,
      }
      return
    }

    if (type === 'ocr_result') {
      const target = ensureAnswerMessage(state, answerId)
      const text = typeof payload?.text === 'string' ? payload.text.trim() : ''
      if (text) {
        target.visualAnalysis = {
          text,
          imageCount: Number(payload?.image_count || 0),
        }
      }
      return
    }

    if (type === 'agent_trace') {
      const target = ensureAnswerMessage(state, answerId)
      if (!target.traceEvents) target.traceEvents = []
      target.traceRunning = true
      const { event: _evt, ...trace } = payload as Record<string, unknown>
      if (trace && typeof trace.seq === 'number' && typeof trace.type === 'string') {
        target.traceEvents.push(trace as unknown as AgentTraceEvent)
        if (trace.type === 'run_complete') {
          state.runStatus = 'succeeded'
          target.traceRunning = false
          state.pendingClarifications = []
        } else if (trace.type === 'cancelled') {
          state.runStatus = 'cancelled'
          target.traceRunning = false
          state.pendingClarifications = []
        } else if (trace.type === 'error') {
          state.runStatus = 'failed'
          target.traceRunning = false
          state.pendingClarifications = []
        } else {
          // AskUserQuestion is not DeviceAgent-only: log-analysis,
          // project-expert and package-search wrap their trace events in
          // `event: "agent_trace"`, so they never reach the bare-frame branch
          // below. Route the clarification pair through the same state here or
          // their question card would never render.
          applyClarificationEvent(state, trace.type as string, payload)
        }
      }
      return
    }

    // Final-answer body increment. Emitted by all three agents when the
    // provider supports partial streaming. Appended live to the assistant
    // bubble; `run_complete.final_text` later does the authoritative
    // correction (see run_complete handling below). Dedup by `(run_id, seq)`
    // is already handled above. We deliberately do NOT push this into
    // `traceEvents` — it is answer prose, not a trace step.
    if (type === 'answer_delta') {
      const chunk = typeof payload?.text_chunk === 'string' ? payload.text_chunk : ''
      if (!chunk) return
      const target = ensureAnswerMessage(state, answerId)
      target.streamedAnswer = (target.streamedAnswer || '') + chunk
      target.content = projectStreamedAnswer(
        target.streamedAnswer, state.runAgentKind !== null && state.runAgentKind !== 'device',
      ) || THINKING_PLACEHOLDER
      target.animateAnswer = true
      target.kind = 'answer'
      target.traceRunning = true
      return
    }

    if (typeof type === 'string' && DEVICE_TRACE_TYPES.has(type)) {
      const target = ensureAnswerMessage(state, answerId)
      if (!target.traceEvents) target.traceEvents = []
      if (type === 'run_start') target.traceRunning = true
      const { event: _evt, ...trace } = payload as Record<string, unknown>
      if (trace && typeof trace.seq === 'number' && typeof trace.type === 'string') {
        target.traceEvents.push(trace as unknown as AgentTraceEvent)
      }
      if (type === 'run_complete') {
        // Authoritative correction: `final_text` is the de-sensitised /
        // trimmed full answer. Overwrite whatever the `answer_delta` stream
        // accumulated so the rendered bubble matches the persisted text. When
        // no `answer_delta` arrived (e.g. provider downgrade), the presentation
        // component progressively reveals this newly received full answer.
        const finalText = (payload as any)?.final_text
        if (typeof finalText === 'string' && finalText.trim()) {
          target.animateAnswer = true
          target.content = finalText.trimStart()
        }
        applySuggestedAgent(state, (payload as any)?.suggested_agent_type)
        state.runStatus = 'succeeded'
        target.traceRunning = false
        state.pendingClarifications = []
      } else if (type === 'cancelled') {
        state.runStatus = 'cancelled'
        target.traceRunning = false
        state.pendingClarifications = []
      }
      if (type === 'tool_permission_request') {
        const requestId = String(payload?.request_id || '')
        if (requestId) {
          const toolInput = payload?.tool_input
          state.pendingPermissions.push({
            request_id: requestId,
            tool_name: String(payload?.tool_name || ''),
            risk: (payload?.risk || 'write') as PendingPermission['risk'],
            rationale: payload?.rationale || undefined,
            tool_input: toolInput && typeof toolInput === 'object' ? toolInput : undefined,
            session_id: payload?.session_id || state.sessionId,
            run_id: payload?.run_id || state.activeRunId || undefined,
            editingArgs: toolInput ? JSON.stringify(toolInput, null, 2) : '{}',
            editingError: null,
          })
        }
      } else if (type === 'tool_permission_resolved') {
        const requestId = String(payload?.request_id || '')
        if (requestId) {
          state.pendingPermissions = state.pendingPermissions.filter(
            (p) => p.request_id !== requestId,
          )
        }
      } else {
        applyClarificationEvent(state, type, payload)
      }
      return
    }

    const target = ensureAnswerMessage(state, answerId)
    if (type === 'chunk' && typeof payload?.content === 'string') {
      const chunk = payload.content
      if (target.content === THINKING_PLACEHOLDER) {
        const trimmedChunk = chunk.trimStart()
        if (trimmedChunk) target.content = trimmedChunk
      } else {
        target.content += chunk
      }
    } else if (type === 'done') {
      if (typeof payload?.answer === 'string' && payload.answer) {
        target.animateAnswer = true
        target.content = payload.answer.trimStart()
      } else if (!target.content || target.content === THINKING_PLACEHOLDER) {
        target.content = t('aiChat.runs.emptyResponse')
      }
      applySuggestedAgent(state, payload?.suggested_agent_type)
      const resultStatus = String(payload?.result?.status || '').toLowerCase()
      if (resultStatus === 'cancelled') state.runStatus = 'cancelled'
      else if (resultStatus === 'stale') state.runStatus = 'stale'
      else if (resultStatus === 'failed' || resultStatus === 'error') state.runStatus = 'failed'
      else if (state.runStatus !== 'cancelled' && state.runStatus !== 'failed') state.runStatus = 'succeeded'
      target.traceRunning = false
    } else if (type === 'error') {
      // The backend already returns a user-facing, friendly message for known
      // cases (e.g. missing metadata.json). Render it verbatim; only fall back
      // to a generic friendly line when no message was provided.
      const backendMsg = typeof payload?.message === 'string' ? payload.message.trim() : ''
      target.content = backendMsg || t('aiChat.runs.genericError')
      target.animateAnswer = false
      state.runStatus = 'failed'
      target.traceRunning = false
    }
  }

  const markTerminal = (state: ConversationState, status: RunStatus) => {
    state.isSending = false
    state.runStatus = status
    localRunningSet.value.delete(state.sessionId)
    state.activeRunId = null
    state.currentAnswerId = null
    state.runAgentKind = null
    state.subscription = null
    // A terminal run can never resolve an outstanding clarification, so drop any
    // pending question cards (e.g. when a cancel-on-timeout aborts the run
    // before its ``clarification_resolved`` event is delivered).
    state.pendingClarifications = []
    // Keep seenSeq so a future reconnect to the same run still dedupes; the
    // map is small and cleared on session reload.
  }

  const terminalStatus = (state: ConversationState): RunStatus => {
    if (state.runStatus === 'cancelled' || state.runStatus === 'failed' || state.runStatus === 'stale') {
      return state.runStatus
    }
    return 'succeeded'
  }

  // ---- snapshot merge -----------------------------------------------------

  const mergeSnapshot = (state: ConversationState, snapshot: any) => {
    if (!snapshot || typeof snapshot !== 'object') return
    const runId = snapshot.run_id
    if (!runId) return
    state.activeRunId = runId
    state.runStatus = (snapshot.status || 'running') as RunStatus
    state.runAgentKind = (snapshot.agent_kind || null) as AgentKind | null
    applySuggestedAgent(state, snapshot.suggested_agent_type)
    state.currentAnswerId = `run:${runId}:assistant`
    if (state.runStatus === 'running') {
      state.isSending = true
      localRunningSet.value.add(state.sessionId)
    }

    // Pending permissions from snapshot.
    const pending = Array.isArray(snapshot.pending_permissions) ? snapshot.pending_permissions : []
    for (const p of pending) {
      const requestId = String(p?.request_id || '')
      if (!requestId) continue
      if (state.pendingPermissions.some((x) => x.request_id === requestId)) continue
      const toolInput = p?.tool_input
      state.pendingPermissions.push({
        request_id: requestId,
        tool_name: String(p?.tool_name || ''),
        risk: (p?.risk || 'write') as PendingPermission['risk'],
        rationale: p?.rationale || undefined,
        tool_input: toolInput && typeof toolInput === 'object' ? toolInput : undefined,
        session_id: p?.session_id || state.sessionId,
        run_id: p?.run_id || runId,
        editingArgs: toolInput ? JSON.stringify(toolInput, null, 2) : '{}',
        editingError: null,
      })
    }

    // Pending clarifications from snapshot.
    const pendingClar = Array.isArray(snapshot.pending_clarifications)
      ? snapshot.pending_clarifications
      : []
    for (const c of pendingClar) {
      const requestId = String(c?.request_id || '')
      if (!requestId) continue
      if (state.pendingClarifications.some((x) => x.request_id === requestId)) continue
      const questions = normalizeClarificationQuestions(c?.questions)
      if (!questions.length) continue
      state.pendingClarifications.push(
        makePendingClarification(requestId, questions, {
          session_id: c?.session_id || state.sessionId,
          run_id: c?.run_id || runId,
        }),
      )
    }

    // Restore the user message that initiated this run. The backend snapshot
    // always carries ``user_message``, but the DB-side ``chat_messages`` row
    // may not exist yet (log-analysis writes user+assistant together at run
    // terminal via ``_persist_exchange``). Without this restore, switching
    // away from an in-flight session and back would show only the assistant
    // bubble — the user's own prompt would silently disappear.
    const snapshotUserMessage =
      typeof snapshot.user_message === 'string' ? snapshot.user_message : ''
    const trimmedUserMessage = snapshotUserMessage.trim()
    if (trimmedUserMessage) {
      const userMsgId = `run:${runId}:user`
      const alreadyPresent = state.messages.some(
        (m) =>
          m.role === 'user' &&
          (m.id === userMsgId ||
            (m.content || '').trim().startsWith(trimmedUserMessage)),
      )
      if (!alreadyPresent) {
        state.messages.push({
          id: userMsgId,
          role: 'user',
          content: snapshotUserMessage,
          kind: 'user',
        })
      }
    }

    // Replay trace events into the assistant placeholder. Snapshot already
    // deduplicates server-side, so we can clear and rebuild.
    const trace: any[] = Array.isArray(snapshot.trace_events) ? snapshot.trace_events : []
    const target = ensureAnswerMessage(state, state.currentAnswerId)
    target.traceEvents = []
    target.traceRunning = state.runStatus === 'running'
    target.animateAnswer = false
    target.streamedAnswer = ''
    const snapshotSeq = new Set<number>()
    for (const ev of [...trace].sort((a, b) => (a?.seq || 0) - (b?.seq || 0))) {
      if (ev && typeof ev.seq === 'number' && typeof ev.type === 'string') {
        if (snapshotSeq.has(ev.seq)) continue
        snapshotSeq.add(ev.seq)
        const key = `${runId}:${ev.seq}`
        // A snapshot can itself contain duplicate trace frames.
        if (ev.type === 'answer_delta') {
          target.streamedAnswer += typeof ev.text_chunk === 'string' ? ev.text_chunk : ''
        } else {
          target.traceEvents.push(ev as AgentTraceEvent)
        }
        state.seenSeq[key] = 1
      }
    }
    if (target.streamedAnswer) {
      target.content = projectStreamedAnswer(
        target.streamedAnswer, state.runAgentKind !== null && state.runAgentKind !== 'device',
      ) || THINKING_PLACEHOLDER
    }
    // If the snapshot has an answer-so-far, render it.
    const answerSoFar = snapshot.answer_so_far
    if (typeof answerSoFar === 'string' && answerSoFar.trim()) {
      target.content = answerSoFar.trimStart()
    }
  }

  // ---- SSE pump -----------------------------------------------------------

  const pumpSSE = async (
    state: ConversationState,
    response: Response,
    abortSignal: AbortSignal,
  ): Promise<{ terminal: boolean }> => {
    if (!response.body) throw new Error('Empty response body; cannot stream')
    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    let terminal = false

    const processChunk = (chunk: string) => {
      buffer += chunk
      let remaining = buffer.replace(/\r\n/g, '\n')
      while (true) {
        const idx = remaining.indexOf('\n\n')
        if (idx === -1) break
        const raw = remaining.slice(0, idx)
        remaining = remaining.slice(idx + 2)
        const trimmed = raw.trim()
        if (!trimmed.startsWith('data:')) continue
        const jsonStr = trimmed.replace(/^data:\s*/, '')
        if (!jsonStr) continue
        try {
          const payload = JSON.parse(jsonStr)
          applyEventToState(state, payload)
          const type = payload?.event || payload?.type
          if (type === 'done' || type === 'error' || type === 'run_complete' || type === 'cancelled') {
            terminal = true
          }
        } catch (err) {
          console.error('Failed to parse stream data', err, jsonStr)
        }
      }
      buffer = remaining
    }

    try {
      while (true) {
        if (abortSignal.aborted) break
        const { value, done } = await reader.read()
        if (value) processChunk(decoder.decode(value, { stream: !done }))
        if (done) break
      }
      if (buffer.trim()) processChunk('\n\n')
    } catch (err: any) {
      if (err?.name === 'AbortError') return { terminal: false }
      throw err
    }
    return { terminal }
  }

  // ---- public API ---------------------------------------------------------

  const buildAuthHeaders = (authToken?: string | null): Record<string, string> => {
    const headers: Record<string, string> = { [LOCALE_HEADER]: getActiveLocale() }
    if (authToken) headers.Authorization = `Bearer ${authToken}`
    return headers
  }

  const readHttpErrorMessage = async (resp: Response): Promise<string> => {
    if (resp.status === 401) return t('aiChat.runs.sessionExpired')
    try {
      const body = await resp.json()
      const detail = body?.detail
      const message = typeof detail === 'string' && detail.trim()
        ? detail
        : typeof detail?.message === 'string'
          ? detail.message
          : typeof body?.message === 'string' ? body.message : ''
      // Older backends return this parser error before application validation.
      const partLimit = message.match(/Part exceeded maximum size of (\d+)KB/i)
      if (partLimit) return t('aiChat.runs.multipartTooLarge', { limit: Number(partLimit[1]) / 1024 })
      if (message) return message
      if (Array.isArray(detail)) {
        const errors = detail.map((item) => typeof item?.msg === 'string' ? item.msg : '').filter(Boolean)
        if (errors.length) return errors.join('; ')
      }
    } catch {
      // ignore non-JSON error bodies
    }
    if (resp.status === 413) return t('aiChat.runs.uploadTooLarge')
    if (resp.status === 429) return t('aiChat.runs.tooManyRequests')
    if ([502, 503, 504].includes(resp.status)) return t('aiChat.runs.serviceUnavailable', { status: resp.status })
    return t('aiChat.runs.httpRequestFailed', { status: resp.status })
  }

  /** Drop the in-memory state for a session (e.g. after deletion). */
  const clearSession = (sessionId: string) => {
    const state = bySession[sessionId]
    if (state?.subscription) {
      try {
        state.subscription.abort()
      } catch {
        // ignore
      }
    }
    delete bySession[sessionId]
    localRunningSet.value.delete(sessionId)
  }

  /** Abort the SSE reader for this session without cancelling the backend run. */
  const abortSubscription = (sessionId: string) => {
    const state = bySession[sessionId]
    if (state?.subscription) {
      try {
        state.subscription.abort()
      } catch {
        // ignore
      }
      state.subscription = null
    }
  }

  /**
   * Load DB messages + query active-run snapshot. If a run is in flight,
   * merge a virtual assistant message and subscribe to the run stream.
   *
   * Caller should pass `force=true` after sending a new message to refresh.
   */
  const sessionLoads = new Map<string, Promise<ConversationState>>()

  const restoreSession = async (
    sessionId: string,
    opts: { authToken?: string | null; isLoggedIn?: boolean; force?: boolean } = {},
  ) => {
    const state = ensureState(sessionId)
    if (state.loaded && !opts.force) return state

    state.loadingMessages = true
    let subscribeRunId: string | null = null
    try {
      if (opts.isLoggedIn) {
        try {
          const resp = await userApi.fetchMessages(sessionId)
          if (resp?.success && Array.isArray(resp.data)) {
            const records = resp.data as ChatMessageRecord[]
            state.messages = records.map((item) => ({
              id: item.id || generateUUID(),
              role: item.role as ChatRole,
              content: item.content || '',
              kind: item.role === 'user' ? 'user' : 'answer',
              traceEvents: Array.isArray(item.trace_events)
                ? (item.trace_events as AgentTraceEvent[])
                : undefined,
              traceRunning: item.run_status === 'running',
            }))
            // Resolve attached-image bytes in the background so the message
            // list paints immediately; each thumbnail appears as it arrives.
            void hydrateHistoryImages(state, records, sessionId, opts.authToken)
            const lastWithAgent = [...records].reverse().find((m) => m.run_agent_kind)
            if (lastWithAgent?.run_agent_kind) {
              state.lastAgentKind = lastWithAgent.run_agent_kind as AgentKind
            }
            if (resp.workspace_context) {
              state.lastAgentKind = lastWithAgent?.run_agent_kind as AgentKind || resp.workspace_context.agent_kind
              state.lastProjectRepoId = resp.workspace_context.project_repo_id
              state.hasLogWorkspaceContext = resp.workspace_context.has_log_workspace
            }
            // LogAnalysisChatService persists `[日志附件] ...` in the user
            // message after a successful run. OR with the live SSE-derived
            // state so a reload during an in-flight run cannot erase it.
            state.hasLogWorkspaceContext = state.hasLogWorkspaceContext || records.some(
              (item) => item.role === 'user' && hasPersistedLogAttachmentMarker(item.content),
            )
          }
        } catch (err) {
          console.warn('Failed to load session messages', err)
        }
      }

      // Query active-run regardless of auth state — backend handles owner_scope.
      const snapshotController = new AbortController()
      const snapshotTimeout = setTimeout(() => snapshotController.abort(), 15000)
      try {
        const resp = await fetch(
          getServiceUrl(`/api/v1/ai-chat/chat/sessions/${encodeURIComponent(sessionId)}/active-run`),
          { headers: buildAuthHeaders(opts.authToken), credentials: 'include', signal: snapshotController.signal },
        )
        if (resp.ok) {
          const snapshot = await resp.json()
          mergeSnapshot(state, snapshot)
          if (state.activeRunId && state.runStatus === 'running') {
            subscribeRunId = state.activeRunId
          } else if (state.activeRunId) {
            markTerminal(state, terminalStatus(state))
          }
        } else if (resp.status === 404 && (state.activeRunId || state.isSending)) {
          markTerminal(state, terminalStatus(state))
        }
      } catch (err) {
        // 404 = no active run; anything else is just a warning.
        if ((err as any)?.name !== 'AbortError') {
          console.debug('active-run query skipped', err)
        }
      } finally {
        clearTimeout(snapshotTimeout)
      }

      state.loaded = true
    } finally {
      state.loadingMessages = false
    }
    if (subscribeRunId) {
      void subscribeRun(sessionId, subscribeRunId, { authToken: opts.authToken })
    }
    return state
  }

  // Sidebar selection and route watchers can request the same history at once.
  // Share that request so an older response cannot erase the restored trace.
  const loadSession = (
    sessionId: string,
    opts: { authToken?: string | null; isLoggedIn?: boolean; force?: boolean } = {},
  ): Promise<ConversationState> => {
    const pending = sessionLoads.get(sessionId)
    if (pending) return pending
    const promise = restoreSession(sessionId, opts).finally(() => sessionLoads.delete(sessionId))
    sessionLoads.set(sessionId, promise)
    return promise
  }

  const subscribeRun = async (
    sessionId: string,
    runId: string,
    opts: { authToken?: string | null } = {},
  ) => {
    const state = ensureState(sessionId)
    // Replace any existing subscription on this session.
    if (state.subscription) {
      try { state.subscription.abort() } catch { /* ignore */ }
    }
    const ac = new AbortController()
    state.subscription = ac
    state.isSending = true
    localRunningSet.value.add(sessionId)

    try {
      const resp = await fetch(
        getServiceUrl(`/api/v1/ai-chat/chat/runs/${encodeURIComponent(runId)}/stream`),
        {
          headers: buildAuthHeaders(opts.authToken),
          credentials: 'include',
          signal: ac.signal,
        },
      )
      if (!resp.ok) throw new ChatHttpError(await readHttpErrorMessage(resp))
      const { terminal } = await pumpSSE(state, resp, ac.signal)
      if (terminal) markTerminal(state, state.runStatus === 'idle' ? 'succeeded' : state.runStatus)
    } catch (err: any) {
      if (err?.name === 'AbortError') return
      console.warn('Failed to subscribe to run', err)
      markTerminal(state, 'failed')
    } finally {
      if (state.subscription === ac) state.subscription = null
    }
  }

  /**
   * Create-or-subscribe via POST /chat/stream. The endpoint resolves to one
   * of: 200 SSE (new run created, or resuming), 409 (active run exists -
   * caller should switch to subscribeRun), 4xx/5xx on error.
   */
  const startDeviceRun = async (
    sessionId: string,
    payload: StartDeviceRunPayload,
    opts: { authToken?: string | null } = {},
  ) => {
    const state = ensureState(sessionId)
    if (state.isSending) return

    // Optimistic local append: user message + assistant placeholder. Stable
    // assistant id keeps the placeholder in place even after run_id arrives,
    // since the backend's session+run_id prologue will overwrite ours.
    const userDisplay = payload.message
    const userMessageId = generateUUID()
    state.messages.push({
      id: userMessageId,
      role: 'user',
      content: userDisplay,
      kind: 'user',
      images: payload.imagePreviews?.length ? payload.imagePreviews : undefined,
    })
    const pendingAnswerId = `run:pending:${generateUUID()}:assistant`
    state.currentAnswerId = pendingAnswerId
    state.messages.push({
      id: pendingAnswerId,
      role: 'ai',
      content: THINKING_PLACEHOLDER,
      kind: 'answer',
      traceEvents: [],
      traceRunning: true,
      animateAnswer: true,
    })
    state.isSending = true
    state.runStatus = 'running'
    state.runAgentKind = 'device'
    localRunningSet.value.add(sessionId)

    const ac = new AbortController()
    state.subscription = ac

    const body = {
      message: payload.message,
      session_id: sessionId,
      history: payload.history || [],
      remember: payload.remember ?? true,
      // ``/chat/stream`` defaults to GeneralAgent unless the specialist type
      // is explicit.  The device picker is the UI's DeviceAgent selector, so
      // a selected target must carry the matching routing discriminator.
      // Keep it absent for ordinary chat, which also uses this transport.
      agent_type: payload.target_device_id ? 'device' : undefined,
      target_device_id: payload.target_device_id || undefined,
      target_device_name: payload.target_device_name || undefined,
      // Only include when non-empty so image-free requests are unchanged.
      images: payload.images && payload.images.length ? payload.images : undefined,
    }
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      ...buildAuthHeaders(opts.authToken),
    }

    try {
      const resp = await fetch(getServiceUrl('/api/v1/ai-chat/chat/stream'), {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        credentials: 'include',
        signal: ac.signal,
      })

      if (resp.status === 409) {
        // Active run already exists — switch to subscribe.
        let activeRunId: string | null = null
        try {
          const errBody = await resp.json()
          activeRunId = errBody?.active_run_id || errBody?.detail?.active_run_id || null
        } catch { /* ignore */ }
        // Roll back the optimistic append since backend is rejecting create.
        state.messages = state.messages.filter((m) => m.id !== pendingAnswerId && m.id !== userMessageId)
        state.isSending = false
        localRunningSet.value.delete(sessionId)
        if (activeRunId) {
          await subscribeRun(sessionId, activeRunId, opts)
        }
        return
      }

      if (!resp.ok) throw new ChatHttpError(await readHttpErrorMessage(resp))

      // Remap our pending placeholder to the run_id-keyed id once the backend
      // ``session`` frame arrives. We do this inline in pumpSSE via
      // applyEventToState (currentAnswerId starts as pendingAnswerId; the
      // first ``session`` frame with run_id sets activeRunId and we then
      // re-key the placeholder).
      const reKeyPump = async () => {
        if (!resp.body) throw new Error('Empty response body; cannot stream')
        const reader = resp.body.getReader()
        const decoder = new TextDecoder('utf-8')
        let buffer = ''
        let terminal = false
        const processChunk = (chunk: string) => {
          buffer += chunk
          let remaining = buffer.replace(/\r\n/g, '\n')
          while (true) {
            const idx = remaining.indexOf('\n\n')
            if (idx === -1) break
            const raw = remaining.slice(0, idx)
            remaining = remaining.slice(idx + 2)
            const trimmed = raw.trim()
            if (!trimmed.startsWith('data:')) continue
            const jsonStr = trimmed.replace(/^data:\s*/, '')
            if (!jsonStr) continue
            try {
              const evPayload = JSON.parse(jsonStr)
              // Re-key the pending assistant placeholder to use run_id once known.
              const newRunId = evPayload?.run_id
              if (
                newRunId &&
                state.currentAnswerId === pendingAnswerId
              ) {
                const stableId = `run:${newRunId}:assistant`
                const target = state.messages.find((m) => m.id === pendingAnswerId)
                if (target) target.id = stableId
                state.currentAnswerId = stableId
              }
              applyEventToState(state, evPayload)
              const t = evPayload?.event || evPayload?.type
              if (t === 'done' || t === 'error' || t === 'run_complete' || t === 'cancelled') {
                terminal = true
              }
            } catch (err) {
              console.error('Failed to parse stream data', err, jsonStr)
            }
          }
          buffer = remaining
        }
        try {
          while (true) {
            if (ac.signal.aborted) break
            const { value, done } = await reader.read()
            if (value) processChunk(decoder.decode(value, { stream: !done }))
            if (done) break
          }
          if (buffer.trim()) processChunk('\n\n')
        } catch (err: any) {
          if (err?.name === 'AbortError') return
          throw err
        }
        return terminal
      }

      const terminal = await reKeyPump()
      if (terminal) markTerminal(state, terminalStatus(state))
    } catch (err: any) {
      if (err?.name === 'AbortError') return
      console.error('Failed to start DeviceAgent run', err)
      const target = state.messages.find((m) => m.id === state.currentAnswerId)
      if (target) {
        target.content = t('aiChat.runs.backendCallFailed', { error: err?.message || String(err) })
        target.traceRunning = false
      }
      markTerminal(state, 'failed')
    } finally {
      if (state.subscription === ac) state.subscription = null
    }
  }

  /**
   * Start a Log Analysis Agent run via the existing multipart endpoint.
   * The backend projects status into chat_agent_runs so the unified
   * active-run/snapshot flow works for resume.
   */
  const startLogAnalysisRun = async (
    sessionId: string,
    payload: StartLogAnalysisPayload,
    opts: { authToken?: string | null } = {},
  ) => {
    const state = ensureState(sessionId)
    if (state.isSending) return

    const logFiles = payload.files?.length
      ? payload.files
      : payload.file
        ? [payload.file]
        : []
    const userDisplay = logFiles.length
      ? [
          payload.message || t('aiChat.defaultLogAnalysisMessage'),
          '',
          `${t('aiChat.runs.attachment')} (${logFiles.length}):`,
          ...logFiles.map((file) => `- ${file.name}`),
        ].join('\n')
      : payload.message
    state.messages.push({
      id: generateUUID(),
      role: 'user',
      content: userDisplay,
      kind: 'user',
      images: payload.imagePreviews?.length ? payload.imagePreviews : undefined,
    })
    const pendingAnswerId = `run:pending:${generateUUID()}:assistant`
    state.currentAnswerId = pendingAnswerId
    state.messages.push({
      id: pendingAnswerId,
      role: 'ai',
      content: THINKING_PLACEHOLDER,
      kind: 'answer',
      traceEvents: [],
      traceRunning: true,
      animateAnswer: true,
    })
    state.isSending = true
    state.runStatus = 'running'
    state.runAgentKind = 'log_analysis'
    localRunningSet.value.add(sessionId)

    const ac = new AbortController()
    state.subscription = ac

    const formData = new FormData()
    formData.append('message', payload.message || '')
    formData.append('session_id', sessionId)
    formData.append('remember', String(payload.remember ?? true))
    if (payload.history) formData.append('history', JSON.stringify(payload.history))
    logFiles.forEach((file) => formData.append('files', file))
    if (payload.project_repo_id != null) {
      formData.append('project_repo_id', String(payload.project_repo_id))
    }
    try {
      appendChatImageFiles(formData, payload.images)
      const resp = await fetch(getServiceUrl('/api/v1/ai-chat/log-analysis/stream'), {
        method: 'POST',
        headers: buildAuthHeaders(opts.authToken),
        body: formData,
        credentials: 'include',
        signal: ac.signal,
      })
      if (!resp.ok) throw new ChatHttpError(await readHttpErrorMessage(resp))

      // Re-key inline as we discover run_id. The log-analysis stream may not
      // emit a session prologue frame; we accept either run_id or session_id
      // attribution.
      if (!resp.body) throw new Error('Empty response body; cannot stream')
      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''
      let terminal = false
      const processChunk = (chunk: string) => {
        buffer += chunk
        let remaining = buffer.replace(/\r\n/g, '\n')
        while (true) {
          const idx = remaining.indexOf('\n\n')
          if (idx === -1) break
          const raw = remaining.slice(0, idx)
          remaining = remaining.slice(idx + 2)
          const trimmed = raw.trim()
          if (!trimmed.startsWith('data:')) continue
          const jsonStr = trimmed.replace(/^data:\s*/, '')
          if (!jsonStr) continue
          try {
            const evPayload = JSON.parse(jsonStr)
            const newRunId = evPayload?.run_id
            if (newRunId && state.currentAnswerId === pendingAnswerId) {
              const stableId = `run:${newRunId}:assistant`
              const target = state.messages.find((m) => m.id === pendingAnswerId)
              if (target) target.id = stableId
              state.currentAnswerId = stableId
            }
            applyEventToState(state, evPayload)
            const t = evPayload?.event || evPayload?.type
            if (t === 'done' || t === 'error') terminal = true
          } catch (err) {
            console.error('Failed to parse stream data', err, jsonStr)
          }
        }
        buffer = remaining
      }
      try {
        while (true) {
          if (ac.signal.aborted) break
          const { value, done } = await reader.read()
          if (value) processChunk(decoder.decode(value, { stream: !done }))
          if (done) break
        }
        if (buffer.trim()) processChunk('\n\n')
      } catch (err: any) {
        if (err?.name !== 'AbortError') throw err
      }

      if (terminal) {
        markTerminal(state, terminalStatus(state))
      } else {
        // SSE closed early without terminal — leave state.isSending true so the
        // sidebar spinner keeps spinning; user can re-select the session to
        // resume via active-run snapshot.
      }
    } catch (err: any) {
      if (err?.name === 'AbortError') return
      console.error('Failed to start log-analysis run', err)
      const target = state.messages.find((m) => m.id === state.currentAnswerId)
      if (target) {
        const message = err?.message || String(err)
        target.content = message === t('aiChat.runs.sessionExpired')
          ? t('aiChat.runs.sessionExpired')
          : err instanceof ChatHttpError
            ? t('aiChat.runs.logAnalysisFailed', { error: message })
            : t('aiChat.runs.logAnalysisUnavailable')
        target.traceRunning = false
      }
      markTerminal(state, 'failed')
    } finally {
      if (state.subscription === ac) state.subscription = null
    }
  }

  /** Shared trace/SSE driver for the project expert and Configuration Manager. */
  const startProjectBoundRun = async (
    sessionId: string,
    payload: StartProjectExpertPayload | StartPackageSearchPayload,
    opts: { authToken?: string | null },
    cfg: {
      agentKind: AgentKind
      stream: (payload: {
        message: string
        sessionId: string
        history?: { role: string; content: string }[]
        remember?: boolean
        projectRepoId?: number | null
        files?: File[]
        images?: ChatImageAttachment[]
        authToken?: string | null
        signal?: AbortSignal
      }) => Promise<Response>
      failureMessage: (error: string) => string
      /**
       * Configuration Manager must report an in-stream failure to its
       * composer so component files can be restored. Project Expert keeps
       * the legacy "HTTP/SSE started" boolean semantics.
       */
      requireSuccessfulTerminal?: boolean
    },
  ): Promise<boolean> => {
    const state = ensureState(sessionId)
    if (state.isSending) return false

    const attachedFiles = 'files' in payload && Array.isArray(payload.files)
      ? payload.files
      : []
    const userDisplay = formatPackageAttachmentMessage(
      payload.message,
      attachedFiles,
      t('aiChat.runs.componentAttachments'),
    )

    state.messages.push({
      id: generateUUID(),
      role: 'user',
      content: userDisplay,
      kind: 'user',
      images: payload.imagePreviews?.length ? payload.imagePreviews : undefined,
    })
    const pendingAnswerId = `run:pending:${generateUUID()}:assistant`
    state.currentAnswerId = pendingAnswerId
    state.messages.push({
      id: pendingAnswerId,
      role: 'ai',
      content: THINKING_PLACEHOLDER,
      kind: 'answer',
      traceEvents: [],
      traceRunning: true,
      animateAnswer: true,
    })
    state.isSending = true
    state.runStatus = 'running'
    state.runAgentKind = cfg.agentKind
    localRunningSet.value.add(sessionId)

    const ac = new AbortController()
    state.subscription = ac

    try {
      const resp = await cfg.stream({
        message: payload.message || '',
        sessionId,
        history: payload.history,
        remember: payload.remember ?? true,
        projectRepoId: payload.project_repo_id,
        files: attachedFiles,
        images: payload.images,
        authToken: opts.authToken || null,
        signal: ac.signal,
      })
      if (!resp.ok) {
        throw new ChatHttpError(await readHttpErrorMessage(resp))
      }

        if (!resp.body) throw new Error('Empty response body; cannot stream')
      const reader = resp.body.getReader()
      const decoder = new TextDecoder('utf-8')
      let buffer = ''
      let terminal = false
      const processChunk = (chunk: string) => {
        buffer += chunk
        let remaining = buffer.replace(/\r\n/g, '\n')
        while (true) {
          const idx = remaining.indexOf('\n\n')
          if (idx === -1) break
          const raw = remaining.slice(0, idx)
          remaining = remaining.slice(idx + 2)
          const trimmed = raw.trim()
          if (!trimmed.startsWith('data:')) continue
          const jsonStr = trimmed.replace(/^data:\s*/, '')
          if (!jsonStr) continue
          try {
            const evPayload = JSON.parse(jsonStr)
            const newRunId = evPayload?.run_id
            if (newRunId && state.currentAnswerId === pendingAnswerId) {
              const stableId = `run:${newRunId}:assistant`
              const target = state.messages.find((m) => m.id === pendingAnswerId)
              if (target) target.id = stableId
              state.currentAnswerId = stableId
            }
            applyEventToState(state, evPayload)
            const frameType = evPayload?.event || evPayload?.type
            const terminalType = frameType === 'agent_trace' ? evPayload?.type : frameType
            if (
              terminalType === 'done'
              || terminalType === 'error'
              || terminalType === 'run_complete'
              || terminalType === 'cancelled'
            ) {
              terminal = true
            }
          } catch (err) {
            console.error(`Failed to parse ${cfg.agentKind} stream data`, err, jsonStr)
          }
        }
        buffer = remaining
      }
      try {
        while (true) {
          if (ac.signal.aborted) break
          const { value, done } = await reader.read()
          if (value) processChunk(decoder.decode(value, { stream: !done }))
          if (done) break
        }
        if (buffer.trim()) processChunk('\n\n')
      } catch (err: any) {
        if (err?.name !== 'AbortError') throw err
      }

      if (terminal) {
        const status = terminalStatus(state)
        markTerminal(state, status)
        if (cfg.requireSuccessfulTerminal) return status === 'succeeded'
      }
      return true
    } catch (err: any) {
      if (err?.name === 'AbortError') return false
      console.error(`Failed to start ${cfg.agentKind} run`, err)
      const target = state.messages.find((m) => m.id === state.currentAnswerId)
      if (target) {
        target.content = cfg.failureMessage(err?.message || String(err))
        target.traceRunning = false
      }
      markTerminal(state, 'failed')
      return false
    } finally {
      if (state.subscription === ac) state.subscription = null
    }
  }

  /** Start a Project Expert Agent run (project repo mandatory). */
  const startProjectExpertRun = (
    sessionId: string,
    payload: StartProjectExpertPayload,
    opts: { authToken?: string | null } = {},
  ) =>
    startProjectBoundRun(sessionId, payload, opts, {
      agentKind: 'project_expert',
      stream: (streamPayload) => projectExpertStream({
        ...streamPayload,
        projectRepoId: streamPayload.projectRepoId as number,
      }),
      failureMessage: (error) => t('aiChat.runs.projectExpertFailed', { error }),
    })

  /**
   * Start a Configuration Manager run. Pure search stays project-bound while
   * component-bearing packaging may start unbound and enter mandatory project
   * and per-file confirmation before any build side effect.
   */
  const startPackageSearchRun = (
    sessionId: string,
    payload: StartPackageSearchPayload,
    opts: { authToken?: string | null } = {},
  ) =>
    startProjectBoundRun(sessionId, payload, opts, {
      agentKind: 'package_search',
      stream: packageSearchStream,
      failureMessage: (error) => t('aiChat.runs.packageSearchFailed', { error }),
      requireSuccessfulTerminal: true,
    })

  /** Drop the local run state when the backend can no longer cancel it. */
  const finalizeCancelledLocally = (state: ConversationState) => {
    const answerId = state.currentAnswerId
    const target = answerId ? state.messages.find((m) => m.id === answerId) : null
    if (target) {
      if (target.content === THINKING_PLACEHOLDER) {
        target.content = t('aiChat.runs.cancelledByUser')
      }
      target.traceRunning = false
    }
    abortSubscription(state.sessionId)
    markTerminal(state, 'cancelled')
  }

  /**
   * Cancel the currently-running run on this session.
   *
   * Prefers the unified ``/chat/runs/{run_id}/cancel`` endpoint when run_id is
   * known. Before the run_id frame has arrived, fall back to the per-agent
   * session cancel endpoint.
   */
  const cancelActiveRun = async (
    sessionId: string,
    opts: { authToken?: string | null } = {},
  ) => {
    const state = ensureState(sessionId)
    const runId = state.activeRunId
    const headers = { 'Content-Type': 'application/json', ...buildAuthHeaders(opts.authToken) }

    if (runId) {
      try {
        const resp = await fetch(
          getServiceUrl(`/api/v1/ai-chat/chat/runs/${encodeURIComponent(runId)}/cancel`),
          { method: 'POST', headers, credentials: 'include' },
        )
        const body = await resp.json().catch(() => null)
        // Backend says the run is unknown/already terminal but we still think
        // it's running — unstick the panel locally.
        if (body && body.cancelled === false && state.isSending) {
          finalizeCancelledLocally(state)
        }
      } catch (err) {
        console.warn('Failed to cancel run', err)
      }
      return
    }

    const sessionCancelPath =
      state.runAgentKind === 'log_analysis'
        ? '/api/v1/ai-chat/log-analysis/cancel'
        : state.runAgentKind === 'project_expert'
          ? '/api/v1/ai-chat/project-expert/cancel'
          : state.runAgentKind === 'package_search'
            ? '/api/v1/ai-chat/package-search/cancel'
            : null
    if (sessionCancelPath) {
      try {
        await fetch(getServiceUrl(sessionCancelPath), {
          method: 'POST',
          headers,
          credentials: 'include',
          body: JSON.stringify({ session_id: sessionId }),
        })
      } catch (err) {
        console.warn('Failed to cancel run', err)
      }
      return
    }

    // No run id and no per-agent endpoint: nothing the backend can do.
    if (state.isSending) finalizeCancelledLocally(state)
  }

  /** Submit a HITL permission decision. Removes the entry locally on success. */
  const submitPermission = async (
    sessionId: string,
    requestId: string,
    decision: 'allow' | 'deny',
    options: { updatedArgs?: Record<string, unknown> | null; authToken?: string | null } = {},
  ) => {
    const state = ensureState(sessionId)
    const head = state.pendingPermissions.find((p) => p.request_id === requestId)
    if (!head) return
    try {
      await resolveChatPermission(
        requestId,
        {
          decision,
          updated_args: options.updatedArgs ?? null,
          session_id: head.session_id || sessionId,
          // Add run_id when known. Backend prefers it over session_id.
          ...(head.run_id ? { run_id: head.run_id } : {}),
        },
        options.authToken || null,
      )
      state.pendingPermissions = state.pendingPermissions.filter((p) => p.request_id !== requestId)
    } catch (err: any) {
      const status = err?.response?.status
      if (status === 404) {
        state.pendingPermissions = state.pendingPermissions.filter((p) => p.request_id !== requestId)
        return
      }
      if (head) {
        head.editingError = t('aiChat.runs.submitFailed', {
          error: err?.response?.data?.detail || err?.message || String(err),
        })
      }
      throw err
    }
  }

  /**
   * Submit the user's answers to a pending AskUserQuestion clarification.
   * Validates that every question has at least one selected option or non-empty
   * custom text before calling the backend. Removes the entry on success.
   */
  const submitClarification = async (
    sessionId: string,
    requestId: string,
    options: { authToken?: string | null } = {},
  ) => {
    const state = ensureState(sessionId)
    const head = state.pendingClarifications.find((c) => c.request_id === requestId)
    if (!head) return

    const answers: ClarificationAnswer[] = head.questions.map((_q, i) => ({
      question_index: i,
      selected_labels: (head.draftSelected[i] || []).filter((s) => s && s.trim()),
      custom_text: (head.draftCustom[i] || '').trim() || null,
    }))

    // Client-side required-answer validation (every question must be answered).
    const unanswered = answers.findIndex(
      (a) => a.selected_labels.length === 0 && !a.custom_text,
    )
    if (unanswered >= 0) {
      head.error = t('aiChat.clarification.requiredError')
      return
    }

    head.submitting = true
    head.error = null
    try {
      await resolveChatClarification(
        requestId,
        {
          answers,
          session_id: head.session_id || sessionId,
          ...(head.run_id ? { run_id: head.run_id } : {}),
        },
        options.authToken || null,
      )
      state.pendingClarifications = state.pendingClarifications.filter(
        (c) => c.request_id !== requestId,
      )
    } catch (err: any) {
      const status = err?.response?.status
      if (status === 404) {
        state.pendingClarifications = state.pendingClarifications.filter(
          (c) => c.request_id !== requestId,
        )
        return
      }
      head.submitting = false
      head.error = t('aiChat.runs.submitFailed', {
        error: err?.response?.data?.detail || err?.message || String(err),
      })
      throw err
    }
  }

  const reset = () => {
    for (const id of Object.keys(bySession)) clearSession(id)
  }

  return {
    bySession,
    localRunningSessionIds,
    ensureState,
    loadSession,
    subscribeRun,
    startDeviceRun,
    startLogAnalysisRun,
    startProjectExpertRun,
    startPackageSearchRun,
    THINKING_PLACEHOLDER,
    cancelActiveRun,
    submitPermission,
    submitClarification,
    abortSubscription,
    clearSession,
    reset,
    // exposed for unit testing & integration glue
    applyEventToState,
    mergeSnapshot,
    markTerminal,
  }
})
