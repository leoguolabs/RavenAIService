## ADDED Requirements

### Requirement: Administrator selects an execution backend
Raven SHALL support `claude_sdk` and `antigravity_cli` behind one invocation boundary for its agents. Only administrators SHALL change defaults or per-agent overrides. A run MUST retain the executor selected at its start.

#### Scenario: Change executor between runs
- **WHEN** an administrator selects Antigravity after a Claude run starts
- **THEN** that run continues on Claude and new runs use Antigravity without an end-user workflow change

### Requirement: Equivalent workspace and tool integration
Both executors MUST receive the task workspace, prompts, materialized skills and appropriate MCP tools. Existing permissions, clarification, cancellation and result persistence MUST continue to work. Skills MUST be shown as loaded only when actually activated.

#### Scenario: Analyze the same evidence with either executor
- **WHEN** each executor analyzes the same log and uses a skill and tools
- **THEN** the common UI displays actual skill activation and paired tool steps and retains the answer after reload

#### Scenario: Cancel a CLI task
- **WHEN** a user cancels an Antigravity run
- **THEN** its process and pending tool calls stop, the normal cancelled terminal event is persisted, and another user's run is unaffected

### Requirement: Versioned backend installation
The backend image MUST contain a verified, pinned Antigravity CLI supporting machine-readable events on Linux ARM64 and AMD64.

#### Scenario: Build the backend image
- **WHEN** the image is built
- **THEN** the CLI checksum and version are checked and no desktop login configuration is modified
