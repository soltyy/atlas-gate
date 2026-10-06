<script setup lang="ts">
// Агенты (#12): список слева, карточка справа. Правка сохранённого агента — обычная правка: первое
// изменение заводит новую версию (копию последней с номером +1), прежние версии остаются только для
// просмотра. Агента можно удалить целиком. Инструменты — «Все» или «Только выбранные» (ToolPicker).
import { computed, reactive, ref, watch } from 'vue'
import FieldErr from './FieldErr.vue'
import ToolPicker from './ToolPicker.vue'
import ModelMetrics from './ModelMetrics.vue'
import type { Agent, OrgConfig, EditorContext } from './types'
import { ALL_TOOLS, allTools, clone, toolCatalog } from './util'

const props = defineProps<{ org: OrgConfig; errors: Record<string, string>; saved: Set<string>; ctx?: EditorContext }>()

const key = (a: Agent) => `${a.id}@${a.version}`
const ids = computed(() => [...new Set(props.org.agents.map((a) => a.id))])
const versionsOf = (id: string) => props.org.agents.filter((a) => a.id === id)
const head = (id: string): Agent | undefined => versionsOf(id).slice(-1)[0]
const catalog = computed(() => toolCatalog(props.org))
const selectedId = ref<string | null>(ids.value[0] ?? null)
watch(ids, (list) => {
  if (selectedId.value === null || !list.includes(selectedId.value)) selectedId.value = list[0] ?? null
})
const current = computed(() => (selectedId.value === null ? undefined : head(selectedId.value)))
// Ошибка сервера у агента (loc «agents.N.…») — открыть этого агента, чтобы ошибка была видна у поля.
watch(() => props.errors, (errs) => {
  const loc = Object.keys(errs).find((l) => /^agents\.\d+/.test(l))
  if (!loc) return
  const a = props.org.agents[Number(loc.split('.')[1])]
  if (a) selectedId.value = a.id
}, { immediate: true })
const isNew = computed(() => !!current.value && versionsOf(current.value.id).every((a) => !props.saved.has(key(a))))
// id правится у нового агента и у сохранённого с неверным id (пустой агент прежних версий редактора).
const AGENT_ID = /^[A-Za-z0-9_-]{1,64}$/
const idEditable = computed(() => isNew.value || (!!current.value && !AGENT_ID.test(current.value.id)))

/** Изменяемая версия агента: несохранённая последняя или новая версия поверх сохранённой. */
function editable(id: string): Agent {
  const last = head(id)!
  if (!props.saved.has(key(last))) return last
  const nums = versionsOf(id).map((a) => Number(a.version))
  const next = nums.every(Number.isInteger) ? Math.max(0, ...nums) + 1 : versionsOf(id).length + 1
  const copy = clone(last)
  copy.version = String(next)
  if (last.default) {
    last.default = false
    copy.default = true
  }
  props.org.agents.push(copy)
  return copy
}

function set<K extends keyof Agent>(field: K, value: Agent[K]) {
  if (!current.value) return
  const a = editable(current.value.id)
  a[field] = value
}

function setId(value: string) {
  const a = current.value
  if (!a || !idEditable.value) return
  for (const v of versionsOf(a.id)) v.id = value
  selectedId.value = value
}

function indexOf(a: Agent | undefined): number {
  return a ? props.org.agents.indexOf(a) : -1
}

// --- модель и маршруты ---------------------------------------------------------------------------

const modelGroups = computed(() =>
  props.org.routes.map((r) => ({ route: r.id, models: props.org.models.filter((m) => m.route_id === r.id) }))
    .filter((g) => g.models.length))

const selectedModels = computed(() => props.org.models.filter(m => {
  const a = current.value
  return a && m.enabled && props.org.routes.some(r => r.id === m.route_id && r.enabled) &&
    (!a.model || a.model === 'inherit' || m.model === a.model) &&
    (!a.allowed_routes.length || a.allowed_routes.includes(m.route_id))
}))

function routeOfModel(model: string): string | undefined {
  return props.org.models.find((m) => m.model === model)?.route_id
}

function setModel(model: string) {
  set('model', model)
  const route = routeOfModel(model)
  const a = current.value!
  if (route && !a.allowed_routes.includes(route)) set('allowed_routes', [...a.allowed_routes, route])
}

const modelWarning = computed(() => {
  const a = current.value
  if (!a || !a.model) return ''
  const route = routeOfModel(a.model)
  if (!route) return `модели «${a.model}» нет среди моделей организации`
  if (!a.allowed_routes.includes(route)) return `модель идёт по маршруту ${route}, а он агенту не разрешён`
  return ''
})

