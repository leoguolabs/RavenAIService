# System & User Metrics

Auditable AI-token-usage and business-activity metrics for the Raven backend.

Every AI invocation and selected business event is persisted as one row in the
`metric_events` table — the single auditable fact source — and exposed through
admin/self HTTP APIs plus a low-cardinality Prometheus `/metrics` endpoint.

With Magpie enabled, shared Agent traces record the actual executor and gateway
route headers. Terminal metadata and routed-query callbacks use the last served
provider/model, not the requested group. A run may span multiple model routes;
its cumulative token count is still one run total, not a per-provider billing
breakdown. Use Magpie usage records for request-level accounting. Antigravity
structured usage maps into the existing token counters; choosing the CLI does
not imply that a Google subscription was used.

For provider `openai`, the shared Responses bridge maps actual response usage to
the existing SDK counters: cached input is separated from uncached input and
output includes billed reasoning tokens. These counters are recorded under the
existing agent run; native screenshot input does not produce an `ocr` event.
The SDK context-count hint is a conservative byte upper bound, not billable
usage. Monetary estimates require a matching provider/model pricing entry;
they do not establish the account's promotional credit balance or charges.

RavenClient capability routes include `api_protocol` (`openai_responses` or
`anthropic_messages`). The desktop uses it to select the corresponding native
client while keeping synchronized credentials out of persisted Redux state.

- Model / API schemas: [app/models/metrics.py](../app/models/metrics.py)
- Aggregation + recording service: [app/services/metrics_service.py](../app/services/metrics_service.py)
- HTTP APIs: [app/api/admin_metrics.py](../app/api/admin_metrics.py)
- Prometheus collectors: [app/utils/metrics.py](../app/utils/metrics.py)
- Historical backfill: [scripts/backfill_metric_events.py](../scripts/backfill_metric_events.py)
- Admin dashboard page: `/admin/metrics` in the web console
  ([frontend/src/views/AdminMetrics.vue](../frontend/src/views/AdminMetrics.vue))

---

## 1. Metrics HTTP APIs

Five read-only endpoints. The four `/admin/metrics/*` endpoints reuse the
existing **admin bearer** auth (`require_admin`); the self endpoint reuses the
**user bearer** auth (`get_current_user`) so a user can never read another
user's usage.

| Method & path | Auth | Purpose |
| --- | --- | --- |
| `GET /admin/metrics/overview` | admin | System-wide token / invocation rollup + business summaries |
| `GET /admin/metrics/users` | admin | Per-user ranking with sorting + pagination |
| `GET /admin/metrics/users/{user_id}` | admin | Single-user detail (series, distributions, recent events) |
| `GET /admin/metrics/events` | admin | Raw (sanitized) event audit feed |
| `GET /api/v1/users/me/metrics` | user | The caller's own metrics only |
| `POST /api/v1/client-ai/usage` | user | Idempotently record one direct RavenClient Assistant invocation |

A request to any `/admin/metrics/*` endpoint without a valid admin bearer token
returns `401`/`403` and no metrics payload. An unknown `user_id` on the detail
endpoint returns `404`.

### 1.1 RavenClient direct-Assistant usage

RavenClient Assistant prompts and responses go directly from the desktop to
the provider selected by the RavenAIService model router. After an invocation,
the desktop posts only a UUID, provider/model/slot, terminal status, bounded
token counters, duration, TTFT, and a low-cardinality error category to
`POST /api/v1/client-ai/usage`.

The endpoint derives `user_id` from the bearer token and records source
`raven_client_assistant`. Its strict schema forbids extra fields, so prompt
text, answers, tool input/output, attachments, credentials, and arbitrary
metadata are rejected with `422` instead of being stored. The idempotency key
is scoped by authenticated user and invocation UUID; retrying a report never
double-counts. These values are client-reported operational/accounting data,
not tamper-proof billing facts.

The companion `GET /api/v1/client-ai/capabilities` response contains effective
upstream credentials and is excluded from request logging. It is returned with
private/no-store headers and must be served over TLS outside local development.

### 1.2 Shared query parameters

