## Why

已登录的用户目前无法在工作台修改自己的 RavenAI 密码，需要联系管理员重设。

## What Changes

- 在工作台设置增加修改密码表单，要求填写当前密码、新密码和确认密码。
- 增加仅作用于当前登录账号的改密接口，验证当前密码并按注册规则校验新密码。
- 为表单和改密结果提供中文和英文文案。

## Capabilities

### New Capabilities

- `self-service-password-change` 提供工作台登录账号的自助改密流程。

### Modified Capabilities

## Impact

涉及 RavenAIService 的 Vue 工作台、用户认证 API、用户服务和回归测试。
