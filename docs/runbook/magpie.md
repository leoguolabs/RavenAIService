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

「模型设置 → Agent 执行与模型路由 → 打开 Magpie 后台」会在新标签页打开管理页。Compose 默认使用 `http://127.0.0.1:<MAGPIE_ADMIN_PORT>/`。通过 SSH 转发时，将本机转发端口设为该地址的端口。如果已经有浏览器可访问的管理地址，可在 RavenAIService 的 `.env` 中设置 `MAGPIE_ADMIN_URL`，填写不含凭据、查询参数或片段的 HTTP(S) URL。首次打开时在 Magpie 页面输入 Web 管理密钥。

Admin API 为 `GET/PUT /admin/agent-runtime` 和 `GET /admin/agent-runtime/models`，需要管理员认证。密钥只写不回显，空密钥保留原值。更改只影响新任务。模型目录中的完整 ID（如 `raven-yinhe/yinhe-thinking`）或 Magpie 路由组 ID 均可填写；单个 Agent 留空即继承默认配置。

执行器和模型分别继承。例如项目专家仅设置 `executor=antigravity_cli`、`model=null`，仍使用全局默认模型；选择 Antigravity 不需要 OpenAI 供应商。测试某个模型后应恢复测试前的配置，交付时不要残留验收专用的模型覆盖。

## 运行与验收

两种执行器共享工作区、项目提示词、materialized Skills 和 Raven MCP handler。Antigravity 使用独立临时 HOME、结构化 stdin/stdout、PreInvocation/PreToolUse/Stop hook；工具权限及澄清仍由 Raven 管理。未知原生工具默认拒绝，避免 CLI 升级绕过现有权限规则。第三方 stdio/HTTP MCP 配置暂不直接透传，只桥接 Raven 拥有的 SDK MCP server。模型能力来自 Magpie 模型目录；改变模型能力后刷新目录。

Antigravity 的首次调用注入已注册 MCP 工具的名称及参数 schema，使设备 Agent 无需先通过 Read/Bash 查找 CLI 内部工具说明。真正的 MCP 调用、原生文件读写和 shell 权限仍经过原有 Raven 审批；这不会把任意文件或命令自动放行。

设备结果校验兼容 Claude SDK 的 content 列表与 Antigravity 的完整 MCP 结果对象。Claude 的 `allowed_tools` 仅自动批准已判定为 read 的设备工具；write/destructive/未知工具必须进入 Raven broker，回传决策会转成 SDK 所需的类型。`--deny` / `--approve-write` 可用本地设备桩交叉检查拒绝后零执行、批准后仅执行一次。

至少验收：两种执行器分别使用银河、DeepSeek、OpenAI；读取含随机证据编号的日志、激活同一个 Skill、调用 MCP；浏览器检查工具卡片、实际执行器与模型，刷新后回放；取消一个会话并确认另一个并发会话正常完成；制造不可用主端点并确认实际备用模型出现在回答及 trace 中。只收到模型文本或容器 healthy 不算完整验收。

模型切换由 Magpie 按单次请求处理。Raven 不会在工具已执行后重跑整个 Agent。实际模型取自 `X-Magpie-Provider` / `X-Magpie-Model`，请求的路由组保存在 `requested_model`；两者可能不同。取消会关闭本次代理与 CLI 进程组，历史 trace 和取消终态继续保留。BugFix 完成后的详情也使用同一 trace 组件。

### 供应商停用与错误排查

若 Magpie 中关闭了 OpenAI，但某个 Agent 的模型覆盖仍为 `raven-openai/gpt-6.1-terra`，请求会被 Magpie 拒绝（HTTP 404，`provider switched off`）。显式模型 ID 不表示自动选其他供应商。清除该 Agent 的模型覆盖即可继承默认模型；需要自动切换时，在 Magpie 创建含多个模型的路由组，并在 Raven 选择 `group/<id>`。仅停用供应商不会自动修改 Raven 已保存的模型覆盖。

两种执行器均应报告 HTTP 状态、执行器、请求模型及可识别的停用原因。桥接层仅输出自有的双语诊断，不持久化原始上游错误正文或 CLI stderr；结构化 CLI 错误终态也不会被后续的 exit code 覆盖。成功重试会清除之前的网关失败，避免把后续工具错误误归因于旧请求。

### 按任务选型、固定模型与银河慢速保护

`python scripts/configure-magpie-task-routing.py` 预览配置，追加 `--apply` 才保存。它复用 Magpie 官方 intent/classifier 规则，新建四个 `raven-task-*` 路由组，不修改供应商密钥、旧路由组或各 Agent 的执行器。保存前会在 `data/magpie-routing-backups/` 留下权限为 0600 的私有回滚副本，其中含网关密钥，禁止提交或输出内容。

