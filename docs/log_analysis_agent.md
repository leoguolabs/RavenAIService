# Log Analysis Agent 设计说明

本文档说明日志分析 Agent（`LogAnalysisAgent`）的工作流、源码定位、以及"代码强制使用"策略。面向后端工程师、Prompt 维护者与 QA。

- Python 入口：[app/agents/log_analysis/agent.py](../app/agents/log_analysis/agent.py)
- 工作区准备：[app/agents/log_analysis/workspace.py](../app/agents/log_analysis/workspace.py)
- MCP 工具：[app/agents/log_analysis/mcp_tools.py](../app/agents/log_analysis/mcp_tools.py)
- 系统提示词：[app/prompts/prompts_config.yaml](../app/prompts/prompts_config.yaml) 下的 `claude_agent_log_analysis.generic`
- 聊天编排：[app/services/log_analysis_chat_service.py](../app/services/log_analysis_chat_service.py)
- Celery 编排：[app/tasks/ai_analysis.py](../app/tasks/ai_analysis.py)

---

## 可选的批量取证执行策略

`AGENT_EVIDENCE_EFFICIENCY_ENABLED=true` 为日志分析和项目专家启用实验性优化，默认关闭。
后端在启动时准备脱敏的任务字段、完整附件清单和完整项目卡片目录；模型仍须完成项目适配
检查，但无需为了取得同一份数据再次读取 `task.json` 或调用项目发现工具。目录不完整、
快照失败或 provider 不支持 MCP 时保留原流程。原始任务文件仍可读取，凭据字段不进入快照。
完整快照可用时，系统及用户模板中的首次任务读取要求会一起改为使用快照；这一步在填入用户问题前完成，
避免改写用户原文。缺失字段仍须按需读取；不能同时保留“必须首先读取 task.json”的相反要求。

工作区绑定的 `mcp__evidence__search_evidence` 和 `mcp__evidence__read_evidence` 接受最多
8 个独立请求，内部最多并发 4 个。搜索按路径排序，以匹配行分页，返回文件、行号、片段和
`next_request`；未遍历完时总数为未知。读取支持行号及长行字符偏移，每个请求最多返回
8,000 个字符。未读完的页面不能用于证明无命中、精确总数或全文覆盖。路径解析拒绝工作区
外部目标和外部符号链接；单项错误不丢弃同批其他结果。特殊格式仍使用原生工具。
正则引擎自动选择；需要前瞻、后顾或反向引用时使用镜像中可用的 PCRE2，仍受 15 秒查询超时限制。
MCP 返回使用附带列名的紧凑行数组：检索行为 `[path,line,column,text,line_excerpted]`，读取行为
`[line,column,text]`。检索命中的读取参数由 `read_match` 模板给出，省去每行重复的路径和参数。
这只压缩传输表示，不删除匹配行、原文、错误或续读信息；非常规读取参数保持原结构返回。
搜索、读取和自动源码核验统一按 LF 计算行号，兼容 CRLF；终端进度中的独立 `\r` 和 Unicode
分隔字符保留在原行内，避免按搜索结果续读时跳到错误位置。
检索请求可指定 `context_lines=1..40`，在同一次调用中读取本页前 3 个命中行的前后上下文。
同文件重叠范围合并，每个检索请求的上下文合计不超过 4,000 个字符；其他命中行仍完整返回，
`context_matches_not_expanded` 标明未展开数量。`context_windows` 分别保留范围、实际行、范围完成状态和
缺失字符的续读参数；范围完整不代表全文覆盖，上下文读取失败也不会删除已得到的检索结果。
读取文件末尾时可传 `tail_lines=1..2000`，与 `start_line`、`column`、`line_count` 互斥。
工具计数 LF 并返回真实行号与 `total_lines`，长行仍按原字符预算续读，无需模型先请求行数再请求末尾范围。
这些读取要求文件在检索和续读期间保持不变；原生工具继续用于特殊格式、统计和其他未支持操作。

