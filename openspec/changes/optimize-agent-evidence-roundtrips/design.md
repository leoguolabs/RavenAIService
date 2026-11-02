## Context

Baseline source is the current local commit, recorded separately from production revision. The production log list supplies original archive bytes, questions, prior answers and attachment groups. Existing Docker volumes are preserved; validation gets isolated data and runtime settings. Production is only read/downloaded.

## Goals / Non-Goals

Goals: reduce model round trips with unchanged evidence access, output schema, conclusions and uncertainty; measure at least 20 paired rounds and at least 10 distinct subsequent log analyses. Minimize median analysis time without a fixed percentage threshold.

The user revised the target from 30% to 20% on 2026-10-29, then removed the fixed percentage threshold on 2026-10-31 and requested a concrete optimization result. Compare the final candidate's fresh end-to-end API cohort with the frozen original baseline. Keep historical plans, measurements and failed quality decisions unchanged. Reduced model-response counts, precloned probes and a single faster pair do not establish acceptance.

Converge the final candidate around verified changes outside model reasoning before adding further prompt experiments. Optional title work, writer contention and empty trace amplification can be isolated from model inputs and reported separately. Experimental retrieval, context and prompt changes that lose findings or increase unsupported certainty remain excluded. Validate the selected patch set against the original analysis prompts/tools/model configuration, with exact retained-output replay as an additional invariance check; replay does not replace the required real API timing cohort or independent log review.

Non-goals: production deployment, changing model to claim an optimization, reducing the 150-turn limit, suppressing required skills, dropping attachments, hiding failures, or treating tool count as API request count.

On 2026-11-02 the user resumed work and explicitly allowed reasonable variation from independent AI analyses while retaining comparable confidence and no large result deviation. Review wording, formatting and secondary coverage separately from decisive evidence losses, contradicted factual claims and unsupported exclusion. Shared baseline errors are not gold findings. Preserve the original strict reviews and report any revised judgment separately; natural baseline variation does not excuse a demonstrated decisive error. Keep the full 20-pair plus 10-distinct-log requirement and measure real model-response changes separately from the four platform-only improvements.

The 30-pair audit exposed a retrieval gap not addressed by the previous missing-topic hook: a topic may appear early while its final state is omitted from the shared preview. Add an independent default-off structured-search endpoint supplement. Verify the original page and requested context against stable bounded files, then append unobserved first/last matching lines in the same tool return. Keep old results/pagination intact, report exact counts and file-order sampling limits, and avoid another model-mediated search solely to retrieve those endpoints. This does not infer a diagnosis, force early completion or guarantee fewer model calls. No case-specific findings enter the implementation or prompts; real-log replay is only mechanism evidence before a fresh quality screen.

The first endpoint screen preserved the main inventory finding but supplied only camera-keyword endpoints; the decisive inventory was found in a subsequent Bash query. The retained original missed-evidence query uses a native basename glob, a literal exclusion and head, outside the structured-only mechanism. Extend the same default-off switch to verified native literal previews. For glob queries, scan only files named in the observed page, verify their exact filtered prefixes and retain all source/output budgets. Counts describe those stable files after the literal exclusion; explicitly mark the overall query incomplete and other files unscanned. Do not expand a potentially large glob or infer global ordering/absence. The original retained command must deliver its missing endpoint through the actual pinned SDK before a new real-model pair. This exact-command proof does not replace fresh quality or end-to-end latency acceptance.

The subsequent native-v2 screen issued 74 Bash calls but no supported supplements: common separate grep flags, binary-as-text and recursive directories bypassed the narrow grammar. Its candidate repeated the decisive inventory omission; the observed 19.4% speed difference is not accepted. Support those ordinary option forms and case-sensitive Unicode literals without interpreting arbitrary shell syntax. Directory pages use the same visible-file-only verification and explicit incomplete scope. The offline SDK also emitted an exact filename prefix absent from retained/direct-shell single-file stdout; verify both complete forms against the requested file rather than assuming one universal rendering. Preserve the failed proof and require another fresh screen after the amended exact-query SDK check.

## Decisions

