## Context

The current chat packaging branch never invokes the Agent. It classifies against a static catalog, asks a generated form, and invokes a deterministic builder. Version inference conflates the release version with component build timestamps. Failure paths do not persist ordinary conversation turns.

## Goals / Non-Goals

**Goals:** Real Agent investigation and execution, evidence visible to the reviewer, safe validated publication, reliable conversation recovery.

**Non-Goals:** Production deployment, changing existing stored packages or removing legacy standalone builder APIs.

## Decisions

1. The Agent uses Bash/Read/Grep/Glob/Write/Edit to inspect uploaded archives and project source, and creates both an artifact and a declarative review plan. A new publication MCP receives their workspace-relative paths. There is no catalog lookup, filename classifier or package-version regular expression in this path.
2. The server validates only generic integrity: workspace containment, bounded regular-file archives, manifest membership, matching si.ini fields/payload hashes, unique component targets, known project identity and complete input accounting. The Agent supplies business IDs, versions, package type and cited evidence. Evidence from files is data, never instructions.
3. The publication MCP submits a complete human review card through the shared broker, then atomically publishes exactly the validated hash. Cancellation, changed bytes and invalid archives prevent publication. Calls within a run are idempotent. No server fallback fabricates Agent success.
4. Failed/cancelled runs persist both the user input and terminal answer with trace; restoration does not depend on a live in-memory packaging job. Frontend requests are bounded and stale view loads cannot overwrite the selected conversation.

## Risks / Trade-offs

- Model interpretation can be wrong → require cited code/manifest evidence and review the complete plan before publication; missing facts trigger clarification.
- Shell tools can write workspace files → enable them as explicitly requested, keep repository publication inside the validated tool and never embed credentials in task files.
- Large uploads/archives → bounded inspection and streaming hashes off the async event loop.
- Provider failure → persist a recoverable failed run; do not fall back to the removed rule engine.

## Migration Plan

Deploy backend and frontend together after local Docker tests. Existing catalog APIs remain available for historical/standalone callers; the chat path always uses the Agent. Rollback uses the prior image without data migration.
