import MarkdownIt from 'markdown-it'

export type AgentCompletionField = {
  label: string
  value: string
}

export type AgentCompletionSummary = {
  title: string
  fields: AgentCompletionField[]
  model?: string
  duration?: string
  packageBuildCompleted?: boolean
}

export type AgentMessageView = {
  displayMarkdown: string
  completionSummary: AgentCompletionSummary | null
}

export type VisualAnalysis = {
  text: string
  imageCount: number
}

const COMPLETION_LINE_RE =
  /^\*\*(.+?\bAgent)\*\*\s+(\u5df2\u5b8c\u6210(?:\u672c\u8f6e)?(?:\u5206\u6790|\u68c0\u7d22|\u6253\u5305|\u6574\u5305\u6253\u5305|\u6574\u5305\u6784\u5efa|\u5904\u7406)|completed (?:(?:this|the current) (?:analysis|search|packaging|package build|run)|(?:the )?full-package build))[\u3002.]*$/i
const LOG_ANALYSIS_AGENT_RE = /^(?:\u65e5\u5fd7\u5206\u6790|Log Analysis) Agent$/i
const SECTION_HEADING_RE = /^##\s+(.+?)\s*$/
const FIELD_RE = /^-\s+([^：:]+)[：:]\s*(.*)$/
const resultMarkdown = new MarkdownIt()
const HIDDEN_RESULT_SECTION_RE = /^(?:\u6458\u8981|\u5173\u952e\u8bcd|summary|keywords)$/i

/** Split the stored log-analysis preamble while preserving the full transcript Markdown. */
export function splitLogAnalysisResult(content: string) {
  const lines = content.replace(/\r\n/g, '\n').split('\n')
  const title = lines[0]?.trim().match(COMPLETION_LINE_RE)?.[1].trim()
  if (!title || !LOG_ANALYSIS_AGENT_RE.test(title)) return null

  // The question can contain Markdown headings. The formatter appends these
  // fields after the question, before any result sections.
  const contextIndex = lines.findIndex((line, index) =>
    /^- (?:\u4e0a\u4e0b\u6587|Context)[：:]/i.test(line) &&
    /^- (?:\u6a21\u578b|Model)[：:]/i.test(lines[index - 1] || '') &&
    /^- (?:\u72b6\u6001|Status)[：:]/i.test(lines[index - 2] || ''),
  )
  if (contextIndex === -1) return null

  const hasErrorType = /^- (?:\u9519\u8bef\u7c7b\u578b|Error type)[：:]/i.test(lines[contextIndex + 1] || '')
  const bodyStart = contextIndex + (hasErrorType ? 2 : 1)
  return {
    summaryMarkdown: lines.slice(0, bodyStart).join('\n'),
    bodyMarkdown: lines.slice(bodyStart).join('\n').trimStart(),
  }
}

/** Hide generated result sections without treating code examples as headings. */
function omitResultMetadataSections(content: string, isLogAnalysis: boolean): string {
  const lines = content.split('\n')
  const tokens = resultMarkdown.parse(content, {})
  const sections = tokens.flatMap((token, index) => {
    if (token.type !== 'heading_open' || token.level !== 0 || !token.map) return []
    if (token.tag !== 'h1' && token.tag !== 'h2') return []
    const heading = tokens[index + 1]?.content.trim() || ''
    return [{
      start: token.map[0],
      hidden: token.tag === 'h2' && (
        HIDDEN_RESULT_SECTION_RE.test(heading) || (isLogAnalysis && heading === '\u6839\u56e0\u5047\u8bbe')
      ),
    }]
  })
  const hiddenRanges = sections.flatMap((section, index) => section.hidden
    ? [{ start: section.start, end: sections[index + 1]?.start ?? lines.length }]
    : [])
  if (!hiddenRanges.length) return content
  return lines
    .filter((_, index) => !hiddenRanges.some(({ start, end }) => index >= start && index < end))
    .join('\n')
    .trim()
}

