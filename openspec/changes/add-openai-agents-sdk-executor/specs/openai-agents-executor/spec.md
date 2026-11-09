## ADDED Requirements

### Requirement: Administrator selection
The system SHALL expose OpenAI Agents SDK as `openai_agents` for global and per-agent selection with Magpie enabled, preserving model inheritance and current defaults.

#### Scenario: Switch an agent
- **WHEN** an administrator saves an OpenAI executor override
- **THEN** the next invocation uses that executor and the effective Magpie model, while other agents retain their selections

#### Scenario: Invalid gateway state
- **WHEN** an OpenAI executor is selected with Magpie disabled
- **THEN** the configuration is rejected without changing persisted settings

### Requirement: Official SDK execution with Raven contracts
The system SHALL use the official Agents SDK and preserve enabled MCP and workspace tools, permissions, skill activation, PreToolUse/PostToolUse/Stop hooks, multimodal inputs, turn limits, cancellation, incremental answer text and final usage.

#### Scenario: Tool permission denied
- **WHEN** Raven denies a function call
- **THEN** the handler is not executed and the matching tool result explains the denial

#### Scenario: Streaming tool round trip
- **WHEN** the model invokes an allowed tool and then answers
- **THEN** trace events pair the tool call and output, show incremental answer text, and finish with usage and the final answer

#### Scenario: Stop requires continuation
- **WHEN** an existing Stop hook blocks a final answer
- **THEN** the SDK continues with that feedback within the original turn budget

#### Scenario: Materialized Skill links
- **WHEN** Raven materializes an enabled Skill as a link to a shared package before execution
- **THEN** Skill and read tools can load its instructions and references, while native write tools cannot modify that shared package and later links do not grant new access

### Requirement: Magpie transport and attribution
Requests SHALL use the configured Magpie credential and model through the per-run bridge, retain gateway task routing, and label the executor and actual provider/model in live and replayed traces without leaking credentials or sending SDK traces to OpenAI.

#### Scenario: Routing group
- **WHEN** Magpie resolves a requested routing group
- **THEN** Raven records the requested group and authoritative resolved route separately

#### Scenario: Gateway failure
- **WHEN** Magpie rejects a request
- **THEN** Raven emits the existing sanitized gateway diagnostic without replaying committed tool effects
