<script setup lang="ts">
// «Новая организация» (#7): id (= имя файла: латиница, цифры, дефис), название, источник —
// шаблон или копия существующей. Ошибки сервера (409 занят, 422 по полям) — у полей.
import { computed, ref } from 'vue'
import { OrgApiError, orgApi } from './api'
import type { OrgSummary } from './types'

const props = defineProps<{ orgs: OrgSummary[] }>()
const emit = defineEmits<{ created: [id: string]; cancel: [] }>()

const id = ref('')
const name = ref('')
const source = ref('template')
const busy = ref(false)
const errors = ref<Record<string, string>>({})
const failure = ref('')

const ID_RE = /^[A-Za-z0-9-]{1,64}$/
const idHint = computed(() => (id.value && !ID_RE.test(id.value) ? 'только латиница, цифры и дефис' : ''))

async function submit() {
  errors.value = {}
  failure.value = ''
  if (!ID_RE.test(id.value.trim())) {
    errors.value = { id: 'только латиница, цифры и дефис (1–64 знака)' }
    return
  }
  if (!name.value.trim()) {
    errors.value = { name: 'нужно название' }
    return
  }
  busy.value = true
  try {
    await orgApi.create(id.value.trim(), name.value.trim(), source.value)
    emit('created', id.value.trim())
  } catch (e) {
    if (e instanceof OrgApiError && e.status === 409) errors.value = { id: e.message }
    else if (e instanceof OrgApiError && e.fields.length) {
      errors.value = Object.fromEntries(e.fields.map((f) => [f.loc || 'form', f.msg]))
    } else failure.value = (e as Error).message
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <form class="new-org" data-new-org @submit.prevent="submit">
    <h3>Новая организация</h3>
    <label>id
      <input v-model.trim="id" type="text" size="20" placeholder="acme" autocomplete="off" data-f="new.id" />
      <span class="muted">имя файла, потом не меняется</span>
    </label>
    <span v-if="errors.id || idHint" class="field-error" role="alert">{{ errors.id || idHint }}</span>
    <label>название <input v-model="name" type="text" size="30" data-f="new.name" /></label>
    <span v-if="errors.name" class="field-error" role="alert">{{ errors.name }}</span>
    <label>источник
      <select v-model="source" data-f="new.source">
        <option value="template">шаблон (только подписка claude-sub, агент по умолчанию, квота 50 USD)</option>
        <option v-for="o in props.orgs" :key="o.org" :value="`copy_of:${o.org}`">копия: {{ o.name }} ({{ o.org }})</option>
      </select>
    </label>
    <span v-if="errors.source" class="field-error" role="alert">{{ errors.source }}</span>
    <span v-if="errors.form" class="field-error" role="alert">{{ errors.form }}</span>
    <p v-if="failure" class="error">{{ failure }}</p>
    <div class="row">
      <button type="submit" class="primary" :disabled="busy" data-act="create">Создать</button>
      <button type="button" :disabled="busy" @click="emit('cancel')">Отмена</button>
    </div>
  </form>
</template>

<style scoped>
.new-org {
  display: flex;
  flex-direction: column;
  gap: 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px;
  margin: 8px 0 12px;
}
.new-org h3 {
  margin: 0 0 4px;
  font-size: 14px;
}
.new-org input,
.new-org select {
  font: inherit;
  padding: 3px 6px;
}
.field-error {
  color: var(--bad);
  font-size: 12px;
}
</style>
