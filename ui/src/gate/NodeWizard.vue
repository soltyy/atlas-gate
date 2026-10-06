<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { gateApi, type NodeOptions, type RouterNode } from './api'

const props = defineProps<{ nodes: RouterNode[]; resume?: RouterNode }>()
const emit = defineEmits<{ close: []; refresh: [] }>()
const heading = ref<HTMLElement>()
const step = ref(props.resume ? 4 : 1)
const steps = ['Размещение', 'Доступ и аккаунт', 'Подготовка', 'Подключение']
const options = ref<NodeOptions>()
const placement = ref('connector')
const backend = ref('codex')
const nodeId = ref('')
const org = ref('')
const routes = ref<string[]>([])
const group = ref('new')
const groupId = ref('')
const capacity = ref(0)
const sessionCapacity = ref(0)
const turnCapacity = ref(0)
const subagents = ref(3)
const endpoint = ref('http://127.0.0.1:8765')
const localEndpoint = ref('http://127.0.0.1:8765')
const token = ref('')
const prepared = ref(false)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const created = ref(props.resume?.node_id ?? '')
const invitation = ref('')
const expires = ref(0)
const now = ref(Date.now())
const timer = setInterval(() => { now.value = Date.now() }, 10000)
onUnmounted(() => clearInterval(timer))
const platform = ref('windows')
const current = computed(() => props.nodes.find(n => n.node_id === created.value))
const compatibleRoutes = computed(() => options.value?.orgs.find(o => o.id === org.value)?.routes.filter(r => !r.backends.length || r.backends.includes(backend.value)) ?? [])
const groups = computed(() => options.value?.account_groups.filter(g => !g.backends.length || g.backends.includes(backend.value)) ?? [])
const selectedGroup = computed(() => groups.value.find(g => g.id === group.value))
const directory = computed(() => `./atlas-data/${created.value || nodeId.value}/identity`)
const config = computed(() => [
  `ATLAS_NODE_ID=${nodeId.value}`, `ATLAS_BACKEND=${backend.value}`, 'ATLAS_GATE_ENABLED=false',
  'ATLAS_TRUST_LOCAL=false', 'ATLAS_TOKEN=ЗАМЕНИТЕ_СЛУЧАЙНЫМ_КЛЮЧОМ',
  `ATLAS_NODE_DB=./atlas-data/${nodeId.value}/node.db`,
  `ATLAS_NODE_CAPACITY=${sessionCapacity.value}`, `ATLAS_NODE_TURN_CAPACITY=${turnCapacity.value}`,
  `ATLAS_NODE_SUBAGENTS=${subagents.value}`,
].join('\n'))
const enrollCommand = computed(() => `uv run atlas-node enroll --directory "${directory.value}" --gate "${options.value?.gate_url}" --node-id "${created.value}"`)
const runCommand = computed(() => {
  if (!/^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?$/.test(localEndpoint.value)) return 'Укажите корректный локальный адрес Router.'
  let gate = options.value?.gate_url ?? ''
  if (options.value?.mtls_port) {
    try { const u = new URL(gate); u.port = String(options.value.mtls_port); gate = u.origin } catch { /* options error shown */ }
  }
  return `uv run atlas-node run --directory "${directory.value}" --router "${localEndpoint.value}" --gate "${gate}"${options.value?.mtls_port ? ' --ca "./gate-ca.pem" --mtls' : ''}`
})
watch([backend, org], () => { routes.value = []; group.value = 'new' })
watch(placement, p => { endpoint.value = p === 'remote' ? '' : 'http://127.0.0.1:8765' })
watch(step, async () => { await nextTick(); heading.value?.focus() })
function next() {
  error.value = ''
  if (step.value === 1 && (!/^[A-Za-z0-9_-]{1,64}$/.test(nodeId.value) || props.nodes.some(n => n.node_id === nodeId.value))) {
    error.value = 'Введите свободный идентификатор: латиница, цифры, дефис или подчёркивание, до 64 знаков.'; return
  }
  if (step.value === 2 && (!org.value || !routes.value.length || (group.value === 'new' && !/^[A-Za-z0-9_-]{1,64}$/.test(groupId.value || `${backend.value}-${nodeId.value}`)))) {
    error.value = 'Выберите организацию, хотя бы один маршрут и корректную группу аккаунта.'; return
  }
  step.value++
}
async function refreshOptions() {
  options.value = await gateApi.nodeOptions()
  if (!org.value) org.value = options.value.orgs[0]?.id ?? ''
}
async function invite() {
  busy.value = true; error.value = ''
  try {
    const result = await gateApi.nodeInvite(created.value)
    invitation.value = result.invitation; expires.value = Date.now() + result.expiresIn * 1000
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}
async function add() {
  error.value = ''; busy.value = true
  try {
    const result = await gateApi.nodeAdd({ revision: options.value!.revision, node_id: nodeId.value,
      backend: backend.value, transport: placement.value === 'connector' ? 'connector' : 'direct',
      org: org.value, routes: routes.value, account_group: selectedGroup.value?.id || groupId.value || `${backend.value}-${nodeId.value}`,
      account_turn_capacity: selectedGroup.value?.capacity ?? capacity.value,
      share_account: !!selectedGroup.value,
      url: placement.value === 'connector' ? '' : endpoint.value, token: placement.value === 'connector' ? '' : token.value })
    token.value = ''; created.value = result.node_id; step.value = 4
    emit('refresh')
    if (placement.value === 'connector') await invite()
  } catch (e) {
    error.value = (e as Error).message
    // Новая ревизия не заменяет черновик: оператор может исправить конфликт и повторить.
    try { await refreshOptions() } catch { /* сохраняем исходную ошибку */ }
  } finally { busy.value = false }
}
async function copy(value: string) {
  try { await navigator.clipboard.writeText(value); notice.value = 'Скопировано' }
  catch { notice.value = 'Буфер обмена недоступен. Выделите и скопируйте текст вручную.' }
}
function close() { invitation.value = ''; token.value = ''; emit('close') }
onMounted(async () => {
  try {
    await refreshOptions()
    if (props.resume) {
      nodeId.value = props.resume.node_id; backend.value = props.resume.backend || props.resume.status?.backend || 'codex'
      placement.value = props.resume.transport === 'connector' ? 'connector' : 'same'
    }
  } catch (e) { error.value = (e as Error).message }
})
</script>

<template>
  <section class="wizard" aria-labelledby="node-wizard-title" :aria-busy="busy">
    <h3 id="node-wizard-title" ref="heading" tabindex="-1">{{ resume ? `Подключение ${created}` : 'Добавить Router' }} <span class="spacer"/><button :disabled="busy" @click="close">Закрыть</button></h3>
    <ol class="steps" aria-label="Этапы подключения"><li v-for="(label, i) in steps" :key="label" :aria-current="step === i + 1 ? 'step' : undefined" :class="{ current: step === i + 1, complete: step > i + 1 }">{{ i + 1 }}. {{ label }}</li></ol>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <p v-if="notice" role="status">{{ notice }}</p>
    <form v-if="step < 4 && options" @submit.prevent="step === 3 ? add() : next()">
      <div v-if="step === 1">
        <fieldset><legend>Где работает Router?</legend>
          <label class="choice"><input v-model="placement" type="radio" value="same"> <span><strong>На машине Gate</strong><small>Прямое соединение через 127.0.0.1. Для каждого Router свой порт.</small></span></label>
          <label class="choice"><input v-model="placement" type="radio" value="connector"> <span><strong>На другой машине, за NAT или в закрытой сети</strong><small>Router сам соединяется с Gate. Открывать входящий порт Router не нужно.</small></span></label>
          <label class="choice"><input v-model="placement" type="radio" value="remote"> <span><strong>На другой машине с доступным HTTPS</strong><small>Gate обращается к Router по защищённому адресу.</small></span></label>
        </fieldset>
        <label class="field">Поставщик подписки<select v-model="backend"><option value="codex">ChatGPT · Codex</option><option value="claude">Claude</option></select></label>
        <label class="field">Идентификатор Router<input v-model.trim="nodeId" placeholder="office-codex-02" maxlength="64" autocomplete="off" required><small>Постоянное имя этой установки. Такое же имя зададим в ATLAS_NODE_ID.</small></label>
        <p class="muted">Одна служба Router использует одного поставщика и один вход. На одной машине можно установить несколько служб с разными портами, каталогами данных и аккаунтами.</p>
      </div>
      <div v-if="step === 2">
        <label class="field">Организация<select v-model="org" required><option v-for="o in options.orgs" :key="o.id" :value="o.id">{{ o.name }} · {{ o.id }}</option></select></label>
        <fieldset><legend>Какие маршруты получат этот Router?</legend><label v-for="r in compatibleRoutes" :key="r.id" class="choice"><input v-model="routes" type="checkbox" :value="r.id">{{ r.id }}</label><p v-if="!compatibleRoutes.length" class="note">Сначала добавьте маршрут подписки этого поставщика в разделе «Организации».</p></fieldset>
        <label class="field">Подписочный аккаунт<select v-model="group"><option value="new">Другой аккаунт — отдельные ресурсы</option><option v-for="g in groups" :key="g.id" :value="g.id">Тот же аккаунт, что у группы {{ g.id }}</option></select><small>Один аккаунт на нескольких машинах делит квоту. Общая сеть или IP не объединяют разные аккаунты.</small></label>
        <label v-if="group === 'new'" class="field">Имя группы аккаунта<input v-model.trim="groupId" :placeholder="`${backend}-${nodeId}`" maxlength="64"><small>Можно оставить пустым: Gate создаст отдельную группу {{ backend }}-{{ nodeId }}.</small></label>
        <details v-if="group === 'new'"><summary>Задать ограничение параллельной работы аккаунта</summary><label class="field">Единиц одновременно<input v-model.number="capacity" type="number" min="0" max="256" required><small>0 — без заданного лимита. При положительном значении ограничение суммируется по всем Router этой группы. Это настройка Gate, а не предел тарифа.</small></label></details>
        <p v-else class="note">Общая группа {{ selectedGroup?.id }}: {{ selectedGroup?.capacity ? `до ${selectedGroup.capacity} единиц одновременно` : 'без заданного лимита параллельной работы' }} на всех её Router.</p>
      </div>
      <div v-if="step === 3">
        <h4>Подготовьте машину Router</h4>
        <ol><li>Установите Atlas Router и зависимости по <a href="https://github.com/soltyy/atlas-router/blob/codex/router-node-contract/docs/NODE-CONTRACT.md" target="_blank" rel="noopener">инструкции Router</a>.</li><li>Создайте отдельный каталог данных и SDK-профиль. Войдите в {{ backend === 'codex' ? 'ChatGPT через Codex' : 'Claude' }} от пользователя, который запускает эту службу.</li><li>Добавьте настройки ниже в её <code>.env</code>. Замените ключ на случайный и используйте абсолютный путь NODE_DB для службы.</li><li>Запустите службу. При ручной проверке из каталога проекта: <code>uv run atlas-router</code>. Проверьте вход и модели в локальной админке Router.</li></ol>
        <details><summary>Параллельная работа Router — настройки</summary>
          <label class="field">Открытые SDK-сессии<input v-model.number="sessionCapacity" type="number" min="0" max="256" required><small>0 — без заданного лимита. Простаивающая сессия тоже занимает слот. Это не количество сохранённых бесед.</small></label>
          <label class="field">Единицы параллельных ходов<input v-model.number="turnCapacity" type="number" min="0" max="256" required><small>0 — без заданного лимита.</small></label>
          <label class="field">Субагентов на ход<input v-model.number="subagents" type="number" min="0" :max="turnCapacity ? Math.max(0, turnCapacity - 1) : 256" required></label>
          <p class="muted">Обычный ход резервирует 1 единицу. При объявленных субагентах резерв равен 1 + их предел. Лимит Router и общий лимит аккаунта должны вмещать этот резерв.</p>
        </details>
        <pre>{{ config }}</pre><button type="button" @click="copy(config)">Копировать настройки</button>
        <p class="note">{{ nodeId }} · {{ backend }} · {{ org }} · {{ routes.join(', ') }} · группа {{ selectedGroup?.id || groupId || `${backend}-${nodeId}` }}</p>
        <template v-if="placement !== 'connector'">
          <label class="field">Адрес Router, доступный с машины Gate<input v-model.trim="endpoint" :placeholder="placement === 'same' ? 'http://127.0.0.1:8769' : 'https://router.example.org'" required><small>{{ placement === 'same' ? '127.0.0.1 означает машину Gate. Не адрес вашего браузера.' : 'Нужен HTTPS с доверенным сертификатом, без /v1 и других путей. Для закрытого адреса используйте исходящее соединение.' }}</small></label>
          <label class="field">Ключ доступа Router<input v-model="token" type="password" autocomplete="off" required><small>ATLAS_TOKEN именно этой службы Router. Gate сохранит его в защищённом файле; подписочный вход остаётся на Router.</small></label>
        </template>
        <label class="choice"><input v-model="prepared" type="checkbox" required>Настройки подготовлены. Идентификатор Router и подписочный аккаунт проверены.</label>
      </div>
      <div class="row footer"><button v-if="step > 1" type="button" :disabled="busy" @click="step--; error = ''">Назад</button><button type="submit" class="primary" :disabled="busy || (step === 3 && !prepared)">{{ busy ? 'Проверка…' : step === 3 ? (placement === 'connector' ? 'Добавить и получить приглашение' : 'Проверить и добавить') : 'Далее' }}</button></div>
    </form>
    <div v-if="step === 4">
      <p v-if="current?.status?.ready" class="note" role="status"><strong>Router подключён и готов.</strong> Новые беседы по выбранным маршрутам смогут использовать его. Существующие остаются на своих Router.</p>
      <template v-if="placement === 'connector'">
        <h4>1. Зарегистрируйте Router</h4>
        <p>Одноразовое приглашение действует 15 минут. Ключ SDK и локальный ATLAS_TOKEN передавать Gate не нужно.</p>
        <template v-if="invitation && now < expires"><p>Действует до {{ new Date(expires).toLocaleTimeString() }}. После истечения создайте новое.</p><pre class="secret">{{ invitation }}</pre><button @click="copy(invitation)">Копировать приглашение</button><button @click="invitation = ''">Скрыть приглашение</button></template>
        <button v-else :disabled="busy" @click="invite">Создать новое приглашение</button>
        <label class="field">Команды для<select v-model="platform"><option value="windows">Windows · PowerShell</option><option value="linux">Linux / macOS · bash</option></select></label>
        <p>В каталоге установленного Router введите приглашение через запрос ниже. Оно не попадёт в историю команд.</p>
        <pre>{{ platform === 'windows' ? '$env:ATLAS_NODE_INVITATION = Read-Host "Приглашение Gate"' : 'read -r -s -p "Приглашение Gate: " ATLAS_NODE_INVITATION\nexport ATLAS_NODE_INVITATION' }}
{{ enrollCommand }}
{{ platform === 'windows' ? 'Remove-Item Env:ATLAS_NODE_INVITATION' : 'unset ATLAS_NODE_INVITATION' }}</pre>
        <button @click="copy(enrollCommand)">Копировать команду регистрации</button>
        <h4>2. Одобрите заявку в Gate</h4><p>Оставьте команду регистрации работать. Нажмите «Проверить связь и заявки», сравните отпечаток с выводом команды на машине Router и одобрите заявку ниже.</p>
        <h4>3. Запустите исходящее соединение</h4>
        <label class="field">Локальный адрес Router<input v-model.trim="localEndpoint" pattern="https?://(127\.0\.0\.1|localhost|\[::1\])(:[0-9]+)?" placeholder="http://127.0.0.1:8765"><small>Адрес на машине Router. У нескольких служб разные порты.</small></label>
        <p>ATLAS_TOKEN локального Router должен быть в окружении команды или его .env. Запускайте connector от того же пользователя; для постоянной работы зарегистрируйте его как службу.</p>
        <p v-if="options?.mtls_port" class="note">Gate требует mTLS на порту {{ options.mtls_port }}. Получите gate-ca.pem у администратора и проверьте доступность порта. Он может отличаться от публичного HTTPS-порта.</p>
        <pre>{{ runCommand }}</pre><button @click="copy(runCommand)">Копировать команду подключения</button>
      </template>
      <p v-else-if="current?.status && !current.status.ready" class="note">Связь подтверждена, но SDK ещё не готов. Войдите в подписочный аккаунт на машине Router и проверьте каталог моделей.</p>
      <p v-if="current?.check_error" class="error">{{ current.check_error }}</p>
      <div class="row footer"><button :disabled="busy" @click="emit('refresh')">Проверить связь и заявки</button><button @click="close">{{ current?.status?.ready ? 'Готово' : 'Вернуться к списку' }}</button></div>
      <p class="muted">Подключение можно продолжить позднее через «Инструкция подключения» на карточке Router.</p>
    </div>
  </section>
</template>

<style scoped>
.wizard { border: 2px solid var(--accent); border-radius: var(--radius); padding: 20px; margin: 16px 0; background: var(--surface); }
h3 { display: flex; align-items: center; gap: 12px; margin-top: 0; }
.steps { display: flex; flex-wrap: wrap; list-style: none; gap: 8px; padding: 0; }
.steps li { padding: 6px 12px; border-radius: 6px; background: var(--bg); color: var(--muted); }
.steps .current { background: var(--accent); color: white; } .steps .complete { color: var(--ok); }
fieldset { border: 1px solid var(--border); border-radius: 6px; margin: 16px 0; padding: 12px; }
legend { font-weight: 600; }
.choice { display: flex; align-items: flex-start; gap: 10px; margin: 12px 0; }
.choice input { margin-top: 4px; }
small { display: block; color: var(--muted); font-size: 13px; margin: 4px 0; }
.field { display: grid; gap: 6px; max-width: 650px; margin: 16px 0; font-weight: 600; }
.field input, .field select { padding: 8px; font: inherit; border: 1px solid var(--border); border-radius: 6px; width: 100%; }
.field small { font-weight: 400; }
pre { background: var(--bg); border-radius: 6px; padding: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.secret { user-select: all; } .footer { margin-top: 20px; } h4 { margin-bottom: 8px; }
</style>
