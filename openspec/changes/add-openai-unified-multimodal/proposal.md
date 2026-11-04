## Why

现有 Agent 仅支持 Anthropic Messages 协议，截图依赖单独 OCR 模型，无法直接使用用户的 OpenAI 多模态额度。需要在保留现有 Agent 工具、权限与运行轨迹的前提下，通过 OpenAI 官方标准接口统一处理文字与原始截图。

## What Changes

- 新增 OpenAI Responses provider，支持标准密钥、流式输出、工具调用和模型设置连通测试。
- 使用每次运行独立的本地协议适配器保留既有 SDK 工具执行、MCP、权限、取消与运行轨迹；所有远程推理请求仅发送到配置的 OpenAI 端点。
- OpenAI 模式下跳过独立 OCR，图片原件与本轮文本直接送入同一个模型；已有非 OpenAI provider 保持兼容。
- 本地 Docker 部署并使用真实 API Key 完成文字、图片、多轮、工具、取消与计量的端到端验证。密钥仅保存于本地私密配置，不添加付款方式、购买额度或自动充值。

## Capabilities

### New Capabilities

- `openai-multimodal-runtime`: OpenAI 标准接口、流式与工具协议转换、同模型图文输入及运行隔离。

### Modified Capabilities

- `anthropic-llm-config`: provider 目录及模型设置支持 OpenAI 原生 Responses 协议，保留既有配置键兼容性。

## Impact

后端模型配置、公共 SDK 调用入口、图像持久化/物化与聊天上下文、Admin 模型界面与文档，以及 RavenClient 同步路由的 Responses 协议选择；新增回归测试和本地 Docker 验证。不接触生产 nr-test，不推送代码或凭据。
