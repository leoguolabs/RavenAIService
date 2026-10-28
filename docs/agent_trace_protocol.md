# Agent Trace Protocol

Unified event schema for streaming Claude Agent SDK internal messages
(assistant text, thinking, tool_use, tool_result, system) out to the
frontend. One event type, two transport channels, same TypeScript and
Python definitions on both sides.

- Python source of truth: [app/agents/log_analysis/trace.py](../app/agents/log_analysis/trace.py)
- TypeScript mirror: [frontend/src/types/agentTrace.ts](../frontend/src/types/agentTrace.ts)

## Event shape

Every event is a flat object with at minimum:

| Field        | Type    | Description                                                                 |
| ------------ | ------- | --------------------------------------------------------------------------- |
| `type`       | string  | One of the 12 event types listed below.                                     |
| `task_id`    | string  | Identifies the agent run. Chat uses `session_id`; Celery uses Celery task ID. |
| `seq`        | integer | Monotonic, starts at 1, never repeats within one run. Used for ordering & de-dup. |
| `timestamp`  | float   | Epoch seconds, 6 decimal places.                                            |

Per-type fields are listed in the table below. Optional fields are
omitted from the payload when empty, so consumers must handle missing
keys gracefully.

| `type`            | Per-type fields                                                                                                              | Notes                                                                                  |
| ----------------- | ---------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `run_start`       | `model`, `provider`, `available_skills`; deprecated alias `loaded_skills`                                                    | Emitted once, before any SDK message is processed.                                     |
| `run_complete`    | `trace_summary`, `final_text`                                                                                                | Terminal. Emitted on successful exit.                                                  |
| `cancelled`       | `trace_summary`, `message`                                                                                                   | Terminal. Always preceded by a `system_notice{kind: "cancel_requested"}`.              |
| `error`           | `trace_summary`, `error_kind`, `message`                                                                                     | Terminal. Emitted on unhandled exception.                                              |
| `step_start`      | `step_id`, `tool_name`, `tool_input`                                                                                         | UUIDv4 `step_id` ties the start/delta/end triple together.                             |
| `step_delta`      | `step_id`, `output_chunk`                                                                                                    | One or more per step; `output_chunk` is ≤ 4 KB UTF-8.                                  |
| `step_end`        | `step_id`, `status` (`ok`/`error`), `output_excerpt`, `duration_seconds`                                                     | `output_excerpt` is ≤ 4 KB UTF-8.                                                      |
| `thinking_start`  | `step_id`                                                                                                                    | Thinking blocks share the same `step_id` namespace as tool steps but a different kind. |
| `thinking_delta`  | `step_id`, `text_chunk`                                                                                                      | `text_chunk` is ≤ 4 KB UTF-8.                                                          |
| `thinking_end`    | `step_id`, `text`, `duration_seconds`                                                                                        | `text` is the full thinking text (≤ 4 KB excerpt).                                     |
| `answer_delta`    | `text_chunk`, `step_id` (optional)                                                                                           | Incremental chunk of the assistant's **final answer body** (≤ 4 KB UTF-8). Concatenating every `answer_delta.text_chunk` of a run in `seq` order MUST equal `run_complete.final_text` (under the same masking). Distinct from `thinking_delta` (folded reasoning) and `step_delta` (tool output). |
| `system_notice`   | `kind`, `subtype`, `detail`                                                                                                  | Used for `heartbeat`, `cancel_requested`, SDK system messages, and `subtype: "endpoint_switch"` (see below). |
| `clarification_request`  | `request_id`, `questions`, `run_id`, `session_id`; optional `mandatory`, `purpose`, `plan_hash`                       | The agent or service paused to ask the user. Blocks until `POST /chat/clarifications/{request_id}/resolve`, timeout, or run end. Configuration Manager packaging uses `mandatory=true`, `purpose="package_build_confirmation"`. |
| `clarification_resolved` | `request_id`, `outcome` (`answered`/`timeout`/`cancelled`/`rejected`), `reason`; optional `mandatory`, `purpose`        | Always follows its `clarification_request`; clears the question card.                   |

