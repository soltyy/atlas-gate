<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { gateApi, type UsageReport } from './api'
const report = ref<UsageReport | null>(null)
const error = ref('')
const busy = ref(false)
const filters = reactive({ hours: '24', org: '', device: '', model: '', route: '' })
let range: { since: number; until: number } | null = null
function number(value: number | null | undefined) { return value == null ? 'нет данных' : value.toLocaleString('ru-RU') }
function percent(value: number | null) { return value == null ? 'нет данных' : `${value.toFixed(1)} %` }
async function load(more = false) {
  busy.value = true; error.value = ''
  if (!more || !range) { const until = Date.now() / 1000; range = { since: until - Number(filters.hours) * 3600, until } }
  const params = new URLSearchParams({ since: String(range.since), until: String(range.until) })
  for (const key of ['org', 'device', 'model', 'route'] as const) if (filters[key].trim()) params.set(key, filters[key].trim())
  if (more && report.value?.nextBefore) params.set('before', String(report.value.nextBefore))
  try {
    const result = await gateApi.usage(params)
    if (more && report.value) result.rows = [...report.value.rows, ...result.rows]
    report.value = result
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
onMounted(() => void load())
</script>
<template>
  <section class="usage panel">
    <h2>Расход токенов</h2>
    <p>Расход — сумма обращений к модели за ход, включая инструменты и субагентов. Это не размер контекстного окна и не остаток подписки.</p>
    <form class="filters" @submit.prevent="load()">
      <label>Период <select v-model="filters.hours"><option value="24">Сутки</option><option value="168">Неделя</option><option value="720">30 дней</option></select></label>
      <label>Организация <input v-model="filters.org" placeholder="Все" /></label>
      <label>Устройство (ID) <input v-model="filters.device" placeholder="Все" /></label>
      <label>Модель <input v-model="filters.model" placeholder="Все" /></label>
      <label>Маршрут <input v-model="filters.route" placeholder="Все" /></label>
      <button :disabled="busy">Обновить</button>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="report">
      <div class="metrics">
        <div>Вход всего <strong>{{ number(report.summary.totalInputTokens) }}</strong><small>Измерено: {{ report.summary.totalInputTokensRecords }} / {{ report.summary.records }}</small></div>
        <div>Чтение кеша <strong>{{ number(report.summary.cacheReadTokens) }}</strong><small>{{ percent(report.summary.cacheReadPercent) }} входа; сопоставимых записей: {{ report.summary.cacheComparableRecords }}</small></div>
        <div>Вход вне кеша <strong>{{ number(report.summary.uncachedInputTokens) }}</strong><small>Измерено: {{ report.summary.uncachedInputTokensRecords }} / {{ report.summary.records }}</small></div>
        <div>Запись кеша <strong>{{ number(report.summary.cacheWriteTokens) }}</strong><small>Измерено: {{ report.summary.cacheWriteTokensRecords }} / {{ report.summary.records }}</small></div>
        <div>Выход <strong>{{ number(report.summary.outputTokens) }}</strong><small>Измерено: {{ report.summary.outputTokensRecords }} / {{ report.summary.records }}</small></div>
      </div>
      <p>Записей: {{ report.summary.records }}. Записей без детализации SDK: {{ report.summary.legacyRecords || 0 }}. «Нет данных» отличается от нуля. Доля кеша рассчитана только для записей с известным входом и чтением кеша.</p>
      <h3>Устройства и модели</h3>
      <div class="table-wrap"><table><thead><tr><th>Устройство</th><th>Модель</th><th>Ходов / запросов</th><th>Вход</th><th>Кеш: чтение</th><th>Доля кеша</th><th>Кеш: запись</th><th>Выход</th></tr></thead><tbody>
        <tr v-for="g in report.groups" :key="`${g.deviceId}:${g.model}:${g.kind}`"><td>{{ g.deviceName || g.deviceId }}<small>{{ g.platform }} · {{ g.kind === 'agent' ? 'подписка' : g.kind }}</small></td><td>{{ g.model }}</td><td>{{ g.records }}</td><td>{{ number(g.totalInputTokens) }}</td><td>{{ number(g.cacheReadTokens) }}</td><td>{{ percent(g.cacheReadPercent) }}<small>{{ g.cacheComparableRecords }} / {{ g.records }} записей</small></td><td>{{ number(g.cacheWriteTokens) }}</td><td>{{ number(g.outputTokens) }}</td></tr>
      </tbody></table></div>
      <h3>Последние ходы и запросы</h3>
      <p v-if="!report.rows.length">За выбранный период записей нет.</p>
      <div class="table-wrap"><table><thead><tr><th>Время</th><th>Устройство / модель</th><th>Вход</th><th>Вне кеша</th><th>Кеш: чтение / запись</th><th>Выход</th><th>SDK, с</th><th>Подробности</th></tr></thead><tbody>
        <tr v-for="row in report.rows" :key="row.id"><td>{{ new Date(row.ts * 1000).toLocaleString('ru-RU') }}</td><td>{{ row.deviceName || row.deviceId }}<small>{{ row.model }} · {{ row.route }}</small></td><td>{{ number(row.totalInputTokens) }}</td><td>{{ number(row.uncachedInputTokens) }}</td><td>{{ number(row.cacheReadTokens) }} / {{ number(row.cacheWriteTokens) }}<small v-if="row.source === 'router-sdk' && row.cacheReadTokens === 0">Без чтения кеша</small></td><td>{{ number(row.outputTokens) }}</td><td>{{ row.durationMs == null ? 'нет данных' : (row.durationMs / 1000).toFixed(1) }}</td><td><details><summary>{{ row.source === 'legacy' ? 'Без детализации SDK' : row.status === 'success' ? 'Успешно' : row.status === 'failed' ? 'Ошибка' : 'Статус неизвестен' }}</summary><p>Сессия: {{ row.sessionId || '—' }}<br />Ход: {{ row.turnId || '—' }}<br />Устройство: {{ row.deviceId }}</p><p v-if="Object.keys(row.modelUsage).length">Разбивка SDK по моделям (входит в итог, повторно не суммируется):</p><pre v-if="Object.keys(row.modelUsage).length">{{ row.modelUsage }}</pre></details></td></tr>
      </tbody></table></div>
      <button v-if="report.nextBefore" :disabled="busy" @click="load(true)">Показать ещё</button>
    </template>
  </section>
</template>
<style scoped>
.usage { padding: 20px; } .filters { display:flex; flex-wrap:wrap; gap:12px; align-items:end; margin:20px 0; }
label { display:flex; flex-direction:column; gap:5px; } input,select,button { padding:8px; } input { max-width:180px; }
.metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; }
.metrics>div { padding:14px; border:1px solid #dce2eb; border-radius:8px; } strong { display:block; font-size:22px; margin:7px 0; }
small { display:block; color:#64748b; } .table-wrap { overflow:auto; } table { width:100%; border-collapse:collapse; }
th,td { text-align:left; padding:10px; border-bottom:1px solid #e2e8f0; vertical-align:top; } th { white-space:nowrap; }
details { max-width:420px; } pre,p { overflow-wrap:anywhere; } pre { white-space:pre-wrap; } [role=alert] { color:#b91c1c; }
</style>
