import axios from 'axios'
import { API_BASE_URL, localeHeaderInterceptor } from '@/api'
import { getActiveLocale, LOCALE_HEADER } from '@/i18n/runtime'
import { appendChatImageFiles } from '@/utils/chatImageUploads'

export type ChatPermissionDecision = 'allow' | 'deny'

/**
 * One image attachment sent alongside a chat turn. ``data`` is a base64 string
 * (may include a ``data:<mime>;base64,`` prefix). OpenAI receives originals with
 * text; other providers use the configured OCR path. Originals are retained in
 * the authorized session image store for history and evidence.
 */
export interface ChatImageAttachment {
  media_type: string
  data: string
}

export interface ChatPermissionResolvePayload {
  decision: ChatPermissionDecision
  updated_args?: Record<string, unknown> | null
  message?: string | null
  session_id?: string | null
  run_id?: string | null
}

export interface ChatPermissionResolveResponse {
  success: boolean
  message: string
  request_id: string
  decision: ChatPermissionDecision
}

export interface ChatClarificationAnswerPayload {
  question_index: number
  selected_labels: string[]
  custom_text?: string | null
}

export interface ChatClarificationResolvePayload {
  answers: ChatClarificationAnswerPayload[]
  session_id?: string | null
  run_id?: string | null
}

export interface ChatClarificationResolveResponse {
  success: boolean
  message: string
  request_id: string
}

export interface ToolPermissionRequestEvent {
  event: 'tool_permission_request'
  request_id: string
  tool_name: string
  tool_input?: Record<string, unknown>
  risk: 'read' | 'write' | 'destructive'
  rationale?: string
  session_id?: string
  run_id?: string
  seq?: number
  ts?: string
}

export interface ToolPermissionResolvedEvent {
  event: 'tool_permission_resolved'
  request_id: string
  decision: ChatPermissionDecision
  reason?: string | null
  updated_args?: Record<string, unknown> | null
  message?: string | null
  session_id?: string
  run_id?: string
  seq?: number
  ts?: string
}

export interface ResultValidationEvent {
  event: 'result_validation'
  status: 'ok' | 'schema_mismatch' | 'result_too_large' | string
  reason?: string | null
  tool_name?: string
  session_id?: string
  seq?: number
  ts?: string
}

export interface ProjectExpertStreamPayload {
  message: string
  sessionId: string
  history?: { role: string; content: string }[]
  remember?: boolean
  projectRepoId: number
  images?: ChatImageAttachment[]
  authToken?: string | null
  signal?: AbortSignal
}

export interface ProjectExpertCancelResponse {
  session_id: string
  cancelled: boolean
  message?: string
}

export interface ProjectExpertResultResponse {
  session_id: string
  status: string
  answer?: string | null
  result?: Record<string, unknown> | null
}

// The Configuration Manager uses the same multipart stream lifecycle as the
// project expert, but packaging turns may start unbound when component files
// are attached. The backend infers project candidates before mandatory human
// confirmation, so ``projectRepoId`` is optional only on this wire contract.
export interface PackageSearchStreamPayload {
  message: string
  sessionId: string
  history?: { role: string; content: string }[]
  remember?: boolean
  projectRepoId?: number | null
  /** Component inputs are repeated multipart ``files`` fields. */
  files?: File[]
  images?: ChatImageAttachment[]
  authToken?: string | null
  signal?: AbortSignal
}
export type PackageSearchCancelResponse = ProjectExpertCancelResponse
export type PackageSearchResultResponse = ProjectExpertResultResponse

const getChatServiceUrl = (path: string) => {
  if (API_BASE_URL && /^https?:\/\//i.test(API_BASE_URL)) {
    return `${API_BASE_URL.replace(/\/$/, '')}${path}`
  }
  if (typeof window !== 'undefined') {
    return `http://${window.location.hostname}:8085${path}`
  }
  return path
}

const chatApi = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

chatApi.interceptors.request.use(localeHeaderInterceptor)

/**
 * Submit the user's HITL decision for a DeviceAgent tool-permission request.
 * Backend route: POST /api/v1/ai-chat/chat/permissions/{request_id}/resolve
 *
 * Returns 200 on resolved, 404 if the request_id has already been resolved,
 * timed out, or never existed. Network/HTTP errors propagate to the caller.
 */
export const resolveChatPermission = (
  requestId: string,
  payload: ChatPermissionResolvePayload,
  authToken?: string | null,
): Promise<ChatPermissionResolveResponse> => {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken) headers.Authorization = `Bearer ${authToken}`
  return chatApi.post(
    `/api/v1/ai-chat/chat/permissions/${encodeURIComponent(requestId)}/resolve`,
    payload,
    { headers },
  ).then((resp) => resp.data as ChatPermissionResolveResponse)
}