1. Keep the existing SDK loop and opt in with `agent_evidence_efficiency_enabled`. Supply a sanitized task/project snapshot only when preparation succeeds; preserve the legacy flow when disabled or incomplete. Include the complete discovery catalog to retain project-fit semantics. Do not preload full logs, source or past answers.
2. Add workspace-bound `search_evidence` and `read_evidence` MCP tools. Batch independent reads with bounded concurrency. Return exact file/line references, pagination/truncation metadata and continuation arguments. Never present a partial search as exhaustive. Preserve native tools for complex operations. Auto-select the regex engine so supported lookaround/backreferences do not require an avoidable model retry. Encode MCP rows with explicit column legends and a shared read-range template; preserve the complete returned evidence and metadata while removing repeated field names and derived parameters. Fall back to the original representation for nonstandard range metadata.
3. Provide concise guidance to group independent searches and explain the remaining evidence gap before expanding investigation. Also enrich successful native source searches through PostToolUse with at most two verified source windows (up to 160 lines each, with an 8000-byte UTF-8 limit for the entire encoded hook payload), retaining exact references and character-column continuation. Compact line tuples avoid repeated JSON field names. The actual SDK persisted oversized hook output and exposed only previews in a real trial, so the complete encoded payload, not only source-character counts, must fit the inline budget. Deduplication covers only complete lines actually returned. The server performs the mechanical search-location-to-source-read step without another model round trip. Limit reads to repository source files within the workspace, validate the matched text, skip ambiguous/unsupported outputs, and deduplicate only added context per run. Original results, tools, retry capability and turn limits remain unchanged. Automatic source context adds no model call and does not force an early answer; final review is described separately below. Reminder-only periodic checkpoints were rejected after a real trial exhausted 150 turns; no periodic reminder is retained.
4. A candidate may request one final evidence review through a Stop hook in the same SDK session. This is an additional model continuation, not a separate judge or an early stop. Preserve supported findings and citations while checking independent blockers, causal/completion claims and consistent uncertainty. Do not inject historical answers. Guard against repeated callbacks and stop_hook_active; skip near the unchanged turn cap using conservatively observed parent responses. This heuristic is not an exact remaining-turn guarantee. Parse only the terminal ResultMessage. Independent quality review, not the hook, determines acceptance.
5. Use the same pinned Docker dependencies, model endpoint and parameters for both benchmark arms. Each run uses an isolated fresh workspace with identical archive bytes and repository commit. Record cache/setup conditions; alternate AB/BA order. Record unsuccessful runs and retries without silently replacing them.
6. Persist the corpus manifest, per-run state, result and trace outside Git. The runner resumes only after inspecting the actual running process or completed output, never because a timeout in observation expired. At least 20 timing pairs and 10 distinct holdout logs are required; holdout quality has its own baseline answers.
7. Compare factual conclusions, evidence references, attachment coverage, counterevidence and calibrated uncertainty for every case. Byte-identical output is measured and reported separately: stochastic generation cannot be guaranteed identical by an execution change. Semantic quality gates do not authorize claiming byte identity.
8. A separate persistence fix commits and closes the chat exchange transaction before optional title generation. Title usage metrics use their own connection and previously waited behind the exchange's SQLite writer lock. Retain title input, locale, timeout and metrics; use a conditional update against owner, deletion state, prior title, message count and update timestamp to preserve concurrent user changes. This fix is independent of the Agent efficiency flag, changes no analysis text, and is reported as post-Agent latency rather than fewer model calls. Existing analysis-result persistence semantics are unchanged; no new atomicity guarantee across log records and chat history is implied.
9. The v10 fixed four-case pilot did not meet the latency/quality gate. It exposed conflicting mandatory task-read instructions in both base and user prompts, and mostly serial native retrieval. Resolve only the known startup instructions before user-data interpolation when a complete snapshot exists; preserve the original path otherwise. Add requested search context within the same MCP call (up to three matches, merged ranges, 4000 context characters per search) and tail reads with LF line counts. Every original search hit remains present, missing context has exact continuation, and native tools remain available. These are retrieval capabilities, not additional automatic review or implicit proof of absence/completion. Both prior hook switches remain default off.
10. A retained v9 terminal response repeated answer/summary keys and included corrupt repair text. The lenient parser previously overwrote earlier fields and reported success. Reject duplicate object keys in complete JSON and recoverable prefixes, including nested objects; retain raw terminal evidence and return schema_mismatch without an additional model call. Optional-Skill plain-text fallback must not accept a rejected JSON response. Existing unique-key complete results and supported truncated-tail/unescaped-quote recovery remain compatible. Replay stored responses as a new audit; never rewrite original benchmark answers or timings.
11. The v12 screen contains a 150-turn failure with 130 native Read calls, including repeated 30-line source windows. A separate default-off Log Analysis switch may coalesce short native source reads before execution. Preserve the entire requested range and all tool access, while adding at most 32 preceding lines and up to 160 total lines within an 8000-byte source/line-format budget. Use the SDK's supported PreToolUse updatedInput plus an explicit range notice, retaining native Read results. Skip logs, Skills, non-source files, unsupported encoding, external symlinks, oversized files and invalid coordinates. Per-run stat-validated byte-count caches are bounded to four files. This mechanism adds no model continuation, changes no turn cap, and requires its own paired evaluation; it is not evidence of an already achieved speedup or quality guarantee.

