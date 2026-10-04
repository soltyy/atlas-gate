<script setup lang="ts">
// Вкладка 1 «Маршруты»: id, вид, протокол, адрес апстрима, имя переменной ключа (+ задана ли она),
// таймаут, включён ли, состояние апстрима. У router-agent нет апстрима и ключа — поля скрыты.
import FieldErr from './FieldErr.vue'
import type { EditorContext, OrgConfig, Route } from './types'

const props = defineProps<{ org: OrgConfig; errors: Record<string, string>; ctx: EditorContext; saved: Set<string> }>()

function setKind(r: Route, kind: Route['kind']) {
  r.kind = kind
  if (kind === 'router-agent') {
    r.protocol = 'atlas-agent'
    r.upstream_url = ''
    r.key_env = ''
  } else if (r.protocol === 'atlas-agent') r.protocol = 'openai'
}

function add() {
  props.org.routes.push({
    id: '', kind: 'gateway', protocol: 'openai', upstream_url: '', key_env: '', timeout_s: 120,
    enabled: true, region: '', zdr: false, max_data_class: '',
  })
}

function remove(i: number) {
  props.org.routes.splice(i, 1)
}

/** Сохранённый маршрут без вызовов и устройств с его моделями удаляется вместе со ссылками на него
 *  (модели, цены, матрица классов, маршруты агентов); иначе — только выключить (GW-ADMIN-03). */
function deletable(r: Route): boolean {
  return props.saved.has(r.id) && props.ctx.route_usage[r.id]?.deletable === true
}

function removeSaved(i: number) {
  const id = props.org.routes[i].id
  const models = props.org.models.filter((m) => m.route_id === id).length
  if (!window.confirm(`Удалить маршрут ${id}? Вместе с ним уйдут его модели (${models}), цены и ссылки в политике и агентах. Удаление запишется при сохранении.`)) return
  const o = props.org
  o.routes.splice(i, 1)
  o.models = o.models.filter((m) => m.route_id !== id)
  o.pricing = o.pricing.filter((p) => p.route_id !== id)
  for (const cls of Object.keys(o.policy.routes_by_class)) {
    o.policy.routes_by_class[cls] = o.policy.routes_by_class[cls].filter((rid) => rid !== id)
  }
  for (const a of o.agents) a.allowed_routes = a.allowed_routes.filter((rid) => rid !== id)
}

function keyState(r: Route): 'ok' | 'bad' | 'neutral' {
  if (!r.key_env) return 'neutral'
  return props.ctx.secrets[r.key_env] ? 'ok' : 'bad'
}
</script>

<template>
  <div class="table-wrap">
    <table class="edit">
      <thead>
        <tr><th>id</th><th>вид</th><th>протокол</th><th>апстрим</th><th>ключ (имя переменной)</th><th>таймаут, с</th><th>вкл.</th><th>состояние</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="(r, i) in org.routes" :key="i" :class="{ off: !r.enabled }">
          <td>
            <input v-model.trim="r.id" type="text" size="14" :readonly="saved.has(r.id)" :data-f="`routes.${i}.id`" />
            <FieldErr :errors="errors" :loc="`routes.${i}.id`" />
          </td>
          <td>
            <select :value="r.kind" :data-f="`routes.${i}.kind`" @change="setKind(r, ($event.target as HTMLSelectElement).value as Route['kind'])">
              <option value="gateway">gateway</option>
              <option value="router-agent">router-agent</option>
            </select>
            <FieldErr :errors="errors" :loc="`routes.${i}.kind`" />
          </td>
          <td>
            <select v-if="r.kind === 'gateway'" v-model="r.protocol" :data-f="`routes.${i}.protocol`">
              <option value="openai">openai</option>
              <option value="anthropic">anthropic</option>
            </select>
            <span v-else class="muted">atlas-agent</span>
            <FieldErr :errors="errors" :loc="`routes.${i}.protocol`" />
          </td>
          <template v-if="r.kind === 'gateway'">
            <td>
              <input v-model.trim="r.upstream_url" type="text" size="26" placeholder="https://api.example.com/v1" :data-f="`routes.${i}.upstream_url`" />
              <FieldErr :errors="errors" :loc="`routes.${i}.upstream_url`" />
            </td>
            <td>
              <input v-model.trim="r.key_env" type="text" size="16" placeholder="PROVIDER_API_KEY" :data-f="`routes.${i}.key_env`" />
              <span class="badge" :class="keyState(r)" :title="r.key_env ? 'значение гейт не показывает' : ''">
                {{ !r.key_env ? '—' : ctx.secrets[r.key_env] ? 'задан' : 'нет' }}
              </span>
              <FieldErr :errors="errors" :loc="`routes.${i}.key_env`" />
            </td>
          </template>
          <td v-else colspan="2" class="muted">ход на подписке роутера — без апстрима и ключа</td>
          <td>
            <input v-model.number="r.timeout_s" type="number" min="1" step="1" class="num-in" :data-f="`routes.${i}.timeout_s`" />
            <FieldErr :errors="errors" :loc="`routes.${i}.timeout_s`" />
          </td>
          <td><input v-model="r.enabled" type="checkbox" :data-f="`routes.${i}.enabled`" /></td>
          <td>
            <span v-if="!r.enabled" class="badge neutral">выключен</span>
            <span v-else-if="r.kind !== 'gateway'" class="badge neutral">подписка</span>
            <span v-else-if="ctx.upstreams[r.id]" class="badge" :class="ctx.upstreams[r.id] === 'ok' ? 'ok' : 'bad'">{{ ctx.upstreams[r.id] }}</span>
            <span v-else class="muted">после сохранения</span>
          </td>
          <td>
            <button v-if="!saved.has(r.id)" type="button" class="small" title="Убрать несохранённый маршрут" @click="remove(i)">×</button>
            <button v-else-if="deletable(r)" type="button" class="small danger" :data-act="`delete-route-${r.id}`"
              title="Вызовов и устройств с его моделями нет — маршрут можно удалить" @click="removeSaved(i)">Удалить</button>
            <span v-else-if="ctx.route_usage[r.id]" class="muted small-text" :data-keep="r.id">только выключить: {{ ctx.route_usage[r.id].reason }}</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
  <FieldErr :errors="errors" loc="routes" />
  <button type="button" class="small" data-act="add-route" @click="add">Добавить маршрут</button>
  <p class="muted hint">Маршрут без вызовов и без устройств с его моделями можно удалить. Остальные — только выключить: модели маршрута уйдут из профиля устройств, учёт вызовов сохранится.</p>
</template>

<style scoped>
.small-text {
  font-size: 12px;
}
</style>
