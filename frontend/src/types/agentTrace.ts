// AgentTraceEvent — TypeScript schema mirroring app/agents/log_analysis/trace.py.
//
// All events share `type`, `task_id`, `seq` (monotonic), `timestamp` (epoch
// seconds with 6 decimals). The discriminated union below lets callers
// narrow per event type. Optional fields are common-but-not-required so
// component code should treat missing fields defensively.

export type AgentTraceEventType =
  | 'run_start'
  | 'run_complete'
  | 'cancelled'
  | 'step_start'
  | 'step_delta'
  | 'step_end'
  | 'thinking_start'
  | 'thinking_delta'
  | 'thinking_end'
  | 'answer_delta'
  | 'system_notice'
  | 'clarification_request'
  | 'clarification_resolved'
  | 'error'

export type StepStatus = 'ok' | 'error'

export interface TraceSummary {
  thought_duration_seconds: number
  tool_call_count: number
  thinking_chars: number
  [key: string]: unknown
}

interface BaseTraceEvent {
  task_id: string
  seq: number
  timestamp: number
}

export interface RunStartEvent extends BaseTraceEvent {
  type: 'run_start'
  model?: string
  provider?: string
  available_skills?: string[]
  /** @deprecated lifecycle alias for the materialized candidate subset */
  loaded_skills?: string[]
}

export interface RunCompleteEvent extends BaseTraceEvent {
  type: 'run_complete'
  trace_summary?: TraceSummary
  final_text?: string
}

export interface CancelledEvent extends BaseTraceEvent {
  type: 'cancelled'
  trace_summary?: TraceSummary
  message?: string
}

export interface ErrorEvent extends BaseTraceEvent {
  type: 'error'
  trace_summary?: TraceSummary
  error_kind?: string
  message?: string
}

export interface StepStartEvent extends BaseTraceEvent {
  type: 'step_start'
  step_id: string
  tool_name: string
  tool_input?: Record<string, unknown>
}

export interface StepDeltaEvent extends BaseTraceEvent {
  type: 'step_delta'
  step_id: string
  output_chunk: string
}

export interface StepEndEvent extends BaseTraceEvent {
  type: 'step_end'
  step_id: string
  status: StepStatus
  duration_seconds?: number
  output_excerpt?: string
}

export interface ThinkingStartEvent extends BaseTraceEvent {
  type: 'thinking_start'
  step_id: string
}

export interface ThinkingDeltaEvent extends BaseTraceEvent {
  type: 'thinking_delta'
  step_id: string
  text_chunk: string
}

export interface ThinkingEndEvent extends BaseTraceEvent {
  type: 'thinking_end'
  step_id: string
  text?: string
  duration_seconds?: number
}

/**
 * Incremental chunk of the assistant's final answer body. Concatenating
 * every `answer_delta.text_chunk` of a run in `seq` order equals
 * `run_complete.final_text` (under the same masking). Distinct from
 * `thinking_delta` (folded reasoning) and `step_delta` (tool output).
 */
export interface AnswerDeltaEvent extends BaseTraceEvent {
  type: 'answer_delta'
  text_chunk: string
  step_id?: string
}

export interface SystemNoticeEvent extends BaseTraceEvent {
  executor?: 'claude_sdk' | 'antigravity_cli'
  provider?: string
  model?: string
  requested_model?: string
  agent_key?: string
  /** Server-side required Skill activation; distinct from SDK tool calls. */
  name?: string
  load_method?: 'server_prompt'
  sha256?: string
  policy_sha256?: string
  type: 'system_notice'
  kind?: string
  subtype?: string
  detail?: string
  available_skills?: string[]
  /** @deprecated lifecycle alias for the materialized candidate subset */
  loaded_skills?: string[]
}

// ---- AskUserQuestion clarification -------------------------------------

export interface ClarificationOption {
  label: string
  description?: string
}

export interface ClarificationQuestion {
  header?: string
  question: string
  question_key?: string
  multiSelect?: boolean
  allow_custom?: boolean
  required?: boolean
  options: ClarificationOption[]
}

export interface ClarificationRequestEvent extends BaseTraceEvent {
  type: 'clarification_request'
  request_id: string
  questions: ClarificationQuestion[]
  run_id?: string
  session_id?: string
}

export type ClarificationOutcome = 'answered' | 'timeout' | 'cancelled'

export interface ClarificationResolvedEvent extends BaseTraceEvent {
  type: 'clarification_resolved'
  request_id: string
  outcome: ClarificationOutcome
  reason?: string
  run_id?: string
  session_id?: string
}

/** A single question's answer submitted by the user. */
export interface ClarificationAnswer {
  question_index: number
  selected_labels: string[]
  custom_text?: string | null
}

/** Pending clarification stored per-session in the conversation runs store. */
export interface PendingClarification {
  request_id: string
  questions: ClarificationQuestion[]
  session_id?: string
  run_id?: string
  // Per-question working state for the card UI (selected labels + free text).
  draftSelected: string[][]
  draftCustom: string[]
  submitting?: boolean
  error?: string | null
}

export type AgentTraceEvent =
  | RunStartEvent
  | RunCompleteEvent
  | CancelledEvent
  | ErrorEvent
  | StepStartEvent
  | StepDeltaEvent
  | StepEndEvent
  | ThinkingStartEvent
  | ThinkingDeltaEvent
  | ThinkingEndEvent
  | AnswerDeltaEvent
  | SystemNoticeEvent
  | ClarificationRequestEvent
  | ClarificationResolvedEvent

export type TerminalEventType = 'run_complete' | 'cancelled' | 'error'

export function isTerminalEvent(
  event: AgentTraceEvent,
): event is RunCompleteEvent | CancelledEvent | ErrorEvent {
  return (
    event.type === 'run_complete' ||
    event.type === 'cancelled' ||
    event.type === 'error'
  )
}