12. Public DeepSeek Flash reports a 1,048,576-token total context, but the pinned SDK 0.2.165 assumes 200,000 for its plain alias. An actual offline SDK comparison verifies the official `[1m]` marker selects 1,000,000 locally while preserving the wire model, output limit and reasoning effort. A separate default-off Log Analysis flag may apply that marker only after resolving the selected endpoint and caller model, only for the verified official HTTPS endpoint and exact alias. Preserve auxiliary models, runtime settings, other Agents, tools, turn cap and timeout. Do not fetch capabilities within every loop or extrapolate to unknown gateways/models. Record this controlled difference in a new frozen cohort; increased context may cost more per request and does not itself establish equivalent answers or lower latency.

13. Retained native source reads revisit small C functions, while fixed search windows can stop inside a function and include unrelated neighbors. Under the existing default-off context/read-window switches, prefer an error-free C syntax function range when it fits the unchanged 160-line and byte budgets. Prioritize verified definition hits over earlier call sites, and permit one complete function to use the remaining total hook payload budget rather than always reserving half for a nonexistent second window. Native Read expansion may reach the function start beyond the ordinary 32-line look-behind, but must retain every requested line. Keep original search output and exact source text. Missing parser, unsupported encoding/language, syntax errors, source changes and oversized functions fall back to the existing window/continuation behavior. Cache only ranges for four stat/content-validated files per run; bounded double reads and SHA-256 detect same-size changes even when filesystem timestamps collide. Limit search-context discovery to eight source files and preserve all original search output. Function completeness is syntactic, never evidence of compiled branches, deployed identity or runtime execution. Pin the parser versions verified with the Docker Python runtime; offline overlap counts do not establish saved calls or quality acceptance.

14. Retained real-model traces also contain short Bash `sed` reads that stop inside a C function, bypassing both line-number search enrichment and native Read expansion. Under the existing default-off source-context switch, recognize only literal ranges shorter than 80 lines, optionally with literal `cd`, `;`/`&&` and `echo` separators. Do not execute a parser, rewrite shell commands or substitute tool results. Match the complete requested text to stdout and a unique contained repository file; normalize only the final LF removed by the pinned SDK. Unsupported shell syntax, transformed/truncated output, ambiguous paths, source changes and non-C functions add nothing. Append only whole parsed functions that fit the existing two-window/8000-byte budget, mark `source_read_line`, and deduplicate by full function coverage rather than merely the anchor line. A too-large function keeps native access without another partial excerpt. Actual SDK wire proof and retained-command replay are mechanism evidence; they do not establish a reduction in model requests or equivalent final answers.

