## Why

RavenAI gates attachments on the legacy provider setting even when Magpie owns routing. A Yinhe default prevents originals from reaching Sol or DeepSeek Flash, while eager OCR cannot know the final route.

## What Changes

- Deliver originals to Magpie for native vision or independent recognition on text-only routes.
- Preserve direct-provider OCR and explicit failures for unavailable originals.
- Use the official DeepSeek Flash ID in default Raven routes.
- Verify all three routes through local Docker and browser.

## Capabilities

### New Capabilities

- `gateway-image-routing` governs attachment delivery through Magpie.

### Modified Capabilities

None.

## Impact

Image storage, OCR preprocessing, Agent input, routing defaults, regression tests and Magpie runbook. Local Docker deployment precedes the NR-TEST night window.
