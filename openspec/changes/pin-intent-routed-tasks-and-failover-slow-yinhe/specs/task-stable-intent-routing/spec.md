## ADDED Requirements

### Requirement: Intent-selected task model
The enabled entry group SHALL classify a task's initial inference using Magpie's official intent rules. Raven SHALL pin the actual answering model for the remainder of that invocation. Explicit model overrides SHALL remain unchanged.

#### Scenario: Later tool turn
- **WHEN** a task selected a cloud model and sends tool results or a follow-up inference
- **THEN** the same explicit model receives the accumulated context without reclassification

#### Scenario: Concurrent inference
- **WHEN** two inference requests reach the same task bridge concurrently
- **THEN** their selections are serialized and the second uses the first accepted model

### Requirement: Safe local performance failover
The system SHALL detect slow local first content, low estimated output throughput and idle generation. Before any response bytes reach the executor, it SHALL cancel the local request and retry only that inference through Magpie at the configured backup. Once output commits it MUST NOT splice a replacement answer or replay that inference.

#### Scenario: Slow before output
- **WHEN** Yinhe fails its pre-output health check
- **THEN** only the backup response reaches the executor and all later inference uses the backup

#### Scenario: Slow after output
- **WHEN** Yinhe becomes slow after bytes have reached the executor
- **THEN** its response finishes as-is and the next inference uses the backup with the existing conversation and tool results

### Requirement: Overload cooldown and isolation
Slow Yinhe observations SHALL open a bounded cooldown shared across workers, with process-local fallback if Redis fails. New tasks during cooldown SHALL still receive intent routing through a cloud-only group. Tasks already switched SHALL never return to Yinhe because cooldown ended.

#### Scenario: Another user during overload
- **WHEN** a local slowdown has opened cooldown and another user starts a task
- **THEN** the new task's classification selects only cloud models

### Requirement: Observable and secure operation
The system SHALL report actual serving model changes through existing route events, preserve credentials inside Magpie, exclude auxiliary catalog/token-count calls from pinning, and release HTTP streams on cancellation.

#### Scenario: Pending switch
- **WHEN** a slow committed stream schedules a backup
- **THEN** its route still names Yinhe until a subsequent successful response identifies the backup

#### Scenario: JSON-encoded local tool arguments
- **WHEN** the local model supplies MCP arguments as a JSON object string
- **THEN** the Antigravity bridge parses them before the existing permission checks, preserves the visible tool ID, and rejects non-object or malformed values without tool dispatch
