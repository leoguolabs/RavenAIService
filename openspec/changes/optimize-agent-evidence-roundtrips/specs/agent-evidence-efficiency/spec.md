## ADDED Requirements

### Requirement: Optional batch-first evidence plan
Log Analysis MAY append a default-off batch-plan instruction when the efficiency snapshot and evidence tools are available. When enabled, the instruction SHALL ask the model to group currently known independent ordinary text searches into one search batch and decisive ranges into one read batch, then use result-dependent follow-up batches. It SHALL preserve all original tools, attachments, required Skills, source checks, pagination, counterevidence, output schema and uncertainty requirements; it SHALL NOT add a model call, lower the turn cap, or force an early answer. A generic failure result SHALL NOT be narrowed to a specific component, node or source branch unless the observed evidence distinguishes that branch; deployment conventions SHALL remain hypotheses unless directly verified.

#### Scenario: A batch plan is enabled with a complete snapshot
- **WHEN** the independent batch-plan flag and efficiency snapshot are both enabled for Log Analysis
- **THEN** the system prompt tells the model to combine known independent requests in one MCP search call, read decisive ranges together, and batch subsequent known gaps after inspecting results
- **AND** it leaves native tools available for discovery, Git, complete deterministic computations, binary/unsupported work and evidence-tool recovery
- **AND** it requires source checks across relevant failure branches before attributing a generic error to one specific component

#### Scenario: A batch plan is disabled or the snapshot is unavailable
- **WHEN** the batch-plan flag is off, efficiency is off, or complete snapshot preparation fails
- **THEN** the additional batch-plan instruction is absent and the prior prompt/fallback behavior remains unchanged

#### Scenario: Batching does not reduce evidence scope
- **WHEN** the model follows the batch plan
- **THEN** batching changes only dispatch grouping; the model still follows relevant pagination, attachment/source coverage, required Skills, counterevidence and uncertainty rules, with no new fixed call/turn budget or stop condition

### Requirement: Independently gated search endpoint context
Log Analysis MAY enable a default-off endpoint supplement for verified structured or supported native searches over task logs. It SHALL require the efficiency snapshot and preserve original commands, results, tools, pagination and budgets. Single literal topics and literal alternatives MAY receive exact first/last matching-line context when those complete lines are absent from the visible page and requested context.

#### Scenario: A represented topic changes after the preview
- **WHEN** a stable bounded full-file replay verifies the original structured page but its first or last matching line is not fully visible
- **THEN** the same tool return may include those missing endpoints with native lines, source identity, physical match counts, explicit omitted context and a statement that event history remains incomplete
- **AND** endpoints are file-order samples, not chronological extrema, causal proof or evidence of an unchanged intervening state

#### Scenario: Endpoints are already visible or verification fails
- **WHEN** the complete endpoint lines are already in the page/context, or path, content, encoding, grammar or budget cannot be verified
- **THEN** no unsupported supplement is added, original output remains unchanged and native tools remain available

#### Scenario: Only endpoint supplementation is enabled
- **WHEN** the endpoint flag is on and the existing native topic flag is off
- **THEN** native Bash and structured MCP search matchers are registered for this mechanism; original command execution, unsupported native syntax, other optional mechanisms and model parameters remain unchanged

#### Scenario: A native glob preview hides a late state
- **WHEN** a supported line-numbered literal grep over one basename glob or recursive directory, optionally followed by one ASCII case-insensitive literal exclusion, ends in a bounded head page
- **THEN** the hook may scan only the contained files named in that page, verify their exact filtered prefixes, and append unseen first/last matching lines from those stable files without executing or rewriting the command
- **AND** per-file counts retain the exclusion, native_page_scope explicitly marks the global query incomplete and other files unscanned, and no ordering, absence or total-count claim is made about other files
- **AND** unstable files/directories, unmatched output, unsupported locale-sensitive filtering or exhausted existing scan/output budgets preserve original behavior without a supplement

#### Scenario: Native option and filename-prefix forms differ
- **WHEN** grep uses combined or separate supported options, including binary-as-text, or a single-file result includes the exact requested filename before its line numbers
- **THEN** verification may normalize only the parsed option representation and compare the complete output against either exact native prefix form; it never rewrites the command or result, guesses a filename, or accepts arbitrary extra output
- **AND** case-sensitive Unicode literal topics are allowed while unsupported regex and locale-sensitive exclusions retain native fallback

### Requirement: Optional per-topic native log context
Log Analysis MAY independently enable a default-off supplement for supported literal native log searches whose shared head preview omits an alternative. The server SHALL require the SDK callback's actual absolute cwd, contained stable regular log files and exact original-output verification. It SHALL retain the command and stdout, never evaluate shell syntax, and label physical matching-line counts and bounded first/last context separately from event coverage or causal certainty.

#### Scenario: Frequent alternatives fill a shared preview
- **WHEN** a verified literal alternative has matches in the full file but none in the original preview
- **THEN** bounded counts and exact first/last adjacent rows may reach the next model request without another model-mediated retrieval, with file identity, native references and explicit omissions

