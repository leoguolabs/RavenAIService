import axios from 'axios'
import { API_BASE_URL, localeHeaderInterceptor } from './index'
import { userToken } from './user'
import type {
  AdminAuthData,
  AdminConversationDetail,
  AdminIdentity,
  AgentSkill,
  AgentSkillAgentInfo,
  ApiResponse,
  MetricsRawEventsData,
  MetricsSystemOverview,
  MetricsUserDetail,
  MetricsUserListData,
  PromptsConfigData,
  ProjectMember,
  ProjectAgentInfo,
  ProjectRepo,
  ProjectRepoPayload,
  ProjectSystemPrompt,
  ProjectSystemPromptPreview,
  SkillFileContent,
  SkillFileTree,
  SystemAnnouncement,
  TestConnectionResult,
  UserProfile,
} from '@/types'

export interface MetricsTimeRangeParams {
  from?: string
  to?: string
  bucket?: 'hour' | 'day'
  project_repo_id?: number | string
}

export interface RegistrationEmailSettings {
  email_regex: string
  email_validation_message: string
}

export type AgentExecutor = 'claude_sdk' | 'antigravity_cli'
export interface AgentRuntimeSettings {
  enabled: boolean
  executor: AgentExecutor
  base_url: string
  api_key_set: boolean
  magpie_admin_url?: string | null
  model: string
  agent_overrides: Record<string, { executor?: AgentExecutor | null; model?: string | null }>
  executors: Record<AgentExecutor, string>
  agents: string[]
}

export interface GatewayModel { id: string; display_name?: string; owned_by?: string }

/** Endpoint slots the Admin page can configure; mirrors FieldSpec.group. */
export type ModelSettingsTarget = 'anthropic' | 'anthropic_backup' | 'bug_fix' | 'ocr'

export interface ModelSettingFieldEntry {
  group: ModelSettingsTarget
  source: 'override' | 'env' | 'unset'
  value?: string | number | boolean
  env_default?: string | number | boolean
  is_set?: boolean
  count?: number
}

export interface ModelProviderProfile {
  api_protocol?: 'anthropic_messages' | 'openai_responses'
  name: string
  /** Human-readable vendor name, e.g. '阿里云百炼 / 通义千问'. */
  label: string
  default_base_url: string
  default_model: string
  default_small_fast_model: string | null
  /** Known model ids offered as presets; the model field stays free-text. */
  models: string[]
  supports_image_input: boolean
  supports_mcp_server_tools: boolean
  notes: string
  /** default_base_url is a template (e.g. {WorkspaceId}) needing a real value. */
  base_url_needs_input: boolean
}

/** Editable fields of one Anthropic endpoint slot (primary or backup). */
export interface EndpointForm {
  provider: string
  api_key: string
  /** Primary only: one secret per line. Backup remains a single key. */
  api_keys: string
  base_url: string
  model: string
  small_fast_model: string
}

/** Live endpoint-routing state, surfaced so a stuck failover is visible. */
export interface ModelRouterSlotState {
  configured: boolean
  provider: string | null
  model: string | null
  key_count: number
  samples: number
  bad_samples: number
}

export interface ModelRouterState {
  enabled?: boolean
  slow_ttft_ms?: number
  window_size?: number
  trip_threshold?: number
  cooldown_seconds?: number
  slots?: Record<string, ModelRouterSlotState>
  primary_breaker_open?: boolean
  serving_slot?: 'primary' | 'backup'
  /** Unix seconds the breaker opened; null while the primary is healthy. */
  breaker_opened_at?: number | null
}

export interface ModelSettingsData {
  fields: Record<string, ModelSettingFieldEntry>
  provider_options: string[]
  provider_profiles: ModelProviderProfile[]
  router?: ModelRouterState
}

export interface TestModelSettingsPayload {
  target: ModelSettingsTarget
  /** Omitted fields fall back to the saved effective config (e.g. the API key). */
  provider?: string
  base_url?: string
  model?: string
  api_key?: string
  api_keys?: string[]
}