15. On 2026-11-02 the user explicitly requested another local experiment targeting fragmented model/tool calls while preserving findings and confidence. The prior unpaired case-01 run used 31 Bash calls and zero MCP calls even though its system prompt already contained the generic batching instruction; its system/user guidance hashes confirm that the supposed skill-guidance treatment did not change the prompt. It is not a timing pair and its source-fix claims are not accepted. Add a separate default-off Log Analysis batch-plan prompt that requires a concrete first search batch and a grouped decisive-range read, followed by evidence-driven batches with no lower turn cap or reduced source/attachment coverage. Keep this as a new treatment, record its actual SDK tool trace, and compare only fresh randomized same-input arms. The first v1 case-01 screen reduced 204.98→147.52s, but both arms had 15 SDK response IDs and 27 tool calls; the candidate shifted five MCP searches into Bash and included an unsupported exact-node claim. Reject it. The v2 prompt adds a general failure-branch ambiguity guard. Its fresh same-input case-01 screen reduced 245.96→194.20s (21.0%), with tool calls 39→30 and assistant response IDs 24→23. Independent review found the same primary cause, severity, unknown flash completion/reboot status, and material actions; the candidate did not regress the central finding. Both answers still name cdev0 although source permits either cdev0/cdev2 and the log records only return -1; preserve this shared uncertainty as an open quality caveat. The v2 pair passes only the one-case primary-equivalence screen, not performance acceptance. Proceed to the fresh 20-pair and 10-distinct-log cohort, retaining all earlier screens.

## Risks / Trade-offs

The isolated context-capacity screen removed two compactions but its final answer
still claimed a whole-track numeric range contradicted by unsampled original
records. The existing overview contains warning groups and first/last prefixes;
it is not a field-range index. Add a separate default-off Log Analysis statistics
tool under the efficiency/snapshot gate, preserving all original tools. Batch
1-8 explicit per-file Python regex queries for counts, literal captured values or
exact decimal/hex extrema with original LF references and sample coordinates.
Retain explicit byte, line, encoding, time, mutation and distribution limits;
partial scans cannot establish absence. Bound workers and isolate regex execution
in cancellable subprocesses so pathological patterns cannot hold the Agent loop.
The tool performs no cross-file event deduplication or semantic inference, adds
no model request, and does not preload case-specific review findings. Verify
original-log extrema and actual SDK delivery before a new paired model screen;
mechanism checks are not timing or quality acceptance.

The next retained screen used MCP reads rather than Bash reads, so the short-shell
mechanism never activated. Extend the same source-context hook to short MCP source
ranges without changing the tool result or arguments. Bind each response to its
request index and canonical repository path; verify every native line and character
against a stable source snapshot before appending only a complete C/H function.
Skip column/tail reads, malformed or truncated rows, changed source, already complete
functions and over-budget functions. Share the existing two-window/8000-byte budget,
per-run coverage and `source_read_line` metadata across all tools. Keep the gate
default off; pinned SDK delivery and retained replay establish mechanism coverage,
not a latency or semantic-quality pass. Prompt guidance describes this same retrieval
capability without adding another review or reducing evidence requirements.

The staged API pilot used many batch searches, but automatic source enrichment
was attached only to native Bash searches. Extend the same default-off hook to
the pinned SDK's MCP search content blocks. Decode only the existing labelled,
complete match rows, verify them against workspace source and reuse the same
function selection, whole-hook byte budget, continuation and cross-tool
deduplication. The entire batch shares two windows/8000 bytes; original search
matches, errors, paging and requested context stay unchanged. Unknown wire shapes
fall back without enrichment. A real offline SDK exchange must demonstrate that
MCP PostToolUse context reaches the next model request; offline overlap counts
alone do not establish fewer requests or an end-to-end improvement.

The private low-collection/high-finalization probes retained original tool history
in one resumed SDK session and reduced exploratory output on two calibration
cases, but still produced unsupported exclusions and whole-window claims. They
exclude HTTP/cold-clone overhead and are not timing or quality acceptance. Add a
default-off integration so the actual API can be evaluated: one router attempt,
unchanged outer timeout, total turns shared across phases, and original tools,
Skills and output contract. Use a static bilingual phase protocol plus the latest
server task prefix because the pinned SDK ignores replacement system prompts on
resume. SDK success, not a fragile model-authored marker, controls the transition.
Only the final result is parsed; first-stage SDK/analysis errors remain terminal.
Keep collection prose in trace rather than answer deltas, and check cancellation
at hidden result/cleanup boundaries. For the verified SDK, combine per-invocation
usage/turns/duration but retain final cumulative API duration/cost. Gate on SDK
0.2.165, verified official Flash extended-context options, a complete snapshot
and no Stop review; unsupported or pre-resumed/dollar-budget options pass through.
Final-stage prompts preserve uncertainty and independent defects but self-checks
are not independent quality validation. No feature is enabled by default based on
these feasibility results.

