<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
const featureKeys = ['agents', 'assets', 'terminal', 'packages', 'chat', 'knowledge', 'account', 'tools', 'settings'] as const
type FeatureKey = (typeof featureKeys)[number]
const selectedImage = ref<FeatureKey | null>(null)
const imageTitle = computed(() =>
  selectedImage.value ? t(`download.showcase.features.${selectedImage.value}.label`) : '',
)
const imageOpen = computed({
  get: () => selectedImage.value !== null,
  set: (value: boolean) => {
    if (!value) selectedImage.value = null
  },
})

const screenshots = import.meta.glob<string>('/src/assets/client-showcase/*.webp', { eager: true, query: '?url', import: 'default' })
const screenshotUrl = (key: FeatureKey) => screenshots[`/src/assets/client-showcase/${key}.webp`]
</script>

<template>
  <section class="client-showcase" aria-labelledby="client-showcase-title">
    <header class="showcase-intro">
      <p class="showcase-eyebrow">{{ t('download.showcase.eyebrow') }}</p>
      <h2 id="client-showcase-title">{{ t('download.showcase.title') }}</h2>
      <p class="showcase-summary">{{ t('download.showcase.intro') }}</p>
      <nav class="feature-nav" :aria-label="t('download.showcase.navigation')">
        <a v-for="key in featureKeys" :key="key" :href="`#client-feature-${key}`">
          {{ t(`download.showcase.features.${key}.label`) }}
        </a>
      </nav>
    </header>

    <article
      v-for="(key, index) in featureKeys"
      :id="`client-feature-${key}`"
      :key="key"
      class="feature-section"
      :class="{ 'feature-section-reverse': index % 2 === 1 }"
      :aria-labelledby="`client-feature-title-${key}`"
    >
      <div class="feature-copy">
        <p class="feature-kicker">
          <span>{{ String(index + 1).padStart(2, '0') }}</span>
        </p>
        <h3 :id="`client-feature-title-${key}`">{{ t(`download.showcase.features.${key}.label`) }}</h3>
        <p class="feature-description">{{ t(`download.showcase.features.${key}.description`) }}</p>
      </div>
      <figure class="feature-figure">
        <button
          type="button"
          class="screenshot-button"
          :aria-label="`${t('download.showcase.enlarge')} · ${t(`download.showcase.features.${key}.label`)}`"
          aria-haspopup="dialog"
          @click="selectedImage = key"
        >
          <img
            :src="screenshotUrl(key)"
            :alt="`${t('download.showcase.caption')} · ${t(`download.showcase.features.${key}.label`)}`"
            loading="lazy"
            decoding="async"
            width="1920"
            height="1018"
          />
          <span class="screenshot-enlarge"
            >{{ t('download.showcase.enlarge') }} <span aria-hidden="true">↗</span></span
          >
        </button>
        <figcaption>
          {{ t('download.showcase.caption') }} · {{ t(`download.showcase.features.${key}.label`) }}
        </figcaption>
      </figure>
    </article>

    <aside class="showcase-requirements">
      <h3>{{ t('download.showcase.requirements') }}</h3>
      <p>{{ t('download.showcase.requirementsText') }}</p>
    </aside>

    <el-dialog
      v-model="imageOpen"
      :title="imageTitle"
      width="min(1200px, 94vw)"
      append-to-body
      destroy-on-close
      :close-on-click-modal="true"
    >
      <img v-if="selectedImage" class="showcase-full-image" :src="screenshotUrl(selectedImage)" :alt="imageTitle" />
    </el-dialog>
  </section>
</template>

