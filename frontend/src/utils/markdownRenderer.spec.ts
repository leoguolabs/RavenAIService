import { describe, expect, it } from 'vitest'

import {
  extractPlainText,
  renderAnnouncementMarkdown,
  renderMarkdown,
} from '@/utils/markdownRenderer'

/** 从渲染结果中取出 data-table-md 属性并解码 HTML 实体 */
function extractTableSource(html: string): string | null {
  const match = html.match(/data-table-md="([^"]*)"/)
  if (!match) return null
  return match[1]
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&amp;/g, '&')
}

describe('markdown table copy button', () => {
  it('wraps tables in a .table-block with a copy button and the original markdown source', () => {
    const table = '| a | b |\n| - | - |\n| 1 | 2 |'
    const html = renderMarkdown(table)

    expect(html).toContain('table-block')
    expect(html).toContain('table-copy-btn')
    expect(html).toContain('markdown-table')
    expect(extractTableSource(html)).toBe(table)
  })

  it('slices the correct source when the table is not at the top of the content', () => {
    const table = '| col1 | col2 |\n| ---- | ---- |\n| **x** | [l](https://e.com) |'
    const content = `# 标题\n\n一段说明文字。\n\n${table}\n\n结尾段落。`
    const html = renderMarkdown(content)

    expect(extractTableSource(html)).toBe(table)
  })

  it('keeps per-table sources distinct when multiple tables exist', () => {
    const t1 = '| a |\n| - |\n| 1 |'
    const t2 = '| b |\n| - |\n| 2 |'
    const html = renderMarkdown(`${t1}\n\n中间文字\n\n${t2}`)

    const sources = [...html.matchAll(/data-table-md="([^"]*)"/g)].map((m) => m[1])
    expect(sources).toHaveLength(2)
    expect(sources[0]).toContain('| a |')
    expect(sources[1]).toContain('| b |')
  })

  it('does not leak the copy-button label into extractPlainText output', () => {
    const text = extractPlainText('| a | b |\n| - | - |\n| 1 | 2 |')
    expect(text).not.toContain('Markdown')
  })
})

describe('markdown code copy button', () => {
  it('adds one control per code block and leaves inline code and Mermaid alone', () => {
    const content = [
      'Use `inline` code:',
      '',
      '```sh',
      '  echo "one & two"',
      '```',
      '',
      '```',
      'plain code',
      '```',
      '',
      'Indented example:',
      '',
      '    indented code',
      '',
      '```mermaid',
      'graph TD;A-->B;',
      '```',
    ].join('\n')
    const html = renderMarkdown(content, { copyCode: true })

    expect(html.match(/class="code-copy-btn"/g)).toHaveLength(3)
    expect(html).toContain('<code>inline</code>')
    expect(html).toContain('<pre class="hljs language-sh">')
    expect(html).toContain('<pre><code>indented code\n</code></pre>')
    expect(html).toContain('class="mermaid-container"')
  })

  it('keeps controls out of default and announcement rendering', () => {
    const content = '```sh\nfree -h\n```'

    expect(renderMarkdown(content)).not.toContain('code-copy-btn')
    expect(renderAnnouncementMarkdown(content)).not.toContain('code-copy-btn')
  })
})

describe('announcement markdown renderer', () => {
  it('renders common Markdown and preserves operational line breaks', () => {
    const html = renderAnnouncementMarkdown('**Maintenance**\nStarts at 22:00\n\n- Save your work')

    expect(html).toContain('<strong>Maintenance</strong><br>')
    expect(html).toContain('<li>Save your work</li>')
    expect(html).toContain('announcement-markdown')
  })

  it('emits semantic markup for lists, nested lists, headings, quotes, code, and tables', () => {
    const html = renderAnnouncementMarkdown([
      '#### Details',
      '',
      '- First',
      '  - Nested',
      '',
      '1. One',
      '2. Two',
      '',
      '> Note',
      '',
      '`inline`',
      '',
      '| Key | Value |',
      '| --- | --- |',
      '| Mode | Safe |',
    ].join('\n'))

    expect(html).toContain('<h4>Details</h4>')
    expect(html).toMatch(/<ul>[\s\S]*<ul>[\s\S]*Nested[\s\S]*<\/ul>[\s\S]*<\/ul>/)
    expect(html).toContain('<ol>')
    expect(html).toContain('<blockquote>')
    expect(html).toContain('<code>inline</code>')
    expect(html).toContain('<table class="markdown-table">')
  })

  it('escapes raw HTML and does not create executable Mermaid containers', () => {
    const html = renderAnnouncementMarkdown(
      '<img src=x onerror="alert(1)">\n\n```mermaid\ngraph TD;A-->B;\n```',
    )

    expect(html).not.toContain('<img')
    expect(html).not.toContain('mermaid-container')
    expect(html).toContain('&lt;img src=x onerror=')
    expect(html).toContain('graph TD;A--&gt;B;')
  })

  it('adds safe external-link attributes', () => {
    const html = renderAnnouncementMarkdown('[Status page](https://status.example.com)')

    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
  })

  it('does not emit unsafe link protocols', () => {
    const html = renderAnnouncementMarkdown('[Open](javascript:alert(1))')

    expect(html).not.toContain('href=')
    expect(html).toContain('Open')
  })
})

describe('conversation package download links', () => {
  it('renders the existing Raven download route as a clickable link', () => {
    const html = renderMarkdown(
      '[下载整包](/raven/api/download/package-123)',
    )

    expect(html).toContain('href="/raven/api/download/package-123"')
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer"')
    expect(html).toContain('下载整包')
  })
})
