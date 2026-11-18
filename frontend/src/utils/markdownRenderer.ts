import { i18n } from '@/i18n'
/**
 * Professional Markdown Renderer using markdown-it + highlight.js
 * 
 * Features:
 * - Code syntax highlighting
 * - Tables support
 * - XML/metadata cleanup
 * - Safe HTML rendering
 * - Tailwind CSS styling
 */

import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js'
import { loadMermaid } from './mermaidLoader'

/**
 * 模块级自增计数器，用于 mermaid.render() 的临时 DOM id。
 * 与容器 id 解耦，保证即使两个图表源码相同（容器 id 一致）也不会发生
 * 渲染时 id 冲突。
 */
let mermaidRenderSeq = 0

/**
 * 根据内容生成稳定的短哈希（djb2）。
 *
 * Mermaid 容器 id 必须由源码内容**确定性**派生，而非自增计数器：
 * `renderMarkdown` 会在组件每次重渲染时被 v-html 调用，若 id 每次不同，
 * 输出的 HTML 字符串就会变化，触发 Vue 重新 patch innerHTML，抹掉
 * `processMermaidBlocks()` 异步插入的 SVG（表现为图表一直“渲染中…”）。
 * 相同内容产出相同 id ⇒ 相同 HTML ⇒ Vue 跳过 patch ⇒ SVG 得以保留。
 */
function hashString(value: string): string {
  let hash = 5381
  for (let i = 0; i < value.length; i++) {
    hash = ((hash << 5) + hash + value.charCodeAt(i)) | 0
  }
  return (hash >>> 0).toString(36)
}

const escapeHtml = (value: string): string =>
  value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')

/**
 * Mermaid 占位容器的内部结构（loading 指示器 + 源码降级块）。
 *
 * 渲染期与「主题切换后重置」共用同一份结构，保证 `refreshMermaidBlocks()`
 * 复原出的 DOM 与首次渲染完全一致。
 *
 * @param escapedSource - 已做 HTML 转义的 Mermaid 源码
 */
function buildMermaidPlaceholder(escapedSource: string): string {
  return (
    `<div class="mermaid-loading">${i18n.global.t('markdown.mermaidLoading')}</div>` +
    `<pre class="hljs mermaid-source language-mermaid"><code>${escapedSource}</code></pre>`
  )
}

/**
 * XML标签清理配置
 */
const XML_CLEANUP_PATTERNS = [
  // 移除log_metadata标签及其内容
  /<log_metadata[^>]*>.*?<\/log_metadata>/gs,
  // 移除log_package标签及其内容
  /<log_package[^>]*>.*?<\/log_package>/gs,
  // 移除file_list标签及其内容
  /<file_list[^>]*>.*?<\/file_list>/gs,
  // 移除file标签及其内容
  /<file[^>]*>.*?<\/file>/gs,
  // 移除extraction标签（单个词）
  /^extraction\s*/gm,
  // 移除document标签及其属性
  /<document[^>]*>.*?<\/document>/gs,
  /<document[^>]*>/g,
  /<\/document>/g,
  // 移除meta标签及其内容
  /<meta[^>]*>.*?<\/meta>/gs,
  /<meta[^>]*>/g,
  /<\/meta>/g,
  // 移除type标签及其内容
  /<type[^>]*>.*?<\/type>/gs,
  /<type[^>]*>/g,
  /<\/type>/g,
  // 移除其他XML标签（但保留context_summary的内容）
  /<context_summary>(.*?)<\/context_summary>/gs,  // 这个会被后面的规则处理，只移除标签本身
  /<reads[^>]*>.*?<\/reads>/gs,
  /<reads[^>]*>/g,
  /<\/reads>/g,
  /<source[^>]*>.*?<\/source>/gs,
  // 移除孤立的XML标签
  /<[^>]+type="[^"]*"[^>]*>/g,
  /<[^>]+source="[^"]*"[^>]*>/g,
  /<[^>]+path="[^"]*"[^>]*>/g,
  // 移除任何剩余的XML标签（但保留内容）
  /<\/?[a-zA-Z_][^>]*>/g,
  // 移除看起来像元数据的行
  /^\d+$/gm,
  /^(true|false)$/gm,
  /^\d{4}-\d{2}-\d{2}T[\d:\.]+Z$/gm,
]

