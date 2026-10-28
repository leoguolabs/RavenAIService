## ADDED Requirements

### Requirement: Optional same-session staged evidence effort
Log Analysis MAY use a separate default-off experiment to collect evidence at low effort and finalize at high effort in the same SDK session. It SHALL require a complete efficiency snapshot, verified extended-context options and no final-review Stop hook. The experiment SHALL be limited to the verified SDK version and official model/endpoint combination; unsupported combinations, prior-session options and dollar-budget options SHALL retain the original query.

#### Scenario: Collection succeeds without a machine-readable stage marker
- **WHEN** the SDK completes collection successfully with a resumable session
- **THEN** SDK completion, not model-authored prose, controls the transition; finalization resumes the same session with original evidence/tools, static phase instructions and the original JSON contract

#### Scenario: Shared limits and endpoint commitment
- **WHEN** either stage is running or the transition is cancelled
- **THEN** both stages remain in one routed attempt and outer timeout, finalization receives only remaining turns from the unchanged total cap, cancellation closes the active iterator and no committed attempt is replayed

#### Scenario: Shell cwd differs from retained conversation history
- **WHEN** native Bash changes directory within collection and the SDK resumes finalization
- **THEN** the static bilingual protocol directs Bash reads/searches to absolute workspace paths and Git to `git -C`, without rewriting commands, resetting shell state or treating a relative-path error as evidence of absence
- **AND** actual SDK validation distinguishes preserved tool history from the resumed shell's original workspace; this mechanism proof is not performance acceptance

#### Scenario: Draft or failure before finalization
- **WHEN** collection emits a draft, SDK rejection or structured analysis error
- **THEN** draft text remains process trace rather than final-answer deltas, SDK rejection fields remain available to the router, and errors do not start another stage or promote a draft to a successful answer

#### Scenario: Finalization completes or fails
- **WHEN** the resumed stage produces its terminal result
- **THEN** only that result supplies the final answer/error, invocation token counters/turns/duration are combined, and session-cumulative API duration and cost are not added twice
- **AND** implementation checks do not establish semantic equivalence or performance acceptance

### Requirement: Avoid empty token-progress trace amplification
The shared SDK trace translator SHALL omit `thinking_tokens` system notices with empty translated detail while preserving actual thinking and answer text, tool evidence, descriptive notices, other system subtypes and terminal parsing. It SHALL NOT filter SDK messages before routing or cancellation checks, change model inputs, or remove idle transport heartbeats.

#### Scenario: Per-token progress floods a run
- **WHEN** SDK progress messages contain no translated detail
- **THEN** they do not allocate trace sequence numbers, enter replay/persistence buffers or produce SSE frames, while the same useful evidence and final answer remain available

#### Scenario: Cancellation during progress or a descriptive notice
- **WHEN** cancellation is requested while only token progress is arriving, or a progress message contains a description
- **THEN** cancellation is still checked on that SDK message and the descriptive notice remains eligible for normal masked emission

### Requirement: Independently gated verified SDK context capacity
Log Analysis MAY use a separately gated, default-off SDK context-capacity annotation for the verified official DeepSeek Flash endpoint and exact model alias. Resolution SHALL follow the selected endpoint and caller override, preserve the outgoing model and generation parameters, and leave unknown endpoints/models and global settings unchanged. It SHALL NOT require a prepared evidence snapshot or change other Agents.

#### Scenario: Verified official endpoint with the experiment enabled
- **WHEN** the selected provider is deepseek, model is deepseek-flash and the endpoint is the official HTTPS Anthropic path on its standard port
- **THEN** the SDK uses deepseek-flash[1m] for local context accounting while preserving auxiliary models, tools, output limit, reasoning effort, turn cap and timeout

#### Scenario: Experiment disabled or endpoint unverified
- **WHEN** the flag is off, the selected endpoint or model is unverified, or the caller explicitly supplies a different model
- **THEN** existing SDK model handling is retained without inferring capacity from primary settings or another run

### Requirement: Bind SDK background models to the selected endpoint
Each SDK invocation SHALL override both current and legacy background-model environment names using its resolved endpoint auxiliary model, falling back to the effective main model when absent. Calls without an endpoint SHALL resolve runtime settings before provider defaults. This SHALL NOT mutate global environment or change the analysis model, prompt or schema.

#### Scenario: Runtime provider changes while container defaults remain old
- **WHEN** the process environment still names a previous provider's auxiliary model
- **THEN** SDK background requests use the current run's configured auxiliary model at its selected endpoint

#### Scenario: A backup endpoint differs from primary settings
- **WHEN** a run uses a resolved backup endpoint
- **THEN** its background model comes from that endpoint and never from the primary settings or another concurrent run

### Requirement: Preserve investigation semantics
The optimized Agent SHALL retain all attachments, project-fit checks, original evidence access, required skills, output schema and uncertainty semantics. Disabled optimization SHALL use the existing execution path.

