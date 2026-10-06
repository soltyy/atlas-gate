<script setup lang="ts">
// Вкладка 6 «MCP»: id, имя, адрес, имя переменной заголовков (+ задана ли), обязательный,
// разрешённые инструменты, ref_fields/display_fields по инструменту («инструмент: поле, поле» по строке).
import { reactive } from 'vue'
import FieldErr from './FieldErr.vue'
import type { EditorContext, Mcp, OrgConfig } from './types'
import { formatFieldMap, joinList, parseFieldMap, splitList } from './util'

const props = defineProps<{ org: OrgConfig; errors: Record<string, string>; ctx: EditorContext }>()

function add() {
  props.org.mcp.push({
    id: '', name: '', transport: 'http', url: '', headers_ref: null, required: false, tools_allowlist: [],
    command: null, args: [], cwd: null, env_ref: [], lifecycle: 'run',
    ref_fields: {}, display_fields: {},
  })
}

const argsDraft = reactive(new Map<Mcp, string>())
const argsErrors = reactive(new Map<Mcp, string>())

function setArgs(m: Mcp, text: string) {
  argsDraft.set(m, text)
  try {
    const args = JSON.parse(text)
    if (!Array.isArray(args) || args.some(v => typeof v !== 'string')) throw new Error('args')
    m.args = args
    argsErrors.delete(m)
  } catch {
    m.args = null // Сервер отклонит сохранение, пока пользователь не исправит JSON.
    argsErrors.set(m, 'Нужен JSON-массив строк')
  }
}

function setTransport(m: Mcp, transport: Mcp['transport']) {
  m.transport = transport
  if (transport === 'stdio' && m.args == null && !argsErrors.has(m)) m.args = []
}

function remove(i: number) {
  const m = props.org.mcp[i]
  argsDraft.delete(m)
  argsErrors.delete(m)
  props.org.mcp.splice(i, 1)
}

function setHeadersRef(m: Mcp, text: string) {
  m.headers_ref = text.trim() || null
}
</script>

<template>
  <FieldErr :errors="errors" loc="mcp" />
  <p v-if="!org.mcp.length" class="muted">MCP-серверов нет</p>
  <section v-for="(m, i) in org.mcp" :key="i" class="sub">
    <div class="grid">
      <label>id <input v-model.trim="m.id" type="text" size="12" :data-f="`mcp.${i}.id`" /></label>
      <label>имя <input v-model.trim="m.name" type="text" size="20" :data-f="`mcp.${i}.name`" /></label>
      <label>транспорт <select :value="m.transport" :data-f="`mcp.${i}.transport`" @change="setTransport(m, ($event.target as HTMLSelectElement).value as Mcp['transport'])"><option value="http">HTTP</option><option value="stdio">stdio</option></select></label>
      <label>жизненный цикл <select v-model="m.lifecycle" :data-f="`mcp.${i}.lifecycle`"><option value="run">на вызов</option><option value="session">на сессию</option></select></label>
      <label v-if="m.transport === 'http'">адрес <input v-model.trim="m.url" type="text" size="36" :data-f="`mcp.${i}.url`" /></label>
      <label v-if="m.transport === 'http'">заголовки (имя переменной)
        <input :value="m.headers_ref ?? ''" type="text" size="16" :data-f="`mcp.${i}.headers_ref`"
          @change="setHeadersRef(m, ($event.target as HTMLInputElement).value)" />
        <span v-if="m.headers_ref" class="badge" :class="ctx.secrets[m.headers_ref] ? 'ok' : 'bad'">{{ ctx.secrets[m.headers_ref] ? 'задан' : 'нет' }}</span>
      </label>
      <label><input v-model="m.required" type="checkbox" :data-f="`mcp.${i}.required`" /> обязательный</label>
      <button type="button" class="small" @click="remove(i)">Убрать</button>
    </div>
    <div v-if="m.transport === 'stdio'" class="grid">
      <label>команда <input v-model.trim="m.command" :data-f="`mcp.${i}.command`" /></label>
      <label>args (JSON-массив строк) <input :value="argsDraft.get(m) ?? JSON.stringify(m.args ?? [])" :data-f="`mcp.${i}.args`" @change="setArgs(m, ($event.target as HTMLInputElement).value)" /></label>
      <p v-if="argsErrors.has(m)" class="bad">{{ argsErrors.get(m) }}</p>
      <label>cwd <input v-model.trim="m.cwd" :data-f="`mcp.${i}.cwd`" /></label>
      <label>env_ref (имена через запятую) <input :value="joinList(m.env_ref ?? [])" :data-f="`mcp.${i}.env_ref`" @change="m.env_ref = splitList(($event.target as HTMLInputElement).value)" /></label>
      <p class="muted">Значения секретов вводятся только на устройстве в Settings MCP.</p>
    </div>
    <FieldErr :errors="errors" :loc="`mcp.${i}`" deep />
    <label class="block">инструменты (через запятую)
      <input :value="joinList(m.tools_allowlist)" type="text" size="60" :data-f="`mcp.${i}.tools_allowlist`"
        @change="m.tools_allowlist = splitList(($event.target as HTMLInputElement).value)" />
    </label>
    <div class="grid two">
      <label class="block">ref_fields
        <textarea :value="formatFieldMap(m.ref_fields)" rows="3" placeholder="инструмент: поле, поле" :data-f="`mcp.${i}.ref_fields`"
          @change="m.ref_fields = parseFieldMap(($event.target as HTMLTextAreaElement).value)" />
      </label>
      <label class="block">display_fields
        <textarea :value="formatFieldMap(m.display_fields)" rows="3" placeholder="инструмент: поле, поле" :data-f="`mcp.${i}.display_fields`"
          @change="m.display_fields = parseFieldMap(($event.target as HTMLTextAreaElement).value)" />
      </label>
    </div>
  </section>
  <button type="button" class="small" data-act="add-mcp" @click="add">Добавить MCP</button>
</template>