function toggleIn(field: 'allowed_routes' | 'mcp_ids', value: string, on: boolean) {
  const list = current.value![field]
  set(field, on ? [...new Set([...list, value])] : list.filter((x) => x !== value))
}

// --- жизненный цикл --------------------------------------------------------------------------------

function addAgent() {
  const taken = new Set(ids.value)
  let n = ids.value.length + 1
  while (taken.has(`agent-${n}`)) n++
  const id = `agent-${n}`
  props.org.agents.push({
    id, version: '1', name: 'Новый агент', system_prompt: '', model: props.org.models[0]?.model ?? '',
    allowed_routes: props.org.models[0] ? [props.org.models[0].route_id] : [],
    tools_allowlist: allTools(catalog.value), mcp_ids: [], settings: {}, default: props.org.agents.length === 0,
  })
  selectedId.value = id
}

function makeDefault() {
  if (!current.value) return
  for (const a of props.org.agents) a.default = false
  current.value.default = true
}

function removeAgent() {
  const a = current.value
  if (!a) return
  const n = versionsOf(a.id).length
  if (!window.confirm(`Удалить агента «${a.name || a.id || "без имени"}» (${a.id || "без id"}) и все его версии (${n})? У устройств он исчезнет после сохранения.`)) return
  const wasDefault = versionsOf(a.id).some((v) => v.default)
  props.org.agents = props.org.agents.filter((v) => v.id !== a.id)
  if (wasDefault && props.org.agents.length) {
    const first = head(props.org.agents[0].id)!
    first.default = true
  }
}

// --- настройки (JSON) ------------------------------------------------------------------------------

const settingsError = reactive<Record<string, string>>({})
function setSettings(text: string) {
  const id = current.value!.id
  try {
    const v = text.trim() ? JSON.parse(text) : {}
    if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('нужен JSON-объект, например {"temperature": 0.2}')
    set('settings', v)
    delete settingsError[id]
  } catch (e) {
    settingsError[id] = (e as Error).message
  }
}

function toolsSummary(a: Agent): string {
  if (a.tools_allowlist.includes(ALL_TOOLS)) return 'все инструменты'
  return a.tools_allowlist.length ? `инструментов: ${a.tools_allowlist.length}` : 'без инструментов'
}

function hasErrors(id: string): boolean {
  return props.org.agents.some((a, i) => a.id === id && Object.keys(props.errors).some((l) => l.startsWith(`agents.${i}.`) || l === `agents.${i}`))
}
</script>

