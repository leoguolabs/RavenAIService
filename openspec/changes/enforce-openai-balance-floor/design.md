## Context

The public Costs API returns expenditure, not remaining credits. The signed-in billing page uses `/v1/dashboard/billing/credit_grants`, whose `total_available` matches its displayed balance; the wallet endpoint alone excludes the available grant credits. The Raven key was matched to this organization. This dashboard endpoint and its session credential are not a stable public API.

## Goals / Non-Goals

**Goals:** Enforce a strict USD 15 admission floor at the start of each eligible Raven task. Unknown balance must fail closed. Preserve transparent protocol bytes and streaming.

**Non-Goals:** Local cost estimates, changing the organization's billing settings, replaying committed inference, preserving an exact minimum against already-running requests or upstream accounting delay.

## Decisions

- Put a small authenticated reverse proxy between Magpie and the fixed `https://api.openai.com` origin. This covers routing groups, direct model requests and explicit overrides without patching Magpie's provider protocol implementation.
- With task mode configured, authenticate the Raven gateway caller by its key fingerprint on the internal admission endpoint. Use that key to read the OpenAI provider's priced Magpie usage. Persist the verified balance and its usage baseline together. Use the verified balance minus added priced spend while the baseline is younger than 12 hours. Refresh at a USD 20 estimate, then after another USD 1 of spend or at the USD 15 floor if the verified amount is already within the buffer. A missing, reset or newly unpriced ledger requires verification. Repeated anomalies are throttled for 15 minutes.
- When a real balance is required, make one lookup within the 2-second deadline. Issue a signed, expiring decision bound to the billing source and inference credential. The loopback bridge passes the decision to the guard through Magpie's provider header allowlist. The guard consumes that header locally.
- For HTTP 429 with `Retry-After`, suppress lookups for the same billing credential until that window ends. Without a valid decision, deny forwarding. Decimal arithmetic enforces `< 15`, so exactly 15 is admitted. The display reads the latest verified snapshot and does not authorize any inference. Deployments that have not enabled task mode retain per-request checks during migration.
- A private read-only secret contains the dashboard authorization, organization and SHA-256 fingerprint of the accepted Raven API key. Read it each time so in-place updates take effect without restart; atomic file replacement requires recreating the Compose secret mount. Never send the dashboard credential or cookies on inference requests or print upstream balance errors.
- Use the matching organization for both balance and inference. Fixed origins and no HTTP redirects prevent credential leakage. Host ports are not published.
- Return HTTP 402 for verified low balance and HTTP 503 when verification is unavailable. Both trigger Magpie's fallback, but only verified low balance triggers credit backoff. Log sanitized diagnostic codes without upstream bodies or credentials. Accepted streams finish without replays. Existing Raven pinning records and retains the actual fallback provider.
- Preserve OpenAI Responses capabilities explicitly when changing its hostname to the internal proxy; verify real tool continuation after deployment.

## Risks / Trade-offs

- Dashboard login expiry or endpoint changes stop OpenAI until the private source is refreshed. This is intentional fail-closed behavior.
- Eligible tasks read the local ledger at startup. Real balance checks run with task traffic, normally at least 12 hours apart. Other applications' spending and delayed usage cannot be estimated from Magpie and are reconciled at the next real check.
- In-flight consumption and upstream billing delay can take the balance below the threshold after admission; this gate rejects based on the latest upstream reading, not a reservation system.
- Magpie can rest a refused key using its normal quota/backoff rules. Recovery can wait for that rest; already switched tasks remain on their fallback.

## Migration Plan

Deploy and health-check the guard before changing the provider. Back up provider configuration privately inside its existing volume. Route its sole Responses endpoint and balance display through the guard, and add DeepSeek fallback. Verify actual balance, allowed real inference and tools, and deterministic blocked/failure cases. Rollback restores the provider endpoint and fallback from the private backup.
