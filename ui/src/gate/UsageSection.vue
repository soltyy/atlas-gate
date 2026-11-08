<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { gateApi, type UsageGroup, type UsageReport, type UsageUser } from './api'

const report = ref<UsageReport | null>(null)
const error = ref('')
const busy = ref(false)
const exporting = ref(false)
const filters = reactive({ preset: '7', from: '', to: '', timezone: 'msk', bucket: 'day',
  group: 'user_model', org: '', user: '', model: '', route: '', device: '', kind: '' })
let applied: URLSearchParams | null = null
let appliedFilters = ''
const shown = ref({ timezone: 'msk', bucket: 'day', group: 'user_model' })
const offsets: Record<string, number> = { msk: 10800, utc: 0 }
const users = computed(() => (report.value?.options?.users ?? []).filter(u => !filters.org || u.org === filters.org))
const organizations = computed(() => [...new Set(report.value?.options?.users.map(u => u.org))])
const chartMax = computed(() => (report.value?.trend ?? []).reduce((max, p) => Math.max(max, p.totalTokens ?? 0), 1))
const format = new Intl.NumberFormat('ru-RU')

function number(value: number | null | undefined) { return value == null ? 'нет данных' : format.format(value) }
function percent(value: number | null) { return value == null ? 'нет данных' : `${value.toFixed(1)} %` }
function userKey(user: UsageUser) { return JSON.stringify([user.org, user.userId]) }
function userLabel(user: Partial<UsageUser>) { return user.userName || user.userEmail || user.userId || '—' }
function groupKey(group: UsageGroup) { return JSON.stringify([group.period, group.org, group.userId, group.model, group.route, group.kind]) }
function applyPreset() {
  if (filters.preset === 'custom') return
  const today = Date.now() + offsets[filters.timezone] * 1000
  filters.to = new Date(today).toISOString().slice(0, 10)
  filters.from = new Date(today - (Number(filters.preset) - 1) * 86400000).toISOString().slice(0, 10)
}
function dateSeconds(date: string) { return Date.parse(`${date}T00:00:00Z`) / 1000 - offsets[filters.timezone] }
function time(ts: number) {
  return new Date(ts * 1000).toLocaleString('ru-RU', { timeZone: shown.value.timezone === 'msk' ? 'Europe/Moscow' : 'UTC' })
}
function nextPeriod(period: string) {
  const date = new Date(`${period}T00:00:00Z`)
  if (shown.value.bucket === 'month') date.setUTCMonth(date.getUTCMonth() + 1)
  else date.setUTCDate(date.getUTCDate() + (shown.value.bucket === 'week' ? 7 : 1))
  return date.toISOString().slice(0, 10)
}
function fullPeriod(period: string) {
  if (!report.value) return false
  const offset = offsets[shown.value.timezone]
  return Date.parse(period) / 1000 - offset >= report.value.since
    && Date.parse(nextPeriod(period)) / 1000 - offset <= report.value.until
}
function delta(index: number, key: 'totalTokens' | 'cacheAdjustedTokens') {
  const trend = report.value?.trend
  if (!trend || index === 0) return '—'
  const prev = trend[index - 1]
  const curr = trend[index]
  const records = `${key}Records` as const
  if (nextPeriod(prev.period) !== curr.period || !fullPeriod(prev.period) || !fullPeriod(curr.period)
      || prev.incompleteRecords || curr.incompleteRecords || prev[records] !== prev.records || curr[records] !== curr.records
      || prev[key] == null || curr[key] == null) return '—'
  const diff = curr[key] - prev[key]
  return diff > 0 ? `+${number(diff)}` : number(diff)
}
function selectedParams() {
  const since = dateSeconds(filters.from)
  const until = Math.min(dateSeconds(filters.to) + 86400, Date.now() / 1000)
  if (!Number.isFinite(since) || !Number.isFinite(until) || since >= until) {
    throw new Error('Укажите корректный диапазон дат.')
  }
  const params = new URLSearchParams({ since: String(since), until: String(until),
    group_by: filters.group, timezone: filters.timezone })
  if (filters.bucket) params.set('bucket', filters.bucket)
  for (const key of ['org', 'device', 'model', 'route', 'kind'] as const) {
    if (filters[key].trim()) params.set(key, filters[key].trim())
  }
  if (filters.user) {
    const [org, user] = JSON.parse(filters.user) as [string, string]
    params.set('org', org)
    params.set('user', user)
  }
  return params
}
async function load(more = false) {
  if (busy.value || exporting.value) return
  error.value = ''
  busy.value = true
  try {
    const selection = JSON.stringify(filters)
    const params = more && applied ? new URLSearchParams(applied) : selectedParams()
    if (more && report.value?.nextBefore) params.set('before', String(report.value.nextBefore))
    const result = await gateApi.usage(params)
    if (more && report.value) {
      report.value = { ...report.value, rows: [...report.value.rows, ...result.rows], nextBefore: result.nextBefore }
    } else {
      report.value = result
      applied = params
      appliedFilters = selection
      shown.value = { timezone: params.get('timezone')!, bucket: params.get('bucket') ?? '', group: params.get('group_by')! }
    }
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
async function exportExcel() {
  if (busy.value || exporting.value) return
  exporting.value = true
  error.value = ''
  try {
    const params = applied && appliedFilters === JSON.stringify(filters) ? applied : selectedParams()
    const blob = await gateApi.usageExcel(params)
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `atlas-usage-${filters.from}_${filters.to}.xlsx`
    document.body.append(link)
    link.click()
    link.remove()
    window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    exporting.value = false
  }
}
applyPreset()
onMounted(() => void load())
</script>

<template>
  <section class="usage panel">
    <h2>Статистика моделей и пользователей</h2>
    <p>Расход с разных устройств объединяется по пользователю в организации. Модель указана по маршруту хода или API-запроса.</p>
    <form @submit.prevent="load()">
      <fieldset class="filters" :disabled="busy || exporting">
        <label>Период <select v-model="filters.preset" @change="applyPreset"><option value="7">7 дней</option><option value="30">30 дней</option><option value="90">90 дней</option><option value="custom">Свой диапазон</option></select></label>
        <label>С <input v-model="filters.from" type="date" required @change="filters.preset = 'custom'" /></label>
        <label>По <input v-model="filters.to" type="date" required @change="filters.preset = 'custom'" /></label>
        <label>Часовой пояс <select v-model="filters.timezone" @change="applyPreset"><option value="msk">МСК</option><option value="utc">UTC</option></select></label>
        <label>Интервал <select v-model="filters.bucket"><option value="day">День</option><option value="week">Неделя с понедельника</option><option value="month">Месяц</option><option value="">Весь период</option></select></label>
        <label>Группировка <select v-model="filters.group"><option value="user_model">Пользователь и модель</option><option value="user">Пользователь</option><option value="model">Модель</option></select></label>
        <label>Организация <input v-model.trim="filters.org" list="usage-orgs" placeholder="Все" @input="filters.user = ''" /></label>
        <label>Пользователь <select v-model="filters.user"><option value="">Все</option>
          <option v-if="filters.user && !users.some(u => userKey(u) === filters.user)" :value="filters.user">{{ JSON.parse(filters.user).join(' · ') }}</option>
          <option v-for="u in users" :key="userKey(u)" :value="userKey(u)">{{ userLabel(u) }} · {{ u.userEmail || u.userId }} · {{ u.org }}</option>
        </select></label>
        <label>Модель <input id="usage-model" v-model="filters.model" list="usage-models" placeholder="Все" /></label>
        <label>Маршрут <input v-model="filters.route" list="usage-routes" placeholder="Все" /></label>
        <button :disabled="busy">Обновить</button>
        <button type="button" @click="exportExcel">{{ exporting ? 'Подготовка Excel…' : 'Скачать Excel' }}</button>
      </fieldset>
      <datalist id="usage-orgs"><option v-for="org in organizations" :key="org" :value="org" /></datalist>
      <datalist id="usage-models"><option v-for="model in report?.options?.models" :key="model" :value="model" /></datalist>
      <datalist id="usage-routes"><option v-for="route in report?.options?.routes" :key="route" :value="route" /></datalist>
      <details class="extra-filters"><summary>Дополнительные фильтры</summary>
        <fieldset class="filters" :disabled="busy || exporting">
          <label>Устройство (ID) <input v-model="filters.device" placeholder="Все" /></label>
          <label>Тип <select v-model="filters.kind"><option value="">Все</option><option value="agent">Подписка</option><option value="gateway">API</option></select></label>
        </fieldset>
      </details>
    </form>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="report">
      <p>Отчёт за {{ time(report.since) }} — {{ time(report.until) }} ({{ shown.timezone === 'msk' ? 'МСК' : 'UTC' }}). Записей {{ number(report.summary.records) }}.</p>
      <div class="metrics">
        <div>Все токены <strong>{{ number(report.summary.totalTokens) }}</strong><small>{{ report.summary.totalTokensRecords }} / {{ report.summary.records }} записей</small></div>
        <div>За вычетом чтения из кеша <strong>{{ number(report.summary.cacheAdjustedTokens) }}</strong><small>{{ report.summary.cacheAdjustedTokensRecords }} / {{ report.summary.records }} записей</small></div>
        <div>Вход всего <strong>{{ number(report.summary.totalInputTokens) }}</strong><small>Измерено: {{ report.summary.totalInputTokensRecords }} / {{ report.summary.records }}</small></div>
        <div>Чтение кеша <strong>{{ number(report.summary.cacheReadTokens) }}</strong><small>{{ percent(report.summary.cacheReadPercent) }} входа на {{ report.summary.cacheComparableRecords }} записях</small></div>
        <div>Запись кеша <strong>{{ number(report.summary.cacheWriteTokens) }}</strong><small>Измерено: {{ report.summary.cacheWriteTokensRecords }} / {{ report.summary.records }}</small></div>
        <div>Выход <strong>{{ number(report.summary.outputTokens) }}</strong><small>Измерено: {{ report.summary.outputTokensRecords }} / {{ report.summary.records }}</small></div>
      </div>
      <p>Все токены = вход + выход. Во второй метрике вычитается чтение из кеша. Запись кеша входит в расход. Неизвестные значения пропускаются, число учтённых записей указано рядом.</p>
      <p>Записей без детализации SDK: {{ report.summary.legacyRecords || 0 }}. Частичный учёт: {{ report.summary.incompleteRecords || 0 }}.</p>
      <template v-if="report.trend?.length">
        <h3>Динамика</h3>
        <p>Δ к предыдущему полному календарному интервалу с измерениями по всем записям. Неполные периоды помечены.</p>
        <div class="table-wrap"><table><thead><tr><th>Начало интервала</th><th>Все токены</th><th>Δ</th><th>За вычетом чтения из кеша</th><th>Δ</th></tr></thead><tbody>
          <tr v-for="(p, i) in report.trend" :key="p.period">
            <td>{{ p.period }}<small v-if="!fullPeriod(p.period)">Неполный период</small></td>
            <td>{{ number(p.totalTokens) }}<div v-if="p.totalTokens != null" class="bar" aria-hidden="true" :style="{ width: (p.totalTokens / chartMax * 100) + '%' }" /></td>
            <td>{{ delta(i, 'totalTokens') }}</td>
            <td>{{ number(p.cacheAdjustedTokens) }}<small>{{ p.cacheAdjustedTokensRecords }} / {{ p.records }} записей</small><div v-if="p.cacheAdjustedTokens != null" class="bar adjusted" aria-hidden="true" :style="{ width: (p.cacheAdjustedTokens / chartMax * 100) + '%' }" /></td>
            <td>{{ delta(i, 'cacheAdjustedTokens') }}</td>
          </tr>
        </tbody></table></div>
      </template>
      <h3>Разбивка</h3>
      <p v-if="!report.groups.length">За выбранный период записей нет.</p>
      <div v-else class="table-wrap"><table><thead><tr>
        <th v-if="shown.bucket">Интервал с</th><th v-if="shown.group !== 'model'">Пользователь</th><th v-if="shown.group !== 'user'">Модель и маршрут</th>
        <th>Записей</th><th>Все токены</th><th>За вычетом чтения из кеша</th><th>Доля кеша</th><th>Кеш: чтение / запись</th>
      </tr></thead><tbody>
        <tr v-for="g in report.groups" :key="groupKey(g)">
          <td v-if="shown.bucket">{{ g.period }}</td>
          <td v-if="shown.group !== 'model'">{{ userLabel(g) }}<small>{{ g.userEmail || g.userId }} · {{ g.org }}</small></td>
          <td v-if="shown.group !== 'user'">{{ g.model }}<small>{{ g.route || 'Без маршрута' }} · {{ g.kind === 'agent' ? 'подписка' : g.kind === 'gateway' ? 'API' : g.kind }}</small></td>
          <td>{{ g.records }}</td>
          <td>{{ number(g.totalTokens) }}<small v-if="g.totalTokensRecords !== g.records">{{ g.totalTokensRecords }} / {{ g.records }}</small></td>
          <td>{{ number(g.cacheAdjustedTokens) }}<small>{{ g.cacheAdjustedTokensRecords }} / {{ g.records }} записей</small></td>
          <td>{{ percent(g.cacheReadPercent) }}<small>{{ g.cacheComparableRecords }} / {{ g.records }} записей</small></td>
          <td>{{ number(g.cacheReadTokens) }} / {{ number(g.cacheWriteTokens) }}</td>
        </tr>
      </tbody></table></div>
      <h3>Последние ходы и запросы</h3>
      <div v-if="report.rows.length" class="table-wrap"><table><thead><tr><th>Время</th><th>Пользователь / модель</th><th>Вход</th><th>Вне кеша</th><th>Кеш: чтение / запись</th><th>Выход</th><th>SDK, с</th><th>Подробности</th></tr></thead><tbody>
        <tr v-for="row in report.rows" :key="row.id"><td>{{ time(row.ts) }}</td><td>{{ userLabel(row) }}<small>{{ row.model }} · {{ row.route }}</small></td><td>{{ number(row.totalInputTokens) }}</td><td>{{ number(row.uncachedInputTokens) }}</td><td>{{ number(row.cacheReadTokens) }} / {{ number(row.cacheWriteTokens) }}<small v-if="row.source === 'router-sdk' && row.cacheReadTokens === 0">Без чтения кеша</small></td><td>{{ number(row.outputTokens) }}</td><td>{{ row.durationMs == null ? 'нет данных' : (row.durationMs / 1000).toFixed(1) }}</td><td><details><summary>{{ row.source === 'legacy' ? 'Без детализации SDK' : row.status === 'success' ? 'Успешно' : row.status === 'failed' ? 'Ошибка' : 'Статус неизвестен' }}</summary><p>Сессия: {{ row.sessionId || '—' }}<br />Ход: {{ row.turnId || '—' }}<br />Устройство: {{ row.deviceName || row.deviceId }}</p><p v-if="Object.keys(row.modelUsage).length">Разбивка SDK по моделям входит в итог.</p><pre v-if="Object.keys(row.modelUsage).length">{{ row.modelUsage }}</pre></details></td></tr>
      </tbody></table></div>
      <button v-if="report.nextBefore" :disabled="busy || exporting" @click="load(true)">Показать ещё</button>
    </template>
  </section>
</template>

<style scoped>
.usage { padding: 20px; }
.filters { display:flex; flex-wrap:wrap; gap:12px; align-items:end; margin:16px 0; padding:0; border:0; }
label { display:flex; flex-direction:column; gap:5px; } input,select,button { padding:8px; max-width:260px; }
.extra-filters { margin-bottom: 16px; }
.metrics { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; }
.metrics>div { padding:14px; border:1px solid #dce2eb; border-radius:8px; } strong { display:block; font-size:22px; margin:7px 0; }
small { display:block; color:#64748b; } .table-wrap { overflow:auto; } table { width:100%; border-collapse:collapse; }
th,td { text-align:left; padding:10px; border-bottom:1px solid #e2e8f0; vertical-align:top; } th { white-space:nowrap; }
.bar { height:4px; background:#3158a3; border-radius:4px; margin-top:5px; } .bar.adjusted { background:#31775c; }
details { max-width:420px; } pre,p { overflow-wrap:anywhere; } pre { white-space:pre-wrap; } [role=alert] { color:#b91c1c; }
</style>
