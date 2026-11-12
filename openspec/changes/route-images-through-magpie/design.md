## Context

Raven uses a legacy provider flag for OCR and workspace images. Magpie already passes images to visual models and describes them for text-only candidates, including fallback candidates.

## Goals / Non-Goals

Deliver originals independently of the legacy provider, retain image history and use Magpie's existing per-candidate recognition. Direct-provider execution retains its current OCR configuration.

## Decisions

A shared image-transport predicate enables mandatory original storage and workspace materialization whenever Magpie is enabled or the direct provider is OpenAI. OCR preprocessing returns native status in that mode. All execution paths receive workspace attachments.

Magpie owns independent recognition so a route change keeps the original image available. Eager Raven OCR would add a request even for native vision. The local gateway uses its available vision model as describer because its Qwen OCR credentials are absent. Administrators can select Qwen in Magpie's Image recognition setting.

Default DeepSeek references use deepseek-flash. Existing saved routes require an explicit scoped migration and catalog refresh.

## Risks / Trade-offs

A missing original must fail explicitly to avoid answering without the user's evidence. Magpie vision settings must allow a describer for Yinhe. Model catalogs can misidentify old aliases, so verification checks actual calls and the canonical Flash name.

## Migration Plan

Build a local image using the current application source and existing installed dependencies. Update only local Raven services, back up affected gateway/runtime settings, migrate DeepSeek references, and test native and text-only paths through the browser. Restore acceptance-only model overrides. NR-TEST deployment is deferred to the night window.
