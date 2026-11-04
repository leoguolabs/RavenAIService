<template>
  <div
    ref="root"
    class="rw-ai-text"
    :class="{ 'rw-ai-text--streaming': busy }"
    :aria-busy="busy"
  >
    <div v-html="html"></div>
    <span v-if="busy" class="answer-cursor" aria-hidden="true"></span>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useProgressiveText } from '@/composables/useProgressiveText'
import { processMermaidBlocks, renderMarkdown } from '@/utils/markdownRenderer'

const props = defineProps<{ content: string; animate?: boolean; running?: boolean }>()
const emit = defineEmits<{ progress: []; settled: []; busy: [value: boolean] }>()
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
  ? renderMarkdown(displayed.value, { wrapperClass: 'markdown-content text-ink' })
  : '')
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
