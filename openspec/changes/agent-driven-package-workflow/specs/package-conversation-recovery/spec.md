## ADDED Requirements

### Requirement: Restore every packaging run state
The system SHALL persist failed and cancelled packaging turns and restore running, failed and completed conversations independently of the initiating connection. A stale UI request MUST NOT restore the wrong session selection.

#### Scenario: Reopen failed packaging
- **WHEN** packaging fails and the user reloads or opens another browser page
- **THEN** the conversation loads with the original request and actionable failure answer

#### Scenario: Reconnect during confirmation
- **WHEN** the user leaves and reopens a conversation awaiting package review
- **THEN** the active run and pending confirmation are restored without starting a duplicate job

#### Scenario: Several clients follow a long run
- **WHEN** initial packaging or replay streams remain open
- **THEN** their request database connections are released before subscribing so other clients can load history

#### Scenario: Workspace persistence fails
- **WHEN** a package run cannot save its workspace metadata
- **THEN** the run still enters a terminal state and closes its live publication capability
