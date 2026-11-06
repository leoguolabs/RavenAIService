import assert from 'node:assert/strict'

// Run with ego-browser nodejs after navigating to a fresh, authenticated local
// RavenAI chat. Pass its Page and API base URL to verifyChatComposerIme().
// Uses real browser composition, real HTTP/SSE, and persisted conversation data;
// only the compatibility event-order cases use synthetic DOM events.
export async function verifyChatComposerIme(page, apiBaseUrl) {
  const input = 'textarea.rw-textarea'
  const marker = `IME_E2E_${Date.now()}`
  const prompt = `在 RavenAI 中如何新建对话？请用一句纯文本回答。键盘测试标记：${marker}。候选词：`
  const checks = []

  await page.waitForSelector(`${input}:not([readonly])`)
  await page.evaluate(() => {
    window.__imeE2E = { requests: [], keys: [] }
    const originalFetch = window.fetch
    window.__imeE2E.originalFetch = originalFetch
    window.fetch = async (...args) => {
      const [url, init] = args
      let record
      if (String(url).includes('/chat/stream') && init?.body) {
        const payload = JSON.parse(init.body)
        if (payload.message) {
          record = { message: payload.message, sessionId: payload.session_id }
          window.__imeE2E.requests.push(record)
        }
      }
      const response = await originalFetch(...args)
      if (record) record.status = response.status
      return response
    }
    const onKeydown = (event) => window.__imeE2E.keys.push({
      key: event.key, isTrusted: event.isTrusted,
      isComposing: event.isComposing, prevented: event.defaultPrevented,
    })
    window.__imeE2E.onKeydown = onKeydown
    document.querySelector('textarea.rw-textarea').addEventListener('keydown', onKeydown)
  })

  const assertDraft = async (expected, label) => {
    const state = await page.evaluate(() => ({
      draft: document.querySelector('textarea.rw-textarea').value,
      requests: window.__imeE2E.requests.length,
    }))
    assert.equal(state.draft, expected, `${label}: draft must be preserved`)
    assert.equal(state.requests, 0, `${label}: no message may be submitted`)
    checks.push(label)
  }

  try {
    for (const candidate of ['hello', '中文']) {
      await page.fill(input, prompt)
      await page.cdp('Input.imeSetComposition', {
        text: candidate, selectionStart: candidate.length, selectionEnd: candidate.length,
      })
      await page.cdp('Input.dispatchKeyEvent', {
        type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 229,
      })
      await page.cdp('Input.insertText', { text: candidate })
      await page.cdp('Input.dispatchKeyEvent', {
        type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13,
      })
      const key = await page.evaluate(() => window.__imeE2E.keys.at(-1))
      assert.equal(key.isTrusted, true)
      assert.equal(key.isComposing, true)
      assert.equal(key.prevented, false, 'IME must retain its native Enter action')
      await assertDraft(prompt + candidate, `native composition: ${candidate}`)
    }

    for (const mode of ['native-flag-only', 'composition-state-only', 'compositionend-before-enter']) {
      await page.fill(input, prompt + 'hello')
      const prevented = await page.evaluate((mode) => {
        const input = document.querySelector('textarea.rw-textarea')
        if (mode !== 'native-flag-only') {
          input.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }))
        }
        if (mode === 'compositionend-before-enter') {
          input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: 'hello' }))
        }
        const enter = new KeyboardEvent('keydown', {
          bubbles: true, cancelable: true, key: 'Enter', code: 'Enter',
          isComposing: mode === 'native-flag-only',
          keyCode: mode === 'compositionend-before-enter' ? 229 : 13,
        })
        input.dispatchEvent(enter)
        if (mode === 'composition-state-only') {
          input.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: 'hello' }))
        }
        return enter.defaultPrevented
      }, mode)
      assert.equal(prevented, false, `${mode}: IME default action must be preserved`)
      await assertDraft(prompt + 'hello', mode)
    }

    await page.press(input, 'Shift+Enter')
    await assertDraft(prompt + 'hello\n', 'Shift+Enter inserts a newline')
    await page.press(input, 'Enter')
    await page.waitForFunction(() => window.__imeE2E.requests.some((r) => r.status === 200), undefined, { timeout: 30000 })
    await page.waitForFunction(() =>
      !!document.querySelector('.rw-ai-actions') &&
      !!document.querySelector('.rw-ai-text[aria-busy="false"]')?.textContent.trim(),
    undefined, { timeout: 60000 })

    const sent = await page.evaluate(() => ({
      requests: window.__imeE2E.requests,
      draft: document.querySelector('textarea.rw-textarea').value,
      answer: document.querySelector('.rw-ai-text').textContent.trim(),
    }))
    assert.equal(sent.requests.length, 1, 'ordinary Enter must send exactly once')
    assert.equal(sent.requests[0].message, prompt + 'hello', 'candidate must be in the submitted text')
    assert.equal(sent.draft, '')
    checks.push('ordinary Enter sends once and renders the real streamed answer')

    const persisted = await page.evaluate(async ({ apiBaseUrl, sessionId }) => {
      const response = await fetch(`${apiBaseUrl}/api/v1/users/chat-sessions/${sessionId}/messages`, {
        headers: { Authorization: `Bearer ${localStorage.getItem('raven_user_token')}` },
      })
      return { status: response.status, body: await response.json() }
    }, { apiBaseUrl, sessionId: sent.requests[0].sessionId })
    assert.equal(persisted.status, 200)
    const messages = persisted.body.data
    assert.ok(Array.isArray(messages), 'history response must contain messages')
    assert.equal(messages.filter((m) => m.role === 'user').length, 1)
    assert.equal(messages.find((m) => m.role === 'user').content, prompt + 'hello')
    checks.push('backend history contains exactly one user message with the committed candidate')
    return { checks, sessionId: sent.requests[0].sessionId, marker, answer: sent.answer }
  } finally {
    await page.evaluate(() => {
      window.fetch = window.__imeE2E.originalFetch
      document.querySelector('textarea.rw-textarea').removeEventListener('keydown', window.__imeE2E.onKeydown)
      delete window.__imeE2E
    })
  }
}
