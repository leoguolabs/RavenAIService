## 1. Implementation

- [x] 1.1 Implement authenticated forwarding and strict real-balance admission.
- [x] 1.2 Add local Compose integration and document credentials, fallback and recovery.

## 2. Verification and activation

- [x] 2.1 Cover threshold boundaries, unknown balance, credential isolation and streaming.
- [x] 2.2 Deploy the guard and configure the scoped Magpie provider after private backup.
- [x] 2.3 Verify real balance, inference and tool continuation, then commit task-owned files.

## 3. Transient balance source failures

- [x] 3.1 Retry transient balance lookups within the original deadline and distinguish unavailable verification from verified low balance.
- [x] 3.2 Verify transient recovery, sustained fallback, low-balance refusal and a real RavenAI request.

## 4. Task-scoped admission

- [x] 4.1 Check the real balance once before the Agent starts, preserve the decision through model requests and tool continuations, and serve the display from the last verified reading.
- [x] 4.2 Verify task isolation, fail-closed fallback and the scoped local deployment.

## 5. Periodic balance verification

- [x] 5.1 Persist a verified balance baseline and estimate from priced Magpie usage until a timed or boundary check is needed.
- [x] 5.2 Verify threshold, ledger anomalies, restart recovery and local deployment.
