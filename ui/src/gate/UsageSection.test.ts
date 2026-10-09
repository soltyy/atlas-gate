import { mount } from '@vue/test-utils'
import { afterEach, expect, it, vi } from 'vitest'
import { flush, jsonResponse } from '../test-utils'
import UsageSection from './UsageSection.vue'
afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear() })
const summary = { records: 2, legacyRecords: 1, totalInputTokens: 100, totalInputTokensRecords: 1, uncachedInputTokens: null, uncachedInputTokensRecords: 0, cacheReadTokens: 0, cacheReadTokensRecords: 1, cacheWriteTokens: null, cacheWriteTokensRecords: 0, outputTokens: 3, outputTokensRecords: 2, cacheReadPercent: 0, cacheComparableRecords: 1 }
const row = { id: 2, ts: 1, deviceId: 'device', deviceName: 'EDT', model: 'gpt-6.1-sol', route: 'sub', source: 'router-sdk', status: 'success', totalInputTokens: 100, uncachedInputTokens: null, cacheReadTokens: 0, cacheWriteTokens: null, outputTokens: 3, durationMs: 1000, sessionId: 'session', turnId: 'turn', modelUsage: {} }
it('показывает ноль отдельно от неизвестного и сохраняет фильтры/период пагинации', async () => {
  sessionStorage.setItem('atlas.gate.adminToken', 'admin')
  const fetcher = vi.fn(async (_url: string, _init?: RequestInit) => jsonResponse({ summary, groups: [{ ...summary, deviceId: 'device', deviceName: 'EDT', model: 'gpt-6.1-sol', kind: 'agent' }], rows: [row], nextBefore: 2 }))
  vi.stubGlobal('fetch', fetcher)
  const w = mount(UsageSection); await flush()
  expect(w.text()).toContain('нет данных')
  expect(w.text()).toContain('Без чтения кеша')
  expect(w.text()).toContain('0.0 %')
  expect(w.text()).toContain('Исторических с неполной детализацией: 1')
  const params = new URL(fetcher.mock.calls[0][0] as string, 'http://localhost').searchParams
  await w.findAll('button').find(b => b.text() === 'Показать ещё')!.trigger('click'); await flush()
  const second = new URL(fetcher.mock.calls[1][0] as string, 'http://localhost').searchParams
  expect(second.get('before')).toBe('2'); expect(second.get('until')).toBe(params.get('until'))
  expect((fetcher.mock.calls[0][1] as RequestInit).headers).toMatchObject({ 'X-Atlas-Token': 'admin' })
  await w.findAll('input')[2].setValue('opus'); await w.find('form').trigger('submit'); await flush()
  expect(new URL(fetcher.mock.calls[2][0] as string, 'http://localhost').searchParams.get('model')).toBe('opus')
})
it('отображает отказ API и позволяет повторить запрос', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ error: { message: 'Нет доступа' } }, 401)))
  const w = mount(UsageSection); await flush()
  expect(w.find('[role=alert]').text()).toBe('Нет доступа')
  expect(w.find('button').attributes('disabled')).toBeUndefined()
})
