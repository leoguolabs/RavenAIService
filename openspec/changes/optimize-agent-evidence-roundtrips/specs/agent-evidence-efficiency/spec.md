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

#### Scenario: Compact evidence representation
- **WHEN** MCP results are encoded as rows with shared column legends and derived read parameters
- **THEN** every returned source character, location, error and continuation is preserved, and nonstandard parameters fall back to the original representation

#### Scenario: Advanced regex is supported by the runtime
- **WHEN** a query uses lookaround or backreferences and ripgrep has PCRE2 support
- **THEN** the tool selects the compatible engine without a failed-query model round trip, retaining the same query timeout and workspace bounds

#### Scenario: Terminal carriage returns in a log
- **WHEN** a file contains standalone carriage returns or Unicode separator characters within LF-delimited lines
- **THEN** searches, subsequent reads and source verification use the same native LF line numbers and preserve those characters within the returned line

### Requirement: Automatic source context for native searches
When efficiency is enabled, successful line-number source searches MAY receive bounded additional source excerpts without an extra model request. The server SHALL verify that each source path is inside a workspace repository and that its referenced line matches the search output. Original tool outputs, tool access, total turn budget, necessary retries and evidence requirements SHALL remain unchanged. Added-context state SHALL be isolated per run.

#### Scenario: Native search locates a source definition
- **WHEN** a native line-number search returns a verified repository source reference
- **THEN** the SDK can add a bounded surrounding excerpt with exact lines and continuation while retaining the original search result

#### Scenario: Ambiguous or unsupported search output
- **WHEN** a path escapes the repository, a line does not match, or output cannot be resolved unambiguously
- **THEN** no excerpt is added for that reference and the original tool workflow remains available

#### Scenario: Source excerpts exceed the SDK inline output budget
- **WHEN** the encoded source context would exceed the complete hook payload budget
- **THEN** the server returns exact bounded prefixes with character-column continuation, including UTF-8 and JSON overhead in the limit, and does not mark omitted or partial lines as fully covered

#### Scenario: Concurrent or subsequent run
- **WHEN** another Agent run starts
- **THEN** its added-context deduplication is independent of previous runs

### Requirement: Bounded final evidence review
An enabled candidate MAY request one final evidence review within its existing SDK session, preserving supported findings, citations and the output schema. The review SHALL NOT inject reference answers, change the turn cap, or claim that self-review establishes factual correctness. Its extra continuation cost SHALL be included in end-to-end timing.

#### Scenario: Natural completion with turn headroom
- **WHEN** the Agent first attempts to finish and observed parent responses leave review headroom
- **THEN** it receives one evidence and uncertainty consistency check and the service parses the final terminal result

#### Scenario: Repeated stop or near turn cap
- **WHEN** review has already been requested, stop_hook_active is true, or observed responses are near the unchanged turn cap
- **THEN** the hook does not request another review or force early termination

### Requirement: Title generation outside the answer writer transaction
Log Analysis and Project Expert chat services SHALL commit their chat exchange before generating an optional initial title. Title metrics SHALL retain their independent persistence without waiting on an uncommitted exchange owned by the caller. This persistence correction SHALL NOT modify analysis text and its measured saving SHALL be reported separately from Agent execution improvements.

#### Scenario: Title generation fails or is cancelled
- **WHEN** title generation times out, fails or is cancelled after the exchange commits
- **THEN** the saved question and answer remain durable with the existing title

#### Scenario: User changes the session during title generation
- **WHEN** the session is renamed, deleted, changes owner or receives another message before the title returns
- **THEN** the generated title does not overwrite the newer session state