const cleanInlineMarkdown = (value: string) =>
  value
    .trim()
    .replace(/`([^`\n]+)`/g, '$1')
    .replace(/\*\*([^*\n]+)\*\*/g, '$1')
    .trim()

/**
 * Specialist-agent replies currently persist their run summary as a Markdown
 * preamble. Split that preamble from the answer so the UI can place it in the
 * trace without changing the stored conversation contract.
 */
export function parseAgentMessage(content: string): AgentMessageView {
  const source = content || ''
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const titleMatch = lines[0]?.trim().match(COMPLETION_LINE_RE)
  if (!titleMatch) {
    return { displayMarkdown: source, completionSummary: null }
  }

  const answerSectionIndex = lines.findIndex((line, index) => {
    if (index === 0) return false
    const sectionMatch = line.trim().match(SECTION_HEADING_RE)
    return !!sectionMatch && /^(?:\u56de\u7b54|answer)$/i.test(sectionMatch[1].trim())
  })
  const firstSectionIndex =
    answerSectionIndex !== -1
      ? answerSectionIndex
      : lines.findIndex((line, index) => index > 0 && SECTION_HEADING_RE.test(line.trim()))
  const preambleEnd = firstSectionIndex === -1 ? lines.length : firstSectionIndex
  const fields: AgentCompletionField[] = []

  for (const rawLine of lines.slice(1, preambleEnd)) {
    const line = rawLine.trim()
    if (!line) continue
    const fieldMatch = line.match(FIELD_RE)
    if (fieldMatch) {
      fields.push({
        label: fieldMatch[1].trim(),
        value: cleanInlineMarkdown(fieldMatch[2]),
      })
    } else if (fields.length > 0) {
      fields[fields.length - 1].value = `${fields[fields.length - 1].value}\n${line}`.trim()
    }
  }

  const modelField = fields.find((field) => /^(?:\u6a21\u578b|model)$/i.test(field.label))
  const statusField = fields.find((field) => /^(?:\u72b6\u6001|status)$/i.test(field.label))
  // Read the persisted completion preamble, not mentions of packaging in the answer.
  const packageBuildCompleted =
    /^(?:\u914d\u7f6e\u7ba1\u7406\u5458|Configuration Manager)\s+Agent$/i.test(titleMatch[1].trim()) &&
    /(?:\u6253\u5305|\u6574\u5305\u6784\u5efa|packaging|package build|full-package build)/i.test(titleMatch[2]) &&
    (!statusField || /^(?:ok|success|succeeded|built|completed)$/i.test(statusField.value))
  let model: string | undefined
  let duration: string | undefined
  if (modelField) {
    const metaMatch = modelField.value.match(
      /^([\s\S]*?)(?:[\uff0c,]\s*(?:\u8017\u65f6|duration)\s*[\uff1a:]\s*(.+))$/i,
    )
    model = cleanInlineMarkdown(metaMatch?.[1] || modelField.value)
    duration = metaMatch?.[2]?.trim()
  }

  let answerStart = firstSectionIndex
  if (firstSectionIndex !== -1) {
    const sectionMatch = lines[firstSectionIndex].trim().match(SECTION_HEADING_RE)
    if (sectionMatch && /^(?:\u56de\u7b54|answer)$/i.test(sectionMatch[1].trim())) {
      answerStart += 1
    }
  }
  const displayMarkdown =
    answerStart === -1 ? '' : lines.slice(answerStart).join('\n').trim()
  const title = titleMatch[1].trim()

  return {
    displayMarkdown: omitResultMetadataSections(displayMarkdown, LOG_ANALYSIS_AGENT_RE.test(title)),
    completionSummary: {
      title,
      fields,
      model,
      duration,
      packageBuildCompleted,
    },
  }
}

/** Recover persisted OCR/vision text from a historical user message. */
export function extractVisualAnalysis(content: string): VisualAnalysis | null {
  const match = (content || '').match(
    /<user_image_ocr\b([^>]*)>([\s\S]*?)<\/user_image_ocr>/i,
  )
  const text = match?.[2]?.trim()
  if (!match || !text) return null
  const countMatch = match[1].match(/\bimage_count=["']?(\d+)/i)
  return {
    text,
    imageCount: Number(countMatch?.[1] || 0),
  }
}