#### Scenario: Directory, grammar, bytes or output cannot be verified
- **WHEN** the cwd is absent, syntax is unsupported, a path escapes task logs, a file changes, binary/processing limits apply, or original stdout differs
- **THEN** no supplement is added and the original tool result remains available

#### Scenario: Supplement reaches the SDK
- **WHEN** an isolated pinned-SDK replay executes the supported original search
- **THEN** both original stdout and complete bounded supplementary evidence are verified on the next request, without treating that proof as final-answer or latency acceptance

#### Scenario: Supplement is disabled or preparation fails
- **WHEN** the independent flag is off, the efficiency snapshot is unavailable or MCP prerequisites are missing
- **THEN** the existing hook/tool/prompt behavior remains unchanged

#### Scenario: Structured log search omits a literal alternative
- **WHEN** a supported MCP literal OR query has a verified page, totals, continuation and requested context over stable contained log files
- **THEN** the same switch may supplement alternatives absent from visible matches/context, with request index, per-file counts and exact bounded adjacent rows
- **AND** paths resolve against the evidence server workspace; the whole batch shares file, byte, time, output and native/MCP deduplication budgets without modifying the original response

#### Scenario: Directory scope or encoded text cannot be verified
- **WHEN** a log directory changes, processing bounds are exceeded, or a matching line contains invalid UTF-8
- **THEN** no completeness claim is added for that query; unrelated invalid bytes may remain as explicitly escaped context, and unsupported/error requests retain their original evidence without suppressing valid siblings

### Requirement: Optional batched full-file statistics
Log Analysis MAY expose a separately gated, default-off statistics tool when the efficiency snapshot is available. It SHALL retain original tools and accept bounded independent regex queries against individual contained regular files. Counts SHALL distinguish occurrences from matching native LF lines; numeric extrema SHALL preserve exact finite values and original references, without float rounding, event deduplication or diagnosis.

#### Scenario: An interior sample contradicts the first and last samples
- **WHEN** a complete numeric scan finds an extremum away from the file boundaries
- **THEN** the tool returns that exact captured value and native location together with scan identity and coverage, without an additional model-mediated read

#### Scenario: Scan or distribution is incomplete
- **WHEN** byte, time, line, encoding, capture, file-change or distribution limits prevent complete processing
- **THEN** completeness fields and omissions remain explicit, no partial result claims global absence/range, and original tools remain available

#### Scenario: Regex is pathological or the Agent is cancelled
- **WHEN** a worker exceeds its timeout or its request is cancelled
- **THEN** its subprocess is terminated and reaped, independent batch requests retain their own results, and the Agent does not retry a committed model attempt

#### Scenario: Statistics are disabled or prerequisites are unavailable
- **WHEN** the separate flag is off, the efficiency snapshot is missing or the provider lacks MCP support
- **THEN** the existing tool set and prompt behavior remain unchanged

#### Scenario: Pinned SDK invokes the new tool
- **WHEN** an isolated SDK fixture requests a numeric scan
- **THEN** its next request receives the correct extrema, source references and completeness fields; this delivery test and retained-log replay do not establish latency or final-answer quality

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

#### Scenario: Ordinary native Bash retains an earlier working directory
- **WHEN** a previous Bash call changed directory within the task workspace
- **THEN** ordinary Log Analysis guidance identifies the workspace as the initial directory and directs Bash reads/searches to absolute workspace paths and Git to an explicit repository path
- **AND** independent calls do not rely on another call changing directory; the service preserves commands, errors and tool behavior, and a relative-path error does not establish file absence

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

#### Scenario: A verified log field needs a source declaration
- **WHEN** the existing source-context flag is enabled and a structured search returns complete zero-column native log rows with supported C source/function hints
- **THEN** a bounded optional scanner may attach a source relation only after verifying the log bytes, unique contained source file, matching literal message format/argument, declared struct/pointer aliases, same-member constant comparison and unambiguous included enum body
- **AND** named-symbol shadowing, macro overrides, unsupported syntax/formatting, source changes, ambiguity or processing limits retain the original evidence and native tools
- **AND** the relation contains raw source, identity hashes and explicit non-exhaustive/source-version limits, without evaluating enum values or inferring compiled branches or event state

#### Scenario: Related declarations share the original context budget
- **WHEN** existing source windows already occupy available slots or the complete relation cannot fit the unchanged shared two-window/8000-byte payload
- **THEN** original source windows retain priority and no clipped relation is added
- **AND** unchanged emitted source relations are deduplicated per run; no AST/log buffers persist, optional-parser failure adds no tool error, and no model request, turn cap, required Skill or original tool result changes

