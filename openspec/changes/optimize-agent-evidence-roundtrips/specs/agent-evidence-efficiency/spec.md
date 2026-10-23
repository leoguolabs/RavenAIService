## ADDED Requirements

### Requirement: Preserve investigation semantics
The optimized Agent SHALL retain all attachments, project-fit checks, original evidence access, required skills, output schema and uncertainty semantics. Disabled optimization SHALL use the existing execution path.

#### Scenario: Startup snapshot available
- **WHEN** optimization is enabled and a sanitized complete task/catalog snapshot is prepared
- **THEN** the Agent can use it instead of repeating startup discovery calls, while still checking project fit

#### Scenario: Snapshot unavailable
- **WHEN** snapshot preparation fails
- **THEN** the Agent retains the original discovery and task reading workflow

### Requirement: Bounded batch evidence with complete continuation
Batch tools SHALL accept only workspace-contained read operations, preserve per-request failures, and return exact source locations plus explicit continuation whenever results are incomplete.

#### Scenario: More matches than the result budget
- **WHEN** a search exceeds its page budget
- **THEN** the response identifies incomplete output and allows retrieving subsequent matches without silently losing evidence

#### Scenario: Path escapes the workspace
- **WHEN** a requested path or symlink resolves outside the workspace
- **THEN** the tool rejects that request without reading the target

#### Scenario: Independent evidence requests
- **WHEN** a model submits several independent searches or file ranges
- **THEN** the server executes them with bounded concurrency and returns separately attributed results in request order
