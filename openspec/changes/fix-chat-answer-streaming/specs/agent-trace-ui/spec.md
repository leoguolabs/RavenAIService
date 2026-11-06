## MODIFIED Requirements

### Requirement: 最终答复正文增量渲染

主对话框 SHALL 消费裸 `answer_delta` 和 `agent_trace` 包装的 `answer_delta`，将 `text_chunk` 投影为当前轮次 assistant 正文。正文 MUST 在终态到达前持续可见；首段正文 MUST 清除思考占位，且正文增量 MUST NOT 进入工具/思考卡片。重复 `(run_id, seq)` MUST 被丢弃。

结构化 JSON 结果 MUST 在 `answer` 字符串尚未闭合时解码并展示已有正文，MUST NOT 展示 JSON 外壳、其他字段或不完整转义。普通 Markdown MUST 正常显示。

实时回答 SHALL 平滑逐字呈现，较大数据块和仅有终态全文的实时回答也 MUST 渐进呈现。历史消息 MUST 直接展示。完成时 MUST 以权威全文校正，差异前缀立即修正，新增后缀可继续平滑显示。减少动态效果偏好 MUST 被尊重。切换会话或卸载 MUST 清理展示计时器，不得串流到其他会话。

#### Scenario: 答复逐字呈现
- **WHEN** 工具和思考完成后，两种 SSE 包装中的任一种陆续传来「根据」「日志分析，」「根因是…」
- **THEN** 正文在 `done` 前逐步增长至「根据日志分析，根因是…」，思考占位消失，工具卡片不包含正文

#### Scenario: 结构化结果尚未闭合
- **WHEN** 模型陆续输出 JSON 元数据和未闭合的 `answer` 字符串，转义横跨多个数据块
- **THEN** 前端持续展示已解码的 `answer`，不显示 JSON 外壳或不完整转义

#### Scenario: run_complete 收尾校正
- **WHEN** 权威全文与增量正文不一致
- **THEN** 正文以权威全文收尾，与持久化内容一致，已显示的错误前缀立即被修正

#### Scenario: 说明文字后输出结构化结果
- **WHEN** 模型先输出调查进展，再增量输出包含顶层 `answer` 和结果元数据的裸 JSON 或 fenced JSON，且任意字符边界可能分块
- **THEN** 前端在结果尚未闭合时展示解码后的正文，缓冲尚不能判定的 JSON 前缀，任何中间帧均不显示结果外壳；重连快照与实时增量使用相同规则
- **AND** 普通 Markdown、独立 JSON 示例及已解码正文内的 JSON / Mermaid 代码块保持完整

#### Scenario: provider 降级回退
- **WHEN** 新一轮没有正文增量，只收到终态全文
- **THEN** 该回答平滑逐字展示，历史消息保持直接展示

#### Scenario: answer_delta 去重及恢复
- **WHEN** 活动快照含正文增量，且重连后重放重复 seq
- **THEN** 前端立即恢复已有正文，后续正文不重复、不丢失，其他会话不受影响

#### Scenario: 展示计时器清理
- **WHEN** 用户在逐字展示期间离开页面
- **THEN** 展示计时器被清理；启用减少动态效果时正文直接展示
