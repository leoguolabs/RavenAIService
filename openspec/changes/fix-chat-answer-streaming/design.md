## Context

The backend already emits partial text. Specialist transports wrap it as `event: agent_trace, type: answer_delta`. The web store currently handles the wrapper before the delta, losing the answer projection. Specialist model text can be a fenced JSON result whose `answer` string is still open.

## Goals / Non-Goals

**Goals:** Show readable answer text before the terminal event, pace coarse chunks and final-only fallback, preserve final persisted text, and restore active runs without duplicate deltas.

**Non-Goals:** Change model prompts, backend execution, persisted result formats, or the Electron client.

## Decisions

- Normalize only answer deltas out of the trace envelope before dispatch. Keep them out of tool/thinking cards and use existing sequence deduplication. Changing backend transports would require coordinated client upgrades.
- Retain raw incremental text separately from visible message content. A small partial JSON reader decodes the top-level `answer` string as it arrives; ordinary Markdown passes through. Incomplete metadata stays behind the thinking indicator. Completion remains authoritative.
- Isolate presentation pacing in a Vue composable/component. Store content stays current for persistence/export; rendered text advances on a short timer with backlog catch-up. Corrections replace any differing prefix immediately, preventing stale text from lingering. Completed history and restored snapshots render immediately. Timers dispose on unmount.
- Re-render diagrams after the visible Markdown changes and keep existing scroll behavior. Respect reduced-motion preferences.

## Risks / Trade-offs

- [Partial JSON escapes span chunks] → decode only complete escapes, with regression coverage for Unicode, quotes, newlines and nested fields.
- [Provider emits only a terminal answer] → animate the newly received answer in the mounted live placeholder; no animation for loaded history.
- [Terminal answer changes streamed text] → immediately correct the differing prefix; only pace additional suffix text.
- [Long replies lag behind the server] → adaptive catch-up bounds presentation delay; terminal controls remain hidden until display catches up.

## Migration Plan

Frontend-only build, with existing API compatibility. Revert the commit to roll back. Validate locally against live backend SSE and controlled transport fixtures in Ego Lite; no production deployment is included.
