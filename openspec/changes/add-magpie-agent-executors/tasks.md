## 1. Runtime and packaging

- [x] 1.1 Inspect current optimization changes and retain the user's Magpie fork as a pinned submodule.
- [x] 1.2 Package checksum-pinned Antigravity 1.5.2 for ARM64/AMD64 and verify installation in the ARM64 backend image.
- [x] 1.3 Add persistent Magpie Compose service and credential bootstrap from existing configured providers.
- [x] 1.4 Add validated administrator runtime settings, model discovery and executor selection API.

## 2. Execution and attribution

- [x] 2.1 Route inference through Magpie with per-run credentials and actual model/provider response attribution.
- [x] 2.2 Implement the Antigravity structured-event adapter and authenticated MCP bridge, preserving permissions and hooks.
- [x] 2.3 Integrate both executors across all Raven agents with cancellation and no whole-run gateway failover.
- [x] 2.4 Persist actual executor and route changes in shared traces and result metadata.

## 3. Administrator and user interface

- [x] 3.1 Add administrator controls for gateway settings, catalog model/group and execution backend.
- [x] 3.2 Show actual model/provider and executor with identical skill/tool rendering and replay for both runtimes.

## 4. Verification and delivery

- [x] 4.1 Run focused backend and frontend regression tests, type checks and OpenSpec validation.
- [x] 4.2 Build an isolated local Docker stack and securely reuse existing Yinhe, DeepSeek and OpenAI settings/data.
- [x] 4.3 Run real tool-and-skill E2E tasks for Claude SDK × Yinhe/DeepSeek/OpenAI and Antigravity CLI × Yinhe/DeepSeek/OpenAI.
- [x] 4.4 Verify browser UI, actual gateway fallback attribution, persisted replay and cancellation without changing normal user flows.
- [x] 4.5 Update runtime/trace/deployment documentation, review diffs and commit scoped changes and umbrella gitlinks.