#### Scenario: Startup snapshot available
- **WHEN** optimization is enabled and a sanitized complete task/catalog snapshot is prepared
- **THEN** the Agent can use it instead of repeating startup discovery calls, while still checking project fit

#### Scenario: Built-in prompts require the already completed startup read
- **WHEN** a complete snapshot is available and built-in system/user templates still require reading task.json first
- **THEN** both templates use the prepared task data, retaining fallback reads for missing fields and interpolating user content only after the instruction change

#### Scenario: Snapshot unavailable
- **WHEN** snapshot preparation fails
- **THEN** the Agent retains the original discovery and task reading workflow

### Requirement: Reject ambiguous structured terminal output
Log Analysis and Project Expert SHALL reject repeated JSON object keys in complete results and recoverable prefixes, including nested objects. The raw terminal response SHALL remain available, and optional-Skill fallback SHALL NOT turn rejected JSON into a successful plain-text answer. This validation SHALL NOT add model calls or substitute an earlier draft.

#### Scenario: Terminal response contains repeated answer or summary fields
- **WHEN** the final response contains duplicate fields, even after an otherwise usable answer
- **THEN** the service reports schema_mismatch and retains the raw final response without selecting a first or last field value

#### Scenario: Unique-key result contains quoted field names
- **WHEN** field names occur inside answer strings or a supported unique-key truncated tail is recovered
- **THEN** existing answer content is retained without treating literal text as duplicate object fields

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

#### Scenario: Search with nearby evidence in one round trip
- **WHEN** a search requests surrounding context
- **THEN** at most three matches receive merged bounded context windows, all original matches remain, and every incomplete context range includes an exact continuation without claiming whole-file coverage

#### Scenario: Read the end of a file
- **WHEN** a read request selects tail_lines instead of absolute range coordinates
- **THEN** the tool returns the file ending with native LF line numbers and total line count, preserving long-line continuation and rejecting conflicting coordinates

#### Scenario: Compact evidence representation
- **WHEN** MCP results are encoded as rows with shared column legends and derived read parameters
- **THEN** every returned source character, location, error and continuation is preserved, and nonstandard parameters fall back to the original representation

#### Scenario: Advanced regex is supported by the runtime
- **WHEN** a query uses lookaround or backreferences and ripgrep has PCRE2 support
- **THEN** the tool selects the compatible engine without a failed-query model round trip, retaining the same query timeout and workspace bounds

#### Scenario: Terminal carriage returns in a log
- **WHEN** a file contains standalone carriage returns or Unicode separator characters within LF-delimited lines
- **THEN** searches, subsequent reads and source verification use the same native LF line numbers and preserve those characters within the returned line

### Requirement: Optional deterministic log overview
Log Analysis MAY append a bounded structural log overview only when its separate default-off switch and the main efficiency gate are enabled with a complete startup snapshot. The overview SHALL retain per-file boundaries, native LF references, scan coverage and explicit omissions, SHALL NOT claim semantic diagnosis or deduplicated events, and SHALL preserve native evidence access without additional model calls.

#### Scenario: Original files are fully scanned
- **WHEN** files fit the time, byte, line and preview budgets and remain unchanged
- **THEN** the overview reports physical record counts and exact first/last sample locations without treating severity or frequency as a cause

#### Scenario: Input or output exceeds the overview budget
- **WHEN** a file is skipped, changes, has unrecognized lines, or exceeds a scan/preview limit
- **THEN** the overview reports the corresponding coverage limits and does not imply missing results are absent from the originals

#### Scenario: Overview fails or is disabled
- **WHEN** the separate switch is disabled, the startup snapshot is unavailable or scanning raises an error
- **THEN** the original native evidence workflow remains available without an overview

#### Scenario: A rare severe event follows frequent warning groups
- **WHEN** more than twelve indexed warning groups include a low-frequency severe event
- **THEN** compact column-labelled group rows retain every indexed group that fits the unchanged encoded preview budget, without a frequency-based top-twelve cutoff
- **AND** the previous preview's visible groups and file inventory are preserved before additions use severity, file and native-line priority, with exact sample prefixes/counts/locations and explicit omissions without implying causal relevance

#### Scenario: Compact inventory exceeds the preview budget
- **WHEN** file rows or group samples cannot fit the complete UTF-8 JSON budget including legends and omission metadata
- **THEN** omitted file rows have no surviving group references, omissions remain explicit, and an impossible metadata budget falls back to native investigation rather than returning oversized or misleading coverage

#### Scenario: Compact column overhead would remove previously visible evidence
- **WHEN** the v2 preview cannot retain all groups and file inventory already shown by the prior bounded preview
- **THEN** the complete prior v1 preview is returned within the same byte limit instead of replacing old evidence with different samples