<template>
  <div class="agents">
    <aside class="agent-list">
      <button v-for="id in ids" :key="id" type="button" class="agent-item" :class="{ active: id === selectedId, bad: hasErrors(id) }"
        :data-agent="id" @click="selectedId = id">
        <span class="agent-name">{{ head(id)?.name || id || '(без id)' }}</span>
        <span class="muted"><code>{{ id || '—' }}</code> · v{{ head(id)?.version }}</span>
        <span class="muted small-text">{{ head(id)?.model || 'модель не выбрана' }} · {{ toolsSummary(head(id)!) }}</span>
        <span v-if="versionsOf(id).some((v) => v.default)" class="badge ok">по умолчанию</span>
      </button>
      <p v-if="!ids.length" class="muted">Агентов пока нет. Агент — это промпт, модель и набор инструментов, которые устройство получает в профиле.</p>
      <button type="button" class="small primary" data-act="add-agent" @click="addAgent">+ Новый агент</button>
    </aside>

    <section v-if="current" class="agent-card" :data-card="current.id">
      <div class="card-head">
        <h3>{{ current.name || current.id }}</h3>
        <span class="badge neutral">версия {{ current.version }}<template v-if="!saved.has(key(current))"> · не сохранена</template></span>
        <span class="spacer" />
        <label class="check"><input type="checkbox" :checked="current.default" data-f="agent.default" @change="makeDefault" /> агент по умолчанию</label>
        <button type="button" class="small danger" data-act="delete-agent" @click="removeAgent">Удалить агента</button>
      </div>
      <FieldErr :errors="errors" :loc="`agents.${indexOf(current)}`" deep />

      <div class="fields two">
        <label class="field">
          <span class="label">Название</span>
          <input :value="current.name" type="text" data-f="agent.name" @input="set('name', ($event.target as HTMLInputElement).value)" />
          <FieldErr :errors="errors" :loc="`agents.${indexOf(current)}.name`" />
        </label>
        <label class="field">
          <span class="label">id <span class="muted">— латиница, цифры, «-», «_»; после сохранения не меняется</span></span>
          <input :value="current.id" type="text" :readonly="!idEditable" data-f="agent.id" @input="setId(($event.target as HTMLInputElement).value.trim())" />
        </label>
      </div>

      <label class="field">
        <span class="label">Модель</span>
        <select :value="current.model" data-f="agent.model" @change="setModel(($event.target as HTMLSelectElement).value)">
          <option value="">— не выбрана —</option>
          <optgroup v-for="g in modelGroups" :key="g.route" :label="`маршрут ${g.route}`">
            <option v-for="m in g.models" :key="g.route + m.model" :value="m.model">{{ m.display_name || m.model }} ({{ m.model }})</option>
          </optgroup>
        </select>
        <span v-if="modelWarning" class="warn-text" data-model-warning>{{ modelWarning }}</span>
      </label>

      <div class="field">
        <span class="label">Разрешённые маршруты</span>
        <div class="chips">
          <label v-for="r in org.routes" :key="r.id" class="chip">
            <input type="checkbox" :checked="current.allowed_routes.includes(r.id)" :data-route-check="r.id"
              @change="toggleIn('allowed_routes', r.id, ($event.target as HTMLInputElement).checked)" /> {{ r.id }}
          </label>
        </div>
      </div>

      <ModelMetrics v-for="m in selectedModels" :key="`${m.route_id}/${m.model}`" :model="m"
        :contract="ctx?.model_contracts?.[m.route_id]?.[m.model]" />

      <ToolPicker :model-value="current.tools_allowlist" :catalog="catalog" @update:model-value="set('tools_allowlist', $event)" />

      <label class="field">
        <span class="label">Системный промпт</span>
        <textarea :value="current.system_prompt" rows="8" data-f="agent.system_prompt"
          @input="set('system_prompt', ($event.target as HTMLTextAreaElement).value)" />
      </label>

      <div class="field">
        <span class="label">MCP-серверы</span>
        <p v-if="!org.mcp.length" class="muted">В организации нет MCP-серверов — добавьте их в разделе «MCP».</p>
        <div v-else class="chips">
          <label v-for="m in org.mcp" :key="m.id" class="chip">
            <input type="checkbox" :checked="current.mcp_ids.includes(m.id)" @change="toggleIn('mcp_ids', m.id, ($event.target as HTMLInputElement).checked)" />
            {{ m.name || m.id }}
          </label>
        </div>
      </div>

      <details class="field">
        <summary>Дополнительно: параметры модели (JSON)</summary>
        <textarea :value="JSON.stringify(current.settings, null, 2)" rows="4" data-f="agent.settings"
          @change="setSettings(($event.target as HTMLTextAreaElement).value)" />
        <span v-if="settingsError[current.id]" class="field-error">{{ settingsError[current.id] }}</span>
      </details>

      <details v-if="versionsOf(current.id).length > 1" class="field versions">
        <summary>Версии агента ({{ versionsOf(current.id).length }})</summary>
        <ul>
          <li v-for="v in versionsOf(current.id)" :key="v.version">
            <strong>v{{ v.version }}</strong>
            <span class="muted"> {{ saved.has(key(v)) ? 'сохранена' : 'новая, запишется при сохранении' }} · {{ v.model || 'без модели' }} · {{ toolsSummary(v) }}</span>
          </li>
        </ul>
      </details>
    </section>
  </div>
</template>

<style scoped>
.agents {
  display: grid;
  grid-template-columns: 260px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}
.agent-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.agent-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  text-align: left;
  padding: 8px 10px;
  border-radius: var(--radius);
}
.agent-item.active {
  border-color: var(--accent);
  box-shadow: inset 3px 0 0 var(--accent);
}
.agent-item.bad {
  border-color: var(--bad);
}
.agent-name {
  font-weight: 600;
}
.agent-card {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.card-head h3 {
  margin: 0;
  font-size: 16px;
}
.spacer {
  flex: 1;
}
.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
}
.chip,
.check {
  display: inline-flex;
  gap: 6px;
  align-items: center;
}
.versions ul {
  margin: 6px 0 0;
  padding-left: 18px;
}
.small-text {
  font-size: 12px;
}
.warn-text {
  color: var(--warn);
  font-size: 12px;
}
.field-error {
  color: var(--bad);
  font-size: 12px;
}
@media (max-width: 900px) {
  .agents {
    grid-template-columns: 1fr;
  }
}
</style>
