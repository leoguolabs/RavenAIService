## 1. Runtime and configuration

- [x] 1.1 Add OpenAI protocol metadata, provider presets and native connectivity checks with safe secret handling.
- [x] 1.2 Implement isolated Responses protocol bridge, text/image/tool conversion, live SSE, error handling and deterministic cleanup; wire every SDK query entry point.

## 2. Unified multimodal input

- [x] 2.1 Bypass separate OCR in OpenAI mode and attach validated original images directly for general/device and workspace-based chats.
- [x] 2.2 Preserve history, image failures, non-OpenAI behavior and same-model auxiliary calls; update bilingual model UI and operating documentation.

## 3. Verification and delivery

- [x] 3.1 Add and pass focused protocol, routing, multimodal, settings and cancellation regression tests; frontend checks and OpenSpec validation.
- [x] 3.2 Create a dedicated API Key securely and deploy an isolated local Docker stack with OpenAI configured and paid fallback disabled.
- [x] 3.3 Complete real authenticated browser/API end-to-end text, image, tool, multi-turn, cancellation, history and usage checks; confirm no payment changes.
- [x] 3.4 Review diffs, preserve pre-existing changes, commit only this task on main, update umbrella gitlink and report verified evidence.