/**
 * Submit the user's answers for a DeviceAgent AskUserQuestion clarification.
 * Backend route: POST /api/v1/ai-chat/chat/clarifications/{request_id}/resolve
 *
 * Returns 200 on resolved, 400 if a required question was left blank, 404 if the
 * request was already resolved / timed out / unknown. Errors propagate.
 */
export const resolveChatClarification = (
  requestId: string,
  payload: ChatClarificationResolvePayload,
  authToken?: string | null,
): Promise<ChatClarificationResolveResponse> => {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (authToken) headers.Authorization = `Bearer ${authToken}`
  return chatApi.post(
    `/api/v1/ai-chat/chat/clarifications/${encodeURIComponent(requestId)}/resolve`,
    payload,
    { headers },
  ).then((resp) => resp.data as ChatClarificationResolveResponse)
}

export const projectExpertStream = (payload: ProjectExpertStreamPayload): Promise<Response> => {
  const formData = new FormData()
  formData.append('message', payload.message || '')
  formData.append('session_id', payload.sessionId)
  formData.append('remember', String(payload.remember ?? true))
  formData.append('project_repo_id', String(payload.projectRepoId))
  if (payload.history) formData.append('history', JSON.stringify(payload.history))
  appendChatImageFiles(formData, payload.images)

  const headers: Record<string, string> = { [LOCALE_HEADER]: getActiveLocale() }
  if (payload.authToken) headers.Authorization = `Bearer ${payload.authToken}`

  return fetch(getChatServiceUrl('/api/v1/ai-chat/project-expert/stream'), {
    method: 'POST',
    headers,
    body: formData,
    credentials: 'include',
    signal: payload.signal,
  })
}

export const projectExpertCancel = (
  sessionId: string,
  authToken?: string | null,
): Promise<ProjectExpertCancelResponse> => {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    [LOCALE_HEADER]: getActiveLocale(),
  }
  if (authToken) headers.Authorization = `Bearer ${authToken}`
  return fetch(getChatServiceUrl('/api/v1/ai-chat/project-expert/cancel'), {
    method: 'POST',
    headers,
    body: JSON.stringify({ session_id: sessionId }),
    credentials: 'include',
  }).then((resp) => {
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    return resp.json() as Promise<ProjectExpertCancelResponse>
  })
}

export const projectExpertResult = (
  sessionId: string,
  authToken?: string | null,
): Promise<ProjectExpertResultResponse> => {
  const headers: Record<string, string> = { [LOCALE_HEADER]: getActiveLocale() }
  if (authToken) headers.Authorization = `Bearer ${authToken}`
  return fetch(
    getChatServiceUrl(`/api/v1/ai-chat/project-expert/result?session_id=${encodeURIComponent(sessionId)}`),
    { headers, credentials: 'include' },
  ).then((resp) => {
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    return resp.json() as Promise<ProjectExpertResultResponse>
  })
}

export const packageSearchStream = (payload: PackageSearchStreamPayload): Promise<Response> => {
  const formData = new FormData()
  formData.append('message', payload.message || '')
  formData.append('session_id', payload.sessionId)
  formData.append('remember', String(payload.remember ?? true))
  if (payload.projectRepoId != null) {
    formData.append('project_repo_id', String(payload.projectRepoId))
  }
  if (payload.history) formData.append('history', JSON.stringify(payload.history))
  for (const file of payload.files || []) formData.append('files', file)
  appendChatImageFiles(formData, payload.images)

  const headers: Record<string, string> = { [LOCALE_HEADER]: getActiveLocale() }
  if (payload.authToken) headers.Authorization = `Bearer ${payload.authToken}`

  return fetch(getChatServiceUrl('/api/v1/ai-chat/package-search/stream'), {
    method: 'POST',
    headers,
    body: formData,
    credentials: 'include',
    signal: payload.signal,
  })
}

export const packageSearchCancel = (
  sessionId: string,
  authToken?: string | null,
): Promise<PackageSearchCancelResponse> => {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    [LOCALE_HEADER]: getActiveLocale(),
  }
  if (authToken) headers.Authorization = `Bearer ${authToken}`
  return fetch(getChatServiceUrl('/api/v1/ai-chat/package-search/cancel'), {
    method: 'POST',
    headers,
    body: JSON.stringify({ session_id: sessionId }),
    credentials: 'include',
  }).then((resp) => {
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    return resp.json() as Promise<PackageSearchCancelResponse>
  })
}

export const packageSearchResult = (
  sessionId: string,
  authToken?: string | null,
): Promise<PackageSearchResultResponse> => {
  const headers: Record<string, string> = { [LOCALE_HEADER]: getActiveLocale() }
  if (authToken) headers.Authorization = `Bearer ${authToken}`
  return fetch(
    getChatServiceUrl(`/api/v1/ai-chat/package-search/result?session_id=${encodeURIComponent(sessionId)}`),
    { headers, credentials: 'include' },
  ).then((resp) => {
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    return resp.json() as Promise<PackageSearchResultResponse>
  })
}
