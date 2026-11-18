<template>
  <div
    ref="root"
    class="rw-ai-text"
    :class="{ 'rw-ai-text--streaming': busy }"
    :aria-busy="busy"
  >
    <div v-html="html" @click="onMarkdownClick"></div>
    <span v-if="busy" class="answer-cursor" aria-hidden="true"></span>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { useProgressiveText } from '@/composables/useProgressiveText'
import { copyToClipboard } from '@/utils'
import { processMermaidBlocks, renderMarkdown } from '@/utils/markdownRenderer'

const props = defineProps<{ content: string; animate?: boolean; running?: boolean }>()
const emit = defineEmits<{ progress: []; settled: []; busy: [value: boolean] }>()
const { t } = useI18n()
const root = ref<HTMLElement | null>(null)
const reducedMotion = ref(false)
let motionQuery: MediaQueryList | undefined
const updateMotion = () => { reducedMotion.value = !!motionQuery?.matches }
onMounted(() => {
  motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  updateMotion()
  motionQuery.addEventListener('change', updateMotion)
})
onUnmounted(() => {
  motionQuery?.removeEventListener('change', updateMotion)
  emit('busy', false)
})
const source = computed(() => props.content)
const animate = computed(() => !!props.animate && !reducedMotion.value)
const { displayed, pending } = useProgressiveText(source, animate)
const busy = computed(() => pending.value || (!!props.running && !!props.content))
// A computed HTML string stays stable across unrelated parent updates, so
// completed Mermaid SVGs are not removed by repeated v-html assignments.
const html = computed(() => displayed.value
  ? renderMarkdown(displayed.value, { wrapperClass: 'markdown-content text-ink', copyCode: true })
  : '')

async function onMarkdownClick(event: MouseEvent) {
  if (!(event.target instanceof Element)) return
  const button = event.target.closest('.code-copy-btn')
  const code = button?.parentElement?.querySelector('pre > code')?.textContent
  if (code == null) return

  const copied = await copyToClipboard(code)
  ElMessage({
    message: t(copied ? 'aiChat.code.copied' : 'aiChat.code.copyFailed'),
    type: copied ? 'success' : 'error',
    grouping: true,
  })
}

watch([displayed, busy], async () => {
  emit('busy', busy.value)
  emit('progress')
  if (props.content && !busy.value && !props.running) emit('settled')
  await nextTick()
  // Wait until the text settles before rendering expensive/incomplete diagrams.
  if (!busy.value && root.value) void processMermaidBlocks(root.value)
}, { immediate: true, flush: 'post' })
</script>

<style scoped>
:deep(.markdown-code-block) {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  margin: 10px 0;
  border-radius: 8px;
  background: var(--rw-surface-dark, #0d1117);
  box-shadow: 0 1px 2px rgb(0 0 0 / 0.08);
}

.rw-ai-text :deep(.markdown-code-block > pre) {
  margin: 0;
  box-shadow: none;
}

:deep(.code-copy-btn) {
  position: relative;
  width: 32px;
  height: 32px;
  margin: 6px 6px 0 0;
  border-radius: 6px;
  color: #d4d4d8;
  cursor: pointer;
}

:deep(.code-copy-btn:hover),
:deep(.code-copy-btn:focus-visible) {
  background: #ffffff26;
  color: #fff;
}

:deep(.code-copy-btn:focus-visible) {
  outline: 2px solid currentColor;
  outline-offset: -2px;
}

:deep(.code-copy-btn)::before,
:deep(.code-copy-btn)::after {
  position: absolute;
  content: '';
  border: 1.5px solid currentColor;
  border-radius: 2px;
  width: 10px;
  height: 11px;
}

:deep(.code-copy-btn)::before {
  top: 8px;
  left: 9px;
  border-bottom-color: transparent;
  border-right-color: transparent;
}

:deep(.code-copy-btn)::after {
  top: 12px;
  left: 13px;
}

.answer-cursor {
  display: inline-block;
  width: 6px;
  height: 15px;
  margin-left: 3px;
  vertical-align: text-bottom;
  border-radius: 2px;
  background: currentColor;
  opacity: .65;
  animation: answer-blink 1s step-end infinite;
}
@keyframes answer-blink { 50% { opacity: .15; } }
@media (prefers-reduced-motion: reduce) {
  .answer-cursor { animation: none; }
}
</style>
