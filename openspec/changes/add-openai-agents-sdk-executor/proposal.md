## Why

Administrators can switch Raven agents between Claude and Antigravity through Magpie, but cannot select OpenAI's official Agents SDK. Add that runtime without changing model routing or the existing agent contracts.

## What Changes

- Add OpenAI Agents SDK to global and per-agent executor selections.
- Run the official Python SDK through Magpie's Responses endpoint with Raven tools, permissions, skills, hooks, streaming, usage, and cancellation.
- Show the actual executor and Magpie route in existing traces.
- Verify switching and real inference through the local Raven/Magpie stack.

## Capabilities

### New Capabilities
- `openai-agents-executor`: Official SDK execution with Magpie and Raven's existing agent lifecycle.

### Modified Capabilities
None.

## Impact

Backend runtime selection and provider dispatch; Python SDK dependency; frontend executor types and trace labels; regression tests and the agent trace runbook. Existing executor defaults remain compatible. No production deployment is included.