<style scoped>
.client-showcase {
  max-width: 1120px;
  margin: 0 auto;
  padding: 56px 32px 80px;
  background: #fff;
  color: #172033;
  border-top: 1px solid #e8ecf1;
}
.showcase-intro {
  max-width: 780px;
  margin-bottom: 64px;
}
.showcase-eyebrow,
.feature-kicker {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: #2762d3;
}
.showcase-eyebrow {
  margin: 0 0 16px;
}
.showcase-intro h2 {
  margin: 0 0 20px;
  font-size: clamp(26px, 3vw, 36px);
  font-weight: 650;
  line-height: 1.35;
  letter-spacing: -0.025em;
}
.showcase-summary {
  margin: 0;
  max-width: 640px;
  color: #657189;
  font-size: 15px;
  line-height: 1.9;
}
.feature-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 20px;
  margin-top: 28px;
}
.feature-nav a {
  color: #46536a;
  font-size: 13px;
  text-decoration: none;
  text-underline-offset: 5px;
}
.feature-nav a:hover {
  color: #2762d3;
  text-decoration: underline;
}
.feature-nav a:focus-visible,
.screenshot-button:focus-visible {
  outline: 2px solid #2762d3;
  outline-offset: 5px;
}
.feature-section {
  display: grid;
  grid-template-columns: minmax(0, 0.82fr) minmax(0, 1.18fr);
  gap: 48px;
  align-items: center;
  padding: 52px 0;
  border-top: 1px solid #e8ecf1;
  scroll-margin-top: 28px;
}
.feature-section-reverse {
  grid-template-columns: minmax(0, 1.18fr) minmax(0, 0.82fr);
}
.feature-section-reverse .feature-copy {
  order: 2;
}
.feature-kicker {
  margin: 0 0 16px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.feature-kicker span {
  font-variant-numeric: tabular-nums;
  color: #8a96a9;
}
.feature-copy h3 {
  margin: 0 0 15px;
  font-size: 24px;
  font-weight: 650;
  line-height: 1.45;
  letter-spacing: -0.025em;
}
.feature-description {
  margin: 0 0 24px;
  font-size: 14px;
  line-height: 1.85;
  color: #657189;
}
.feature-points {
  display: grid;
  gap: 16px;
  margin: 0;
}
.feature-points dt {
  margin: 0 0 5px;
  font-size: 13px;
  font-weight: 600;
  color: #253148;
}
.feature-points dd {
  margin: 0;
  font-size: 12px;
  line-height: 1.85;
  color: #657189;
}
.feature-figure {
  margin: 0;
  min-width: 0;
}
.screenshot-button {
  display: block;
  position: relative;
  width: 100%;
  padding: 0;
  border: 1px solid #dce3ec;
  border-radius: 10px;
  background: #fff;
  overflow: hidden;
  cursor: zoom-in;
  box-shadow: 0 12px 36px rgb(24 38 60 / 7%);
}
.screenshot-button img {
  display: block;
  width: 100%;
  height: auto;
}
.screenshot-enlarge {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  padding: 11px 14px;
  border-top: 1px solid #e8ecf1;
  font-size: 11px;
  color: #657189;
  background: #fff;
}
.screenshot-button:hover .screenshot-enlarge {
  color: #2762d3;
}
.feature-figure figcaption {
  margin-top: 13px;
  font-size: 11px;
  text-align: center;
  color: #8a96a9;
}
.showcase-requirements {
  border-top: 1px solid #e8ecf1;
  padding-top: 28px;
  margin-top: 20px;
}
.showcase-requirements h3 {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
}
.showcase-requirements p {
  margin: 0;
  max-width: 850px;
  color: #657189;
  font-size: 12px;
  line-height: 1.9;
}
.showcase-full-image {
  display: block;
  width: 100%;
  height: auto;
}
@media (max-width: 900px) {
  .feature-section,
  .feature-section-reverse {
    grid-template-columns: minmax(0, 1fr);
    gap: 30px;
  }
  .feature-section-reverse .feature-copy {
    order: initial;
  }
  .feature-points {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 20px;
  }
}
@media (max-width: 540px) {
  .client-showcase {
    padding: 40px 20px 56px;
  }
  .showcase-intro {
    margin-bottom: 40px;
  }
  .feature-section {
    padding: 36px 0;
  }
  .feature-points {
    grid-template-columns: minmax(0, 1fr);
  }
  .feature-copy h3 {
    font-size: 22px;
  }
}
</style>
