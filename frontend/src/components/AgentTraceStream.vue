<template>
  <div v-if="hasContent" class="agent-trace">
    <div v-if="runtimeModel || loadedSkills.length" class="agent-trace__metadata">
      <div v-if="runtimeModel" class="agent-trace__runtime">{{ runtimeModel }}</div>
      <div v-if="loadedSkills.length" class="agent-trace__skills" :title="t('agentTrace.loadedSkillsTitle')">
        <span class="agent-trace__skills-label">{{ t('agentTrace.loadedSkillsLabel') }}</span>
        <span
          v-for="skill in loadedSkills"
          :key="skill"
          class="agent-trace__skill-pill"
          :title="skill"
        >
          {{ skill }}
        </span>
      </div>
    </div>

    <!-- Completed runs share one disclosure for the Skill catalog and trace. -->
    <button
      v-if="terminal"
      type="button"
      class="agent-trace__summary-row"
      :aria-expanded="summaryExpanded"
      @click="summaryExpanded = !summaryExpanded"
    >
      <span class="agent-trace__summary-icon" aria-hidden="true">
        <Check v-if="terminal.type === 'run_complete'" class="agent-trace__summary-icon-svg" />
        <Ban v-else-if="terminal.type === 'cancelled'" class="agent-trace__summary-icon-svg" />
        <AlertTriangle v-else class="agent-trace__summary-icon-svg" />
      </span>
      <span class="agent-trace__summary-text">
        {{ summaryLine }}
        <span v-if="availableSkills.length" class="agent-trace__summary-skills">
          · {{ t('agentTrace.availableSkillsLabel') }} {{ availableSkills.length }}
        </span>
      </span>
      <ChevronDown
        class="agent-trace__summary-chevron"
        :class="{ 'agent-trace__summary-chevron--open': summaryExpanded }"
        aria-hidden="true"
      />
    </button>

    <!-- Expanded list: running view, OR terminal view after user expands. -->
    <div v-if="!terminal || summaryExpanded" class="agent-trace__list">
      <component
        :is="terminal ? 'section' : 'details'"
        v-if="availableSkills.length"
        class="agent-trace__available-skills"
        :title="t('agentTrace.availableSkillsTitle')"
        :aria-label="t('agentTrace.availableSkillsLabel')"
      >
        <component :is="terminal ? 'h3' : 'summary'" class="agent-trace__available-summary">
          <span class="agent-trace__skills-label">{{ t('agentTrace.availableSkillsLabel') }}</span>
          <span class="agent-trace__available-count">{{ availableSkills.length }}</span>
          <ChevronDown v-if="!terminal" class="agent-trace__available-chevron" aria-hidden="true" />
        </component>
        <div class="agent-trace__skills agent-trace__skills--available">
          <span v-for="skill in availableSkills" :key="skill" class="agent-trace__skill-pill" :title="skill">
            {{ skill }}
          </span>
        </div>
      </component>

      <section v-if="cards.length" class="agent-trace__steps" :aria-label="t('agentTrace.toolCallsLabel')">
        <h3 v-if="terminal" class="agent-trace__section-title">{{ t('agentTrace.toolCallsLabel') }}</h3>
        <TraceStepCard
          v-for="card in cards"
          :key="card.stepId"
          :card="card"
          :display-name="toolDisplay.resolve(card.toolName, card.toolInput)"
        />
      </section>

      <div v-if="running && cards.length === 0" class="agent-trace__placeholder">
        <span class="agent-trace__placeholder-label">{{ t('agentTrace.thinking') }}</span>
        <span class="agent-trace__placeholder-dots" aria-hidden="true">
          <span /><span /><span />
        </span>
      </div>

      <section
        v-if="!running && (completionSummary || visualAnalysis)"
        class="agent-trace__completion"
        :aria-label="t('agentTrace.runSummary')"
      >
        <div class="agent-trace__completion-head">
          <span class="agent-trace__completion-mark" aria-hidden="true"><Check /></span>
          <div>
            <div class="agent-trace__completion-eyebrow">{{ t('agentTrace.runSummary') }}</div>
            <div class="agent-trace__completion-title">
              {{ completionSummary?.title || t('agentTrace.completed') }}
            </div>
          </div>
        </div>

        <dl v-if="completionSummary?.fields.length" class="agent-trace__completion-grid">
          <div
            v-for="field in completionSummary.fields"
            :key="`${field.label}:${field.value}`"
            class="agent-trace__completion-field"
          >
            <dt>{{ field.label }}</dt>
            <dd>{{ field.value }}</dd>
          </div>
        </dl>

        <div v-if="visualAnalysis" class="agent-trace__visual">
          <div class="agent-trace__visual-label">
            {{ t('agentTrace.visualAnalysis', { count: visualImageCount || 1 }) }}
          </div>
          <div class="agent-trace__visual-content">{{ visualAnalysis }}</div>
        </div>
      </section>

      <div v-if="running && props.onCancel" class="agent-trace__actions">
        <button
          type="button"
          class="agent-trace__cancel"
          :disabled="cancelling"
          @click="onCancelClick"
        >
          {{ cancelling ? t('agentTrace.cancelling') : t('agentTrace.cancel') }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, toRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { AlertTriangle, Ban, Check, ChevronDown } from 'lucide-vue-next'
import type { AgentTraceEvent, TraceSummary } from '@/types/agentTrace'
import type { AgentCompletionSummary } from '@/utils/agentResultSummary'
import {
  collectAvailableSkills,
  collectLoadedSkills,
  useAgentTraceStream,
} from '@/composables/useAgentTraceStream'
import { useToolDisplayName, type ToolNameMap } from '@/composables/useToolDisplayName'
import TraceStepCard from './TraceStepCard.vue'
import { collectRuntime } from '@/utils/agentRuntime'

const { t } = useI18n()

const props = defineProps<{
  events: AgentTraceEvent[]
  running?: boolean
  toolNameMap?: ToolNameMap
  onCancel?: () => void | Promise<void>
  completionSummary?: AgentCompletionSummary | null
  visualAnalysis?: string | null
  visualImageCount?: number
}>()

const eventsRef = toRef(props, 'events')
const runtime = computed(() => collectRuntime(props.events || []))
const runtimeModel = computed(() => {
  const route = runtime.value.routes[runtime.value.routes.length - 1]
  const model = route?.model || props.completionSummary?.model || ''
  const providerPrefix = route ? `${route.provider}/` : ''
  return providerPrefix && model.startsWith(providerPrefix) ? model.slice(providerPrefix.length) : model
})
const { cards, running: streamRunning, summary, terminal } = useAgentTraceStream(eventsRef)
const toolDisplay = useToolDisplayName(props.toolNameMap)

// `running` prop wins when explicitly provided; otherwise infer from the
// presence of a terminal event in the stream.
const running = computed(() => {
  if (props.running !== undefined) return props.running && !terminal.value
  return streamRunning.value
})

const hasContent = computed(
  () =>
    cards.value.length > 0 ||
    running.value ||
    !!terminal.value ||
    availableSkills.value.length > 0 ||
    loadedSkills.value.length > 0 ||
    !!props.completionSummary ||
    !!props.visualAnalysis,
)

const summaryExpanded = ref(false)

const availableSkills = computed(() => collectAvailableSkills(props.events || []))
const loadedSkills = computed(() => collectLoadedSkills(props.events || []))

const cancelling = ref(false)
watch(terminal, (term: unknown) => {
  if (term) {
    cancelling.value = false
    summaryExpanded.value = false
  }
})

async function onCancelClick() {
  if (!props.onCancel || cancelling.value) return
  cancelling.value = true
  try {
    await props.onCancel()
  } catch {
    cancelling.value = false
  }
}

function formatDuration(secs: number | undefined | null): string {
  if (typeof secs !== 'number' || !Number.isFinite(secs)) return '0.0s'
  return `${secs.toFixed(1)}s`
}

const summaryLine = computed(() => {
  const term = terminal.value
  if (!term) return ''
  const s: TraceSummary | null = summary.value
  const toolCalls = s?.tool_call_count ?? 0
  const duration = s?.thought_duration_seconds ?? 0
  if (term.type === 'run_complete') {
    return `Thought for ${formatDuration(duration)} · ${toolCalls} tool calls`
  }
  if (term.type === 'cancelled') {
    return `Cancelled after ${formatDuration(duration)} · ${toolCalls} tool calls`
  }
  return `Failed after ${formatDuration(duration)} · ${toolCalls} tool calls`
})
</script>

<style scoped>
.agent-trace__metadata,
.agent-trace__runtime {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
  min-width: 0;
  font-size: 12px;
  color: var(--el-text-color-secondary, #64748b);
  overflow-wrap: anywhere;
}

.agent-trace__metadata {
  column-gap: 16px;
}

.agent-trace__runtime + .agent-trace__skills {
  min-width: 0;
}
.agent-trace {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 13px;
  color: var(--el-text-color-primary, #111827);
}

.agent-trace__list,
.agent-trace__steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.agent-trace__skills {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 2px 0;
}

.agent-trace__available-skills {
  padding: 2px 0;
}

.agent-trace__available-summary {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--el-text-color-secondary, #6b7280);
  margin: 0;
  font: inherit;
  list-style: none;
  user-select: none;
}

summary.agent-trace__available-summary {
  cursor: pointer;
}

.agent-trace__section-title {
  margin: 0;
  font-size: 12px;
  font-weight: 500;
  color: var(--el-text-color-secondary, #6b7280);
}

section.agent-trace__available-skills + .agent-trace__steps {
  padding-top: 10px;
  border-top: 1px solid var(--el-border-color-lighter, #e5e7eb);
}

.agent-trace__available-summary::-webkit-details-marker {
  display: none;
}

.agent-trace__available-count {
  display: inline-grid;
  min-width: 20px;
  height: 20px;
  padding: 0 5px;
  place-items: center;
  border-radius: 999px;
  background: var(--el-fill-color-light, #f5f7fa);
  color: var(--el-text-color-regular, #4b5563);
  font-size: 11px;
  line-height: 1;
}

.agent-trace__available-chevron {
  width: 13px;
  height: 13px;
  transition: transform 0.15s ease;
}

.agent-trace__available-skills[open] .agent-trace__available-chevron {
  transform: rotate(180deg);
}

.agent-trace__skills--available {
  padding-top: 7px;
}

.agent-trace__skills-label {
  font-size: 12px;
  color: var(--el-text-color-secondary, #6b7280);
}

.agent-trace__skill-pill {
  display: inline-flex;
  align-items: center;
  max-width: 220px;
  height: 22px;
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid var(--el-border-color-lighter, #e5e7eb);
  background: var(--el-fill-color-light, #f5f7fa);
  color: var(--el-text-color-regular, #4b5563);
  font-size: 12px;
  line-height: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-trace__summary-row {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--el-border-color-lighter, #e5e7eb);
  background: var(--el-fill-color-light, #f5f7fa);
  cursor: pointer;
  font: inherit;
  color: inherit;
  text-align: left;
}

.agent-trace__summary-row:hover {
  border-color: var(--el-border-color, #d1d5db);
}

.agent-trace__summary-row:focus-visible {
  outline: 2px solid var(--el-color-primary, #409eff);
  outline-offset: 2px;
}

.agent-trace__summary-icon-svg {
  width: 16px;
  height: 16px;
}

.agent-trace__summary-text {
  flex: 1;
  min-width: 0;
  font-weight: 500;
}

.agent-trace__summary-skills {
  display: inline-block;
  font-weight: 400;
  color: var(--el-text-color-secondary, #6b7280);
}

.agent-trace__summary-chevron {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  transition: transform 0.15s ease;
}

.agent-trace__summary-chevron--open {
  transform: rotate(180deg);
}

.agent-trace__placeholder {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--el-text-color-secondary, #6b7280);
  font-size: 13px;
  padding: 6px 4px;
}

.agent-trace__placeholder-label {
  font-weight: 500;
}

.agent-trace__completion {
  position: relative;
  overflow: hidden;
  margin-top: 2px;
  padding: 14px;
  border: 1px solid var(--el-border-color-lighter, #e5e7eb);
  border-radius: 10px;
  background:
    linear-gradient(135deg, color-mix(in srgb, var(--el-color-success, #16a34a) 7%, transparent), transparent 42%),
    var(--el-bg-color, #fff);
}

.agent-trace__completion::after {
  content: '';
  position: absolute;
  top: 0;
  right: 0;
  width: 72px;
  height: 72px;
  border-radius: 0 0 0 72px;
  background: color-mix(in srgb, var(--el-color-success, #16a34a) 5%, transparent);
  pointer-events: none;
}

.agent-trace__completion-head {
  position: relative;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 10px;
}

.agent-trace__completion-mark {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  flex: 0 0 28px;
  border-radius: 8px;
  color: var(--el-color-success-dark-2, #15803d);
  background: color-mix(in srgb, var(--el-color-success, #16a34a) 13%, transparent);
}

.agent-trace__completion-mark svg {
  width: 15px;
  height: 15px;
  stroke-width: 2.2;
}

.agent-trace__completion-eyebrow {
  margin-bottom: 2px;
  color: var(--el-text-color-secondary, #6b7280);
  font-size: 10px;
  font-weight: 650;
  letter-spacing: .08em;
  line-height: 1.2;
  text-transform: uppercase;
}

.agent-trace__completion-title {
  color: var(--el-text-color-primary, #111827);
  font-size: 13px;
  font-weight: 650;
}

.agent-trace__completion-grid {
  display: grid;
  grid-template-columns: minmax(82px, .32fr) minmax(0, 1fr);
  gap: 0;
  margin: 12px 0 0;
  padding-top: 8px;
  border-top: 1px solid var(--el-border-color-lighter, #e5e7eb);
}

.agent-trace__completion-field {
  display: contents;
}

.agent-trace__completion-field dt,
.agent-trace__completion-field dd {
  margin: 0;
  padding: 4px 0;
  line-height: 1.45;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.agent-trace__completion-field dt {
  padding-right: 12px;
  color: var(--el-text-color-secondary, #6b7280);
  font-size: 11.5px;
}

.agent-trace__completion-field dd {
  color: var(--el-text-color-regular, #374151);
  font-size: 12px;
}

.agent-trace__visual {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--el-border-color-lighter, #e5e7eb);
}

.agent-trace__visual-label {
  margin-bottom: 6px;
  color: var(--el-text-color-secondary, #6b7280);
  font-size: 11.5px;
  font-weight: 600;
}

.agent-trace__visual-content {
  max-height: 220px;
  overflow: auto;
  padding: 9px 10px;
  border-radius: 7px;
  background: var(--el-fill-color-light, #f5f7fa);
  color: var(--el-text-color-regular, #374151);
  font-family: var(--rw-mono, ui-monospace, monospace);
  font-size: 11.5px;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 560px) {
  .agent-trace__completion-grid {
    grid-template-columns: 72px minmax(0, 1fr);
  }
}

.agent-trace__placeholder-dots {
  display: inline-flex;
  align-items: center;
  gap: 3px;
}

.agent-trace__placeholder-dots span {
  display: inline-block;
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background-color: currentColor;
  animation: agent-trace-bounce 1.2s ease-in-out infinite;
}

.agent-trace__placeholder-dots span:nth-child(1) { animation-delay: 0s; }
.agent-trace__placeholder-dots span:nth-child(2) { animation-delay: 0.2s; }
.agent-trace__placeholder-dots span:nth-child(3) { animation-delay: 0.4s; }

@keyframes agent-trace-bounce {
  0%, 60%, 100% { transform: translateY(0); opacity: 0.4; }
  30% { transform: translateY(-4px); opacity: 1; }
}

.agent-trace__actions {
  padding: 4px 0;
}

.agent-trace__cancel {
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 6px;
  background: transparent;
  border: 1px solid var(--el-border-color, #d1d5db);
  color: var(--el-text-color-regular, #4b5563);
  cursor: pointer;
}

.agent-trace__cancel:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.agent-trace__cancel:hover:not(:disabled) {
  background: var(--el-fill-color-light, #f5f7fa);
}
</style>
