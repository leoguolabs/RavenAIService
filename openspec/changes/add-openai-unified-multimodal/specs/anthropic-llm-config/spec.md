## ADDED Requirements

### Requirement: OpenAI is selectable with protocol-aware configuration and connectivity

The model configuration SHALL offer an OpenAI provider using the official API base URL and an available vision-capable model preset. The existing configuration keys and non-OpenAI providers MUST remain compatible. Provider metadata MUST distinguish Responses from Messages. Main, small/fast and screenshot reasoning SHALL use the same selected model in OpenAI mode. Admin connectivity MUST test the native Responses endpoint and mask secrets.

#### Scenario: Configure and test OpenAI
- **WHEN** an administrator selects OpenAI and saves its key and model
- **THEN** the next run and connectivity probe use the native OpenAI endpoint, and API responses expose no full key

#### Scenario: Keep existing providers
- **WHEN** the selected provider uses Anthropic Messages
- **THEN** its previous SDK and independent OCR paths remain unchanged
