<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ExternalLink } from 'lucide-vue-next'
import { adminApi, type AgentRuntimeSettings, type GatewayModel } from '@/api/admin'

const emit = defineEmits<{ 'routing-change': [enabled: boolean] }>()
const { t } = useI18n()
const form = ref<AgentRuntimeSettings | null>(null)
const models = ref<GatewayModel[]>([])
const key = ref('')
const busy = ref(false)
const message = ref('')
const error = ref('')

function setForm(value: AgentRuntimeSettings) {
  form.value = value
  for (const agent of value.agents) {
    const current = value.agent_overrides[agent] || {}
    value.agent_overrides[agent] = { executor: current.executor || undefined, model: current.model || '' }
  }
  // Report only the server-confirmed state, never an unsaved checkbox edit.
  emit('routing-change', value.enabled)
}
async function refreshModels() {
  try { models.value = (await adminApi.getGatewayModels()).data || [] }
  catch { error.value = t('admin.agentRuntime.modelLoadFailed') }
}
async function save() {
  if (!form.value) return
  busy.value = true
  error.value = ''
  message.value = ''
  try {
    const { enabled, executor, base_url, model, agent_overrides } = form.value
    const result = await adminApi.updateAgentRuntime({ enabled, executor, base_url, model, agent_overrides, api_key: key.value })
    if (!result.data) throw new Error('Missing runtime configuration')
    setForm(result.data)
    key.value = ''
    message.value = t('admin.agentRuntime.saved')
    if (result.data.api_key_set) await refreshModels()
  } catch { error.value = t('admin.agentRuntime.saveFailed') }
  finally { busy.value = false }
}
onMounted(async () => {
  try {
    const result = await adminApi.getAgentRuntime()
    if (!result.data) throw new Error('Missing runtime configuration')
    setForm(result.data)
    if (form.value?.api_key_set) await refreshModels()
  } catch { error.value = t('admin.agentRuntime.loadFailed') }
})
</script>

<template>
  <section class="runtime-card" aria-labelledby="runtime-title">
    <div class="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
      <h2 id="runtime-title">{{ t('admin.agentRuntime.title') }}</h2>
      <a
        v-if="form?.magpie_admin_url"
        :href="form.magpie_admin_url"
        target="_blank"
        rel="noopener noreferrer"
        class="runtime-link inline-flex items-center gap-1 text-sm font-medium underline underline-offset-2"
      >
        {{ t('admin.agentRuntime.openMagpieAdmin') }}
        <ExternalLink :size="14" aria-hidden="true" />
      </a>
    </div>
    <p>{{ t('admin.agentRuntime.description') }}</p>
    <form v-if="form" @submit.prevent="save">
      <label class="runtime-toggle"><input v-model="form.enabled" type="checkbox">{{ t('admin.agentRuntime.routeThroughMagpie') }}</label>
      <div class="runtime-grid">
        <label>{{ t('admin.agentRuntime.gatewayUrl') }}<input v-model="form.base_url" type="url" required></label>
        <label>{{ t('admin.agentRuntime.gatewayCredential') }}<input v-model="key" type="password" autocomplete="new-password" :placeholder="form.api_key_set ? t('admin.agentRuntime.credentialSet') : ''"></label>
        <label>{{ t('admin.agentRuntime.defaultExecutor') }}<select v-model="form.executor"><option v-for="(label, value) in form.executors" :key="value" :value="value">{{ label }}</option></select></label>
        <label>{{ t('admin.agentRuntime.defaultModelGroup') }}<input v-model="form.model" list="magpie-models" :required="form.enabled"></label>
      </div>
      <datalist id="magpie-models"><option v-for="model in models" :key="model.id" :value="model.id">{{ model.display_name || model.id }}</option></datalist>
      <details>
        <summary>{{ t('admin.agentRuntime.agentOverrides') }}</summary>
        <div v-for="agent in form.agents" :key="agent" class="runtime-agent">
          <span>{{ t(`agentTrace.agents.${agent}`, agent) }}</span>
          <select v-model="form.agent_overrides[agent].executor" :aria-label="`${agent} executor`">
            <option :value="undefined">{{ t('admin.agentRuntime.inheritExecutor') }}</option>
            <option v-for="(label, value) in form.executors" :key="value" :value="value">{{ label }}</option>
          </select>
          <input v-model="form.agent_overrides[agent].model" list="magpie-models" :aria-label="`${agent} model`" :placeholder="t('admin.agentRuntime.inheritModel')">
        </div>
      </details>
      <div class="runtime-actions">
        <button type="submit" :disabled="busy">{{ t('admin.agentRuntime.save') }}</button>
        <button type="button" :disabled="busy || !form.api_key_set" @click="refreshModels">{{ t('admin.agentRuntime.refreshModels') }}</button>
      </div>
    </form>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="error" role="alert" class="runtime-error">{{ error }}</p>
  </section>
</template>

<style scoped>
.runtime-card { padding: 20px; border: 1px solid var(--el-border-color, #e2e8f0); border-radius: 16px; background: var(--el-bg-color, white); }
h2 { font-size: 18px; font-weight: 600; }
.runtime-link { color: var(--el-color-primary, #2563eb); }
p { color: var(--el-text-color-secondary, #64748b); font-size: 13px; margin: 6px 0 16px; }
.runtime-toggle, .runtime-actions { display: flex; align-items: center; gap: 10px; margin: 16px 0; }
.runtime-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-bottom: 20px; }
.runtime-grid label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; }
input:not([type=checkbox]), select { width: 100%; min-width: 0; border: 1px solid var(--el-border-color, #cbd5e1); border-radius: 8px; padding: 8px 10px; background: var(--el-bg-color, white); }
.runtime-agent { display: grid; grid-template-columns: 120px 1fr 1fr; align-items: center; gap: 12px; margin-top: 12px; font-size: 13px; }
button { border: 1px solid var(--el-border-color, #cbd5e1); border-radius: 8px; padding: 8px 14px; }
button[type=submit] { background: #2563eb; color: white; }
button:disabled { opacity: .5; }
summary { cursor: pointer; font-size: 14px; }
.runtime-error { color: #dc2626; }
@media (max-width: 700px) { .runtime-grid, .runtime-agent { grid-template-columns: 1fr; } }
</style>
