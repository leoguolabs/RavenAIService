## ADDED Requirements

### Requirement: Verified balance admission
In task mode, Raven SHALL decide admission before starting an Agent task whose route may use the protected provider. The guard SHALL use the verified USD credit-grants balance minus subsequent priced Magpie usage while the baseline is younger than 12 hours and the estimate is outside the next verification boundary. At that boundary, when the baseline expires, or when the ledger becomes untrustworthy, the guard SHALL obtain a fresh balance before issuing an allow decision. Each protected inference SHALL use that task's authenticated decision. Streaming, tool continuation, executor retries and fallback SHALL NOT trigger balance queries. The guard SHALL refuse forwarding below USD 15 and SHALL admit a verified USD 15.

#### Scenario: Recent trustworthy baseline
- **WHEN** the estimated balance remains above the next verification boundary and Magpie's ledger is consistent
- **THEN** a new task uses the estimate without a request to the billing endpoint

#### Scenario: Boundary reached or ledger uncertainty
- **WHEN** estimated balance falls to USD 20, or the ledger loses continuity or contains new unpriced use
- **THEN** the next task requires a real balance check, subject to backoff after repeated anomalies

#### Scenario: Below threshold
- **WHEN** the source reports 14.99
- **THEN** no inference reaches OpenAI and Magpie receives a fallback-eligible refusal

#### Scenario: Boundary
- **WHEN** the source reports 15.00
- **THEN** the request may reach OpenAI

#### Scenario: Tool continuation and display polling
- **WHEN** one admitted task sends multiple requests, or Magpie polls the balance display
- **THEN** the guard reuses the task's decision and displays the most recent verified reading without querying the upstream balance again

### Requirement: Unknown balance stops requests
Missing credentials, authentication failure, timeout, malformed JSON, missing/non-finite balance or an unsuccessful source response SHALL deny forwarding. An earlier task's successful reading MUST NOT authorize a later task whose query fails.

#### Scenario: Transient source failure
- **WHEN** a task's lookup receives a retryable response or transport error
- **THEN** the task is denied OpenAI admission and no second lookup is sent for that task

#### Scenario: Verification remains unavailable
- **WHEN** no valid balance can be verified within the deadline
- **THEN** the guard returns HTTP 503 so Magpie may fall back without recording a credit failure

#### Scenario: Billing source requests a rate-limit delay
- **WHEN** the balance endpoint returns HTTP 429 with a valid `Retry-After`
- **THEN** the guard SHALL suppress balance lookups for the same credential during that window and SHALL refuse inference without a fresh reading

#### Scenario: Source fails after success
- **WHEN** one task was allowed and the next task's balance query fails
- **THEN** inference from the second task is not sent to OpenAI

### Requirement: Secure transparent transport
The guard SHALL authenticate the configured Raven key, keep billing authorization separate from inference authorization, restrict destinations to OpenAI, preserve accepted request bodies and streamed responses, and close responses on cancellation.

#### Scenario: Tool continuation after fallback
- **WHEN** a pinned OpenAI request is rejected by the guard
- **THEN** Magpie can use its configured fallback and Raven pins the actual provider with the existing tool context

#### Scenario: Invalid caller
- **WHEN** the caller does not present the configured key
- **THEN** neither the balance query nor inference is sent upstream
