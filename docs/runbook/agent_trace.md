# Runbook: Agent Trace Channel

Operational reference for the SSE-based agent trace channel introduced
in the `stream-agent-trace-to-ui` change. Background and schema:
[../agent_trace_protocol.md](../agent_trace_protocol.md).

The trace channel is best-effort: a degraded or missing trace stream
MUST NOT block a successful log analysis result. Use the diagnostics
below in the order they appear; most symptoms can be cleared without
touching the agent loop itself.

---

## OpenAI text and screenshot requests

Select `openai` in Admin → Model settings, use `https://api.openai.com/v1`, and
choose a vision-capable Responses model (the preset is `gpt-6.1-terra`). Existing
`ANTHROPIC_*` configuration names are retained for compatibility. Test connection
uses native Responses; a green probe verifies inference, not the whole tool/UI
path. OpenAI fixes the auxiliary model to the main model and bypasses independent
OCR, even without an OCR key. Allow sufficient output/time budget for image and
tool work. Check `run_start` provider/model, real tool results, terminal status,
and persisted usage in an authenticated screenshot conversation.

The SDK may log `unrecognized_model` for an OpenAI id: this is its local catalogue,
not an upstream model rejection. The selected id must remain unchanged on the
Responses request. The loopback bridge is private and temporary; do not expose
its port or log credentials. An upstream `credit_balance_exhausted` is a failure,
not a reason to buy credits, enable recharge or silently select a paid fallback.
API subscription credits can be used without an API payment method; monitor
their actual balance/expiry in API Platform separately from model token metrics.

## Excessive empty token-progress notices

Group trace events by `type` and `subtype` before attributing a large history to
tool activity. Some SDK streams produce one `thinking_tokens` system message
per reasoning token. The shared translator omits only those whose translated
`detail` is empty; actual thinking text, answer increments, tool evidence and
descriptive notices are retained. This applies to Agents using that translator.
Routing and cancellation still receive every SDK message, and idle SSE
connections retain their existing heartbeat behavior. Old stored traces remain
readable and are not rewritten. Compare meaningful payloads and final answers
when replaying retained evidence; measure runtime separately from event/byte
reduction. Do not count suppressed notices as fewer model calls or tokens.

## Background model errors after changing provider

