<script setup lang="ts">
// Редактор профиля организации (#6, переработка UX — #12). Правится черновик полной конфигурации;
// «Сохранить» шлёт её PUT с If-Match (ETag файла): 422 — сводка ошибок наверху и ошибки у полей,
// 409 — файл изменили после чтения. Разделы — постоянный список слева (NN/g vertical nav), правки —
// липкая панель внизу (Primer saving, Shopify save bar), уход с несохранёнными правками — предупреждение.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import AgentsTab from './AgentsTab.vue'
import GeneralTab from './GeneralTab.vue'
import HistoryPanel from './HistoryPanel.vue'
import McpTab from './McpTab.vue'
import ModelsTab from './ModelsTab.vue'
import PolicyTab from './PolicyTab.vue'
import PricingTab from './PricingTab.vue'
import RoutesTab from './RoutesTab.vue'
import { OrgApiError, orgApi } from './api'
import type { EditorContext, OrgConfig, OrgDetails } from './types'
import { clone, tabOf } from './util'

const props = defineProps<{ orgId: string }>()
const emit = defineEmits<{ close: [] }>()

const TABS = [
  ['routes', 'Маршруты', 'Куда идут запросы: подписка роутера и провайдеры (адрес, имя переменной с ключом, таймаут).'],
  ['models', 'Модели', 'Какие модели каждого маршрута получают устройства.'],
  ['pricing', 'Цены', 'Цена за 1 млн токенов — по ней гейт считает стоимость вызовов и месячную квоту.'],
  ['policy', 'Политика', 'Какие маршруты разрешены для какого класса данных и как ведут себя инструменты.'],
  ['agents', 'Агенты', 'Промпт, модель и инструменты агентов, которые устройство получает в профиле.'],
  ['mcp', 'MCP', 'Внешние MCP-серверы, которые агенты могут подключать.'],
  ['general', 'Общее', 'Название, квота, что пользователь может менять у себя, версии приложения.'],
] as const
type Tab = (typeof TABS)[number][0]

const details = ref<OrgDetails | null>(null)
const draft = ref<OrgConfig | null>(null)
const original = ref<OrgConfig | null>(null)
const tab = ref<Tab>('routes')
const view = ref<'edit' | 'history' | 'json'>('edit')
const errors = ref<Record<string, string>>({})
const general = ref<string[]>([])
const message = ref('')
const notice = ref('')
const warnings = ref<string[]>([])
const conflict = ref(false)
const busy = ref(false)

const ctx = computed<EditorContext>(() => ({
  secrets: details.value?.secrets ?? {},
  upstreams: details.value?.upstreams ?? {},
  subscription_models: details.value?.subscription_models ?? [],
  subscription_models_by_route: details.value?.subscription_models_by_route,
  route_usage: details.value?.route_usage ?? {},
}))
const savedRoutes = computed(() => new Set((original.value?.routes ?? []).map((r) => r.id)))
const savedAgents = computed(() => new Set((original.value?.agents ?? []).map((a) => `${a.id}@${a.version}`)))
const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(original.value))
const errorsByTab = computed(() => {
  const out: Record<string, number> = {}
  for (const loc of Object.keys(errors.value)) {
    const t = tabOf(loc)
    if (t) out[t] = (out[t] ?? 0) + 1
  }
  return out
})
const counts = computed<Record<string, number | string>>(() => {
  const o = draft.value
  if (!o) return {} as Record<string, number | string>
  return {
    routes: o.routes.length, models: o.models.length, pricing: o.pricing.length,
    agents: new Set(o.agents.map((a) => a.id)).size, mcp: o.mcp.length, policy: o.policy.tool_policies.length, general: '',
  }
})
const tabInfo = computed(() => TABS.find(([id]) => id === tab.value)!)

async function load() {
  message.value = ''
  conflict.value = false
  try {
    const d = await orgApi.get(props.orgId)
    details.value = d
    original.value = clone(d.org)
    draft.value = clone(d.org)
    warnings.value = d.warnings
    const agents = draft.value.agents
    if (agents.length && !agents.some((a) => a.default)) {
      // Файл до #6 не знал флага default: отмечаем последнюю версию первого агента — видно и сохраняемо.
      const first = agents.filter((a) => a.id === agents[0].id)
      first[first.length - 1].default = true
      notice.value = `Агент по умолчанию не был отмечен — отмечен «${agents[0].id}»; сохраните, чтобы записать.`
    }
  } catch (e) {
    message.value = (e as Error).message
  }
}