默认入口 `group/raven-task-intent`：DeepSeek Flash 分类；简单问答、翻译和提取使用 DeepSeek Flash；明确的常规查询和只读排查优先银河；复杂诊断、设计、代码与设备配置修改及分类不明确时优先 GPT。含图片请求优先 GPT。规则决定初始优先级，首次回答前仍有 Magpie 的故障兜底；分类不是质量认证。

`agent_runtime.task_routing` 是可验证的独立策略，仅在选择了其 `entry_group` 时生效。Raven 每次执行器 invocation 创建一个会话并固定实际返回模型；随后不再让每轮工具结果触发重新分类。显式模型覆盖不参与这个策略。并发推理在该任务内串行；不同任务互不占用此锁。

- 银河首 token：专属子组设置 1 秒，包含文本、推理或工具调用的首个有效输出，超时由 Magpie 转 GPT。这是银河尝试的时限，不包含分类和备用模型的响应时间。
- 银河生成速度：首个有效输出后观察 1 秒再开始交付；短且已完成的响应立即交付，快速大块输出也可提前交付。按有效输出 UTF-8 字节数 / 4 估算 token，包括返回的思考与工具参数，不将心跳当输出；窗口低于 10 估算 token/s，或生成空闲达到 5 秒，视为慢速。它不是供应商 tokenizer 的精确计数，需结合实际工作负载调整。
- 输出前变慢：取消未交付推理，仅重试这一次请求到 GPT，保留全部请求上下文，后续固定 GPT。
- 输出后变慢：按用户选择保留当前流，不拼接、不重放；后续请求固定 GPT。当前流仍受原有传输超时约束。
- 慢速检测共享 60 秒冷却，其他新任务使用 `group/raven-task-cloud` 继续按意图在 DeepSeek/GPT 间选择。Redis 不可用时退化为进程内冷却。冷却结束只影响新任务，已切换的任务不再返回银河。首次请求由 Magpie 自己完成的首 token 兜底仍适用其原生重复慢速休息规则；桥接层能观测到的慢速及已固定银河的后续兜底会打开共享冷却。

这些参数是初始运行阈值，不是性能承诺。恢复旧行为可禁用 `task_routing.enabled` 并恢复原默认模型；不要把 `stays=strict` 用作此策略的替代，因为它会禁止需要的慢速兜底。更改只适用于新启动的 invocation；当前运行仍使用其策略快照。实际模型继续来自 `X-Magpie-*`，计划切换不会提前冒充已服务模型。部署时 backend、worker、worker-bugfix 必须具有相同桥接实现。

银河真实调用还可能把 Antigravity 的 MCP `Arguments` 编码为 JSON 字符串。控制桥接层只将合法 JSON 对象解析后交给原有权限检查；畸形 JSON、数组或空值返回工具错误，不触发工具执行，拒绝权限和调用 ID 的关联保持不变。

### OpenAI 真实余额门槛

可选层 `docker-compose.magpie-balance.yml` 在 Magpie 与 OpenAI 之间运行内部 `openai-balance-guard`，不发布宿主端口。每次新请求先查询同一组织的真实剩余额度，低于 **15 USD** 返回 402；恰好 15 USD 允许请求。余额缺失、无效、查询失败或超过 2 秒均停止转发，不使用历史成功值或本地消费估算。已开始的推理继续原流，不重放工具；后续请求重新检查。供应商的 DeepSeek fallback 接收拒绝后的请求，启用了任务固定策略的 invocation 记录实际备用模型并继续使用它。

余额来源是登录后账单页实际使用的 `https://api.openai.com/v1/dashboard/billing/credit_grants` 的 `total_available`，已与页面余额核对。`wallet.balance` 不含所有赠送额度，不能代替它；公开 Costs API 给的是消费金额，也不是余额。这个 dashboard 接口不是稳定的公开 API，登录凭据失效或接口变化时会暂停 OpenAI，直到重新接入有效来源。这是严格模式的预期行为。

私有 JSON 文件含 `authorization`（账单来源的 Bearer 凭据）、`organization`（匹配 API key 的组织）和 `api_key_sha256`（该推理 key 的完整 SHA-256 摘要）。文件权限 0600，父目录 0700，放在仓库之外；不要输出、提交或粘贴到聊天。账单凭据只发送到固定账单端点，推理 key 只发送到固定 OpenAI API 源站，二者都不跟随重定向。新增或轮换推理 key 时必须同步指纹及组织校验，不能只更改 Magpie key。

