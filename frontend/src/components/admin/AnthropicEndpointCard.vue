<script setup lang="ts">
/**
 * One Anthropic-compatible endpoint's configuration card.
 *
 * The primary and the failover backup are the same form — same provider
 * catalogue, same preset behaviour, same connectivity probe — differing only in
 * which backend keys they write and whether they can be switched off. Rendering
 * both from one component keeps the two in step; the alternative was ~150 lines
 * of near-identical markup that would inevitably drift.
 *
 * ``form`` is mutated in place: the parent owns one reactive object per slot and
 * passes the slice down, matching how the rest of the page already works.
 */
import { computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CheckCircle2, PlugZap, XCircle } from 'lucide-vue-next'
import type {
  EndpointForm,
  ModelProviderProfile,
  ModelSettingFieldEntry,
  ModelSettingsTestResult,
} from '@/api/admin'

const props = defineProps<{
  /** Which endpoint this card edits — also picks the backend key prefix. */
  slotName: 'primary' | 'backup' | 'bug_fix'
  form: EndpointForm
  fields: Record<string, ModelSettingFieldEntry> | undefined
  providerOptions: string[]
  profiles: ModelProviderProfile[]
  /** Whether a secret is already stored server-side (it is never sent back). */
  keySet: boolean
  /** Number of primary keys stored server-side. Backup stays single-key. */
  keyCount?: number
  testing: boolean
  testResult: ModelSettingsTestResult | null
  /** Backup only: dim + block the fields while the slot is switched off. */
  inactive?: boolean
}>()

const emit = defineEmits<{ (e: 'test'): void }>()

const { t } = useI18n()

// Backend key names per slot. Mirrors ``model_settings_service.AnthropicSlot``;
// deliberately an explicit map rather than string concatenation, because
// ``anthropic_backup_model`` also starts with ``anthropic_``.
type SharedField = Exclude<keyof EndpointForm, 'api_keys'>
const KEYS: Record<'primary' | 'backup' | 'bug_fix', Record<SharedField, string>> = {
  primary: {
    provider: 'anthropic_provider',
    api_key: 'anthropic_api_key',
    base_url: 'anthropic_base_url',
    model: 'anthropic_model',
    small_fast_model: 'anthropic_small_fast_model',
  },
  bug_fix: {
    provider: 'bug_fix_agent_provider',
    api_key: 'bug_fix_agent_api_key',
    base_url: 'bug_fix_agent_base_url',
    model: 'bug_fix_agent_model',
    small_fast_model: 'bug_fix_agent_small_fast_model',
  },
  backup: {
    provider: 'anthropic_backup_provider',
    api_key: 'anthropic_backup_api_key',
    base_url: 'anthropic_backup_base_url',
    model: 'anthropic_backup_model',
    small_fast_model: 'anthropic_backup_small_fast_model',
  },
}

const keyOf = (field: SharedField) => KEYS[props.slotName][field]

const sourceOf = (field: SharedField) =>
  props.fields?.[keyOf(field)]?.source ?? 'env'
const sourceLabel = (field: SharedField) => {
  const src = sourceOf(field)
  if (src === 'override') return t('admin.modelSettings.sourceOverride')
  if (src === 'unset') return t('admin.modelSettings.sourceUnset')
  return t('admin.modelSettings.sourceEnv')
}
const primaryKeySource = computed(() => {
  const pool = props.fields?.anthropic_api_keys
  if (pool?.is_set) return pool.source
  return props.fields?.anthropic_api_key?.source ?? 'unset'
})

const profileOf = (name: string) => props.profiles.find((p) => p.name === name) ?? null
const selectedProfile = computed(() => profileOf(props.form.provider))
const isCustomProvider = computed(() => props.form.provider === 'custom')
const isOpenAI = computed(() => props.form.provider === 'openai')
watch(() => [props.form.provider, props.form.model], () => {
  if (isOpenAI.value) props.form.small_fast_model = props.form.model
}, { immediate: true })

const providerLabel = (name: string) => {
  const label = profileOf(name)?.label
  return label ? `${label} · ${name}` : name
}

// The text inputs stay the source of truth (a provider may ship a model newer
// than this table); the selects are shortcuts that write into them.
const CUSTOM_MODEL = '__custom__'
const modelPresets = computed(() => selectedProfile.value?.models ?? [])
const presetValue = (current: string) =>
  modelPresets.value.includes(current) ? current : CUSTOM_MODEL
const applyPreset = (field: 'model' | 'small_fast_model', event: Event) => {
  const value = (event.target as HTMLSelectElement).value
  if (value !== CUSTOM_MODEL) props.form[field] = value
}