export interface ModelSettingsTestResult {
  ok: boolean
  target: ModelSettingsTarget
  provider?: string
  base_url?: string
  model?: string
  latency_ms?: number
  status_code?: number
  reply?: string
  usage?: Record<string, unknown> | null
  error_kind?: string
  detail?: string
  key_count?: number
  tested_key_count?: number
  healthy_key_count?: number
  failed_key_count?: number
  key_results?: Array<{
    key_id: string
    ok: boolean
    status_code?: number
    latency_ms?: number
    error_kind?: string
    detail?: string
  }>
  compatibility_result?: {
    ok: boolean
    status_code?: number
    latency_ms?: number
    error_kind?: string
    detail?: string
  } | null
}

export interface UpdateModelSettingsPayload {
  bug_fix_agent_provider?: string
  bug_fix_agent_api_key?: string
  bug_fix_agent_base_url?: string
  bug_fix_agent_model?: string
  bug_fix_agent_small_fast_model?: string
  bug_fix_agent_max_tokens?: number
  bug_fix_agent_max_turns?: number
  bug_fix_agent_request_timeout_seconds?: number

  anthropic_provider?: string
  anthropic_api_key?: string | null
  anthropic_api_keys?: string[] | null
  anthropic_base_url?: string
  anthropic_model?: string
  anthropic_small_fast_model?: string
  anthropic_max_tokens?: number
  anthropic_backup_enabled?: boolean
  anthropic_backup_provider?: string
  anthropic_backup_api_key?: string | null
  anthropic_backup_base_url?: string
  anthropic_backup_model?: string
  anthropic_backup_small_fast_model?: string
  /**
   * Routing policy. Rejected by the backend as a set, not field by field:
   * e.g. a trip threshold above the window size, or a first-token deadline
   * below the "slow" label. Surface the 400 detail rather than pre-validating.
   */
  model_router_enabled?: boolean
  model_router_first_token_deadline_ms?: number
  model_router_slow_ttft_ms?: number
  model_router_window_size?: number
  model_router_trip_threshold?: number
  model_router_min_samples?: number
  model_router_hard_failure_trip?: number
  model_router_cooldown_seconds?: number
  model_router_sample_ttl_seconds?: number
  ocr_enabled?: boolean
  ocr_api_key?: string | null
  ocr_base_url?: string
  ocr_model?: string
  ocr_provider?: string
}

const ADMIN_TOKEN_KEY = 'raven_admin_token'

const getStorage = () => {
  if (typeof window === 'undefined') return undefined
  return window.sessionStorage
}

export const adminToken = {
  get(): string {
    const storage = getStorage()
    return storage?.getItem(ADMIN_TOKEN_KEY) || userToken.get()
  },
  set(token: string) {
    const storage = getStorage()
    if (storage) storage.setItem(ADMIN_TOKEN_KEY, token)
  },
  clear() {
    const storage = getStorage()
    if (storage) storage.removeItem(ADMIN_TOKEN_KEY)
  },
}

const adminClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 20000,
  headers: {
    'Content-Type': 'application/json',
  },
})

adminClient.interceptors.request.use((config) => {
  const token = adminToken.get()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return localeHeaderInterceptor(config)
})

adminClient.interceptors.response.use(
  (response) => response.data,
  (error) => {
    if (error?.response?.status === 401) {
      adminToken.clear()
    }
    return Promise.reject(error)
  }
)

