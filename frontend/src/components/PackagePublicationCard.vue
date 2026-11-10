<template>
  <section class="rw-publication" :class="{ 'is-modal': review.warnings.length }" :aria-label="title">
    <header class="rw-publication-header">
      <h3>{{ title }}</h3>
      <strong>{{ review.project_name }}</strong>
      <span v-if="review.project_code !== review.project_name"> ({{ review.project_code }})</span>
      <span> · {{ review.version }} · {{ t(`aiChat.publication.${review.mode}`) }}</span>
    </header>
    <div class="rw-publication-body" tabindex="0">
      <ul v-if="review.warnings.length" class="rw-publication-warnings" :aria-label="t('aiChat.publication.warnings')">
        <li v-for="(warning, index) in review.warnings" :key="index">{{ warning }}</li>
      </ul>
      <ul :aria-label="t('aiChat.publication.components')">
        <li v-for="(component, index) in review.components" :key="index">
          {{ component.name }} <span>· {{ component.version }}</span>
        </li>
      </ul>
      <template v-if="review.exclusions.length">
        <h4>{{ t('aiChat.publication.exclusions') }}</h4>
        <ul>
          <li v-for="(item, index) in review.exclusions" :key="index">
            {{ item.name }} <span>· {{ item.reason }}</span>
          </li>
        </ul>
      </template>
    </div>
    <footer class="rw-publication-actions">
      <p v-if="error" role="alert">{{ error }}</p>
      <button type="button" :disabled="submitting" @click="$emit('decide', 'cancel')">
        {{ t('aiChat.publication.cancel') }}
      </button>
      <button type="button" class="is-primary" :disabled="submitting" @click="$emit('decide', 'confirm')">
        {{ t(submitting ? 'aiChat.clarification.submitting' : 'aiChat.publication.confirm') }}
      </button>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { PackagePublicationReview } from '@/types/agentTrace'

const props = defineProps<{ review: PackagePublicationReview; submitting?: boolean; error?: string | null }>()
defineEmits<{ decide: [decision: 'confirm' | 'cancel'] }>()

const { t } = useI18n()
const title = computed(() => t(props.review.warnings.length
  ? 'aiChat.clarification.publicationTitle'
  : 'aiChat.publication.ready'))
</script>

<style scoped>
.rw-publication {
  display: flex;
  flex-direction: column;
  max-height: min(40vh, 340px);
  border: 1px solid var(--rw-hairline, #e5e5e5);
  border-radius: 12px;
  background: var(--rw-canvas, #fff);
  color: var(--rw-ink, #171717);
  font-size: 13px;
}
.rw-publication.is-modal { max-height: calc(100vh - 48px); border: 0; }
.rw-publication-header, .rw-publication-body, .rw-publication-actions { padding: 10px 14px; }
.rw-publication-header, .rw-publication-actions { flex: 0 0 auto; }
.rw-publication-header h3 { margin: 0 0 4px; font-size: 14px; }
.rw-publication-header, .rw-publication-body { overflow-wrap: anywhere; }
.rw-publication-body { min-height: 0; overflow-y: auto; border-block: 1px solid var(--rw-hairline, #e5e5e5); }
.rw-publication-body ul { margin: 0; padding-inline-start: 18px; list-style: disc; }
.rw-publication-body h4 { margin: 8px 0 2px; font-weight: 600; }
.rw-publication span { color: var(--rw-ink-soft, #555); }
.rw-publication-body .rw-publication-warnings {
  margin-bottom: 8px;
  color: var(--rw-ink, #171717);
  font-weight: 600;
}
.rw-publication-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.rw-publication-actions p { flex-basis: 100%; margin: 0; color: var(--rw-danger, #b42318); }
.rw-publication-actions button {
  padding: 5px 10px;
  border: 1px solid var(--rw-hairline, #e5e5e5);
  border-radius: 6px;
  background: var(--rw-canvas, #fff);
  cursor: pointer;
}
.rw-publication-actions button.is-primary { background: var(--rw-primary, #171717); color: var(--rw-on-primary, #fff); }
.rw-publication-actions button:disabled { opacity: .6; cursor: default; }
</style>
