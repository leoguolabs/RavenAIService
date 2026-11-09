## Context

The public Costs API returns expenditure, not remaining credits. The signed-in billing page uses `/v1/dashboard/billing/credit_grants`, whose `total_available` matches its displayed balance; the wallet endpoint alone excludes the available grant credits. The Raven key was matched to this organization. This dashboard endpoint and its session credential are not a stable public API.

## Goals / Non-Goals

**Goals:** Enforce a strict USD 15 admission floor on every new request to this Magpie provider, including direct requests from pinned tasks. Unknown balance must fail closed. Preserve transparent protocol bytes and streaming.

**Non-Goals:** Local cost estimates, changing the organization's billing settings, replaying committed inference, preserving an exact minimum against already-running requests or upstream accounting delay.

## Decisions

- Put a small authenticated reverse proxy between Magpie and the fixed `https://api.openai.com` origin. This covers routing groups, direct model requests and explicit overrides without patching Magpie's provider protocol implementation.
- Require a fresh reading from the verified credit-grants endpoint for each inference without a positive balance cache. Retry a transient HTTP or transport failure once within the same 2-second deadline. For HTTP 429 with `Retry-After`, suppress further lookups for the same billing credential until that window ends. Without a valid reading, deny forwarding. Decimal arithmetic enforces `< 15`, so exactly 15 is admitted.
- A private read-only secret contains the dashboard authorization, organization and SHA-256 fingerprint of the accepted Raven API key. Read it each time so in-place updates take effect without restart; atomic file replacement requires recreating the Compose secret mount. Never send the dashboard credential or cookies on inference requests or print upstream balance errors.
- Use the matching organization for both balance and inference. Fixed origins and no HTTP redirects prevent credential leakage. Host ports are not published.
- Return HTTP 402 for verified low balance and HTTP 503 when verification is unavailable. Both trigger Magpie's fallback, but only verified low balance triggers credit backoff. Log sanitized diagnostic codes without upstream bodies or credentials. Accepted streams finish without replays. Existing Raven pinning records and retains the actual fallback provider.
- Preserve OpenAI Responses capabilities explicitly when changing its hostname to the internal proxy; verify real tool continuation after deployment.

## Risks / Trade-offs

- Dashboard login expiry or endpoint changes stop OpenAI until the private source is refreshed. This is intentional fail-closed behavior.
- Every new inference adds a balance lookup. Queries time out after 2 seconds rather than delaying fallback indefinitely.
- In-flight consumption and upstream billing delay can take the balance below the threshold after admission; this gate rejects based on the latest upstream reading, not a reservation system.
- Magpie can rest a refused key using its normal quota/backoff rules. Recovery can wait for that rest; already switched tasks remain on their fallback.

## Migration Plan

Deploy and health-check the guard before changing the provider. Back up provider configuration privately inside its existing volume. Route its sole Responses endpoint and balance display through the guard, and add DeepSeek fallback. Verify actual balance, allowed real inference and tools, and deterministic blocked/failure cases. Rollback restores the provider endpoint and fallback from the private backup.
