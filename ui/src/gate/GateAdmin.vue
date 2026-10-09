<script setup lang="ts">
// Админка гейта: разделы «Устройства» (регистрации — одобрить/отклонить, устройства — отозвать) и
// «Организации» (список, редактор профиля, #6/#12). Ключ доступа — компактно в шапке: с машины роутера
// он обычно не нужен (доверие к loopback), а снаружи без него API отвечает 401.
import { onMounted, reactive, ref } from 'vue'
import { adminToken, gateApi, setAdminToken, type Device, type Enrollment } from './api'
import OrgsSection from './orgs/OrgsSection.vue'
import NodesSection from './NodesSection.vue'
import UsageSection from './UsageSection.vue'

const section = ref<'devices' | 'orgs' | 'nodes' | 'usage'>('devices')

const enrollments = ref<Enrollment[]>([])
const orgs = ref<string[]>([])
const devices = ref<Device[]>([])
const error = ref('')
const notice = ref('')
const busy = ref(false)
const token = ref(adminToken())

function applyToken() {
  setAdminToken(token.value)
  void refresh()
}
// Код из verification_url (?user_code=…) подсвечивается — его назвал сотрудник.
const highlighted = new URLSearchParams(window.location.search).get('user_code')?.toUpperCase() ?? ''
const forms = reactive<Record<string, { id: string; email: string; name: string; org: string }>>({})

function form(code: string) {
  if (!forms[code]) forms[code] = { id: '', email: '', name: '', org: orgs.value[0] ?? '' }
  return forms[code]
}

async function refresh() {
  try {
    const [e, d] = await Promise.all([gateApi.enrollments(), gateApi.devices()])
    enrollments.value = e.enrollments
    orgs.value = e.orgs
    devices.value = d.devices
    error.value = ''
  } catch (e) {
    error.value = (e as Error).message
  }
}

async function act(what: () => Promise<unknown>, done: string) {
  busy.value = true
  try {
    await what()
    notice.value = done
    error.value = ''
    await refresh()
  } catch (e) {
    error.value = (e as Error).message
  } finally {
    busy.value = false
  }
}

function approve(e: Enrollment) {
  const f = form(e.user_code)
  if (!f.id.trim() || !f.org) {
    error.value = 'нужны идентификатор сотрудника и организация'
    return
  }
  void act(() => gateApi.approve(e.user_code, f.org, { id: f.id.trim(), email: f.email.trim(), name: f.name.trim() }),
    `Регистрация ${e.user_code} одобрена`)
}

function deny(e: Enrollment) {
  void act(() => gateApi.deny(e.user_code), `Регистрация ${e.user_code} отклонена`)
}

function revoke(d: Device) {
  if (!window.confirm(`Отозвать устройство «${d.device_name}» (${d.user_email})?`)) return
  void act(() => gateApi.revoke(d.device_id), `Устройство ${d.device_name} отозвано`)
}

function when(ts: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleString() : '—'
}

onMounted(() => void refresh())
</script>

