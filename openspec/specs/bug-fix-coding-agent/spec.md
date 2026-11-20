# bug-fix-coding-agent Specification

## Purpose

Provide a Claude Agent SDK-based coding agent that, given a bug fix task, clones the target project repository into an isolated workspace, applies code fixes with write-capable tools, and produces merge requests to resolve diagnosed defects.

## Requirements

### Requirement: Bug Fix Coding Agent runs on Claude Agent SDK in an isolated workspace with write tools

The system SHALL provide a `BugFixCodingAgent` built on the Claude Agent SDK that, given a Bug 修复任务（`bug_fix_task`）, clones the task's project repository into an isolated temporary workspace and operates with write-capable tools. The agent's `allowed_tools` SHALL include at minimum `Read`, `Grep`, `Glob`, `Edit`, `Write`, and `Bash`, and SHALL run with `permission_mode="bypassPermissions"` and `cwd` set to the workspace. The clone URL SHALL be resolved via the same token-injection helper used by log analysis, reusing the per-repo `git_token` or the global `code_repo_git_token`.

#### Scenario: Agent receives a task and clones the repo

- **WHEN** `run_bug_fix_task` starts for a `bug_fix_task` whose `project_repo` resolves to a clone URL
- **THEN** an isolated workspace is prepared and the repository is cloned into it before the agent loop begins
- **AND** the workspace is cleaned up after the run regardless of success or failure

#### Scenario: Agent has write tools but cannot escape the workspace

- **WHEN** the agent loop builds its options
- **THEN** `allowed_tools` contains `Edit`, `Write`, and `Bash` in addition to the read-only investigation tools
- **AND** `cwd` is the task workspace so all file edits and git commands operate on the cloned repo only

### Requirement: Agent applies the minimal-change principle

The agent's system prompt SHALL instruct it to apply the **minimal-change principle**: modify only the code directly related to the diagnosed root cause, avoid unrelated refactoring or reformatting, avoid introducing unrelated dependencies, and keep each change small and focused. The agent SHALL NOT commit directly to the repository's default branch.

#### Scenario: Fix touches only root-cause code

- **WHEN** the agent fixes a problem described in one `proposed_fixes` item
- **THEN** the resulting branch's changes are scoped to the files needed for that fix
- **AND** no commit is made onto the repository's default branch

### Requirement: Agent produces one or more independent Merge Requests per task

The agent SHALL create a separate branch (cut from the default branch), an independent commit, a push, and a separate Merge Request for **each mutually independent** problem listed in `proposed_fixes`. Multiple changes SHALL be combined into a single Merge Request ONLY when they share one root cause and must be merged together. Branch names SHALL use a consistent prefix that encodes the task and an index (for example `bugfix/ai-<task_id>-<index>`). Commit messages SHALL reference the source log id and the fix item title.

#### Scenario: Automated commits identify the model

- **WHEN** the agent commits a fix from its workspace
- **THEN** both the Git author and committer use the active model name and `bot@ravenai.local`, overriding any identity inherited from the worker
- **AND** with Magpie routing, subsequent commits use the model reported by the latest inference route, with `RavenAI Bug Fix Agent` as the fallback when only a routing group is known

#### Scenario: Multiple independent problems yield multiple MRs

- **WHEN** a task's `proposed_fixes` contains two independent problems
- **THEN** the agent creates two branches, two commits, two pushes, and two Merge Requests
- **AND** each Merge Request targets the default branch as its base

#### Scenario: Tightly coupled changes share one MR

- **WHEN** two edits belong to the same root cause and only make sense merged together
- **THEN** the agent places both edits on a single branch and opens a single Merge Request

### Requirement: Merge Requests are created via the Git hosting platform

After pushing a branch, the agent SHALL create a Merge Request (GitLab) — or Pull Request for platforms that use that term — targeting the default branch, authenticating with the per-repo or global git token. For providers where an in-process MR tool is unavailable, the agent MAY fall back to a CLI (`glab`/`gh`) or direct REST call. The created Merge Request SHALL be left open for human review; the agent SHALL NOT merge it.

#### Scenario: MR is opened against the default branch

- **WHEN** the agent has pushed a fix branch for a registered GitLab project
- **THEN** a Merge Request is created with the fix branch as source and the project's default branch as target
- **AND** the Merge Request is left in an open (un-merged) state

#### Scenario: MR creation does not auto-merge

- **WHEN** a Merge Request is successfully created
- **THEN** the agent does not perform a merge and leaves the decision to human reviewers

### Requirement: Agent emits structured results for each Merge Request

The agent's final output SHALL be a fenced JSON object containing a `merge_requests` array; each element SHALL carry at minimum `title`, `branch_name`, `base_branch`, `mr_url`, `mr_iid`, `commit_sha`, `changed_files` (list of file paths with added/removed line counts), and a `diff_stat` summary. When the agent could not produce any Merge Request, it SHALL emit an empty `merge_requests` array together with an `error_kind`.

#### Scenario: Structured MR result is returned

- **WHEN** the agent finishes a run that produced one Merge Request
- **THEN** the output JSON `merge_requests` array has one element with `branch_name`, `mr_url`, `commit_sha`, and `changed_files`
- **AND** these fields are persisted onto `bug_fix_merge_request` rows

### Requirement: Tokens are redacted from Bug Fix Agent traces, logs, and results

The system SHALL apply the same token redaction (`https://[^@]+@` → `https://***@`) used by log analysis to all Bug Fix Agent `tool_trace`/trace output, structured logs, persisted task/MR records, and API responses. No plaintext git token or MR API token SHALL ever be persisted or returned.

#### Scenario: Clone and MR tokens are masked

- **WHEN** the agent runs `git clone https://oauth2:secret@host/foo.git` and calls the MR API with a token
- **THEN** any persisted trace or log shows `https://***@host/foo.git`
- **AND** the stored `mr_url` is a clean, clickable URL containing no credentials
