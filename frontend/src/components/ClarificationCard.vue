<template>
  <div class="rw-clarify" :class="{ 'rw-clarify--paused': pending.paused }">
    <div class="rw-clarify-head">
      <HelpCircle class="rw-clarify-icon" aria-hidden="true" />
      <div>
        <h3 class="rw-clarify-title">{{ title }}</h3>
        <p class="rw-clarify-sub">
          {{ t(subtitle) }}
        </p>
      </div>
    </div>

    <div
      class="rw-clarify-body"
      role="group"
      tabindex="0"
      :aria-label="publicationReview ? title : t('aiChat.clarification.questionsLabel', { count: pending.questions.length })"
    >
      <div
        v-for="(q, qi) in pending.questions"
        :key="qi"
        class="rw-clarify-q"
      >
        <div class="rw-clarify-q-head">
          <span v-if="q.header" class="rw-clarify-chip">{{ q.header }}</span>
          <span class="rw-clarify-q-text">{{ q.question }}</span>
        </div>

        <div class="rw-clarify-options">
          <button
            v-for="opt in q.options"
            :key="opt.label"
            type="button"
            class="rw-clarify-option"
            :class="{ 'is-selected': isSelected(qi, opt.label) }"
            :title="opt.description || ''"
            :disabled="pending.submitting"
            @click="toggleOption(qi, opt.label, q.multiSelect)"
          >
            <span class="rw-clarify-option-label">{{ opt.label }}</span>
            <span v-if="opt.description" class="rw-clarify-option-desc">{{ opt.description }}</span>
          </button>
        </div>

        <input
          v-if="q.allow_custom !== false"
          v-model="pending.draftCustom[qi]"
          type="text"
          class="rw-input rw-clarify-custom"
          :placeholder="t('aiChat.clarification.customPlaceholder')"
          :disabled="pending.submitting"
        />
      </div>

      <div v-if="pending.error" class="rw-clarify-error">{{ pending.error }}</div>
    </div>

    <div class="rw-clarify-actions">
      <button
        type="button"
        class="rw-btn-primary"
        :disabled="pending.submitting"
        @click="$emit('submit')"
      >
        {{ t(pending.submitting ? 'aiChat.clarification.submitting' : submitLabel) }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { HelpCircle } from 'lucide-vue-next'
import type { PendingClarification } from '@/types/agentTrace'

const { t } = useI18n()

const props = defineProps<{ pending: PendingClarification }>()
defineEmits<{ (e: 'submit'): void }>()

const publicationReview = computed(() => props.pending.purpose === 'package_build_confirmation')
const title = computed(() =>
  t(publicationReview.value ? 'aiChat.clarification.publicationTitle' : 'aiChat.clarification.title'),
)
const subtitle = computed(() => {
  if (publicationReview.value) return 'aiChat.clarification.publicationSubtitle'
  return props.pending.paused ? 'aiChat.clarification.pausedSubtitle' : 'aiChat.clarification.subtitle'
})
const submitLabel = computed(() => {
  if (publicationReview.value) return 'aiChat.clarification.publicationSubmit'
  return props.pending.paused ? 'aiChat.clarification.resume' : 'aiChat.clarification.submit'
})

const isSelected = (qi: number, label: string): boolean =>
  (props.pending.draftSelected[qi] || []).includes(label)

const toggleOption = (qi: number, label: string, multi?: boolean): void => {
  const current = props.pending.draftSelected[qi] || []
  if (multi) {
    props.pending.draftSelected[qi] = current.includes(label)
      ? current.filter((l) => l !== label)
      : [...current, label]
  } else {
    // Single-select: clicking the active option clears it; otherwise replace.
    props.pending.draftSelected[qi] = current.includes(label) ? [] : [label]
  }
}
</script>

<style scoped>
.rw-clarify {
  border: 1px solid var(--rw-hairline, #e5e5e5);
  border-radius: 12px;
  background: var(--rw-canvas, #fff);
  overflow: hidden;
  max-height: calc(100vh - 32px);
  display: flex;
  flex-direction: column;
}
.rw-clarify.rw-clarify--paused { max-height: min(45vh, 420px); }
.rw-clarify-head {
  display: flex;
  flex: 0 0 auto;
  gap: 12px;
  align-items: flex-start;
  padding: 14px 16px;
  border-bottom: 1px solid var(--rw-hairline, #e5e5e5);
}
.rw-clarify-icon { width: 20px; height: 20px; flex-shrink: 0; color: var(--rw-accent, #4f46e5); margin-top: 2px; }
.rw-clarify-title { font-size: 15px; font-weight: 600; margin: 0; color: var(--rw-ink, #171717); }
.rw-clarify-sub { font-size: 12px; margin: 2px 0 0; color: var(--rw-ink-soft, #6b7280); }
.rw-clarify-body {
  flex: 1 1 auto;
  min-height: 0;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}
.rw-clarify-q { display: flex; flex-direction: column; gap: 8px; }
.rw-clarify-q-head { display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
.rw-clarify-chip {
  font-size: 11px; font-weight: 600;
  padding: 2px 8px; border-radius: 999px;
  background: var(--rw-accent-soft, #eef2ff); color: var(--rw-accent, #4f46e5);
}
.rw-clarify-q-text {
  font-size: 14px;
  font-weight: 500;
  color: var(--rw-ink, #171717);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  min-width: 0;
}
.rw-clarify-options { display: flex; flex-direction: column; gap: 8px; }
.rw-clarify-option {
  display: flex; flex-direction: column; gap: 2px;
  text-align: left;
  padding: 10px 12px;
  border: 1px solid var(--rw-hairline, #e5e5e5);
  border-radius: 10px;
  background: var(--rw-canvas, #fff);
  cursor: pointer;
  transition: border-color 0.12s, background 0.12s;
}
.rw-clarify-option:hover:not(:disabled) { border-color: var(--rw-accent, #4f46e5); }
.rw-clarify-option.is-selected {
  border-color: var(--rw-accent, #4f46e5);
  background: var(--rw-accent-soft, #eef2ff);
}
.rw-clarify-option:disabled { opacity: 0.6; cursor: not-allowed; }
.rw-clarify-option-label { font-size: 13px; font-weight: 600; color: var(--rw-ink, #171717); }
.rw-clarify-option-desc { font-size: 12px; color: var(--rw-ink-soft, #6b7280); }
.rw-clarify-custom { margin-top: 2px; }
.rw-clarify-error { font-size: 12px; color: var(--rw-danger, #dc2626); }
.rw-clarify-actions {
  display: flex; justify-content: flex-end;
  flex: 0 0 auto;
  padding: 12px 16px;
  border-top: 1px solid var(--rw-hairline, #e5e5e5);
}
</style>