function applyErrors(e: OrgApiError) {
  const map: Record<string, string> = {}
  const top: string[] = []
  for (const f of e.fields) {
    if (f.loc) map[f.loc] = map[f.loc] ? `${map[f.loc]}; ${f.msg}` : f.msg
    else top.push(f.msg)
  }
  errors.value = map
  general.value = top
  const first = Object.keys(map).map(tabOf).find(Boolean) as Tab | undefined
  if (first) tab.value = first
}

/** Из сводки ошибок — к полю: открыть раздел и поставить фокус (GOV.UK error summary). */
function goTo(loc: string) {
  const t = tabOf(loc) as Tab
  if (t) tab.value = t
  view.value = 'edit'
  requestAnimationFrame(() => {
    const el = document.querySelector<HTMLElement>(`[data-f="${loc}"]`)
    el?.focus()
    el?.scrollIntoView?.({ block: 'center' })
  })
}

function tabTitle(loc: string): string {
  return TABS.find(([id]) => id === tabOf(loc))?.[1] ?? 'Профиль'
}

async function save() {
  if (!draft.value || !details.value) return
  busy.value = true
  errors.value = {}
  general.value = []
  message.value = ''
  notice.value = ''
  const blankNames = draft.value.agents.flatMap((a, i) =>
    a.name.trim() ? [] : [[`agents.${i}.name`, 'имя агента не должно быть пустым'] as const])
  if (blankNames.length) {
    errors.value = Object.fromEntries(blankNames)
    message.value = 'Профиль не сохранён: исправьте отмеченные поля'
    tab.value = 'agents'
    busy.value = false
    return
  }
  try {
    const saved = await orgApi.save(props.orgId, draft.value, details.value.etag)
    await load()
    notice.value = `Сохранено: профиль версии ${saved.profile_version}`
    warnings.value = saved.warnings
  } catch (e) {
    if (e instanceof OrgApiError && e.status === 422) {
      applyErrors(e)
      message.value = 'Профиль не сохранён: исправьте отмеченные поля'
    } else if (e instanceof OrgApiError && e.status === 409) {
      conflict.value = true
      message.value = 'Файл организации изменён после открытия — перечитайте (ваши правки будут потеряны) и повторите'
    } else {
      message.value = (e as Error).message
    }
  } finally {
    busy.value = false
  }
}

function cancel() {
  if (!original.value) return
  draft.value = clone(original.value)
  errors.value = {}
  general.value = []
  message.value = ''
}

function close() {
  if (dirty.value && !window.confirm('Есть несохранённые изменения. Уйти к списку без сохранения?')) return
  emit('close')
}

async function onRestored(saved: { profile_version: number; warnings: string[] }) {
  await load()
  view.value = 'edit'
  notice.value = `Откат выполнен: профиль версии ${saved.profile_version}`
  warnings.value = saved.warnings
}

