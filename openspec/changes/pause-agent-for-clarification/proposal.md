## Why

用户暂时离开时，普通澄清问题会在五分钟后失效，本轮随后被取消或按不完整的信息继续。

## What Changes

- 普通澄清发出后结束当前 Agent 执行段，在原对话中保留问题。
- 用户回答后，从当前执行上下文启动下一段。刷新或重新连接对话时仍可回答。
- 移除普通澄清的超时操作设置。

## Capabilities

### New Capabilities

- `agent-clarification` covers pausing and resuming an interactive run.

### Modified Capabilities

## Impact

普通澄清工具、执行器续跑、聊天流和工作台设置。服务端强制发布确认有独立的授权生命周期。
