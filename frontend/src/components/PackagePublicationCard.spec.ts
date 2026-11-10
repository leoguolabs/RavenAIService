import { renderToString } from '@vue/server-renderer'
import { describe, expect, it } from 'vitest'
import { createSSRApp, h } from 'vue'
import PackagePublicationCard from '@/components/PackagePublicationCard.vue'
import { i18n, setI18nLocale } from '@/i18n'
import type { PackagePublicationReview } from '@/types/agentTrace'

describe('PackagePublicationCard', () => {
  it.each([[], ['Firmware is absent']].map((warnings) => ({ warnings })))(
    'shows a summary and direct actions with warnings $warnings',
    async ({ warnings }) => {
      setI18nLocale('en')
      const review: PackagePublicationReview = {
        project_name: 'Lingxi 07a',
        project_code: 'oam_lx07a',
        version: '1.1.0.0',
        mode: 'full',
        components: [{ name: 'oam', version: '1.0.0.2' }],
        warnings,
        exclusions: [{ name: 'reference.bin', reason: 'Reference only' }],
      }
      const app = createSSRApp({ render: () => h(PackagePublicationCard, { review }) }).use(i18n)
      const html = await renderToString(app)
      expect(html).toContain(warnings.length ? 'Software package publication review' : 'Package ready to publish')
      expect(html).toContain('Lingxi 07a')
      expect(html).toContain('1.1.0.0 · Full package')
      expect(html).toContain('1.0.0.2')
      expect(html).toContain('reference.bin')
      expect(html).toContain('Confirm publication')
      expect(html).toContain('Cancel publication')
      if (warnings.length) expect(html).toContain(warnings[0])
      else expect(html).not.toContain('Requires review before publication')
    },
  )
})
