<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { adminApi, type AgentRuntimeSettings, type GatewayModel } from '@/api/admin'

const emit = defineEmits<{ 'routing-change': [enabled: boolean] }>()
const { locale } = useI18n()
const en = computed(() => locale.value.startsWith('en'))
const form = ref<AgentRuntimeSettings | null>(null)
const models = ref<GatewayModel[]>([])
const key = ref('')
const busy = ref(false)
const message = ref('')
const error = ref('')
const agentNames: Record<string, [string, string]> = {
  general_agent: ['通用问答', 'General chat'], log_analysis: ['日志分析', 'Log analysis'],
  device_agent: ['设备助手', 'Device agent'], project_expert: ['项目专家', 'Project expert'],
  package_search: ['软件包检索', 'Package search'], bug_fix: ['问题修复', 'Bug fix'],
}

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
  catch { error.value = en.value ? 'Unable to load Magpie models. Save the connection first.' : '无法读取 Magpie 模型，请先保存连接配置。' }
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
    message.value = en.value ? 'Saved. New runs use this configuration.' : '已保存，新任务使用此配置。'
    if (result.data.api_key_set) await refreshModels()
  } catch { error.value = en.value ? 'Save failed. Check the gateway credential and model.' : '保存失败，请检查网关密钥和模型配置。' }
  finally { busy.value = false }
}
onMounted(async () => {
  try {
    const result = await adminApi.getAgentRuntime()
    if (!result.data) throw new Error('Missing runtime configuration')
    setForm(result.data)
    if (form.value?.api_key_set) await refreshModels()
  } catch { error.value = en.value ? 'Unable to load Agent settings.' : '无法读取 Agent 配置。' }
})
</script>

<template>
  <section class="runtime-card" aria-labelledby="runtime-title">
    <h2 id="runtime-title">{{ en ? 'Agent execution and model routing' : 'Agent 执行与模型路由' }}</h2>
    <p>{{ en ? 'Choose the executor here; Magpie manages providers, subscriptions and model fallback.' : '在此选择执行器；供应商、订阅及模型故障切换由 Magpie 管理。' }}</p>
    <form v-if="form" @submit.prevent="save">
      <label class="runtime-toggle"><input v-model="form.enabled" type="checkbox">{{ en ? 'Route models through Magpie' : '通过 Magpie 路由模型' }}</label>
      <div class="runtime-grid">
        <label>{{ en ? 'Gateway URL' : '网关地址' }}<input v-model="form.base_url" type="url" required></label>
        <label>{{ en ? 'Gateway credential' : '网关密钥' }}<input v-model="key" type="password" autocomplete="new-password" :placeholder="form.api_key_set ? (en ? 'Configured · leave blank to keep' : '已配置 · 留空保留') : ''"></label>
        <label>{{ en ? 'Default executor' : '默认执行器' }}<select v-model="form.executor"><option v-for="(label, value) in form.executors" :key="value" :value="value">{{ label }}</option></select></label>
        <label>{{ en ? 'Default model / routing group' : '默认模型 / 路由组' }}<input v-model="form.model" list="magpie-models" :required="form.enabled"></label>
      </div>
      <datalist id="magpie-models"><option v-for="model in models" :key="model.id" :value="model.id">{{ model.display_name || model.id }}</option></datalist>
      <details>
        <summary>{{ en ? 'Per-Agent overrides' : '按 Agent 单独配置' }}</summary>
        <div v-for="agent in form.agents" :key="agent" class="runtime-agent">
          <span>{{ agentNames[agent]?.[en ? 1 : 0] || agent }}</span>
          <select v-model="form.agent_overrides[agent].executor" :aria-label="`${agent} executor`">
            <option :value="undefined">{{ en ? 'Default executor' : '跟随默认执行器' }}</option>
            <option v-for="(label, value) in form.executors" :key="value" :value="value">{{ label }}</option>
          </select>
          <input v-model="form.agent_overrides[agent].model" list="magpie-models" :aria-label="`${agent} model`" :placeholder="en ? 'Default model' : '跟随默认模型'">
        </div>
      </details>
      <div class="runtime-actions"><button type="submit" :disabled="busy">{{ en ? 'Save Agent settings' : '保存 Agent 配置' }}</button><button type="button" :disabled="busy || !form.api_key_set" @click="refreshModels">{{ en ? 'Refresh models' : '刷新模型列表' }}</button></div>
    </form>
    <p v-if="message" role="status">{{ message }}</p>
    <p v-if="error" role="alert" class="runtime-error">{{ error }}</p>
  </section>
</template>

<style scoped>
.runtime-card { padding: 20px; border: 1px solid var(--el-border-color, #e2e8f0); border-radius: 16px; background: var(--el-bg-color, white); }
h2 { font-size: 18px; font-weight: 600; }
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
