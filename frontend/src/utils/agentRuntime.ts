import type { AgentTraceEvent } from '@/types/agentTrace'

export function collectRuntime(events: AgentTraceEvent[]) {
  let executor = ''
  const routes: { provider: string; model: string }[] = []
  for (const event of events) {
    if (event.type !== 'system_notice') continue
    if (event.kind !== 'raven_executor' && event.kind !== 'raven_route') continue
    if (event.executor) executor = event.executor === 'antigravity_cli' ? 'Antigravity CLI' : 'Claude Agent SDK'
    if (event.kind === 'raven_route' && event.provider && event.model) {
      const previous = routes[routes.length - 1]
      if (!previous || previous.provider !== event.provider || previous.model !== event.model) {
        routes.push({ provider: event.provider, model: event.model })
      }
    }
  }
  return { executor, routes }
}
