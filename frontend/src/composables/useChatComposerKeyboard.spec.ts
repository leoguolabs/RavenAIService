import { describe, expect, it, vi } from 'vitest'
import { useChatComposerKeyboard } from './useChatComposerKeyboard'

function keydown(overrides: Partial<KeyboardEvent> = {}) {
  return {
    key: 'Enter',
    keyCode: 13,
    shiftKey: false,
    isComposing: false,
    preventDefault: vi.fn(),
    ...overrides,
  } as unknown as KeyboardEvent
}

describe('chat composer keyboard', () => {
  it('sends once on ordinary Enter and prevents a newline', () => {
    const send = vi.fn()
    const keyboard = useChatComposerKeyboard(send)
    const event = keydown()
    keyboard.handleKeydown(event)
    expect(send).toHaveBeenCalledTimes(1)
    expect(event.preventDefault).toHaveBeenCalledOnce()
  })

  it.each([
    { shiftKey: true },
    { key: 'a', keyCode: 65 },
    { isComposing: true },
    { keyCode: 229 },
  ])('preserves native input without sending for %j', (options) => {
    const send = vi.fn()
    const event = keydown(options)
    useChatComposerKeyboard(send).handleKeydown(event)
    expect(send).not.toHaveBeenCalled()
    expect(event.preventDefault).not.toHaveBeenCalled()
  })

  it('does not send during composition even if the native flags are missing', () => {
    const send = vi.fn()
    const keyboard = useChatComposerKeyboard(send)
    keyboard.handleCompositionStart()
    // Candidate confirmation can lack the native isComposing flag.
    const confirm = keydown()
    keyboard.handleKeydown(confirm)
    expect(send).not.toHaveBeenCalled()
    expect(confirm.preventDefault).not.toHaveBeenCalled()

    keyboard.handleCompositionEnd()
    keyboard.handleKeydown(keydown())
    expect(send).toHaveBeenCalledTimes(1)
  })

  it('ignores confirming Enter delivered after compositionend, then allows the next Enter', () => {
    const send = vi.fn()
    const keyboard = useChatComposerKeyboard(send)
    keyboard.handleCompositionStart()
    keyboard.handleCompositionEnd()
    const confirm = keydown({ keyCode: 229 })
    keyboard.handleKeydown(confirm)
    expect(send).not.toHaveBeenCalled()
    expect(confirm.preventDefault).not.toHaveBeenCalled()
    keyboard.handleKeydown(keydown())
    expect(send).toHaveBeenCalledTimes(1)
  })

  it('does not carry composition state between separate composers', () => {
    const firstSend = vi.fn()
    const secondSend = vi.fn()
    const first = useChatComposerKeyboard(firstSend)
    const second = useChatComposerKeyboard(secondSend)
    first.handleCompositionStart()
    first.handleKeydown(keydown())
    second.handleKeydown(keydown())
    expect(firstSend).not.toHaveBeenCalled()
    expect(secondSend).toHaveBeenCalledOnce()
  })
})
