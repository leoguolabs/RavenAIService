import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick, watchEffect } from 'vue'

import { THINKING_PLACEHOLDER, useConversationRunsStore } from '@/stores/conversationRuns'
import { userApi } from '@/api/user'
import {
  packageSearchStream,
  projectExpertStream,
  resolveChatPermission,
  resolveChatClarification,
} from '@/api/chat'
import { mergeUniquePackageFiles } from '@/utils/packageAttachments'
import { LOCALE_HEADER } from '@/i18n/runtime'
import { setI18nLocale } from '@/i18n'
import type { ChatEntry, PendingPermission } from '@/stores/conversationRuns'
import type { AgentTraceEvent } from '@/types/agentTrace'

vi.mock('@/api/chat', () => ({
  packageSearchStream: vi.fn(),
  projectExpertStream: vi.fn(),
  resolveChatPermission: vi.fn(),
  resolveChatClarification: vi.fn(),
}))

const flushPromises = () => new Promise((resolve) => setTimeout(resolve, 0))

const sseResponse = (events: Record<string, unknown>[]) => {
  const encoder = new TextEncoder()
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const event of events) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
      }
      controller.close()
    },
  })
  return new Response(body, { status: 200 })
}

const openSseResponse = (events: Record<string, unknown>[]) => {
  const encoder = new TextEncoder()
  let controller: ReadableStreamDefaultController<Uint8Array> | null = null
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c
      for (const event of events) {
        c.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`))
      }
    },
  })
  return {
    response: new Response(body, { status: 200 }),
    close: () => controller?.close(),
  }
}

const traceEvent = (
  runId: string,
  sessionId: string,
  seq: number,
  type: AgentTraceEvent['type'],
): Record<string, unknown> => ({
  event: type,
  type,
  task_id: runId,
  run_id: runId,
  session_id: sessionId,
  seq,
  timestamp: seq,
})

describe('conversationRuns store', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    setI18nLocale('zh')
  })

  it('routes streamed events by session/run and keeps another selected session clean', () => {
    const store = useConversationRunsStore()
    const sessionA = store.ensureState('session-a')
    const sessionB = store.ensureState('session-b')

    store.mergeSnapshot(sessionA, {
      run_id: 'run-a',
      session_id: 'session-a',
      status: 'running',
      agent_kind: 'device',
      answer_so_far: 'A 正在处理',
      trace_events: [traceEvent('run-a', 'session-a', 1, 'run_start')],
    })
    sessionB.messages.push({ id: 'b-user', role: 'user', content: 'B message', kind: 'user' })

    store.applyEventToState(sessionA, traceEvent('run-a', 'session-a', 2, 'step_delta'))
    store.applyEventToState(sessionB, traceEvent('run-a', 'session-a', 3, 'step_delta'))
    store.applyEventToState(sessionB, traceEvent('run-b', 'session-b', 1, 'step_delta'))

    const aAnswer = sessionA.messages.find((m: ChatEntry) => m.id === 'run:run-a:assistant')
    const bAnswer = sessionB.messages.find((m: ChatEntry) => m.id === 'run:run-b:assistant')

    expect(aAnswer?.traceEvents?.map((e: AgentTraceEvent) => e.seq)).toEqual([1, 2])
    expect(sessionB.messages.some((m: ChatEntry) => m.id === 'run:run-a:assistant')).toBe(false)
    expect(bAnswer?.traceEvents?.map((e: AgentTraceEvent) => e.seq)).toEqual([1])
    expect(sessionA.seenSeq['run-a:1']).toBe(1)
    expect(sessionB.seenSeq['run-b:1']).toBe(1)
  })

  it('allows a device run in session B while session A log analysis is still running', async () => {
    const store = useConversationRunsStore()
    const sessionA = store.ensureState('session-a')

    store.mergeSnapshot(sessionA, {
      run_id: 'run-a',
      session_id: 'session-a',
      status: 'running',
      agent_kind: 'log_analysis',
      trace_events: [traceEvent('run-a', 'session-a', 1, 'run_start')],
    })

    const fetchMock = vi.fn().mockResolvedValue(sseResponse([
      { event: 'session', session_id: 'session-b', run_id: 'run-b' },
      traceEvent('run-b', 'session-b', 1, 'run_start'),
      { ...traceEvent('run-b', 'session-b', 2, 'run_complete'), final_text: 'B final' },
      { event: 'done', session_id: 'session-b', run_id: 'run-b', answer: 'B final' },
    ]))
    vi.stubGlobal('fetch', fetchMock)

    await store.startDeviceRun('session-b', {
      message: 'hello from B',
      target_device_id: 'dev-b',
    })

    const sessionB = store.ensureState('session-b')
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/ai-chat/chat/stream'),
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ [LOCALE_HEADER]: 'zh' }),
      }),
    )
    const requestBody = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect(requestBody.agent_type).toBe('device')
    expect(requestBody.target_device_id).toBe('dev-b')
    expect(sessionA.isSending).toBe(true)
    expect(sessionA.activeRunId).toBe('run-a')
    expect(sessionB.isSending).toBe(false)
    expect(sessionB.runStatus).toBe('succeeded')
    expect(sessionB.messages.map((m: ChatEntry) => m.content)).toContain('hello from B')
    expect(sessionB.messages.map((m: ChatEntry) => m.content)).toContain('B final')
    expect(store.localRunningSessionIds).toEqual(['session-a'])
  })

  it('restores a log-analysis run snapshot and cancels only that run id', async () => {
    const store = useConversationRunsStore()
    const sessionA = store.ensureState('session-a')
    const traceEvents = Array.from({ length: 10 }, (_, idx) =>
      traceEvent('log-run-a', 'session-a', idx + 1, idx === 0 ? 'run_start' : 'step_delta'),
    )

    store.mergeSnapshot(sessionA, {
      run_id: 'log-run-a',
      session_id: 'session-a',
      status: 'running',
      agent_kind: 'log_analysis',
      answer_so_far: '日志分析中',
      trace_events: traceEvents,
      pending_permissions: [
        {
          request_id: 'req-a',
          tool_name: 'Read',
          risk: 'read',
          tool_input: { path: 'a.log' },
          run_id: 'log-run-a',
          session_id: 'session-a',
        },
      ],
    })

    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await store.cancelActiveRun('session-a')

    const answer = sessionA.messages.find((m: ChatEntry) => m.id === 'run:log-run-a:assistant')
    expect(sessionA.runAgentKind).toBe('log_analysis')
    expect(sessionA.isSending).toBe(true)
    expect(answer?.traceEvents).toHaveLength(10)
    expect(sessionA.pendingPermissions[0]).toMatchObject({
      request_id: 'req-a',
      run_id: 'log-run-a',
    })
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/ai-chat/chat/runs/log-run-a/cancel'),
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('restores a Fork workspace and project without inheriting the source run', async () => {
    const store = useConversationRunsStore()
    vi.spyOn(userApi, 'fetchMessages').mockResolvedValue({
      success: true,
      message: 'ok',
      data: [
        { id: 'u', session_id: 'fork', role: 'user', content: '原问题', created_at: '', updated_at: '' },
        { id: 'a', session_id: 'fork', role: 'ai', content: '原分析', created_at: '', updated_at: '' },
      ],
      workspace_context: { agent_kind: 'log_analysis', project_repo_id: 42, has_log_workspace: true },
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })))
    const state = await store.loadSession('fork', { isLoggedIn: true, force: true })
    expect(state.messages.map((m) => m.content)).toEqual(['原问题', '原分析'])
    expect(state.lastAgentKind).toBe('log_analysis')
    expect(state.lastProjectRepoId).toBe(42)
    expect(state.hasLogWorkspaceContext).toBe(true)
    expect(state.activeRunId).toBeNull()
    expect(state.isSending).toBe(false)
  })

  it('loads DB history, restores active-run trace, and finishes from the resumed stream', async () => {
    const store = useConversationRunsStore()
    const traceEvents = Array.from({ length: 10 }, (_, idx) =>
      traceEvent('run-a', 'session-a', idx + 1, idx === 0 ? 'run_start' : 'step_delta'),
    )

    vi.spyOn(userApi, 'fetchMessages').mockResolvedValue({
      success: true,
      data: [
        {
          id: 'msg-user-a',
          session_id: 'session-a',
          role: 'user',
          content: '请检查设备状态',
          created_at: '2026-05-25T10:00:00.000Z',
          updated_at: '2026-05-25T10:00:00.000Z',
        },
      ],
    } as any)

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        run_id: 'run-a',
        session_id: 'session-a',
        status: 'running',
        agent_kind: 'device',
        answer_so_far: '已读取设备信息',
        trace_events: traceEvents,
        pending_permissions: [
          {
            request_id: 'req-a',
            tool_name: 'Write',
            risk: 'write',
            tool_input: { command: 'reset' },
            run_id: 'run-a',
            session_id: 'session-a',
          },
        ],
      }), { status: 200 }))
      .mockResolvedValueOnce(sseResponse([
        traceEvent('run-a', 'session-a', 11, 'step_delta'),
        { ...traceEvent('run-a', 'session-a', 12, 'run_complete'), final_text: '设备状态正常' },
        { event: 'done', session_id: 'session-a', run_id: 'run-a', answer: '设备状态正常' },
      ]))
    vi.stubGlobal('fetch', fetchMock)

    const state = await store.loadSession('session-a', { isLoggedIn: true, authToken: 'token', force: true })
    await flushPromises()
    const answer = state.messages.find((m: ChatEntry) => m.id === 'run:run-a:assistant')

    expect(userApi.fetchMessages).toHaveBeenCalledWith('session-a')
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining('/api/v1/ai-chat/chat/sessions/session-a/active-run'),
      expect.objectContaining({ credentials: 'include' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining('/api/v1/ai-chat/chat/runs/run-a/stream'),
      expect.objectContaining({ credentials: 'include' }),
    )
    expect(state.messages[0]).toMatchObject({ id: 'msg-user-a', role: 'user' })
    expect(answer?.content).toBe('设备状态正常')
    expect(answer?.traceEvents?.map((e: AgentTraceEvent) => e.seq)).toEqual([...Array(12)].map((_, i) => i + 1))
    expect(state.pendingPermissions[0]).toMatchObject({ request_id: 'req-a', run_id: 'run-a' })
    expect(state.isSending).toBe(false)
    expect(state.runStatus).toBe('succeeded')
    expect(store.localRunningSessionIds).toEqual([])
  })

  it('restores persisted trace events from DB history after page refresh', async () => {
    const store = useConversationRunsStore()
    const traceEvents = [
      traceEvent('run-a', 'session-a', 1, 'run_start'),
      traceEvent('run-a', 'session-a', 2, 'thinking_start'),
      {
        ...traceEvent('run-a', 'session-a', 3, 'thinking_delta'),
        text_chunk: '分析日志结构',
      },
      {
        ...traceEvent('run-a', 'session-a', 4, 'thinking_end'),
        text: '分析日志结构',
      },
      traceEvent('run-a', 'session-a', 5, 'run_complete'),
    ]

    vi.spyOn(userApi, 'fetchMessages').mockResolvedValue({
      success: true,
      data: [
        {
          id: 'msg-user-a',
          session_id: 'session-a',
          role: 'user',
          content: '请分析日志',
          created_at: '2026-05-25T10:00:00.000Z',
          updated_at: '2026-05-25T10:00:00.000Z',
        },
        {
          id: 'msg-ai-a',
          session_id: 'session-a',
          role: 'ai',
          content: '分析完成',
          created_at: '2026-05-25T10:01:00.000Z',
          updated_at: '2026-05-25T10:01:00.000Z',
          run_id: 'run-a',
          run_status: 'succeeded',
          run_agent_kind: 'log_analysis',
          trace_events: traceEvents,
        },
      ],
    } as any)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })))

    const state = await store.loadSession('session-a', { isLoggedIn: true, authToken: 'token', force: true })
    const answer = state.messages.find((m: ChatEntry) => m.id === 'msg-ai-a')

    expect(answer?.traceEvents?.map((e: AgentTraceEvent) => e.seq)).toEqual([1, 2, 3, 4, 5])
    expect(answer?.traceRunning).toBe(false)
  })

  it('tracks a successful log workspace context only on the event session', () => {
    const store = useConversationRunsStore()
    const sessionA = store.ensureState('session-a')
    const sessionB = store.ensureState('session-b')

    expect(sessionA.hasLogWorkspaceContext).toBe(false)
    expect(sessionB.hasLogWorkspaceContext).toBe(false)

    store.applyEventToState(sessionA, {
      event: 'log_analysis_context',
      session_id: 'session-a',
      log_id: 'log-a',
      filenames: ['first.zip'],
    })
    store.applyEventToState(sessionB, {
      event: 'log_analysis_context',
      session_id: 'session-a',
      log_id: 'log-a',
    })

    expect(sessionA.hasLogWorkspaceContext).toBe(true)
    expect(sessionB.hasLogWorkspaceContext).toBe(false)
  })

  it('restores persisted log attachment context without leaking it to another session', async () => {
    const store = useConversationRunsStore()
    vi.spyOn(userApi, 'fetchMessages').mockImplementation(async (sessionId: string) => ({
      success: true,
      data: [
        {
          id: `msg-${sessionId}`,
          session_id: sessionId,
          role: 'user',
          content: sessionId === 'session-a'
            ? '请分析\n\n[日志附件] first.zip'
            : '普通设备问题',
          created_at: '2026-08-12T10:00:00.000Z',
          updated_at: '2026-08-12T10:00:00.000Z',
        },
      ],
    } as any))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })))

    const sessionA = await store.loadSession('session-a', { isLoggedIn: true, force: true })
    const sessionB = await store.loadSession('session-b', { isLoggedIn: true, force: true })

    expect(sessionA.hasLogWorkspaceContext).toBe(true)
    expect(sessionB.hasLogWorkspaceContext).toBe(false)
  })

  it('returns from loadSession after restoring a running snapshot without waiting for stream terminal', async () => {
    const store = useConversationRunsStore()
    const openStream = openSseResponse([
      traceEvent('run-a', 'session-a', 2, 'step_delta'),
    ])

    vi.spyOn(userApi, 'fetchMessages').mockResolvedValue({
      success: true,
      data: [
        {
          id: 'msg-user-a',
          session_id: 'session-a',
          role: 'user',
          content: '继续检查设备',
          created_at: '2026-05-25T10:00:00.000Z',
          updated_at: '2026-05-25T10:00:00.000Z',
        },
      ],
    } as any)

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        run_id: 'run-a',
        session_id: 'session-a',
        status: 'running',
        agent_kind: 'device',
        answer_so_far: '正在读取设备状态',
        trace_events: [traceEvent('run-a', 'session-a', 1, 'run_start')],
        pending_permissions: [],
      }), { status: 200 }))
      .mockResolvedValueOnce(openStream.response)
    vi.stubGlobal('fetch', fetchMock)

    const state = await store.loadSession('session-a', { isLoggedIn: true, authToken: 'token', force: true })
    const answer = state.messages.find((m: ChatEntry) => m.id === 'run:run-a:assistant')

    expect(state.loadingMessages).toBe(false)
    expect(state.isSending).toBe(true)
    expect(answer?.content).toBe('正在读取设备状态')
    expect(fetchMock).toHaveBeenCalledTimes(2)

    openStream.close()
  })

  it('submits restored HITL decisions with run_id and clears only that request', async () => {
    const store = useConversationRunsStore()
    const sessionA = store.ensureState('session-a')
    store.mergeSnapshot(sessionA, {
      run_id: 'run-a',
      session_id: 'session-a',
      status: 'running',
      agent_kind: 'device',
      pending_permissions: [
        {
          request_id: 'req-a',
          tool_name: 'Write',
          risk: 'write',
          tool_input: { command: 'reset' },
          run_id: 'run-a',
          session_id: 'session-a',
        },
        {
          request_id: 'req-b',
          tool_name: 'Read',
          risk: 'read',
          tool_input: { path: '/tmp/log' },
          run_id: 'run-a',
          session_id: 'session-a',
        },
      ],
    })
    vi.mocked(resolveChatPermission).mockResolvedValue({
      success: true,
      message: 'ok',
      request_id: 'req-a',
      decision: 'allow',
    })

    await store.submitPermission('session-a', 'req-a', 'allow', {
      updatedArgs: { command: 'status' },
      authToken: 'token',
    })

    expect(resolveChatPermission).toHaveBeenCalledWith(
      'req-a',
      {
        decision: 'allow',
        updated_args: { command: 'status' },
        session_id: 'session-a',
        run_id: 'run-a',
      },
      'token',
    )
    expect(sessionA.pendingPermissions.map((p: PendingPermission) => p.request_id)).toEqual(['req-b'])
  })

  it('restores the user message from snapshot when DB history lacks it (log-analysis resume)', () => {
    const store = useConversationRunsStore()
    const sessionA = store.ensureState('session-a')

    // Simulates the post-DB-fetch state for a log-analysis run that is still
    // in flight: the chat_messages row hasn't been written yet because the
    // log-analysis service only persists user+assistant together at terminal.
    store.mergeSnapshot(sessionA, {
      run_id: 'log-run-a',
      session_id: 'session-a',
      status: 'running',
      agent_kind: 'log_analysis',
      answer_so_far: '日志分析中',
      user_message: '请分析这个日志包',
      trace_events: [traceEvent('log-run-a', 'session-a', 1, 'run_start')],
    })

    const userMsg = sessionA.messages.find((m: ChatEntry) => m.role === 'user')
    const answerMsg = sessionA.messages.find((m: ChatEntry) => m.id === 'run:log-run-a:assistant')

    expect(userMsg).toMatchObject({
      id: 'run:log-run-a:user',
      role: 'user',
      content: '请分析这个日志包',
    })
    // User message must be ordered before the assistant placeholder.
    const userIdx = sessionA.messages.findIndex((m: ChatEntry) => m.id === 'run:log-run-a:user')
    const aiIdx = sessionA.messages.findIndex((m: ChatEntry) => m.id === 'run:log-run-a:assistant')
    expect(userIdx).toBeLessThan(aiIdx)
    expect(answerMsg?.content).toBe('日志分析中')
  })

  it('does not duplicate the user message when DB history already has it', () => {
    const store = useConversationRunsStore()
    const sessionA = store.ensureState('session-a')
    // Simulate post-DB-fetch state: backend already persisted the user msg
    // (DeviceAgent path commits the user message before the SSE stream
    // starts), so loadSession's fetchMessages has it in state.messages.
    sessionA.messages.push({
      id: 'msg-user-a',
      role: 'user',
      content: '请检查设备状态',
      kind: 'user',
    })

    store.mergeSnapshot(sessionA, {
      run_id: 'run-a',
      session_id: 'session-a',
      status: 'running',
      agent_kind: 'device',
      user_message: '请检查设备状态',
      trace_events: [traceEvent('run-a', 'session-a', 1, 'run_start')],
    })

    const userMsgs = sessionA.messages.filter((m: ChatEntry) => m.role === 'user')
    expect(userMsgs).toHaveLength(1)
    expect(userMsgs[0].id).toBe('msg-user-a')
  })

  // -- answer_delta incremental rendering ----------------------------------

  const answerDelta = (
    runId: string,
    sessionId: string,
    seq: number,
    textChunk: string,
  ): Record<string, unknown> => ({
    event: 'answer_delta',
    type: 'answer_delta',
    run_id: runId,
    session_id: sessionId,
    seq,
    timestamp: seq,
    text_chunk: textChunk,
  })

  it('appends answer_delta chunks and clears the thinking placeholder on first delta', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))

    const answer = state.messages.find((m: ChatEntry) => m.id === 'run:run-a:assistant')
    expect(answer?.content).toBe(THINKING_PLACEHOLDER)

    store.applyEventToState(state, answerDelta('run-a', 'session-a', 2, '根据'))
    expect(answer?.content).toBe('根据')
    store.applyEventToState(state, answerDelta('run-a', 'session-a', 3, '日志分析，'))
    store.applyEventToState(state, answerDelta('run-a', 'session-a', 4, '根因是…'))
    expect(answer?.content).toBe('根据日志分析，根因是…')
    // answer_delta is prose, not a trace step.
    expect(answer?.traceEvents?.some((e: AgentTraceEvent) => e.type === 'answer_delta')).toBeFalsy()
  })

  it('dedupes answer_delta by seq on replay so no characters repeat', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))

    store.applyEventToState(state, answerDelta('run-a', 'session-a', 2, 'AB'))
    store.applyEventToState(state, answerDelta('run-a', 'session-a', 3, 'CD'))
    // Replayed duplicates with the same seq must be dropped.
    store.applyEventToState(state, answerDelta('run-a', 'session-a', 2, 'AB'))
    store.applyEventToState(state, answerDelta('run-a', 'session-a', 3, 'CD'))

    const answer = state.messages.find((m: ChatEntry) => m.id === 'run:run-a:assistant')
    expect(answer?.content).toBe('ABCD')
  })

  it.each(['log_analysis', 'project_expert', 'package_search'] as const)(
    'displays wrapped %s answer deltas before done without polluting trace cards', (agentKind) => {
      const store = useConversationRunsStore()
      const state = store.ensureState('session-a')
      state.runAgentKind = agentKind
      store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
      const wrapped = (seq: number, chunk: string) => ({
        ...answerDelta('run-a', 'session-a', seq, chunk), event: 'agent_trace',
      })
      store.applyEventToState(state, wrapped(2, '```json\n{"status":"ok","answer":"根据'))
      const answer = state.messages.find((message) => message.role === 'ai')!
      expect(answer.content).toBe('根据')
      store.applyEventToState(state, wrapped(3, '日志分析，'))
      store.applyEventToState(state, wrapped(3, '日志分析，'))
      expect(answer.content).toBe('根据日志分析，')
      expect(answer.traceEvents?.some((frame) => frame.type === 'answer_delta')).toBe(false)
      expect(state.runStatus).toBe('running')
      store.applyEventToState(state, { event: 'done', answer: '权威全文' })
      expect(answer.content).toBe('权威全文')
    },
  )

  it('restores answer deltas from an active snapshot and dedupes subsequent replay', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    const first = answerDelta('run-a', 'session-a', 2, '{"answer":"已有')
    store.mergeSnapshot(state, {
      run_id: 'run-a', status: 'running', agent_kind: 'project_expert',
      trace_events: [first, traceEvent('run-a', 'session-a', 1, 'run_start'), first],
    })
    const answer = state.messages.find((message) => message.role === 'ai')!
    expect(answer.content).toBe('已有')
    expect(answer.animateAnswer).toBe(false)
    // Production specialist trace frames omit run_id after the prologue.
    const { run_id: _runId, ...replayed } = first
    store.applyEventToState(state, { ...replayed, event: 'agent_trace' })
    store.applyEventToState(state, {
      ...answerDelta('run-a', 'session-a', 3, '内容'), event: 'agent_trace',
    })
    expect(answer.content).toBe('已有内容')
    expect(answer.animateAnswer).toBe(true)
  })

  it('corrects the bubble to final_text on run_complete after streaming deltas', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
    store.applyEventToState(state, answerDelta('run-a', 'session-a', 2, '根据日志'))

    store.applyEventToState(state, {
      ...traceEvent('run-a', 'session-a', 3, 'run_complete'),
      final_text: '根据日志分析，根因是失锁。',
    })

    const answer = state.messages.find((m: ChatEntry) => m.id === 'run:run-a:assistant')
    expect(answer?.content).toBe('根据日志分析，根因是失锁。')
    expect(state.runStatus).toBe('succeeded')
    expect(answer?.traceRunning).toBe(false)
  })

  it('clears the local running overlay when a run fails before run_id is known', async () => {
    const store = useConversationRunsStore()
    const fetchMock = vi.fn().mockRejectedValue(new Error('network down'))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.stubGlobal('fetch', fetchMock)

    await store.startDeviceRun('session-a', { message: 'hello' })

    const state = store.ensureState('session-a')
    expect(state.isSending).toBe(false)
    expect(state.runStatus).toBe('failed')
    expect(store.localRunningSessionIds).toEqual([])
  })

  it('uploads large log-analysis images as binary files while preserving log attachments', async () => {
    const store = useConversationRunsStore()
    const fetchMock = vi.fn().mockResolvedValue(sseResponse([{ event: 'done', answer: 'ok' }]))
    vi.stubGlobal('fetch', fetchMock)
    const bytes = 'x'.repeat(700 * 1024)
    const log = new File(['frequency=2080000'], 'lrun_oam4.log')
    await store.startLogAnalysisRun('upload-images', {
      message: '分析频点', files: [log],
      images: [
        { media_type: 'image/png', data: btoa(bytes) },
        { media_type: 'image/png', data: `data:image/png;base64,${btoa(bytes)}` },
      ],
    })
    const form = fetchMock.mock.calls[0][1].body as FormData
    expect(form.has('images')).toBe(false)
    expect(form.getAll('files')).toEqual([log])
    const images = form.getAll('image_files') as File[]
    expect(images.map((file) => file.size)).toEqual([bytes.length, bytes.length])
    expect(images.map((file) => file.type)).toEqual(['image/png', 'image/png'])
    expect(await images[1].text()).toBe(bytes)
    expect(store.ensureState('upload-images').runStatus).toBe('succeeded')
  })

  it.each([
    [400, { detail: { reason: 'unsupported_type', message: '请选择 PNG 图片' } }, '请选择 PNG 图片'],
    [413, { message: '第 2 张图片超过单图 5 MiB 上限，请压缩图片。' }, '第 2 张图片超过单图 5 MiB 上限'],
    [400, { message: 'Part exceeded maximum size of 1024KB.' }, '1 MiB'],
    [422, { detail: [{ loc: ['body', 'project_repo_id'], msg: 'Project ID must be a number' }] }, 'Project ID must be a number'],
    [401, { detail: 'Unauthorized' }, '登录会话已过期'],
  ])('shows actionable log-upload HTTP %s details', async (status, body, expected) => {
    const store = useConversationRunsStore()
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status })))
    await store.startLogAnalysisRun('upload-error', { message: '分析' })
    const state = store.ensureState('upload-error')
    const answer = state.messages.find((message) => message.role === 'ai')!
    expect(answer.content).toContain(expected)
    expect(answer.content).not.toContain('请检查网络')
    expect(answer.traceRunning).toBe(false)
    expect(state.runStatus).toBe('failed')
    expect(store.localRunningSessionIds).toEqual([])
  })

  it.each([
    [413, 'zh', '超过服务器允许的大小'],
    [413, 'en', 'exceed the server size limit'],
    [502, 'zh', 'HTTP 502'],
    [503, 'en', 'HTTP 503'],
    [504, 'zh', 'HTTP 504'],
    [429, 'zh', '请求过于频繁'],
    [403, 'zh', 'HTTP 403'],
  ])('explains non-JSON proxy HTTP %s in %s without showing HTML', async (status, locale, expected) => {
    setI18nLocale(locale)
    const store = useConversationRunsStore()
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>nginx error</html>', { status })))
    await store.startLogAnalysisRun('proxy-error', { message: '分析' })
    const answer = store.ensureState('proxy-error').messages.find((message) => message.role === 'ai')!
    expect(answer.content).toContain(expected)
    expect(answer.content).not.toContain('<html>')
    expect(answer.content).not.toContain('请检查网络')
  })

  it('uploads a multi-gigabyte log attachment', async () => {
    const store = useConversationRunsStore()
    const fetchMock = vi.fn().mockResolvedValue(new Response('', { status: 200 }))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.stubGlobal('fetch', fetchMock)
    const huge = new File(['x'], 'gnb_ts_fpga.log')
    Object.defineProperty(huge, 'size', { value: 2 * 1024 * 1024 * 1024 })

    await store.startLogAnalysisRun('too-big', {
      message: '分析',
      files: [huge],
    })

    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('reserves the connection failure message for failed log-analysis transport', async () => {
    const store = useConversationRunsStore()
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    await store.startLogAnalysisRun('offline', { message: '分析' })
    const state = store.ensureState('offline')
    expect(state.messages.find((message) => message.role === 'ai')?.content).toContain('请检查网络')
    expect(state.isSending).toBe(false)
  })

  it('updates the visible error reactively on the first request in a new conversation', async () => {
    const store = useConversationRunsStore()
    vi.spyOn(console, 'error').mockImplementation(() => {})
    let rejectUpload!: (response: Response) => void
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(new Promise<Response>((resolve) => { rejectUpload = resolve })))
    let visibleAnswer: string | undefined
    const stop = watchEffect(() => {
      visibleAnswer = store.bySession['first-upload']?.messages.find((message) => message.role === 'ai')?.content
    })
    const pending = store.startLogAnalysisRun('first-upload', { message: '分析' })
    await nextTick()
    expect(visibleAnswer).toBe(THINKING_PLACEHOLDER)
    rejectUpload(new Response(JSON.stringify({ message: '图片超过 5 MiB，请压缩后重试。' }), { status: 413 }))
    await pending
    await nextTick()
    expect(visibleAnswer).toContain('图片超过 5 MiB，请压缩后重试。')
    stop()
  })

  it('falls back to whole-segment final_text render when no answer_delta arrives', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))

    const answer = state.messages.find((m: ChatEntry) => m.id === 'run:run-a:assistant')
    expect(answer?.content).toBe(THINKING_PLACEHOLDER)

    store.applyEventToState(state, {
      ...traceEvent('run-a', 'session-a', 2, 'run_complete'),
      final_text: '整段渲染的答复。',
    })
    expect(answer?.content).toBe('整段渲染的答复。')
  })

  // -- suggested_agent_type routing hint -----------------------------------

  it('captures suggested_agent_type from run_complete and resets on next run', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
    store.applyEventToState(state, {
      ...traceEvent('run-a', 'session-a', 2, 'run_complete'),
      final_text: '该需求需要使用项目专家。',
      suggested_agent_type: 'project_expert',
    })
    expect(state.suggestedAgentType).toBe('project_expert')

    // A brand-new run latches a new run_id and clears the stale suggestion.
    state.activeRunId = null
    store.applyEventToState(state, traceEvent('run-b', 'session-a', 1, 'run_start'))
    expect(state.suggestedAgentType).toBe(null)
  })

  it('ignores unknown suggested_agent_type values', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
    store.applyEventToState(state, {
      ...traceEvent('run-a', 'session-a', 2, 'run_complete'),
      final_text: '答复',
      suggested_agent_type: 'totally_unknown',
    })
    expect(state.suggestedAgentType).toBe(null)
  })

  it('reads suggested_agent_type from the done frame', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
    store.applyEventToState(state, {
      event: 'done',
      run_id: 'run-a',
      session_id: 'session-a',
      status: 'succeeded',
      answer: '请先选择日志分析。',
      suggested_agent_type: 'log_analysis',
    })
    expect(state.suggestedAgentType).toBe('log_analysis')
  })

  // ---- AskUserQuestion clarification --------------------------------------

  const clarificationRequest = (
    runId: string,
    sessionId: string,
    seq: number,
    requestId: string,
  ): Record<string, unknown> => ({
    event: 'clarification_request',
    type: 'clarification_request',
    task_id: runId,
    run_id: runId,
    session_id: sessionId,
    seq,
    timestamp: seq,
    request_id: requestId,
    questions: [
      {
        header: 'svc',
        question: 'which service?',
        options: [
          { label: 'nginx', description: 'web' },
          { label: 'redis', description: 'cache' },
        ],
      },
    ],
  })

  it('pushes a pending clarification on clarification_request and clears it on resolved', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
    store.applyEventToState(state, clarificationRequest('run-a', 'session-a', 2, 'req-1'))
    expect(state.pendingClarifications.map((c) => c.request_id)).toEqual(['req-1'])
    expect(state.pendingClarifications[0].draftSelected).toEqual([[]])

    store.applyEventToState(state, {
      event: 'clarification_resolved',
      type: 'clarification_resolved',
      task_id: 'run-a',
      run_id: 'run-a',
      session_id: 'session-a',
      seq: 3,
      timestamp: 3,
      request_id: 'req-1',
      outcome: 'answered',
    })
    expect(state.pendingClarifications).toEqual([])
  })

  // The workspace agents (log-analysis / project-expert / package-search) wrap
  // every trace event in `event: "agent_trace"`, unlike DeviceAgent which
  // streams them bare. That wrapper used to divert their clarification events
  // into the trace-only branch, so their question card never rendered even
  // though the user had clarification switched on.
  const wrappedClarificationRequest = (
    runId: string,
    sessionId: string,
    seq: number,
    requestId: string,
  ): Record<string, unknown> => ({
    ...clarificationRequest(runId, sessionId, seq, requestId),
    event: 'agent_trace',
  })

  it('pushes a pending clarification from an agent_trace-wrapped request', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
    store.applyEventToState(
      state,
      wrappedClarificationRequest('run-a', 'session-a', 2, 'req-1'),
    )
    expect(state.pendingClarifications.map((c) => c.request_id)).toEqual(['req-1'])
    expect(state.pendingClarifications[0].run_id).toBe('run-a')

    store.applyEventToState(state, {
      event: 'agent_trace',
      type: 'clarification_resolved',
      task_id: 'run-a',
      run_id: 'run-a',
      session_id: 'session-a',
      seq: 3,
      timestamp: 3,
      request_id: 'req-1',
      outcome: 'answered',
    })
    expect(state.pendingClarifications).toEqual([])
  })

  it('drops a pending clarification when an agent_trace run terminates', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
    store.applyEventToState(
      state,
      wrappedClarificationRequest('run-a', 'session-a', 2, 'req-1'),
    )
    expect(state.pendingClarifications).toHaveLength(1)

    store.applyEventToState(state, {
      event: 'agent_trace',
      type: 'run_complete',
      task_id: 'run-a',
      run_id: 'run-a',
      session_id: 'session-a',
      seq: 3,
      timestamp: 3,
    })
    expect(state.pendingClarifications).toEqual([])
    expect(state.runStatus).toBe('succeeded')
  })

  it('keeps one session\'s clarification out of another session', () => {
    const store = useConversationRunsStore()
    const sessionA = store.ensureState('session-a')
    const sessionB = store.ensureState('session-b')
    store.applyEventToState(sessionA, clarificationRequest('run-a', 'session-a', 1, 'req-a'))
    expect(sessionA.pendingClarifications).toHaveLength(1)
    expect(sessionB.pendingClarifications).toHaveLength(0)
  })

  it('restores pending clarifications from snapshot', () => {
    const store = useConversationRunsStore()
    const sessionA = store.ensureState('session-a')
    store.mergeSnapshot(sessionA, {
      run_id: 'run-a',
      session_id: 'session-a',
      status: 'running',
      agent_kind: 'device',
      pending_clarifications: [
        {
          request_id: 'req-a',
          run_id: 'run-a',
          session_id: 'session-a',
          questions: [{ question: 'pick?', options: [{ label: 'x' }, { label: 'y' }] }],
        },
      ],
    })
    expect(sessionA.pendingClarifications.map((c) => c.request_id)).toEqual(['req-a'])
  })

  it('submits clarification answers and removes the card on success', async () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, clarificationRequest('run-a', 'session-a', 1, 'req-1'))
    state.pendingClarifications[0].draftSelected[0] = ['nginx']
    vi.mocked(resolveChatClarification).mockResolvedValue({
      success: true,
      message: 'ok',
      request_id: 'req-1',
    })

    await store.submitClarification('session-a', 'req-1', { authToken: 'token' })

    expect(resolveChatClarification).toHaveBeenCalledWith(
      'req-1',
      {
        answers: [{ question_index: 0, selected_labels: ['nginx'], custom_text: null }],
        session_id: 'session-a',
        run_id: 'run-a',
      },
      'token',
    )
    expect(state.pendingClarifications).toEqual([])
  })

  it('blocks submit and records an error when a question is unanswered', async () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, clarificationRequest('run-a', 'session-a', 1, 'req-1'))

    await store.submitClarification('session-a', 'req-1', { authToken: 'token' })

    expect(resolveChatClarification).not.toHaveBeenCalled()
    expect(state.pendingClarifications[0].error).toBeTruthy()
  })

  it('preserves explicit confirmation constraints and the legacy custom default', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('package-session')
    store.applyEventToState(state, {
      event: 'agent_trace',
      type: 'clarification_request',
      task_id: 'package-run',
      run_id: 'package-run',
      session_id: 'package-session',
      seq: 1,
      timestamp: 1,
      request_id: 'package-confirmation',
      questions: [
        {
          question_key: 'project',
          question: 'Confirm project',
          allow_custom: false,
          required: true,
          options: [{ label: 'LX07A' }, { label: 'LX10' }],
        },
        { question: 'Legacy question', options: [{ label: 'Option' }] },
      ],
    })

    const questions = state.pendingClarifications[0].questions
    expect(questions[0]).toMatchObject({
      question_key: 'project',
      allow_custom: false,
      required: true,
    })
    expect(questions[1].allow_custom).toBeUndefined()
  })

  it('requires all thirteen component-mapping answers before submitting', async () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('package-session')
    const questions = Array.from({ length: 13 }, (_, index) => ({
      header: `file-${index + 1}`,
      question: `Map component-${index + 1}.bin`,
      options: [{ label: `component-${index + 1}` }, { label: 'exclude' }],
    }))
    store.applyEventToState(state, {
      event: 'agent_trace',
      type: 'clarification_request',
      task_id: 'package-run',
      run_id: 'package-run',
      session_id: 'package-session',
      seq: 1,
      timestamp: 1,
      request_id: 'package-confirmation',
      questions,
    })
    const pending = state.pendingClarifications[0]
    for (let index = 0; index < 12; index += 1) {
      pending.draftSelected[index] = [`component-${index + 1}`]
    }

    await store.submitClarification('package-session', 'package-confirmation')
    expect(resolveChatClarification).not.toHaveBeenCalled()
    expect(pending.error).toBeTruthy()

    pending.draftSelected[12] = ['exclude']
    vi.mocked(resolveChatClarification).mockResolvedValue({
      success: true,
      message: 'ok',
      request_id: 'package-confirmation',
    })
    await store.submitClarification('package-session', 'package-confirmation')

    const submitted = vi.mocked(resolveChatClarification).mock.calls[0][1].answers
    expect(submitted).toHaveLength(13)
    expect(submitted[12]).toEqual({
      question_index: 12,
      selected_labels: ['exclude'],
      custom_text: null,
    })
    expect(state.pendingClarifications).toEqual([])
  })

  it('drops pending clarifications when the run reaches a terminal state', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
    store.applyEventToState(state, clarificationRequest('run-a', 'session-a', 2, 'req-1'))
    expect(state.pendingClarifications).toHaveLength(1)
    store.applyEventToState(state, {
      event: 'cancelled',
      type: 'cancelled',
      run_id: 'run-a',
      session_id: 'session-a',
      seq: 3,
      timestamp: 3,
    })
    expect(state.pendingClarifications).toEqual([])
  })

  it('sends images in the device-run body when present and omits them otherwise', async () => {
    const store = useConversationRunsStore()

    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(sseResponse([
      { event: 'session', session_id: 'session-img', run_id: 'run-img' },
      { event: 'done', session_id: 'session-img', run_id: 'run-img', answer: 'ok' },
    ])))
    vi.stubGlobal('fetch', fetchMock)

    await store.startDeviceRun('session-img', {
      message: 'look at this',
      images: [{ media_type: 'image/png', data: 'data:image/png;base64,AAAA' }],
    })
    const withImagesBody = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect(withImagesBody.images).toHaveLength(1)
    expect(withImagesBody.images[0].media_type).toBe('image/png')

    fetchMock.mockClear()
    await store.startDeviceRun('session-noimg', { message: 'plain' })
    const noImagesBody = JSON.parse(fetchMock.mock.calls[0][1].body as string)
    expect(noImagesBody.images).toBeUndefined()
    expect(noImagesBody.agent_type).toBeUndefined()
  })

  it('propagates thirteen component files and lists them optimistically', async () => {
    const store = useConversationRunsStore()
    const files = Array.from({ length: 13 }, (_, index) =>
      new File([`payload-${index + 1}`], `component-${index + 1}.bin`),
    )
    vi.mocked(packageSearchStream).mockResolvedValue(sseResponse([
      {
        event: 'agent_trace',
        type: 'run_start',
        task_id: 'package-run',
        run_id: 'package-run',
        session_id: 'package-session',
        seq: 1,
        timestamp: 1,
      },
      {
        event: 'done',
        run_id: 'package-run',
        session_id: 'package-session',
        answer: 'built',
      },
    ]))

    const started = await store.startPackageSearchRun('package-session', {
      message: 'build package',
      project_repo_id: null,
      files,
    })

    expect(started).toBe(true)
    expect(packageSearchStream).toHaveBeenCalledWith(expect.objectContaining({
      projectRepoId: null,
      files,
    }))
    const userMessage = store.ensureState('package-session').messages.find(
      (message) => message.role === 'user',
    )?.content || ''
    for (const file of files) expect(userMessage).toContain(file.name)
  })

  it('signals a failed package upload so the composer can restore its file snapshot', async () => {
    const store = useConversationRunsStore()
    vi.spyOn(console, 'error').mockImplementation(() => undefined)
    vi.mocked(packageSearchStream).mockResolvedValue(new Response(
      JSON.stringify({ detail: 'upload rejected' }),
      { status: 413, headers: { 'Content-Type': 'application/json' } },
    ))

    const started = await store.startPackageSearchRun('failed-package-session', {
      message: 'build package',
      files: [new File(['payload'], 'component.bin')],
    })

    expect(started).toBe(false)
    expect(store.ensureState('failed-package-session').runStatus).toBe('failed')
    expect(store.ensureState('failed-package-session').messages.find((message) => message.role === 'ai')?.content)
      .toContain('upload rejected')
  })

  it('restores component files when an HTTP 200 package stream ends with an error', async () => {
    const store = useConversationRunsStore()
    const files = [
      new File(['component-a'], 'component-a.bin', { lastModified: 1 }),
      new File(['component-b'], 'component-b.zip', { lastModified: 2 }),
    ]
    vi.mocked(packageSearchStream).mockResolvedValue(sseResponse([
      {
        event: 'agent_trace',
        type: 'run_start',
        task_id: 'package-preprocess-run',
        run_id: 'package-preprocess-run',
        session_id: 'package-preprocess-session',
        seq: 1,
        timestamp: 1,
      },
      {
        event: 'error',
        run_id: 'package-preprocess-run',
        session_id: 'package-preprocess-session',
        message: 'component classification failed',
      },
    ]))

    // Mirrors AIChat's optimistic clear followed by failure recovery.
    let selectedPackageFiles: File[] = []
    const packageRunSucceeded = await store.startPackageSearchRun(
      'package-preprocess-session',
      { message: 'build package', files },
    )
    if (!packageRunSucceeded) {
      selectedPackageFiles = mergeUniquePackageFiles(selectedPackageFiles, files)
    }

    expect(packageRunSucceeded).toBe(false)
    expect(selectedPackageFiles).toEqual(files)
    expect(store.ensureState('package-preprocess-session').runStatus).toBe('failed')
  })

  it('reports a cancelled package stream as unsuccessful without changing Project Expert semantics', async () => {
    const store = useConversationRunsStore()
    vi.mocked(packageSearchStream).mockResolvedValue(sseResponse([
      {
        event: 'agent_trace',
        type: 'cancelled',
        task_id: 'cancelled-package-run',
        run_id: 'cancelled-package-run',
        session_id: 'cancelled-package-session',
        seq: 1,
        timestamp: 1,
      },
    ]))
    vi.mocked(projectExpertStream).mockResolvedValue(sseResponse([
      {
        event: 'error',
        run_id: 'project-run',
        session_id: 'project-session',
        message: 'project preprocessing failed',
      },
    ]))

    const packageSucceeded = await store.startPackageSearchRun('cancelled-package-session', {
      message: 'build package',
      files: [new File(['component'], 'component.bin')],
    })
    const projectStreamStarted = await store.startProjectExpertRun('project-session', {
      message: 'inspect project',
      project_repo_id: 1,
    })

    expect(packageSucceeded).toBe(false)
    expect(store.ensureState('cancelled-package-session').runStatus).toBe('cancelled')
    expect(projectStreamStarted).toBe(true)
    expect(store.ensureState('project-session').runStatus).toBe('failed')
  })

  it('records an OCR degradation hint on the assistant bubble', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
    store.applyEventToState(state, {
      event: 'ocr_status',
      status: 'unconfigured',
      image_count: 2,
      error_kind: null,
      run_id: 'run-a',
      session_id: 'session-a',
    })
    const answer = state.messages.find((m: ChatEntry) => m.id === 'run:run-a:assistant')
    expect(answer?.ocrStatus).toEqual({ status: 'unconfigured', imageCount: 2, errorKind: null })
  })

  it('records successful visual analysis on the assistant bubble', () => {
    const store = useConversationRunsStore()
    const state = store.ensureState('session-a')
    store.applyEventToState(state, traceEvent('run-a', 'session-a', 1, 'run_start'))
    store.applyEventToState(state, {
      event: 'ocr_result',
      status: 'succeeded',
      image_count: 2,
      text: '[图片 1]\nError: timeout',
      run_id: 'run-a',
      session_id: 'session-a',
    })
    const answer = state.messages.find((m: ChatEntry) => m.id === 'run:run-a:assistant')
    expect(answer?.visualAnalysis).toEqual({
      text: '[图片 1]\nError: timeout',
      imageCount: 2,
    })
  })
})
