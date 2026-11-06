## ADDED Requirements

### Requirement: Actual execution and model attribution
The shared trace protocol SHALL carry the executor identity, requested route and actual provider/model observed from Magpie. Route changes MUST remain visible in live streams and persisted replay. A requested routing group MUST NOT be presented as a confirmed model.

#### Scenario: Model selected by a routing group
- **WHEN** Magpie returns an actual model/provider in its response headers
- **THEN** Raven emits route attribution and the frontend shows that model and executor without requiring the user to configure them

#### Scenario: Route changes during a run
- **WHEN** a later model turn is served by another member
- **THEN** the UI and persisted trace retain the change and the final metadata describes the observed route rather than the initial group
