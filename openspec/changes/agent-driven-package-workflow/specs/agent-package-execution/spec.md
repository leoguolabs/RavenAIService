## ADDED Requirements

### Requirement: Keep repository credentials server-side
The Configuration Manager SHALL expose only project discovery and server-side cloning. New and reused checkouts MUST NOT retain HTTP/HTTPS credentials in Git remote URLs, and traces SHALL redact credential-bearing URLs and GitLab PATs.

#### Scenario: Agent inspects Git remotes
- **WHEN** an Agent runs `git remote -v` after cloning or reusing a project
- **THEN** it sees credential-free repository addresses

### Requirement: Agent owns package interpretation and execution
The system SHALL invoke the configured Agent for packaging, enable workspace code tools, and let it determine types, IDs, versions and layout using uploaded data, project source and existing package evidence. Static business catalogs MUST NOT gate this workflow.

#### Scenario: Timestamp component version
- **WHEN** an input has version `1.2.30.12.202609170917`
- **THEN** the Agent separates component and requested release versions using evidence without catalog rejection

### Requirement: Validate and confirm publication
The system SHALL validate artifact structure, hashes, input accounting and project scope, display the Agent's complete review plan, and publish only the confirmed artifact. It MUST reject unsafe archives, changed bytes, cancellation and duplicate publication.

#### Scenario: Agent builds a new component type
- **WHEN** the Agent supplies evidence for a component absent from the static catalog
- **THEN** generic validation and explicit confirmation allow publication without a catalog edit

#### Scenario: Artifact changes after review
- **WHEN** bytes change while confirmation is pending
- **THEN** publication fails and no package is registered
