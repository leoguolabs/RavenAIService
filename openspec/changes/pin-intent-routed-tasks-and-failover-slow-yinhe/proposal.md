## Why

The current ordered group routes each request afresh and returns to an overloaded local Yinhe model after a fallback. Tasks need intent-based initial selection, a stable model, and bounded latency without replaying completed tools.

## What Changes

- Configure Magpie intent rules for simple, routine and complex requests, using a cloud classifier.
- Pin the actual answering model for each Raven invocation. Only a slow/unavailable Yinhe model can move the invocation to its configured cloud backup.
- Add a short pre-output speed check and a streaming watchdog. After output starts, finish that response without replay and use the backup for later requests.
- Share a short Yinhe cooldown across workers; degraded new tasks use a cloud-only intent group.
- Preserve explicit model selections, existing executors, credentials and unrelated deployments.

## Capabilities

### New Capabilities
- `task-stable-intent-routing`: Intent-selected, invocation-stable routing with safe local-model latency failover.

### Modified Capabilities
None. This is an opt-in extension of the Magpie bridge; Magpie still owns provider protocols, authentication and initial intent selection.

## Impact

Raven runtime policy, Magpie bridge, routing configuration script, regression tests and the Magpie runbook. Only the authorized local Raven Magpie stack is deployed; production and the independent OpenAI stack are outside scope.