本策略不降低轮数上限、不移除必需 Skill、不省略源码验证，也不改变最终 JSON schema。
批量取证开关本身不启用自动源码补充或全文复核；二者默认关闭，分别用
`AGENT_EVIDENCE_SOURCE_CONTEXT_ENABLED=true` 和 `AGENT_EVIDENCE_FINAL_REVIEW_ENABLED=true`
额外启用，且仍要求批量取证快照准备成功。自动源码补充开启后，对原生 Bash 中带行号的
`rg` / `grep` 源码检索，通过 SDK
`PostToolUse.additionalContext` 自动附带最多 2 段上下文（每段最多 160 行，整个返回内容不超过
8000 个 UTF-8 字节，包含说明和 JSON 开销）。源码行使用 `[行号, 字符列, 原文]` 紧凑表示；
不足以放入整行时给出准确的字符列续读位置。按总字节数限制可避免 SDK 将补充内容存盘后只返回预览，
导致模型还要再读一次文件。去重仅覆盖实际返回的完整行。
只读取当前工作区仓库内、命中路径/行号/原文核验一致的源码文件；歧义、超大文件或无法解析的
命中继续沿用原工具流程。片段带准确行号和续读参数，避免“查到位置后再单独调用一次读取”的往返。
原始工具结果不改写、不截断；自动附带源码本身不增加模型请求，不阻止重试或继续调查，也不缩减原生工具和必需 Skill。
已附带的范围按运行独立去重，文件变化后可重新附带。重复提示式检查点未通过实际试验，已移除。
仅在全文复核开关开启时，在模型自然结束后额外通过一次 `Stop` hook 要求它依据已收集证据复核最终结论、覆盖范围与确定程度，
并返回原 JSON schema 的完整结果。复核使用同一 SDK 会话，会增加至少一次模型往返，是否值得需单独实测。
同一运行只允许触发一次，`stop_hook_active` 为真时不再触发；按已观察到的主 Agent 回复数估算，
距轮数上限估算不超过 5 轮时跳过。该估算不是精确的 API 请求数或剩余轮数保证，SDK 原有上限仍有效。
复核不注入历史答案，不保证自动消除事实错误；只有独立的逐例质量审查可以决定是否接受。
组合策略的本地配对试验未通过耗时和质量验收，且观察到复核将完整草稿改为损坏输出的案例。
因此两种额外机制必须独立选择和验证，不能因启用批量工具而自动增加上下文或模型往返。
最终解析以终态 `ResultMessage` 为准，不能把首次草稿当作完成结果。
结果解析保留模型省略围栏但输出完整合法 JSON 时的结构化字段，避免将整个 JSON 当作普通
Skill 文本包装，丢失根因、证据与建议。未带围栏的截断或任意 JSON 不作为完整分析结果接受。
是否提高速度及保留分析质量，需要相同附件、问题、源码提交和模型配置下的成对实测。
`scripts/benchmark_agent_roundtrips.py` 将原始下载包、原附件、历史参考答案及运行证据保存在
仓库外；下载 ZIP 的包装信息不会作为分析输入。历史答案仅用于事后审查，不能注入被测 Agent。
汇总同时报告两臂的成功/失败数、失败率，以及包含失败的终止耗时中位数和 P90。
原有耗时下降比例明确只针对两臂都成功的配对样本；失败更快不表示回答更快。
不足 20 对成功样本、任何失败或无效计时都会阻止耗时目标通过；达到耗时目标后仍须独立审核答案质量，
统计脚本不会自行宣告最终验收通过。

日志分析与项目专家的聊天编排先提交回答和聊天记录，再生成首次会话标题。标题调用及其独立
用量统计事务不再占用聊天记录的 SQLite 写锁；标题失败、超时或取消不能撤销已经保存的回答。
标题仍有原来的 8 秒等待预算，更新时原子核对会话所有者、删除状态、原标题、消息数和更新时间，
避免覆盖期间的重命名或新消息。这项持久化修复独立于上述实验开关，不改变分析输入和正文。
计时报告须分别列出 Agent 执行耗时及其完成后的保存/标题等待，不能把写锁收益计为减少模型往返。

