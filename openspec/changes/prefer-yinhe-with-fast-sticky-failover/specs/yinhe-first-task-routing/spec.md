## ADDED Requirements

### Requirement: Local preference without intent classification
Every default invocation outside shared cooldown SHALL first request the Yinhe-only group. Managed legacy groups SHALL have no classifier or intent rules; cloud fallback SHALL use a fixed order.

#### Scenario: Task complexity has no effect
- **WHEN** a new simple or complex task starts outside cooldown
- **THEN** Yinhe is attempted directly with no classifier model call

### Requirement: One deadline includes headers and meaningful output
The local first-output budget SHALL be 0.5 seconds from request dispatch. HTTP header delay and heartbeat frames SHALL consume the same budget. Timeout SHALL cancel the uncommitted attempt and preserve its full request context for fallback.

#### Scenario: Early headers followed by silence
- **WHEN** local headers arrive before the deadline but meaningful output does not
- **THEN** the local attempt is closed and cloud fallback begins without another first-token allowance

### Requirement: Slow fallback remains pinned
The bridge SHALL observe local generation for 0.5 seconds, require 20 estimated tokens/s and detect 2 seconds without output. A slow local model SHALL open a 120-second shared cooldown. An invocation that switches SHALL never return to Yinhe, even after cooldown expires.

#### Scenario: Slowness after delivery
- **WHEN** local output has already been delivered before slowdown is detected
- **THEN** its current stream finishes without replay and the next inference uses cloud

#### Scenario: New task during cooldown
- **WHEN** a new invocation starts while local cooldown is active
- **THEN** it starts with the ordered cloud group and pins the actual responder

### Requirement: Existing safety boundaries persist
The change SHALL preserve full tool history, billing protection, executor selections and task cancellation behavior.

#### Scenario: User cancellation while waiting for local headers
- **WHEN** the task is cancelled before local headers arrive
- **THEN** its pending request is cancelled and no fallback request is started
