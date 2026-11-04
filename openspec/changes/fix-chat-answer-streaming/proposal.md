## Why

Specialist agents wrap answer deltas in `agent_trace`, but the web chat routes these into the trace instead of the answer. Users see no answer until completion; structured JSON and coarse provider chunks also prevent a readable, continuous response.

## What Changes

- Consume bare and wrapped answer deltas through the same deduplicated answer path, including snapshot replay.
- Project partial structured results into readable answer text without exposing JSON envelopes.
- Smooth newly generated answers on screen, including whole-answer fallback, while retaining authoritative completion text and immediate historical rendering.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `agent-trace-ui`: readable incremental answer projection and paced rendering for live responses across both SSE envelopes.

## Impact

Vue chat store, answer rendering, focused regression tests, and trace protocol documentation. No endpoint or persistence schema changes.
