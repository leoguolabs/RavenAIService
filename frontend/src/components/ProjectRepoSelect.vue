<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ProjectRepoOption } from '@/api'
import { filterProjectRepoOptions } from '@/utils/projectRepoSearch'

const PREVIEW_DELAY_MS = 3000

const props = defineProps<{
  modelValue: number | null
  options: ProjectRepoOption[]
  loading?: boolean
  required?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: number | null]
}>()

const { t } = useI18n()
const rootRef = ref<HTMLElement | null>(null)
const inputRef = ref<HTMLInputElement | null>(null)
const menuRef = ref<HTMLElement | null>(null)
const listboxId = `project-repo-listbox-${useId()}`
const optionId = (option: ProjectRepoOption) => `${listboxId}-option-${option.id}`
const isOpen = ref(false)
const searchQuery = ref('')
const activeIndex = ref(-1)
const activeSource = ref<'keyboard' | 'pointer' | null>(null)
const previewOption = ref<ProjectRepoOption | null>(null)
let previewTimer: ReturnType<typeof setTimeout> | null = null

const selectedOption = computed(() =>
  props.options.find((option) => option.id === props.modelValue) || null,
)
const placeholder = computed(() =>
  props.loading
    ? t('aiChat.project.loading')
    : props.required
      ? t('aiChat.project.requiredPlaceholder')
      : t('aiChat.project.optionalPlaceholder'),
)
const filteredOptions = computed(() => filterProjectRepoOptions(props.options, searchQuery.value))
const activeOption = computed(() => filteredOptions.value[activeIndex.value])
const inputValue = computed({
  get: () => isOpen.value ? searchQuery.value : selectedOption.value?.project_name ?? '',
  set: (value: string) => {
    openMenu()
    searchQuery.value = value
  },
})

const clearPreviewTimer = () => {
  if (previewTimer !== null) {
    clearTimeout(previewTimer)
    previewTimer = null
  }
}

const resetPreview = () => {
  clearPreviewTimer()
  previewOption.value = null
}

const closeMenu = () => {
  isOpen.value = false
  searchQuery.value = ''
  activeIndex.value = -1
  activeSource.value = null
  resetPreview()
}

const openMenu = () => {
  if (props.loading || isOpen.value) return
  isOpen.value = true
  activeIndex.value = filteredOptions.value.findIndex((option) => option.id === props.modelValue)
  activeSource.value = null
  resetPreview()
  void scrollActiveOptionIntoView()
}

const toggleMenu = () => {
  if (isOpen.value) closeMenu()
  else openMenu()
  inputRef.value?.focus()
}

const selectOption = (option: ProjectRepoOption) => {
  emit('update:modelValue', option.id)
  closeMenu()
}

const clearSelection = () => {
  emit('update:modelValue', null)
  closeMenu()
}

const scrollActiveOptionIntoView = async () => {
  await nextTick()
  const active = rootRef.value?.querySelector<HTMLElement>('[data-project-option-active="true"]')
  active?.scrollIntoView({ block: 'nearest' })
}

const armPreview = (index: number, source: 'keyboard' | 'pointer') => {
  const option = filteredOptions.value[index]
  if (!option) return

  activeIndex.value = index
  activeSource.value = source
  resetPreview()
  if (option.project_card.trim()) {
    previewTimer = setTimeout(() => {
      if (isOpen.value && activeIndex.value === index && activeSource.value === source) {
        previewOption.value = option
      }
      previewTimer = null
    }, PREVIEW_DELAY_MS)
  }
}

const showPreviewImmediately = (index: number) => {
  const option = filteredOptions.value[index]
  if (!option?.project_card.trim()) return

  clearPreviewTimer()
  activeIndex.value = index
  activeSource.value = 'pointer'
  previewOption.value = option
}

const moveActiveOption = (direction: 1 | -1) => {
  openMenu()
  const optionCount = filteredOptions.value.length
  if (!optionCount) return

  let nextIndex = activeIndex.value
  if (nextIndex < 0) {
    nextIndex = direction === 1 ? 0 : optionCount - 1
  } else {
    nextIndex = (nextIndex + direction + optionCount) % optionCount
  }
  armPreview(nextIndex, 'keyboard')
  void scrollActiveOptionIntoView()
}

const handleKeydown = (event: KeyboardEvent) => {
  if (event.isComposing) return

  switch (event.key) {
    case 'ArrowDown':
      event.preventDefault()
      moveActiveOption(1)
      break
    case 'ArrowUp':
      event.preventDefault()
      moveActiveOption(-1)
      break
    case 'Enter':
      event.preventDefault()
      if (!isOpen.value) {
        openMenu()
      } else if (activeOption.value) {
        selectOption(activeOption.value)
      }
      break
    case 'Escape':
      if (!isOpen.value) return
      event.preventDefault()
      closeMenu()
      void nextTick(() => inputRef.value?.select())
      break
  }
}

const handleOptionLeave = (index: number) => {
  if (activeIndex.value !== index || activeSource.value !== 'pointer') return
  // Once visible, keep the card stable so the pointer can move onto it for reading.
  if (!previewOption.value) clearPreviewTimer()
}

