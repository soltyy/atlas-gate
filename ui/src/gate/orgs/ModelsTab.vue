<script setup lang="ts">
// Вкладка 2 «Модели» по маршрутам: id, имя, окно, максимум ответа, включена. У router-agent —
// галочки из списка моделей подписки роутера (/v1/models): отмеченная модель попадает в конфигурацию.
import { computed } from 'vue'
import FieldErr from './FieldErr.vue'
import type { EditorContext, ModelEntry, OrgConfig, Route } from './types'
import { fmtNum } from './util'

const props = defineProps<{ org: OrgConfig; errors: Record<string, string>; ctx: EditorContext }>()

const indexed = computed(() => props.org.models.map((m, i) => ({ m, i })))

function modelsOf(r: Route) {
  return indexed.value.filter(({ m }) => m.route_id === r.id)
}

/** Модели подписки плюс уже настроенные (даже если роутер их сейчас не называет). */
function subscriptionChoices(r: Route): string[] {
  const set = new Set(props.ctx.subscription_models)
  for (const { m } of modelsOf(r)) set.add(m.model)
  return [...set].sort()
}

function entry(r: Route, model: string): ModelEntry | undefined {
  return props.org.models.find((m) => m.route_id === r.id && m.model === model)
}

function toggle(r: Route, model: string, on: boolean) {
  if (on && !entry(r, model)) {
    props.org.models.push({ route_id: r.id, model, display_name: model, context_window: 0, max_output: 0, enabled: true })
  } else if (!on) {
    const i = props.org.models.findIndex((m) => m.route_id === r.id && m.model === model)
    if (i >= 0) props.org.models.splice(i, 1)
  }
}

function setAll(r: Route, on: boolean) {
  for (const name of subscriptionChoices(r)) toggle(r, name, on)
}

function add(r: Route) {
  props.org.models.push({ route_id: r.id, model: '', display_name: '', context_window: 0, max_output: 0, enabled: true })
}

function remove(i: number) {
  props.org.models.splice(i, 1)
}

function idxOf(r: Route, model: string): number {
  return props.org.models.findIndex((m) => m.route_id === r.id && m.model === model)
}
</script>

<template>
  <FieldErr :errors="errors" loc="models" />
  <section v-for="r in org.routes" :key="r.id" class="sub" :data-route="r.id">
    <h4>{{ r.id || '(без id)' }} <span class="muted">{{ r.kind === 'router-agent' ? 'подписка роутера' : 'провайдер' }}</span>
      <span v-if="!r.enabled" class="badge neutral">маршрут выключен</span>
      <template v-if="r.kind === 'router-agent'">
        <span class="spacer" />
        <button type="button" class="small" :data-act="`sub-all-${r.id}`" @click="setAll(r, true)">Выбрать все</button>
        <button type="button" class="small" :data-act="`sub-none-${r.id}`" @click="setAll(r, false)">Снять все</button>
      </template>
    </h4>

    <template v-if="r.kind === 'router-agent'">
      <p class="muted hint">Отметьте модели подписки, которые получат устройства. Отмеченная — в профиле; снятая — нет.</p>
      <p v-if="!ctx.subscription_models.length" class="muted">список моделей подписки роутера сейчас недоступен</p>
      <table class="edit">
        <thead><tr><th>в профиле</th><th>модель подписки</th><th>название для людей</th><th>окно, токенов</th><th>макс. ответ, токенов</th></tr></thead>
        <tbody>
          <tr v-for="name in subscriptionChoices(r)" :key="name">
            <td><input type="checkbox" :checked="!!entry(r, name)" :data-sub="name" @change="toggle(r, name, ($event.target as HTMLInputElement).checked)" /></td>
            <td>
              <code>{{ name }}</code>
              <span v-if="!ctx.subscription_models.includes(name)" class="badge warn">нет у роутера</span>
            </td>
            <template v-if="entry(r, name)">
              <td><input v-model.trim="entry(r, name)!.display_name" type="text" size="26" /></td>
              <td><input v-model.number="entry(r, name)!.context_window" type="number" min="0" class="num-in" /><div class="muted num-hint">{{ fmtNum(entry(r, name)!.context_window) }}</div></td>
              <td>
                <input v-model.number="entry(r, name)!.max_output" type="number" min="0" class="num-in" /><div class="muted num-hint">{{ fmtNum(entry(r, name)!.max_output) }}</div>
                <FieldErr :errors="errors" :loc="`models.${idxOf(r, name)}`" deep />
              </td>
            </template>
            <td v-else colspan="3" class="muted">не в профиле</td>
          </tr>
        </tbody>
      </table>
    </template>

    <template v-else>
      <table class="edit">
        <thead><tr><th>id модели у провайдера</th><th>название для людей</th><th>окно, токенов</th><th>макс. ответ, токенов</th><th>включена</th><th></th></tr></thead>
        <tbody>
          <tr v-for="{ m, i } in modelsOf(r)" :key="i">
            <td><input v-model.trim="m.model" type="text" size="20" :data-f="`models.${i}.model`" /><FieldErr :errors="errors" :loc="`models.${i}.model`" /></td>
            <td><input v-model.trim="m.display_name" type="text" size="20" :data-f="`models.${i}.display_name`" /></td>
            <td><input v-model.number="m.context_window" type="number" min="0" class="num-in" :data-f="`models.${i}.context_window`" /><div class="muted num-hint">{{ fmtNum(m.context_window) }}</div><FieldErr :errors="errors" :loc="`models.${i}.context_window`" /></td>
            <td><input v-model.number="m.max_output" type="number" min="0" class="num-in" :data-f="`models.${i}.max_output`" /><div class="muted num-hint">{{ fmtNum(m.max_output) }}</div><FieldErr :errors="errors" :loc="`models.${i}.max_output`" /></td>
            <td><input v-model="m.enabled" type="checkbox" :data-f="`models.${i}.enabled`" /></td>
            <td><button type="button" class="small" title="Убрать модель" @click="remove(i)">×</button></td>
          </tr>
        </tbody>
      </table>
      <button type="button" class="small" :data-act="`add-model-${r.id}`" @click="add(r)">Добавить модель</button>
    </template>
  </section>
</template>

<style scoped>
.num-hint {
  font-size: 11px;
}
</style>
