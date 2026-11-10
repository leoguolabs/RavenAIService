## Context

The deployed Raven bridge already pins the actual model and avoids replay after output. Its old default group classifies task intent, while local streaming deadlines start only after HTTP headers. Magpie's native first-token setting has whole-second granularity. The current deployment also contains newer OpenAI Agents SDK and packaging work that must be preserved.

## Goals / Non-Goals

**Goals:** Prefer Yinhe without classification, enforce a 0.5-second dispatch-to-first-output budget, and retain the actual cloud fallback for the invocation.

**Non-Goals:** Cancelling already delivered streams, restarting whole agents, changing cloud credentials or billing protection, or deploying to production.

## Decisions

- Use the singleton `group/raven-task-yinhe` as both Raven entry and local retry group. Keep legacy `raven-task-local` and `raven-task-intent` IDs as ordered aliases, explicitly clearing their rules and classifiers. Cloud order is OpenAI then DeepSeek; preserve OpenAI's provider-level balance fallback.
- Enforce the sub-second budget in Raven around the local HTTP send, and pass the same start time into the local stream meter. Early headers and heartbeats do not reset the deadline. A local-only group prevents this timer from cancelling a cloud attempt already chosen inside a mixed group.
- Local HTTP failures also switch before delivery. Non-streamed local replies must complete within the same budget. External task cancellation closes resources and never initiates fallback.
- Keep the existing stream commitment boundary: buffer a 0.5-second speed observation, then deliver; after delivery, slowness marks the next request for cloud without replay. Require 20 estimated tokens/s, cap idle at 2 seconds, and share a 120-second cooldown.
- Reuse the existing policy schema. Explicit model overrides still opt out; current six agents inherit the default. Preserve the newer executor selection and scope image updates to the three routing files.

## Risks / Trade-offs

- Aggressive deadlines can bypass a healthy but slower local model; this is the requested preference for responsiveness and may increase paid usage.
- Estimated tokens use UTF-8 bytes / 4, not the provider tokenizer. Tests assert timing and commitment behavior, not answer quality.
- Existing invocations retain their policy snapshot; apply only after checking local services for active work. Balance-source failures still exclude OpenAI, leaving DeepSeek.

## Migration Plan

Run focused regressions, layer changed files onto the currently running backend image, update only local backend/workers/beat, then apply backed-up route configuration. Verify live tool continuation, actual fallback pinning and absence of classifier rules. Roll back with the private runtime/group backup and recorded previous image if necessary.
