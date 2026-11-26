## Why

会话工作区保留解包日志和仓库副本，会持续占用服务磁盘。把已在 NR-TEST 执行的 30 天清理规则加入 RavenAIService 的日常维护。

## What Changes

- 每天删除最后活动超过 30 天的完整会话工作区，包括解包日志和仓库副本。
- 任务跳过正在运行或排队的会话，适用于已部署版本创建的工作区。
- 会话历史和源日志上传继续由各自的生命周期管理。

## Capabilities

### New Capabilities

- `chat-workspace-retention` 管理持久会话工作区的保留期和定时清理。

### Modified Capabilities

## Impact

Celery 维护队列、会话运行期间的工作区访问和共享工作区卷。
