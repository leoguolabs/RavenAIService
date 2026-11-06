// 日志元数据类型
export interface LogMetadata {
  source?: string
  environment?: string
  service_name?: string
  version?: string
  version_info?: {
    raw_content?: string
    [key: string]: any
  }
  tags?: string[]
  extra_fields?: Record<string, any>
}

export interface AnalysisTriggerUser {
  id?: string
  username?: string
  display_name?: string
  email?: string
}

export interface AnalysisTriggerInfo {
  source?: string
  run_id?: string
  session_id?: string
  task_id?: string
  user?: AnalysisTriggerUser
  started_at?: string
  finished_at?: string
}

// 日志记录类型
export interface LogRecord {
  id: string
  filename: string
  original_filename?: string
  file_size: number
  file_path?: string
  analysis_group_id?: string | null
  attachment_count?: number
  attachments?: Array<{
    id: string
    filename: string
    file_size: number
  }>
  download_filename?: string | null
  project_id?: number | null
  project_code?: string | null
  project_name?: string | null
  status: 'pending' | 'processing' | 'completed' | 'failed'
  progress?: number
  task_id?: string
  task_name?: string
  task_description?: string
  retry_count?: number
  processing_started_at?: string
  processed_at?: string
  checksum?: string
  mime_type?: string
  log_level?: 'debug' | 'info' | 'warn' | 'error' | 'fatal'
  metadata?: LogMetadata
  error_message?: string
  issue_description?: string
  manual_analysis?: string
  manual_analysis_updated_at?: string
  manual_analysis_author?: {
    id?: string
    username?: string
    display_name?: string
    email?: string
  }
  download_count: number
  download_url?: string
  file_size_human?: string
  ai_analysis_result?: any
  has_ai_analysis?: boolean
  has_manual_analysis?: boolean
  ai_analysis_conversation?: any[]
  ai_analysis_triggered_by?: AnalysisTriggerInfo | null
  ai_analysis_task_id?: string
  ai_analysis_status?: string
  ai_analysis_progress?: number
  ai_analysis_error?: string
  ai_analysis_query?: string
  ai_analysis_started_at?: string
  ai_analysis_finished_at?: string
  // 兼容旧字段
  upload_time?: string
  created_at: string
  updated_at: string
}

// API响应类型
export interface ApiResponse<T = any> {
  success: boolean
  data?: T
  message?: string
  error?: string
}

// 分页响应类型（后端统一格式）
export interface PaginationInfo {
  page: number
  per_page: number
  total: number
  pages: number
}

export interface LogListData {
  logs: LogRecord[]
  pagination: PaginationInfo
}

export interface DownloadInfo {
  download_url: string
  filename: string
  file_size: number
  expires_at: string
}

// 设备长链类型
export type DeviceStatus = 'online' | 'offline'

export interface DeviceInfo {
  id: string
  name: string
  host?: string | null
  models: string[]
  capabilities: Record<string, any>
  last_seen?: string | null
  status: DeviceStatus
}

export interface DeviceListResponse {
  devices: DeviceInfo[]
}

// 上传文件类型
export interface UploadFile {
  file: File
  progress: number
  status: 'pending' | 'uploading' | 'success' | 'error'
  error?: string
}

// 路由参数类型
export interface RouteParams {
  id?: string
}

// 通知类型
export interface NotificationOptions {
  title: string
  message?: string
  type: 'success' | 'warning' | 'info' | 'error'
  duration?: number
}

// 重构包列表类型
export interface RavenComponent {
  name: string
  version?: string
}

export interface RavenMetadata {
  description?: string
  tags?: string[]
  components?: RavenComponent[]
  sha256?: string
  isPatch?: boolean | string
}

export interface RavenPackage {
  id: string
  name: string
  version?: string
  projectCode?: string
  size: number
  createdAt: string
  path?: string
  metadata?: RavenMetadata
  // Presentation-only hint from the detail endpoint: true when the current
  // user may edit this package's description and tags. The backend re-checks
  // on the metadata PATCH, so this only gates whether edit controls render.
  canEditMetadata?: boolean
}

export interface RavenPagination {
  currentPage: number
  totalPages: number
  totalItems: number
  itemsPerPage: number
}

export interface RavenPackageList {
  packages: RavenPackage[]
  pagination: RavenPagination
}

