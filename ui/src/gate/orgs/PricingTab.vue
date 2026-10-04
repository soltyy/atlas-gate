<script setup lang="ts">
// Вкладка 3 «Цены»: валюта, вход без кэша / из кэша / выход за 1 млн токенов, версия прайса.
// Цена подписки (subscription) — нули, только чтение: ход на подписке деньгами не считается.
import { computed } from 'vue'
import FieldErr from './FieldErr.vue'
import type { OrgConfig } from './types'

const props = defineProps<{ org: OrgConfig; errors: Record<string, string> }>()

const unpriced = computed(() =>
  props.org.models.filter((m) => !props.org.pricing.some((p) => p.route_id === m.route_id && p.model === m.model)),
)

function isSubscription(routeId: string, subscription: boolean): boolean {
  return subscription || props.org.routes.find((r) => r.id === routeId)?.kind === 'router-agent'
}

function addFor(key: string) {
  const [route_id, model] = key.split('\u0000')
  const sub = props.org.routes.find((r) => r.id === route_id)?.kind === 'router-agent'
  props.org.pricing.push({
    route_id, model, currency: props.org.quota.currency || 'USD', input_uncached: 0, input_cached: 0, output: 0,
    pricing_version: '1', subscription: sub,
  })
}

// Подписка — одной строкой: цены там нулевые и не правятся (#12).
const paid = computed(() => props.org.pricing.map((p, i) => ({ p, i })).filter(({ p }) => !isSubscription(p.route_id, p.subscription)))
const subscription = computed(() => props.org.pricing.filter((p) => isSubscription(p.route_id, p.subscription)))

function remove(i: number) {
  props.org.pricing.splice(i, 1)
}
</script>

<template>
  <p v-if="subscription.length" class="muted" data-sub-prices>
    Подписка: {{ subscription.map((p) => p.model).join(', ') }} — 0 за токены, в квоту не входят.
  </p>
  <div class="table-wrap">
    <table v-if="paid.length" class="edit">
      <thead>
        <tr><th>маршрут</th><th>модель</th><th>валюта</th><th>вход, за 1 млн</th><th>вход из кэша, за 1 млн</th><th>выход, за 1 млн</th><th>версия прайса</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="{ p, i } in paid" :key="i">
          <td>{{ p.route_id }}</td>
          <td><code>{{ p.model }}</code></td>
            <td><input v-model.trim="p.currency" type="text" size="4" :data-f="`pricing.${i}.currency`" /></td>
            <td><input v-model.number="p.input_uncached" type="number" min="0" step="0.01" class="num-in" :data-f="`pricing.${i}.input_uncached`" /><FieldErr :errors="errors" :loc="`pricing.${i}.input_uncached`" /></td>
            <td><input v-model.number="p.input_cached" type="number" min="0" step="0.01" class="num-in" :data-f="`pricing.${i}.input_cached`" /><FieldErr :errors="errors" :loc="`pricing.${i}.input_cached`" /></td>
            <td><input v-model.number="p.output" type="number" min="0" step="0.01" class="num-in" :data-f="`pricing.${i}.output`" /><FieldErr :errors="errors" :loc="`pricing.${i}.output`" /></td>
            <td><input v-model.trim="p.pricing_version" type="text" size="8" :data-f="`pricing.${i}.pricing_version`" /></td>
          <td><button type="button" class="small" :title="`Убрать цену ${p.model}`" @click="remove(i)">Убрать</button></td>
        </tr>
      </tbody>
    </table>
    <p v-else class="muted">Платных моделей нет.</p>
  </div>
  <FieldErr :errors="errors" loc="pricing" />
  <p v-if="unpriced.length" class="row">
    <span class="muted">без цены (стоимость вызовов пишется null):</span>
    <button v-for="m in unpriced" :key="m.route_id + m.model" type="button" class="small" @click="addFor(m.route_id + '\u0000' + m.model)">
      + {{ m.route_id }} / {{ m.model }}
    </button>
  </p>
</template>
