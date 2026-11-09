## ADDED Requirements

### Requirement: Verified balance admission
The protected provider SHALL obtain a fresh query of its actual USD credit-grants balance before forwarding each new upstream inference. It SHALL refuse forwarding below USD 15 and SHALL admit exactly USD 15. No local usage estimate SHALL substitute for a balance query.

#### Scenario: Below threshold
- **WHEN** the source reports 14.99
- **THEN** no inference reaches OpenAI and Magpie receives a fallback-eligible refusal

#### Scenario: Boundary
- **WHEN** the source reports 15.00
- **THEN** the request may reach OpenAI

### Requirement: Unknown balance stops requests
Missing credentials, authentication failure, timeout, malformed JSON, missing/non-finite balance or an unsuccessful source response SHALL deny forwarding. An earlier successful reading MUST NOT authorize a later request whose query fails.

#### Scenario: Transient source failure
- **WHEN** the first lookup receives a retryable response or transport error and its retry fits within the balance deadline
- **THEN** the guard retries only the balance lookup once and admits inference only if it obtains a valid balance at or above the floor

#### Scenario: Verification remains unavailable
- **WHEN** no valid balance can be verified within the deadline
- **THEN** the guard returns HTTP 503 so Magpie may fall back without recording a credit failure

#### Scenario: Billing source requests a rate-limit delay
- **WHEN** the balance endpoint returns HTTP 429 with a valid `Retry-After`
- **THEN** the guard SHALL suppress balance lookups for the same credential during that window and SHALL refuse inference without a fresh reading

#### Scenario: Source fails after success
- **WHEN** one request was allowed and the next balance query fails
- **THEN** the second inference is not sent to OpenAI

### Requirement: Secure transparent transport
The guard SHALL authenticate the configured Raven key, keep billing authorization separate from inference authorization, restrict destinations to OpenAI, preserve accepted request bodies and streamed responses, and close responses on cancellation.

#### Scenario: Tool continuation after fallback
- **WHEN** a pinned OpenAI request is rejected by the guard
- **THEN** Magpie can use its configured fallback and Raven pins the actual provider with the existing tool context

#### Scenario: Invalid caller
- **WHEN** the caller does not present the configured key
- **THEN** neither the balance query nor inference is sent upstream
