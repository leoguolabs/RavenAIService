import { describe, expect, it } from 'vitest'
import { createSSRApp, h, reactive } from 'vue'
import { renderToString } from '@vue/server-renderer'

import AnthropicEndpointCard from '@/components/admin/AnthropicEndpointCard.vue'
import { i18n, setI18nLocale } from '@/i18n'
import type {
  EndpointForm,
  ModelProviderProfile,
  ModelSettingFieldEntry,
} from '@/api/admin'

const profile = (over: Partial<ModelProviderProfile> = {}): ModelProviderProfile => ({
  name: 'deepseek',
  label: 'DeepSeek',
  default_base_url: 'https://api.deepseek.com/anthropic',
  default_model: 'deepseek-v4-pro',
  default_small_fast_model: 'deepseek-v4-flash',
  models: ['deepseek-v4-pro', 'deepseek-v4-flash'],
  supports_image_input: false,
  supports_mcp_server_tools: true,
  notes: 'DeepSeek Anthropic compatible endpoint',
  base_url_needs_input: false,
  ...over,
})

const emptyForm = (over: Partial<EndpointForm> = {}): EndpointForm =>
  reactive({
    provider: 'deepseek',
    api_key: '',
    api_keys: '',
    base_url: '',
    model: '',
    small_fast_model: '',
    ...over,
  })

type CardProps = InstanceType<typeof AnthropicEndpointCard>['$props']

const render = async (
  props: { slotName: 'primary' | 'backup'; form: EndpointForm } & Partial<CardProps>,
) => {
  setI18nLocale('zh')
  const app = createSSRApp({
    render: () =>
      h(AnthropicEndpointCard, {
        fields: {},
        providerOptions: ['anthropic', 'deepseek', 'moonshot'],
        profiles: [profile(), profile({ name: 'moonshot', label: 'Kimi' })],
        keySet: false,
        testing: false,
        testResult: null,
        ...props,
      } as CardProps),
  })
  app.use(i18n)
  return renderToString(app)
}

describe('AnthropicEndpointCard', () => {
  it('uses one main model and disables the auxiliary model for OpenAI', async () => {
    const form = emptyForm({ provider: 'openai', model: 'gpt-6.1-terra', small_fast_model: 'old-model' })
    const html = await render({ slotName: 'primary', form,
      providerOptions: ['openai'], profiles: [profile({ name: 'openai', label: 'OpenAI',
        api_protocol: 'openai_responses', models: ['gpt-6.1-terra'], supports_image_input: true })] })
    expect(form.small_fast_model).toBe('gpt-6.1-terra')
    expect(html).toContain('OpenAI 模式固定使用上方主力模型')
    expect(html).toContain('disabled')
  })
  it('reads source badges from the primary keys for the primary slot', async () => {
    const fields: Record<string, ModelSettingFieldEntry> = {
      anthropic_provider: { group: 'anthropic', source: 'override' },
      anthropic_api_keys: { group: 'anthropic', source: 'override', is_set: true, count: 2 },
      anthropic_base_url: { group: 'anthropic', source: 'override' },
      // The backup entries must be ignored here — picking these up would mean
      // the two cards are reading each other's state.
      anthropic_backup_provider: { group: 'anthropic_backup', source: 'unset' },
    }
    const html = await render({ slotName: 'primary', form: emptyForm(), fields })

    expect(html).toContain('ms-badge--override')
    expect(html).not.toContain('ms-badge--unset')
  })

  it('reads source badges from the backup keys for the backup slot', async () => {
    const fields: Record<string, ModelSettingFieldEntry> = {
      anthropic_provider: { group: 'anthropic', source: 'override' },
      anthropic_backup_provider: { group: 'anthropic_backup', source: 'unset' },
    }
    const html = await render({ slotName: 'backup', form: emptyForm(), fields })

    // The primary's `override` must not leak into the backup card.
    expect(html).toContain('ms-badge--unset')
    expect(html).not.toContain('ms-badge--override')
  })

  it('offers the selected provider model presets and its capability summary', async () => {
    const html = await render({ slotName: 'primary', form: emptyForm() })

    expect(html).toContain('deepseek-v4-pro')
    expect(html).toContain('deepseek-v4-flash')
    // deepseek supports MCP tools but not image input.
    expect(html).toContain('支持 MCP 工具')
    expect(html).toContain('不支持图像输入')
  })

  it('warns when the provider default base URL is still a template', async () => {
    const html = await render({
      slotName: 'backup',
      form: emptyForm({ provider: 'aliyun' }),
      profiles: [
        profile({
          name: 'aliyun',
          label: '阿里云百炼',
          default_base_url: 'https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/apps/anthropic',
          base_url_needs_input: true,
        }),
      ],
    })

    expect(html).toContain('ms-warn')
  })

  it('disables the probe button while the slot is inactive', async () => {
    const active = await render({ slotName: 'backup', form: emptyForm() })
    expect(active).not.toContain('pointer-events-none')

    const inactive = await render({ slotName: 'backup', form: emptyForm(), inactive: true })
    expect(inactive).toContain('pointer-events-none')
    expect(inactive).toContain('disabled')
  })

  it('labels the probe button per slot', async () => {
    const primary = await render({ slotName: 'primary', form: emptyForm() })
    const backup = await render({ slotName: 'backup', form: emptyForm() })

    expect(primary).toContain('测试连接')
    expect(backup).toContain('测试备用连接')
  })

  it('renders a multi-key textarea only for the primary slot', async () => {
    const primary = await render({
      slotName: 'primary',
      form: emptyForm(),
      keySet: true,
      keyCount: 15,
    })
    const backup = await render({ slotName: 'backup', form: emptyForm() })

    expect(primary).toContain('<textarea')
    expect(primary).toContain('已配置 15 个')
    expect(backup).not.toContain('<textarea')
    expect(backup).toContain('type="password"')
  })
})
