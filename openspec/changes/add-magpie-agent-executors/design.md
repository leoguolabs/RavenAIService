## Context

Six Raven agents prepare workspaces and consume Claude SDK message shapes. `provider_query.py` already provides a shared invocation boundary and `routed_query.py` protects against replay after tool effects. The evidence-roundtrip experiment has committed changes in these paths which must remain intact. Magpie exposes Anthropic and Gemini inference APIs plus `X-Magpie-Provider` and `X-Magpie-Model` response headers. Antigravity 1.5.2 provides headless structured events; the host's 1.1.3 is too old.

## Goals / Non-Goals

**Goals:** administrator-selectable execution, all inference routed through Magpie when enabled, existing credentials reused, real route/executor attribution, common tool/skill UI, isolated live Docker acceptance for both executors and Yinhe/DeepSeek/OpenAI.

**Non-Goals:** automatic or LLM-based executor selection, changing the evidence-analysis workflow, changing the user's desktop CLI installation, or removing the existing SDK implementation.

## Decisions

1. Add an independently validated runtime configuration service and admin API/UI. Snapshot executor and route at invocation; never mutate global environment or another run's settings. Global defaults with optional per-agent overrides form the future policy boundary.
2. Keep `provider_query` as the execution boundary. The Claude adapter retains existing SDK calls. The Antigravity adapter translates structured CLI events into the message shapes consumed by existing trace handlers. Both share lifecycle cancellation and terminal error semantics.
3. Use a per-run loopback proxy to the configured Magpie service. It authenticates local callers, forwards inference without implementing provider routing, and emits actual route metadata from Magpie's response headers. OAuth/provider secrets stay in Magpie; CLI processes receive only ephemeral loopback credentials. Magpie mode returns one endpoint, disabling Raven's competing upstream failover.
4. Bridge existing in-process MCP servers over an authenticated local transport. Keep the original handlers, permission broker and hooks; normalize CLI tool names so existing UI labels, tool results and skill activation remain truthful. Do not invent tool or skill events.
5. Pin Antigravity download URLs and checksums for Linux ARM64/AMD64. Add Magpie as a separate persistent Compose service, using the fork pinned by the umbrella submodule. A bootstrap path imports only existing model configuration, preserving source settings for rollback and never printing credentials.
6. Persist executor identity and observed routes in normal trace events. The UI consumes actual route notices and does not infer a served model from the requested group. Before a response, indicate only the requested route. Keep existing result metadata consistent with observed route information.

## Risks / Trade-offs

- CLI schemas and behavior differ → parse versioned fixtures and test against the installed binary before live acceptance; unsupported capabilities fail explicitly.
- CLI permissions differ → preserve Raven's broker for MCP operations and configure built-in permissions per workload; validate denied actions and cancellation.
- A gateway can switch members between model turns → capture every route change and retain history, not only the first configured name.
- Existing optimization experiments depend on the shared Docker installation → use a separate Compose project, ports and volumes; leave existing containers and records intact.
- An upstream account may require interactive sign-in → finish independent API-key work and hand off only the specific OAuth login if required.

## Migration Plan

Create the new stack, install the pinned CLI, bootstrap Magpie from the existing deployment's configured credentials, then enable the gateway and choose an executor in Admin. Verify API behavior and browser-visible traces before completion. Rollback selects Claude SDK and disables gateway mode; original upstream settings remain available. Production rollout is not claimed by local validation.

## Open Questions

Resolve during implementation against the actual CLI: exact permission and MCP configuration schema, structured event fields, and gateway compatibility for all three existing model sources. No undocumented behavior is accepted as an E2E pass.
