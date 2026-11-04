import { afterEach, describe, expect, it, vi } from 'vitest'
import { effectScope, ref } from 'vue'
import { useProgressiveText } from './useProgressiveText'

describe('progressive answer presentation', () => {
  afterEach(() => vi.useRealTimers())

  it('reveals a coarse live chunk before completion and finishes appended terminal text', () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const source = ref('')
    const view = scope.run(() => useProgressiveText(source, ref(true)))!
    source.value = '根据日志分析，设备重启导致连接中断。'
    expect(view.displayed.value).toBe('')
    vi.advanceTimersByTime(48)
    expect(source.value.startsWith(view.displayed.value)).toBe(true)
    expect(view.displayed.value.length).toBeGreaterThan(0)
    expect(view.displayed.value.length).toBeLessThan(source.value.length)
    source.value += '\n建议检查供电。'
    vi.runAllTimers()
    expect(view.displayed.value).toBe(source.value)
    expect(view.pending.value).toBe(false)
    scope.stop()
  })

  it('does not starve display under frequent deltas, and never splits emoji', () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const source = ref('')
    const view = scope.run(() => useProgressiveText(source, ref(true)))!
    for (let index = 0; index < 10; index += 1) {
      source.value += '🚀'
      vi.advanceTimersByTime(5)
    }
    expect(view.displayed.value.length).toBeGreaterThan(0)
    expect(Array.from(view.displayed.value).every((char) => char === '🚀')).toBe(true)
    scope.stop()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('shows historical/reduced-motion text immediately and applies authoritative corrections', () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const source = ref('历史回答')
    const animate = ref(false)
    const view = scope.run(() => useProgressiveText(source, animate))!
    expect(view.displayed.value).toBe(source.value)
    animate.value = true
    source.value += '，新后缀'
    vi.advanceTimersByTime(16)
    source.value = '脱敏后的正确全文'
    expect(view.displayed.value).toBe(source.value)
    source.value += '，后续内容'
    animate.value = false
    expect(view.displayed.value).toBe(source.value)
    scope.stop()
    expect(vi.getTimerCount()).toBe(0)
  })
})
