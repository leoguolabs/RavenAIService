## Context

Magpie already classifies user turns and retries candidates before output. Raven creates one authenticated bridge and one session ID per executor invocation. Its current group uses `affinity=off`; the native first-token limit does not detect low generation throughput. Existing tools must never be replayed by restarting an Agent.

## Goals / Non-Goals

**Goals:** classify the first inference through official Magpie intent rules, keep subsequent inference on the actual model, and make a one-way transition from slow Yinhe to a cloud backup. Preserve streaming after a short health gate. Share cooldown across backend/workers.

**Non-Goals:** task decomposition, automatic model quality certification, cross-provider secrets in Raven, production deployment, or interrupting committed output to splice in another answer.

## Decisions

- Opt-in runtime policy applies only to its named entry group. Explicit model overrides and legacy paths keep their behavior.
- Use official Magpie groups: simple requests to DeepSeek Flash, routine requests to Yinhe, difficult/uncertain requests to GPT. DeepSeek is the cloud classifier. A cloud-only counterpart serves new tasks during Yinhe cooldown.
- The bridge pins the returned provider/model. A pinned Yinhe model uses a dedicated Yinhe-to-GPT group to retain Magpie's native 3-second first-token failover; once cloud answers, use its explicit model thereafter. Serialize inference within each invocation to prevent concurrent first requests choosing different models.
- Before forwarding local response bytes, observe a 3-second window, allowing short completed responses immediately. Use a conservative UTF-8/4 output-token estimate, including visible thinking and tool arguments, with an initial 10 estimated tokens/s floor and 5-second idle limit. These are operator thresholds, not vendor performance guarantees.
- If the gate detects slowness, cancel only that uncommitted inference and repeat its unchanged context at the cloud backup. After output commits, finish the stream unchanged and mark the next inference for the backup, as the user selected. Never restart the whole Agent.
- A 60-second Redis-backed cooldown sends new tasks to the cloud-only intent group. Redis failure retains process-local cooldown; task pinning itself needs no Redis. A task already switched never returns when cooldown expires.
- Catalog and count-token calls do not pick or change the task model. Route events continue to report actual response headers, not merely a planned switch.
- Live Yinhe verification exposed JSON-encoded MCP Arguments in Antigravity. Normalize valid object strings consistently for tool IDs and permission checks; reject other values without dispatching tools.

## Risks / Trade-offs

- The speed gate adds up to 3 seconds of initial display latency for healthy Yinhe replies. Cloud responses stream immediately.
- Throughput is estimated before final usage exists; include reasoning and tool arguments and expose thresholds for tuning.
- A response that becomes slow after commitment is not interrupted or replayed; the following model request switches. Streaming silence can still last until the existing transport timeout.
- Intent classification can be wrong. Unknown classifications default to GPT; no quality guarantee is implied.
- Deployment must preserve the running backend's recent packaging changes and the separate OpenAI stack.

## Migration Plan

Keep a private backup of runtime settings and routing groups. Install the scoped code on each existing Magpie-stack backend/worker image, retaining its other files, then configure groups and enable policy. Verify fake slow streams, real classification/tool continuation and runtime route reporting. Rollback selects the previous runtime model and disables this policy; original groups and credentials remain.

## Open Questions

None. The user selected preserving committed streaming output and switching subsequent requests.
