## ADDED Requirements

### Requirement: Pause interactive Agent execution for clarification

The system SHALL suspend an interactive run after an ordinary clarification request and keep the question answerable without a response deadline. An answer SHALL resume execution using the original turn context.

#### Scenario: User returns after the previous deadline

- **WHEN** the user reconnects to the conversation after the previous five-minute deadline
- **THEN** the pending question is restored and a submitted answer resumes the same turn once

#### Scenario: User cancels while waiting

- **WHEN** the owner cancels the waiting run
- **THEN** the system clears the question and terminates the run without resuming execution
