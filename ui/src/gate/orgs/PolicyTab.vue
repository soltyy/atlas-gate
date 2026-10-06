<script setup lang="ts">
// Политика (#6, UX — #12): классы данных чипами, матрица классы × маршруты с «вся строка / весь
// столбец», политики инструментов с выбором имени из каталога, встроенные инструменты — переключателями;
// явное пояснение, как встроенные и политики сочетаются.
import { computed, ref } from 'vue'
import FieldErr from './FieldErr.vue'
import type { OrgConfig, ToolPolicy } from './types'
import { BUILTIN_TOOLS, toolCatalog } from './util'

const props = defineProps<{ org: OrgConfig; errors: Record<string, string> }>()

const catalog = computed(() => toolCatalog(props.org))
const newClass = ref('')

function allowed(cls: string, route: string): boolean {
  return (props.org.policy.routes_by_class[cls] ?? []).includes(route)
}

function setAllowed(cls: string, route: string, on: boolean) {
  const list = props.org.policy.routes_by_class[cls] ?? []
  const next = on ? [...new Set([...list, route])] : list.filter((r) => r !== route)
  // Порядок маршрутов — как в списке маршрутов: дифф истории не пляшет от порядка кликов.
  props.org.policy.routes_by_class[cls] = props.org.routes.map((r) => r.id).filter((id) => next.includes(id))
}

function rowAll(cls: string): boolean {
  return props.org.routes.every((r) => allowed(cls, r.id))
}
function setRow(cls: string, on: boolean) {
  for (const r of props.org.routes) setAllowed(cls, r.id, on)
}
function colAll(route: string): boolean {
  return props.org.policy.data_classes.every((c) => allowed(c, route))
}
function setCol(route: string, on: boolean) {
  for (const c of props.org.policy.data_classes) setAllowed(c, route, on)
}

function addClass() {
  const name = newClass.value.trim()
  const p = props.org.policy
  if (!name || p.data_classes.includes(name)) return
  p.data_classes.push(name)
  newClass.value = ''
}

function removeClass(cls: string) {
  const p = props.org.policy
  p.data_classes = p.data_classes.filter((c) => c !== cls)
  delete p.routes_by_class[cls]
}

function moveClass(i: number, delta: number) {
  const list = props.org.policy.data_classes
  const j = i + delta
  if (j < 0 || j >= list.length) return
  ;[list[i], list[j]] = [list[j], list[i]]
}

function addTool() {
  const classes = props.org.policy.data_classes
  const t: ToolPolicy = { tool: '', mode: 'ask', max_data_class: classes[classes.length - 1] ?? '', escalatable: false }
  props.org.policy.tool_policies.push(t)
}

function removeTool(i: number) {
  props.org.policy.tool_policies.splice(i, 1)
}
</script>

