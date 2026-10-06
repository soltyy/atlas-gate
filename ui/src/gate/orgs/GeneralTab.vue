<script setup lang="ts">
// Общее (#6, UX — #12): подписи над полями, пояснения к разрешениям, флаги — JSON с проверкой.
import { ref } from 'vue'
import FieldErr from './FieldErr.vue'
import type { OrgConfig } from './types'
import { numberOrNull } from './util'

const props = defineProps<{ org: OrgConfig; errors: Record<string, string> }>()

const overrideNames = [
  ['allow_personal_agents', 'Свои агенты', 'пользователь может заводить личных агентов у себя'],
  ['allow_local_mcp', 'Свои MCP-серверы', 'пользователь может подключать MCP-серверы на своей машине'],
  ['allow_local_routes', 'Свои маршруты', 'пользователь может ходить к своим провайдерам мимо гейта (только автономный режим)'],
  ['allow_data_class_raise', 'Поднимать класс данных', 'пользователь может сам поднять класс данных сессии'],
  ['allow_workspace_root', 'Корень рабочей области', 'пользователь может сменить папку, с которой работает агент'],
] as const

const flagsError = ref('')
function setFlags(text: string) {
  try {
    const v = text.trim() ? JSON.parse(text) : {}
    if (!v || typeof v !== 'object' || Array.isArray(v)) throw new Error('нужен JSON-объект, например {"new_ui": true}')
    props.org.feature_flags = v
    flagsError.value = ''
  } catch (e) {
    flagsError.value = `флаги: ${(e as Error).message}`
  }
}

function setTurns(text: string) {
  const n = numberOrNull(text)
  props.org.quota.agent_turns_per_day = n === null ? null : Math.trunc(n)
}
</script>

<template>
  <section class="sub">
    <h4>Организация</h4>
    <div class="fields two">
      <label class="field">
        <span class="label">Название</span>
        <input v-model.trim="org.name" type="text" data-f="name" />
        <FieldErr :errors="errors" loc="name" />
      </label>
      <label class="field">
        <span class="label">id <span class="muted">— имя файла, не меняется</span></span>
        <input :value="org.id" type="text" readonly />
      </label>
    </div>
  </section>

  <section class="sub">
    <h4>Квота на пользователя</h4>
    <div class="fields three">
      <label class="field">
        <span class="label">Лимит в месяц</span>
        <input :value="org.quota.limit ?? ''" type="text" placeholder="без лимита" data-f="quota.limit"
          @change="org.quota.limit = numberOrNull(($event.target as HTMLInputElement).value)" />
        <span class="muted hint">календарный месяц UTC; пусто — без лимита</span>
      </label>
      <label class="field">
        <span class="label">Валюта</span>
        <input v-model.trim="org.quota.currency" type="text" data-f="quota.currency" />
      </label>
      <label class="field">
        <span class="label">Ходов агента в день</span>
        <input :value="org.quota.agent_turns_per_day ?? ''" type="text" placeholder="без лимита" data-f="quota.agent_turns_per_day"
          @change="setTurns(($event.target as HTMLInputElement).value)" />
      </label>
    </div>
    <FieldErr :errors="errors" loc="quota" deep />
  </section>

  <section class="sub">
    <h4>Что пользователь может менять у себя</h4>
    <div class="overrides">
      <label v-for="[k, title, help] in overrideNames" :key="k" class="override">
        <input v-model="org.overrides[k]" type="checkbox" :data-f="`overrides.${k}`" />
        <span><strong>{{ title }}</strong><br /><span class="muted hint">{{ help }}</span></span>
      </label>
    </div>
    <label class="override">
      <input v-model="org.audit.include_messages" type="checkbox" data-f="audit.include_messages" />
      <span><strong>Аудит с текстом сообщений</strong><br /><span class="muted hint">в журнал аудита попадает текст переписки, а не только события</span></span>
    </label>
  </section>

  <section class="sub">
    <h4>Приложение</h4>
    <div class="fields three">
      <label class="field">
        <span class="label">Минимальная версия</span>
        <input v-model.trim="org.min_app_version" type="text" data-f="min_app_version" />
        <span class="muted hint">ниже — приложение не запустится</span>
      </label>
      <label class="field">
        <span class="label">Рекомендуемая версия</span>
        <input v-model.trim="org.recommended_app_version" type="text" data-f="recommended_app_version" />
      </label>
      <label class="field">
        <span class="label">Канал обновлений (URL)</span>
        <input v-model.trim="org.update_channel_url" type="text" data-f="update_channel_url" />
      </label>
    </div>
    <FieldErr :errors="errors" loc="min_app_version" />
    <FieldErr :errors="errors" loc="update_channel_url" />
  </section>

  <details class="sub">
    <summary><strong>Для разработчиков: флаги функций (JSON)</strong></summary>
    <textarea :value="JSON.stringify(org.feature_flags, null, 2)" rows="4" data-f="feature_flags"
      @change="setFlags(($event.target as HTMLTextAreaElement).value)" />
    <span v-if="flagsError" class="field-error">{{ flagsError }}</span>
    <FieldErr :errors="errors" loc="feature_flags" deep />
  </details>
</template>

<style scoped>
.overrides {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px 20px;
  margin-bottom: 10px;
}
.override {
  display: flex;
  gap: 8px;
  align-items: flex-start;
}
.override input {
  margin-top: 3px;
}
.field-error {
  color: var(--bad);
  font-size: 12px;
}
</style>
