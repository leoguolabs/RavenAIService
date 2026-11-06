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