## 1. 工作区布局

日志详情的“Fork 到我的对话”调用 `POST /api/v1/logs/{log_id}/analysis/fork`，
要求登录。服务端按已保存轮次复制问答，从全部原日志附件重新建立 UUID 工作目录，
绑定当前用户的新会话及 `owner_user_id`；代码在首次追问时按正常 Agent 流程独立克隆。
不继承原用户运行 ID、SDK 会话或可变代码目录。旧版单结果也可复制。

上下文注册文件的 `forked_from_log_id` 标识来源；追问仅保存个人 ChatMessage，
不覆盖共享日志分析，也不自动分发来源日志的 BugFix。消息 API 的 `workspace_context`
仅在会话所有权校验后返回 Agent/项目选择，不暴露文件系统路径，供刷新恢复。
附件准备或持久化失败会回滚并清理本次新目录。

主对话支持直接上传 Markdown（`.md`、`.markdown`）、配置文本（`.ini`、`.cfg`、
`.conf`、`.yaml`、`.yml`、`.toml`、`.properties`）以及 Excel（`.xls`、`.xlsx`、
`.xlsm`），并保留已有日志文本和压缩包格式。文本原样放入 `logs/`；Excel 按
`spreadsheet` 识别并完整保留，旧版 `.xls` 可通过运行镜像中的 `xlrd` 读取。

主对话在保存任何附件前校验整批文件。格式不支持时，SSE 返回 `event=error`、
`reason=unsupported_format`、`filename`，以及包含文件名、拒绝类型、支持扩展名和
处理建议的中英文 `message`。文件大小超限与文件名非法分别返回 `file_too_large`
和 `invalid_filename`；这些校验失败不启动 Agent，不再显示通用的工作区准备失败。

随消息附带的图片使用重复的 `image_files` 文件字段，与日志 `files` 字段分开。
日志分析、项目专家和配置管理员三个 multipart 入口均采用此协议，仍兼容旧版
`images` Base64 JSON 字段。单图大小和每轮图片数量沿用 `OCR_MAX_IMAGE_MB`
及 `OCR_MAX_IMAGES`（包括管理界面的运行时覆盖）；不会叠加 1 MiB 的图片字段限制。
旧字段解析容量至少 16 MiB，并随允许的全部图片 Base64 容量加 1 MiB JSON 余量扩展。
图片在进入 OCR/Agent 前统一校验；单图超限返回 HTTP 413 / `image_too_large`，
包含图片序号、实际大小、限额及压缩建议。旧字段超限返回 413 /
`multipart_field_too_large`，数量和格式错误返回带可读原因的 HTTP 400。
普通 JSON 对话继续使用 `images`，并共享图片校验。前端区分 HTTP 拒绝与网络故障，
保留服务端错误原因；非 JSON 的代理 413 会提示压缩或分批上传。

每次任务在 `code_repo_clone_base_dir/<task_id>/` 下创建独立工作区：

```
<workspace>/
  task.json    # 任务元数据：log_id, question, log_type, hints, repo_info(可选)
  logs/        # 解压后的日志归档；metadata.json 可能在其子目录任意位置
  repo/        # 代码克隆目录（强制使用，详见第 3 节）
```

`task.json` 中的 `repo_info` 形态：

```json
{
  "project_code": "...",
  "project_name": "...",
  "repo_url": "https://...",          // 不带 token，用于展示
  "clone_url": "https://oauth2:***@...", // 注入 token，用于实际 clone
  "default_branch": "main",
  "auth_required": true,
  "matched_via": "user_selection",     // 或 metadata 中匹配到的 project_code 值
  "source": "user_selected_project_repo" // 仅当用户在前端显式选择项目时存在
}
```

---