设置 `RAVEN_BALANCE_GUARD_IMAGE` 为包含 `app/services/openai_balance_guard.py` 的后端镜像，`OPENAI_BALANCE_SOURCE_FILE` 为私有 JSON 的绝对路径。在**现有部署的完整 Compose 参数后**追加本层，只启动 `openai-balance-guard`；保留已有部署的镜像及覆盖层。服务使用移除全部 capabilities、只读文件系统的 root 身份读取 0600 secret。`/health` 只表示进程健康，并给出最后余额检查时间与决策；它不表示余额仍可用。

确认服务健康后，先在 Magpie 自己的私有数据卷备份 `providers.json`，再执行：

```sh
magpie provider fallback raven-openai raven-deepseek/deepseek-v4-flash
magpie provider set raven-openai \
  responses=http://openai-balance-guard:8099/v1 \
  balance=http://openai-balance-guard:8099/balance \
  'balance.path=$balance' additional-tools=on
```

这里保留原有模型、key、压缩策略及其他供应商设置；`additional-tools=on` 显式保留 OpenAI 原域名下默认支持的动态工具。确认该供应商没有仍直连 OpenAI 的其他协议入口。`/balance` 也需要匹配的推理 key，低余额时仍返回真实金额供管理页显示，推理是否放行由独立的逐请求检查决定。不要把管理页余额缓存当作门禁依据。

刷新凭据后，原地写入文件会在下次请求生效；如果用原子替换更新文件，需要仅重建此服务，使 Compose secret 的文件挂载指向新文件。Magpie 的配额休息可能延迟恢复；已经转到备用模型的任务保持原选择。回滚需恢复私有备份中的供应商入口及 fallback；这样也会撤销余额保护，不能以恢复调用为由自动绕过门槛。

此规则按上游最新报告余额决定是否接收新请求，不为在途请求预留费用。并发消费、其他客户端消费或账单更新延迟仍可能让最终余额低于 15 USD。每次余额检查也会增加一次网络往返。

### 六类 Agent 交叉用例

`scripts/verify-agent-model-matrix.py` 是显式运行的真实调用用例。每个单元使用独立进程/容器及私有运行配置副本，不保存管理员配置。覆盖真实 Agent 类入口、模型选择、真实 CLI 与网关、工具执行、结果解析和 trace；采用短合成提示词及本地只读设备/项目目录桩，因此不代表完整日志分析、真实设备操作或修复质量验收。

| 维度 | 用例与断言 |
| --- | --- |
| Agent | 通用、日志分析、设备、项目专家、配置管理员、BugFix 六种实际入口 |
| 执行器 | `claude_sdk`、`antigravity_cli` |
| 启用模型 | 遍历 `/v1/models` 的全部目标模型；必须有实际路由、真实工具步骤及随机证据回读，结构化 Agent 还必须解析成功 |
| 已停用模型 | 加 `--disabled`；必须指出供应商停用，没有实际路由，也不执行模型驱动的工具 |
| 路由组 | 已停用 OpenAI 放首位、启用模型放后位；`--actual-model` 指定备用模型，断言 requested 与 actual 分别正确 |
| 继承 | 单独覆盖执行器后修改默认模型，Agent 必须继承新模型；配置保存及启动用例另有单元回归 |

在具有同版本 CLI、网关网络访问及隔离数据目录的验收容器中运行单元，例如：

```sh
PYTHONPATH=/app RUNTIME_SETTINGS_PATH=/run/runtime.private.json \
  python scripts/verify-agent-model-matrix.py \
  --agent device_agent --executor antigravity_cli \
  --model raven-yinhe/yinhe-thinking --output /evidence/device-yinhe.json
```

运行配置副本包含 caller key，应只读挂载、限制文件权限，不提交。矩阵运行时保持已停用供应商关闭，不能把预期失败列计作模型成功调用。新增供应商/模型后扩展目标列表重跑，不能用三家现有模型的结果推定所有未来模型都支持工具或图片输入。

## 回滚与边界

先把默认执行器改为 Claude SDK，并清除 Antigravity 覆盖，再关闭「通过 Magpie 路由模型」；可以同一次保存。新任务恢复原有端点配置，正在运行的任务保持原选择。不要删除 `magpie_data`，否则 OAuth 与路由状态会丢失。

独立部署用不同 Compose project、端口、数据卷和管理员配置；不要复用运行中的实验卷。此功能不修改默认关闭的 Claude SDK 性能实验开关，不构成该性能实验的验收。生产升级需要另行部署授权。

本轮本地镜像和完整 E2E 在 ARM64 上验收。AMD64 下载包的 SHA-512 校验通过，但本机跨架构执行未通过，因此不将其记为 AMD64 运行验收；部署 AMD64 主机时需执行镜像构建中的版本检查及上述 E2E。