export interface RavenUploadMetadata {
  isPatch?: boolean
  description?: string
  projectCode?: string
  version?: string
  tags?: string[]
  components?: RavenComponent[]
}

// Trace entry attached to the agent-search response, mirroring
// `tool_trace` produced by app/agents/package_search/agent.py. Each entry
// is either a tool-call summary or a `warning`/`info` notice appended by
// the API layer (e.g. `filtered N invalid ids`).
export interface PackageAgentToolTraceEntry {
  type?: string
  tool?: string
  step_id?: string
  status?: 'ok' | 'error' | string
  duration_seconds?: number
  input?: Record<string, unknown>
  output_excerpt?: string
  message?: string
  [key: string]: unknown
}

export interface PackageAgentUsage {
  input_tokens?: number
  output_tokens?: number
  cache_read_tokens?: number
  [key: string]: unknown
}

// Response body for `POST /raven/packages/agent-search` with
// `stream: false`. Mirrors the dict returned by the FastAPI handler in
// app/api/packages.py.
export interface PackageAgentSearchResponse {
  answer: string
  recommended_package_ids: string[]
  relevant_package_ids: string[]
  notes?: string | null
  tool_trace: PackageAgentToolTraceEntry[]
  model: string
  usage: PackageAgentUsage
}

// Trace event emitted on the SSE channel. The schema is the union of the
// standard `AgentTraceEvent` types plus a synthetic terminal `final`
// event whose `data` field carries the same payload as the non-stream
// response.
export interface PackageAgentFinalEvent {
  type: 'final'
  task_id: string
  seq: number
  timestamp: number
  data: PackageAgentSearchResponse
}

export type PackageAgentTraceEvent =
  | PackageAgentFinalEvent
  | {
      type: string
      task_id?: string
      seq?: number
      timestamp?: number
      [key: string]: unknown
    }

// 管理端
export interface AdminAuthData {
  username: string
  token: string
  expires_at: string
  ttl_minutes: number
}

export type AdminAccessLevel = 'global_admin' | 'project_member'

export interface AdminIdentity {
  username: string
  access_level: AdminAccessLevel
  allowed_nav_keys: string[]
  allowed_project_ids: number[]
  allowed_project_codes: string[]
}

export interface PromptsSummary {
  log_type_keys: string[]
  has_default_plan: boolean
  has_default_summary: boolean
  function_keys: string[]
  editable_prompt_count: number
}

export interface PromptEntry {
  id: string
  function_key: string
  function_name: string
  function_description?: string | null
  agent_key: string
  agent_name: string
  agent_description?: string | null
  prompt_key: string
  prompt_label: string
  prompt_type: string
  path: string[]
  content: string
  locale?: string | null
}

export interface PromptsConfigData {
  path: string
  content: string
  updated_at: string
  size: number
  checksum: string
  summary: PromptsSummary
  prompts: PromptEntry[]
}

export interface TestConnectionResult {
  success: boolean
  message: string
  auth_method: string
}

export interface ProjectRepo {
  id: number
  project_code: string
  project_name: string
  repo_url: string
  default_branch: string
  git_token_set: boolean
  has_repo?: boolean
  enabled_agent_keys?: string[]
  project_card: string
  enabled: boolean
  member_count?: number
  created_at: string
  updated_at: string
}

export interface ProjectAgentInfo {
  key: string
  name: string
  display_name: string
  framework: string
  requires_repo: boolean
  description?: string | null
}

export interface ProjectMember {
  id: string
  username: string
  display_name?: string | null
  email?: string | null
}

export interface ProjectRepoPayload {
  project_code?: string
  project_name?: string
  repo_url?: string
  default_branch?: string
  git_token?: string | null
  project_card?: string
  enabled?: boolean
  enabled_agent_keys?: string[]
}

// Bug 修复任务
export type BugFixTaskStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'cancelled'
  | string

export type BugFixMergeRequestStatus =
  | 'created'
  | 'open'
  | 'push_failed'
  | 'mr_failed'
  | string

export interface BugFixProposedFix {
  title?: string
  description?: string
  rationale?: string
  suspected_files?: string[]
  suspected_symbols?: string[]
  [key: string]: unknown
}

