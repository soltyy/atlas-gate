<script setup lang="ts">
// Раздел «Организации»: список (версия, подпись, счётчики, предупреждения, архив) → редактор выбранной;
// «Новая организация» — из шаблона или копией (#7).
import { onMounted, ref } from 'vue'
import NewOrgDialog from './NewOrgDialog.vue'
import OrgEditor from './OrgEditor.vue'
import { gateApi } from '../api'
import { orgApi } from './api'
import type { OrgSummary } from './types'

const orgs = ref<OrgSummary[]>([])
const errors = ref<Record<string, string>>({})
const failure = ref('')
const open = ref<string | null>(null)
const creating = ref(false)

async function refresh() {
  try {
    const r = await orgApi.list()
    orgs.value = r.orgs
    errors.value = r.errors
    failure.value = ''
  } catch (e) {
    failure.value = (e as Error).message
  }
}

function close() {
  open.value = null
  void refresh()
}

// Правку файла руками гейт подхватывает «Перечитать с диска» (раньше — отдельная карточка на главной, #12).
const reloadNote = ref('')
async function reloadFromDisk() {
  try {
    const r = await gateApi.reload()
    const bad = Object.keys(r.errors)
    reloadNote.value = `Перечитано: ${Object.entries(r.orgs).map(([o, v]) => `${o} v${v}`).join(', ') || 'организаций нет'}`
      + (bad.length ? `; отвергнуто: ${bad.join(', ')}` : '')
    await refresh()
  } catch (e) {
    failure.value = (e as Error).message
  }
}

function created(id: string) {
  creating.value = false
  open.value = id
}

function when(ts: number | null): string {
  return ts ? new Date(ts * 1000).toLocaleString() : '—'
}

onMounted(() => void refresh())
</script>

<template>
  <OrgEditor v-if="open" :key="open" :org-id="open" @close="close" />
  <section v-else class="card">
    <h2>
      Организации <span class="badge neutral">{{ orgs.length }}</span>
      <span class="spacer" />
      <button type="button" class="small" data-act="reload" title="Подхватить правки файлов gate/orgs/*.json, сделанные вручную" @click="reloadFromDisk">Перечитать с диска</button>
      <button v-if="!creating" type="button" class="small primary" data-act="new-org" @click="creating = true">+ Новая организация</button>
    </h2>
    <NewOrgDialog v-if="creating" :orgs="orgs" @created="created" @cancel="creating = false" />
    <p v-if="failure" class="error">{{ failure }}</p>
    <p v-if="reloadNote" class="note" data-reload-note>{{ reloadNote }}</p>
    <div class="table-wrap">
      <table v-if="orgs.length">
        <thead>
          <tr><th>Организация</th><th>Версия</th><th>Подписан</th><th>Маршруты / модели / агенты</th><th>Устройства</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="o in orgs" :key="o.org" :data-org="o.org" :class="{ archived: o.archived }">
            <td>
              {{ o.name }} <span v-if="o.archived" class="badge neutral" data-archived>архив</span><br /><code>{{ o.org }}</code>
              <details v-if="o.warnings.length" class="muted small-text">
                <summary>⚠ предупреждений: {{ o.warnings.length }}</summary>
                <div v-for="(w, i) in o.warnings" :key="i">{{ w }}</div>
              </details>
            </td>
            <td class="num">{{ o.profile_version ?? '—' }}</td>
            <td class="muted">{{ when(o.signed_at) }}</td>
            <td>{{ o.counts.routes }} / {{ o.counts.models }} / {{ o.counts.agents }}</td>
            <td class="num">{{ o.counts.devices }}</td>
            <td class="num"><button type="button" class="small primary" :data-open="o.org" @click="open = o.org">Открыть</button></td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="!failure" class="muted">нет загруженных организаций</p>
    </div>
    <p v-for="(msg, org) in errors" :key="org" class="error">{{ org }}: файл отвергнут — {{ msg }}</p>
  </section>
</template>

<style scoped>
.small-text {
  font-size: 12px;
}
tr.archived td {
  color: var(--muted);
  background: var(--bg);
}
</style>
