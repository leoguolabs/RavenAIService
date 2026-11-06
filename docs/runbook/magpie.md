# Magpie 与 Agent 执行器

管理员在「模型设置 → Agent 执行与模型路由」选择默认执行器、默认模型和六类 Agent 的覆盖配置。支持 `claude_sdk` 与 `antigravity_cli`；普通用户仍使用原来的聊天、日志分析和任务入口。自动选择执行器不在本阶段范围。

## 启动

在 RavenAI 父仓库初始化 `Magpie` 子模块（`git@github.com:leoguolabs/magpie.git`）。服务目录的 `docker-compose.magpie.yml` 使用 `../Magpie` 构建网关，`magpie_data` 保存账户、OAuth 状态及路由配置。后台镜像安装 SHA-512 校验的 Antigravity CLI 1.5.2，支持 Linux ARM64/AMD64；不读取或修改桌面的 CLI 登录状态。

在 RavenAIService 目录生成一次 Web 管理密钥。父目录仅当前用户可访问；文件需要允许容器内 Magpie 用户读取。不要覆盖已有密钥，不要提交文件。

```sh
python - <<'PY'
from pathlib import Path
import secrets
directory = Path('data/magpie-private')
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
directory.chmod(0o700)
with (directory / 'web-key').open('x') as out:
    out.write(secrets.token_urlsafe(48))
(directory / 'web-key').chmod(0o644)
PY
docker compose -f docker-compose.yml -f docker-compose.magpie.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.magpie.yml exec backend python scripts/bootstrap-magpie.py --enable
```

Bootstrap 从现有主模型、备用模型和 BugFix 模型配置读取端点及密钥池，新增 `raven-<provider>`，创建 Raven 专用 caller key，并缓存模型能力。重复执行保留已有 Magpie provider、caller key 和管理员选择，不覆盖手工调整。旧模型配置保留用于回滚；以后新增账户、配额、路由组、故障切换均在 Magpie 中维护。已有同名 provider 的密钥池变更需要在 Magpie 管理页显式更新。

默认管理页只绑定宿主 `127.0.0.1:3430`（可用 `MAGPIE_ADMIN_PORT` 更改）；网关 `3425` 仅在 Compose 网络开放。后台仅持有 caller key，子进程只拿到本次运行的 loopback token。远程主机通过 SSH 转发访问管理页。OAuth 账户需要在 Magpie 中按对应提供商完成登录；使用现有 API Key 不需要 Antigravity Google 登录。

Admin API 为 `GET/PUT /admin/agent-runtime` 和 `GET /admin/agent-runtime/models`，需要管理员认证。密钥只写不回显，空密钥保留原值。更改只影响新任务。模型目录中的完整 ID（如 `raven-yinhe/yinhe-thinking`）或 Magpie 路由组 ID 均可填写；单个 Agent 留空即继承默认配置。

## 运行与验收

两种执行器共享工作区、项目提示词、materialized Skills 和 Raven MCP handler。Antigravity 使用独立临时 HOME、结构化 stdin/stdout、PreInvocation/PreToolUse/Stop hook；工具权限及澄清仍由 Raven 管理。未知原生工具默认拒绝，避免 CLI 升级绕过现有权限规则。第三方 stdio/HTTP MCP 配置暂不直接透传，只桥接 Raven 拥有的 SDK MCP server。模型能力来自 Magpie 模型目录；改变模型能力后刷新目录。

至少验收：两种执行器分别使用银河、DeepSeek、OpenAI；读取含随机证据编号的日志、激活同一个 Skill、调用 MCP；浏览器检查工具卡片、实际执行器与模型，刷新后回放；取消一个会话并确认另一个并发会话正常完成；制造不可用主端点并确认实际备用模型出现在回答及 trace 中。只收到模型文本或容器 healthy 不算完整验收。

模型切换由 Magpie 按单次请求处理。Raven 不会在工具已执行后重跑整个 Agent。实际模型取自 `X-Magpie-Provider` / `X-Magpie-Model`，请求的路由组保存在 `requested_model`；两者可能不同。取消会关闭本次代理与 CLI 进程组，历史 trace 和取消终态继续保留。BugFix 完成后的详情也使用同一 trace 组件。

## 回滚与边界

先把默认执行器改为 Claude SDK，并清除 Antigravity 覆盖，再关闭「通过 Magpie 路由模型」；可以同一次保存。新任务恢复原有端点配置，正在运行的任务保持原选择。不要删除 `magpie_data`，否则 OAuth 与路由状态会丢失。

独立部署用不同 Compose project、端口、数据卷和管理员配置；不要复用运行中的实验卷。此功能不修改默认关闭的 Claude SDK 性能实验开关，不构成该性能实验的验收。生产升级需要另行部署授权。

本轮本地镜像和完整 E2E 在 ARM64 上验收。AMD64 下载包的 SHA-512 校验通过，但本机跨架构执行未通过，因此不将其记为 AMD64 运行验收；部署 AMD64 主机时需执行镜像构建中的版本检查及上述 E2E。
