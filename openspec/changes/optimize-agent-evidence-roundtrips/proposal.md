## Why

Production traces show minutes of Agent execution with only seconds spent in tools, many small evidence searches, and repeated context compaction. Improve useful evidence collected per model round trip while preserving conclusions, evidence coverage, project boundaries, and expressed uncertainty.

## What Changes

- Add an opt-in execution policy for Log Analysis and Project Expert with a server-prepared, sanitized startup snapshot and bounded batch search/read tools.
- Preserve original tools, complete evidence access, output schema, required skills, project-fit checks, total turn budget, and provider routing safety boundaries.
- Add a resumable local Docker benchmark with original production archives/questions, immutable input hashes and source revisions, alternating baseline/candidate runs, and durable full results/traces.
- Run at least 20 paired timing rounds targeting a 30% median reduction, followed by at least 10 distinct production-log analyses with paired quality verification. No production deployment is included.

## Capabilities

### New Capabilities
- `agent-evidence-efficiency`: batching and initial evidence snapshot without reducing investigation requirements.
- `agent-efficiency-validation`: paired timing and evidence-based answer-equivalence acceptance.

### Modified Capabilities

None. Existing analysis output and evidence requirements remain unchanged; the execution policy is opt-in until validation passes.

## Impact

Backend Agent setup, shared read-only evidence tools, local benchmark scripts and focused tests. Production data is downloaded to private local storage, never committed. Local Docker uses isolated data and pinned dependencies; existing local volumes and production services remain intact.