The shared SDK translator omits `thinking_tokens` system notices when their
translated `detail` is empty. These per-token progress messages contain no
answer, reasoning text or counter in this protocol. Descriptive notices, actual
`thinking_*` / `answer_delta` content, tool evidence and other system subtypes
remain unchanged. SDK messages still reach routing and cancellation checks;
idle transports retain their regular heartbeats. Sequence numbers count emitted
events, so new traces can be smaller without requiring a schema change.

The default-off staged-evidence experiment emits `system_notice` with
`kind: "evidence_stage"` and `phase: "collection" | "finalization"`.
Collection assistant content remains in the ordinary thinking/tool trace, but
its partial text does not enter `answer_delta`. The router and cancellation
checks still see every SDK frame. Only the final stage's terminal result is
parsed/persisted; a first-stage SDK or structured analysis error terminates
normally through the existing error handling. There is one run lifecycle and
one total timeout/turn budget, not a fresh run for each stage.

### Skill availability and actual use

`available_skills` contains the complete enabled Skill catalog for the selected
Agent/project; it is independent from the smaller request-relevant subset
materialized into the run workspace. The compatibility field `loaded_skills`
may carry that filtered subset; it does **not** prove that the model read or used
those Skills. A `system_notice` with `kind: "skills_available"` reports the
enabled catalog.

The trace UI keeps the full available catalog collapsed by default and shows its
item count; users can expand it without changing the Skills exposed to the model.

On-demand loading is represented by a normal `step_start` event whose `tool_name`
is `Skill`; the invoked Skill name is read from `tool_input.skill`. The frontend
therefore displays “Available Skills” and “Loaded Skills” separately. It still
accepts legacy `skills_loaded` notices and lifecycle `loaded_skills` as
availability data when replaying older traces.

Required response policies have a distinct `system_notice` with
`kind: "required_skill_loaded"`, `name`, `load_method: "server_prompt"`,
`sha256` (full upstream SKILL.md bytes) and `policy_sha256` (integration prompt).
Log Analysis and Project Expert emit one after server-side full loading on every
run, before the model request. The UI includes it in Loaded Skills, without
creating a fake tool step or increasing tool-call counts. This proves prompt
loading, not that the model made a Skill tool call or achieved a writing score.

### Clarification (AskUserQuestion)

Any chat-facing agent — `device`, `log_analysis`, `project_expert`,
`package_search` — may pause mid-run and ask the user to disambiguate an
unclear instruction. The capability is gated on the user's **global**
preference (`User.clarification_enabled` / `_max_rounds` / `_on_timeout`),
never on which agent is running; wiring lives in one place,
[app/agents/clarification.py](../app/agents/clarification.py), and each agent
calls `ClarificationBinding.setup()` exactly once per run.

Configuration Manager has one deliberate exception: when component files are
submitted for full-package creation, the **service** emits a mandatory
clarification before the Agent can build or publish. It uses the same broker,
resolve endpoint, trace schema, card, replay, and shared `SeqCounter`, but is
independent of `User.clarification_enabled` and requires the project plus every
input-to-component mapping to be answered. A partial/timeout answer terminates
the side-effect path.

Two consequences worth knowing when adding a new agent or transport:

- The ask tool MUST emit onto the run's own `SeqCounter`. A second counter
  produces duplicate `(run_id, seq)` keys and the browser's replay de-duper
  silently drops the question card.
- `clarification_*` events carry `run_id` explicitly because the answer is
  routed back through `chat_run_service`'s broker registry, keyed by `run_id`.
  An agent with no `run_id` cannot ask, and `setup_clarification` returns
  `None` for it rather than exposing a tool nobody can answer.

Agents that run their SDK loop off the FastAPI event loop (the three
workspace agents go through `asyncio.to_thread → run_sync → asyncio.run`)
resolve across loops; `PermissionBroker` handles that internally via
`call_soon_threadsafe`.

### Endpoint switching mid-run

[app/agents/routed_query.py](../app/agents/routed_query.py) may abandon the
chosen endpoint and restart on the next candidate — on a connection/auth/API
rejection, or when no first token arrives within
`MODEL_ROUTER_FIRST_TOKEN_DEADLINE_MS`. Two consequences a consumer must
tolerate, both a deliberate trade for never leaving the user on a dead screen:

- **The CLI's own `init`/`status` notices can appear more than once per run.**
  They are forwarded as they arrive rather than buffered until the first token,
  so an abandoned attempt has already emitted its pair. A
  `system_notice{subtype: "endpoint_switch"}` is emitted immediately before the
  second set and explains it; `detail` is user-facing text and the underlying
  `data` carries `from_slot`, `to_slot`, `reason`
  (`first_token_deadline` / `hard_failure` / `billing_error` / …) and
  `waited_ms`.
- **`run_start.model` can be stale.** It is emitted before the stream opens, so
  a run that switched was served by a different model than the one named. The
  authoritative value is the model reported on the terminal event, which the
  agent updates from `on_endpoint`.

Some compatible gateways encode a request rejection as assistant text, for
example `API Error: 402 Insufficient Balance`, before the SDK raises a generic
`error result: success`. The router quarantines that narrow, anchored response
until structured SDK metadata or the terminal result confirms it. A confirmed
rejection is not emitted as `answer_delta`; it remains pre-commit and can switch
endpoints safely. If every candidate fails, the terminal error preserves the
sanitized status and provider detail. Similar prose in a successful response is
released unchanged and in order.

Switching never happens once the first genuine model frame has arrived — at
that point tools may have run, so `seq` continuity and the
single-terminal-event invariant below are unaffected.

### Invariants

- `seq` is strictly monotonic within one `task_id`. Consumers MUST
  de-dupe by `seq` and re-order if they receive events out of order.
- Every `step_id` MUST eventually see a `step_end` (or `thinking_end`).
  On cancel/error, the agent emits a synthetic terminating event for
  any in-flight `step_id`.
- Token-bearing URLs (e.g. `https://abc@host/repo.git`) are masked to
  `https://***@host/repo.git` before being placed into any field. The
  Python helper `mask_input` walks tool inputs recursively.
- A terminal event (`run_complete` / `cancelled` / `error`) is always
  emitted exactly once, and is always the last event for that run.
- `answer_delta` events are produced only when the active provider
  supports SDK chunked streaming (`include_partial_messages`). When it
  does not, the run emits **no** `answer_delta` and the client falls
  back to rendering `run_complete.final_text` in one shot. Either way
  `run_complete.final_text` is the authoritative full answer body and
  is used for persistence and reconnect replay correction.

## Trace summary

Every terminal event carries a `trace_summary` object with at minimum:

| Field                       | Type    | Description                                                |
| --------------------------- | ------- | ---------------------------------------------------------- |
| `thought_duration_seconds`  | float   | Wall-clock span between first and last event of the run.    |
| `tool_call_count`           | integer | Number of `step_end` events emitted.                        |
| `thinking_chars`            | integer | Sum of `thinking_delta.text_chunk` lengths (chars, not bytes). |

The Python helper `trace.summarize(events)` computes this from a raw
event list and is also used as the canonical fallback when the agent
exits without a terminal event.

## Example event sequence

A minimal happy path showing one `Bash` call followed by completion:

```jsonc
{"type":"run_start","task_id":"job-7","seq":1,"timestamp":1747700000.000001,"model":"claude-opus-4-7","provider":"anthropic"}
{"type":"step_start","task_id":"job-7","seq":2,"timestamp":1747700000.105,"step_id":"3f...","tool_name":"Bash","tool_input":{"command":"ls -la"}}
{"type":"step_delta","task_id":"job-7","seq":3,"timestamp":1747700000.220,"step_id":"3f...","output_chunk":"total 0\nfile-a\n"}
{"type":"step_delta","task_id":"job-7","seq":4,"timestamp":1747700000.240,"step_id":"3f...","output_chunk":"file-b\n"}
{"type":"step_end","task_id":"job-7","seq":5,"timestamp":1747700000.260,"step_id":"3f...","status":"ok","duration_seconds":0.16,"output_excerpt":"total 0\nfile-a\nfile-b\n"}
{"type":"answer_delta","task_id":"job-7","seq":6,"timestamp":1747700000.300,"text_chunk":"目录下"}
{"type":"answer_delta","task_id":"job-7","seq":7,"timestamp":1747700000.310,"text_chunk":"有两个文件。"}
{"type":"run_complete","task_id":"job-7","seq":8,"timestamp":1747700000.320,"final_text":"目录下有两个文件。","trace_summary":{"thought_duration_seconds":0.32,"tool_call_count":1,"thinking_chars":0}}
```