All endpoints share the same parsing/validation helpers in
[app/api/admin_metrics.py](../app/api/admin_metrics.py), so defaults and bounds
are enforced uniformly and a single call can never trigger an unbounded
full-table scan.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `from` | ISO-8601 | `to - 7d` | Inclusive lower bound. Trailing `Z` accepted; tz-aware values are converted to naive UTC to match the persisted `occurred_at`. |
| `to` | ISO-8601 | now (UTC) | Exclusive upper bound. |
| `bucket` | `hour` \| `day` | `day` | Time-series granularity (overview / detail / self only). Any other value falls back to `day`. |
| `page` | int ≥ 1 | `1` | Pagination (users / events). |
| `per_page` | int | `20` users / `50` events | Clamped to `[1, 200]`. |
| `sort` | string | `total_tokens` | Ranking key for the users list. |
| `event_type`, `source`, `user_id` | string | — | Optional equality filters on `GET /admin/metrics/events`. |

#### Date range semantics

- The window is **`[from, to)`** — `from` inclusive, `to` exclusive — so
  adjacent windows tile without double-counting an event on the boundary.
- Omitting both `from` and `to` yields **the last 7 days**.
- Omitting only `from` defaults it to `to - 7 days`.
- `from` **must** be earlier than `to`, otherwise `400`.
- The maximum queryable window is **366 days**. A wider request is silently
  clamped to the last 366 days measured back from `to` (not rejected), so an
  over-broad call still returns a bounded, well-defined result.
- All timestamps in requests and responses are **UTC**. `occurred_at` is
  persisted naive-UTC; a tz-aware `from`/`to` is normalized to naive UTC before
  comparison.

#### Bucket options

`bucket` controls the granularity of `time_series` only (it does not affect
totals or distributions):

- `day` — one bucket per UTC calendar day (default).
- `hour` — one bucket per UTC hour. Prefer this only for short windows; over a
  366-day range it produces up to ~8,784 buckets.

Each `time_series` entry (`TimeSeriesBucket`) carries `bucket_start` plus the
four token counters, `total_tokens`, and `invocation_count` / `success_count` /
`failure_count` for that bucket.

### 1.2 Response field meanings

All endpoints return the standard `BaseResponse` envelope with the payload under
`data`.

**Token breakdown** (`TokenBreakdown`, present on every aggregate):

| Field | Meaning |
| --- | --- |
| `input_tokens` | Prompt / input tokens. |
| `output_tokens` | Generated / completion tokens. |
| `cache_read_tokens` | Tokens served from the provider prompt cache. |
| `cache_write_tokens` | Tokens written to the provider prompt cache. |
| `total_tokens` | `input + output + cache_read + cache_write`. Missing usage normalizes every counter to `0`. |

