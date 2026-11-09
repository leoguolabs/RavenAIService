import { describe, expect, it } from 'vitest'
import { collectRuntime } from './agentRuntime'
import type { AgentTraceEvent } from '@/types/agentTrace'

describe('runtime attribution', () => {
  it('identifies the official OpenAI SDK in live and replayed routes', () => {
    const events: AgentTraceEvent[] = [{ task_id: 'openai', timestamp: 1, seq: 0,
      type: 'system_notice', kind: 'raven_route', executor: 'openai_agents', agent_key: 'general_agent',
      provider: 'deepseek', model: 'served-model', requested_model: 'group/intent' }]
    expect(collectRuntime(JSON.parse(JSON.stringify(events)))).toEqual({
      executor: 'OpenAI Agents SDK', agentKey: 'general_agent', routes: [{ provider: 'deepseek', model: 'served-model' }],
    })
  })
  it('uses actual route notices and retains fallback history on replay', () => {
    const base = { task_id: 'one', timestamp: 1 }
    const events: AgentTraceEvent[] = [
      { ...base, seq: 0, type: 'run_start', model: 'group/requested' },
      { ...base, seq: 1, type: 'system_notice', kind: 'raven_executor', executor: 'antigravity_cli', agent_key: 'project_expert' },
      { ...base, seq: 2, type: 'system_notice', kind: 'raven_route', provider: 'yinhe', model: 'actual-one' },
      { ...base, seq: 3, type: 'system_notice', kind: 'raven_route', provider: 'deepseek', model: 'actual-two' },
    ]
    expect(collectRuntime(events)).toEqual({ executor: 'Antigravity CLI', agentKey: 'project_expert', routes: [
      { provider: 'yinhe', model: 'actual-one' }, { provider: 'deepseek', model: 'actual-two' },
    ] })
    expect(collectRuntime(JSON.parse(JSON.stringify(events)))).toEqual(collectRuntime(events))
  })
})
