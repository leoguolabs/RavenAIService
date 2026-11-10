## ADDED Requirements

### Requirement: Selectable announcement presentation
A global administrator SHALL be able to choose a classic text layout or a cover image layout and preview the resulting announcement dialog before publication. Both layouts SHALL preserve safe Markdown rendering and the existing per-user acknowledgement behavior.

#### Scenario: Read an existing announcement
- **WHEN** a stored announcement or publish request has no layout field
- **THEN** the system uses the classic text layout

#### Scenario: Preview a cover announcement
- **WHEN** a global administrator selects the cover layout, uploads a supported image, and opens the dialog preview
- **THEN** the preview shows the cover above the title and body using the same layout as the workbench
- **AND** closing the preview does not publish or acknowledge an announcement

#### Scenario: Publish a cover announcement
- **WHEN** a global administrator publishes a cover announcement with one PNG, JPEG, or WebP image up to 2 MiB
- **THEN** the system stores the cover and its alternative text with the announcement and delivers them to eligible authenticated users

#### Scenario: Reject an invalid cover
- **WHEN** a cover publication has no cover or supplies an unsupported image, invalid encoding, or an image over the size limit
- **THEN** the service rejects the publication and keeps the existing announcement

#### Scenario: Switch to classic text
- **WHEN** a global administrator publishes in the classic layout
- **THEN** the system displays the classic dialog without cover artwork or fireworks

### Requirement: Optional cover fireworks
The cover layout SHALL offer a fireworks effect that is disabled by default and plays once on opening for about two seconds when enabled. It SHALL respect reduced-motion preferences and allow immediate acknowledgement.

#### Scenario: Show fireworks when enabled
- **WHEN** a cover announcement opens with fireworks enabled and reduced motion is off
- **THEN** fireworks play briefly in the cover and stop without user interaction

#### Scenario: Honor reduced motion
- **WHEN** a user requests reduced motion
- **THEN** the cover remains visible without fireworks
