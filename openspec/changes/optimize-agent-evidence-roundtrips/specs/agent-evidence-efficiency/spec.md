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

### Requirement: Automatic source context for native searches
When efficiency is enabled, successful line-number source searches MAY receive bounded additional source excerpts without an extra model request. The server SHALL verify that each source path is inside a workspace repository and that its referenced line matches the search output. Original tool outputs, tool access, total turn budget, necessary retries and evidence requirements SHALL remain unchanged. Added-context state SHALL be isolated per run.

#### Scenario: Native search locates a source definition
- **WHEN** a native line-number search returns a verified repository source reference
- **THEN** the SDK can add a bounded surrounding excerpt with exact lines and continuation while retaining the original search result

#### Scenario: Ambiguous or unsupported search output
- **WHEN** a path escapes the repository, a line does not match, or output cannot be resolved unambiguously
- **THEN** no excerpt is added for that reference and the original tool workflow remains available

#### Scenario: Concurrent or subsequent run
- **WHEN** another Agent run starts
- **THEN** its added-context deduplication is independent of previous runs
