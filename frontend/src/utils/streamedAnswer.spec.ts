import { describe, expect, it } from 'vitest'
import { projectStreamedAnswer } from './streamedAnswer'

describe('readable streamed answers', () => {
  it('shows the answer before the JSON string or object closes, hiding metadata', () => {
    const raw = '```json\n{"status":"ok","nested":{"answer":"wrong"},"answer":"根因是失锁。\\n建议重试。","summary":"hidden"}\n```'
    const observed = Array.from({ length: raw.length }, (_, index) => projectStreamedAnswer(raw.slice(0, index)))
    expect(observed).toContain('根因')
    expect(observed).toContain('根因是失锁。\n建议重试。')
    expect(observed.every((text) => !text.includes('status') && !text.includes('wrong') && !text.includes('hidden'))).toBe(true)
    expect(projectStreamedAnswer(raw)).toBe('根因是失锁。\n建议重试。')
  })

  it('decodes split escapes without showing unfinished Unicode or surrogate pairs', () => {
    const prefix = '{"answer":"引用：\\"ok\\"\\n'
    expect(projectStreamedAnswer(prefix + '\\u4F')).toBe('引用："ok"\n')
    expect(projectStreamedAnswer(prefix + '\\u4F60')).toBe('引用："ok"\n你')
    expect(projectStreamedAnswer(prefix + '\\uD83D')).toBe('引用："ok"\n')
    expect(projectStreamedAnswer(prefix + '\\uD83D\\uDE80')).toBe('引用："ok"\n🚀')
  })

  it('preserves ordinary Markdown including JSON examples', () => {
    const markdown = '示例：\n```json\n{"answer":"example"}\n```\n**完成**'
    expect(projectStreamedAnswer(markdown)).toBe(markdown)
  })

  it.each(['```json\n', '```\n', ''])('hides a result after progress text with a %j wrapper at every chunk boundary', (fence) => {
    const preamble = '项目已存在，直接复用。调查完成，以下是画图解释：\n'
    const answer = '1PPS 秒脉冲和 TOD 时间。\n```mermaid\nflowchart LR\n  A --> B\n```'
    const raw = preamble + fence + JSON.stringify({
      status: 'ok', question_type: 'qa', nested: { answer: 'wrong' }, answer, summary: 'hidden',
    }) + (fence ? '\n```' : '')
    const observed = Array.from({ length: raw.length + 1 }, (_, index) => projectStreamedAnswer(raw.slice(0, index)))
    for (const text of observed) {
      expect(text).not.toMatch(/"(?:status|question_type|answer|summary)"|wrong|hidden/)
      expect(text).not.toContain('```json')
    }
    expect(observed).toContain('1PPS 秒脉冲')
    expect(projectStreamedAnswer(raw)).toBe(answer)
  })

  it('finds the result after an ordinary code example without selecting its nested answer', () => {
    const example = '配置示例：\n```json\n{"payload":{"answer":"example"}}\n```\n'
    const raw = example + '调查完成。```json\n{"status":"ok","answer":"真正的回答'
    expect(projectStreamedAnswer(raw)).toBe('真正的回答')
  })

  it('keeps JSON examples inside the decoded answer, including result-shaped examples', () => {
    const answer = '协议示例：\n```json\n{"status":"ok","answer":"example"}\n```'
    expect(projectStreamedAnswer('完成。\n```json\n' + JSON.stringify({ status: 'ok', answer }))).toBe(answer)
  })

  it.each([
    '示例：\n```json\n{"status":"ok","count":2}\n```\n完成',
    '示例：\n```json\n{"answer":"example"}\n```\n完成',
    '示例：\n```js\nconst value = {"status":"ok","answer":"example"}\n```',
    '字段 `{"status":"ok","answer":"example"}` 是一个示例。',
  ])('preserves ordinary prose and code: %s', (markdown) => {
    expect(projectStreamedAnswer(markdown)).toBe(markdown)
  })
})
