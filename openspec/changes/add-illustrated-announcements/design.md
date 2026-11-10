## Context

The service stores one versioned announcement in runtime settings. The Admin editor and the workbench already share a safe Markdown renderer, and acknowledgement is keyed by announcement ID.

## Goals / Non-Goals

**Goals**

- Keep the classic layout as the default for existing announcements and API callers.
- Give administrators a full dialog preview and keep cover assets within the application's authenticated flow.

**Non-Goals**

- Image libraries, external cover URLs, and per-user layout settings.

## Decisions

- The announcement stores its optional cover as a bounded raster data URL. Publishing replaces the image and message in the same atomic runtime settings write. The single current record makes separate upload and cleanup endpoints unnecessary.
- The Admin preview reuses the workbench dialog. The cover layout reserves a wide image area and keeps the complete image visible on narrow screens.
- Fireworks are local to the cover, opt-in per announcement, and play for about two seconds. Reduced-motion preferences suppress the effect.

## Risks / Trade-offs

- Embedding cover bytes increases response and runtime settings size. Cap the original image at 2 MiB and accept PNG, JPEG, and WebP only.
- A cover can contain information absent from the body. Provide an alternative text field and keep the title, body, and acknowledgement available when image decoding fails.

## Migration Plan

The new fields are additive. Existing records and older clients use the classic layout. Older service code can read the title and body after rollback.