export type BugFixOutcomeKind =
  | 'created_mr'
  | 'already_implemented'
  | 'rejected'
  | 'skipped'
  | 'failed'
  | string

export interface BugFixFixOutcome {
  fix_index?: number | null
  title?: string | null
  outcome: BugFixOutcomeKind
  reason?: string | null
  branch_name?: string | null
  mr_url?: string | null
}

export interface BugFixChangedFile {
  path?: string
  file_path?: string
  filename?: string
  name?: string
  additions?: number
  deletions?: number
  insertions?: number
  added?: number
  removed?: number
  [key: string]: unknown
}

export interface BugFixDiffStat {
  files?: number
  file_count?: number
  additions?: number
  deletions?: number
  insertions?: number
  removed?: number
  [key: string]: unknown
}

export interface BugFixMergeRequest {
  id: string
  title: string
  status: BugFixMergeRequestStatus
  branch_name: string
  base_branch: string
  mr_url?: string | null
  mr_iid?: string | null
  commit_sha?: string | null
  changed_files?: BugFixChangedFile[] | Record<string, unknown> | null
  diff_stat?: BugFixDiffStat | null
}

export interface BugFixTaskSummary {
  id: string
  title: string
  project_code?: string | null
  project_name?: string | null
  status: BugFixTaskStatus
  merge_request_count: number
  source_log_id?: string | null
  created_at?: string | null
  finished_at?: string | null
}

export interface BugFixTaskDetail extends BugFixTaskSummary {
  trace_events?: import('./agentTrace').AgentTraceEvent[]
  model?: string | null
  context_availability?: string | null
  summary?: string | null
  source_analysis_task_id?: string | null
  error?: string | null
  started_at?: string | null
  proposed_fixes: BugFixProposedFix[]
  fix_outcomes?: BugFixFixOutcome[]
  merge_requests: BugFixMergeRequest[]
}

export interface BugFixTaskListResponse {
  success: boolean
  data: BugFixTaskSummary[]
  total: number
  page: number
  page_size: number
  message?: string
}

export interface BugFixTaskDetailResponse {
  success: boolean
  data?: BugFixTaskDetail
  message?: string
}

// Agent Skills
export interface AgentSkillAgentInfo {
  key: string
  name: string
  framework: string
  description?: string | null
}

export interface AgentSkill {
  id: string
  name: string
  description: string
  enabled: boolean
  required?: boolean
  source_filename: string
  size_bytes: number
  installed_at?: string | null
  updated_at?: string | null
}

export interface SkillFileNode {
  name: string
  path: string
  type: 'dir' | 'file'
  size?: number
  children?: SkillFileNode[]
}

export interface SkillFileTree {
  name: string
  tree: SkillFileNode
}

export interface SkillFileContent {
  path: string
  size: number
  encoding: 'utf-8' | 'binary'
  content?: string
  truncated: boolean
}

// 项目级追加系统提示词（与 prompts_config.yaml 的 Agent 级基础提示词分级叠加）
export interface ProjectSystemPrompt {
  project_code: string
  // null = 项目共享层（对所有 Agent 生效）；否则为该 Agent 的专属层（含代码工作流）。
  agent_key?: string | null
  content: string
  exists: boolean
  size_bytes: number
  updated_at?: string | null
}

export interface ProjectSystemPromptPreviewLayer {
  key: string
  label: string
  exists: boolean
  size_bytes: number
  updated_at?: string | null
}

export interface ProjectSystemPromptPreview {
  project_code: string
  project_name: string
  agent_key: string
  locale?: string | null
  base_prompt: string
  project_addendum: string
  content: string
  base_chars: number
  addendum_chars: number
  total_chars: number
  layers: ProjectSystemPromptPreviewLayer[]
}

// 用户与会话
export type UserRole = 'user' | 'admin'
export type UserProfileRole = 'developer' | 'tester' | 'product' | 'ops' | 'other' | string

export interface UserProfile {
  id: string
  username: string
  display_name?: string | null
  email?: string | null
  is_active: boolean
  // Admin note left when the account was disabled; shown to the user when
  // their login is rejected. Cleared as soon as the account is re-enabled.
  disabled_message?: string | null
  disabled_at?: string | null
  role?: UserRole | string
  profile_role?: UserProfileRole | null
  language?: string | null
  clarification_enabled?: boolean
  clarification_max_rounds?: number
  clarification_on_timeout?: 'cancel' | 'continue' | string
  last_login_at?: string | null
  created_at: string
  updated_at: string
}