// Переименование и архив (#7) пишут файл сами — при несохранённых правках кнопки выключены.
async function act(what: () => Promise<{ profile_version: number; warnings: string[] }>, done: (v: number) => string) {
  busy.value = true
  message.value = ''
  try {
    const saved = await what()
    await load()
    notice.value = done(saved.profile_version)
    warnings.value = saved.warnings
  } catch (e) {
    message.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

function rename() {
  const current = details.value?.org.name ?? ''
  const name = window.prompt(`Новое название организации ${props.orgId} (id не меняется):`, current)
  if (name === null || !name.trim() || name.trim() === current) return
  void act(() => orgApi.rename(props.orgId, name.trim()), (v) => `Переименовано: профиль версии ${v}`)
}

function setArchived(archived: boolean) {
  const question = archived
    ? `Архивировать ${props.orgId}? Устройства организации перестанут получать профиль, модели и агента; файл и история останутся.`
    : `Вернуть ${props.orgId} из архива? Устройства снова получат профиль.`
  if (!window.confirm(question)) return
  void act(() => (archived ? orgApi.archive(props.orgId) : orgApi.unarchive(props.orgId)),
    (v) => (archived ? `Организация в архиве: профиль версии ${v}` : `Организация возвращена из архива: профиль версии ${v}`))
}

function when(ts: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleString() : '—'
}

function beforeUnload(e: BeforeUnloadEvent) {
  if (dirty.value) {
    e.preventDefault()
    e.returnValue = ''
  }
}
onMounted(() => window.addEventListener('beforeunload', beforeUnload))
onBeforeUnmount(() => window.removeEventListener('beforeunload', beforeUnload))

void load()
</script>

<template>
  <section class="card org-editor">
    <header class="ed-head">
      <div class="ed-title">
        <h2>{{ details?.org.name ?? orgId }}</h2>
        <code>{{ orgId }}</code>
        <span v-if="details" class="badge neutral">версия {{ details.profile_version }}</span>
        <span v-if="details?.archived" class="badge warn" data-archived>в архиве</span>
        <span v-if="details" class="muted small-text">подписан {{ when(details.signed_at) }}</span>
      </div>
      <div class="ed-actions">
        <template v-if="details">
          <button type="button" class="small" data-act="history" :class="{ primary: view === 'history' }" @click="view = view === 'history' ? 'edit' : 'history'">История</button>
          <button type="button" class="small" data-act="json" :class="{ primary: view === 'json' }" @click="view = view === 'json' ? 'edit' : 'json'">JSON</button>
          <button type="button" class="small" data-act="rename" :disabled="busy || dirty" :title="dirty ? 'сначала сохраните или отмените правки' : ''" @click="rename">Переименовать</button>
          <button v-if="!details.archived" type="button" class="small danger" data-act="archive" :disabled="busy || dirty" @click="setArchived(true)">Архивировать</button>
          <button v-else type="button" class="small" data-act="unarchive" :disabled="busy || dirty" @click="setArchived(false)">Разархивировать</button>
        </template>
        <button type="button" class="small" data-act="close" @click="close">← К списку</button>
      </div>
    </header>

    <p v-if="message" class="error" role="alert">{{ message }} <button v-if="conflict" type="button" class="small" data-act="reread" @click="load">Перечитать</button></p>
    <div v-if="Object.keys(errors).length || general.length" class="error-summary" role="alert" data-error-summary>
      <strong>Исправьте, чтобы сохранить:</strong>
      <ul>
        <li v-for="(g, i) in general" :key="'g' + i" data-general-error>{{ g }}</li>
        <li v-for="(msg, loc) in errors" :key="loc">
          <button type="button" class="linkish" :data-goto="loc" @click="goTo(String(loc))">{{ tabTitle(String(loc)) }}: {{ msg }}</button>
        </li>
      </ul>
    </div>
    <p v-if="notice" class="note" role="status">{{ notice }}</p>
    <details v-if="warnings.length" class="warnings" data-warnings>
      <summary>⚠ Предупреждений: {{ warnings.length }} — что не ушло устройствам и почему</summary>
      <ul>
        <li v-for="(w, i) in warnings" :key="i">{{ w }}</li>
      </ul>
    </details>

    <div v-if="draft" class="ed-body">
      <nav class="ed-nav" aria-label="Разделы профиля">
        <button v-for="[id, title] in TABS" :key="id" type="button" class="nav-item" :class="{ active: tab === id && view === 'edit' }"
          :aria-current="tab === id ? 'page' : undefined" :data-tab="id" @click="tab = id; view = 'edit'">
          <span>{{ title }}</span>
          <span v-if="errorsByTab[id]" class="badge bad">{{ errorsByTab[id] }}</span>
          <span v-else-if="counts[id] !== ''" class="count">{{ counts[id] }}</span>
        </button>
      </nav>

      <div class="ed-main">
        <template v-if="view === 'edit'">
          <div class="section-head">
            <h3>{{ tabInfo[1] }}</h3>
            <p class="muted">{{ tabInfo[2] }}</p>
          </div>
          <RoutesTab v-if="tab === 'routes'" :org="draft" :errors="errors" :ctx="ctx" :saved="savedRoutes" />
          <ModelsTab v-else-if="tab === 'models'" :org="draft" :errors="errors" :ctx="ctx" />
          <PricingTab v-else-if="tab === 'pricing'" :org="draft" :errors="errors" />
          <PolicyTab v-else-if="tab === 'policy'" :org="draft" :errors="errors" />
          <AgentsTab v-else-if="tab === 'agents'" :org="draft" :errors="errors" :saved="savedAgents" />
          <McpTab v-else-if="tab === 'mcp'" :org="draft" :errors="errors" :ctx="ctx" />
          <GeneralTab v-else :org="draft" :errors="errors" />
        </template>
        <template v-else-if="view === 'json'">
          <div class="section-head">
            <h3>JSON профиля</h3>
            <p class="muted">Только чтение — так конфигурация запишется в файл при сохранении (с вашими несохранёнными правками).</p>
          </div>
          <pre class="json" data-json>{{ JSON.stringify(draft, null, 2) }}</pre>
        </template>
        <HistoryPanel v-else-if="original" :org-id="orgId" :current="original" :current-version="details?.profile_version ?? null"
          @restored="onRestored" @error="message = $event" />
      </div>
    </div>

    <div v-if="draft && (dirty || busy)" class="savebar" role="region" aria-label="Несохранённые изменения" data-savebar>
      <span><strong>Несохранённые изменения</strong><span class="muted"> — устройства получат их после сохранения</span></span>
      <span class="spacer" />
      <button type="button" data-act="cancel" :disabled="busy" @click="cancel">Отменить изменения</button>
      <button type="button" class="primary" data-act="save" :disabled="busy" @click="save">{{ busy ? 'Сохраняю…' : 'Сохранить' }}</button>
    </div>
  </section>
</template>

<style>
/* Редактор шире колонки админки: страница расширяется, пока открыт редактор. */
#app:has(.org-editor) {
  max-width: 1440px;
}
.org-editor .ed-head {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}
.org-editor .ed-title {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.org-editor .ed-title h2 {
  margin: 0;
  font-size: 18px;
}
.org-editor .ed-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.org-editor .ed-body {
  display: grid;
  grid-template-columns: 200px minmax(0, 1fr);
  gap: 20px;
  margin-top: 12px;
}
.org-editor .ed-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  position: sticky;
  top: 12px;
  align-self: start;
}
.org-editor .nav-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  text-align: left;
  border: 0;
  background: transparent;
  padding: 8px 10px;
  border-radius: var(--radius);
  font-size: 14px;
}
.org-editor .nav-item:hover {
  background: var(--bg);
}
.org-editor .nav-item.active {
  background: var(--bg);
  box-shadow: inset 3px 0 0 var(--accent);
  font-weight: 600;
}
.org-editor .nav-item .count {
  color: var(--muted);
  font-size: 12px;
}
.org-editor .ed-main {
  min-width: 0;
}
.org-editor .section-head h3 {
  margin: 0;
  font-size: 16px;
}
.org-editor .section-head p {
  margin: 2px 0 12px;
}
.org-editor .savebar {
  position: sticky;
  bottom: 0;
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 16px -16px -16px;
  padding: 10px 16px;
  background: var(--surface);
  border-top: 2px solid var(--accent);
  border-radius: 0 0 var(--radius) var(--radius);
  box-shadow: 0 -4px 12px rgba(0, 0, 0, 0.06);
  z-index: 5;
}
.org-editor .error-summary {
  border: 2px solid var(--bad);
  border-radius: var(--radius);
  padding: 8px 12px;
  margin: 8px 0;
}
.org-editor .error-summary ul {
  margin: 4px 0 0;
  padding-left: 18px;
}
.org-editor .linkish {
  border: 0;
  background: none;
  padding: 0;
  color: var(--bad);
  text-decoration: underline;
  cursor: pointer;
  text-align: left;
}
.org-editor .warnings {
  margin: 6px 0;
  color: var(--warn);
}
.org-editor .warnings ul {
  margin: 4px 0;
  padding-left: 18px;
  color: var(--muted);
}
.org-editor .spacer {
  flex: 1;
}
.org-editor .small-text {
  font-size: 12px;
}
/* поля: подпись над полем, всегда видимая (Baymard) */
.org-editor .field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.org-editor .field > .label,
.org-editor .field > summary {
  font-weight: 600;
  font-size: 13px;
}
.org-editor .fields {
  display: grid;
  gap: 12px 16px;
}
.org-editor .fields.two {
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
}
.org-editor .fields.three {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
}
.org-editor .sub {
  border-top: 1px solid var(--border);
  padding: 12px 0;
}
.org-editor .sub h4 {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  margin: 0 0 8px;
}
.org-editor table.edit td {
  vertical-align: top;
}
.org-editor tr.off td {
  opacity: 0.6;
}
.org-editor input[type='number'].num-in {
  width: 110px;
}
.org-editor input,
.org-editor select,
.org-editor textarea {
  font: inherit;
  padding: 4px 8px;
}
.org-editor textarea {
  width: 100%;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12.5px;
}
.org-editor .grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  align-items: center;
}
.org-editor .grid.two > * {
  flex: 1 1 280px;
}
.org-editor label.block {
  display: block;
  margin: 6px 0;
}
.org-editor .json {
  max-height: 70vh;
  overflow: auto;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 8px;
  font-size: 12px;
}
.org-editor .hint {
  font-size: 12px;
}
@media (max-width: 900px) {
  .org-editor .ed-body {
    grid-template-columns: 1fr;
  }
  .org-editor .ed-nav {
    position: static;
    flex-direction: row;
    flex-wrap: wrap;
  }
}
</style>