<template>
  <section class="sub">
    <h4>Классы данных</h4>
    <p class="muted hint">По возрастанию чувствительности: первый — самый открытый. Класс — метка сессии; ниже решается, какие маршруты ему разрешены.</p>
    <div class="class-chips" data-f="policy.data_classes">
      <span v-for="(c, i) in org.policy.data_classes" :key="c" class="class-chip" :data-class="c">
        <button type="button" class="icon" :disabled="i === 0" title="Раньше" @click="moveClass(i, -1)">‹</button>
        <code>{{ c }}</code>
        <button type="button" class="icon" :disabled="i === org.policy.data_classes.length - 1" title="Позже" @click="moveClass(i, 1)">›</button>
        <button type="button" class="icon" :disabled="org.policy.data_classes.length === 1" :title="`Убрать класс ${c}`" @click="removeClass(c)">×</button>
      </span>
      <input v-model.trim="newClass" type="text" size="14" placeholder="новый класс" data-f="policy.new_class" @keydown.enter.prevent="addClass" />
      <button type="button" class="small" data-act="add-class" @click="addClass">Добавить</button>
    </div>
    <FieldErr :errors="errors" loc="policy.data_classes" deep />
    <label class="field narrow">
      <span class="label">Класс по умолчанию для новой сессии</span>
      <select v-model="org.policy.default_data_class" data-f="policy.default_data_class">
        <option v-for="c in org.policy.data_classes" :key="c" :value="c">{{ c }}</option>
      </select>
      <FieldErr :errors="errors" loc="policy.default_data_class" />
    </label>
  </section>

  <section class="sub">
    <h4>Маршруты по классам</h4>
    <p class="muted hint">Отметка — сессии этого класса можно отправлять по маршруту. «Все» в строке и столбце отмечает или снимает целиком.</p>
    <div class="table-wrap">
      <table class="edit matrix">
        <thead>
          <tr>
            <th>класс \ маршрут</th>
            <th v-for="r in org.routes" :key="r.id">
              {{ r.id }}
              <label class="all"><input type="checkbox" :checked="colAll(r.id)" :data-col="r.id" @change="setCol(r.id, ($event.target as HTMLInputElement).checked)" /> все</label>
            </th>
            <th>вся строка</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in org.policy.data_classes" :key="c">
            <th>{{ c }}</th>
            <td v-for="r in org.routes" :key="r.id">
              <label class="cell">
                <input type="checkbox" :checked="allowed(c, r.id)" :data-cell="`${c}|${r.id}`"
                  @change="setAllowed(c, r.id, ($event.target as HTMLInputElement).checked)" />
                <span class="muted">{{ allowed(c, r.id) ? 'разрешён' : 'нет' }}</span>
              </label>
            </td>
            <td><input type="checkbox" :checked="rowAll(c)" :data-row="c" @change="setRow(c, ($event.target as HTMLInputElement).checked)" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <FieldErr :errors="errors" loc="policy.routes_by_class" deep />
  </section>

  <section class="sub">
    <h4>Инструменты</h4>
    <p class="muted hint">
      Сначала действуют <strong>встроенные</strong>: «запрещён» закрывает инструмент для всех агентов. Для разрешённых
      <strong>политики</strong> уточняют: <code>auto</code> — без вопроса, <code>ask</code> — спросить пользователя, <code>deny</code> — запрещён;
      «потолок класса» — выше этого класса данных инструмент не работает. Какие инструменты видит агент — в его карточке.
    </p>
    <div class="builtins">
      <div v-for="b in BUILTIN_TOOLS" :key="b" class="builtin">
        <code>{{ b }}</code>
        <div class="seg" role="radiogroup" :aria-label="`встроенный ${b}`">
          <label :class="{ on: org.builtin_tools[b] === 'allow' }"><input v-model="org.builtin_tools[b]" type="radio" value="allow" :data-f="`builtin_tools.${b}`" /> разрешён</label>
          <label :class="{ on: org.builtin_tools[b] === 'deny' }"><input v-model="org.builtin_tools[b]" type="radio" value="deny" /> запрещён</label>
        </div>
      </div>
    </div>

    <datalist id="tool-catalog">
      <option v-for="t in catalog" :key="t" :value="t" />
    </datalist>
    <table class="edit">
      <thead><tr><th>инструмент</th><th>режим</th><th>потолок класса данных</th><th>пользователь может поднять</th><th></th></tr></thead>
      <tbody>
        <tr v-for="(t, i) in org.policy.tool_policies" :key="i">
          <td><input v-model.trim="t.tool" type="text" size="20" list="tool-catalog" :data-f="`policy.tool_policies.${i}.tool`" /><FieldErr :errors="errors" :loc="`policy.tool_policies.${i}.tool`" /></td>
          <td>
            <select v-model="t.mode" :data-f="`policy.tool_policies.${i}.mode`">
              <option value="auto">auto — без вопроса</option>
              <option value="ask">ask — спросить</option>
              <option value="deny">deny — запрещён</option>
            </select>
          </td>
          <td>
            <select v-model="t.max_data_class" :data-f="`policy.tool_policies.${i}.max_data_class`">
              <option v-for="c in org.policy.data_classes" :key="c" :value="c">{{ c }}</option>
            </select>
            <FieldErr :errors="errors" :loc="`policy.tool_policies.${i}.max_data_class`" />
          </td>
          <td><input v-model="t.escalatable" type="checkbox" /></td>
          <td><button type="button" class="small" :title="`Убрать политику ${t.tool}`" @click="removeTool(i)">Убрать</button></td>
        </tr>
      </tbody>
    </table>
    <button type="button" class="small" data-act="add-tool" @click="addTool">+ Политика инструмента</button>
  </section>
</template>

<style scoped>
.class-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  margin-bottom: 10px;
}
.class-chip {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 2px 6px;
  background: var(--bg);
}
.icon {
  border: 0;
  background: none;
  padding: 0 4px;
  cursor: pointer;
  color: var(--muted);
}
.icon:disabled {
  opacity: 0.3;
}
.narrow {
  max-width: 320px;
}
.matrix th .all {
  display: block;
  font-weight: normal;
  font-size: 12px;
}
.cell {
  display: inline-flex;
  gap: 6px;
  align-items: center;
}
.builtins {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 24px;
  margin: 8px 0 14px;
}
.builtin {
  display: flex;
  align-items: center;
  gap: 8px;
}
.seg {
  display: inline-flex;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}
.seg label {
  padding: 3px 10px;
  cursor: pointer;
}
.seg label.on {
  background: var(--accent);
  color: #fff;
}
.seg input {
  position: absolute;
  opacity: 0;
  pointer-events: none;
}
</style>
