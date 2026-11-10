## ADDED Requirements

### Requirement: Keep repository credentials server-side
The Configuration Manager SHALL expose only project discovery and server-side cloning. New and reused checkouts MUST NOT retain HTTP/HTTPS credentials in Git remote URLs, and traces SHALL redact credential-bearing URLs and GitLab PATs.

#### Scenario: Agent inspects Git remotes
- **WHEN** an Agent runs `git remote -v` after cloning or reusing a project
- **THEN** it sees credential-free repository addresses

### Requirement: Agent owns package interpretation and execution
The system SHALL invoke the configured Agent for packaging, enable workspace code tools, and let it determine types, IDs, versions and layout using uploaded data, project source and existing package evidence. Static business catalogs MUST NOT gate this workflow.
The shared instructions and materialized Skill SHALL omit fixed project identities, component sets, business IDs and legacy catalogs. The publication tool SHALL continue validating the supported TGZ and `si.ini` format.

#### Scenario: Timestamp component version
- **WHEN** an input has version `1.2.30.12.202609170917`
- **THEN** the Agent separates component and requested release versions using evidence without catalog rejection

#### Scenario: Uploaded components cover only part of the release target
- **WHEN** the uploads supply a supported subset of the selected project's full release
- **THEN** the Agent selects patch mode using the attribute defined for that target before resolving the release version
- **AND** if Packet_Ver is still needed, it asks for the patch package version without proposing example values

### Requirement: Validate and confirm publication
The system SHALL validate artifact structure, hashes, input accounting and project scope, retain evidence in the audit plan, and publish only the confirmed artifact. The publication summary SHALL contain the project, release version and type, component names and versions, and any release warnings or excluded uploads. It MUST reject unsafe archives, changed bytes, cancellation and duplicate publication.

#### Scenario: Routine publication
- **WHEN** the validated package has no outstanding warning that needs human judgment
- **THEN** the conversation shows an inline summary with direct publication and cancellation actions and no source evidence

#### Scenario: An outstanding decision affects release
- **WHEN** the Agent has built a valid package but a remaining risk or scope change requires a human decision
- **THEN** a publication dialog shows the same compact summary and the release impact

#### Scenario: Agent builds a new component type
- **WHEN** the Agent supplies evidence for a component absent from the static catalog
- **THEN** generic validation and explicit confirmation allow publication without a catalog edit

#### Scenario: Artifact changes after review
- **WHEN** bytes change while confirmation is pending
- **THEN** publication fails and no package is registered
