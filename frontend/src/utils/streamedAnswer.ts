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

/** Read only outer object fields; braces and field names in strings are data. */
function readObject(source: string, start: number) {
  const fields = new Set<string>()
  let answer: string | undefined
  let depth = 0
  let index = start
  for (; index < source.length; index += 1) {
    const char = source[index]
    if (char === '{' || char === '[') depth += 1
    else if (char === '}' || char === ']') {
      depth -= 1
      if (depth === 0) return { fields, answer, end: index + 1, complete: true }
    } else if (char === '"') {
      const key = readString(source, index)
      if (!key.complete) break
      let valueStart = key.end
      while (valueStart < source.length && /\s/.test(source[valueStart])) valueStart += 1
      if (depth === 1 && source[valueStart] === ':') {
        fields.add(key.text)
        if (key.text === 'answer') {
          valueStart += 1
          while (valueStart < source.length && /\s/.test(source[valueStart])) valueStart += 1
          if (source[valueStart] === '"') {
            const value = readString(source, valueStart)
            answer = value.text.trimStart()
            if (!value.complete) break
            index = value.end - 1
            continue
          }
        }
      }
      index = key.end - 1
    }
  }
  return { fields, answer, end: source.length, complete: false }
}

const RESULT_METADATA = ['status', 'question_type', 'summary', 'error_kind', 'root_cause_hypotheses', 'recommended_actions']

/**
 * Model progress text may precede the final JSON, even in the same text block.
 * Buffer ambiguous JSON prefixes so their shell never flashes on screen, then
 * project the outer answer. Ordinary code examples remain Markdown; decoded
 * answer text is returned verbatim, without recursively interpreting examples.
 */
export function projectStreamedAnswer(raw: string, allowPrefacedResult = true): string {
  const source = raw.trimStart()
  for (let index = 0; index < source.length; index += 1) {
    // Plain-text agents may intentionally explain the specialist result schema.
    // Only structured-result agents opt into interpreting objects after prose.
    if (index > 0 && !allowPrefacedResult) return source
    const candidateStart = index
    let fence: string | undefined
    if (source[index] === '`') {
      const rest = source.slice(index)
      // A fence/language token can be split at any character boundary.
      if (/^`{1,3}(?:j(?:s(?:o(?:n)?)?)?)?\s*$/i.test(rest)) {
        return source.slice(0, candidateStart).trimEnd()
      }
      fence = rest.match(/^`+/)![0]
      if (fence.length >= 3) {
        const opening = rest.match(/^`{3,}(?:json\b)?\s*/i)![0]
        index += opening.length
        if (source[index] !== '{') {
          const end = source.indexOf(fence, index)
          if (end < 0) return source
          index = end + fence.length - 1
          continue
        }
      } else {
        // Never inspect JSON inside an inline code span.
        const end = source.indexOf(fence, index + fence.length)
        if (end < 0) return source
        index = end + fence.length - 1
        continue
      }
    }
    if (source[index] !== '{') continue
    // Ordinary prose such as {name} is not a JSON object.
    const first = source.slice(index + 1).trimStart()[0]
    if (first && first !== '"' && first !== '}') continue

    const object = readObject(source, index)
    const hasAnswer = object.fields.has('answer')
    const hasMetadata = RESULT_METADATA.some((field) => object.fields.has(field))
    // An answer-only example following prose is ambiguous, so require result
    // metadata there. Keep supporting the original answer-only root envelope.
    if (hasAnswer && (candidateStart === 0 || hasMetadata)) return object.answer || ''
    if (!object.complete) return source.slice(0, candidateStart).trimEnd()

    index = object.end - 1
    if (fence) {
      const end = source.indexOf(fence, object.end)
      if (end < 0) return source
      index = end + fence.length - 1
    }
  }
  return source
}
