## ADDED Requirements

### Requirement: Native OpenAI runtime preserves agent tool and stream behavior

The system SHALL support the OpenAI Responses API for every agent through the shared query entry point. It MUST retain existing SDK tools, MCP, tool permissions, streaming trace, usage, cancellation and endpoint isolation. The per-run protocol bridge MUST bind only to loopback, authenticate local requests and close after completion or cancellation. Upstream errors MUST remain failures without leaking credentials or replaying committed tools.

#### Scenario: Tool round trip with incremental answer
- **WHEN** an OpenAI agent requests a permitted tool and then answers
- **THEN** the tool executes through existing permission controls and its output returns to the same OpenAI model
- **AND** text deltas, tool trace, final answer and token usage reach the existing chat client

#### Scenario: Cancel or upstream failure
- **WHEN** a run is canceled or the upstream rejects/truncates a response
- **THEN** resources are closed and the run records the correct terminal status without credential leakage or post-commit retries

### Requirement: Original images and text use one model

When the active provider is OpenAI, the system SHALL skip independent OCR and send validated original image bytes together with the current question to the selected main model. This MUST work for general, device, project-expert, log-analysis and configuration-manager chat paths, and for image tool results. Missing images MUST yield an explicit failure, never a fabricated image description. Other providers retain existing OCR behavior.

#### Scenario: Screenshot without OCR credentials
- **WHEN** an OpenAI chat includes a screenshot and no OCR key is configured
- **THEN** the selected model receives an image content block and answers from visible evidence without any separate OCR request

#### Scenario: No images
- **WHEN** an OpenAI chat has only text
- **THEN** normal text, tools and multi-turn conversation remain usable with the same model

### Requirement: Local Docker acceptance uses real API credentials without payment changes

The implementation SHALL be deployed in a local Docker stack and verified end to end using a real authorized project API Key. Verification MUST cover authenticated browser chat, text, screenshot recognition, history/multi-turn, tools, cancellation, telemetry and model configuration. Credentials MUST remain outside commits and reports; no payment method, purchased credits or auto recharge SHALL be added.

#### Scenario: Acceptance succeeds
- **WHEN** the local test stack completes real text/image/tool requests
- **THEN** UI and persisted run evidence show OpenAI and the selected model, accurate final output and usage, and no independent OCR or paid billing configuration
