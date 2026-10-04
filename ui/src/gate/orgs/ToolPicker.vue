<script setup lang="ts">
// Инструменты агента (#12): режим «Все — включая добавленные позже» или «Только выбранные» с
// каталогом галочками, «Выбрать все / Снять все», поиском и своим именем — как «All / Only select»
// у GitHub. «Все» пишется как ["*", <каталог поимённо>]: клиент без поддержки «*» получит список.
import { computed, ref } from 'vue'
import { ALL_TOOLS, allTools } from './util'

const props = defineProps<{ modelValue: string[]; catalog: string[]; disabled?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string[]] }>()

const isAll = computed(() => props.modelValue.includes(ALL_TOOLS))
const selected = computed(() => props.modelValue.filter((t) => t !== ALL_TOOLS))
const options = computed(() => [...new Set([...props.catalog, ...selected.value])])
const query = ref('')
const shown = computed(() => {
  const q = query.value.trim().toLowerCase()
  return q ? options.value.filter((t) => t.toLowerCase().includes(q)) : options.value
})
const custom = ref('')

function setMode(all: boolean) {
  // Из «все» в «выбранные» — все остаются отмеченными: снять лишнее проще, чем отметить заново.
  emit('update:modelValue', all ? allTools(options.value) : [...options.value])
}

function toggle(tool: string, on: boolean) {
  const next = on ? [...new Set([...selected.value, tool])] : selected.value.filter((t) => t !== tool)
  emit('update:modelValue', next)
}

function addCustom() {
  const name = custom.value.trim()
  if (!name || name === ALL_TOOLS) return
  custom.value = ''
  emit('update:modelValue', isAll.value ? allTools([...options.value, name]) : [...new Set([...selected.value, name])])
}
</script>

<template>
  <fieldset class="tool-picker" :disabled="disabled">
    <legend class="label">Инструменты</legend>
    <label class="mode">
      <input type="radio" :checked="isAll" data-mode="all" @change="setMode(true)" />
      <span><strong>Все инструменты</strong> — включая добавленные позже
        <span class="muted">(что разрешают встроенные инструменты и политики)</span></span>
    </label>
    <label class="mode">
      <input type="radio" :checked="!isAll" data-mode="selected" @change="setMode(false)" />
      <span><strong>Только выбранные</strong>
        <span v-if="!isAll" class="muted"> — выбрано {{ selected.length }} из {{ options.length }}</span></span>
    </label>

    <div v-if="!isAll" class="picker">
      <div class="row">
        <input v-if="options.length > 8" v-model="query" type="search" placeholder="Найти инструмент" class="search" />
        <button type="button" class="small" data-act="tools-all" @click="emit('update:modelValue', [...options])">Выбрать все</button>
        <button type="button" class="small" data-act="tools-none" @click="emit('update:modelValue', [])">Снять все</button>
      </div>
      <div class="grid-checks">
        <label v-for="t in shown" :key="t" class="check">
          <input type="checkbox" :checked="selected.includes(t)" :data-tool="t" @change="toggle(t, ($event.target as HTMLInputElement).checked)" />
          <code>{{ t }}</code>
        </label>
      </div>
      <p v-if="!selected.length" class="warn-text" role="status">Ни одного инструмента — агент сможет только отвечать текстом.</p>
    </div>
    <div class="row add">
      <input v-model="custom" type="text" placeholder="имя инструмента, которого нет в списке" data-f="tool-custom" @keydown.enter.prevent="addCustom" />
      <button type="button" class="small" data-act="tool-add" @click="addCustom">Добавить</button>
    </div>
  </fieldset>
</template>

<style scoped>
.tool-picker {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
  margin: 0;
  min-width: 0;
}
.label {
  font-weight: 600;
  padding: 0 4px;
}
.mode {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 4px 0;
}
.picker {
  margin: 6px 0 0 24px;
}
.grid-checks {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 4px 12px;
  margin: 8px 0;
}
.check {
  display: flex;
  gap: 6px;
  align-items: center;
}
.search {
  min-width: 220px;
}
.add {
  margin-top: 8px;
}
.add input {
  min-width: 280px;
}
.warn-text {
  color: var(--warn);
  font-size: 12px;
  margin: 0;
}
</style>