**Cost** (see [§3](#3-optional-cost-estimates)):

- `estimated_cost_usd` — estimated USD cost, or `null` when no pricing is
  configured for the relevant provider/model. **An estimate, not a bill.**
- `cost_estimated` — `true` when at least one contributing event had a usable
  price.

**Overview** (`GET /admin/metrics/overview` → `SystemOverview`):

- `tokens`, `estimated_cost_usd`, `cost_estimated`.
- `invocation_count`, `status_counts` (`succeeded` / `failed` / `cancelled` /
  `stale` / `timeout` / `other`), `error_count`.
- `duration_ms_avg`, `duration_ms_p95` (null when no durations recorded).
- `invocations_by_source` / `_by_agent_kind` / `_by_provider` / `_by_model` /
  `_by_status` — each a list of `{key, invocation_count, total_tokens}`
  (`key == null` means unknown).
- `time_series` — list of `TimeSeriesBucket` for the requested bucket.
- Business summaries: `chat`, `logs`, `packages`, `devices` (see below).

**Users list** (`GET /admin/metrics/users` → rows of `UserMetricsRow`):
`user_id`, `username`, `display_name`, `role`, token breakdown,
`estimated_cost_usd`, `run_count`, `success_count`, `failure_count`,
`message_count`, `last_active_at`, `top_agent_kind`. Rows are sorted by `sort`;
`page` / `per_page` / `total` describe the page.

**User detail** (`GET /admin/metrics/users/{user_id}` → `UserMetricsDetail`):
identity + token breakdown + cost, `invocation_count`, `status_counts`,
`message_count`, `last_active_at`, the four `invocations_by_*` distributions,
`errors_by_kind`, `time_series`, and `recent_events` (sanitized `RawMetricEvent`
list).

**Self** (`GET /api/v1/users/me/metrics` → `SelfMetricsSummary`):
the caller's own `user_id`, token breakdown + cost, `invocation_count`,
`status_counts`, `message_count`, `last_active_at`, `invocations_by_agent_kind`,
and `time_series`. It exposes **no other user's** id, username, events, or
totals, and accepts no `user_id` parameter.

**Raw events** (`GET /admin/metrics/events` → `RawMetricEvent` list): one row per
event with token counters, `source`, `status`, `model`, `occurred_at`, the
audit-only ownership identifiers, and a sanitized `metadata` object. It never
includes prompt text, assistant answers, tool input/output, or secrets.

**Business summaries** embedded in the overview:

- `chat` — `total_users`, `active_users`, `chat_session_count`,
  `chat_message_count`, `run_counts_by_status`.
- `logs` — `upload_count`, `uploaded_bytes`, `counts_by_log_type`,
  `counts_by_status`, `ai_analysis_counts`.
- `packages` — `package_count`, `total_bytes`, `counts_by_project`,
  `activity_counts`, `search_count`.
- `devices` — `counts_by_state` (current connection snapshot).

---

## 2. Prometheus metrics

`GET /metrics` returns Prometheus text exposition when `prometheus_client` is
installed. If it is unavailable the endpoint returns **HTTP 503** with an
explicit message (never a silently empty scrape), and all collectors degrade to
no-op stubs so imports stay safe.

> **Cardinality rule.** New metric labels are restricted to low/medium-cardinality
> dimensions only. `user_id`, `username`, `owner_scope`, `session_id`, `run_id`,
> `task_id`, `log_id`, `project_repo_id`, and `project_code` **MUST NEVER**
> appear as Prometheus labels. Per-user / per-run / per-project attribution
> lives exclusively in the database fact source and its APIs, never in
> Prometheus. A defensive `_label()` helper also coerces empty/None label values
> to `unknown`.

AI-usage collectors are bumped **only after a `metric_events` insert succeeds**,
so a duplicate idempotency key never double-counts in Prometheus.

| Metric | Type | Labels |
| --- | --- | --- |
| `raven_ai_tokens_total` | Counter | `source`, `agent_kind`, `provider`, `model`, `token_type` |
| `raven_ai_invocations_total` | Counter | `source`, `agent_kind`, `provider`, `model`, `status` |
| `raven_ai_invocation_duration_seconds` | Histogram | `source`, `agent_kind`, `provider`, `model`, `status` |
| `raven_ai_errors_total` | Counter | `source`, `agent_kind`, `error_kind` |
| `raven_http_requests_total` | Counter | `method`, `route`, `status_code` |
| `raven_http_request_duration_seconds` | Histogram | `method`, `route`, `status_code` |
| `raven_log_uploads_total` | Counter | `log_type`, `status` |
| `raven_log_uploaded_bytes_total` | Counter | `log_type` |
| `raven_package_activity_total` | Counter | `action`, `status` |
| `raven_device_connections` | Gauge | `state` |
| `raven_metrics_record_failures_total` | Counter | `source` |

Notes:

- `token_type` is one of `input` / `output` / `cache_read` / `cache_write`.
- `route` is always a **route template** (e.g. `/api/packages/{id}`), never a raw
  path, to keep HTTP cardinality bounded.
- `raven_metrics_record_failures_total` is incremented whenever best-effort
  metrics recording fails for a `source` (see [§5](#5-failure-isolation)).
- `raven_package_activity_total` no longer has the historical `package_type`
  label. Old Grafana queries grouped by `package_type` must be rewritten to
  group by `action` / `status`, or use `GET /admin/metrics/overview`
  `packages.counts_by_project` for project distribution.
- Pre-existing trace collectors `ai_analysis_trace_events_emitted_total{kind}`
  and `ai_analysis_trace_redis_bytes` remain unchanged.
  Empty `thinking_tokens` notices are omitted before trace emission, so new
  runs can have lower trace counts/bytes with identical model usage. This
  filtering does not alter token accumulation, tool-call counts or invocation
  metrics; it must not be interpreted as a reduction in model calls or tokens.

---

## 3. Optional cost estimates

Cost is **optional, configuration-driven, and always an estimate** — it is
computed from configured unit prices, not from any provider invoice.

Configure prices via the `AI_METRICS_PRICING_JSON` setting
(`ai_metrics_pricing_json`). The value is a JSON string mapping
`provider → model → token-type price`, where each unit is **USD per 1,000,000
tokens**:

```json
{
  "anthropic": {
    "claude-opus-4-8": {
      "input_per_million": 15.0,
      "output_per_million": 75.0,
      "cache_read_per_million": 1.5,
      "cache_write_per_million": 18.75
    }
  }
}
```

Behavior:

- When pricing **is** configured for an event's provider + model, the event is
  persisted with a non-null `cost_microusd` (integer micro-USD) and the APIs
  expose `estimated_cost_usd` (= `cost_microusd / 1_000_000`, rounded to 6 dp)
  with `cost_estimated = true`.
- When pricing is **absent** or the JSON fails to parse, recording still
  succeeds with full token counters, `cost_microusd` is `null`, and the APIs
  return `estimated_cost_usd = null` / `cost_estimated = false`. Missing pricing
  never blocks metrics. Default: no prices configured (no cost estimation).
- Treat `estimated_cost_usd` as a planning estimate, not a billing figure;
  actual provider charges may differ.

---

## 4. Historical backfill

[scripts/backfill_metric_events.py](../scripts/backfill_metric_events.py) is an
**explicit, manually-invoked** maintenance script. It never runs on service
startup and never auto-runs. It derives one `ai_usage` event per historical log
analysis from the token usage already stored in
`log_records.metadata_json.extra_fields.ai_analysis_result`.

It uses the **same idempotency key the live Celery path uses**
(`ai_usage:log_task:<log_id>`), so inserts are idempotent: rows already recorded
by the live path are never duplicated and re-running is safe. Backfilled rows
carry `metadata.historical = true` so they are distinguishable from
live-recorded events. Prometheus counters are intentionally **not** bumped —
those are cumulative, timestamp-free totals and backfill must not retroactively
inflate live dashboards.

```bash
# Dry run (default): report what would be inserted, write nothing.
python scripts/backfill_metric_events.py

# Apply: actually insert the derived metric events.
python scripts/backfill_metric_events.py --apply

# Limit how many log records are scanned (useful for a first pass).
python scripts/backfill_metric_events.py --apply --limit 500
```

Output reports `scanned`, `candidates`, `inserted`, and `skipped_existing`
tallies.

**Rollback.** Deleting backfilled rows is safe and affects no business table:

```sql
DELETE FROM metric_events
WHERE event_type = 'ai_usage'
  AND metadata_json LIKE '%"historical": true%';
```

(Review the matched rows before running.)

---

## 5. Failure isolation

Metrics recording is **best-effort** for primary business flows. A metrics
storage or Prometheus update failure is logged and counted
(`raven_metrics_record_failures_total{source}`) but never causes the original
chat, log analysis, project expert, package search, title generation, upload, or
download operation to fail.

Log Analysis and Project Expert commit and close the chat exchange transaction
before generating the optional initial title. The title's independent usage
transaction therefore does not wait behind a SQLite writer lock held by that
same workflow. Title usage remains recorded; title failure cannot roll back a
saved answer. Both services publish completion and end answer subscriptions before
awaiting this optional title work in the same background job. Title timeout cleanup
therefore cannot delay the completed answer. Title usage may arrive after the
terminal Agent metric; benchmarks distinguish answer completion from optional
metadata work and do not attribute this scheduling change to fewer model calls.

## 6. Privacy contract

The `metric_events` table and every export honor an **allowlist**:
`metadata_json` may only ever contain these keys — `tool_call_count`,
`trace_event_count`, `log_type`, `package_type`, `result_count`, `project_code`,
`error_kind`, `historical`. `package_type` is kept only so historical rows can
still be read; new package activity events write `project_code` (or
`unassociated`) instead. Everything else is dropped silently at sanitize time.
Metrics never persist or export prompts, assistant answers, raw tool
input/output, log content, credentialed headers, cookies, git tokens, or
token-bearing URLs. Project attribution is limited to non-sensitive identifiers
(e.g. `project_code`, `project_repo_id`), never a raw clone URL.

### 管理员对话审阅与临时追问

原始事件中的「查看对话」使用居中弹窗，仅点击右上角 X 关闭（遮罩及 Escape 不关闭）。正文中的 Mermaid 可点击打开可缩放大图。原始事件列表在计数、分页前排除 `log_upload` 和 `package_download`；下载记录及下载统计仍保留。

`POST /admin/metrics/events/{event_id}/conversation/follow-up` 是管理员专用临时 SSE 接口，请求为 `{question, history: [{role: "user"|"assistant", content}]}`；history 只包含当前弹窗内已完成的临时问答对。原对话正文由服务端重新读取。服务端通过事件 `run_id` 校验会话及用户后定位 `ChatAgentRun.workspace_path`，旧事件缺少 run_id 时选择事件发生前最近的同会话运行。工作空间不存在、已清理或位于工作空间根目录外时返回 409，不创建替代工作空间。

追问通过独立 SDK 调用读取原工作空间，只提供有路径边界检查的只读 list/read/search MCP 工具；禁用所有内置工具、项目设置/钩子、自动记忆及 SDK 会话持久化，CLI 配置使用独立临时目录并在结束/取消时清理。追问不调用普通聊天服务，不写入 ChatSession、ChatMessage、ChatAgentRun、分享快照或 metrics 正文日志，也不复用原用户 SDK session ID。当前弹窗仅在浏览器内存保留临时问答，关闭、卸载页面或切换对话会取消请求并清空。重新打开从空临时历史开始。

流事件为 `start`、`delta(text)`、`done(answer)` 或 `error(message)`，连接意外结束不能作为完成答案。该路由排除请求/响应正文日志，响应使用 `Cache-Control: no-store`；异常原文不返回客户端。每个问题最多 8000 字符，临时历史最多 40 条/100000 字符，单条回答最多 32000 字符，原对话上下文最多 300000 字符，运行最多 300 秒。仅支持能提供 in-process MCP 的模型端点。临时追问可查询同一工作空间的日志/源码，但不执行原 Agent 的设备、写文件等操作。

For optional isolated evidence verification, Log Analysis duration includes packet construction and the separate verifier call. The verifier terminal usage is added to the run token totals; helper/title activity must be accounted for when checking end-to-end API time and routing. Safe trace notices distinguish total verification seconds and model seconds. These are mechanism measurements, not benchmark acceptance. Packets, edit JSON and credentials are not metric labels.

The private timing harness keeps `observed_model_response_ids` as primary-only for historical comparability. It additionally records `verification_observed_model_response_ids` and `total_observed_model_response_ids`, and archives verifier transcripts separately. Totals count distinct assistant IDs within each stage; auxiliary title requests and requests without assistant responses are excluded, so these are not HTTP request counts. Existing pilot metadata corrected after completion uses separate hash-bound addenda and preserves the original results/timing.

Evidence-progress notices count distinct observed PostToolUse callbacks, not model turns or HTTP requests. At most two notices occur; they request no separate model call. Timing acceptance must still count all primary/verifier work and retain max-turn failures.

Named-log component scans are included in packet/verification duration and byte counts; the optional scan adds no model call. Omitted groups/files and unavailable-budget metadata do not imply semantic coverage or record absence.

SDK auxiliary session-title requests are not persisted as normal primary/verifier assistant transcript responses. Count HTTP calls by request kind when auditing this experiment; do not equate transcript response IDs with all requests. Synthetic protocol replay observed one primary plus one concurrent SDK-title request versus one primary only, with equal normalized primary inputs. Its injected delay is not a real latency result.

The default-off native session checkpoint similarly separates completed tool results, SDK `ResultMessage.num_turns`, transcript assistant IDs and HTTP requests. Pinned native proof reports4SDK turns for one response containing three parallel tools plus the checkpoint boundary. The remaining150budget must use authoritative SDK counters. Only the genuine resumed final terminal is public; aggregate per-invocation duration/usage, retain already cumulative API duration/cost, and include all process/resumption work in API/Agent timing. These protocol/accounting checks establish no quality or latency acceptance.
