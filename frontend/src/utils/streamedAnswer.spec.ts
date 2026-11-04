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
})