const handleDocumentPointerDown = (event: PointerEvent) => {
  if (!rootRef.value?.contains(event.target as Node)) closeMenu()
}

const handleFocusOut = (event: FocusEvent) => {
  if (!rootRef.value?.contains(event.relatedTarget as Node | null)) closeMenu()
}

watch([filteredOptions, searchQuery], () => {
  if (!isOpen.value) return
  activeIndex.value = searchQuery.value.trim() && filteredOptions.value.length ? 0 : -1
  activeSource.value = null
  resetPreview()
  if (menuRef.value) menuRef.value.scrollTop = 0
}, { deep: true })

watch(() => props.options, () => {
  if (!props.options.some((option) => option.id === props.modelValue) && props.modelValue !== null) {
    closeMenu()
  }
}, { deep: true })

onMounted(() => document.addEventListener('pointerdown', handleDocumentPointerDown))
onUnmounted(() => {
  document.removeEventListener('pointerdown', handleDocumentPointerDown)
  clearPreviewTimer()
})
</script>

<template>
  <div
    ref="rootRef"
    class="rw-project-picker"
    :class="{ required: required && modelValue === null, open: isOpen }"
    @focusout="handleFocusOut"
  >
    <div class="rw-project-select" :class="{ loading }">
      <input
        ref="inputRef"
        v-model="inputValue"
        type="text"
        class="rw-project-input"
        role="combobox"
        autocomplete="off"
        aria-autocomplete="list"
        :aria-expanded="isOpen"
        :aria-controls="isOpen ? listboxId : undefined"
        :aria-activedescendant="isOpen && activeOption ? optionId(activeOption) : undefined"
        :aria-label="t('aiChat.project.selectorLabel')"
        :aria-required="required || undefined"
        :placeholder="isOpen ? t('aiChat.project.searchPlaceholder') : placeholder"
        :disabled="loading"
        @focus="inputRef?.select()"
        @click="openMenu"
        @keydown="handleKeydown"
      />
      <button
        type="button"
        class="rw-project-toggle"
        tabindex="-1"
        :aria-label="t('aiChat.project.selectorLabel')"
        :aria-expanded="isOpen"
        :aria-controls="isOpen ? listboxId : undefined"
        :disabled="loading"
        @mousedown.prevent
        @click="toggleMenu"
      >
        <svg class="rw-project-chevron" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="m6 9 6 6 6-6" />
        </svg>
      </button>
    </div>

    <div v-if="isOpen" ref="menuRef" class="rw-project-menu">
      <div :id="listboxId" role="listbox" :aria-label="t('aiChat.project.selectorLabel')">
        <button
          v-if="!required && modelValue !== null && !searchQuery.trim()"
          type="button"
          class="rw-project-option rw-project-option--clear"
          role="option"
          tabindex="-1"
          :aria-selected="false"
          @mousedown.prevent
          @click="clearSelection"
        >
          {{ t('aiChat.project.optionalPlaceholder') }}
        </button>
        <button
          v-for="(option, index) in filteredOptions"
          :id="optionId(option)"
          :key="option.id"
          type="button"
          class="rw-project-option"
          :class="{
            active: activeIndex === index,
            selected: modelValue === option.id,
          }"
          role="option"
          tabindex="-1"
          :aria-selected="modelValue === option.id"
          :data-project-option-active="activeIndex === index"
          @mouseenter="armPreview(index, 'pointer')"
          @mouseleave="handleOptionLeave(index)"
          @mousedown.prevent
          @click="selectOption(option)"
        >
          <span class="rw-project-option__content">
            <span class="rw-project-option__name">{{ option.project_name }}</span>
            <span
              class="rw-project-help"
              aria-hidden="true"
              @mouseenter.stop="showPreviewImmediately(index)"
              @click.stop.prevent
            >?</span>
          </span>
          <svg v-if="modelValue === option.id" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="m5 12 4 4L19 6" />
          </svg>
        </button>
      </div>
      <div v-if="!filteredOptions.length" class="rw-project-empty" role="status">
        {{ t('aiChat.project.noMatches') }}
      </div>
    </div>

    <aside
      v-if="isOpen && previewOption"
      class="rw-project-preview"
      role="status"
      aria-live="polite"
    >
      <div class="rw-project-preview__eyebrow">{{ t('aiChat.project.cardPreview') }}</div>
      <div class="rw-project-preview__head">
        <strong>{{ previewOption.project_name }}</strong>
        <span>{{ previewOption.project_code }}</span>
      </div>
      <p>{{ previewOption.project_card }}</p>
    </aside>
  </div>
</template>

<style scoped>
.rw-project-picker {
  position: relative;
  min-width: 0;
}

.rw-project-select {
  width: 220px;
  height: 28px;
  display: flex;
  align-items: center;
  border: 1px solid var(--rw-hairline-strong);
  background: var(--rw-canvas);
  color: var(--rw-ink);
  border-radius: 999px;
  font: inherit;
  font-size: 12.5px;
  font-weight: 500;
  transition: border-color .15s, box-shadow .15s;
}