export interface UserAuthPayload {
  token: string
  expires_at: number
  user: UserProfile
}

export interface SystemAnnouncement {
  id: string
  title: string
  content: string
  published_at: string
  published_by: string
  active: boolean
}

export interface AnnouncementDismissData {
  announcement_id: string
  dismissed: boolean
}

export interface ChatSessionSummary {
  id: string
  title: string
  last_message_at: string
  message_count: number
  created_at: string
  updated_at: string
  is_pinned?: boolean
  pinned_at?: string | null
  active_run_id?: string | null
  run_status?: string | null
  run_agent_kind?: string | null
  run_started_at?: string | null
  run_updated_at?: string | null
}

export interface ChatSessionSearchResult extends ChatSessionSummary {
  snippet: string
}

export interface ChatSessionSearchData {
  items: ChatSessionSearchResult[]
  has_more: boolean
}

export interface ReleaseItem {
  id: string
  platform: 'linux' | 'macos' | 'windows'
  version: string
  filename: string
  file_size: number
  description?: string | null
  download_count: number
  created_at: string
}

export interface ChatMessageRecord {
  id: string
  session_id: string
  role: 'user' | 'ai' | 'system'
  content: string
  created_at: string
  updated_at: string
  run_id?: string | null
  run_status?: string | null
  run_agent_kind?: string | null
  trace_events?: unknown[] | null
  /** Metadata for images attached to a user turn; bytes come from the chat-images endpoint. */
  images?: ChatMessageImage[] | null
}

/** One image attached to a user turn, as returned by the history endpoint. */
export interface ChatMessageImage {
  id: string
  media_type: string
  name: string
  size: number
}

// ===== 对话分享（owner 侧分享状态 + 公开只读快照）=====

// Owner-facing share status for a session. The unshared state is represented
// by `is_active: false` with empty token/url, so the UI can render one shape.
export interface ShareInfo {
  is_active: boolean
  token?: string | null
  share_url?: string | null
  shared_at?: string | null
  message_count?: number | null
}

// A single message in the public snapshot. AI turns may additionally carry the
// agent trace (thinking + tool calls) captured at share time; older snapshots
// predating trace capture omit it.
export interface PublicShareMessage {
  role: 'user' | 'ai' | 'system'
  content: string
  created_at?: string | null
  trace_events?: unknown[] | null
}

// Public, unauthenticated snapshot read response (flat, no identity fields).
export interface PublicShareSnapshot {
  title: string
  shared_at: string
  message_count: number
  messages: PublicShareMessage[]
}

// ==================== 系统/用户指标 (Metrics) ====================

export interface MetricsTokenBreakdown {
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  total_tokens: number
}

export interface MetricsStatusCounts {
  succeeded: number
  failed: number
  cancelled: number
  stale: number
  timeout: number
  other: number
}

export interface MetricsTimeSeriesBucket {
  bucket_start: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  total_tokens: number
  invocation_count: number
  success_count: number
  failure_count: number
  counts_by_agent?: Record<string, number>
}

export interface MetricsGroupCount {
  key: string | null
  invocation_count: number
  total_tokens: number
}

export interface MetricsServerTimezone {
  name: string | null
  offset_minutes: number
  offset_label: string
  abbreviation: string | null
}

export interface MetricsChatActivitySummary {
  total_users: number
  active_users: number
  chat_session_count: number
  chat_message_count: number
  run_counts_by_status: Record<string, number>
}

export interface MetricsLogActivitySummary {
  upload_count: number
  uploaded_bytes: number
  counts_by_project: Record<string, number>
  counts_by_status: Record<string, number>
  ai_analysis_counts: Record<string, number>
}

export interface MetricsPackageActivitySummary {
  package_count: number
  total_bytes: number
  counts_by_project: Record<string, number>
  activity_counts: Record<string, number>
  search_count: number
}

export interface MetricsDeviceActivitySummary {
  counts_by_state: Record<string, number>
}

