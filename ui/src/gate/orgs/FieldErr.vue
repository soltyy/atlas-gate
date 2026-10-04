<script setup lang="ts">
// Ошибка сервера у поля: loc из ответа 422 (например «routes.0.kind»). deep — и все вложенные.
import { computed } from 'vue'

const props = defineProps<{ errors: Record<string, string>; loc: string; deep?: boolean }>()
const messages = computed(() =>
  Object.entries(props.errors)
    .filter(([l]) => l === props.loc || (props.deep && l.startsWith(props.loc + '.')))
    .map(([, m]) => m),
)
</script>

<template>
  <span v-for="(m, i) in messages" :key="i" class="field-error" role="alert">{{ m }}</span>
</template>

<style scoped>
.field-error {
  display: block;
  color: var(--bad);
  font-size: 12px;
}
</style>