/**
 * Switching provider re-points Base URL / models at that vendor's defaults —
 * the whole purpose of the dropdown. Any hand-typed value is replaced, so the
 * form always describes one coherent upstream.
 */
const handleProviderChange = () => {
  const profile = selectedProfile.value
  if (!profile) return
  props.form.base_url = profile.default_base_url
  props.form.model = profile.default_model
  props.form.small_fast_model = profile.default_small_fast_model ?? ''
}

const testBtnLabel = computed(() =>
  props.slotName === 'backup'
    ? t('admin.modelSettings.testBackupBtn')
    : t('admin.modelSettings.testBtn'),
)
</script>

<template>
  <div class="grid gap-4 lg:grid-cols-2" :class="{ 'opacity-50 pointer-events-none': inactive }">
    <label class="block text-sm text-slate-700">
      <span class="flex items-center gap-2">
        {{ t('admin.modelSettings.providerLabel') }}
        <span class="ms-badge" :class="`ms-badge--${sourceOf('provider')}`">{{ sourceLabel('provider') }}</span>
      </span>
      <select
        v-model="form.provider"
        class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none bg-white"
        @change="handleProviderChange"
      >
        <option v-for="opt in providerOptions" :key="opt" :value="opt">
          {{ providerLabel(opt) }}
        </option>
      </select>
      <p class="text-xs text-slate-500 mt-1">{{ t('admin.modelSettings.providerHint') }}</p>
      <p v-if="selectedProfile" class="text-xs text-slate-500 mt-1">
        {{ t('admin.modelSettings.capabilities') }}:
        <span :class="selectedProfile.supports_image_input ? 'text-emerald-600' : 'text-slate-400'">
          {{ selectedProfile.supports_image_input ? t('admin.modelSettings.capImageYes') : t('admin.modelSettings.capImageNo') }}
        </span>
        ·
        <span :class="selectedProfile.supports_mcp_server_tools ? 'text-emerald-600' : 'text-slate-400'">
          {{ selectedProfile.supports_mcp_server_tools ? t('admin.modelSettings.capMcpYes') : t('admin.modelSettings.capMcpNo') }}
        </span>
      </p>
      <p v-if="selectedProfile?.notes" class="text-xs text-slate-500 mt-1">
        {{ selectedProfile.notes }}
      </p>
    </label>

    <label class="block text-sm text-slate-700">
      <span class="flex items-center gap-2">
        {{ slotName === 'primary' ? t('admin.modelSettings.apiKeyPoolLabel') : t('admin.modelSettings.apiKeyLabel') }}
        <span
          class="ms-badge"
          :class="`ms-badge--${slotName === 'primary' ? primaryKeySource : sourceOf('api_key')}`"
        >
          {{ slotName === 'primary' ? t('admin.modelSettings.apiKeyCount', { count: keyCount ?? 0 }) : sourceLabel('api_key') }}
        </span>
      </span>
      <textarea
        v-if="slotName === 'primary'"
        v-model="form.api_keys"
        rows="5"
        autocomplete="off"
        spellcheck="false"
        class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
        :placeholder="keySet ? t('admin.modelSettings.apiKeyPoolSetPlaceholder') : t('admin.modelSettings.apiKeyPoolUnsetPlaceholder')"
      />
      <input
        v-else
        v-model="form.api_key"
        type="password"
        autocomplete="off"
        class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
        :placeholder="keySet ? t('admin.modelSettings.apiKeySetPlaceholder') : t('admin.modelSettings.apiKeyUnsetPlaceholder')"
      />
      <p class="text-xs text-slate-500 mt-1">
        {{ slotName === 'primary' ? t('admin.modelSettings.apiKeyPoolHint') : t('admin.modelSettings.apiKeyHint') }}
      </p>
    </label>

    <label class="block text-sm text-slate-700">
      <span class="flex items-center gap-2">
        {{ t('admin.modelSettings.baseUrlLabel') }}
        <span class="ms-badge" :class="`ms-badge--${sourceOf('base_url')}`">{{ sourceLabel('base_url') }}</span>
      </span>
      <input
        v-model="form.base_url"
        type="text"
        spellcheck="false"
        class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
        :placeholder="selectedProfile?.default_base_url || t('admin.modelSettings.customRequiredPlaceholder')"
      />
      <p v-if="selectedProfile?.base_url_needs_input" class="ms-warn text-xs mt-1">
        {{ t('admin.modelSettings.baseUrlPlaceholderHint') }}
      </p>
      <p class="text-xs text-slate-500 mt-1">
        {{ isCustomProvider ? t('admin.modelSettings.baseUrlCustomHint') : t('admin.modelSettings.baseUrlHint') }}
      </p>
    </label>

    <label class="block text-sm text-slate-700">
      <span class="flex items-center gap-2">
        {{ t('admin.modelSettings.modelLabel') }}
        <span class="ms-badge" :class="`ms-badge--${sourceOf('model')}`">{{ sourceLabel('model') }}</span>
      </span>
      <select
        v-if="modelPresets.length"
        class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none bg-white"
        :value="presetValue(form.model)"
        @change="applyPreset('model', $event)"
      >
        <option v-for="m in modelPresets" :key="m" :value="m">{{ m }}</option>
        <option :value="CUSTOM_MODEL">{{ t('admin.modelSettings.modelCustomOption') }}</option>
      </select>
      <input
        v-model="form.model"
        type="text"
        spellcheck="false"
        class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
        :placeholder="selectedProfile?.default_model || t('admin.modelSettings.customRequiredPlaceholder')"
      />
      <p class="text-xs text-slate-500 mt-1">
        {{ isCustomProvider ? t('admin.modelSettings.modelCustomHint') : t('admin.modelSettings.modelHint') }}
      </p>
    </label>

    <label class="block text-sm text-slate-700">
      <span class="flex items-center gap-2">
        {{ t('admin.modelSettings.smallFastModelLabel') }}
        <span class="ms-badge" :class="`ms-badge--${sourceOf('small_fast_model')}`">{{ sourceLabel('small_fast_model') }}</span>
      </span>
      <select
        v-if="modelPresets.length"
        class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none bg-white"
        :value="presetValue(form.small_fast_model)"
        :disabled="isOpenAI"
        @change="applyPreset('small_fast_model', $event)"
      >
        <option v-for="m in modelPresets" :key="m" :value="m">{{ m }}</option>
        <option :value="CUSTOM_MODEL">{{ t('admin.modelSettings.modelCustomOption') }}</option>
      </select>
      <input
        v-model="form.small_fast_model"
        :disabled="isOpenAI"
        type="text"
        spellcheck="false"
        class="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 font-mono text-sm focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100 outline-none"
        :placeholder="selectedProfile?.default_small_fast_model || t('admin.modelSettings.customRequiredPlaceholder')"
      />
      <p class="text-xs text-slate-500 mt-1">{{ t(isOpenAI ? 'admin.modelSettings.openaiUnifiedHint' : 'admin.modelSettings.smallFastModelHint') }}</p>
    </label>

    <!-- Slot-specific extras (the primary's max-tokens field lives here). -->
    <slot name="extra" />
  </div>

  <div
    class="mt-4 pt-4 border-t border-slate-200"
    :class="{ 'opacity-50 pointer-events-none': inactive }"
  >
    <div class="flex flex-wrap items-center gap-3">
      <button type="button" class="ms-test-btn" :disabled="testing || inactive" @click="emit('test')">
        <PlugZap :size="15" />
        {{ testing ? t('admin.modelSettings.testingBtn') : testBtnLabel }}
      </button>
      <span class="text-xs text-slate-500">{{ t('admin.modelSettings.testHint') }}</span>
    </div>

    <div v-if="testResult" class="ms-test-result" :class="testResult.ok ? 'is-ok' : 'is-fail'">
      <component :is="testResult.ok ? CheckCircle2 : XCircle" :size="16" class="ms-test-icon" />
      <div class="min-w-0">
        <p class="ms-test-title">
          {{
            testResult.tested_key_count
              ? t('admin.modelSettings.testPoolResult', { healthy: testResult.healthy_key_count ?? 0, total: testResult.tested_key_count })
              : testResult.ok
              ? t('admin.modelSettings.testOk', { ms: testResult.latency_ms ?? 0 })
              : t('admin.modelSettings.testFailed')
          }}
        </p>
        <p class="ms-test-meta">{{ testResult.model }} · {{ testResult.base_url }}</p>
        <p v-if="testResult.ok && testResult.reply" class="ms-test-meta">
          {{ t('admin.modelSettings.testReply') }}: {{ testResult.reply }}
        </p>
        <p v-if="!testResult.ok && !testResult.tested_key_count" class="ms-test-meta">
          [{{ testResult.error_kind }}] {{ testResult.detail }}
        </p>
        <p v-if="testResult.tested_key_count" class="ms-test-meta">{{ testResult.detail }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped src="@/styles/model-settings-fields.css"></style>
