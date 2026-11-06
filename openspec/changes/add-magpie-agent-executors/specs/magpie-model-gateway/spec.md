## ADDED Requirements

### Requirement: Magpie owns upstream routing
When gateway mode is enabled, all Agent inference SHALL go through Magpie. Raven MUST NOT independently retry the entire Agent against another upstream. Administrators SHALL select Magpie catalog models or routing groups independently from the executor.

#### Scenario: Group fallback
- **WHEN** the first member of a Magpie routing group fails before responding
- **THEN** Magpie selects its fallback and Raven continues the same execution without replaying completed tools

### Requirement: Reuse configured credentials
The integration SHALL import the existing Yinhe, DeepSeek and OpenAI upstream configuration into Magpie without asking for keys again. Credentials MUST NOT appear in UI responses, committed files or test evidence.

#### Scenario: Bootstrap the gateway
- **WHEN** an administrator initializes gateway routing from existing settings
- **THEN** configured upstreams are usable through Magpie and original configuration remains available for rollback

### Requirement: Full live compatibility matrix
Acceptance MUST exercise both executors against each of the three configured providers using actual inference and tools, plus browser-visible skills, tools and route identity. Mocked provider tests alone MUST NOT satisfy acceptance.

#### Scenario: Six executor-provider combinations
- **WHEN** the acceptance matrix completes
- **THEN** each cell has a successful real task, matched Magpie route evidence and persisted frontend trace evidence

#### Scenario: Disabled explicitly selected provider
- **WHEN** a selected model's provider is disabled in Magpie
- **THEN** both executors report the HTTP failure, requested model and disabled-provider cause without exposing credentials or raw upstream bodies
- **AND** Raven does not silently switch an explicit model to another provider or replay the Agent

#### Scenario: Agent and executor are independent of model provider
- **WHEN** each of the six Agent kinds selects each configured model with either executor
- **THEN** enabled models are callable with the selected executor and the actual route is recorded
- **AND** disabled models fail explicitly, while unrelated enabled models remain callable with OpenAI disabled
- **AND** acceptance-only model overrides are removed after testing so executor-only overrides inherit the administrator's default model