### Requirement: Optional coalescing of short native source reads
Log Analysis MAY expand a short native source Read when a separate default-off switch, the efficiency gate and a complete startup snapshot are present. The actual range SHALL contain every originally requested line and retain native output semantics, all tools and the turn cap. Added context SHALL be bounded and identified to the model, without claiming whole-file coverage or adding a model continuation.

#### Scenario: Short source read fits the context budget
- **WHEN** an explicit valid range under 80 lines targets UTF-8 source within a workspace repository
- **THEN** the SDK may read a surrounding range up to 160 lines and 8000 source/line-format bytes, retaining the entire requested range and reporting actual coordinates

#### Scenario: Original request or file cannot be safely expanded
- **WHEN** the input is a log, Skill, external path, oversized or unsupported file, invalid range, or the original range itself exceeds the expansion budget
- **THEN** the hook returns no modification and native Read handles the original request

#### Scenario: Short read is contained in a complete C function
- **WHEN** an error-free parsed C/H function contains every requested line and fits the same 160-line and byte bounds
- **THEN** the hook may return that complete function, including more than 32 preceding lines, without unrelated trailing functions or loss of requested evidence
- **AND** an already complete function read does not expand merely to fill the budget

#### Scenario: Byte-count cache sees a different file identity
- **WHEN** the device, inode, size, modification time, change time or bounded content digest differs
- **THEN** cached line byte counts are replaced before expansion; cache state remains bounded and isolated per run

### Requirement: Automatic source context for native searches
When efficiency and the separate source-context switch are enabled, successful native or batch line-number source searches MAY receive bounded additional source excerpts without an extra model request. The source-context switch SHALL default off. The server SHALL verify that each source path is inside a workspace repository and that its referenced line matches the search output. Original tool outputs, tool access, total turn budget, necessary retries and evidence requirements SHALL remain unchanged. Added-context state SHALL be isolated per run.

#### Scenario: Batch search uses the shared source-context path
- **WHEN** the pinned SDK returns complete, column-labelled matches from the evidence search tool
- **THEN** all requests share the same two-window and 8000-byte context budget and native/batch added-context deduplication
- **AND** original matches, per-request errors, pagination and requested context remain unchanged; unsupported or excerpted match rows receive no automatic enrichment

#### Scenario: Batch tools enabled without automatic context
- **WHEN** efficiency is enabled but the source-context switch is off
- **THEN** batch tools and the startup snapshot remain available without registering the source-enrichment hook

#### Scenario: Native search locates a source definition
- **WHEN** a native line-number search returns a verified repository source reference
- **THEN** the SDK can add a bounded surrounding excerpt with exact lines and continuation while retaining the original search result

#### Scenario: A complete function fits the remaining context budget
- **WHEN** verified C/H hits include an error-free function definition or a location inside that function
- **THEN** definition hits are preferred for added context and a complete function may use the remaining total payload budget within the unchanged line bound
- **AND** completeness is labeled only for that syntax range and does not establish compiled branches, deployed revision, runtime behavior or whole-file coverage

#### Scenario: Function parsing is unavailable or incomplete
- **WHEN** the parser is missing, syntax or encoding is unsupported, the source changes, or the complete function exceeds the context budget
- **THEN** the existing exact window and continuation behavior remains available without a false function-completeness claim

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
An enabled candidate with the separate final-review switch enabled MAY request one final evidence review within its existing SDK session, preserving supported findings, citations and the output schema. The final-review switch SHALL default off and SHALL be independent of automatic source context. The review SHALL NOT inject reference answers, change the turn cap, or claim that self-review establishes factual correctness. Its extra continuation cost SHALL be included in end-to-end timing.

#### Scenario: Batch tools enabled without final review
- **WHEN** efficiency is enabled but the final-review switch is off
- **THEN** the Agent uses its natural terminal result without a review continuation or Stop hook

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

#### Scenario: Optional title request remains pending after the answer is saved
- **WHEN** the analysis answer has been committed and its terminal state published
- **THEN** answer subscriptions finish without waiting for optional title generation or SDK timeout cleanup, while the existing background job can finish the guarded title update

#### Scenario: Title work is cancelled after answer completion
- **WHEN** optional title work is cancelled after the subscriber receives the saved answer
- **THEN** the completed answer and run state remain unchanged and no new analysis error event is emitted

### Requirement: Metadata-only sidebar run status
The session-list API SHALL read only run identity, status and timestamp metadata for its latest-run summaries. It SHALL NOT hydrate historical answers, request payloads or full trace JSON, and SHALL preserve the existing ownership filter, ordering and active-run overlay.

#### Scenario: Visible sessions have large historical traces
- **WHEN** the workbench requests its session list while an Agent is running
- **THEN** the same latest-run metadata and owner-scoped active state are returned without loading historical trace or answer payloads