A cancellation always produces the two-step pattern:

```jsonc
{"type":"system_notice","task_id":"job-7","seq":42,"timestamp":...,"kind":"cancel_requested"}
{"type":"cancelled","task_id":"job-7","seq":43,"timestamp":...,"trace_summary":{...}}
```

## Transport channels

Two SSE channels exist; both carry the same event schema. Pick by
entry point.

### 1. Chat entry — in-process

- Endpoint: `POST /api/v1/ai-chat/log-analysis/stream`
- Frame: each event is sent as an SSE message with `event:` set to
  `agent_trace` and a JSON payload that is the event object itself.
  Existing event types (`log_analysis_status`, `session`, `done`,
  `error`) are unchanged — older clients ignore `agent_trace` frames
  and still receive the final `done`.
- For Log Analysis and Project Expert chat, optional initial-title generation
  follows durable answer publication and terminal run finalization. It may still
  be pending after the answer stream closes; it does not add trace events or
  change the completed analysis result.
- Storage: events live in `AgentJob.trace_events` (in-memory). The
  `_subscribe` loop replays the buffer to a reconnecting client so
  late subscribers still get the full history.
- Heartbeat: if no event has been emitted for ≥ 15s while the run is
  active, the subscriber loop synthesises a
  `system_notice{kind: "heartbeat"}` frame to keep proxies / browsers
  from idling the connection out.

### 2. Log detail entry — Redis-backed

- Endpoint: `GET /api/v1/logs/{log_id}/ai-analysis/trace/stream`
- Frame: same as chat, but the channel is dedicated to trace events
  (no `log_analysis_status` mixed in).
- Storage:
  - In-flight: `app.services.agent_trace_redis.TraceBuffer` writes each
    event to the Redis list `ai_analysis:trace:{task_id}` via a pipeline
    of `RPUSH` + `LTRIM 0 1999` + `EXPIRE 3600`. Read side uses
    `LRANGE` polled every ≤ 250ms; no `BLPOP`/`BRPOP` so events are
    never consumed.
  - Finished: the full event list and summary are written to
    `LogRecord.ai_analysis_result.trace_events` /
    `LogRecord.ai_analysis_result.trace_summary`. The endpoint reads
    from the DB once the Celery task is done.
- Reconnection: the client passes `?from_seq=N` to request only
  events with `seq >= N`. With Redis still warm, this serves an
  incremental replay; once the task is done, it serves the relevant
  slice of the persisted list.
- 404 contract: if `log_id` has no `ai_analysis_task_id` the endpoint
  returns HTTP 404 and does NOT open the SSE stream.

## Backward compatibility

- `LogRecord.ai_analysis_result.tool_trace` is **still produced** by
  the backend, derived from `trace_events` via `derive_tool_trace()`.
  The shape (`[{name, input, output_excerpt}, ...]`) is unchanged and
  guarded by [tests/agents/log_analysis/test_legacy_tool_trace_snapshot.py](../tests/agents/log_analysis/test_legacy_tool_trace_snapshot.py).
- Older frontends without an `agent_trace` handler still see the
  existing SSE events (`log_analysis_status`, `done`, etc.) and still
  read `tool_trace` from the persisted analysis result.
- The TypeScript discriminated union (`AgentTraceEvent`) is the only
  shared schema between the two channels — keep both ends aligned
  when adding new event types or fields.

## User-facing model errors

The workbench formats standalone SDK/API failure messages with
`frontend/src/utils/agentErrorMessage.ts` before rendering, copying, or exporting
an assistant message. This also applies to historical conversation messages.
The active UI locale selects the explanation; HTTP status and a bounded,
alphanumeric request ID remain available for support. Upstream 5xx errors do
not imply a specific timeout cause or prove that automatic retries occurred.
Rate limits, billing, authentication, request size, and turn limits have separate
messages. Ordinary answers and quoted diagnostic examples are left unchanged.
Raw persisted errors and server diagnostics are retained; this presentation
change does not alter the trace schema or endpoint retry policy.
