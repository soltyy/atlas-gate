<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { gateApi, type RouterNode, type NodeEnrollment } from './api'
import NodeWizard from './NodeWizard.vue'
const adding = ref(false)
const resuming = ref<RouterNode>()
const nodes = ref<RouterNode[]>([])
const pending = ref<NodeEnrollment[]>([])
const fingerprintChecked = ref<Record<string, boolean>>({})
const error = ref('')
const busy = ref(false)
async function refresh() {
  busy.value = true
  try {
    const [n, e] = await Promise.all([gateApi.nodes(), gateApi.nodeEnrollments()])
    nodes.value = n.nodes; pending.value = e.enrollments; error.value = ''
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function act(action: () => Promise<unknown>) {
  busy.value = true
  try { await action(); await refresh() }
  catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
function instruction(node: RouterNode) { resuming.value = node; adding.value = true }
function count(used: number, limit: number) { return limit ? `${used}/${limit}` : `${used} занято · без заданного лимита` }
function quota(node: RouterNode) {
  const q = node.status?.quota?.status
  return q === 'allowed' ? 'SDK разрешает работу' : q === 'allowed_warning' ? 'SDK предупреждает об остатке' : q === 'rejected' ? 'SDK сообщает об исчерпании' : 'SDK не передал сведения'
}
function revoke(node: RouterNode) {
  if (window.confirm(`Отозвать Router ${node.node_id}? Его текущие беседы станут недоступны до восстановления связи.`))
    void act(() => gateApi.nodeRevoke(node.node_id))
}
function status(n: RouterNode): string {
  if (n.revoked) return 'Отозван'
  if (!n.enabled) return 'Выключен'
  if (!n.status) return 'Нет связи'
  if (n.status.draining) return 'Приём новых ходов остановлен'
  if (n.cooldown_seconds > 0) return 'Ограничение подписки'
  return n.status.ready ? 'Готов' : 'Нужен вход SDK'
}
onMounted(() => void refresh())
</script>

<template>
  <section class="card">
    <h2>Router <span class="spacer" /><button class="primary" :disabled="adding" @click="resuming = undefined; adding = true">Добавить Router</button><button :disabled="busy" @click="refresh">Обновить</button></h2>
    <p v-if="error" class="error">{{ error }}</p>
    <p class="muted">Новые беседы распределяются по свободным ресурсам. Продолжения остаются на выбранном Router.</p>
    <NodeWizard v-if="adding" :key="resuming?.node_id ?? 'new'" :nodes="nodes" :resume="resuming" @close="adding = false; resuming = undefined" @refresh="refresh" />
    <div v-for="e in pending" :key="e.id" class="card">
      <strong>Ожидает одобрения: {{ e.node_id }}</strong>
      <p>Сверьте отпечаток заявки с машиной Router: <code>{{ e.fingerprint }}</code></p>
      <p class="muted">Заявка действует до {{ new Date(e.expires * 1000).toLocaleString() }}.</p>
      <label><input v-model="fingerprintChecked[e.id]" type="checkbox"> Отпечаток совпадает с выводом команды на машине Router</label>
      <p><button :disabled="busy || !fingerprintChecked[e.id]" @click="act(() => gateApi.nodeApprove(e.id))">Одобрить Router</button></p>
    </div>
    <p v-if="!nodes.length" class="muted">Подключённых Router пока нет. Нажмите «Добавить Router»: мастер поможет выбрать соединение, предоставить доступ и проверить готовность.</p>
    <div v-for="n in nodes" :key="n.node_id" class="card">
      <h3>{{ n.node_id }} · {{ status(n) }}</h3>
      <p>{{ n.status?.backend || n.backend || 'SDK не определён' }} · {{ n.transport === 'connector' ? 'Исходящее подключение' : 'Прямое подключение' }} · группа аккаунта {{ n.account_group }}</p>
      <p v-if="n.url" class="muted">Адрес Router: <code>{{ n.url }}</code> (с машины Gate)</p>
      <p>Организации: {{ n.orgs.join(', ') }}. Маршруты: {{ n.routes.join(', ') }}.</p>
      <div v-if="n.status" class="resources">
        <div><strong>Параллельные ходы</strong><p>{{ count(n.status.reservedUnits, n.status.turnCapacity) }}</p><small>Зарезервированные единицы: основной ход и субагенты.</small></div>
        <div><strong>Открытые SDK-сессии</strong><p>{{ count(n.status.sessions, n.status.capacity) }}</p><small>Включая простаивающие. Сохранённые беседы не считаются этим лимитом.</small></div>
        <div><strong>Работающие субагенты</strong><p>{{ n.status.activeSubagents }}</p><small>Дочерние исполнители внутри ходов.</small></div>
      </div>
      <p>Параллельная работа всей группы аккаунта: {{ n.account_turn_capacity ? `до ${n.account_turn_capacity} единиц` : 'без заданного лимита' }}.</p>
      <p>Состояние подписки: {{ quota(n) }}.</p>
      <p v-if="n.check_error && !n.status" class="error">{{ n.check_error }}</p>
      <details><summary>Как считаются ресурсы и лимиты?</summary><p>Числа справа — настройки оператора Router и Gate, а не предел тарифа. Значение 0 в конфигурации означает отсутствие заданного лимита. Обычный ход резервирует 1 единицу; при объявленных субагентах Router резервирует 1 + их предел. Узлы одного подписочного аккаунта делят общий лимит группы и квоту поставщика.</p><p>На Router это ATLAS_NODE_CAPACITY (SDK-сессии) и ATLAS_NODE_TURN_CAPACITY (единицы ходов); в Gate — account_turn_capacity. Лимиты подписки определяет поставщик и сообщает SDK, когда такие данные доступны.</p></details>
      <p v-if="n.cooldown_seconds > 0">Повторная проверка ограничения через {{ Math.ceil(n.cooldown_seconds) }} с.</p>
      <p v-if="n.credential">Удостоверение действует до {{ new Date(n.credential.expires * 1000).toLocaleString() }}.</p>
      <div class="row">
        <button :disabled="busy || !n.status || n.revoked" @click="act(() => gateApi.nodeDrain(n.node_id, !n.status?.draining))">{{ n.status?.draining ? 'Разрешить новые ходы' : 'Остановить приём новых ходов' }}</button>
        <button :disabled="busy || !n.enabled" @click="instruction(n)">Инструкция подключения</button>
        <button class="danger" :disabled="busy || n.revoked" @click="revoke(n)">Отозвать</button>
      </div>
      <details v-if="n.bindings.length"><summary>Беседы: {{ n.bindings.length }}</summary><p v-for="b in n.bindings" :key="b.id"><code>{{ b.id }}</code> · {{ b.route }} · {{ b.status }}</p></details>
    </div>
  </section>
</template>

<style scoped>
h2 { gap: 8px; flex-wrap: wrap; }
.resources { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 16px 0; }
.resources > div { background: var(--bg); border-radius: 6px; padding: 12px; }
.resources p { font-size: 18px; margin: 6px 0; }
small { color: var(--muted); }
details { margin: 14px 0; }
@media (max-width: 700px) { .resources { grid-template-columns: 1fr; } }
</style>
