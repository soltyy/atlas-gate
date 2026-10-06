<script setup lang="ts">
import { computed } from 'vue'
import type { ModelContract, ModelEntry } from './types'
import { fmtNum } from './util'

const props = defineProps<{ model: ModelEntry; contract?: ModelContract }>()
const caps = computed(() => props.contract?.attachments)
const labels = [
  ['maxFiles', 'Файлов за ход'], ['maxRawBytes', 'Байт на файл'], ['maxTotalBytes', 'Байт суммарно'],
  ['maxTextChars', 'Символов текста'], ['maxPdfPages', 'Страниц PDF'], ['maxPagePixels', 'Пикселей страницы'],
] as const
function positive(value: unknown) {
  return typeof value === 'number' && value > 0 ? fmtNum(value) : 'неизвестно'
}
function supported(value: unknown) {
  return typeof value === 'boolean' ? (value ? 'да' : 'нет') : 'неизвестно'
}
</script>

<template>
  <section class="metrics" :data-model-metrics="`${model.route_id}/${model.model}`">
    <strong>{{ model.display_name || model.model }} · {{ model.route_id }}</strong>
    <dl>
      <dt>Окно, токенов</dt><dd>{{ positive(model.context_window) }}</dd>
      <dt>Макс. ответ, токенов</dt><dd>{{ positive(model.max_output) }}</dd>
      <template v-for="[field, label] in labels" :key="field"><dt>{{ label }}</dt><dd>{{ positive(caps?.[field]) }}</dd></template>
      <dt>Изображения</dt><dd>{{ supported(caps?.image) }}</dd>
      <dt>PDF нативно</dt><dd>{{ supported(caps?.pdfNative) }}</dd>
      <dt>Страницы PDF</dt><dd>{{ supported(caps?.documentPages) }}</dd>
      <dt>Инструменты документа</dt><dd>{{ supported(caps?.agentDocumentTools) }}</dd>
    </dl>
    <p class="muted">Источник лимитов: {{ contract?.limits_source ?? 'конфигурация организации' }}.
      Источник вложений: {{ caps?.source ?? 'неизвестно' }}. Текущий расход контекста сообщает SDK сессии.</p>
  </section>
</template>

<style scoped>
.metrics { margin: 12px 0; padding: 12px; border: 1px solid var(--border, #d0d7de); border-radius: 6px; }
dl { display: grid; grid-template-columns: minmax(160px, 1fr) 1fr; gap: 5px 12px; }
dd { margin: 0; font-variant-numeric: tabular-nums; }
</style>
