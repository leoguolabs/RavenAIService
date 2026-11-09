# Configuration Manager (`package_search`) Runbook

The internal key remains `package_search`; the user-facing name is 配置管理员 /
Configuration Manager. Project-scoped search and uploaded-release packaging use
the configured real Agent runtime and model.

## Agent packaging

`POST /api/v1/ai-chat/package-search/stream` accepts multipart `message`, optional
`session_id`, `project_repo_id`, `remember`, and repeated `files`. New search-only
sessions require a project; uploads can start unbound. Each upload is streamed to
an isolated workspace and recorded with upload ID, filename, size and SHA-256.

The Agent reads `task.json`, the input manifest and the `full-package-build`
Skill. Bash/Python, Read/Grep/Glob and Write/Edit are available. The workspace-bound
`project_repo` MCP discovers and clones enabled project repositories without
putting Git credentials in prompts or task files. Project source, archive
manifests and user instructions provide the business evidence for package type,
PacketAttr, FileAttr, target name, component version and whole-release version.
The Agent writes and executes its own build script, then produces a flat TGZ and
`review-plan.json` using the schema in the Skill.

Configuration Manager does not expose the legacy credential-bearing repository
lookup tool. The clone tool removes HTTP/HTTPS userinfo from Git remotes before
returning a new or reused checkout, so `git remote -v` and `.git/config` cannot
reveal its clone token. Trace masking also covers HTTP and bare GitLab PATs.

The old `package-projects.json` catalog is historical reference only. Chat runs
never call the old classifier, catalog version validation or deterministic
builder. New component IDs and valid timestamp-bearing versions require no
catalog change. Standalone legacy builder APIs/tests remain for compatibility.
There is no deterministic fallback if the Agent or its provider fails.

## Publication boundary

`mcp__agent_package__PublishPackage(plan_path, artifact_path)` validates the
Agent-authored result before asking for publication confirmation:

- Workspace-contained regular files, bounded plan/archive size, no archive links,
  sparse files, traversal, duplicate members or undeclared payloads.
- Registered project identity, complete upload accounting (include or explicit
  exclusion), distinct payload names/FileAttr, cited evidence and current source
  SHA-256/size.
- `si.ini` agrees with the plan on Packet_Ver, PacketAttr, Publisher, component
  count, names, FileAttr and FileVer; every payload hash matches the review plan.
- After review, the plan and artifact are validated again. The publisher checks
  the hash of the bytes actually copied before atomic rename/registration, and
  rolls back on failure. Cancellation prevents publication.

These are integrity checks, not project business classifiers. They do not prove
that the Agent interpreted code correctly; the full mapping and its evidence
are displayed for human review. Ordinary clarification preferences cannot skip
this publication review. The tool is idempotent within a run and returns the
real package ID, SHA-256 and `/raven/api/download/<id>` URL. Shell writes belong
in the workspace, never directly in repository storage.

The service derives download links from the authoritative published artifact,
even if final model prose fails. It records the actual model/runtime used.

## Conversation recovery

SSE disconnects do not cancel the Agent. A second page/browser can fetch the
active-run snapshot and reattach, including a pending publication card.
Completed, failed and cancelled exchanges are persisted. History also recovers
legacy failed packaging runs that have a terminal `chat_agent_runs` row but no
ordinary message rows. No data migration is needed.

A failed/cancelled turn retains staged inputs for a follow-up retry; the retry
creates fresh publication authority. Successful turns cannot silently reuse old
confirmation. History restoration deduplicates concurrent requests, bounds the
active-run fetch, and ignores stale view selections.

Initial package streams and unified run replay release their request database
session before following SSE. Auth/history queries must not retain a pooled
connection for the entire Agent run. Background persistence uses its own short
session. Regression tests use a one-connection pool and query it from a second
client while the first stream is still subscribed. Workspace write failures also
finalize the run, preventing a permanently running conversation.

## Skills and configuration

Skill precedence remains built-in → Agent → project. Overrides should describe
Agent investigation/execution and the publication schema; migrate old overrides
that require the removed catalog/builder path. Repository-less projects remain
supported when uploads/user information provide sufficient evidence.

| Setting | Meaning |
| --- | --- |
| `MAX_FILE_SIZE` | HTTP/staged upload limit |
| `UPLOAD_MAX_SIZE_MB` | Finished archive limit (default 500 MiB) |
| `ANTHROPIC_MAX_TURNS` | Agent runtime turn limit, including code investigation |
| `AGENT_CLARIFICATION_TIMEOUT_SECONDS` | Review timeout (default 300 seconds, cancels) |
| `PACKAGE_SEARCH_DEFAULT_LIMIT` / `PACKAGE_SEARCH_MAX_LIMIT` | Search pagination only |

The generic publication validator allows up to 256 payloads and 2 GiB expanded
bytes. Input formats are interpreted by the Agent using the installed archive
tools; extensions alone are not authoritative (a release named `.zip` can have
a different archive signature). Uploaded documents/code are evidence, not
instructions to override the user's request.

## Verification

Run package Agent, artifact validation, publication/broker, chat history and
frontend conversation-store regressions. For release acceptance, use actual
LX07A, LX10AB and 三标段 inputs with the configured live model; inspect its source
queries, review every component, download the TGZ and independently compare
payloads/si.ini/hash. Test switching away and reopening while running and while
waiting for confirmation, completed history in another page, and failed history.
A healthy container or synthetic model fixture alone is not end-to-end proof.