export interface MetricsSystemOverview {
  from_time: string
  to_time: string
  server_timezone: MetricsServerTimezone
  bucket: string
  tokens: MetricsTokenBreakdown
  estimated_cost_usd: number | null
  cost_estimated: boolean
  invocation_count: number
  status_counts: MetricsStatusCounts
  error_count: number
  duration_ms_avg: number | null
  duration_ms_p95: number | null
  invocations_by_source: MetricsGroupCount[]
  invocations_by_agent_kind: MetricsGroupCount[]
  invocations_by_provider: MetricsGroupCount[]
  invocations_by_model: MetricsGroupCount[]
  invocations_by_project: MetricsGroupCount[]
  invocations_by_status: MetricsGroupCount[]
  time_series: MetricsTimeSeriesBucket[]
  chat: MetricsChatActivitySummary
  logs: MetricsLogActivitySummary
  packages: MetricsPackageActivitySummary
  devices: MetricsDeviceActivitySummary
}

export interface MetricsUserRow {
  user_id: string
  username: string | null
  display_name: string | null
  role: string | null
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  total_tokens: number
  estimated_cost_usd: number | null
  run_count: number
  success_count: number
  failure_count: number
  message_count: number
  last_active_at: string | null
  top_agent_kind: string | null
}

export interface MetricsUserListData {
  from_time: string
  to_time: string
  server_timezone: MetricsServerTimezone
  page: number
  per_page: number
  total: number
  sort: string
  rows: MetricsUserRow[]
}

/**
 * An OCR sub-event folded into its parent agent event.
 *
 * Image OCR preprocesses a project-expert / log-analysis / package-search turn
 * and shares that run's `run_id`, so the audit feed merges it into the parent
 * row instead of listing it separately.
 */
export interface MetricsMergedOcrEvent {
  id: string
  occurred_at: string
  source: string
  agent_kind: string | null
  provider: string | null
  model: string | null
  status: string | null
  error_kind: string | null
  duration_ms: number | null
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  total_tokens: number
  cost_microusd: number | null
  image_count: number | null
}

export interface MetricsRawEvent {
  id: string
  idempotency_key: string
  occurred_at: string
  event_type: string
  source: string
  user_id: string | null
  username: string | null
  display_name: string | null
  conversation_available: boolean
  owner_scope: string | null
  session_id: string | null
  run_id: string | null
  task_id: string | null
  log_id: string | null
  project_repo_id: string | null
  agent_kind: string | null
  provider: string | null
  model: string | null
  status: string | null
  error_kind: string | null
  duration_ms: number | null
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  total_tokens: number
  cost_microusd: number | null
  metadata: Record<string, unknown> | null
  /** OCR calls that belong to this same request; empty for image-free turns. */
  ocr_events?: MetricsMergedOcrEvent[]
}

/** Metadata for one image a user attached; bytes come from the admin endpoint. */
export interface AdminConversationImage {
  id: string
  media_type: string | null
  name: string | null
  size: number | null
}

/** The admin view of a message: the public shape plus the turn's attachments. */
export interface AdminConversationMessage extends PublicShareMessage {
  images?: AdminConversationImage[]
}

export interface AdminConversationDetail {
  event_id: string
  session_id: string
  user_id: string
  username: string | null
  display_name: string | null
  title: string
  message_count: number
  created_at: string
  last_message_at: string
  is_deleted: boolean
  messages: AdminConversationMessage[]
}

export interface MetricsUserDetail {
  user_id: string
  username: string | null
  display_name: string | null
  role: string | null
  from_time: string
  to_time: string
  server_timezone: MetricsServerTimezone
  bucket: string
  tokens: MetricsTokenBreakdown
  estimated_cost_usd: number | null
  cost_estimated: boolean
  invocation_count: number
  status_counts: MetricsStatusCounts
  message_count: number
  last_active_at: string | null
  invocations_by_source: MetricsGroupCount[]
  invocations_by_agent_kind: MetricsGroupCount[]
  invocations_by_provider: MetricsGroupCount[]
  invocations_by_model: MetricsGroupCount[]
  errors_by_kind: MetricsGroupCount[]
  time_series: MetricsTimeSeriesBucket[]
  recent_events: MetricsRawEvent[]
}

export interface MetricsRawEventsData {
  from_time: string
  to_time: string
  server_timezone: MetricsServerTimezone
  page: number
  per_page: number
  total: number
  events: MetricsRawEvent[]
}