If main tool calls succeed but SDK `generate_session_title` requests return HTTP
400 for a previous provider's model, compare the effective auxiliary model with
the per-run `ANTHROPIC_DEFAULT_HAIKU_MODEL` and legacy
`ANTHROPIC_SMALL_FAST_MODEL` overrides. The SDK inherits the container environment
unless `build_options` supplies both names from the selected endpoint. Do not
print API keys while checking this mapping. Keep provider-switch trials separate
from earlier timing cohorts, and align these model names in both benchmark arms.
See the [SDK background model aliases](https://code.claude.com/docs/en/model-config#environment-variables).

## Early compaction on official DeepSeek Flash

Check the effective endpoint, SDK version and reported `modelUsage.contextWindow`
before attributing repeated compaction to a provider limit. The pinned SDK
0.2.165 uses 200,000 for plain `deepseek-flash`; the official `[1m]` marker makes
it use 1,000,000 while keeping the outgoing model unchanged. The independently
gated `AGENT_EVIDENCE_EXTENDED_CONTEXT_ENABLED` experiment applies only to Log
Analysis on the verified official endpoint/model. Unknown gateways and models
are not upgraded. See [DeepSeek's integration guide](https://api-docs.deepseek.com/guides/coding_agents/).
Verify generation parameters and endpoint isolation with the actual SDK, retain
all prior results, and freeze a new benchmark cohort before enabling it. Record
compaction time separately; longer context alone does not establish a speedup.

## Repeated reads after a batch source search

With both evidence efficiency and source-context experiments enabled, the same
PostToolUse hook handles Bash line-number searches and `mcp__evidence__search_evidence`.
Check whether the returned matches are complete rows in the supported column
format and match local repository source. Unsupported shapes, excerpted lines,
external paths and already covered ranges retain the ordinary tool result without
extra context. The entire batch shares two windows and an 8000-byte hook budget;
it does not get that budget for each request. Original matches, per-request errors,
pagination and explicitly requested context are preserved. Verify the subsequent
SDK request contains the added context before attributing a missing read to it.
Count actual requests and audit final answers in a fresh cohort; added context
alone proves neither saved calls nor equivalent conclusions.

For short literal Bash `sed -n 'start,endp' source` reads, check the same hook for
`source_read_line` and a complete-function range. Every requested source character
must match stdout; only the SDK's removed terminal LF is normalized. Supported
literal `cd` and separator commands do not change the original command or output.
Pipelines, substitutions, redirections, ambiguous paths, changed source and
over-budget functions retain native behavior without an extra partial excerpt.
A previously added partial range must not suppress the function's missing branch.
Verify both the original read and the added function in the next SDK request;
offline replay and wire delivery are separate from a measured speedup.

The same hook also handles short `mcp__evidence__read_evidence` source ranges.
Check request/result indices, canonical repository path and every numbered row;
only complete, zero-column ranges below 80 requested lines qualify. Tail reads,
changed/truncated rows and already complete functions add no context. Original
results, per-request failures and continuations stay intact, and all requests
share the existing budget and cross-tool coverage state. Verify the real SDK
receives both the original result and the missing function branch; offline replay
counts context opportunities, not saved requests or accepted analysis results.

## Relative-path failures across evidence stages

For the optional staged experiment, distinguish retained SDK conversation history
from shell state. An offline SDK 0.2.165 probe confirms that Bash calls within a
stage retain `cd`, while the resumed stage starts at the original workspace.
Check the preceding Bash command and stage notice before interpreting a missing
relative path. The phase protocol directs Bash reads/searches to workspace-based
absolute paths and Git operations to `git -C`; it does not rewrite commands or
remove tools. Count failed path lookups and corrective requests separately.
The offline reproduction establishes the mechanism, not a measured model speedup
or answer-equivalence result.

## Upload rejected before the SSE stream starts

For `/ai-chat/log-analysis/stream`, `/project-expert/stream` and
`/package-search/stream`, first check the HTTP status. HTTP 413 with
`image_too_large` identifies the image size and configured per-image limit;
`multipart_field_too_large` identifies the legacy text-field capacity. These
requests have not started an agent run. Refresh older pages to use `image_files`,
compress the indicated image, shorten oversized messages/history, or start a new
conversation as directed by the message. Image limits use the effective
`OCR_MAX_IMAGE_MB` and `OCR_MAX_IMAGES` runtime settings; do not change nginx or
restart workers based only on a parser error.

If nginx returns an HTML 413, inspect its `client_max_body_size` and any upstream
proxy limit. The UI provides an upload-size explanation even without a JSON
body; it cannot report a numeric proxy limit unless the proxy supplies one.
Malformed inputs return an explicit HTTP 400. A browser transport failure is
reported separately from these HTTP responses.

When deploying the binary upload contract, update the backend before the
frontend. When rolling back, reverse this order. Old frontends remain supported
by the backend's legacy `images` field.

---

## Symptom 1 — Redis unavailable / trace SSE returns no events

**What users see**

- Log detail page opens, AI analysis is running, but the trace card
  list stays empty.
- Refreshing after the task finishes still shows traces (because they
  were persisted to `LogRecord.ai_analysis_result.trace_events`),
  confirming the problem is the in-flight Redis path only.
- Chat trace stream (`/ai-chat/log-analysis/stream`) is unaffected
  because it does not depend on Redis.

**Where to look**

1. App logs for `TraceBuffer: ...` lines.
   - `failed to build redis client`: client construction failed —
     `redis` package missing or invalid `app.config.settings.redis_*`.
   - `pipeline write failed task_id=...`: connection succeeded once
     but later writes are failing; Redis is likely under memory
     pressure or being restarted.
   - `read failed task_id=...`: SSE endpoint cannot poll; usually
     Redis network blip.
2. Redis health:
   - `redis-cli -h $HOST -p $PORT ping` — expect `PONG`.
   - `redis-cli ... INFO memory` — if `used_memory_rss` is close to
     `maxmemory`, eviction can drop trace keys.
3. Confirm a key exists for an in-flight task:
   - `redis-cli ... KEYS 'ai_analysis:trace:*'`
   - `redis-cli ... LLEN ai_analysis:trace:{task_id}` — should grow as
     the agent runs.

**Resolution**

- If Redis is fully down: trace channel for in-flight runs is lost;
  finished runs still serve traces from the DB. Confirm the agent
  itself completed successfully (`LogRecord.ai_analysis_status`).
- If pipeline writes are timing out: bump `socket_timeout` /
  `socket_connect_timeout` in [agent_trace_redis.py](../../app/services/agent_trace_redis.py)
  *temporarily*, find the root cause (Redis CPU, network), revert.
- If Redis is full: free memory or raise `maxmemory`; agent trace
  keys carry `EXPIRE 3600` so they should reap themselves within the
  hour.
- Do **not** kill the Celery worker to recover trace — the analysis
  result is the authoritative output and it is unaffected.

---

## Symptom 2 — SSE connection drops mid-stream

**What users see**

- A trace card list that froze mid-run, often after ~30–60s of an
  idle long-running tool (e.g. `git clone`, large `Read`).
- Browser network panel shows the SSE connection closed with no
  `done` / `run_complete` frame.

**Where to look**

1. Confirm the agent is still alive by checking the Celery task
   state (`celery -A app.celery_app inspect active`) or, for the chat
   path, the in-memory `AgentJob` state. If the agent finished but
   the client never received `run_complete`, the drop is downstream.
   For Log Analysis and Project Expert chat, distinguish Agent `run_complete`
   from the saved-answer `done` event. Optional initial-title generation now runs
   after answer completion and can finish later; a title timeout alone does not
   mean the analysis failed. Record pre-Agent, Agent, persistence and optional
   metadata timings separately when investigating a slow completion.
   If health checks also stall while the workbench is open, inspect process
   memory and database query payloads. Sidebar status polling must only select
   run metadata, not historical trace/answer bodies. Preserve the interrupted
   run and compare database, SDK and client timestamps before assigning cause.
2. Reverse proxy / load balancer timeouts — most common cause.
   - `nginx` default `proxy_read_timeout` is 60s. The trace stream
     emits a `system_notice{kind: "heartbeat"}` frame every ≥ 15s of
     silence, but only if the upstream sends. Confirm
     `proxy_read_timeout` is **≥ 120s** for the trace endpoints.
   - Check for buffering: `proxy_buffering off;` MUST be set on
     `*/trace/stream` and `*/log-analysis/stream` location blocks.
3. Browser side: confirm the EventSource was not garbage-collected
   (`AIChat.vue` keeps it on the message; `LogDetail.vue` closes it
   when the component unmounts).
4. Heartbeat presence: with the developer tools open, you should
   see `event: agent_trace\ndata: {"type":"system_notice","kind":"heartbeat",...}`
   at least every 15s during long tool calls.

**Resolution**

- Raise the upstream `proxy_read_timeout` to a value greater than the
  longest realistic tool duration (e.g. 300s for `git clone` against
  slow remotes).
- Ensure `proxy_buffering off;` and `X-Accel-Buffering: no` are set;
  the FastAPI route also emits the header but a misconfigured proxy
  can still buffer.
- The frontend will automatically reconnect (chat) or display the
  finalised result on reload (log detail) — no server-side action is
  needed once the proxy is fixed.

---

## Symptom 3 — Events appear out of order or are missing in the UI

**What users see**

- Tool card output looks chopped or shows the wrong status (e.g.
  `running` after the task is clearly finished).
- `trace_summary.tool_call_count` in `run_complete` does not match
  the number of cards rendered.

**Where to look**

1. **Always check `seq` first.** Inspect a few raw frames in the
   browser network panel — they MUST be strictly monotonic per
   `task_id`. The frontend
   [`useAgentTraceStream`](../../frontend/src/composables/useAgentTraceStream.ts)
   re-orders and de-dupes by `seq`, so client-side display order
   should match `seq` regardless of arrival order.
2. Backend buffer integrity:
   - Chat: `AgentJob.trace_events` should match `AgentJob.events`
     filtered by `event == "agent_trace"` (same payloads).
   - Celery: `LRANGE ai_analysis:trace:{task_id} 0 -1` should yield a
     `seq`-increasing JSON sequence (use `redis-cli ... | jq .seq`).
3. List truncation: if `LLEN ai_analysis:trace:{task_id} == 2000`,
   the bounded `LTRIM` dropped early events. This is by design but
   may indicate a runaway agent — check the task's tool call count
   in the analysis result.
4. Reconnect contract: when the client passes `?from_seq=N`, the
   endpoint serves only `seq > N`. A bug at the call site (e.g.
   passing the wrong cursor) will look like missing events. Confirm
   the query string by reproducing manually:

   ```bash
   curl -N -H "Authorization: Bearer $TOKEN" \
     "https://.../api/v1/logs/{log_id}/ai-analysis/trace/stream?from_seq=0"
   ```

5. Persistence check (post-task): after a run completes, the events
   are written to `LogRecord.ai_analysis_result.trace_events`. If
   that field is missing or empty but the task succeeded, the agent
   wrote the legacy fields only — check
   [app/tasks/ai_analysis.py](../../app/tasks/ai_analysis.py) hasn't
   been refactored to drop the trace plumbing.

**Resolution**

- Missing because of `LTRIM`: increase `MAX_TRACE_EVENTS` in
  [agent_trace_redis.py](../../app/services/agent_trace_redis.py)
  (current cap 2000) — only as a stopgap; investigate why the agent
  is producing so many events.
- Missing because of `from_seq` mishandling: file a bug against the
  client; the SSE endpoint contract is `seq > from_seq` so the
  client should remember the highest `seq` seen and pass it on
  reconnect.
- Out of order arrival but correct render: nothing to do; the
  frontend composable handles re-ordering.
- Out of order arrival AND wrong render: capture the raw frames and
  attach to the bug; suspect a regression in
  `LogAnalysisAgent._RunState.seq_counter`.

---

## Quick checks cheat sheet

| Check                                                | Command                                                                       |
| ---------------------------------------------------- | ----------------------------------------------------------------------------- |
| Redis liveness                                       | `redis-cli -h $HOST -p $PORT ping`                                            |
| List length for one task                             | `redis-cli ... LLEN ai_analysis:trace:{task_id}`                              |
| Dump events for one task                             | `redis-cli ... LRANGE ai_analysis:trace:{task_id} 0 -1`                       |
| Trace channel SSE smoke test (token required)        | `curl -N -H 'Authorization: Bearer …' '.../logs/{id}/ai-analysis/trace/stream'` |
| Persisted events for a finished task                 | `select ai_analysis_result -> 'trace_events' from log_records where id = …;`  |

When investigating the Skill chips, do not treat a lifecycle
`loaded_skills` field as proof of invocation. New traces expose the complete
enabled Agent/project catalog as `available_skills`; actual use requires a
`step_start` whose `tool_name` is
`Skill`. See the protocol's “Skill availability and actual use” section.

For schema details, sequencing invariants, and the cancellation
contract, see [agent_trace_protocol.md](../agent_trace_protocol.md).

For the default-off isolated review experiment, confirm both the complete evidence snapshot and `AGENT_EVIDENCE_FINAL_REVIEW_ENABLED=true`, then set `AGENT_EVIDENCE_FINAL_REVIEW_MODE=isolated` only in the authorized validation environment. Verify the primary has no Stop review and the verifier has no tools or project/session continuation, uses the effective primary model/endpoint and includes complete required Skills. Inspect safe verification start/complete notices and authoritative final text; do not infer success from a streamed draft. Invalid edits must fail without run_complete; cancelling a blocked verifier must close its generator. Compare fresh API totals including verification, preserve failed runs and review native evidence before expanding timing tests. Never dump whole raw results or tool traces to check this mode.
