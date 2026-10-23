## Context

Baseline source is the current local commit, recorded separately from production revision. The production log list supplies original archive bytes, questions, prior answers and attachment groups. Existing Docker volumes are preserved; validation gets isolated data and runtime settings. Production is only read/downloaded.

## Goals / Non-Goals

Goals: reduce model round trips with unchanged evidence access, output schema, conclusions and uncertainty; measure at least 20 paired rounds and at least 10 distinct subsequent log analyses. A 30% median reduction is an experimental target, not an assumed result.

Non-goals: production deployment, changing model to claim an optimization, reducing the 150-turn limit, suppressing required skills, dropping attachments, hiding failures, or treating tool count as API request count.

## Decisions

1. Keep the existing SDK loop and opt in with `agent_evidence_efficiency_enabled`. Supply a sanitized task/project snapshot only when preparation succeeds; preserve the legacy flow when disabled or incomplete. Include the complete discovery catalog to retain project-fit semantics. Do not preload full logs, source or past answers.
2. Add workspace-bound `search_evidence` and `read_evidence` MCP tools. Batch independent reads with bounded concurrency. Return exact file/line references, pagination/truncation metadata and continuation arguments. Never present a partial search as exhaustive. Preserve native tools for complex operations.
3. Provide concise guidance to group independent searches and explain the remaining evidence gap before expanding investigation. Also enrich successful native source searches through PostToolUse with at most two verified source windows (160 lines / 8000 characters each), retaining exact references and continuation. The server performs the mechanical search-location-to-source-read step without another model round trip. Limit reads to repository source files within the workspace, validate the matched text, skip ambiguous/unsupported outputs, and deduplicate only added context per run. Original results, tools, retry capability and turn limits remain unchanged. No additional planner/judge model call or forced early answer is inserted. Reminder-only periodic checkpoints were rejected after a real trial exhausted 150 turns; no periodic reminder is retained.
4. Use the same pinned Docker dependencies, model endpoint and parameters for both benchmark arms. Each run uses an isolated fresh workspace with identical archive bytes and repository commit. Record cache/setup conditions; alternate AB/BA order. Record unsuccessful runs and retries without silently replacing them.
5. Persist the corpus manifest, per-run state, result and trace outside Git. The runner resumes only after inspecting the actual running process or completed output, never because a timeout in observation expired. At least 20 timing pairs and 10 distinct holdout logs are required; holdout quality has its own baseline answers.
6. Compare factual conclusions, evidence references, attachment coverage, counterevidence and calibrated uncertainty for every case. Byte-identical output is measured and reported separately: stochastic generation cannot be guaranteed identical by an execution change. Semantic quality gates do not authorize claiming byte identity.

## Risks / Trade-offs

- Changed prompting can change answers → opt-in plus paired full-answer review; fail acceptance on material discrepancy.
- Batch truncation can hide evidence → explicit totals/continuations and tests of exhaustive pagination.
- Symlinks/path traversal can escape a workspace → resolve paths, reject escapes, and test malicious paths.
- Different provider load/cache can confound timing → alternate arm order, pin model, record timing and rerun discrepancies transparently.
- Original production answers can be wrong → retain them as references, verify decisive citations against frozen logs/source; unresolved equivalence is not a pass.
- Model usage is potentially cumulative → retain raw usage and distinguish API timing from SDK/tool events.

## Migration Plan

Local-only implementation and evaluation, default disabled. Revert by disabling the flag. Production deployment requires a separate user instruction.