/**
 * 从顶层 ```markdown / ```md 包裹块中提取内容。
 * 规则：
 * 1. 仅当内容整体被 markdown 代码块包裹时才提取；
 * 2. 使用最后一个行首 ``` 作为外层结束标记，避免内层代码块被误截断。
 */
function extractMarkdownBlock(content: string): string | null {
  if (!content || typeof content !== 'string') return null

  const normalized = content.replace(/\r\n/g, '\n')
  const trimmed = normalized.trim()
  if (!trimmed) return null

  const openMatch = trimmed.match(/^```(?:markdown|md)\s*\n/i)
  if (!openMatch) return null

  const bodyStart = openMatch[0].length

  // 必须是行首 ``` 且后面只允许空白，取最后一个作为外层关闭标记
  const closeRegex = /\n```[ \t]*$/gm
  let closeStart = -1
  let m: RegExpExecArray | null
  while ((m = closeRegex.exec(trimmed)) !== null) {
    closeStart = m.index
  }

  if (closeStart < bodyStart) {
    // 没有合法结束标记时，返回起始后的内容，尽量容错
    return trimmed.slice(bodyStart).trim()
  }

  return trimmed.slice(bodyStart, closeStart).trim()
}

/**
 * 清理内容中的XML标签和元数据
 */
function cleanXmlAndMetadata(content: string): string {
  if (!content || typeof content !== 'string') {
    return ''
  }

  // 首先尝试从```markdown块中提取
  const extracted = extractMarkdownBlock(content)
  if (extracted !== null) {
    return extracted
  }

  // 特殊处理：先提取context_summary的内容（保留内容，只删除标签）
  let cleaned = content
  cleaned = cleaned.replace(/<context_summary>(.*?)<\/context_summary>/gs, '$1')
  
  // 然后应用其他清理规则（跳过context_summary规则）
  for (const pattern of XML_CLEANUP_PATTERNS) {
    // 跳过context_summary的pattern（已经在上面处理过了）
    if (pattern.source && pattern.source.includes('context_summary')) {
      continue
    }
    cleaned = cleaned.replace(pattern, '')
  }

  // 清理多余的空行
  cleaned = cleaned.replace(/\n{3,}/g, '\n\n').trim()

  return cleaned
}

/**
 * 配置markdown-it实例
 */
interface MarkdownRendererConfig {
  allowHtml: boolean
  enableMermaid: boolean
  breaks: boolean
}

const DEFAULT_RENDERER_CONFIG: MarkdownRendererConfig = {
  allowHtml: true,
  enableMermaid: true,
  breaks: false,
}

function createMarkdownRenderer(config: MarkdownRendererConfig): MarkdownIt {
  const md: MarkdownIt = new MarkdownIt({
    html: config.allowHtml,
    linkify: true,     // 自动转换URL为链接
    typographer: true, // 启用一些语言中立的替换 + 引号美化
    breaks: config.breaks,
    highlight: function (str: string, lang: string): string {
      // Mermaid 代码块：输出占位容器，稍后由 processMermaidBlocks() 异步渲染为 SVG。
      if (lang === 'mermaid' && config.enableMermaid) {
        // 容器 id 由源码哈希派生，保证同一内容多次渲染产出一致的 HTML。
        const id = `mermaid-${hashString(str)}`
        const escapedSource = escapeHtml(str)
        // data-mermaid-source 保存原始源码（HTML 实体编码，读取 dataset 时自动解码），
        // 供异步渲染、错误降级与复制源码功能使用。
        return (
          `<div class="mermaid-container" data-mermaid-id="${id}" ` +
          `data-mermaid-source="${escapedSource}" data-mermaid-state="pending">` +
          buildMermaidPlaceholder(escapedSource) +
          `</div>`
        )
      }

      // 代码高亮处理
      if (lang && hljs.getLanguage(lang)) {
        try {
          const highlighted = hljs.highlight(str, { 
            language: lang,
            ignoreIllegals: true 
          }).value
          
          return `<pre class="hljs language-${lang}"><code class="language-${lang}">${highlighted}</code></pre>`
        } catch (e) {
          console.warn('Syntax highlighting failed:', e)
        }
      }
      
      // 无语言标识或高亮失败时的fallback
      const escaped = escapeHtml(str)
      return `<pre class="hljs"><code>${escaped}</code></pre>`
    }
  })

  // 复制按钮位于可横向滚动的 pre 外，Mermaid 继续使用自己的源码操作。
  for (const type of ['fence', 'code_block'] as const) {
    const renderBlock = md.renderer.rules[type]!
    md.renderer.rules[type] = (tokens, idx, options, env, self) => {
      const html = renderBlock(tokens, idx, options, env, self)
      if (!env?.copyCode || html.includes('<div class="mermaid-container"')) return html

      const label = escapeHtml(i18n.global.t('aiChat.code.copy'))
      return (
        `<div class="markdown-code-block">${html}` +
        `<button class="code-copy-btn" type="button" title="${label}" aria-label="${label}"></button>` +
        '</div>\n'
      )
    }
  }

  // 自定义表格渲染规则：
  // 外层 .table-block 提供定位上下文与「复制 Markdown」按钮（按钮必须在
  // .table-wrapper 的横向滚动容器之外，否则会随表格一起滚动出视野）；
  // 表格的原始 markdown 源码通过 token.map 行号从 env.source 中切片取出，
  // 存入 data-table-md 供点击复制使用（HTML 实体编码，dataset 读取时自动解码）。
  md.renderer.rules.table_open = (tokens, idx, _options, env) => {
    let source = ''
    const map = tokens[idx].map
    if (map && env && typeof env.source === 'string') {
      source = env.source
        .replace(/\r\n/g, '\n')
        .split('\n')
        .slice(map[0], map[1])
        .join('\n')
        .trim()
    }
    if (!source) {
      return '<div class="table-block"><div class="table-wrapper"><table class="markdown-table">\n'
    }
    const label = i18n.global.t('markdown.copyTableMarkdown')
    return (
      `<div class="table-block" data-table-md="${escapeHtml(source)}">` +
      `<button class="table-copy-btn" type="button" aria-label="${label}">${label}</button>` +
      `<div class="table-wrapper"><table class="markdown-table">\n`
    )
  }
  md.renderer.rules.table_close = () => '</table></div></div>\n'
  
  // 自定义链接渲染规则（添加target="_blank"和安全属性）
  type LinkOpenRule = NonNullable<typeof md.renderer.rules.link_open>
  const defaultLinkRender = md.renderer.rules.link_open
  
  const linkOpenRule: LinkOpenRule = function (tokens, idx, options, env, self) {
    const aIndex = tokens[idx].attrIndex('target')
    
    if (aIndex < 0) {
      tokens[idx].attrPush(['target', '_blank'])
    } else {
      tokens[idx].attrs![aIndex][1] = '_blank'
    }
    
    tokens[idx].attrPush(['rel', 'noopener noreferrer'])
    
    return defaultLinkRender
      ? defaultLinkRender(tokens, idx, options, env, self)
      : self.renderToken(tokens, idx, options)
  }
  md.renderer.rules.link_open = linkOpenRule

  return md
}

// 按配置复用 renderer；默认配置保持现有聊天渲染行为不变。
const rendererInstances = new Map<string, MarkdownIt>()

/**
 * 获取markdown渲染器实例（单例）
 */
export function getMarkdownRenderer(
  options: Partial<MarkdownRendererConfig> = {},
): MarkdownIt {
  const config: MarkdownRendererConfig = {
    ...DEFAULT_RENDERER_CONFIG,
    ...options,
  }
  const key = `${config.allowHtml}:${config.enableMermaid}:${config.breaks}`
  let renderer = rendererInstances.get(key)
  if (!renderer) {
    renderer = createMarkdownRenderer(config)
    rendererInstances.set(key, renderer)
  }
  return renderer
}

/**
 * 渲染markdown内容为HTML
 * 
 * @param content - 原始markdown内容（可能包含XML标签）
 * @param options - 渲染选项
 * @returns 渲染后的HTML字符串
 */
export function renderMarkdown(
  content: string,
  options: {
    cleanXml?: boolean      // 是否清理XML标签，默认true
    wrapperClass?: string   // 包装div的class名称
    copyCode?: boolean      // 仅在支持代码复制交互的视图中启用
  } = {}
): string {
  const {
    cleanXml = true,
    wrapperClass = 'markdown-content'
  } = options

  // 安全检查
  if (!content || typeof content !== 'string') {
    return `<div class="${wrapperClass}"><p class="text-gray-500">${i18n.global.t('markdown.noContent')}</p></div>`
  }

  // 清理XML标签
  const cleaned = cleanXml ? cleanXmlAndMetadata(content) : content

  // 如果清理后为空
  if (!cleaned) {
    return `<div class="${wrapperClass}"><p class="text-gray-500">${i18n.global.t('markdown.noContent')}</p></div>`
  }

  // 渲染markdown（env.source 供表格渲染规则按行号切片出原始 markdown 源码）
  const md = getMarkdownRenderer()
  const html = md.render(cleaned, { source: cleaned, copyCode: options.copyCode })

  // 包装在容器中
  return `<div class="${wrapperClass}">${html}</div>`
}

/**
 * Render administrator-authored announcement Markdown for `v-html` consumers.
 *
 * Announcement content deliberately uses a restricted renderer:
 * - raw HTML is escaped rather than inserted into the DOM;
 * - Mermaid execution is disabled (the fence falls back to escaped code);
 * - soft line breaks remain visible for short operational notices;
 * - table copy controls are omitted because announcement surfaces do not
 *   install the interactive Markdown toolbar used by chat messages.
 */
export function renderAnnouncementMarkdown(
  content: string,
  wrapperClass = 'announcement-markdown',
): string {
  const source = typeof content === 'string' ? content.trim() : ''
  if (!source) {
    return `<div class="${wrapperClass}"><p>${i18n.global.t('markdown.noContent')}</p></div>`
  }

  const md = getMarkdownRenderer({
    allowHtml: false,
    enableMermaid: false,
    breaks: true,
  })
  return `<div class="${wrapperClass}">${md.render(source)}</div>`
}

/**
 * 处理容器内所有 Mermaid 占位元素，将其异步渲染为 SVG 图表。
 *
 * 行为：
 * - 查找所有处于 `pending` 状态的 `.mermaid-container`；
 * - 懒加载 Mermaid 实例后，对每个容器调用 `mermaid.render()`；
 * - 渲染成功：替换为 SVG，并附加“复制源码”按钮；
 * - 渲染失败：显示带高亮的原始源码 + 错误提示（优雅降级）；
 * - 库加载失败：所有占位元素降级为普通源码块。
 *
 * 该函数幂等：仅处理 `data-mermaid-state="pending"` 的容器，可安全重复调用。
 *
 * @param containerEl - 包含 Mermaid 占位元素的 DOM 容器（如消息列表根节点）
 */
export async function processMermaidBlocks(containerEl: HTMLElement | null): Promise<void> {
  if (!containerEl) return

  const containers = Array.from(
    containerEl.querySelectorAll<HTMLElement>('.mermaid-container[data-mermaid-state="pending"]')
  )
  if (containers.length === 0) return

  // 标记为处理中，避免并发重复渲染同一容器
  containers.forEach((el) => {
    el.dataset.mermaidState = 'rendering'
  })

  let mermaid
  try {
    mermaid = await loadMermaid()
  } catch (err) {
    // 库加载失败：降级为普通源码块（占位容器已内置 <pre> 源码，保留即可）
    console.warn(i18n.global.t('markdown.mermaidLoadFail'), err)
    containers.forEach((el) => {
      el.dataset.mermaidState = 'error'
      el.classList.add('is-error')
      const loading = el.querySelector('.mermaid-loading')
      if (loading) loading.remove()
    })
    return
  }

  await Promise.all(
    containers.map(async (el) => {
      const source = el.dataset.mermaidSource || ''
      const renderId = `mermaid-render-${++mermaidRenderSeq}`
      try {
        const { svg } = await mermaid.render(renderId, source)
        el.innerHTML =
          `<div class="mermaid-svg">${svg}</div>` +
          `<button class="mermaid-copy-btn" type="button" aria-label="${i18n.global.t('markdown.copySource')}">${i18n.global.t('markdown.copySource')}</button>`
        el.dataset.mermaidState = 'done'
        el.classList.add('is-rendered')
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err)
        el.innerHTML =
          `<div class="mermaid-error">${i18n.global.t('markdown.mermaidRenderFail', { msg: escapeHtml(message) })}</div>` +
          `<pre class="hljs mermaid-source language-mermaid"><code>${escapeHtml(source)}</code></pre>` +
          `<button class="mermaid-copy-btn" type="button" aria-label="${i18n.global.t('markdown.copySource')}">${i18n.global.t('markdown.copySource')}</button>`
        el.dataset.mermaidState = 'error'
        el.classList.add('is-error')
      }
    })
  )
}