.rw-project-input {
  flex: 1;
  min-width: 0;
  height: 100%;
  padding: 0 0 0 11px;
  border: 0;
  outline: none;
  border-radius: inherit;
  background: transparent;
  color: inherit;
  font: inherit;
  text-overflow: ellipsis;
}

.rw-project-input::placeholder { color: var(--rw-muted); opacity: 1; }
.rw-project-select:hover { border-color: var(--rw-ink); }
.rw-project-select:focus-within {
  border-color: var(--rw-ink);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--rw-ink) 12%, transparent);
}
.rw-project-select.loading { opacity: .6; }
.rw-project-select :disabled { cursor: not-allowed; }
.rw-project-picker.required .rw-project-select { border-color: var(--rw-danger, #b91c1c); }
.rw-project-toggle {
  flex: 0 0 auto;
  height: 100%;
  padding: 0 9px 0 8px;
  border: 0;
  border-radius: inherit;
  background: transparent;
  color: inherit;
  cursor: pointer;
}
.rw-project-chevron { transition: transform .16s ease; }
.rw-project-picker.open .rw-project-chevron { transform: rotate(180deg); }

.rw-project-menu {
  position: absolute;
  right: 0;
  bottom: calc(100% + 8px);
  z-index: 31;
  width: max(100%, 240px);
  max-height: 264px;
  overflow-y: auto;
  padding: 5px;
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 11px;
  background: var(--rw-canvas);
  box-shadow: 0 18px 44px rgba(0, 0, 0, .14), 0 3px 12px rgba(0, 0, 0, .08);
  animation: project-menu-in .14s ease-out;
}

.rw-project-option {
  width: 100%;
  min-height: 34px;
  padding: 7px 9px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  border: 0;
  border-radius: 7px;
  background: transparent;
  color: var(--rw-ink);
  font: inherit;
  font-size: 12.5px;
  line-height: 1.35;
  text-align: left;
  cursor: pointer;
}

.rw-project-option__content {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 5px;
}
.rw-project-option__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rw-project-help {
  width: 15px;
  height: 15px;
  flex: 0 0 auto;
  display: inline-grid;
  place-items: center;
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 50%;
  background: var(--rw-canvas);
  color: var(--rw-muted);
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
  cursor: help;
  transition: border-color .12s, background .12s, color .12s, transform .12s;
}
.rw-project-help:hover {
  border-color: var(--rw-ink);
  background: var(--rw-ink);
  color: var(--rw-on-ink);
  transform: scale(1.06);
}
.rw-project-option.active { background: var(--rw-surface-strong); }
.rw-project-option.selected { font-weight: 600; }
.rw-project-option--clear {
  margin-bottom: 4px;
  color: var(--rw-muted);
  border-bottom: 1px solid var(--rw-hairline-strong);
  border-radius: 7px 7px 3px 3px;
}
.rw-project-empty {
  padding: 8px 9px;
  color: var(--rw-muted);
  font-size: 12.5px;
}

.rw-project-preview {
  position: absolute;
  right: calc(100% + 10px);
  bottom: calc(100% + 8px);
  z-index: 32;
  width: min(340px, calc(100vw - 32px));
  padding: 15px 16px 16px;
  border: 1px solid var(--rw-hairline-strong);
  border-radius: 13px;
  background: var(--rw-canvas);
  color: var(--rw-ink);
  box-shadow: 0 22px 56px rgba(0, 0, 0, .16), 0 4px 14px rgba(0, 0, 0, .08);
  animation: project-preview-in .18s ease-out;
}

.rw-project-preview::after {
  content: '';
  position: absolute;
  right: -5px;
  bottom: 16px;
  width: 9px;
  height: 9px;
  border-top: 1px solid var(--rw-hairline-strong);
  border-right: 1px solid var(--rw-hairline-strong);
  background: var(--rw-canvas);
  transform: rotate(45deg);
}
.rw-project-preview__eyebrow {
  margin-bottom: 7px;
  color: var(--rw-muted);
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: .12em;
  text-transform: uppercase;
}
.rw-project-preview__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.rw-project-preview__head strong {
  min-width: 0;
  font-size: 14px;
  line-height: 1.35;
}
.rw-project-preview__head span {
  flex: 0 0 auto;
  padding: 2px 6px;
  border-radius: 5px;
  background: var(--rw-surface-strong);
  color: var(--rw-muted);
  font-family: var(--rw-mono);
  font-size: 10.5px;
}
.rw-project-preview p {
  margin: 9px 0 0;
  color: var(--rw-body);
  font-size: 12.5px;
  line-height: 1.65;
  white-space: pre-wrap;
}

@keyframes project-menu-in {
  from { opacity: 0; transform: translateY(4px) scale(.985); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
@keyframes project-preview-in {
  from { opacity: 0; transform: translateX(5px) scale(.985); }
  to { opacity: 1; transform: translateX(0) scale(1); }
}

@media (max-width: 720px) {
  .rw-project-select { width: min(220px, 44vw); }
  .rw-project-preview {
    right: 0;
    bottom: calc(100% + 58px);
  }
  .rw-project-preview::after { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .rw-project-menu,
  .rw-project-preview { animation: none; }
}
</style>
