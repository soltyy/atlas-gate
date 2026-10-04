<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { gateApi, type RouterNode, type NodeEnrollment } from './api'
const nodes = ref<RouterNode[]>([])
const pending = ref<NodeEnrollment[]>([])
const error = ref('')
const invitation = ref('')
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
async function invite(node: RouterNode) {
  await act(async () => {
    const result = await gateApi.nodeInvite(node.node_id)
    invitation.value = result.invitation
  })
}
function revoke(node: RouterNode) {
  if (window.confirm(`Отозвать Router ${node.node_id}? Его текущие беседы станут недоступны до восстановления связи.`))
    void act(() => gateApi.nodeRevoke(node.node_id))
}
function status(n: RouterNode): string {
  if (n.revoked) return 'Отозван'
  if (!n.enabled) return 'Выключен'
  if (!n.status) return 'Нет связи'
  if (n.status.draining) return 'Завершает работу'
  if (n.cooldown_seconds > 0) return 'Ограничение подписки'
  return n.status.ready ? 'Готов' : 'Нужен вход SDK'
}
onMounted(() => void refresh())
</script>

<template>
  <section class="card">
    <h2>Router <span class="spacer" /><button :disabled="busy" @click="refresh">Обновить</button></h2>
    <p v-if="error" class="error">{{ error }}</p>
    <p class="muted">Новые беседы распределяются по свободным ресурсам. Продолжения остаются на выбранном Router.</p>
    <p v-if="invitation" class="note">Приглашение действует 15 минут. Передайте его администратору Router: <code>{{ invitation }}</code><button @click="invitation = ''">Скрыть</button></p>
    <div v-for="e in pending" :key="e.id" class="card">
      <strong>Ожидает одобрения: {{ e.node_id }}</strong>
      <p>Сверьте отпечаток заявки с машиной Router: <code>{{ e.fingerprint }}</code></p>
      <button :disabled="busy" @click="act(() => gateApi.nodeApprove(e.id))">Одобрить</button>
    </div>
    <p v-if="!nodes.length" class="muted">Добавьте Router и его организации, маршруты и группу аккаунта в конфигурацию Gate.</p>
    <div v-for="n in nodes" :key="n.node_id" class="card">
      <h3>{{ n.node_id }} · {{ status(n) }}</h3>
      <p>{{ n.status?.backend ?? 'SDK не определён' }} · {{ n.transport === 'connector' ? 'Исходящее подключение' : 'Прямое подключение' }} · аккаунт {{ n.account_group }}</p>
      <p>Организации: {{ n.orgs.join(', ') }}. Маршруты: {{ n.routes.join(', ') }}.</p>
      <p v-if="n.status">Ресурсы ходов: {{ n.status.reservedUnits }}/{{ n.status.turnCapacity }}; субагенты: {{ n.status.activeSubagents }}; сессии: {{ n.status.sessions }}/{{ n.status.capacity }}.</p>
      <p>Предел аккаунта: {{ n.account_turn_capacity }}. Остаток подписки: {{ n.status?.quota?.status ?? 'нет данных SDK' }}.</p>
      <p v-if="n.cooldown_seconds > 0">Повторная проверка ограничения через {{ Math.ceil(n.cooldown_seconds) }} с.</p>
      <p v-if="n.credential">Удостоверение действует до {{ new Date(n.credential.expires * 1000).toLocaleString() }}.</p>
      <div class="row">
        <button :disabled="busy || !n.status || n.revoked" @click="act(() => gateApi.nodeDrain(n.node_id, !n.status?.draining))">{{ n.status?.draining ? 'Вернуть в работу' : 'Завершить работу' }}</button>
        <button :disabled="busy || !n.enabled" @click="invite(n)">Пригласить / сменить ключ</button>
        <button class="danger" :disabled="busy || n.revoked" @click="revoke(n)">Отозвать</button>
      </div>
      <details v-if="n.bindings.length"><summary>Беседы: {{ n.bindings.length }}</summary><p v-for="b in n.bindings" :key="b.id"><code>{{ b.id }}</code> · {{ b.route }} · {{ b.status }}</p></details>
    </div>
  </section>
</template>
