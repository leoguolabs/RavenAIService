## Why

OpenAI must stop serving new requests when its actual remaining credits are below USD 15. The user requires a failed balance query to stop requests too; local usage estimates cannot account for other applications or expiring credits.

## What Changes

- Add an internal OpenAI forwarding service that checks the authenticated dashboard credit-grants balance before each eligible Raven task and applies that decision to the task's requests.
- Reject low, missing, malformed or unavailable balance readings before inference is sent.
- Configure Magpie's existing OpenAI provider through this service, with DeepSeek as its fallback. Keep task pinning and streaming unchanged.
- Isolate the dashboard credential in a private mounted secret; expose a sanitized balance reading to Magpie.

## Capabilities

### New Capabilities
- `provider-balance-floor`: Verified remaining-balance admission for OpenAI requests.

### Modified Capabilities
None.

## Impact

One additional local Docker service, provider endpoint configuration, regression tests and the Magpie runbook. No OpenAI billing limits or keys are created or changed; no production or separate OpenAI-stack changes.
