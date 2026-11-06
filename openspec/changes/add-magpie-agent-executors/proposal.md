## Why

Raven currently binds every workload to Claude Agent SDK and manages upstream credentials and failover itself. Administrators need interchangeable Claude SDK and Antigravity CLI execution while Magpie owns model routing, without changing the user's workflow or losing tool, skill, permission, and result visibility.

## What Changes

- Add administrator-selected execution backends, initially Claude Agent SDK and Antigravity CLI, behind a common execution boundary for all six Raven agents.
- Run both backends against Raven's existing task workspaces, prompts, skills and tools; normalize their events into the existing trace and cancellation lifecycle.
- Route model inference through a dedicated Magpie service; reuse existing Yinhe, DeepSeek and OpenAI credentials without exposing them to the frontend or requiring re-entry.
- Show the actual execution backend and resolved upstream model/provider, including gateway fallback changes, in live and persisted conversations.
- Install a pinned Antigravity CLI in the backend image and retain the user's Magpie fork as an umbrella submodule for future integration changes.
- Preserve the existing evidence-roundtrip optimization and its experiments. Validate the new architecture in a separate local Docker stack.

## Capabilities

### New Capabilities

- `agent-executors`: administrator-selected execution backends with common workspace, tool, skill, permission, stream and cancellation behavior.
- `magpie-model-gateway`: centralized upstream routing, credential migration, model discovery and actual route attribution.

### Modified Capabilities

- `agent-trace-stream`: executor identity and resolved upstream metadata are retained in live events and replay.

## Impact

Backend execution/configuration services, administrator APIs and UI, agent trace types/rendering, Docker packaging, focused tests and live E2E fixtures. The existing Claude execution path stays available. Future rule-based executor selection is enabled by the boundary but is outside this phase; selection is administrator-controlled.