/**
 * 主题（浅色/深色）切换后重绘容器内已渲染的 Mermaid 图表。
 *
 * Mermaid 把配色烘焙进 SVG 的内联样式，CSS 无法事后翻转，只能用新主题
 * 重新 render。做法是把 `done`/`error` 状态的容器复原成 `pending` 占位结构，
 * 再交给 `processMermaidBlocks()`（它会在 `loadMermaid()` 中按当前 html.dark
 * 重新 initialize Mermaid）。
 *
 * 仍处于 `pending`/`rendering` 的容器不受影响：前者会被本次处理顺带渲染，
 * 后者的在途渲染沿用旧主题，但主题切换后会再次触发本函数。
 *
 * @param containerEl - 搜索范围，通常传 `document.body` 以覆盖弹窗等传送节点
 */
export async function refreshMermaidBlocks(containerEl: HTMLElement | null): Promise<void> {
  if (!containerEl) return

  const rendered = Array.from(
    containerEl.querySelectorAll<HTMLElement>(
      '.mermaid-container[data-mermaid-state="done"], .mermaid-container[data-mermaid-state="error"]'
    )
  )
  if (rendered.length === 0) return

  rendered.forEach((el) => {
    el.classList.remove('is-rendered', 'is-error')
    el.innerHTML = buildMermaidPlaceholder(escapeHtml(el.dataset.mermaidSource || ''))
    el.dataset.mermaidState = 'pending'
  })

  await processMermaidBlocks(containerEl)
}

/**
 * 仅清理XML标签，不渲染markdown
 */
export function cleanContent(content: string): string {
  return cleanXmlAndMetadata(content)
}

/**
 * 提取纯文本（移除markdown和HTML标签）
 */
export function extractPlainText(content: string): string {
  const cleaned = cleanXmlAndMetadata(content)
  const md = getMarkdownRenderer()
  const html = md.render(cleaned)
  
  // 简单的HTML标签移除
  return html.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim()
}