## 2. Agent 工作流（Mandatory Workflow）

系统提示词把单次分析拆为 6 步，**Step 2–4 全部强制**：

| 步骤 | 标题 | 是否强制 |
|---|---|---|
| Step 0 | 问题分类（root_cause / qa / search / stats / meta / other） | 强制 |
| Step 1 | 定位 `metadata.json`（best-effort，找不到不报错） | 尽力 |
| Step 2 | 解析仓库信息（`task.json` / `metadata.json` / 注册表） | **强制** |
| Step 3 | 解析失败回退到 `mcp__project_repo__lookup_project_repo` | **强制** |
| Step 4 | `git clone` 到 `repo/`（若 `repo/.git` 已存在则复用） | **强制** |
| Step 5 | 同时基于日志和源码进行调查 | **强制** |
| Step 6 | 输出 fenced JSON（schema 见提示词） | 强制 |

当 Provider 支持进程内 MCP 工具时，第 4 步优先调用工作区绑定的
`mcp__project_repo__clone_project_repo`，而不是让模型接触 clone URL。主项目仍使用
`repo/`；项目卡片表明问题还需要其他项目时，工具将其克隆到
`related_repos/<project_code>/` 并返回路径、分支和 commit。项目专家复用同一工具与
路径约定。

仓库信息查找优先级（高→低）：

1. `repo_info.clone_url` / `repo_info.repo_url` + `repo_info.default_branch`
2. `project_info.clone_url` / `project_info.repo_url` + `project_info.default_branch`
3. 顶层 `clone_url` / `repo_url` / `repository_url` + `default_branch`
4. `git_context.repository_url` + `git_context.branch_name` / `git_context.commit_id`
5. 项目身份回退：`project_info.project_code` → `project_code` → `issue_info.project_code` → `log_types.<log_type>.project_code` → 任一 `log_types.*.project_code` → `issue_info.service_name`

无任何可解析仓库信息时，直接输出 `"status": "error", "error_kind": "missing_project_identity"`，**不允许仅凭日志答题**。

### 2.1 Skill 相关性预选

对于可选领域 Skill，每次运行不会把所有已启用 Skill 都物化到工作区。服务端先根据当前问题、hints、
`issue_info` 的描述/服务/环境，以及附件**文件名**做确定性打分；项目同名 Skill 先覆盖
全局 Skill，再参与排序。只保留有正向证据且达到最高分 65% 的候选，最多 3 个；没有
匹配时保持空集，不回退为“全部加载”。Skill 正文仅在服务端用于匹配，不拼进提示词。

