<script setup lang="ts">
// История версий организации: список, двухколоночный дифф «версия N ↔ текущая», откат
// (POST …/restore — как сохранение этой версии: новая версия профиля, прежние остаются в истории).
import { computed, onMounted, ref } from 'vue'
import { orgApi } from './api'
import type { HistoryEntry, OrgConfig, Saved } from './types'
import { diffLines } from './util'

const props = defineProps<{ orgId: string; current: OrgConfig; currentVersion: number | null }>()
const emit = defineEmits<{ restored: [saved: Saved]; error: [message: string] }>()

const versions = ref<HistoryEntry[]>([])
const picked = ref<number | null>(null)
const pickedConfig = ref<OrgConfig | null>(null)
const onlyChanges = ref(true)
const busy = ref(false)

const rows = computed(() => {
  if (!pickedConfig.value) return []
  const all = diffLines(JSON.stringify(pickedConfig.value, null, 2), JSON.stringify(props.current, null, 2))
  return onlyChanges.value ? all.filter((r) => r.kind !== 'same') : all
})

async function load() {
  try {
    versions.value = (await orgApi.history(props.orgId)).versions
  } catch (e) {
    emit('error', (e as Error).message)
  }
}

async function pick(v: number) {
  picked.value = v
  try {
    pickedConfig.value = await orgApi.version(props.orgId, v)
  } catch (e) {
    pickedConfig.value = null
    emit('error', (e as Error).message)
  }
}

async function restore() {
  if (picked.value === null) return
  if (!window.confirm(`Откатить ${props.orgId} к версии ${picked.value}? Будет создана новая версия профиля.`)) return
  busy.value = true
  try {
    const saved = await orgApi.restore(props.orgId, picked.value)
    emit('restored', saved)
    picked.value = null
    pickedConfig.value = null
    await load()
  } catch (e) {
    emit('error', (e as Error).message)
  } finally {
    busy.value = false
  }
}

function when(ts: number): string {
  return new Date(ts * 1000).toLocaleString()
}

onMounted(() => void load())
defineExpose({ load })
</script>

<template>
  <section class="history">
    <h3>История версий</h3>
    <p v-if="!versions.length" class="muted">история пуста</p>
    <ul class="versions">
      <li v-for="h in versions" :key="h.version">
        <button type="button" class="small" :class="{ primary: h.version === picked }" :data-version="h.version" @click="pick(h.version)">
          v{{ h.version }}
        </button>
        <span class="muted">{{ when(h.saved_at) }}</span>
        <span v-if="h.version === currentVersion" class="badge ok">текущая</span>
      </li>
    </ul>
    <template v-if="pickedConfig">
      <div class="row">
        <strong>v{{ picked }} ↔ текущая</strong>
        <label><input v-model="onlyChanges" type="checkbox" /> только изменения</label>
        <span class="spacer" />
        <button type="button" class="danger small" data-act="rollback" :disabled="busy || picked === currentVersion" @click="restore">
          Откатить к v{{ picked }}
        </button>
      </div>
      <p v-if="!rows.length" class="muted">совпадает с текущей</p>
      <table v-else class="diff">
        <thead><tr><th>v{{ picked }}</th><th>текущая</th></tr></thead>
        <tbody>
          <tr v-for="(r, i) in rows" :key="i" :class="r.kind">
            <td><pre>{{ r.left ?? '' }}</pre></td>
            <td><pre>{{ r.right ?? '' }}</pre></td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>

<style scoped>
.versions {
  list-style: none;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
}
.versions li {
  display: flex;
  align-items: center;
  gap: 6px;
}
.diff {
  table-layout: fixed;
  width: 100%;
}
.diff pre {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 12px;
}
.diff tr.del td:first-child,
.diff tr.mod td:first-child {
  background: var(--bad-bg);
}
.diff tr.add td:last-child,
.diff tr.mod td:last-child {
  background: var(--ok-bg);
}
.spacer {
  flex: 1;
}
</style>
