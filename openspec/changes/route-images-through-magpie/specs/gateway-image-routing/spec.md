## ADDED Requirements

### Requirement: Gateway receives original attachments
When Magpie is enabled Raven SHALL deliver original image bytes independent of the legacy provider and SHALL skip eager Raven OCR.

#### Scenario: Native model with a legacy Yinhe default
- **WHEN** the user uploads an image and Magpie selects Sol or deepseek-flash
- **THEN** the selected model receives the image and no independent recognition call is made

#### Scenario: Missing original
- **WHEN** an authorized attachment cannot be stored or restored
- **THEN** the request fails explicitly instead of silently losing the image

### Requirement: Text-only routes receive image descriptions
Magpie SHALL use its configured or automatic vision model to describe images for text-only models.

#### Scenario: Yinhe reads an attachment
- **WHEN** the selected route is Yinhe and a describer is available
- **THEN** a separate recognition call supplies image content to Yinhe and the final answer reflects that content

### Requirement: End-to-end local acceptance
Acceptance SHALL exercise actual inference through the browser and local Docker stack.

#### Scenario: Three image routes
- **WHEN** Sol, deepseek-flash and Yinhe are each tested with image-only facts
- **THEN** the browser shows correct answers and recorded routes establish native input for Sol and Flash and independent recognition for Yinhe
