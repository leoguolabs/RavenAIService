import axios from 'axios'
import { API_BASE_URL, localeHeaderInterceptor } from './index'
import type {
  ApiResponse,
  AdminIdentity,
  UserAuthPayload,
  UserProfile,
  UserProfileRole,
  ChatSessionSummary,
  ChatSessionSearchData,
  ChatMessageRecord,
  AnnouncementDismissData,
  SystemAnnouncement,
} from '@/types'

export interface UserProfileUpdatePayload {
  display_name?: string | null
  email?: string | null
  language?: string
  profile_role?: UserProfileRole | null
  clarification_enabled?: boolean
  clarification_max_rounds?: number
  clarification_on_timeout?: 'cancel' | 'continue'
}

const USER_TOKEN_KEY = 'raven_user_token'
const USER_TOKEN_FALLBACK_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

const getStorage = () => {
  if (typeof window === 'undefined') return undefined
  try {
    return window.localStorage
  } catch {
    return undefined
  }
}

const decodeBase64Url = (value: string) => {
  try {
    const normalized = value.replace(/-/g, '+').replace(/_/g, '/')
    const padded = normalized + '='.repeat((4 - (normalized.length % 4)) % 4)
    return atob(padded)
  } catch {
    return ''
  }
}

const getTokenMaxAge = (token: string) => {
  const payload = token.split('.', 1)[0]
  const decoded = payload ? decodeBase64Url(payload) : ''
  const exp = Number(decoded.split(':')[2])
  if (Number.isFinite(exp) && exp > 0) {
    return Math.max(0, Math.floor(exp - Date.now() / 1000))
  }
  return USER_TOKEN_FALLBACK_MAX_AGE_SECONDS
}

const getTokenExpiresAt = (token: string) => {
  const payload = token.split('.', 1)[0]
  const decoded = payload ? decodeBase64Url(payload) : ''
  const exp = Number(decoded.split(':')[2])
  return Number.isFinite(exp) && exp > 0 ? exp : null
}

const getCookieToken = () => {
  if (typeof document === 'undefined') return ''
  const prefix = `${USER_TOKEN_KEY}=`
  const item = document.cookie
    .split(';')
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix))
  if (!item) return ''
  try {
    return decodeURIComponent(item.slice(prefix.length))
  } catch {
    return item.slice(prefix.length)
  }
}

const setCookieToken = (token: string) => {
  if (typeof document === 'undefined') return
  const secure = typeof window !== 'undefined' && window.location.protocol === 'https:' ? '; Secure' : ''
  document.cookie = [
    `${USER_TOKEN_KEY}=${encodeURIComponent(token)}`,
    'Path=/',
    `Max-Age=${getTokenMaxAge(token)}`,
    'SameSite=Lax',
    secure,
  ].filter(Boolean).join('; ')
}

const clearCookieToken = () => {
  if (typeof document === 'undefined') return
  document.cookie = `${USER_TOKEN_KEY}=; Path=/; Max-Age=0; SameSite=Lax`
}

export const userToken = {
  get(): string {
    const storage = getStorage()
    const stored = storage?.getItem(USER_TOKEN_KEY) || ''
    if (stored) {
      setCookieToken(stored)
      return stored
    }
    const cookieToken = getCookieToken()
    if (cookieToken && storage) {
      storage.setItem(USER_TOKEN_KEY, cookieToken)
    }
    return cookieToken
  },
  set(token: string) {
    const storage = getStorage()
    if (storage) storage.setItem(USER_TOKEN_KEY, token)
    setCookieToken(token)
  },
  clear() {
    const storage = getStorage()
    if (storage) storage.removeItem(USER_TOKEN_KEY)
    clearCookieToken()
  },
  isExpired(token: string, skewSeconds = 0): boolean {
    const expiresAt = getTokenExpiresAt(token)
    if (!expiresAt) return false
    return expiresAt <= Math.floor(Date.now() / 1000) + skewSeconds
  },
}

const userClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 20000,
})

userClient.interceptors.request.use((config) => {
  const token = userToken.get()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return localeHeaderInterceptor(config)
})

userClient.interceptors.response.use(
  (response) => response.data,
  (error) => Promise.reject(error)
)

export const userApi = {
  login: (username: string, password: string): Promise<ApiResponse<UserAuthPayload>> =>
    userClient.post('/api/v1/users/auth/login', { username, password }),

  register: (payload: {
    username: string
    password: string
    display_name?: string | null
    email: string
  }): Promise<ApiResponse<UserAuthPayload>> =>
    userClient.post('/api/v1/users/auth/register', payload),

  me: (): Promise<ApiResponse<UserProfile>> => userClient.get('/api/v1/users/auth/me'),

  // Probe the signed-in user's scope, even if a legacy admin session also exists.
  adminIdentity: (): Promise<ApiResponse<AdminIdentity>> => userClient.get('/admin/auth/me'),

  updateProfile: (payload: UserProfileUpdatePayload): Promise<ApiResponse<UserProfile>> =>
    userClient.patch('/api/v1/users/auth/me', payload),

  getPendingAnnouncement: (): Promise<ApiResponse<SystemAnnouncement | null>> =>
    userClient.get('/api/v1/announcements/pending'),

  dismissAnnouncement: (
    announcementId: string
  ): Promise<ApiResponse<AnnouncementDismissData>> =>
    userClient.post(`/api/v1/announcements/${announcementId}/dismiss`),

  listSessions: (): Promise<ApiResponse<ChatSessionSummary[]>> =>
    userClient.get('/api/v1/users/chat-sessions'),

  searchSessions: (query: string, offset = 0, signal?: AbortSignal): Promise<ApiResponse<ChatSessionSearchData>> =>
    userClient.get('/api/v1/users/chat-sessions/search', {
      params: { q: query, offset, limit: 20 }, signal,
    }),

  fetchMessages: (sessionId: string): Promise<ApiResponse<ChatMessageRecord[]> & {
    workspace_context?: { agent_kind: 'log_analysis'; project_repo_id: number | null; has_log_workspace: boolean } | null
  }> =>
    userClient.get(`/api/v1/users/chat-sessions/${sessionId}/messages`),

  deleteSession: (sessionId: string): Promise<ApiResponse<ChatSessionSummary[]>> =>
    userClient.delete(`/api/v1/users/chat-sessions/${sessionId}`),

  pinSession: (sessionId: string, pinned: boolean): Promise<ApiResponse<ChatSessionSummary[]>> =>
    userClient.patch(`/api/v1/users/chat-sessions/${sessionId}/pin`, { pinned }),

  renameSession: (sessionId: string, title: string): Promise<ApiResponse<ChatSessionSummary[]>> =>
    userClient.patch(`/api/v1/users/chat-sessions/${sessionId}/rename`, { title }),

  saveMessages: (sessionId: string, userContent: string, aiContent: string, titleHint?: string): Promise<ApiResponse<{ session_id: string }>> =>
    userClient.post(`/api/v1/users/chat-sessions/${sessionId}/messages`, {
      user_content: userContent,
      ai_content: aiContent,
      title_hint: titleHint,
    }),

  summarizeUserMessage: (payload: {
    user_content: string
    session_id?: string | null
    max_length?: number
    persist?: boolean
  }): Promise<{
    success?: boolean
    summary: string
    session_id?: string | null
    persisted?: boolean
  }> =>
    userClient.post('/api/v1/ai-chat/chat/summarize', {
      user_content: payload.user_content,
      session_id: payload.session_id || undefined,
      max_length: payload.max_length ?? 16,
      persist: payload.persist ?? true,
    }),
}

export default userApi
