## 1. Image delivery

- [x] 1.1 Enable original storage and Agent input for Magpie independently of the legacy provider, and bypass eager OCR.
- [x] 1.2 Update DeepSeek routing defaults and document gateway recognition setup.
- [x] 1.3 Add and run regressions for native delivery, direct OCR and missing originals.

## 2. Local acceptance

- [x] 2.1 Build and deploy scoped local Docker services and configure Flash and independent recognition.
- [x] 2.2 Complete browser-to-model tests for Sol, Flash and Yinhe, including follow-up image context and request evidence.
- [x] 2.3 Restore temporary test overrides, review the diff and commit the change.


Local acceptance on 2026-11-12 passed 132 regression tests in Docker. Browser runs read random image-only identifiers and followed up on previously unmentioned shapes and colors for Sol, Flash and Yinhe. Magpie records show image parts for native Sol/Flash calls and a separate successful vision call followed by text-only Yinhe inference. Claude SDK and Antigravity Flash runs and the restored automatic route also passed. Local independent recognition uses deepseek-flash because Qwen credentials are absent. Evidence is retained at /Volumes/TP-1TB/CodexExperiments/raven-native-images-20261112/. NR-TEST deployment awaits its night window.
