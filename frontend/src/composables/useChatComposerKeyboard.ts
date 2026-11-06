/** Keep IME candidate confirmation separate from the chat's Enter shortcut. */
export function useChatComposerKeyboard(sendMessage: () => void) {
  let composing = false

  const handleCompositionStart = () => {
    composing = true
  }

  const handleCompositionEnd = () => {
    composing = false
  }

  const handleKeydown = (event: KeyboardEvent) => {
    // Some IMEs omit isComposing; others end composition before the confirming
    // keydown and only expose keyCode 229. Leave their default action intact.
    if (composing || event.isComposing || event.keyCode === 229) return
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      sendMessage()
    }
  }

  return { handleKeydown, handleCompositionStart, handleCompositionEnd }
}