<template>
  <div class="gate-shell">
  <header class="shell-head">
    <strong class="brand">Гейт Atlas Harness</strong>
    <nav class="shell-nav" aria-label="Разделы админки">
      <button type="button" :class="{ active: section === 'devices' }" data-section="devices" @click="section = 'devices'">
        Устройства <span v-if="enrollments.length" class="badge warn">{{ enrollments.length }}</span>
      </button>
      <button type="button" :class="{ active: section === 'orgs' }" data-section="orgs" @click="section = 'orgs'">Организации</button>
      <button type="button" :class="{ active: section === 'nodes' }" data-section="nodes" @click="section = 'nodes'">Router</button>
      <button type="button" :class="{ active: section === 'usage' }" data-section="usage" @click="section = 'usage'">Расход</button>
    </nav>
    <span class="spacer" />
    <details class="access">
      <summary>{{ token ? 'Ключ доступа задан' : 'Ключ доступа' }}</summary>
      <form class="row" @submit.prevent="applyToken">
        <label for="gate-token" class="muted">X-Atlas-Token</label>
        <input id="gate-token" v-model="token" type="password" autocomplete="off" size="24" />
        <button type="submit" class="small primary">Применить</button>
      </form>
      <p class="muted hint">Ключ администратора Gate обязателен и на локальной машине.</p>
    </details>
  </header>
  <main v-if="section === 'orgs'">
    <OrgsSection />
  </main>
  <main v-else-if="section === 'nodes'"><NodesSection /></main>
  <main v-else-if="section === 'usage'"><UsageSection /></main>
  <main v-else>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="notice" class="note">{{ notice }}</p>

    <section class="card">
      <h2>Ожидающие регистрации <span class="badge neutral">{{ enrollments.length }}</span>
        <span class="spacer" /><button class="small" :disabled="busy" data-act="refresh" @click="refresh">Обновить</button></h2>
      <p v-if="!enrollments.length" class="muted">Новых регистраций нет. Заявка появится здесь, когда сотрудник начнёт вход в Atlas Harness и назовёт код.</p>
      <div v-for="e in enrollments" :key="e.user_code" class="enroll" :class="{ mark: e.user_code === highlighted }">
        <div class="row">
          <code>{{ e.user_code }}</code>
          <span>{{ e.device_name }}</span>
          <span class="muted">{{ e.platform }} · {{ e.app_version }} · до {{ when(e.expires_at) }}</span>
        </div>
        <form class="row" @submit.prevent="approve(e)">
          <input v-model="form(e.user_code).id" type="text" placeholder="id сотрудника" size="12" />
          <input v-model="form(e.user_code).email" type="text" placeholder="почта" size="18" />
          <input v-model="form(e.user_code).name" type="text" placeholder="имя" size="14" />
          <select v-model="form(e.user_code).org">
            <option v-for="o in orgs" :key="o" :value="o">{{ o }}</option>
          </select>
          <button type="submit" class="primary" :disabled="busy">Одобрить</button>
          <button type="button" class="danger" :disabled="busy" @click="deny(e)">Отклонить</button>
        </form>
      </div>
    </section>

    <section class="card">
      <h2>Устройства <span class="badge neutral">{{ devices.length }}</span></h2>
      <div class="table-wrap">
        <table v-if="devices.length">
          <thead>
            <tr><th>Устройство</th><th>Сотрудник</th><th>Организация</th><th>Создано</th><th>Состояние</th><th></th></tr>
          </thead>
          <tbody>
            <tr v-for="d in devices" :key="d.device_id">
              <td>{{ d.device_name }}<br /><span class="muted">{{ d.platform }} · {{ d.app_version }}</span></td>
              <td>{{ d.user_name || d.user_id }}<br /><span class="muted">{{ d.user_email }}</span></td>
              <td>{{ d.org }}</td>
              <td class="muted">{{ when(d.created_at) }}</td>
              <td>
                <span class="badge" :class="d.revoked_at ? 'bad' : 'ok'">{{ d.revoked_at ? 'отозвано' : 'активно' }}</span>
              </td>
              <td class="num">
                <button v-if="!d.revoked_at" class="small danger" :disabled="busy" @click="revoke(d)">Отозвать</button>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="muted">Устройств пока нет — они появятся после одобрения регистрации.</p>
      </div>
    </section>

  </main>
  </div>
</template>

<style scoped>
.sections {
  display: flex;
  gap: 6px;
  margin: 0 0 12px;
}
.enroll {
  border-top: 1px solid var(--border);
  padding: 8px 0;
}
.enroll.mark {
  background: var(--warn-bg);
}
select {
  font: inherit;
  padding: 4px 6px;
}
</style>

<style>
#app:has(.gate-shell) {
  max-width: 1440px;
}
.gate-shell .shell-head {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 4px 0 12px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--border);
}
.gate-shell .brand {
  font-size: 17px;
}
.gate-shell .shell-nav {
  display: flex;
  gap: 4px;
}
.gate-shell .shell-nav button {
  border: 0;
  background: transparent;
  padding: 6px 12px;
  border-radius: var(--radius);
  font-size: 14px;
}
.gate-shell .shell-nav button.active {
  background: var(--surface);
  box-shadow: inset 0 -2px 0 var(--accent);
  font-weight: 600;
}
.gate-shell .spacer {
  flex: 1;
}
.gate-shell .access summary {
  cursor: pointer;
  color: var(--muted);
  font-size: 13px;
}
.gate-shell .access[open] {
  position: relative;
}
.gate-shell .access form,
.gate-shell .access .hint {
  position: absolute;
  right: 0;
  z-index: 10;
  background: var(--surface);
}
.gate-shell .access form {
  top: 24px;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.08);
  white-space: nowrap;
}
.gate-shell .access .hint {
  top: 76px;
  font-size: 12px;
  padding: 0 10px;
  white-space: nowrap;
}
</style>
