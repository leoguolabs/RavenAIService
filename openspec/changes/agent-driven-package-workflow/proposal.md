## Why

Configuration Manager bypasses the model and rejects real component versions using a static project catalog. Failed packaging runs can also disappear or fail to restore when opening the conversation again.

## What Changes

- Run the actual Agent for uploaded packages, with code/repository/archive investigation and workspace scripting enabled.
- Let the Agent determine project, component IDs, versions, payload layout and build the artifact from evidence; remove catalog-driven classification/build from the chat execution path.
- Keep generic archive safety, input integrity, explicit publication confirmation and atomic repository delivery.
- Persist failed and cancelled turns and make conversation restoration independent of long-lived streams and stale view requests.

## Capabilities

### New Capabilities

- `agent-package-execution`: Evidence-based Agent packaging and validated artifact publication.
- `package-conversation-recovery`: Recoverable running, failed and completed packaging conversations.

### Modified Capabilities

None.

## Impact

Package Agent/tools/Skill, packaging chat service, conversation history and frontend run store, focused regression tests and local Docker end-to-end verification using LX07A, LX10AB and 三标段 releases. No production deployment is included.
