import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'

/** Pace display only: the source always retains the authoritative full text. */
export function useProgressiveText(source: Ref<string>, animate: Ref<boolean>) {
  const displayed = ref(animate.value ? '' : source.value)
  let timer: ReturnType<typeof setTimeout> | undefined
  const pending = computed(() => displayed.value !== source.value)
  const stop = () => {
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
  }
  const advance = () => {
    timer = undefined
    const rest = Array.from(source.value.slice(displayed.value.length))
    // Small chunks read like typing; large replies catch up without a long tail.
    const count = Math.max(1, Math.ceil(rest.length / 40))
    displayed.value += rest.slice(0, count).join('')
    if (pending.value) timer = setTimeout(advance, 16)
  }
  watch([source, animate], ([text, enabled]) => {
    if (!enabled) {
      stop()
      displayed.value = text
      return
    }
    if (!text.startsWith(displayed.value)) {
      stop()
      // Replace an authoritative correction immediately. Never leave stale or
      // unsanitized prefixes on screen while animating a replacement.
      displayed.value = text
      return
    }
    if (pending.value && timer === undefined) timer = setTimeout(advance, 16)
  }, { immediate: true, flush: 'sync' })
  onScopeDispose(stop)
  return { displayed, pending }
}