export const adminApi = {
  login: (username: string, password: string): Promise<ApiResponse<AdminAuthData>> =>
    adminClient.post('/admin/auth/login', { username, password }),

  me: (): Promise<ApiResponse<AdminIdentity>> => adminClient.get('/admin/auth/me'),

  logout: (): Promise<ApiResponse> => adminClient.post('/admin/auth/logout'),

  getCurrentAnnouncement: (): Promise<ApiResponse<SystemAnnouncement | null>> =>
    adminClient.get('/admin/announcements/current'),

  publishAnnouncement: (payload: {
    title: string
    content: string
  }): Promise<ApiResponse<SystemAnnouncement>> =>
    adminClient.put('/admin/announcements/current', payload),

  deactivateAnnouncement: (): Promise<ApiResponse<SystemAnnouncement>> =>
    adminClient.delete('/admin/announcements/current'),

  fetchPromptsConfig: (): Promise<ApiResponse<PromptsConfigData>> =>
    adminClient.get('/admin/prompts/config'),

  savePromptsConfig: (payload: {
    content?: string
    prompts?: Array<{ id: string; content: string }>
    expected_checksum?: string
    force?: boolean
  }): Promise<ApiResponse<PromptsConfigData>> => adminClient.put('/admin/prompts/config', payload),

  listUsers: (): Promise<ApiResponse<UserProfile[]>> => adminClient.get('/api/v1/users'),

  getRegistrationEmailSettings: (): Promise<ApiResponse<RegistrationEmailSettings>> =>
    adminClient.get('/api/v1/users/registration-email-settings'),

  updateRegistrationEmailSettings: (
    payload: RegistrationEmailSettings
  ): Promise<ApiResponse<RegistrationEmailSettings>> =>
    adminClient.put('/api/v1/users/registration-email-settings', payload),

  getModelSettings: (): Promise<ApiResponse<ModelSettingsData>> =>
    adminClient.get('/admin/model-settings'),

  getAgentRuntime: (): Promise<ApiResponse<AgentRuntimeSettings>> => adminClient.get('/admin/agent-runtime'),
  updateAgentRuntime: (payload: Partial<AgentRuntimeSettings> & { api_key?: string }): Promise<ApiResponse<AgentRuntimeSettings>> =>
    adminClient.put('/admin/agent-runtime', payload),
  getGatewayModels: (): Promise<ApiResponse<GatewayModel[]>> => adminClient.get('/admin/agent-runtime/models'),

  updateModelSettings: (
    payload: UpdateModelSettingsPayload
  ): Promise<ApiResponse<ModelSettingsData>> =>
    adminClient.put('/admin/model-settings', payload),

  resetModelSettings: (): Promise<ApiResponse<ModelSettingsData>> =>
    adminClient.delete('/admin/model-settings'),

  testModelSettings: (
    payload: TestModelSettingsPayload
  ): Promise<ApiResponse<ModelSettingsTestResult>> =>
    // A primary pool is deliberately checked one key at a time so the verifier
    // does not manufacture upstream concurrency/rate-limit failures. Allow the
    // full nginx Admin proxy window for a real multi-key pool.
    adminClient.post('/admin/model-settings/test', payload, { timeout: 300000 }),

  createUser: (payload: {
    username: string
    password: string
    display_name?: string
    email?: string
    role?: 'user' | 'admin'
  }): Promise<ApiResponse<UserProfile>> => adminClient.post('/api/v1/users', payload),

  updateUser: (
    userId: string,
    payload: {
      display_name?: string
      email?: string
      is_active?: boolean
      password?: string
      role?: 'user' | 'admin'
      disabled_message?: string | null
    }
  ): Promise<ApiResponse<UserProfile>> => adminClient.patch(`/api/v1/users/${userId}`, payload),

  disableUser: (userId: string): Promise<ApiResponse<UserProfile>> =>
    adminClient.delete(`/api/v1/users/${userId}`),

  listProjectRepos: (params?: {
    include_disabled?: boolean
    offset?: number
    limit?: number
  }): Promise<ApiResponse<ProjectRepo[]>> =>
    adminClient.get('/admin/project-repos', { params }),

  listProjectAgents: (): Promise<ApiResponse<ProjectAgentInfo[]>> =>
    adminClient.get('/admin/project-agents'),

  createProjectRepo: (payload: ProjectRepoPayload): Promise<ApiResponse<ProjectRepo>> =>
    adminClient.post('/admin/project-repos', payload),

  updateProjectRepo: (
    repoId: number,
    payload: ProjectRepoPayload
  ): Promise<ApiResponse<ProjectRepo>> =>
    adminClient.put(`/admin/project-repos/${repoId}`, payload),

  deleteProjectRepo: (repoId: number, force = false): Promise<void> =>
    adminClient.delete(`/admin/project-repos/${repoId}`, {
      params: force ? { force: true } : undefined,
    }),

  testProjectRepoConnection: (repoId: number): Promise<ApiResponse<TestConnectionResult>> =>
    adminClient.post(`/admin/project-repos/${repoId}/test-connection`),

  listProjectRepoMembers: (repoId: number): Promise<ApiResponse<ProjectMember[]>> =>
    adminClient.get(`/admin/project-repos/${repoId}/members`),

  addProjectRepoMember: (
    repoId: number,
    userId: string
  ): Promise<ApiResponse<ProjectMember[]>> =>
    adminClient.post(`/admin/project-repos/${repoId}/members`, { user_id: userId }),

  removeProjectRepoMember: (repoId: number, userId: string): Promise<void> =>
    adminClient.delete(`/admin/project-repos/${repoId}/members/${userId}`),

  listSkillAgents: (): Promise<ApiResponse<AgentSkillAgentInfo[]>> =>
    adminClient.get('/admin/agents'),

  listAgentSkills: (agentKey: string): Promise<ApiResponse<AgentSkill[]>> =>
    adminClient.get(`/admin/agents/${agentKey}/skills`),

  uploadAgentSkill: (
    agentKey: string,
    file: File,
    overwrite = false,
    onProgress?: (percent: number) => void
  ): Promise<ApiResponse<AgentSkill>> => {
    const form = new FormData()
    form.append('file', file)
    return adminClient.post(`/admin/agents/${agentKey}/skills`, form, {
      params: { overwrite },
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000,
      onUploadProgress: (e) => {
        if (!onProgress || !e.total) return
        onProgress(Math.round((e.loaded / e.total) * 100))
      },
    })
  },

  updateAgentSkill: (
    agentKey: string,
    skillId: string,
    payload: { enabled: boolean }
  ): Promise<ApiResponse<AgentSkill>> =>
    adminClient.patch(`/admin/agents/${agentKey}/skills/${skillId}`, payload),

  deleteAgentSkill: (agentKey: string, skillId: string): Promise<void> =>
    adminClient.delete(`/admin/agents/${agentKey}/skills/${skillId}`),

  listSkillFiles: (
    agentKey: string,
    skillId: string
  ): Promise<ApiResponse<SkillFileTree>> =>
    adminClient.get(`/admin/agents/${agentKey}/skills/${skillId}/files`),

  readSkillFile: (
    agentKey: string,
    skillId: string,
    path: string
  ): Promise<ApiResponse<SkillFileContent>> =>
    adminClient.get(`/admin/agents/${agentKey}/skills/${skillId}/file`, {
      params: { path },
    }),

  // ==================== 项目级 Skill ====================

  listProjectSkills: (projectCode: string): Promise<ApiResponse<AgentSkill[]>> =>
    adminClient.get(`/admin/project-repos/${projectCode}/skills`),

  uploadProjectSkill: (
    projectCode: string,
    file: File,
    overwrite = false,
    onProgress?: (percent: number) => void
  ): Promise<ApiResponse<AgentSkill>> => {
    const form = new FormData()
    form.append('file', file)
    return adminClient.post(`/admin/project-repos/${projectCode}/skills`, form, {
      params: { overwrite },
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 120000,
      onUploadProgress: (e) => {
        if (!onProgress || !e.total) return
        onProgress(Math.round((e.loaded / e.total) * 100))
      },
    })
  },

  updateProjectSkill: (
    projectCode: string,
    skillId: string,
    payload: { enabled: boolean }
  ): Promise<ApiResponse<AgentSkill>> =>
    adminClient.patch(`/admin/project-repos/${projectCode}/skills/${skillId}`, payload),

  deleteProjectSkill: (projectCode: string, skillId: string): Promise<void> =>
    adminClient.delete(`/admin/project-repos/${projectCode}/skills/${skillId}`),

  listProjectSkillFiles: (
    projectCode: string,
    skillId: string
  ): Promise<ApiResponse<SkillFileTree>> =>
    adminClient.get(`/admin/project-repos/${projectCode}/skills/${skillId}/files`),

  readProjectSkillFile: (
    projectCode: string,
    skillId: string,
    path: string
  ): Promise<ApiResponse<SkillFileContent>> =>
    adminClient.get(`/admin/project-repos/${projectCode}/skills/${skillId}/file`, {
      params: { path },
    }),

  // ==================== 项目级系统提示词 ====================

  getProjectSystemPrompt: (
    projectCode: string,
    agent?: string | null
  ): Promise<ApiResponse<ProjectSystemPrompt>> =>
    adminClient.get(`/admin/project-repos/${projectCode}/system-prompt`, {
      params: agent ? { agent } : undefined,
    }),

  getProjectSystemPromptPreview: (
    projectCode: string,
    agent: string,
    locale?: string | null
  ): Promise<ApiResponse<ProjectSystemPromptPreview>> =>
    adminClient.get(`/admin/project-repos/${projectCode}/system-prompt/preview`, {
      params: {
        agent,
        ...(locale ? { locale } : {}),
      },
    }),

  updateProjectSystemPrompt: (
    projectCode: string,
    content: string,
    agent?: string | null
  ): Promise<ApiResponse<ProjectSystemPrompt>> =>
    adminClient.put(
      `/admin/project-repos/${projectCode}/system-prompt`,
      { content },
      { params: agent ? { agent } : undefined }
    ),

  // ==================== 系统/用户指标 (Metrics) ====================

  metricsOverview: (
    params?: MetricsTimeRangeParams
  ): Promise<ApiResponse<MetricsSystemOverview>> =>
    adminClient.get('/admin/metrics/overview', { params }),

  metricsUsers: (params?: {
    from?: string
    to?: string
    project_repo_id?: number | string
    page?: number
    per_page?: number
    sort?: string
  }): Promise<ApiResponse<MetricsUserListData>> =>
    adminClient.get('/admin/metrics/users', { params }),

  metricsUserDetail: (
    userId: string,
    params?: MetricsTimeRangeParams
  ): Promise<ApiResponse<MetricsUserDetail>> =>
    adminClient.get(`/admin/metrics/users/${userId}`, { params }),

  metricsEvents: (params?: {
    from?: string
    to?: string
    event_type?: string
    source?: string
    user_id?: string
    project_repo_id?: number | string
    page?: number
    per_page?: number
  }): Promise<ApiResponse<MetricsRawEventsData>> =>
    adminClient.get('/admin/metrics/events', { params }),

  metricsEventConversation: (
    eventId: string
  ): Promise<ApiResponse<AdminConversationDetail>> =>
    adminClient.get(`/admin/metrics/events/${encodeURIComponent(eventId)}/conversation`),

  /**
   * Raw bytes of one image attached to the conversation behind `eventId`.
   *
   * The endpoint needs the admin bearer token, so the bytes are fetched here
   * (the client's request interceptor attaches it) and the caller wraps the Blob
   * in an object URL rather than pointing `<img src>` straight at the endpoint.
   */
  metricsEventChatImage: (eventId: string, imageId: string): Promise<Blob> =>
    adminClient.get(
      `/admin/metrics/events/${encodeURIComponent(eventId)}/chat-images/${encodeURIComponent(imageId)}`,
      { responseType: 'blob' }
    ) as unknown as Promise<Blob>,
}

export default adminApi