运行开始事件中的 `available_skills` 表示当前 Agent/项目已启用的完整 Skill 清单，
请求相关性筛选仍只物化最多 3 个候选，因此该清单不会整体进入模型上下文。只有 trace 中
出现 `Skill` 工具调用，才表示可选 Skill 的说明被模型实际读取。协议细节见
[agent_trace_protocol.md](agent_trace_protocol.md#skill-availability-and-actual-use)。

日志分析和项目专家还会在**每轮调用（包括追问）**强制加载源码内置的 `humanizer-zh`。
完整上游规则、MIT 许可证、固定版本信息位于 `app/agents/required_skills/humanizer-zh/`；
`response_policy.md` 固化技术回答约束。服务端将完整规则直接加入最终系统提示词，
不依赖关键词匹配、管理员上传或模型自行调用 `Skill`，不占用上述 3 个可选名额。
同名上传 Skill 无法覆盖它，新环境随镜像默认具备；缺少必需文件时在调用模型前报错。
后台可预览，不能禁用、删除或覆盖。前端“已加载 Skills”通过真实的
`required_skill_loaded` 事件显示加载状态。

该规则仅整理自然语言，必须保留原始日志、代码、数值、引用、事实与不确定性，
服从回答语言和 JSON 输出契约，不附带通用写作评分表，也不启用纯文本结果兜底。
加载事件证明完整规则已送入本轮提示词，不代表额外执行了一次模型工具调用或质量评分。

### 2.2 澄清提问优先于工作流

当用户开启了全局偏好「指令不清晰时允许 Agent 向我提问」时，本 Agent 会额外拿到
`mcp__ask__AskUserQuestion` 工具，以及一段说明「澄清优先于上面的强制工作流」的提示词
（[app/agents/clarification.py](../app/agents/clarification.py) 的 `workflow_agent=True` 分支）。

这段说明是必需的，不是锦上添花：上面的 6 步工作流本身在提示词里被表述为强制流程，
模型会把它理解为「无论如何都要把流程走完并产出 JSON」，从而在收到「请定位问题」这类
没有指明现象、模块或时间范围的笼统诉求时，直接挑一种解读继续跑完，而不是先问清楚。
指引里因此显式列出了本 Agent 场景下「问题不清晰」的判定标准（多处彼此独立的可疑点、
现象在材料中无对应线索、多附件未指明目标），以及「澄清可以发生在流程中间，拿到答案后
从当前步骤继续，最终仍按规定输出围栏 JSON」。

提问由聊天服务通过 `clarification_binding` 注入；Celery 批处理入口不传该参数，因此
不会提问（没有人在 SSE 那头作答）。事件与 broker 机制见
[agent_trace_protocol.md](agent_trace_protocol.md#clarification-askuserquestion)。

### 2.3 项目卡片发现与多项目工作区

项目专家和日志分析在作出项目相关结论前先调用
`mcp__project_repo__discover_projects` 读取完整的已启用项目卡片目录：

- 当前项目已覆盖问题：克隆/复用 `repo/`，不追加无关项目。
- 日志分析发现当前项目选错、另一个项目明确匹配：在当前会话调用
  `clone_project_repo(project_code)`，从返回的 `related_repos/...` 检出中回答。
  项目专家则应请用户改选项目；改选后同一会话会自动创建对应的新工作区。
- 问题确实跨项目：只克隆完成问题所必需的项目，并在证据中标明项目与仓库路径。
- 无匹配或证据含糊：如实报告无匹配或请求澄清，不试探性克隆最相近项目。

项目专家在同一聊天会话中改选项目时，服务端先校验新项目，再创建全新工作区；
同项目追问继续复用当前工作区。新工作区的 `task.json.repo_info` 以本轮选择为准，
完整的既往文字对话写入 `conversation_history.json`，最近 12 条消息仍作为简短提示。
旧项目的回答仅帮助理解追问，不能作为新项目实现的源码证据。任务仍在运行时，
切换请求会明确要求任务结束后重试，不替换运行中的工作区。

针对当前所选项目的实现结论，项目专家只把**经项目卡片确认的同系列仓库**作为
实现证据。如果该系列缺少协议栈等组件仓库，应直接说明该组件源码无法核实。
其他系列的代码可用于明确比较或参考，但回答必须标明参考项目与真实来源，
不得把参考代码的行为写成所选项目的实现；系列归属不清楚时先澄清。

克隆工具只接受 `project_code`，目标路径由服务端决定；HTTPS token、SSH 身份与仓库
URL 不会出现在工具响应。重复调用复用已有检出，成功后在 `task.json.related_repos`
保存不含凭据的项目卡片、相对路径、分支与 commit，供后续轮次和运行态核验。

---

## 3. 代码强制使用策略

**核心原则**：源码是日志真实含义的 ground truth；克隆并查阅源码是每次分析的强制环节，不再由模型自行判断。

适用范围：

- 无论问题类型（`qa` / `search` / `stats` / `meta` / `root_cause` / `other`）。
- 无论日志包是否含 `metadata.json`。
- 无论用户是否在前端显式选择了项目仓库。

### 3.1 行为对比

| 场景 | 旧行为 | 新行为 |
|---|---|---|
| 有 metadata.json，qa 类问题 | AI 自行判断"不用代码" → 不克隆 | 必须克隆 + 用代码佐证 |
| 无 metadata.json，前端选了项目 | AI 自行判断 → 经常跳过克隆 | 直接用 `task.json` 里的 `repo_info` → 必须克隆 + 用代码佐证 |
| 无 metadata.json 且未选项目 | 看情况报 `missing_project_identity` | 一律报 `missing_project_identity` 终止 |
| 克隆失败 | 行为未定义 | 在 `answer` 里如实报错，降级为 logs-only，禁止编造代码证据 |

### 3.2 用户显式选择项目的链路

1. 前端把 `project_repo_id` 随请求带入；
2. [log_analysis_chat_service.py](../app/services/log_analysis_chat_service.py) 调 `prepare(log_record, require_metadata=False)`（不再要求 `metadata.json` 存在），并调用 `_inject_repo_info_from_project_id`；
3. [ai_analysis.py](../app/tasks/ai_analysis.py) 的 `_inject_repo_info_from_project_id` 把仓库信息写入 `task.json`，标记 `source: "user_selected_project_repo"`；
4. [agent.py](../app/agents/log_analysis/agent.py) 在渲染提示词时探测到该标记，向系统提示词追加 *User-Selected Project Repository* 段落，告知 Agent：`repo_info` 权威、可跳过 metadata 发现，但**克隆与代码查阅仍是强制项**。

### 3.3 克隆失败的容错

- `git clone` 失败时，Agent 必须在 `answer` 中如实说明失败（带退出码 / 错误摘要），随后降级为 logs-only 模式。
- **不允许**用日志虚构出"代码层面"的结论。
- 证据数组中 `repo:path/file.go:N` 形式的引用必须指向**真实存在**的代码行。

---

## 4. Provider / MCP 能力矩阵

`discover_projects`、`lookup_project_repo` 与工作区绑定的 `clone_project_repo` 在不同 Provider 下的可用性见 [app/agents/anthropic_client.py](../app/agents/anthropic_client.py) 的 `PROVIDER_PROFILES`。当 `supports_mcp_server_tools=False` 时：

- 三个 MCP 工具都从 `allowed_tools` 中剔除；
- 系统提示词追加 *Runtime Constraint* 段落，提示 Agent 仅使用显式的 `repo_info` / `metadata.json` 字段；
- 仍**强制**克隆与代码查阅；找不到显式仓库信息时输出 `project_repo_not_registered` 终止。
- 该运行不能追加其他项目，且不得声称已完成多项目分析。

---

## 5. 输出 Schema（Step 6）

```json
{
  "status": "ok",
  "question_type": "root_cause|qa|search|stats|meta|other",
  "answer": "<面向用户问题的直接回答，中文>",
  "summary": "<一句话摘要，中文>",
  "severity": "info|warn|error|critical",
  "root_cause_hypotheses": [
    {
      "hypothesis": "<描述>",
      "evidence": ["repo:path/file.go:42", "log:subdir/app.log:100"]
    }
  ],
  "recommended_actions": [],
  "related_keywords": ["keyword1", "keyword2"]
}
```

字段约束按 `question_type` 分桶（详见提示词 Step 6）：

- `root_cause`：`root_cause_hypotheses` 可填，但每条必须带证据；无相关假设时留 `[]`，不要为凑数编造无关问题。
- `qa` / `search` / `stats` / `meta` / `other`：`root_cause_hypotheses` 必须为 `[]`，`severity` 默认 `info`，真正答案放 `answer`（可用 Markdown）。

---

## 6. 错误码（`error_kind`）

| `error_kind` | 触发条件 |
|---|---|
| `missing_archive` | `LogRecord` 无 `archive_path` / `file_path`，或文件不存在 |
| `missing_project_identity` | `metadata.json` / `task.json` 中均无可解析的仓库字段或项目身份 |
| `project_repo_not_registered` | 通过 `lookup_project_repo` 仍解析不到仓库；或 MCP 工具不可用且无显式仓库信息 |
| `timeout` | 超出 `anthropic_request_timeout_seconds` |
| `cancelled` | 用户主动取消（来自 `cancel_event`） |
| `schema_mismatch` | 模型输出不含 fenced JSON 或缺字段（兜底） |

---

## 7. Trace 协议

Agent 运行期间通过 `trace_emitter` 回调推送 `AgentTraceEvent`，事件类型与传输方式见 [agent_trace_protocol.md](agent_trace_protocol.md)。`LogAnalysisAgent.run` 同时把所有事件累计到结果里的 `trace_events`，便于断线重连/历史回放。

---

## 8. 维护清单

修改 Agent 行为时建议至少同步以下位置：

- [app/prompts/prompts_config.yaml](../app/prompts/prompts_config.yaml) — 系统提示词主体
- [app/agents/log_analysis/agent.py](../app/agents/log_analysis/agent.py) — 运行时提示词增量（Runtime Constraint / User-Selected Project Repository）
- [app/tasks/ai_analysis.py](../app/tasks/ai_analysis.py) — `repo_info` 注入逻辑
- [app/agents/clarification.py](../app/agents/clarification.py) — 澄清提问工具与提示词（四个对话 Agent 共用，改动会同时影响它们）
- 本文档 — 行为对比表与错误码表

## BugFix 独立审核与上下文交接

日志分析的 `requires_code_fix` / `proposed_fixes` 表示修复建议。BugFix 使用后台“模型设置”中的独立端点（`bug_fix_agent_provider/api_key/base_url/model`），不会回退到分析主备模型。API Key 留空保留已保存值，连接测试 target 为 `bug_fix`。Token 上限、最大回合与超时也可独立调整；未配置专用密钥会返回 `bug_fix_model_unconfigured`。

兼容不支持 Token/超时构造参数的 Python SDK 版本：底层 CLI 同时接收 `CLAUDE_CODE_MAX_OUTPUT_TOKENS` 与 `API_TIMEOUT_MS`，BugFix 编排另有总执行时限，避免后台保存成功但预算参数在 SDK 层被忽略。

新派发任务在共享 `data/bug_fix_context/<task-id>/` 保存不可变证据快照：原始 task 的 question/hints/附件清单、完整分析结果、所有日志文件以及用户截图原件。日志分析工作区清理或会话图片删除后，BugFix 与重试仍从快照恢复并校验 SHA-256。快照不会复制源码仓库或 Git 凭据。原图能力由 BugFix 自己的 provider 决定；文本模型使用同一份 OCR/描述，必须说明视觉证据限制，不得凭空补全图片信息。

`source_analysis.json` 是便于读取的证据索引，保留问题、历史、分析结论与附件清单；完整分析结果及历史工具输出另存 `analysis_result.json` 供定向查阅，避免长工具轨迹超过 SDK 的单次读取限制。即使分析模型不支持原图，已授权会话中早先轮次的可用截图也会交给 BugFix。

旧任务从原日志附件分组、匹配修复项的历史分析结果、任务创建前的聊天消息与可用截图重建上下文。详情标记“旧任务上下文重建”，并在源上下文中列出未保留的提示词及缺失截图；无法读取必需日志或快照校验失败时直接记录错误。

每项修复建议必须得到唯一、完整的 `fix_outcomes` 结局。`rejected` 表示 BugFix 独立审核后拒绝分析建议，必须提供日志/源码反证或明确缺失证据；`already_implemented` 表示基线已实现。全部有据拒绝可成功完成审核且不创建 MR。缺失/重复编号、无理由拒绝、没有对应 MR 的 `created_mr` 均不得报告全部成功。

执行过程按项写 `bug_fix_result.json` 检查点，超回合或 SDK 异常时可恢复已产出的 MR 并将未完成项标记失败。详情保存实际模型以及脱敏后的错误类型和原始错误说明；部分产出不会因错误标签被吞掉而误判为全部成功。写入型运行不自动重新调用模型以重放工具副作用。
