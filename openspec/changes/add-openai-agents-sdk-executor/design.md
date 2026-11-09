## Context

All six agents consume the same Claude-shaped message contract. `provider_query` selects a per-run executor and wraps Magpie transport; the gateway already accepts Responses requests. Existing control code connects Raven-owned MCP servers and enforces permission callbacks and hooks.

## Goals / Non-Goals

**Goals:** expose `openai_agents` globally and per agent; use the official Python `Agent`/`Runner` loop; preserve Raven tools, skills, hooks, streaming, usage, cancellation, and authoritative route attribution; verify through the local stack.

**Non-Goals:** replace business-agent prompts, change Magpie routing policy, introduce OpenAI-hosted execution, or deploy to production.

## Decisions

- Use an explicit `AsyncOpenAI` client and `OpenAIResponsesModel` pointing to the per-run Magpie bridge. Disable SDK tracing and HTTP retries for this run; never set process-global credentials or SDK defaults. The gateway retains ownership of upstream routing and errors.
- Adapt SDK stream events and tool execution to the existing message contract. An invocation pins its executor/model; subsequent invocations read the latest administrator selection.
- Reuse Raven's MCP control and authorization/hook implementation. Offer explicit local function tools for the workspace operations used by Raven agents, honoring disabled tools and permission decisions. Function calls retain the SDK call ID for approvals and trace pairing.
- Load skills from the existing workspace materialization. Snapshot the initial Skill symlink targets for read-only access to shared packages and references; later links cannot extend access. Enforce Stop hooks with continuation inside the original turn budget. Unsupported tools are not advertised.
- Keep existing trace transports and display a distinct SDK engine label. Default executor stays unchanged until an administrator switches it.

## Risks / Trade-offs

- Gateway providers differ in Responses support → test real Magpie model/group routes, tools and streaming; report provider failures separately from executor compatibility.
- Cross-runtime hook semantics can drift → focused tests for permission denial, argument rewriting, post-tool context, Stop continuation and cancellation.
- SDK version drift → pin the validated version in requirements and inspect the installed SDK interface.
- Native shell tools carry existing agent permissions → invoke authorization before execution, use run cwd, sanitize inherited credentials, bound output/time and terminate on cancellation.

## Migration Plan

Rebuild the local backend/worker and frontend images with the dependency. Verify selection persistence and real runs; switch back to the original default after testing. Rollback is selecting an existing executor. Production is outside this change.
