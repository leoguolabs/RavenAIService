/** Read a JSON string even when its closing quote has not arrived yet. */
function readString(source: string, start: number) {
  let text = ''
  let index = start + 1
  for (; index < source.length; index += 1) {
    const char = source[index]
    if (char === '"') return { text, end: index + 1, complete: true }
    if (char !== '\\') {
      text += char
      continue
    }
    const escape = source[++index]
    if (!escape) break
    if (escape === 'u') {
      const hex = source.slice(index + 1, index + 5)
      if (!/^[\da-f]{4}$/i.test(hex)) break
      text += String.fromCharCode(parseInt(hex, 16))
      index += 4
    } else {
      const escapes: Record<string, string> = {
        '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t',
      }
      if (!(escape in escapes)) break
      text += escapes[escape]
    }
  }
  // Do not expose half of a surrogate pair when a Unicode escape spans chunks.
  return { text: text.replace(/[\uD800-\uDBFF]$/, ''), end: index, complete: false }
}

/**
 * Specialist agents stream fenced JSON results, while general chat streams
 * Markdown. Project only the top-level answer string, before JSON is complete.
 */
export function projectStreamedAnswer(raw: string): string {
  let source = raw.trimStart()
  const fence = source.match(/^```json\s*/i)
  if (fence) source = source.slice(fence[0].length).trimStart()
  else if (/^`{1,3}(?:j(?:s(?:o(?:n)?)?)?)?\s*$/i.test(source)) return ''
  else if (source.startsWith('```')) {
    // A bare JSON fence is allowed by the result parser too.
    const body = source.slice(3).trimStart()
    if (body.startsWith('{')) source = body
  }
  if (!source.startsWith('{')) return fence ? '' : raw.trimStart()

  let depth = 0
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index]
    if (char === '{' || char === '[') depth += 1
    else if (char === '}' || char === ']') depth -= 1
    else if (char === '"') {
      const key = readString(source, index)
      if (!key.complete) return ''
      let valueStart = key.end
      while (/\s/.test(source[valueStart] || '') && valueStart < source.length) valueStart += 1
      if (depth === 1 && key.text === 'answer' && source[valueStart] === ':') {
        valueStart += 1
        while (/\s/.test(source[valueStart] || '') && valueStart < source.length) valueStart += 1
        return source[valueStart] === '"' ? readString(source, valueStart).text.trimStart() : ''
      }
      index = key.end - 1
    }
  }
  return ''
}