A later real-model screen exposed repeated relative-path recovery when Bash
inherited a prior `cd` or resumed at the original workspace. A deterministic
offline exchange with SDK 0.2.165 reproduces both behaviors and verifies that an
absolute workspace path reads the unchanged evidence across resume. Add bilingual
path guidance to the existing static stage protocol: use absolute workspace
paths for Bash reads/searches and `git -C` for Git. Preserve commands, tools,
history and limits; do not silently reset cwd or infer file absence from a failed
relative lookup. This narrow prompt correction does not adopt the unsuccessful
private no-thinking/answer-format trials or establish a performance gain.

A subsequent ordinary (unstaged) native-tool screen reproduced two cwd-dependent
path failures. Apply the same absolute-path guidance to the ordinary bilingual
Log Analysis prompt and runtime workspace hint. Describe the initial directory
without implying that Bash always remains there; independent calls must not rely
on another call's `cd`. Do not reset cwd, rewrite commands, hide errors or infer
file absence. This correction is independent of the rejected batch-frontier
prompt and requires fresh real-model evidence before any latency claim.

The staged-effort feasibility screen missed a rare critical antenna-offline event
that its paired baseline found. The original deterministic overview also omitted
it because it ranked groups by frequency and selected only twelve. Under the
existing default-off overview switch, encode groups as column-labelled rows with
file indices and reuse identical first/last samples. Preserve original message
prefixes, timestamps, LF references and counts; allow every indexed group that
fits the unchanged 12,000-byte encoded budget. A thirty-case mechanical audit
found that severity-only selection evicted some formerly shown groups in nine
cases. Reserve the previous preview's visible groups and file inventory first,
then add other groups in severity, file and native-line order rather than frequency.
If compact column overhead would evict old evidence or inventory, return the
original bounded v1 representation. Severity only orders
the preview and does not establish relevance or causation. Preserve all scan
limits, per-file unindexed-record counts and explicit output omissions. References
to omitted inventory rows must never survive. Verify Unicode/escaping accounting,
rare-event coverage and exact original samples before another quality screen;
coverage of one event is not semantic-equivalence or speed acceptance.

Retained local traces contain tens of thousands of `thinking_tokens` notices
with no translated detail, answer text or token counter. The shared translator
already excludes these from workflow logs but still buffers and forwards them.
Omit only empty notices at that translation boundary, after normal usage
handling. Preserve descriptive notices and all other subtypes, actual thinking
and answer increments, tool evidence and terminal parsing. Do not filter the
SDK iterator: routing commitment and per-message cancellation remain unchanged.
Existing transport heartbeats keep idle subscriptions alive. Verify a progress
flood against the same message sequence without that noise, and replay retained
traces without modifying originals. Changes to event counts/sequence positions
are expected; answer and evidence semantics must be identical. This correction
adds no model calls and changes no prompt, but byte reduction alone does not
establish an end-to-end speedup.

Sidebar status polling previously selected every column of all historical runs
for the user's visible sessions, although it only returns small status metadata.
Defer historical trace, answer and request payloads with a raising lazy-load guard.
Keep the existing ownership filter, ordering and active-run overlay. Verify both
the returned summaries and absence of payload hydration against real SQLite.
This is a separate UI/backend resource correction, not a reduction in Agent model
calls. A local interrupted stream observed alongside elevated process memory is
not sufficient by itself to assign the interruption's cause to this query.

The prompt-only pilot preserved image/model/data identity but reproduced unsupported
exclusion and whole-window numeric claims; shortening a prompt is not acceptance.
Its first pair also retained roughly 21–30 seconds between Agent completion and
request completion, with optional title timeouts and no observed database-lock
warning. Do not attribute that entire interval to one cause without measurement.
Move optional title work after durable answer publication and terminal run
finalization in the existing background job. Keep title inputs, timeout, usage
recording and conditional update unchanged. The subscriber ends while that job may
still be generating a title; errors or cancellation in this later phase cannot
replace the completed answer with an error. Validate this boundary with blocked
title requests, real SQLite persistence and the actual SSE subscriber before a
new matched runtime experiment. This change reduces critical-path work; it does
not reduce analysis model calls or establish a measured speedup by itself.

