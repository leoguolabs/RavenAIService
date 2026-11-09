## 1. Implementation

- [x] 1.1 Add validated opt-in runtime policy and shared cooldown.
- [x] 1.2 Pin inference per task and add pre-output/committed-stream local performance handling.
- [x] 1.3 Add an idempotent intent-group configuration command and update the runbook.

## 2. Verification and activation

- [x] 2.1 Verify routing isolation, real streaming, slow failover, cancellation and unchanged tool context.
- [x] 2.2 Deploy scoped files to the local Magpie stack while preserving its running application changes.
- [x] 2.3 Enable official intent rules and verify real classification, tool continuation and reported model identity.
- [x] 2.4 Review the final diff, validate OpenSpec and commit task-owned files without pushing.
