## Why

The user now prefers the internal Yinhe model for every task, with faster abandonment when it is congested. Intent classification adds a model call and no longer reflects the requested selection policy.

## What Changes

- Replace intent rules and classifier calls with Yinhe-first ordered selection.
- Bound the local attempt from request dispatch to first meaningful output at 0.5 seconds, including delayed HTTP headers.
- Tighten the generation observation window to 0.5 seconds, minimum estimated rate to 20 tokens/s, idle limit to 2 seconds, and shared cooldown to 120 seconds.
- Preserve streams already delivered, full continuation context, per-task fallback pinning, and the OpenAI real-balance guard.

## Capabilities

### New Capabilities
- `yinhe-first-task-routing`: Deterministic local preference with bounded, sticky slow-model failover.

### Modified Capabilities
None. This supersedes the unarchived intent-selection change for the current deployment.

## Impact

Raven routing policy, bridge, configuration script, tests and runbook; the authorized local Magpie stack only. No Magpie source, provider credentials, unrelated agent settings or production deployment changes.