The user-authorized switch to public DeepSeek starts a separate comparison cohort with identical effective model settings in both arms. A real SDK probe exposed an inherited `yinhe-chat` background title request returning HTTP 400 even though the runtime auxiliary model had changed. Pin both current and legacy SDK background-model environment variables per endpoint/run; never mutate process-global environment. An endpoint without a resolved auxiliary model uses its effective main model. Align the old baseline's container background-model environment with the configured model as a documented fixture correction, so that obsolete model errors do not masquerade as evidence-batching improvements. Retain the original failing probe and all previous model cohorts.

A subsequent local candidate may independently enable a deterministic startup log overview under the main efficiency gate. It scans at most 64 files, 16 MiB per file, 64 MiB total and a two-second cooperative deadline. The encoded preview is bounded to 12,000 UTF-8 bytes, with explicit omitted rows, unknown lines, scan hashes and incomplete coverage. Structured severity/source-location counts and first/last message prefixes are per-file physical observations, not diagnoses, event deduplication or proof of absence. Native tools and mandatory source investigation remain available. This experiment adds no model call and applies only to Log Analysis; it is default off and must pass new paired timing and quality checks.

The combined v9 trial completed all 20 timing pairs and 10 separate holdout pairs but failed latency and factual/uncertainty acceptance. Automatic source context and final review therefore have separate, default-off settings under the main efficiency gate. This permits testing batching and lossless representation without adding source excerpts or a mandatory model continuation. Enabling either extra mechanism still requires a complete startup snapshot and provider compatibility. A valid earlier draft is not substituted for a corrupt terminal review result in benchmark evidence.

- Changed prompting can change answers → opt-in plus paired full-answer review; fail acceptance on material discrepancy.
- Batch truncation can hide evidence → explicit totals/continuations and tests of exhaustive pagination.
- Symlinks/path traversal can escape a workspace → resolve paths, reject escapes, and test malicious paths.
- Different provider load/cache can confound timing → alternate arm order, pin model, record timing and rerun discrepancies transparently.
- Original production answers can be wrong → retain them as references, verify decisive citations against frozen logs/source; unresolved equivalence is not a pass.
- Model usage is potentially cumulative → retain raw usage and distinguish API timing from SDK/tool events.

## Migration Plan

The statistics-v1 private pair did not pass: its registered statistics tool was
never called, and a native OR search piped through `head 25` returned no mapping
rows despite 34 original matches because frequent independent topics filled the
preview. This is a verified missed-evidence path, not a complete explanation of
the answer regression. Add a separate default-off Log Analysis post-tool option
for a deliberately small literal `grep`/`head` grammar, including literal-file
loops. The pinned SDK's PostToolUse cwd was verified to follow Bash directory
changes and reset on resume. Resolve files against that actual cwd, require
contained task logs, stable full reads and exact correspondence with original
stdout, and never evaluate shell code. For literal alternatives absent from the
preview, attach physical matching-line counts and first/last context under a
shared byte/time budget. Two endpoint samples do not establish interior coverage;
include bounded adjacent rows and explicit continuations. Preserve native output,
other tools, prompts, total budgets and independent switches. Replay and wire
delivery only justify a new real-model screen, not acceptance of this mechanism.

Local-only implementation and evaluation, default disabled. Revert by disabling the flag. Production deployment requires a separate user instruction.

The fresh topic-context-v1 pair did not emit any native supplement and failed
quality review. Its observed 8.6% timing difference is not attributable to the
mechanism. Offline replay shows that accepting an absolute `cd;` prefix alone
would still add no context. Instead, an actual structured search omitted a
nonempty literal branch from its shared page. Extend the same topic hook to
MCP log requests, resolving paths at the evidence server workspace. Replay the
entire supported literal page, clipping, totals, pagination and requested context
against bounded stable files before selecting missing alternatives. Preserve
raw results and require directory identity checks for the scanned scope. Share
all budgets and deduplication across requests and native/MCP tools. Invalid UTF-8
on an unrelated line may be skipped during literal verification and escaped in
added context; a matching invalid line prevents enrichment of that request.
This corrects a limitation exposed by replaying the real log, without weakening
the byte/time limits or accepting a final-answer quality regression.