#### Scenario: A bounded header preview starts inside an enum body
- **WHEN** the normal verified C/H search-context start falls inside an error-free parsed enum body
- **THEN** the same default-off hook may start at the enum beginning only when the entire enum body and original match fit the existing 160-line and shared byte limits
- **AND** it preserves the original tool result, exact source rows, identity validation and native fallback, without evaluating implicit values, macros, preprocessing conditions or deployed state
- **AND** missing parsers, incomplete syntax or over-budget enums retain the bounded original workflow and never gain a completeness claim

#### Scenario: A literal short shell read ends inside a source function
- **WHEN** a successful Bash literal `sed -n` range shorter than 80 lines matches a unique workspace source file and the full returned text, allowing only the SDK's removed final LF
- **THEN** the same default-off hook may append complete parsed C/H functions within the existing two-window/8000-byte budget, marked with `source_read_line`, without rewriting or rerunning the command or replacing stdout
- **AND** full-function deduplication preserves previously unseen branches; syntax, path, content or size uncertainty retains the original workflow without a partial added window

#### Scenario: Short shell read context reaches the real SDK
- **WHEN** the pinned SDK executes a supported literal source read
- **THEN** an offline SDK exchange verifies that the original result and the exact added function reach the next request, while retained-command replay and this exchange remain separate from real-model latency and quality acceptance

#### Scenario: Short MCP source read stops within a function
- **WHEN** a batch read requests fewer than 80 lines at zero column and every returned row matches the indexed request, canonical repository path and stable source snapshot
- **THEN** the same default-off hook may append missing complete C/H functions within the shared two-window/8000-byte budget and cross-tool deduplication, using `source_read_line`
- **AND** original arguments, results, per-request errors and continuations remain unchanged; tail reads, truncated or changed rows, unsupported shapes, complete reads and over-budget functions receive no partial added excerpt

#### Scenario: MCP read context reaches the real SDK
- **WHEN** the pinned SDK executes a supported short evidence read with the registered hook
- **THEN** an offline exchange verifies delivery of the original result and exact missing function branch to the next request, without treating replayed opportunities as measured model-call savings

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

### Requirement: Independently gated exact regex endpoint replay
Log Analysis MAY enable an independent default-off replay for supported task-log regex searches. It SHALL use the original ripgrep argument builder, verify the original match page and continuation, and validate source identities/content before appending omitted first/last LF matches. A batch SHALL bound source files, directory entries, unique source bytes, elapsed scan time and total added output. Supplement failures SHALL retain the original page; cancellation SHALL propagate and reap scan subprocesses.

#### Scenario: Later matching state is outside the page
- **WHEN** the complete bounded regex replay agrees with the original page and sources remain stable
- **THEN** missing first/last matches may be appended with source hashes, exact physical counts and explicit file-order sampling limits, without changing original evidence or paging

#### Scenario: Replay fails or the Agent is cancelled
- **WHEN** source, encoding, scope, time or output limits prevent a verified supplement
- **THEN** the original page remains available, without claiming complete event history or causal certainty
- **AND** cancellation propagates and terminates/reaps any active replay process

### Requirement: Independently gated captured-set comparison
Log Analysis MAY expose a default-off batched comparison tool under a complete efficiency snapshot. Each request SHALL name one contained file, a bounded regex with group/value captures, and two distinct group identifiers. The isolated worker SHALL preserve native LF references, physical occurrence counts, unique values, and stable-source identity. It SHALL bound files, lines, regex runtime, selected-group values and output.
The parameter descriptions SHALL distinguish captured group values from capture names, with a generic example independent of validation cases. Invalid capture feedback SHALL explain the required names and selected-value semantics.

#### Scenario: Complete selected groups
- **WHEN** a stable complete scan indexes both selected captured-value sets
- **THEN** it returns common count and exact only-left/only-right captured values, with duplicate counts and source references
- **AND** it does not infer physical deletion, chronology or causal attribution from those differences

#### Scenario: Unobserved group or incomplete scan
- **WHEN** no matching record belongs to a selected group
- **THEN** that group is explicitly unobserved, distinct from a confirmed empty snapshot
- **AND** comparison completeness is false and all difference fields are unavailable, even if the physical regex scan is complete; feedback asks for source verification or original-tool fallback
- **WHEN** processing or value limits prevent a complete comparison
- **THEN** difference fields are unavailable and limits remain visible; original evidence tools remain available

### Requirement: Unambiguous recovered result fields
Lenient terminal recovery SHALL NOT absorb known result fields across a mismatched apostrophe closing a JSON string. A structurally ambiguous object SHALL remain raw diagnostic evidence and SHALL NOT become a successful or plain-Skill result. Complete valid JSON string examples SHALL remain unchanged. This validation SHALL NOT dispatch a model retry.

#### Scenario: Malformed result field boundary
- **WHEN** standard string decoding fails and a mismatched apostrophe boundary precedes another known result field before any valid closing delimiter
- **THEN** the entire result is rejected rather than swallowing later fields into answer or returning an earlier answer with corrupted summary
- **AND** raw terminal text remains available without an additional model call
