import { describe, expect, it } from 'vitest'
import { extractVisualAnalysis, parseAgentMessage } from './agentResultSummary'

describe('parseAgentMessage', () => {
  it('moves the completion preamble out of a specialist answer', () => {
    const view = parseAgentMessage([
      '**项目专家 Agent** 已完成本轮分析。',
      '',
      '- 项目：`RavenAI`',
      '- 问题：为什么启动失败？',
      '- 状态：`ok`',
      '- 模型：`claude-sonnet-4-5`，耗时：12.34s',
      '- 上下文：工作区已保留。',
      '',
      '## 回答',
      '',
      '启动失败是因为配置缺失。',
      '',
      '## 关键词',
      '`config`',
    ].join('\n'))

    expect(view.displayMarkdown).toBe('启动失败是因为配置缺失。')
    expect(view.completionSummary).toMatchObject({
      title: '项目专家 Agent',
      model: 'claude-sonnet-4-5',
      duration: '12.34s',
      packageBuildCompleted: false,
    })
    expect(view.completionSummary?.fields).toContainEqual({
      label: '项目',
      value: 'RavenAI',
    })
  })

  it('leaves ordinary assistant Markdown unchanged', () => {
    const content = '## 回答\n\n普通回复'
    expect(parseAgentMessage(content)).toEqual({
      displayMarkdown: content,
      completionSummary: null,
    })
  })

  it('hides summary and keywords while retaining later diagnosis and recommendations', () => {
    const view = parseAgentMessage([
      '**日志分析 Agent** 已完成本轮分析。',
      '',
      '- 模型：`yinhe-thinking`，耗时：69.96s',
      '',
      '## 回答',
      '正文结论。',
      '',
      '## 摘要',
      '不展示的重复摘要。',
      '### 摘要细节',
      '也不展示。',
      '',
      '## 根因假设',
      '- 配置缺失。',
      '',
      '## 建议',
      '- 检查配置。',
      '',
      '## 关键词',
      '`config` `timeout`',
    ].join('\n'))
    expect(view.displayMarkdown).toBe('正文结论。\n\n## 根因假设\n- 配置缺失。\n\n## 建议\n- 检查配置。')
    expect(view.completionSummary).toMatchObject({ model: 'yinhe-thinking', duration: '69.96s' })
  })

  it('preserves code, quoted headings, and nested answer sections', () => {
    const answer = [
      '## 回答',
      '```markdown',
      '## 摘要',
      '这是代码示例。',
      '## 关键词',
      '```',
      '',
      '> ## 摘要',
      '> 引用内容。',
      '',
      '### 摘要',
      '正文中的子标题。',
    ].join('\n')
    const view = parseAgentMessage('**项目专家 Agent** 已完成本轮分析。\n\n' + answer + '\n\n## 关键词\n`hidden`')
    expect(view.displayMarkdown).toBe(answer.replace('## 回答\n', ''))
  })

  it('also hides English result metadata and keeps subsequent sections', () => {
    const view = parseAgentMessage([
      '**Project Expert Agent** completed this analysis.',
      '',
      '## Answer',
      'The answer.',
      '',
      '## Summary',
      'Repeated summary.',
      '',
      '## Keywords',
      '`hidden`',
      '',
      '# References',
      '[Evidence](https://example.com)',
    ].join('\n'))
    expect(view.displayMarkdown).toBe('The answer.\n\n# References\n[Evidence](https://example.com)')
  })

  it('supports a Configuration Manager package-build summary and preserves its download link', () => {
    const view = parseAgentMessage([
      '**配置管理员 Agent** 已完成本轮整包构建。',
      '',
      '- 项目：`LX10`',
      '- 整包：`LX10-V1.0.0.3.tgz`',
      '- SHA-256：`0123456789abcdef`',
      '- 模型：`qwen-max`',
      '',
      '## 回答',
      '整包已发布。',
      '',
      '[下载整包](/raven/api/download/package-123)',
    ].join('\n'))
    expect(view.displayMarkdown).toContain('整包已发布。')
    expect(view.displayMarkdown).toContain('[下载整包](/raven/api/download/package-123)')
    expect(view.completionSummary?.title).toBe('配置管理员 Agent')
    expect(view.completionSummary?.fields).toContainEqual({
      label: '整包',
      value: 'LX10-V1.0.0.3.tgz',
    })
    expect(view.completionSummary?.model).toBe('qwen-max')
    expect(view.completionSummary?.duration).toBeUndefined()
    expect(view.completionSummary?.packageBuildCompleted).toBe(true)
  })

  it.each([
    ['**配置管理员 Agent** 已完成本轮打包。', '- 状态：`ok`', true],
    ['**配置管理员 Agent** 已完成本轮整包打包。', '- 状态：`ok`', true],
    ['**Configuration Manager Agent** completed this packaging.', '- Status: `ok`', true],
    ['**Configuration Manager Agent** completed the full-package build.', '- Status: `ok`', true],
    ['**配置管理员 Agent** 已完成本轮检索。', '- 状态：`ok`', false],
    ['**Configuration Manager Agent** completed this search.', '- Status: `ok`', false],
    ['**项目专家 Agent** 已完成本轮分析。', '- 状态：`ok`', false],
    ['**配置管理员 Agent** 已完成本轮打包。', '- 状态：`error`', false],
    ['**配置管理员 Agent** 已完成本轮打包。', '- 状态：`cancelled`', false],
    ['**配置管理员 Agent** 已完成本轮打包。', '- 状态：`pending`', false],
  ])('recognizes only completed package builds: %s / %s', (title, status, expected) => {
    const view = parseAgentMessage([
      title,
      '',
      status,
      '- 模型：`full-package-build`，耗时：69.96s',
      '',
      '## 配置管理员说明',
      '回复正文可能提到打包或已完成本轮打包，不代表这一轮执行过打包。',
    ].join('\n'))
    expect(view.completionSummary?.packageBuildCompleted).toBe(expected)
    expect(view.completionSummary?.duration).toBe('69.96s')
  })
})

describe('extractVisualAnalysis', () => {
  it('recovers the OCR text and image count from persisted user content', () => {
    expect(extractVisualAnalysis([
      '请分析截图',
      '',
      '<user_image_ocr note="素材" image_count="2">',
      '[图片 1]',
      'Error: timeout',
      '</user_image_ocr>',
    ].join('\n'))).toEqual({
      text: '[图片 1]\nError: timeout',
      imageCount: 2,
    })
  })
})
